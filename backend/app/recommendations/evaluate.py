"""Offline evaluation of recommendation retrievers and rankers.

    python -m app.recommendations.evaluate [--boards PATH] [--out PATH]
        [--retrievers centroid,per-pin,...] [--rankers cosine,mmr,...]
        [--pool 20] [--k 10] [--judgments-cohesive CSV] [--judgments-complement CSV]
        [--export-human DIR]

Retrieval is evaluated first, with no ranker, and each board's pools are kept.
Every ranker is then scored on those identical pools, so ranking comparisons
are not confounded by retrieval. Cohesive metrics and complement diagnostics
are reported in separate sections. The results file is deterministic for a
fixed corpus, model, and board set; timings go to a separate latency file.

Benchmark boards are files of Met object ids, not user boards. The worker role
maps object ids to asset ids; everything else reads through the API role and
`ready_assets`, exactly as the endpoint does.
"""

import argparse
import csv
import hashlib
import json
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import text

from app.core.config import REPO_ROOT, Settings
from app.db.engine import create_api_engine, create_worker_engine
from app.embeddings.provider import build_provider
from app.recommendations import metrics
from app.recommendations.models import Candidate, CandidatePool, ProfilePin
from app.recommendations.rank import Ranker
from app.recommendations.repository import RecommendationRepository
from app.recommendations.retrieve import Retriever, merge
from app.recommendations.strategy import RANKERS, RETRIEVERS

DEFAULT_BOARDS = REPO_ROOT / "backend" / "evaluation" / "boards" / "v1.json"
DEFAULT_OUT = REPO_ROOT / "backend" / "evaluation" / "results" / "latest.json"
NEAR_DUPLICATE = 0.95
# Every asset, so pools and sheets name candidates by Met object id too.
EXTERNAL_IDS = """
SELECT source_records.external_id, assets.id
FROM assets JOIN source_records ON source_records.id = assets.source_record_id
"""


@dataclass(frozen=True)
class BenchmarkBoard:
    name: str
    kind: str
    pins: tuple[str, ...]


@dataclass(frozen=True)
class BoardSet:
    version: str
    digest: str
    labels: dict[str, str]
    boards: tuple[BenchmarkBoard, ...]


@dataclass
class Pools:
    full: CandidatePool
    # Held-out pin id -> pool retrieved without it. Boards with one pin have none.
    leave_one_out: dict[UUID, CandidatePool]


def load_boards(path: Path) -> BoardSet:
    raw = path.read_bytes()
    data = json.loads(raw)
    return BoardSet(
        version=data["version"],
        digest=hashlib.sha256(raw).hexdigest(),
        labels=dict(data["labels"]),
        boards=tuple(
            BenchmarkBoard(board["name"], board["kind"], tuple(board["pins"]))
            for board in data["boards"]
        ),
    )


def load_judgments(path: Path | None) -> dict[str, dict[str, float]]:
    """A graded sheet from --export-human: board, external_id, grade (blank = unjudged)."""
    if path is None:
        return {}
    judgments: dict[str, dict[str, float]] = {}
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            if (row.get("grade") or "").strip():
                judgments.setdefault(row["board"], {})[row["external_id"]] = float(row["grade"])
    return judgments


def _round(value):
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: _round(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round(item) for item in value]
    return value


def _params(component) -> dict:
    return asdict(component) if is_dataclass(component) else {"name": component.name}


class Evaluation:
    def __init__(
        self,
        repository: RecommendationRepository,
        model_key: str,
        model_version: str,
        board_set: BoardSet,
        asset_ids: Mapping[str, UUID],
        pool: int,
        k: int,
    ) -> None:
        self.repository = repository
        self.model_key = model_key
        self.model_version = model_version
        self.board_set = board_set
        self.asset_ids = dict(asset_ids)
        self.external = {asset_id: external for external, asset_id in self.asset_ids.items()}
        self.labels = {
            self.asset_ids[external]: label
            for external, label in board_set.labels.items()
            if external in self.asset_ids
        }
        self.pool = pool
        self.k = k
        self.timings: dict[str, dict[str, float]] = {}
        self.pins: dict[str, tuple[ProfilePin, ...]] = {}
        self.missing: dict[str, list[str]] = {}
        for board in board_set.boards:
            wanted = [self.asset_ids[pin] for pin in board.pins if pin in self.asset_ids]
            found = repository.pins_for(wanted, model_key, model_version)
            self.pins[board.name] = found
            present = {pin.asset_id for pin in found}
            missing = [pin for pin in board.pins if self.asset_ids.get(pin) not in present]
            if missing:
                self.missing[board.name] = missing

    def _retrieve(self, retriever: Retriever, pins: Sequence[ProfilePin]) -> CandidatePool:
        probes = retriever.probes(pins, self.pool)
        saved = frozenset(pin.asset_id for pin in pins)
        hits = self.repository.candidates(probes, saved, self.model_key, self.model_version)
        return merge(hits, probes, pins, saved)

    def retrieve(self, name: str, retriever: Retriever) -> dict[str, Pools]:
        pools: dict[str, Pools] = {}
        for board in self.board_set.boards:
            pins = self.pins[board.name]
            if not pins:
                continue
            started = time.perf_counter()
            full = self._retrieve(retriever, pins)
            self.timings.setdefault(name, {})[board.name] = time.perf_counter() - started
            held = {}
            if len(pins) >= 2:
                for pin in pins:
                    others = [other for other in pins if other.asset_id != pin.asset_id]
                    held[pin.asset_id] = self._retrieve(retriever, others)
            pools[board.name] = Pools(full, held)
        return pools

    def board_labels(self, board: BenchmarkBoard) -> set[str]:
        return {
            self.labels[pin.asset_id]
            for pin in self.pins[board.name]
            if pin.asset_id in self.labels
        }

    def retrieval_metrics(
        self, pools: dict[str, Pools], cohesive: dict[str, dict[str, float]]
    ) -> dict:
        boards = {}
        for board in self.board_set.boards:
            if board.name not in pools:
                continue
            entry = pools[board.name]
            ids = [c.card.id for c in entry.full.candidates]
            judged = self._grades(cohesive, board.name)
            relevant = [asset_id for asset_id, grade in judged.items() if grade >= 2]
            boards[board.name] = {
                "kind": board.kind,
                "probes": len(entry.full.probes),
                "pool_size": len(ids),
                "held_out_pool_recall": metrics.mean(
                    [
                        1.0 if target in {c.card.id for c in pool.candidates} else 0.0
                        for target, pool in entry.leave_one_out.items()
                    ]
                ),
                "judged_relevant_pool_recall": (
                    sum(asset_id in ids for asset_id in relevant) / len(relevant)
                    if relevant
                    else None
                ),
                "pool_interest_coverage": metrics.interest_coverage(
                    ids, self.labels, self.board_labels(board)
                ),
                "relevance_novelty_correlation": metrics.relevance_novelty_correlation(
                    entry.full.candidates
                ),
                "pool": [self.external.get(asset_id, str(asset_id)) for asset_id in ids],
            }
        return {"boards": boards, "mean": _means(boards)}

    def ranking_metrics(
        self,
        pools: dict[str, Pools],
        ranker: Ranker,
        cohesive: dict[str, dict[str, float]],
        complement: dict[str, dict[str, float]],
    ) -> dict:
        boards = {}
        recommended: set[UUID] = set()
        for board in self.board_set.boards:
            if board.name not in pools:
                continue
            entry = pools[board.name]
            top = [scored.candidate for scored in ranker.rank(entry.full, self.k)]
            ids = [c.card.id for c in top]
            recommended.update(ids)
            leave_one_out = [
                metrics.hit_and_reciprocal_rank(
                    [scored.candidate.card.id for scored in ranker.rank(pool, self.k)],
                    target,
                    self.k,
                )
                for target, pool in entry.leave_one_out.items()
            ]
            boards[board.name] = {
                "kind": board.kind,
                "top_k": [self.external.get(asset_id, str(asset_id)) for asset_id in ids],
                "cohesive": {
                    "held_out_hit_at_k": metrics.mean([hit for hit, _ in leave_one_out]),
                    "held_out_mrr": metrics.mean([rr for _, rr in leave_one_out]),
                    "ndcg_at_k": metrics.ndcg(ids, self._grades(cohesive, board.name), self.k),
                    "interest_coverage_at_k": metrics.interest_coverage(
                        ids, self.labels, self.board_labels(board)
                    ),
                },
                "complement": {
                    "human_mean_grade": _mean_grade(ids, self._grades(complement, board.name)),
                    "diagnostics": _diagnostics(top),
                },
            }
        return {
            "boards": boards,
            "mean": {
                "cohesive": _means({name: board["cohesive"] for name, board in boards.items()}),
                "complement": {
                    "human_mean_grade": metrics.mean(
                        [board["complement"]["human_mean_grade"] for board in boards.values()]
                    ),
                    "diagnostics": _means(
                        {name: board["complement"]["diagnostics"] for name, board in boards.items()}
                    ),
                },
            },
            "catalog_coverage": len(recommended) / max(1, len(self.corpus)),
        }

    def _grades(self, judgments: dict[str, dict[str, float]], board: str) -> dict[UUID, float]:
        return {
            self.asset_ids[external]: grade
            for external, grade in judgments.get(board, {}).items()
            if external in self.asset_ids
        }

    @property
    def corpus(self) -> list[UUID]:
        if not hasattr(self, "_corpus"):
            self._corpus = self.repository.corpus_ids(self.model_key, self.model_version)
        return self._corpus


def _diagnostics(top: Sequence[Candidate]) -> dict:
    return {
        "novelty": metrics.novelty(top),
        "intra_list_diversity": metrics.intra_list_diversity(top),
        "near_duplicate_rate": metrics.near_duplicate_rate(top, NEAR_DUPLICATE),
    }


def _mean_grade(ids: Sequence[UUID], grades: Mapping[UUID, float]) -> float | None:
    judged = [grades[asset_id] for asset_id in ids if asset_id in grades]
    return metrics.mean(judged)


def _means(boards: Mapping[str, Mapping]) -> dict:
    keys = sorted(
        {
            key
            for board in boards.values()
            for key, value in board.items()
            if isinstance(value, (int, float)) or value is None
        }
        - {"kind"}
    )
    return {key: metrics.mean([board.get(key) for board in boards.values()]) for key in keys}


def export_human(
    evaluation: Evaluation,
    pools: Mapping[str, dict[str, Pools]],
    compare: Mapping[str, list[tuple[str, str]]],
    directory: Path,
) -> None:
    """Blinded sheets: one row per distinct item, shuffled, no strategy column.

    The key file maps each row back to the strategies and ranks that produced
    it. Cohesive and complement sheets are separate and graded 0-3.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for mode, pairs in compare.items():
        rows, key = [], {}
        for board in evaluation.board_set.boards:
            produced: dict[UUID, dict[str, int]] = {}
            titles: dict[UUID, str | None] = {}
            for retriever, ranker in pairs:
                entry = pools[retriever].get(board.name)
                if entry is None:
                    continue
                ranked = RANKERS[ranker].rank(entry.full, evaluation.k)
                for rank, scored in enumerate(ranked, start=1):
                    produced.setdefault(scored.candidate.card.id, {})[f"{retriever}+{ranker}"] = (
                        rank
                    )
                    titles[scored.candidate.card.id] = scored.candidate.card.title
            # Deterministic shuffle: order by a hash of board and item, not by rank.
            order = sorted(
                produced,
                key=lambda asset_id: hashlib.sha256(
                    f"{board.name}:{asset_id}".encode()
                ).hexdigest(),
            )
            for asset_id in order:
                external = evaluation.external.get(asset_id, str(asset_id))
                rows.append(
                    {
                        "board": board.name,
                        "board_pins": " ".join(board.pins),
                        "external_id": external,
                        "title": titles[asset_id] or "",
                        "thumbnail": f"/api/catalog/{asset_id}/image/thumbnail",
                        "grade": "",
                    }
                )
                key[f"{board.name}:{external}"] = produced[asset_id]
        with (directory / f"{mode}-sheet.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(
                handle, ["board", "board_pins", "external_id", "title", "thumbnail", "grade"]
            )
            writer.writeheader()
            writer.writerows(rows)
        (directory / f"{mode}-key.json").write_text(json.dumps(key, indent=2, sort_keys=True))


def _pairs(value: str) -> list[tuple[str, str]]:
    pairs = []
    for item in value.split(","):
        retriever, ranker = item.split("+", 1)
        if retriever not in RETRIEVERS or ranker not in RANKERS:
            raise SystemExit(f"Unknown strategy {item!r}")
        pairs.append((retriever, ranker))
    return pairs


def _names(value: str, known: Mapping) -> list[str]:
    names = [name for name in value.split(",") if name]
    unknown = [name for name in names if name not in known]
    if unknown:
        raise SystemExit(f"Unknown names {unknown}; known: {sorted(known)}")
    return names


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.recommendations.evaluate")
    parser.add_argument("--boards", type=Path, default=DEFAULT_BOARDS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--retrievers", default=",".join(RETRIEVERS))
    parser.add_argument("--rankers", default=",".join(RANKERS))
    parser.add_argument("--pool", type=int, default=20)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--judgments-cohesive", type=Path)
    parser.add_argument("--judgments-complement", type=Path)
    parser.add_argument("--export-human", type=Path)
    parser.add_argument("--compare-cohesive", default="centroid+cosine,cluster+round-robin")
    parser.add_argument(
        "--compare-complement", default="centroid+complement-baseline-v0,centroid+mmr"
    )
    args = parser.parse_args(argv)

    retrievers = _names(args.retrievers, RETRIEVERS)
    rankers = _names(args.rankers, RANKERS)
    compare = {
        "cohesive": _pairs(args.compare_cohesive),
        "complement": _pairs(args.compare_complement),
    }
    settings = Settings()
    # Identity only: no model weights are loaded and no image is embedded.
    provider = build_provider(settings)
    board_set = load_boards(args.boards)
    cohesive = load_judgments(args.judgments_cohesive)
    complement = load_judgments(args.judgments_complement)

    worker = create_worker_engine(settings)
    try:
        with worker.connect() as connection:
            rows = connection.execute(text(EXTERNAL_IDS)).all()
    finally:
        worker.dispose()
    api = create_api_engine(settings)
    try:
        evaluation = Evaluation(
            RecommendationRepository(api),
            provider.model_key,
            provider.model_version,
            board_set,
            {row.external_id: row.id for row in rows},
            args.pool,
            args.k,
        )
        corpus = evaluation.corpus
        needed = set(retrievers) | {r for pairs in compare.values() for r, _ in pairs}
        pools = {name: evaluation.retrieve(name, RETRIEVERS[name]) for name in sorted(needed)}
        results = {
            "manifest": {
                "board_set": {"version": board_set.version, "sha256": board_set.digest},
                "model": {"key": provider.model_key, "version": provider.model_version},
                "corpus": {
                    "assets": len(corpus),
                    "sha256": hashlib.sha256(
                        "\n".join(str(asset_id) for asset_id in corpus).encode()
                    ).hexdigest(),
                },
                "pool": args.pool,
                "k": args.k,
                "near_duplicate_threshold": NEAR_DUPLICATE,
                "missing_pins": evaluation.missing,
                "retrievers": {name: _params(RETRIEVERS[name]) for name in retrievers},
                "rankers": {name: _params(RANKERS[name]) for name in rankers},
                "judgments": {
                    "cohesive": str(args.judgments_cohesive) if args.judgments_cohesive else None,
                    "complement": (
                        str(args.judgments_complement) if args.judgments_complement else None
                    ),
                },
            },
            "retrieval": {
                name: evaluation.retrieval_metrics(pools[name], cohesive) for name in retrievers
            },
            "ranking": {
                f"{retriever}+{ranker}": evaluation.ranking_metrics(
                    pools[retriever], RANKERS[ranker], cohesive, complement
                )
                for retriever in retrievers
                for ranker in rankers
            },
        }
        if args.export_human:
            export_human(evaluation, pools, compare, args.export_human)
    finally:
        api.dispose()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(_round(results), indent=2, sort_keys=True) + "\n")
    latency = args.out.with_name(args.out.stem + ".latency.json")
    latency.write_text(json.dumps(_round(evaluation.timings), indent=2, sort_keys=True) + "\n")
    _summary(results, sys.stdout)
    print(f"\nresults: {args.out}\nlatency: {latency}")
    return 0


def _summary(results: dict, out) -> None:
    def show(value) -> str:
        return "   -  " if value is None else f"{value:6.3f}"

    print("Retrieval (no ranker)", file=out)
    print(f"  {'retriever':24} recall  cover  corr", file=out)
    for name, block in results["retrieval"].items():
        mean = block["mean"]
        print(
            f"  {name:24} {show(mean['held_out_pool_recall'])} "
            f"{show(mean['pool_interest_coverage'])} {show(mean['relevance_novelty_correlation'])}",
            file=out,
        )
    print("\nCohesive ranking (identical pools per retriever)", file=out)
    print(f"  {'strategy':40} hit@k    mrr  cover   ndcg", file=out)
    for name, block in results["ranking"].items():
        mean = block["mean"]["cohesive"]
        print(
            f"  {name:40} {show(mean['held_out_hit_at_k'])} {show(mean['held_out_mrr'])} "
            f"{show(mean['interest_coverage_at_k'])} {show(mean['ndcg_at_k'])}",
            file=out,
        )
    print("\nComplement (human grade is primary; the rest are diagnostics)", file=out)
    print(f"  {'strategy':40} human  novel  divers  dup", file=out)
    for name, block in results["ranking"].items():
        mean = block["mean"]["complement"]
        diagnostics = mean["diagnostics"]
        print(
            f"  {name:40} {show(mean['human_mean_grade'])} {show(diagnostics['novelty'])} "
            f"{show(diagnostics['intra_list_diversity'])} "
            f"{show(diagnostics['near_duplicate_rate'])}",
            file=out,
        )


if __name__ == "__main__":
    raise SystemExit(main())

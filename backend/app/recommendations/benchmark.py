"""Measure recommendation latency with EXPLAIN ANALYZE at real and synthetic scale.

    python -m app.recommendations.benchmark [--sizes 1000 10000] [--real] [--out PATH]

Synthetic corpora go into a throwaway database created from MIGRATION_DATABASE_URL,
migrated to head, filled with READY verified-CC0 rows and seeded random unit
vectors, measured through the API role, and dropped. `--real` measures the
configured catalog with the active embedding identity and changes nothing.
Retrieval is exact; no vector index is created.
"""

import argparse
import json
import os
import statistics
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from uuid import uuid4

import numpy as np
import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import Engine, text
from sqlalchemy.engine import make_url

from app.core.config import REPO_ROOT, Settings
from app.db import catalog
from app.db.engine import create_api_engine
from app.embeddings.provider import build_provider
from app.recommendations import queries
from app.recommendations.models import BoardPins, Probe
from app.recommendations.repository import RecommendationRepository
from app.recommendations.retrieve import CentroidRetriever
from app.recommendations.service import RecommendationService
from app.recommendations.strategy import STRATEGIES, grid_strategy

BENCH_KEY, BENCH_VERSION = "bench", "random-v1"
DEFAULT_OUT = REPO_ROOT / "backend" / "evaluation" / "results" / "latency-benchmark.json"
RUNS, WARMUP, PINS, LIMIT = 30, 3, 6, 20


def _swap(url: str, database: str) -> str:
    return make_url(url).set(database=database).render_as_string(hide_password=False)


def _percentiles(samples: Sequence[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "p50_ms": round(statistics.median(ordered) * 1000, 3),
        "p95_ms": round(ordered[max(0, int(len(ordered) * 0.95) - 1)] * 1000, 3),
        "runs": len(ordered),
    }


def _time(call: Callable[[], object]) -> dict[str, float]:
    for _ in range(WARMUP):
        call()
    samples = []
    for _ in range(RUNS):
        started = time.perf_counter()
        call()
        samples.append(time.perf_counter() - started)
    return _percentiles(samples)


def _explain(engine: Engine, board: BoardPins, probes: int, key: str, version: str) -> dict:
    vectors = [pin.vector for pin in board.profile_pins[:probes]]
    params = {
        "probes": [queries.vector_literal(vector) for vector in vectors],
        "limits": [LIMIT] * len(vectors),
        "exclude_ids": sorted(board.saved_ids),
        "model_key": key,
        "model_version": version,
    }
    with engine.connect() as connection:
        plan = [
            row[0]
            for row in connection.execute(
                text("EXPLAIN (ANALYZE, BUFFERS) " + queries.CANDIDATES), params
            )
        ]
    execution = next((line for line in plan if line.startswith("Execution Time")), "")
    return {"execution": execution, "plan": plan}


def measure(engine: Engine, key: str, version: str) -> dict:
    repository = RecommendationRepository(engine)
    corpus = repository.corpus_ids(key, version)
    pins = repository.pins_for(corpus[:PINS], key, version)
    board = BoardPins(frozenset(pin.asset_id for pin in pins), pins, "benchmark")
    service = RecommendationService(repository, lambda limit: [], {}, key, version)
    strategies = {
        "centroid-cosine": STRATEGIES["centroid-cosine"],
        "complement-baseline-v0": STRATEGIES["complement-baseline-v0"],
        "per-pin+cosine": grid_strategy("per-pin", "cosine"),
        "cluster+round-robin": grid_strategy("cluster", "round-robin"),
    }
    return {
        "assets": len(corpus),
        "pins": len(pins),
        "explain": {
            f"{count}_probe": _explain(engine, board, count, key, version) for count in (1, PINS)
        },
        "candidate_query": {
            f"{len(probes)}_probe": _time(
                lambda probes=probes: repository.candidates(probes, board.saved_ids, key, version)
            )
            for probes in (
                CentroidRetriever().probes(pins, LIMIT),
                [Probe(pin.vector, (pin.asset_id,), LIMIT) for pin in pins],
            )
        },
        "ranked_path": {
            name: _time(lambda strategy=strategy: service.ranked(board, strategy, LIMIT))
            for name, strategy in strategies.items()
        },
    }


def _fill(url: str, size: int) -> None:
    rng = np.random.default_rng(size)
    with psycopg.connect(url, autocommit=True) as connection:
        provider = connection.execute(
            "INSERT INTO providers (name, base_url, policy_url) "
            "VALUES ('bench', 'https://bench.invalid', 'https://bench.invalid/policy') "
            "RETURNING id"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO source_records (provider_id, external_id, rights_classification, "
            "eligibility_status) "
            "SELECT %s, 'b' || g, %s, %s FROM generate_series(1, %s) AS g",
            (provider, catalog.RIGHTS_CC0, catalog.ELIGIBILITY_ELIGIBLE_CC0, size),
        )
        connection.execute(
            "INSERT INTO rights_evidence (source_record_id, rights_type, verification_status, "
            "verification_method) SELECT id, %s, %s, %s FROM source_records",
            (catalog.RIGHTS_CC0, catalog.VERIFICATION_VERIFIED, catalog.METHOD_PROVIDER_POLICY),
        )
        connection.execute(
            "INSERT INTO assets (source_record_id, original_key, thumbnail_key, sha256, mime_type, "
            "width, height, status, title) "
            "SELECT id, 'o/' || external_id, 't/' || external_id, "
            "encode(sha256(convert_to(external_id, 'UTF8')), 'hex'), 'image/jpeg', 640, 480, %s, "
            "'Bench ' || external_id FROM source_records",
            (catalog.ASSET_READY,),
        )
        ids = [row[0] for row in connection.execute("SELECT id FROM assets ORDER BY id")]
        vectors = rng.normal(size=(len(ids), catalog.VECTOR_DIMENSIONS))
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        with connection.cursor().copy(
            f"COPY {catalog.EMBEDDINGS_TABLE} "
            "(asset_id, model_key, model_version, dimensions, embedding) FROM STDIN"
        ) as copy:
            for asset_id, vector in zip(ids, vectors, strict=True):
                copy.write_row(
                    (
                        asset_id,
                        BENCH_KEY,
                        BENCH_VERSION,
                        catalog.VECTOR_DIMENSIONS,
                        queries.vector_literal(vector),
                    )
                )
        connection.execute("ANALYZE")


def synthetic(settings: Settings, size: int) -> dict:
    name = f"cc0_bench_{uuid4().hex[:12]}"
    admin = _swap(settings.migration_database_url, "postgres")
    migration_url = _swap(settings.migration_database_url, name)
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    previous = os.environ.get("MIGRATION_DATABASE_URL")
    os.environ["MIGRATION_DATABASE_URL"] = migration_url
    try:
        command.upgrade(Config(str(REPO_ROOT / "backend" / "alembic.ini")), "head")
        _fill(migration_url, size)
        engine = create_api_engine(
            settings.model_copy(update={"database_url": _swap(settings.database_url, name)})
        )
        try:
            return measure(engine, BENCH_KEY, BENCH_VERSION)
        finally:
            engine.dispose()
    finally:
        if previous is None:
            os.environ.pop("MIGRATION_DATABASE_URL", None)
        else:
            os.environ["MIGRATION_DATABASE_URL"] = previous
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def _timing(label: str, timing: dict[str, float]) -> str:
    return f"  {label:32} p50 {timing['p50_ms']:8.3f} ms  p95 {timing['p95_ms']:8.3f} ms"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.recommendations.benchmark")
    parser.add_argument("--sizes", type=int, nargs="*", default=[1000, 10000])
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    settings = Settings()
    report: dict[str, dict] = {}
    if args.real:
        provider = build_provider(settings)
        engine = create_api_engine(settings)
        try:
            report["real"] = {
                "model": f"{provider.model_key}/{provider.model_version}",
                **measure(engine, provider.model_key, provider.model_version),
            }
        finally:
            engine.dispose()
    for size in args.sizes:
        report[f"synthetic_{size}"] = synthetic(settings, size)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    for name, result in report.items():
        print(f"{name}: {result['assets']} assets, {result['pins']} pins")
        for probes, block in result["explain"].items():
            print(f"  EXPLAIN {probes:8} {block['execution']}")
        for probes, timing in result["candidate_query"].items():
            print(_timing(f"query {probes}", timing))
        for strategy, timing in result["ranked_path"].items():
            print(_timing(f"path {strategy}", timing))
    print(f"\nreport: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

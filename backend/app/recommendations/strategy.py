"""Named retriever + ranker pairings. Production picks one per mode by config.

Retrievers and rankers combine freely; the evaluation CLI builds the full grid
from RETRIEVERS x RANKERS. Only names registered in STRATEGIES are accepted in
configuration. The pool size is how many candidates retrieval returns in total
before ranking; None means exactly the requested limit.
"""

from dataclasses import dataclass

from app.recommendations.rank import (
    ComplementBaselineV0,
    CosineRanker,
    MMRRanker,
    Ranker,
    RoundRobinRanker,
)
from app.recommendations.retrieve import (
    CentroidRetriever,
    ClusterRetriever,
    PerPinRetriever,
    Retriever,
)

DEFAULT_COHESIVE = "centroid-cosine"
DEFAULT_COMPLEMENT = "complement-baseline-v0"
EXPERIMENT_POOL = 100


@dataclass(frozen=True)
class Strategy:
    name: str
    retriever: Retriever
    ranker: Ranker
    pool: int | None = None

    def pool_size(self, limit: int) -> int:
        return limit if self.pool is None else max(self.pool, limit)


RETRIEVERS: dict[str, Retriever] = {
    "centroid": CentroidRetriever(),
    "per-pin": PerPinRetriever(),
    "cluster": ClusterRetriever(),
    "cluster-proportional": ClusterRetriever(budget="proportional", name="cluster-proportional"),
}

RANKERS: dict[str, Ranker] = {
    "cosine": CosineRanker(),
    "mmr": MMRRanker(),
    "round-robin": RoundRobinRanker(),
    "complement-baseline-v0": ComplementBaselineV0(),
}

STRATEGIES: dict[str, Strategy] = {
    # Phase A baseline. A single-pin board matches /assets/{id}/related.
    DEFAULT_COHESIVE: Strategy(DEFAULT_COHESIVE, RETRIEVERS["centroid"], RANKERS["cosine"]),
    # Experimental control for complement research; see rank.ComplementBaselineV0.
    DEFAULT_COMPLEMENT: Strategy(
        DEFAULT_COMPLEMENT,
        RETRIEVERS["centroid"],
        RANKERS["complement-baseline-v0"],
        EXPERIMENT_POOL,
    ),
}


def grid_strategy(retriever: str, ranker: str, pool: int = EXPERIMENT_POOL) -> Strategy:
    return Strategy(f"{retriever}+{ranker}", RETRIEVERS[retriever], RANKERS[ranker], pool)


def configured(name: str) -> Strategy:
    try:
        return STRATEGIES[name]
    except KeyError:
        known = ", ".join(sorted(STRATEGIES))
        raise ValueError(f"Unknown recommender strategy {name!r}; known: {known}") from None

"""Reciprocal rank fusion.

Lexical rank and cosine distance are not on the same scale, so they are fused
as ranks. k = 60 is the usual constant: a first-place vote is worth 1/61 from
each list that contains the asset.
"""

from uuid import UUID

RRF_K = 60
# How many candidates each list contributes. The catalog is a few hundred rows;
# this cap keeps one query from ranking the whole table twice.
CANDIDATE_POOL = 50
# Lexical and cosine each contribute at most CANDIDATE_POOL ids, so a fused
# page snapshot holds at most twice that. Ranked pagination stores this list
# and does not expand it to the rest of the catalog.
RANK_SNAPSHOT_LIMIT = CANDIDATE_POOL * 2


def fuse(rankings: list[list[UUID]], *, k: int = RRF_K) -> list[UUID]:
    """Higher fused score first. Ties break on id so a page is stable."""
    scores: dict[UUID, float] = {}
    for ranking in rankings:
        for rank, asset_id in enumerate(ranking, start=1):
            scores[asset_id] = scores.get(asset_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda asset_id: (-scores[asset_id], str(asset_id)))

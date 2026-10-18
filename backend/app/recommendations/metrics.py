"""Offline evaluation metrics. Pure functions over ids and unit vectors.

Cohesive metrics measure whether results fit the board. Complement
diagnostics only describe a list (how new, how varied); they are not evidence
of complement quality, which comes from human judgments.
"""

import math
from collections.abc import Mapping, Sequence
from uuid import UUID

import numpy as np

from app.recommendations.models import Candidate


def hit_and_reciprocal_rank(ranked: Sequence[UUID], target: UUID, k: int) -> tuple[float, float]:
    top = list(ranked[:k])
    if target not in top:
        return 0.0, 0.0
    return 1.0, 1.0 / (top.index(target) + 1)


def ndcg(ranked: Sequence[UUID], grades: Mapping[UUID, float], k: int) -> float | None:
    """Graded nDCG@k. None when nothing relevant was judged for the board."""
    ideal = sorted((grade for grade in grades.values() if grade > 0), reverse=True)[:k]
    if not ideal:
        return None
    gain = sum(
        (2 ** grades.get(asset_id, 0.0) - 1) / math.log2(index + 2)
        for index, asset_id in enumerate(ranked[:k])
    )
    best = sum((2**grade - 1) / math.log2(index + 2) for index, grade in enumerate(ideal))
    return gain / best


def interest_coverage(
    ids: Sequence[UUID], labels: Mapping[UUID, str], board_labels: set[str]
) -> float | None:
    """Fraction of the board's labeled interests present among `ids`."""
    if not board_labels:
        return None
    present = {labels[asset_id] for asset_id in ids if asset_id in labels}
    return len(present & board_labels) / len(board_labels)


def novelty(candidates: Sequence[Candidate]) -> float | None:
    """Mean distance from each result to its nearest saved pin."""
    if not candidates:
        return None
    return float(np.mean([1.0 - float(c.pin_similarity.max()) for c in candidates]))


def intra_list_diversity(candidates: Sequence[Candidate]) -> float | None:
    """Mean pairwise cosine distance among results."""
    if len(candidates) < 2:
        return None
    matrix = np.stack([c.vector for c in candidates])
    similarity = matrix @ matrix.T
    upper = similarity[np.triu_indices(len(candidates), k=1)]
    return float(np.mean(1.0 - upper))


def near_duplicate_rate(candidates: Sequence[Candidate], threshold: float) -> float | None:
    if not candidates:
        return None
    return sum(float(c.pin_similarity.max()) >= threshold for c in candidates) / len(candidates)


def pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    """None when either side is constant or there are fewer than three points."""
    if len(left) < 3:
        return None
    a, b = np.asarray(left, dtype=float), np.asarray(right, dtype=float)
    if a.std() < 1e-12 or b.std() < 1e-12:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def relevance_novelty_correlation(candidates: Sequence[Candidate]) -> float | None:
    """Correlation of probe relevance with pin novelty over a pool.

    Near -1 means a weighted relevance + novelty score collapses into one
    similarity ordering, as it does when every pin is its own probe.
    """
    return pearson(
        [float(c.probe_similarity.max()) for c in candidates],
        [1.0 - float(c.pin_similarity.max()) for c in candidates],
    )


def mean(values: Sequence[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return None if not present else float(np.mean(present))

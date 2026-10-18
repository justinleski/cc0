"""Offline evaluation metrics for board recommendations."""

from uuid import UUID

import numpy as np
import pytest

from app.recommendations import metrics
from app.recommendations.retrieve import CentroidRetriever, PerPinRetriever, merge
from tests.unit.test_recommendation_rank import _hit, _pin, _unit


def _id(number: int) -> UUID:
    return UUID(int=number)


def test_hit_reciprocal_rank_and_ndcg() -> None:
    ranked = [_id(1), _id(2), _id(3)]
    assert metrics.hit_and_reciprocal_rank(ranked, _id(2), 3) == (1.0, 0.5)
    assert metrics.hit_and_reciprocal_rank(ranked, _id(3), 2) == (0.0, 0.0)
    grades = {_id(2): 3.0, _id(3): 1.0}
    perfect = metrics.ndcg([_id(2), _id(3), _id(1)], grades, 3)
    assert perfect == pytest.approx(1.0)
    assert 0 < metrics.ndcg(ranked, grades, 3) < 1
    assert metrics.ndcg(ranked, {}, 3) is None


def test_interest_coverage_counts_only_board_labels() -> None:
    labels = {_id(1): "landscape", _id(2): "portrait", _id(3): "garden"}
    assert metrics.interest_coverage([_id(1), _id(3)], labels, {"landscape", "portrait"}) == 0.5
    assert metrics.interest_coverage([_id(1)], labels, set()) is None


def _random_pool(retriever, rng):
    pins = [_pin(index, _unit(*rng.normal(size=8))) for index in range(1, 4)]
    probes = retriever.probes(pins, 30)
    hits = [_hit(100 + index, _unit(*rng.normal(size=8))) for index in range(30)]
    return merge(hits, probes, pins, frozenset(pin.asset_id for pin in pins))


def test_per_pin_relevance_is_the_inverse_of_pin_novelty() -> None:
    rng = np.random.default_rng(3)
    per_pin = _random_pool(PerPinRetriever(floor=1), rng)
    # Relevance (best probe) and novelty (nearest pin) are the same similarity.
    assert metrics.relevance_novelty_correlation(per_pin.candidates) == pytest.approx(-1.0)
    centroid = _random_pool(CentroidRetriever(), rng)
    assert metrics.relevance_novelty_correlation(centroid.candidates) > -0.999


def test_list_diagnostics() -> None:
    rng = np.random.default_rng(5)
    pool = _random_pool(CentroidRetriever(), rng)
    top = list(pool.candidates[:5])
    assert 0 <= metrics.novelty(top) <= 2
    assert 0 <= metrics.intra_list_diversity(top) <= 2
    assert metrics.near_duplicate_rate(top, 2.0) == 0.0
    assert metrics.near_duplicate_rate(top, -2.0) == 1.0
    assert metrics.intra_list_diversity(top[:1]) is None
    assert metrics.pearson([1, 1, 1], [1, 2, 3]) is None
    assert metrics.mean([None, 1.0, 3.0]) == 2.0

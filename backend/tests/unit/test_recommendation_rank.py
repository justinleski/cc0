"""Pure retrieval and ranking functions for board recommendations."""

from datetime import UTC, datetime
from uuid import UUID

import numpy as np

from app.recommendations.models import CandidateHit, ProfilePin
from app.recommendations.rank import (
    ComplementBaselineV0,
    CosineRanker,
    MMRRanker,
    RoundRobinRanker,
)
from app.recommendations.retrieve import (
    CentroidRetriever,
    ClusterRetriever,
    PerPinRetriever,
    choose_clusters,
    merge,
)
from app.search.models import AssetCard


def _unit(*values: float) -> np.ndarray:
    vector = np.asarray(values, dtype=float)
    return vector / np.linalg.norm(vector)


def _id(number: int) -> UUID:
    return UUID(int=number)


def _pin(number: int, vector: np.ndarray, position: int | None = None) -> ProfilePin:
    return ProfilePin(_id(number), number if position is None else position, None, None, (), vector)


def _hit(number: int, vector: np.ndarray, probe: int = 0) -> CandidateHit:
    card = AssetCard(
        _id(number), None, None, None, None, None, None, "t.jpg", datetime.now(UTC), 1, 1
    )
    return CandidateHit(probe, card, (), vector)


def _ids(scored) -> list[int]:
    return [item.candidate.card.id.int for item in scored]


def test_centroid_is_unit_length_and_a_single_pin_is_its_own_probe() -> None:
    pins = [_pin(1, _unit(1, 0, 0)), _pin(2, _unit(0, 1, 0))]
    [probe] = CentroidRetriever().probes(pins, 20)
    assert np.isclose(np.linalg.norm(probe.vector), 1.0)
    assert np.allclose(probe.vector, _unit(1, 1, 0))
    assert probe.limit == 20
    [single] = CentroidRetriever().probes(pins[:1], 5)
    assert np.array_equal(single.vector, pins[0].vector)


def test_per_pin_budget_is_equal_and_ignores_board_position() -> None:
    pins = [_pin(number, _unit(number, 1, 0), position=10 - number) for number in range(1, 8)]
    probes = PerPinRetriever(floor=2).probes(pins, 21)
    assert {probe.limit for probe in probes} == {3}
    assert [probe.pin_ids[0].int for probe in probes] == list(range(1, 8))
    reordered = PerPinRetriever(floor=2).probes(list(reversed(pins)), 21)
    assert [probe.pin_ids for probe in reordered] == [probe.pin_ids for probe in probes]
    assert {probe.limit for probe in PerPinRetriever(floor=5).probes(pins, 7)} == {5}


def test_clustering_stays_whole_for_one_style_and_splits_two() -> None:
    rng = np.random.default_rng(7)
    one_style = np.stack([_unit(*(np.array([1.0, 0, 0, 0]) + rng.normal(0, 0.05, 4)))
                          for _ in range(6)])
    assert set(choose_clusters(one_style, 4, 2, 0.15, 0.15).tolist()) == {0}

    left = [_unit(*(np.array([1.0, 0, 0, 0]) + rng.normal(0, 0.05, 4))) for _ in range(3)]
    right = [_unit(*(np.array([0, 0, 1.0, 0]) + rng.normal(0, 0.05, 4))) for _ in range(3)]
    labels = choose_clusters(np.stack(left + right), 4, 2, 0.15, 0.15)
    assert len(set(labels[:3].tolist())) == len(set(labels[3:].tolist())) == 1
    assert labels[0] != labels[3]

    pins = [_pin(index, vector) for index, vector in enumerate(left + right, start=1)]
    probes = ClusterRetriever(floor=1).probes(pins, 10)
    assert [len(probe.pin_ids) for probe in probes] == [3, 3]
    assert [probe.limit for probe in probes] == [5, 5]
    reordered = ClusterRetriever(floor=1).probes(list(reversed(pins)), 10)
    assert [probe.pin_ids for probe in reordered] == [probe.pin_ids for probe in probes]
    assert all(
        np.array_equal(a.vector, b.vector) for a, b in zip(probes, reordered, strict=True)
    )


def test_merge_dedupes_drops_saved_and_scores_every_probe() -> None:
    pins = [_pin(1, _unit(1, 0, 0)), _pin(2, _unit(0, 1, 0))]
    probes = PerPinRetriever(floor=1).probes(pins, 2)
    hits = [
        _hit(10, _unit(1, 1, 0), probe=0),
        _hit(10, _unit(1, 1, 0), probe=1),
        _hit(11, _unit(0, 1, 0.2), probe=1),
        _hit(2, _unit(0, 1, 0), probe=1),
    ]
    pool = merge(hits, probes, pins, frozenset({_id(1), _id(2)}))
    assert [c.card.id.int for c in pool.candidates] == [10, 11]
    assert pool.candidates[0].sources == (0, 1)
    assert pool.candidates[1].sources == (1,)
    assert pool.candidates[1].probe_similarity.shape == (2,)
    assert pool.candidates[1].probe_similarity[0] < pool.candidates[1].probe_similarity[1]


def _pool(vectors: dict[int, np.ndarray], pins: list[ProfilePin]):
    probes = CentroidRetriever().probes(pins, len(vectors))
    hits = [_hit(number, vector) for number, vector in vectors.items()]
    return merge(hits, probes, pins, frozenset(pin.asset_id for pin in pins))


def test_cosine_breaks_ties_on_asset_id_and_complement_drops_near_duplicates() -> None:
    pins = [_pin(1, _unit(1, 0, 0))]
    pool = _pool(
        {12: _unit(1, 0.5, 0), 11: _unit(1, 0.5, 0), 13: _unit(1, 0.01, 0), 14: _unit(0, 1, 0)},
        pins,
    )
    assert _ids(CosineRanker().rank(pool, 4)) == [13, 11, 12, 14]
    assert _ids(ComplementBaselineV0(near_duplicate=0.99).rank(pool, 4)) == [11, 12, 14]


def test_mmr_trades_relevance_for_diversity_among_results() -> None:
    pins = [_pin(1, _unit(1, 0, 0))]
    pool = _pool({10: _unit(1, 0.1, 0), 11: _unit(1, 0.1, 0.01), 12: _unit(1, 0, 0.6)}, pins)
    assert _ids(CosineRanker().rank(pool, 2)) == [10, 11]
    assert _ids(MMRRanker(trade_off=0.5).rank(pool, 2)) == [10, 12]
    assert _ids(MMRRanker(trade_off=1.0).rank(pool, 2)) == [10, 11]


def test_round_robin_gives_every_probe_a_turn() -> None:
    pins = [_pin(1, _unit(1, 0, 0)), _pin(2, _unit(0, 1, 0))]
    probes = PerPinRetriever(floor=1).probes(pins, 4)
    hits = [
        _hit(10, _unit(1, 0.01, 0), 0),
        _hit(11, _unit(1, 0.02, 0), 0),
        _hit(12, _unit(1, 0.03, 0), 0),
        _hit(20, _unit(0.2, 1, 0), 1),
    ]
    pool = merge(hits, probes, pins, frozenset({_id(1), _id(2)}))
    assert _ids(CosineRanker().rank(pool, 2)) == [10, 11]
    assert _ids(RoundRobinRanker().rank(pool, 3)) == [10, 20, 11]

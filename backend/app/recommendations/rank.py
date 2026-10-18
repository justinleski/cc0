"""Rankers order a fixed candidate pool. Pure, deterministic, interchangeable.

Every ranker breaks ties on asset id. `ComplementBaselineV0` is a placeholder
control for complement research, not a definition of complementarity: it only
removes near-duplicates of saved pins and then ranks by relevance.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from app.recommendations.models import Candidate, CandidatePool, Scored


class Ranker(Protocol):
    name: str

    def rank(self, pool: CandidatePool, limit: int) -> list[Scored]: ...


def relevance(candidate: Candidate) -> float:
    """Similarity to the best-matching probe."""
    return float(candidate.probe_similarity.max()) if candidate.probe_similarity.size else 0.0


def nearest_pin_similarity(candidate: Candidate) -> float:
    return float(candidate.pin_similarity.max()) if candidate.pin_similarity.size else 0.0


def _top(scored: Sequence[Scored], limit: int) -> list[Scored]:
    return sorted(scored, key=lambda item: (-item.score, item.candidate.card.id))[:limit]


@dataclass(frozen=True)
class CosineRanker:
    name: str = "cosine"

    def rank(self, pool: CandidatePool, limit: int) -> list[Scored]:
        return _top([Scored(c, relevance(c)) for c in pool.candidates], limit)


@dataclass(frozen=True)
class ComplementBaselineV0:
    """Drop candidates at or above `near_duplicate` cosine to any pin, then cosine."""

    near_duplicate: float = 0.95
    name: str = "complement-baseline-v0"

    def rank(self, pool: CandidatePool, limit: int) -> list[Scored]:
        kept = [c for c in pool.candidates if nearest_pin_similarity(c) < self.near_duplicate]
        return _top([Scored(c, relevance(c)) for c in kept], limit)


@dataclass(frozen=True)
class MMRRanker:
    """Maximal marginal relevance: diversity among selected results only."""

    trade_off: float = 0.7
    name: str = "mmr"

    def rank(self, pool: CandidatePool, limit: int) -> list[Scored]:
        remaining = sorted(pool.candidates, key=lambda c: c.card.id)
        scores = {c.card.id: relevance(c) for c in remaining}
        chosen: list[Scored] = []
        chosen_vectors: list[np.ndarray] = []
        while remaining and len(chosen) < limit:
            if chosen_vectors:
                redundancy = np.max(
                    np.stack([c.vector for c in remaining]) @ np.stack(chosen_vectors).T, axis=1
                )
            else:
                redundancy = np.zeros(len(remaining))
            marginal = [
                self.trade_off * scores[c.card.id] - (1 - self.trade_off) * float(redundancy[i])
                for i, c in enumerate(remaining)
            ]
            # max() keeps the first maximum, and `remaining` is in id order.
            best = max(range(len(remaining)), key=lambda i: marginal[i])
            pick = remaining.pop(best)
            chosen.append(Scored(pick, marginal[best]))
            chosen_vectors.append(pick.vector)
        return chosen


@dataclass(frozen=True)
class RoundRobinRanker:
    """Take each probe's best remaining candidate in turn, so every interest shows."""

    name: str = "round-robin"

    def rank(self, pool: CandidatePool, limit: int) -> list[Scored]:
        queues = []
        for index in range(len(pool.probes)):
            own = [c for c in pool.candidates if index in c.sources]
            queues.append(
                sorted(own, key=lambda c: (-float(c.probe_similarity[index]), c.card.id))
            )
        chosen: list[Scored] = []
        seen: set = set()
        while len(chosen) < limit and any(queues):
            for index, queue in enumerate(queues):
                while queue and queue[0].card.id in seen:
                    queue.pop(0)
                if queue and len(chosen) < limit:
                    pick = queue.pop(0)
                    seen.add(pick.card.id)
                    chosen.append(Scored(pick, float(pick.probe_similarity[index])))
        return chosen

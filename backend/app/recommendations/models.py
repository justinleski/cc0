"""Typed board profiles, candidate pools, and results.

Vectors are unit-length numpy arrays. Nothing here imports HTTP or SQL driver
types. The card is the search feature's public card so both features serialize
assets the same way.
"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import numpy as np

from app.search.models import AssetCard

Mode = Literal["cohesive", "complement"]


@dataclass(frozen=True)
class ProfilePin:
    """A saved pin that is eligible and has a vector for the active model."""

    asset_id: UUID
    position: int
    title: str | None
    creator_id: UUID | None
    style_tags: tuple[str, ...]
    vector: np.ndarray


@dataclass(frozen=True)
class BoardPins:
    # Every saved pin, eligible or not. The only source of the exclusion list.
    saved_ids: frozenset[UUID]
    # Ordered by board position. A subset of saved_ids.
    profile_pins: tuple[ProfilePin, ...]
    board_version: str

    @property
    def ignored_pin_count(self) -> int:
        return len(self.saved_ids) - len(self.profile_pins)


@dataclass(frozen=True)
class Probe:
    """One retrieval query vector, the pins behind it, and its candidate budget."""

    vector: np.ndarray
    pin_ids: tuple[UUID, ...]
    limit: int


@dataclass(frozen=True)
class CandidateHit:
    """One row of the candidate query: a candidate retrieved by one probe."""

    probe_index: int
    card: AssetCard
    style_tags: tuple[str, ...]
    vector: np.ndarray


@dataclass(frozen=True)
class Candidate:
    card: AssetCard
    style_tags: tuple[str, ...]
    vector: np.ndarray
    # Probes whose own top-k retrieved this candidate, ascending.
    sources: tuple[int, ...]
    # Cosine similarity to every probe and every profile pin, not only sources.
    probe_similarity: np.ndarray
    pin_similarity: np.ndarray


@dataclass(frozen=True)
class CandidatePool:
    probes: tuple[Probe, ...]
    pins: tuple[ProfilePin, ...]
    # Deduplicated, ordered by asset id so rankers never see retrieval order.
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True)
class Scored:
    candidate: Candidate
    score: float


@dataclass(frozen=True)
class Recommendation:
    card: AssetCard
    score: float | None
    reasons: tuple[str, ...]
    closest_pin_id: UUID | None


@dataclass(frozen=True)
class Recommendations:
    mode: Mode
    strategy: str
    fallback: Literal["browse"] | None
    profile_pin_count: int
    ignored_pin_count: int
    board_version: str
    items: list[Recommendation]

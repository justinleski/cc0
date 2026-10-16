"""Typed search inputs and results shared by orchestration and persistence."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class AssetCard:
    id: UUID
    title: str | None
    creator_id: UUID | None
    creator_name: str | None
    location_label: str | None
    created_year_start: int | None
    created_year_end: int | None
    thumbnail_key: str
    ingested_at: datetime


@dataclass(frozen=True)
class AssetDetail:
    card: AssetCard
    description: str | None
    original_key: str
    width: int
    height: int
    landing_url: str | None
    rights_type: str
    evidence_url: str | None
    verified_at: datetime


@dataclass(frozen=True)
class RelatedHit:
    card: AssetCard
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SearchPage:
    applied_query: str
    suggestion: str | None
    items: list[AssetCard]
    next_cursor: str | None


@dataclass(frozen=True)
class CreatorHit:
    id: UUID
    name: str


@dataclass(frozen=True)
class CreatorPage:
    creators: list[CreatorHit]
    did_you_mean: str | None


@dataclass(frozen=True)
class SearchFilters:
    creator_id: UUID | None = None
    location: str | None = None
    year_from: int | None = None
    year_to: int | None = None


@dataclass(frozen=True)
class BrowsePosition:
    ingested_at: datetime
    id: UUID


@dataclass(frozen=True)
class RankedCandidates:
    lexical: list[UUID]
    semantic: list[UUID]

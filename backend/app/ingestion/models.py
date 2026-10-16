"""Typed ingestion results, independent of HTTP and database drivers."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.db import catalog


@dataclass(frozen=True)
class SourceRecord:
    id: UUID
    provider_id: UUID
    external_id: str
    landing_url: str | None
    original_url: str | None
    # The allowlisted provider fields, kept so a retry can rebuild the asset
    # columns without asking the provider for metadata it already validated.
    raw_metadata: dict[str, Any]
    rights_classification: str
    eligibility_status: str
    next_recheck_at: datetime | None
    # Evaluated in SQL against now() so the decision uses the database clock.
    recheck_due: bool

    @property
    def is_negative_cached(self) -> bool:
        return self.eligibility_status in catalog.CACHED_STATUSES

    @property
    def is_fresh_negative_cache(self) -> bool:
        """True when a repeat discovery must stop without any request."""
        return self.is_negative_cached and not self.recheck_due


@dataclass(frozen=True)
class Asset:
    id: UUID
    status: str
    sha256: str
    original_key: str
    thumbnail_key: str


@dataclass(frozen=True)
class Evidence:
    rights_type: str
    verification_status: str
    verification_method: str
    checked_at: datetime


@dataclass(frozen=True)
class IngestOutcome:
    external_id: str
    result: str
    detail: str = ""
    # True when the job already reached a terminal state inside the work's own
    # transaction, so the caller must not write over it.
    job_settled: bool = False

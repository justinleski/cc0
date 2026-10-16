"""Typed results and catalog rows for embedding jobs."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class Asset:
    id: UUID
    status: str
    sha256: str
    original_key: str
    thumbnail_key: str


@dataclass(frozen=True)
class EmbedOutcome:
    asset_id: str
    result: str
    detail: str = ""
    job_settled: bool = False

"""Typed export results and domain errors, independent of HTTP and SQL drivers."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


class ExportNotFound(Exception):
    pass


class ExportEmpty(Exception):
    """Nothing on the board is currently exportable."""


class ExportTooLarge(Exception):
    pass


class ExportLimitReached(Exception):
    pass


@dataclass(frozen=True)
class Export:
    id: UUID
    board_id: UUID | None
    status: str
    failure_reason: str | None
    included_count: int | None
    excluded_count: int | None
    created_at: datetime
    completed_at: datetime | None
    expires_at: datetime | None
    # Present only on a READY export that passed the download-time recheck.
    download_url: str | None = None
    download_expires_in: int | None = None

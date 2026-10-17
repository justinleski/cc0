"""Typed board results and domain errors, independent of HTTP and SQL drivers."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


class BoardNotFound(Exception):
    pass


class BoardConflict(Exception):
    pass


class InvalidBoardChange(Exception):
    pass


@dataclass(frozen=True)
class BoardItem:
    asset_id: UUID
    position: int
    notes: str


@dataclass(frozen=True)
class Board:
    id: UUID
    name: str
    description: str
    created_at: datetime
    updated_at: datetime
    items: tuple[BoardItem, ...] = ()

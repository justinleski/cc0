"""Opaque cursors.

Browse is a keyset on `(ingested_at, id)`. A ranked page carries the fused id
order from the first page, bound to the query, the filters, and the embedding
identity. Later pages walk that snapshot.
"""

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.search.rank import RANK_SNAPSHOT_LIMIT


class InvalidCursor(Exception):
    """The cursor is not one this API issued for this query."""


@dataclass(frozen=True)
class RankSnapshot:
    applied_query: str
    phrases: tuple[str, ...]
    unquoted: str
    creator_id: str | None
    location: str | None
    year_from: int | None
    year_to: int | None
    model_key: str
    model_version: str
    ids: tuple[UUID, ...]
    offset: int


def encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(value.encode()))
    except (ValueError, json.JSONDecodeError) as exc:
        raise InvalidCursor() from exc
    if not isinstance(payload, dict) or "t" not in payload:
        raise InvalidCursor()
    return payload


def browse_cursor(ingested_at: datetime, asset_id: UUID) -> str:
    return encode_cursor(
        {"t": "browse", "at": ingested_at.isoformat(), "id": str(asset_id)}
    )


def rank_cursor(snapshot: RankSnapshot) -> str:
    return encode_cursor(
        {
            "t": "rank",
            "q": snapshot.applied_query,
            "phrases": list(snapshot.phrases),
            "unquoted": snapshot.unquoted,
            "creator_id": snapshot.creator_id,
            "location": snapshot.location,
            "year_from": snapshot.year_from,
            "year_to": snapshot.year_to,
            "model_key": snapshot.model_key,
            "model_version": snapshot.model_version,
            "ids": [str(asset_id) for asset_id in snapshot.ids],
            "offset": snapshot.offset,
        }
    )


def parse_rank_cursor(payload: dict[str, Any]) -> RankSnapshot:
    """The fused id list and the request it belongs to. Anything else is invalid."""
    try:
        applied = _text(payload["q"])
        phrases = _texts(payload["phrases"])
        unquoted = _text(payload["unquoted"])
        creator_id = _optional_text(payload["creator_id"])
        location = _optional_text(payload["location"])
        year_from = _optional_int(payload["year_from"])
        year_to = _optional_int(payload["year_to"])
        model_key = _text(payload["model_key"])
        model_version = _text(payload["model_version"])
        raw_ids = payload["ids"]
        offset = _required_int(payload["offset"])
    except (KeyError, TypeError) as exc:
        raise InvalidCursor() from exc
    if creator_id is not None:
        _uuid(creator_id)
    if not model_key or not model_version:
        raise InvalidCursor()
    if not isinstance(raw_ids, list) or len(raw_ids) > RANK_SNAPSHOT_LIMIT:
        raise InvalidCursor()
    ids = tuple(_uuid(item) for item in raw_ids)
    if offset < 0 or offset > len(ids):
        raise InvalidCursor()
    return RankSnapshot(
        applied_query=applied,
        phrases=phrases,
        unquoted=unquoted,
        creator_id=creator_id,
        location=location,
        year_from=year_from,
        year_to=year_to,
        model_key=model_key,
        model_version=model_version,
        ids=ids,
        offset=offset,
    )


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidCursor()
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return _text(value)


def _texts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise InvalidCursor()
    return tuple(_text(item) for item in value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return _required_int(value)


def _required_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidCursor()
    return value


def _uuid(value: Any) -> UUID:
    if not isinstance(value, str):
        raise InvalidCursor()
    try:
        return UUID(value)
    except ValueError as exc:
        raise InvalidCursor() from exc

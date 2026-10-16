"""Search orchestration: parsing, embeddings, fusion, and cursor snapshots.

Persistence lives behind SearchRepository. Text is encoded before repository
candidate retrieval so inference never holds a pooled database connection.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from app.embeddings.provider import EmbeddingProvider
from app.search.cursor import (
    InvalidCursor,
    RankSnapshot,
    browse_cursor,
    decode_cursor,
    parse_rank_cursor,
    rank_cursor,
)
from app.search.models import (
    AssetDetail,
    BrowsePosition,
    CreatorPage,
    RelatedHit,
    SearchFilters,
    SearchPage,
)
from app.search.parse import ParsedQuery, parse_search_text
from app.search.rank import RANK_SNAPSHOT_LIMIT, fuse
from app.search.repository import SearchRepository


class SearchQueryError(Exception):
    """The request cannot be searched. The message is safe to return."""


class SearchService:
    def __init__(self, repository: SearchRepository, embeddings: EmbeddingProvider) -> None:
        self._repository = repository
        self._embeddings = embeddings

    def search(
        self,
        *,
        q: str | None,
        creator_id: UUID | None,
        location: str | None,
        year_from: int | None,
        year_to: int | None,
        limit: int,
        cursor: str | None,
    ) -> SearchPage:
        if year_from is not None and year_to is not None and year_from > year_to:
            raise SearchQueryError("year_from cannot be after year_to")
        parsed = parse_search_text(q or "")
        filters = SearchFilters(creator_id, location, year_from, year_to)
        if not parsed.applied_query:
            return self._browse(filters, limit, cursor, parsed.applied_query)
        return self._ranked(
            parsed,
            filters,
            limit,
            cursor,
        )

    def asset(self, asset_id: UUID) -> AssetDetail | None:
        return self._repository.asset(asset_id)

    def related(self, asset_id: UUID, limit: int) -> list[RelatedHit] | None:
        return self._repository.related(
            asset_id,
            limit,
            self._embeddings.model_key,
            self._embeddings.model_version,
        )

    def creators(self, query: str | None, limit: int) -> CreatorPage:
        hint = " ".join((query or "").split())
        creators = self._repository.creators(hint, limit)
        suggestion = None
        if hint and creators and creators[0].name.casefold() != hint.casefold():
            suggestion = creators[0].name
        return CreatorPage(creators=creators, did_you_mean=suggestion)

    def _browse(
        self,
        filters: SearchFilters,
        limit: int,
        cursor: str | None,
        applied: str,
    ) -> SearchPage:
        after = None
        if cursor is not None:
            payload = _expect_cursor(cursor, "browse")
            try:
                cursor_at = datetime.fromisoformat(payload["at"])
                cursor_id = UUID(payload["id"])
                after = BrowsePosition(cursor_at, cursor_id)
            except (KeyError, TypeError, ValueError) as exc:
                raise InvalidCursor() from exc
        cards = self._repository.browse(filters, limit + 1, after)
        page, more = cards[:limit], len(cards) > limit
        next_cursor = browse_cursor(page[-1].ingested_at, page[-1].id) if more and page else None
        return SearchPage(
            applied_query=applied,
            suggestion=None,
            items=page,
            next_cursor=next_cursor,
        )

    def _ranked(
        self,
        parsed: ParsedQuery,
        filters: SearchFilters,
        limit: int,
        cursor: str | None,
    ) -> SearchPage:
        scope = _rank_scope(
            parsed,
            filters,
            model_key=self._embeddings.model_key,
            model_version=self._embeddings.model_version,
        )
        if cursor is not None:
            snapshot = parse_rank_cursor(_expect_cursor(cursor, "rank"))
            _require_same_scope(snapshot, scope)
            return self._page_snapshot(
                list(snapshot.ids),
                snapshot.offset,
                scope,
                limit,
                parsed,
                suggest=False,
            )
        # Encode before checkout. OpenCLIP serializes inference on one lock,
        # and the API pool is shared with browse and readiness.
        vector = self._embeddings.embed_text(parsed.unquoted) if parsed.unquoted else None
        candidates = self._repository.candidates(
            parsed,
            filters,
            vector,
            self._embeddings.model_key,
            self._embeddings.model_version,
        )
        lexical, semantic = candidates.lexical, candidates.semantic
        ordered = fuse([lexical, semantic]) if parsed.unquoted else lexical
        ordered = ordered[:RANK_SNAPSHOT_LIMIT]
        return self._page_snapshot(ordered, 0, scope, limit, parsed, suggest=True)

    def _page_snapshot(
        self,
        ordered: list[UUID],
        offset: int,
        scope: RankSnapshot,
        limit: int,
        parsed: ParsedQuery,
        *,
        suggest: bool,
    ) -> SearchPage:
        page_ids = ordered[offset : offset + limit]
        cards = self._repository.cards(page_ids)
        suggestion = None
        if suggest and offset == 0 and not ordered:
            hint = parsed.unquoted or " ".join(parsed.phrases)
            suggestion = self._suggest(hint)
        next_offset = offset + limit
        next_cursor = None
        if next_offset < len(ordered):
            next_cursor = rank_cursor(
                RankSnapshot(
                    applied_query=scope.applied_query,
                    phrases=scope.phrases,
                    unquoted=scope.unquoted,
                    creator_id=scope.creator_id,
                    location=scope.location,
                    year_from=scope.year_from,
                    year_to=scope.year_to,
                    model_key=scope.model_key,
                    model_version=scope.model_version,
                    ids=tuple(ordered),
                    offset=next_offset,
                )
            )
        return SearchPage(
            applied_query=parsed.applied_query,
            suggestion=suggestion,
            items=cards,
            next_cursor=next_cursor,
        )

    def _suggest(self, hint: str) -> str | None:
        if not hint:
            return None
        creators = self._repository.creators(hint, 1)
        if not creators or creators[0].name.casefold() == hint.casefold():
            return None
        return creators[0].name


def _rank_scope(
    parsed: ParsedQuery,
    filters: SearchFilters,
    *,
    model_key: str,
    model_version: str,
) -> RankSnapshot:
    return RankSnapshot(
        applied_query=parsed.applied_query,
        phrases=parsed.phrases,
        unquoted=parsed.unquoted,
        creator_id=None if filters.creator_id is None else str(filters.creator_id),
        location=filters.location or None,
        year_from=filters.year_from,
        year_to=filters.year_to,
        model_key=model_key,
        model_version=model_version,
        ids=(),
        offset=0,
    )


def _require_same_scope(snapshot: RankSnapshot, scope: RankSnapshot) -> None:
    if (
        snapshot.applied_query != scope.applied_query
        or snapshot.phrases != scope.phrases
        or snapshot.unquoted != scope.unquoted
        or snapshot.creator_id != scope.creator_id
        or snapshot.location != scope.location
        or snapshot.year_from != scope.year_from
        or snapshot.year_to != scope.year_to
        or snapshot.model_key != scope.model_key
        or snapshot.model_version != scope.model_version
    ):
        raise InvalidCursor()


def _expect_cursor(cursor: str, kind: str) -> dict[str, Any]:
    payload = decode_cursor(cursor)
    if payload.get("t") != kind:
        raise InvalidCursor()
    return payload

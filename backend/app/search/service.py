"""Search reads the public view, as cc0_api.

`ready_assets` is READY assets whose latest evidence row is verified CC0.
Browse, ranked search, related candidates, and asset detail all read that view.

Exact creator, location, and year filters are SQL predicates. A quoted phrase
is a required match on the unstemmed lexeme, applied before fusion. Unquoted
text is reciprocal rank fusion of the weighted full-text rank and cosine
distance in the active model's vector space. Names are not stemmed; titles,
tags, and descriptions are, on a second lexeme.

Query text is encoded before a connection is checked out. A ranked cursor
stores the fused id order from the first page and later pages walk that list.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from app.db import catalog
from app.embeddings.provider import EmbeddingProvider
from app.search.cursor import (
    InvalidCursor,
    RankSnapshot,
    browse_cursor,
    decode_cursor,
    parse_rank_cursor,
    rank_cursor,
)
from app.search.parse import ParsedQuery, parse_search_text
from app.search.rank import CANDIDATE_POOL, RANK_SNAPSHOT_LIMIT, fuse

_CARD_COLUMNS = """
    assets.id, assets.title, assets.creator_id, assets.creator_name,
    assets.location_label, assets.created_year_start, assets.created_year_end,
    assets.thumbnail_key, assets.ingested_at
"""

_PUBLIC_FROM = f"FROM {catalog.READY_ASSETS_VIEW} AS assets"


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


class SearchQueryError(Exception):
    """The request cannot be searched. The message is safe to return."""


class SearchService:
    def __init__(self, engine: Engine, embeddings: EmbeddingProvider) -> None:
        self._engine = engine
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
        where, params = _filters(parsed, creator_id, location, year_from, year_to)
        if not parsed.applied_query:
            return self._browse(where, params, limit, cursor, parsed.applied_query)
        return self._ranked(
            parsed,
            where,
            params,
            limit,
            cursor,
            creator_id=creator_id,
            location=location,
            year_from=year_from,
            year_to=year_to,
        )

    def asset(self, asset_id: UUID) -> AssetDetail | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    f"""
                    SELECT asset_id, title, creator_id, creator_name, location_label,
                           created_year_start, created_year_end, thumbnail_key, ingested_at,
                           description, original_key, width, height, landing_url,
                           rights_type, evidence_url, verified_at
                    FROM {catalog.READY_ASSETS_VIEW}
                    WHERE asset_id = :asset_id
                    """
                ),
                {"asset_id": asset_id},
            ).one_or_none()
        if row is None:
            return None
        return AssetDetail(
            card=_card(row),
            description=row.description,
            original_key=row.original_key,
            width=row.width,
            height=row.height,
            landing_url=row.landing_url,
            rights_type=row.rights_type,
            evidence_url=row.evidence_url,
            verified_at=row.verified_at,
        )

    def related(self, asset_id: UUID, limit: int) -> list[RelatedHit] | None:
        """None when the asset is not public. An empty list has no active vector."""
        with self._engine.connect() as connection:
            visible = connection.execute(
                text(
                    f"""
                    SELECT 1 FROM {catalog.READY_ASSETS_VIEW} WHERE asset_id = :asset_id
                    """
                ),
                {"asset_id": asset_id},
            ).one_or_none()
            if visible is None:
                return None
            rows = connection.execute(
                text(
                    f"""
                    SELECT {_CARD_COLUMNS},
                           (
                               assets.creator_id IS NOT NULL
                               AND assets.creator_id = seed.creator_id
                           ) AS same_creator,
                           (
                               assets.location_label IS NOT NULL
                               AND assets.location_label = seed.location_label
                           ) AS same_location,
                           (
                               cardinality(assets.style_tags) > 0
                               AND assets.style_tags && seed.style_tags
                           ) AS same_style
                    {_PUBLIC_FROM}
                    JOIN {catalog.READY_ASSETS_VIEW} AS seed ON seed.id = :asset_id
                    JOIN {catalog.EMBEDDINGS_TABLE} AS embedding
                      ON embedding.asset_id = assets.id
                     AND embedding.model_key = :model_key
                     AND embedding.model_version = :model_version
                    JOIN {catalog.EMBEDDINGS_TABLE} AS seed_embedding
                      ON seed_embedding.asset_id = seed.id
                     AND seed_embedding.model_key = :model_key
                     AND seed_embedding.model_version = :model_version
                    WHERE assets.id <> seed.id
                    ORDER BY embedding.embedding <=> seed_embedding.embedding, assets.id
                    LIMIT :limit
                    """
                ),
                {
                    "asset_id": asset_id,
                    "model_key": self._embeddings.model_key,
                    "model_version": self._embeddings.model_version,
                    "limit": limit,
                },
            ).all()
        hits: list[RelatedHit] = []
        for row in rows:
            reasons: list[str] = []
            if row.same_creator:
                reasons.append("same_creator")
            if row.same_location:
                reasons.append("same_location")
            if row.same_style:
                reasons.append("same_style_tag")
            hits.append(RelatedHit(card=_card(row), reasons=tuple(reasons)))
        return hits

    def creators(self, query: str | None, limit: int) -> CreatorPage:
        hint = " ".join((query or "").split())
        with self._engine.connect() as connection:
            if not hint:
                rows = connection.execute(
                    text(
                        """
                        SELECT id, name FROM creators
                        ORDER BY name, id
                        LIMIT :limit
                        """
                    ),
                    {"limit": limit},
                ).all()
                return CreatorPage(
                    creators=[CreatorHit(id=row.id, name=row.name) for row in rows],
                    did_you_mean=None,
                )
            rows = connection.execute(
                text(_CREATOR_SIMILARITY),
                {"query": hint, "limit": limit},
            ).all()
        creators = [CreatorHit(id=row.id, name=row.name) for row in rows]
        suggestion = None
        if creators and creators[0].name.casefold() != hint.casefold():
            suggestion = creators[0].name
        return CreatorPage(creators=creators, did_you_mean=suggestion)

    def _browse(
        self,
        where: str,
        params: dict[str, Any],
        limit: int,
        cursor: str | None,
        applied: str,
    ) -> SearchPage:
        cursor_at = None
        cursor_id = None
        if cursor is not None:
            payload = _expect_cursor(cursor, "browse")
            try:
                cursor_at = datetime.fromisoformat(payload["at"])
                cursor_id = UUID(payload["id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise InvalidCursor() from exc
        statement = f"""
            SELECT {_CARD_COLUMNS}
            {_PUBLIC_FROM}
            WHERE {where}
        """
        query_params = dict(params)
        if cursor_at is not None:
            statement += """
              AND (assets.ingested_at, assets.id) < (:cursor_at, :cursor_id)
            """
            query_params["cursor_at"] = cursor_at
            query_params["cursor_id"] = cursor_id
        statement += """
            ORDER BY assets.ingested_at DESC, assets.id DESC
            LIMIT :limit
        """
        query_params["limit"] = limit + 1
        with self._engine.connect() as connection:
            rows = connection.execute(text(statement), query_params).all()
        page, more = _page(rows, limit)
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
        where: str,
        params: dict[str, Any],
        limit: int,
        cursor: str | None,
        *,
        creator_id: UUID | None,
        location: str | None,
        year_from: int | None,
        year_to: int | None,
    ) -> SearchPage:
        scope = _rank_scope(
            parsed,
            creator_id=creator_id,
            location=location,
            year_from=year_from,
            year_to=year_to,
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
        with self._engine.connect() as connection:
            lexical = _lexical_ids(connection, parsed, where, params)
            semantic: list[UUID] = []
            if vector is not None:
                semantic = _semantic_ids(
                    connection,
                    where,
                    params,
                    vector,
                    self._embeddings.model_key,
                    self._embeddings.model_version,
                )
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
        cards = self._cards(page_ids)
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

    def _cards(self, asset_ids: list[UUID]) -> list[AssetCard]:
        if not asset_ids:
            return []
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"""
                    SELECT {_CARD_COLUMNS}
                    {_PUBLIC_FROM}
                    WHERE assets.id = ANY(:ids)
                    """
                ),
                {"ids": asset_ids},
            ).all()
        by_id = {row.id: _card(row) for row in rows}
        return [by_id[asset_id] for asset_id in asset_ids if asset_id in by_id]

    def _suggest(self, hint: str) -> str | None:
        if not hint:
            return None
        with self._engine.connect() as connection:
            row = connection.execute(
                text(_CREATOR_SIMILARITY),
                {"query": hint, "limit": 1},
            ).one_or_none()
        if row is None or row.name.casefold() == hint.casefold():
            return None
        return row.name


def _rank_scope(
    parsed: ParsedQuery,
    *,
    creator_id: UUID | None,
    location: str | None,
    year_from: int | None,
    year_to: int | None,
    model_key: str,
    model_version: str,
) -> RankSnapshot:
    return RankSnapshot(
        applied_query=parsed.applied_query,
        phrases=parsed.phrases,
        unquoted=parsed.unquoted,
        creator_id=None if creator_id is None else str(creator_id),
        location=location or None,
        year_from=year_from,
        year_to=year_to,
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


def _filters(
    parsed: ParsedQuery,
    creator_id: UUID | None,
    location: str | None,
    year_from: int | None,
    year_to: int | None,
) -> tuple[str, dict[str, Any]]:
    # Visibility is the ready_assets view. These clauses are the caller's filters.
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if creator_id is not None:
        clauses.append("assets.creator_id = :creator_id")
        params["creator_id"] = creator_id
    if location:
        clauses.append("assets.location_label = :location")
        params["location"] = location
    if year_from is not None or year_to is not None:
        clauses.append(
            "(assets.created_year_start IS NOT NULL OR assets.created_year_end IS NOT NULL)"
        )
    if year_from is not None:
        clauses.append(
            "COALESCE(assets.created_year_end, assets.created_year_start) >= :year_from"
        )
        params["year_from"] = year_from
    if year_to is not None:
        clauses.append(
            "COALESCE(assets.created_year_start, assets.created_year_end) <= :year_to"
        )
        params["year_to"] = year_to
    for index, phrase in enumerate(parsed.phrases):
        key = f"phrase_{index}"
        clauses.append(f"assets.lexeme_simple @@ phraseto_tsquery('simple', :{key})")
        params[key] = phrase
    if not clauses:
        return "TRUE", params
    return " AND ".join(clauses), params


def _lexical_ids(
    connection: Any,
    parsed: ParsedQuery,
    where: str,
    params: dict[str, Any],
) -> list[UUID]:
    if parsed.quotes_only:
        rank = " + ".join(
            f"ts_rank_cd(assets.lexeme_simple, phraseto_tsquery('simple', :phrase_{index}))"
            for index in range(len(parsed.phrases))
        )
        statement = f"""
            SELECT assets.id
            {_PUBLIC_FROM}
            WHERE {where}
            ORDER BY {rank} DESC, assets.id
            LIMIT :pool
        """
        rows = connection.execute(text(statement), {**params, "pool": CANDIDATE_POOL}).all()
        return [row.id for row in rows]
    if not parsed.unquoted:
        return []
    statement = f"""
        SELECT assets.id
        {_PUBLIC_FROM}
        WHERE {where}
          AND (
              assets.lexeme_simple @@ plainto_tsquery('simple', :unquoted)
              OR assets.lexeme_english @@ plainto_tsquery('english', :unquoted)
          )
        ORDER BY
            ts_rank_cd(assets.lexeme_simple, plainto_tsquery('simple', :unquoted))
          + ts_rank_cd(assets.lexeme_english, plainto_tsquery('english', :unquoted))
            DESC,
            assets.id
        LIMIT :pool
    """
    rows = connection.execute(
        text(statement),
        {**params, "unquoted": parsed.unquoted, "pool": CANDIDATE_POOL},
    ).all()
    return [row.id for row in rows]


def _semantic_ids(
    connection: Any,
    where: str,
    params: dict[str, Any],
    vector: list[float],
    model_key: str,
    model_version: str,
) -> list[UUID]:
    statement = f"""
        SELECT assets.id
        {_PUBLIC_FROM}
        JOIN {catalog.EMBEDDINGS_TABLE} AS embedding
          ON embedding.asset_id = assets.id
         AND embedding.model_key = :model_key
         AND embedding.model_version = :model_version
        WHERE {where}
        ORDER BY embedding.embedding <=> CAST(:vector AS vector), assets.id
        LIMIT :pool
    """
    rows = connection.execute(
        text(statement),
        {
            **params,
            "model_key": model_key,
            "model_version": model_version,
            "vector": _vector_literal(vector),
            "pool": CANDIDATE_POOL,
        },
    ).all()
    return [row.id for row in rows]


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(format(value, ".8g") for value in values) + "]"


def _card(row: Any) -> AssetCard:
    return AssetCard(
        id=row.id if hasattr(row, "id") else row.asset_id,
        title=row.title,
        creator_id=row.creator_id,
        creator_name=row.creator_name,
        location_label=row.location_label,
        created_year_start=row.created_year_start,
        created_year_end=row.created_year_end,
        thumbnail_key=row.thumbnail_key,
        ingested_at=row.ingested_at,
    )


def _page(rows: list[Any], limit: int) -> tuple[list[AssetCard], bool]:
    more = len(rows) > limit
    return [_card(row) for row in rows[:limit]], more


def _expect_cursor(cursor: str, kind: str) -> dict[str, Any]:
    payload = decode_cursor(cursor)
    if payload.get("t") != kind:
        raise InvalidCursor()
    return payload


_CREATOR_SIMILARITY = """
    SELECT id, name
    FROM creators
    WHERE name % :query
       OR EXISTS (
            SELECT 1
            FROM jsonb_array_elements_text(aliases) AS alias
            WHERE alias % :query
       )
    ORDER BY GREATEST(
        similarity(name, :query),
        COALESCE((
            SELECT max(similarity(alias, :query))
            FROM jsonb_array_elements_text(aliases) AS alias
        ), 0)
    ) DESC, name, id
    LIMIT :limit
"""

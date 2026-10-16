"""PostgreSQL search reads through ready_assets using the API database role."""

from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from app.db import catalog
from app.search.models import (
    AssetCard,
    AssetDetail,
    BrowsePosition,
    CreatorHit,
    RankedCandidates,
    RelatedHit,
    SearchFilters,
)
from app.search.parse import ParsedQuery
from app.search.rank import CANDIDATE_POOL

_CARD_COLUMNS = """
    assets.id, assets.title, assets.creator_id, assets.creator_name,
    assets.location_label, assets.created_year_start, assets.created_year_end,
    assets.thumbnail_key, assets.ingested_at
"""

_PUBLIC_FROM = f"FROM {catalog.READY_ASSETS_VIEW} AS assets"


class SearchRepository(Protocol):
    """Public catalog reads. Results contain no driver rows or SQL fragments."""

    def browse(
        self,
        filters: SearchFilters,
        limit: int,
        after: BrowsePosition | None,
    ) -> list[AssetCard]:
        """Return up to limit public cards, newest first, after the keyset position."""
        ...

    def candidates(
        self,
        parsed: ParsedQuery,
        filters: SearchFilters,
        vector: list[float] | None,
        model_key: str,
        model_version: str,
    ) -> RankedCandidates:
        """Return independently ranked lexical and optional semantic candidates."""
        ...

    def cards(self, asset_ids: list[UUID]) -> list[AssetCard]:
        """Return currently public cards in input order, omitting hidden IDs."""
        ...

    def asset(self, asset_id: UUID) -> AssetDetail | None: ...

    def related(
        self,
        asset_id: UUID,
        limit: int,
        model_key: str,
        model_version: str,
    ) -> list[RelatedHit] | None:
        """None means a hidden seed; an empty list means no related matches."""
        ...

    def creators(self, hint: str, limit: int) -> list[CreatorHit]:
        """List alphabetically for an empty hint, otherwise by similarity."""
        ...


class PostgresSearchRepository:
    def __init__(self, engine: Engine) -> None:
        # Construction must not check out a connection: inference runs first.
        self._engine = engine

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

    def related(
        self,
        asset_id: UUID,
        limit: int,
        model_key: str,
        model_version: str,
    ) -> list[RelatedHit] | None:
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
                    "model_key": model_key,
                    "model_version": model_version,
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

    def creators(self, hint: str, limit: int) -> list[CreatorHit]:
        with self._engine.connect() as connection:
            if not hint:
                rows = connection.execute(
                    text("SELECT id, name FROM creators ORDER BY name, id LIMIT :limit"),
                    {"limit": limit},
                ).all()
            else:
                rows = connection.execute(
                    text(_CREATOR_SIMILARITY),
                    {"query": hint, "limit": limit},
                ).all()
        return [CreatorHit(id=row.id, name=row.name) for row in rows]

    def browse(
        self,
        filters: SearchFilters,
        limit: int,
        after: BrowsePosition | None,
    ) -> list[AssetCard]:
        where, params = _filters(ParsedQuery("", (), ""), filters)
        statement = f"""
            SELECT {_CARD_COLUMNS}
            {_PUBLIC_FROM}
            WHERE {where}
        """
        query_params = dict(params)
        if after is not None:
            statement += """
              AND (assets.ingested_at, assets.id) < (:cursor_at, :cursor_id)
            """
            query_params["cursor_at"] = after.ingested_at
            query_params["cursor_id"] = after.id
        statement += """
            ORDER BY assets.ingested_at DESC, assets.id DESC
            LIMIT :limit
        """
        query_params["limit"] = limit
        with self._engine.connect() as connection:
            rows = connection.execute(text(statement), query_params).all()
        return [_card(row) for row in rows]

    def candidates(
        self,
        parsed: ParsedQuery,
        filters: SearchFilters,
        vector: list[float] | None,
        model_key: str,
        model_version: str,
    ) -> RankedCandidates:
        where, params = _filters(parsed, filters)
        with self._engine.connect() as connection:
            lexical = _lexical_ids(connection, parsed, where, params)
            semantic: list[UUID] = []
            if vector is not None:
                semantic = _semantic_ids(
                    connection,
                    where,
                    params,
                    vector,
                    model_key,
                    model_version,
                )
        return RankedCandidates(lexical=lexical, semantic=semantic)

    def cards(self, asset_ids: list[UUID]) -> list[AssetCard]:
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


def _filters(
    parsed: ParsedQuery,
    filters: SearchFilters,
) -> tuple[str, dict[str, Any]]:
    creator_id = filters.creator_id
    location = filters.location
    year_from = filters.year_from
    year_to = filters.year_to
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
        clauses.append("COALESCE(assets.created_year_end, assets.created_year_start) >= :year_from")
        params["year_from"] = year_from
    if year_to is not None:
        clauses.append("COALESCE(assets.created_year_start, assets.created_year_end) <= :year_to")
        params["year_to"] = year_to
    for index, phrase in enumerate(parsed.phrases):
        key = f"phrase_{index}"
        clauses.append(f"assets.lexeme_simple @@ phraseto_tsquery('simple', :{key})")
        params[key] = phrase
    if not clauses:
        return "TRUE", params
    return " AND ".join(clauses), params


def _lexical_ids(
    connection: Connection,
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
    connection: Connection,
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

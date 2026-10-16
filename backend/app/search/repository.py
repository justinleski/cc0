"""Execute public search queries using the API role and map typed results."""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from app.search import queries
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


class SearchRepository:
    def __init__(self, engine: Engine) -> None:
        # Construction must not check out a connection: inference runs first.
        self._engine = engine

    def asset(self, asset_id: UUID) -> AssetDetail | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                text(queries.ASSET_DETAIL),
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
                text(queries.PUBLIC_ASSET_EXISTS),
                {"asset_id": asset_id},
            ).one_or_none()
            if visible is None:
                return None
            rows = connection.execute(
                text(queries.RELATED),
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
                    text(queries.CREATORS),
                    {"limit": limit},
                ).all()
            else:
                rows = connection.execute(
                    text(queries.CREATOR_SIMILARITY),
                    {"query": hint, "limit": limit},
                ).all()
        return [CreatorHit(id=row.id, name=row.name) for row in rows]

    def browse(
        self,
        filters: SearchFilters,
        limit: int,
        after: BrowsePosition | None,
    ) -> list[AssetCard]:
        statement, params = queries.browse_query(filters, limit, after)
        with self._engine.connect() as connection:
            rows = connection.execute(text(statement), params).all()
        return [_card(row) for row in rows]

    def candidates(
        self,
        parsed: ParsedQuery,
        filters: SearchFilters,
        vector: list[float] | None,
        model_key: str,
        model_version: str,
    ) -> RankedCandidates:
        with self._engine.connect() as connection:
            lexical = _candidate_ids(connection, queries.lexical_query(parsed, filters))
            semantic: list[UUID] = []
            if vector is not None:
                semantic = _candidate_ids(
                    connection,
                    queries.semantic_query(parsed, filters, vector, model_key, model_version),
                )
        return RankedCandidates(lexical=lexical, semantic=semantic)

    def cards(self, asset_ids: list[UUID]) -> list[AssetCard]:
        if not asset_ids:
            return []
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(queries.CARDS),
                {"ids": asset_ids},
            ).all()
        by_id = {row.id: _card(row) for row in rows}
        return [by_id[asset_id] for asset_id in asset_ids if asset_id in by_id]


def _candidate_ids(
    connection: Connection,
    query: tuple[str, dict[str, Any]] | None,
) -> list[UUID]:
    if query is None:
        return []
    statement, params = query
    return [row.id for row in connection.execute(text(statement), params).all()]


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

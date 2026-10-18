"""Execute recommendation queries with the API role and map typed results."""

import json
from collections.abc import Sequence
from typing import Any
from uuid import UUID

import numpy as np
from sqlalchemy import Engine, text

from app.boards.models import BoardNotFound
from app.recommendations import queries
from app.recommendations.models import BoardPins, CandidateHit, Probe, ProfilePin
from app.search.models import AssetCard


class RecommendationRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def board_pins(
        self, owner_id: UUID, board_id: UUID, model_key: str, model_version: str
    ) -> BoardPins:
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(queries.BOARD_PINS),
                {
                    "owner_id": owner_id,
                    "board_id": board_id,
                    "model_key": model_key,
                    "model_version": model_version,
                },
            ).all()
        if not rows:
            raise BoardNotFound("board not found")
        saved = frozenset(row.asset_id for row in rows if row.asset_id is not None)
        pins = tuple(
            ProfilePin(
                asset_id=row.asset_id,
                position=row.position,
                title=row.title,
                creator_id=row.creator_id,
                style_tags=tuple(row.style_tags or ()),
                vector=_vector(row.embedding),
            )
            for row in rows
            if row.eligible and row.embedding is not None
        )
        return BoardPins(saved, pins, rows[0].updated_at.isoformat())

    def pins_for(
        self, asset_ids: Sequence[UUID], model_key: str, model_version: str
    ) -> tuple[ProfilePin, ...]:
        """Evaluation boards: public, embedded pins in the given order."""
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(queries.PINS_BY_ID),
                {
                    "asset_ids": list(asset_ids),
                    "model_key": model_key,
                    "model_version": model_version,
                },
            ).all()
        by_id = {row.asset_id: row for row in rows}
        return tuple(
            ProfilePin(
                asset_id=asset_id,
                position=position,
                title=by_id[asset_id].title,
                creator_id=by_id[asset_id].creator_id,
                style_tags=tuple(by_id[asset_id].style_tags or ()),
                vector=_vector(by_id[asset_id].embedding),
            )
            for position, asset_id in enumerate(asset_ids)
            if asset_id in by_id
        )

    def corpus_ids(self, model_key: str, model_version: str) -> list[UUID]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(queries.CORPUS_IDS),
                {"model_key": model_key, "model_version": model_version},
            ).all()
        return [row.id for row in rows]

    def candidates(
        self,
        probes: Sequence[Probe],
        exclude_ids: frozenset[UUID],
        model_key: str,
        model_version: str,
    ) -> list[CandidateHit]:
        if not probes:
            return []
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(queries.CANDIDATES),
                {
                    "probes": [queries.vector_literal(probe.vector) for probe in probes],
                    "limits": [probe.limit for probe in probes],
                    "exclude_ids": sorted(exclude_ids),
                    "model_key": model_key,
                    "model_version": model_version,
                },
            ).all()
        return [
            CandidateHit(
                probe_index=row.probe_index,
                card=_card(row),
                style_tags=tuple(row.style_tags or ()),
                vector=_vector(row.embedding),
            )
            for row in rows
        ]


def _vector(value: str) -> np.ndarray:
    return np.asarray(json.loads(value), dtype=np.float64)


def _card(row: Any) -> AssetCard:
    return AssetCard(
        id=row.id,
        title=row.title,
        creator_id=row.creator_id,
        creator_name=row.creator_name,
        location_label=row.location_label,
        created_year_start=row.created_year_start,
        created_year_end=row.created_year_end,
        thumbnail_key=row.thumbnail_key,
        ingested_at=row.ingested_at,
        width=row.width,
        height=row.height,
    )

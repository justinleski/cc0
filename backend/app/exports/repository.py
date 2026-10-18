"""Owner-scoped export transactions for the API role."""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from app.exports import queries
from app.exports.models import (
    Export,
    ExportEmpty,
    ExportLimitReached,
    ExportNotFound,
    ExportTooLarge,
)


def _export(row: Any) -> Export:
    status = row.status
    if status == "READY" and row.expired:
        status = "EXPIRED"
    return Export(
        id=row.id,
        board_id=row.board_id,
        status=status,
        failure_reason=row.failure_reason,
        included_count=row.included_count,
        excluded_count=row.excluded_count,
        created_at=row.created_at,
        completed_at=row.completed_at,
        expires_at=row.expires_at,
    )


class ExportRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create(
        self,
        owner_id: UUID,
        board_id: UUID,
        *,
        max_assets: int,
        max_active: int,
        max_attempts: int,
    ) -> Export:
        """Record the request, its pin snapshot, and its job in one transaction.

        The board row lock is the one every board mutation takes, so the snapshot
        is a state the board really had. Only pins that are READY with verified
        CC0 evidence are captured; the worker rechecks them at packaging.
        """
        params = {"board_id": board_id, "owner_id": owner_id}
        with self._engine.begin() as connection:
            board = connection.execute(text(queries.LOCK_BOARD), params).one_or_none()
            if board is None:
                raise ExportNotFound("board not found")
            eligible = connection.execute(text(queries.ELIGIBLE_COUNT), params).scalar_one()
            if eligible == 0:
                raise ExportEmpty("board has no exportable images")
            if eligible > max_assets:
                raise ExportTooLarge(f"a board export holds at most {max_assets} images")
            active = connection.execute(text(queries.ACTIVE_COUNT), params).scalar_one()
            if active >= max_active:
                raise ExportLimitReached("too many exports in progress")
            created = connection.execute(
                text(queries.INSERT_EXPORT), {**params, "board_name": board.name}
            ).one()
            connection.execute(
                text(queries.SNAPSHOT_ITEMS), {"export_id": created.id, "board_id": board_id}
            )
            connection.execute(
                text(queries.INSERT_JOB),
                {
                    "dedupe_key": f"export:{created.id}",
                    "payload": json.dumps({"export_id": str(created.id)}),
                    "max_attempts": max_attempts,
                },
            )
        return _export(created)

    def get(self, owner_id: UUID, export_id: UUID) -> tuple[Export, str | None, bool]:
        """The export, its artifact key, and whether every packaged asset is still eligible."""
        with self._engine.connect() as connection:
            row = connection.execute(
                text(queries.GET_OWNED), {"export_id": export_id, "owner_id": owner_id}
            ).one_or_none()
            if row is None:
                raise ExportNotFound("export not found")
            suppressed = connection.execute(
                text(queries.SUPPRESSED_SINCE), {"export_id": export_id}
            ).scalar_one()
        return _export(row), row.artifact_key, suppressed == 0

    def list_for_board(self, owner_id: UUID, board_id: UUID, limit: int) -> list[Export]:
        params = {"board_id": board_id, "owner_id": owner_id}
        with self._engine.connect() as connection:
            if connection.execute(text(queries.BOARD_OWNED), params).one_or_none() is None:
                raise ExportNotFound("board not found")
            rows = connection.execute(text(queries.LIST_FOR_BOARD), {**params, "limit": limit})
            return [_export(row) for row in rows]

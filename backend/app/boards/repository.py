"""Owner-scoped transactions. A parent row lock serializes every board mutation."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from app.boards import queries
from app.boards.models import Board, BoardConflict, BoardItem, BoardNotFound


def _board(row: Any, items: tuple[BoardItem, ...] = ()) -> Board:
    return Board(row.id, row.name, row.description, row.created_at, row.updated_at, items)


def _read(connection: Connection, params: dict) -> Board:
    row = connection.execute(text(queries.OWNED), params).one()
    items = tuple(
        BoardItem(item.asset_id, item.position, item.notes)
        for item in connection.execute(text(queries.ITEMS), params)
    )
    return _board(row, items)


def _lock(connection: Connection, params: dict, *, read: bool = False) -> None:
    row = connection.execute(
        text(queries.READ_OWNED if read else queries.LOCK_OWNED), params
    ).one_or_none()
    if row is None:
        raise BoardNotFound("board not found")


def _positions(connection: Connection, params: dict, ids: list[UUID]) -> None:
    # Deferred uniqueness permits swaps; nonnegative/unique constraints hold at commit.
    if ids:
        connection.execute(
            text(queries.POSITION),
            [
                {**params, "asset_id": asset_id, "position": index}
                for index, asset_id in enumerate(ids)
            ],
        )


class BoardRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create(
        self, owner_id: UUID, name: str, description: str,
        idempotency_key: str | None, request_hash: bytes | None,
    ) -> Board:
        with self._engine.begin() as connection:
            params = {"owner_id": owner_id, "name": name, "description": description}
            if idempotency_key is None:
                return _board(connection.execute(text(queries.CREATE), params).one())
            operation = {
                "owner_id": owner_id,
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
            }
            claimed = connection.execute(text(queries.CLAIM_CREATE), operation).one_or_none()
            if claimed is not None:
                return _board(connection.execute(
                    text(queries.CREATE_CLAIMED), {**params, "board_id": claimed.board_id}
                ).one())
            previous = connection.execute(text(queries.READ_CREATE), operation).one()
            if previous.request_hash != request_hash:
                raise BoardConflict("idempotency key already used with different details")
            board = connection.execute(
                text(queries.READ_OWNED), {"owner_id": owner_id, "board_id": previous.board_id}
            ).one_or_none()
            if board is None:
                raise BoardNotFound("board created by this operation was deleted")
            return _read(connection, {"owner_id": owner_id, "board_id": previous.board_id})

    def list(self, owner_id: UUID, limit: int, offset: int) -> list[Board]:
        with self._engine.connect() as connection:
            return [
                _board(row)
                for row in connection.execute(
                    text(queries.LIST), {"owner_id": owner_id, "limit": limit, "offset": offset}
                )
            ]

    def read(self, owner_id: UUID, board_id: UUID) -> Board:
        params = {"owner_id": owner_id, "board_id": board_id}
        with self._engine.begin() as connection:
            _lock(connection, params, read=True)
            return _read(connection, params)

    def edit(
        self, owner_id: UUID, board_id: UUID, name: str | None, description: str | None
    ) -> Board:
        params = {
            "owner_id": owner_id,
            "board_id": board_id,
            "name": name,
            "description": description,
        }
        with self._engine.begin() as connection:
            _lock(connection, params)
            connection.execute(text(queries.EDIT), params)
            return _read(connection, params)

    def delete(self, owner_id: UUID, board_id: UUID) -> None:
        params = {"owner_id": owner_id, "board_id": board_id}
        with self._engine.begin() as connection:
            _lock(connection, params)
            connection.execute(text(queries.DELETE), params)

    def change_item(
        self,
        owner_id: UUID,
        board_id: UUID,
        asset_id: UUID,
        action: Literal["add", "remove", "notes"],
        notes: str = "",
    ) -> Board:
        params = {"owner_id": owner_id, "board_id": board_id, "asset_id": asset_id, "notes": notes}
        statement = {"add": queries.ADD, "remove": queries.REMOVE, "notes": queries.NOTES}[action]
        with self._engine.begin() as connection:
            _lock(connection, params)
            changed = connection.execute(text(statement), params).scalar_one_or_none()
            if changed is None:
                if action == "add":
                    raise BoardConflict("asset unavailable or already pinned")
                raise BoardNotFound("board item not found")
            if action == "remove":
                ids = [row.asset_id for row in connection.execute(text(queries.ALL_ITEMS), params)]
                _positions(connection, params, ids)
            connection.execute(text(queries.TOUCH), params)
            return _read(connection, params)

    def reorder(self, owner_id: UUID, board_id: UUID, asset_ids: list[UUID]) -> Board:
        params = {"owner_id": owner_id, "board_id": board_id}
        with self._engine.begin() as connection:
            _lock(connection, params)
            rows = connection.execute(text(queries.ALL_ITEMS), params).all()
            visible = {row.asset_id for row in rows if row.available}
            if len(asset_ids) != len(visible) or set(asset_ids) != visible:
                raise BoardConflict("items changed; reload the board before reordering")
            hidden = [row.asset_id for row in rows if not row.available]
            _positions(connection, params, asset_ids + hidden)
            connection.execute(text(queries.TOUCH), params)
            return _read(connection, params)

"""Board business validation. Persistence owns authorization and atomic mutations."""

from __future__ import annotations

from uuid import UUID

from app.boards.models import Board, InvalidBoardChange
from app.boards.repository import BoardRepository


def _name(value: str) -> str:
    value = value.strip()
    if not 1 <= len(value) <= 200 or "\x00" in value:
        raise InvalidBoardChange("name must contain 1–200 characters")
    return value


def _text(value: str) -> str:
    if len(value) > 5000 or "\x00" in value:
        raise InvalidBoardChange("text must contain at most 5000 characters and no null bytes")
    return value


class BoardService:
    def __init__(self, repository: BoardRepository) -> None:
        self._repository = repository

    def create(self, owner_id: UUID, name: str, description: str) -> Board:
        return self._repository.create(owner_id, _name(name), _text(description))

    def list(self, owner_id: UUID, limit: int, offset: int) -> list[Board]:
        return self._repository.list(owner_id, limit, offset)

    def read(self, owner_id: UUID, board_id: UUID) -> Board:
        return self._repository.read(owner_id, board_id)

    def edit(
        self, owner_id: UUID, board_id: UUID, name: str | None, description: str | None
    ) -> Board:
        if name is None and description is None:
            raise InvalidBoardChange("provide name or description")
        return self._repository.edit(
            owner_id,
            board_id,
            _name(name) if name is not None else None,
            _text(description) if description is not None else None,
        )

    def delete(self, owner_id: UUID, board_id: UUID) -> None:
        self._repository.delete(owner_id, board_id)

    def add(self, owner_id: UUID, board_id: UUID, asset_id: UUID, notes: str) -> Board:
        return self._repository.change_item(owner_id, board_id, asset_id, "add", _text(notes))

    def remove(self, owner_id: UUID, board_id: UUID, asset_id: UUID) -> Board:
        return self._repository.change_item(owner_id, board_id, asset_id, "remove")

    def notes(self, owner_id: UUID, board_id: UUID, asset_id: UUID, notes: str) -> Board:
        return self._repository.change_item(owner_id, board_id, asset_id, "notes", _text(notes))

    def reorder(self, owner_id: UUID, board_id: UUID, asset_ids: list[UUID]) -> Board:
        if len(asset_ids) != len(set(asset_ids)):
            raise InvalidBoardChange("asset_ids must be unique")
        return self._repository.reorder(owner_id, board_id, asset_ids)

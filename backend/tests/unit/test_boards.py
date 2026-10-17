import hashlib
import json
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.boards import queries
from app.boards.models import InvalidBoardChange
from app.boards.service import BoardService


def test_service_normalizes_names_preserves_notes_and_passes_verified_owner():
    repository = Mock()
    service = BoardService(repository)
    owner, board, asset = uuid4(), uuid4(), uuid4()
    service.create(owner, "  Landscape  ", "notes", "operation-1")
    fingerprint = hashlib.sha256(json.dumps(
        {"name": "  Landscape  ", "description": "notes"},
        separators=(",", ":"), ensure_ascii=False,
    ).encode()).digest()
    repository.create.assert_called_once_with(
        owner, "Landscape", "notes", "operation-1", fingerprint
    )
    service.notes(owner, board, asset, "  meaningful spacing\n")
    repository.change_item.assert_called_once_with(
        owner, board, asset, "notes", "  meaningful spacing\n"
    )
    service.reorder(owner, board, [asset])
    repository.reorder.assert_called_once_with(owner, board, [asset])


@pytest.mark.parametrize("name", [" ", "x" * 201, "bad\x00name"])
def test_invalid_names_never_write(name):
    repository = Mock()
    with pytest.raises(InvalidBoardChange):
        BoardService(repository).create(uuid4(), name, "")
    repository.create.assert_not_called()


def test_invalid_reorder_and_notes_never_write():
    repository = Mock()
    service = BoardService(repository)
    owner, board, asset = uuid4(), uuid4(), uuid4()
    with pytest.raises(InvalidBoardChange):
        service.reorder(owner, board, [asset, asset])
    with pytest.raises(InvalidBoardChange):
        service.notes(owner, board, asset, "x" * 5001)
    with pytest.raises(InvalidBoardChange):
        service.edit(owner, board, None, None)
    assert repository.mock_calls == []


def test_create_without_key_keeps_original_contract_and_rejects_invalid_keys():
    repository = Mock()
    service = BoardService(repository)
    owner = uuid4()
    service.create(owner, "  Board  ", "")
    repository.create.assert_called_once_with(owner, "Board", "", None, None)
    repository.reset_mock()
    for key in ("", "space key", "x" * 129, "é"):
        with pytest.raises(InvalidBoardChange, match="Idempotency-Key"):
            service.create(owner, "Board", "", key)
    repository.create.assert_not_called()


def test_board_sql_binds_owner_and_eligibility_is_the_public_view():
    for name in [
        "OWNED",
        "LOCK_OWNED",
        "READ_OWNED",
        "LIST",
        "CREATE",
        "CLAIM_CREATE",
        "READ_CREATE",
        "CREATE_CLAIMED",
        "EDIT",
        "DELETE",
        "TOUCH",
        "ITEMS",
        "ALL_ITEMS",
        "ADD",
        "REMOVE",
        "NOTES",
        "POSITION",
    ]:
        assert ":owner_id" in getattr(queries, name)
    assert "ready_assets" in queries.ADD
    assert "ready_assets" in queries.ITEMS
    assert "FOR UPDATE" in queries.LOCK_OWNED

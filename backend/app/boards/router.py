"""Authenticated board HTTP boundary and dependency wiring."""

from collections.abc import Iterator
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.auth.dependencies import current_user
from app.auth.local import Identity
from app.boards.models import Board, BoardConflict, BoardNotFound, InvalidBoardChange
from app.boards.repository import BoardRepository
from app.boards.service import BoardService

router = APIRouter(prefix="/api/v1/boards", tags=["boards"])
User = Annotated[Identity, Depends(current_user)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateBoard(Input):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)


class EditBoard(Input):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)

    @model_validator(mode="before")
    @classmethod
    def no_null_fields(cls, value):
        if isinstance(value, dict) and any(item is None for item in value.values()):
            raise ValueError("omit unchanged fields; use an empty string to clear description")
        return value


class AddItem(Input):
    asset_id: UUID
    notes: str = Field(default="", max_length=5000)


class EditNotes(Input):
    notes: str = Field(max_length=5000)


class ReorderItems(Input):
    asset_ids: list[UUID] = Field(max_length=10000)


class BoardSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    description: str
    created_at: datetime
    updated_at: datetime


class ItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    asset_id: UUID
    position: int
    notes: str


class BoardResponse(BoardSummary):
    items: list[ItemResponse]


class BoardList(BaseModel):
    items: list[BoardSummary]


def service(request: Request) -> Iterator[BoardService]:
    try:
        yield BoardService(BoardRepository(request.app.state.db_engine))
    except BoardNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except BoardConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except InvalidBoardChange as exc:
        raise HTTPException(400, str(exc)) from exc


Service = Annotated[BoardService, Depends(service)]


@router.post("", response_model=BoardResponse, status_code=201)
def create_board(
    body: CreateBoard,
    user: User,
    boards: Service,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Board:
    return boards.create(user.id, body.name, body.description, idempotency_key)


@router.get("", response_model=BoardList)
def list_boards(
    user: User,
    boards: Service,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return {"items": boards.list(user.id, limit, offset)}


@router.get("/{board_id}", response_model=BoardResponse)
def read_board(board_id: UUID, user: User, boards: Service) -> Board:
    return boards.read(user.id, board_id)


@router.patch("/{board_id}", response_model=BoardResponse)
def edit_board(board_id: UUID, body: EditBoard, user: User, boards: Service) -> Board:
    return boards.edit(user.id, board_id, body.name, body.description)


@router.delete("/{board_id}", status_code=204)
def delete_board(board_id: UUID, user: User, boards: Service) -> Response:
    boards.delete(user.id, board_id)
    return Response(status_code=204)


@router.post("/{board_id}/items", response_model=BoardResponse, status_code=201)
def add_item(board_id: UUID, body: AddItem, user: User, boards: Service) -> Board:
    return boards.add(user.id, board_id, body.asset_id, body.notes)


@router.put("/{board_id}/items/order", response_model=BoardResponse)
def reorder_items(board_id: UUID, body: ReorderItems, user: User, boards: Service) -> Board:
    return boards.reorder(user.id, board_id, body.asset_ids)


@router.patch("/{board_id}/items/{asset_id}", response_model=BoardResponse)
def edit_notes(
    board_id: UUID, asset_id: UUID, body: EditNotes, user: User, boards: Service
) -> Board:
    return boards.notes(user.id, board_id, asset_id, body.notes)


@router.delete("/{board_id}/items/{asset_id}", response_model=BoardResponse)
def remove_item(board_id: UUID, asset_id: UUID, user: User, boards: Service) -> Board:
    return boards.remove(user.id, board_id, asset_id)

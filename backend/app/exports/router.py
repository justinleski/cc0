"""Authenticated export HTTP boundary."""

from collections.abc import Iterator
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from app.auth.dependencies import current_user
from app.auth.local import Identity
from app.exports.models import (
    Export,
    ExportEmpty,
    ExportLimitReached,
    ExportNotFound,
    ExportTooLarge,
)
from app.exports.repository import ExportRepository
from app.exports.service import ExportService
from app.integrations.object_store import ObjectStore, create_s3_client

router = APIRouter(prefix="/api/v1", tags=["exports"])
User = Annotated[Identity, Depends(current_user)]


class ExportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    board_id: UUID | None
    status: str
    failure_reason: str | None
    included_count: int | None
    excluded_count: int | None
    created_at: datetime
    completed_at: datetime | None
    expires_at: datetime | None
    download_url: str | None
    download_expires_in: int | None


class ExportList(BaseModel):
    items: list[ExportResponse]


def service(request: Request) -> Iterator[ExportService]:
    state = request.app.state
    # Signing is local, but the signature binds the host the browser will call.
    public = state.settings.object_store_public_endpoint
    client = create_s3_client(state.settings, endpoint=public) if public else state.s3_client
    try:
        yield ExportService(
            ExportRepository(state.db_engine),
            ObjectStore(client, state.settings.object_store_bucket),
            state.settings,
        )
    except ExportNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ExportEmpty as exc:
        raise HTTPException(409, str(exc)) from exc
    except ExportTooLarge as exc:
        raise HTTPException(400, str(exc)) from exc
    except ExportLimitReached as exc:
        raise HTTPException(429, str(exc)) from exc
    finally:
        if public:
            client.close()


Exports = Annotated[ExportService, Depends(service)]


@router.post("/boards/{board_id}/exports", response_model=ExportResponse, status_code=202)
def create_export(board_id: UUID, user: User, exports: Exports) -> Export:
    return exports.create(user.id, board_id)


@router.get("/boards/{board_id}/exports", response_model=ExportList)
def list_exports(
    board_id: UUID, user: User, exports: Exports, limit: int = Query(default=10, ge=1, le=50)
) -> dict:
    return {"items": exports.list_for_board(user.id, board_id, limit)}


@router.get("/exports/{export_id}", response_model=ExportResponse)
def read_export(export_id: UUID, user: User, exports: Exports) -> Export:
    return exports.read(user.id, export_id)

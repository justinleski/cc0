from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.config import Settings
from app.integrations.readiness import check_object_store, check_postgres, run_checks

router = APIRouter(prefix="/api/v1")


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ReadyResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    checks: dict[str, Literal["ok", "error"]]


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/ready", response_model=ReadyResponse)
def ready(request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    checks = run_checks(
        {
            "postgres": lambda: check_postgres(
                request.app.state.db_engine, settings.readiness_timeout_seconds
            ),
            "object_store": lambda: check_object_store(
                request.app.state.s3_client, settings.object_store_bucket
            ),
        },
        request_id=request.state.request_id,
    )
    status: Literal["ok", "unavailable"] = (
        "ok" if all(value == "ok" for value in checks.values()) else "unavailable"
    )
    body = ReadyResponse(status=status, checks=checks)
    return JSONResponse(
        status_code=200 if status == "ok" else 503,
        content=body.model_dump(),
    )

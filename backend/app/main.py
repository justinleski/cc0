import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.boards.router import router as boards_router
from app.core.config import Settings, get_settings
from app.core.logging import RequestContextMiddleware, configure_logging
from app.db.engine import create_api_engine
from app.embeddings.provider import build_provider
from app.integrations.object_store import create_s3_client
from app.search.router import router as search_router


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()
    configured = settings or get_settings()
    cursor_key = configured.cursor_key()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = create_api_engine(app.state.settings)
        try:
            client = create_s3_client(app.state.settings)
            try:
                app.state.db_engine = engine
                app.state.s3_client = client
                # Fake embeddings prepare in memory. OpenCLIP loads here only
                # when that provider is configured, and checks the vector width.
                embeddings = build_provider(app.state.settings)
                embeddings.prepare()
                app.state.embeddings = embeddings
                yield
            finally:
                client.close()
        finally:
            engine.dispose()

    app = FastAPI(title="cc0", version="0.1.0", lifespan=lifespan)
    app.state.settings = configured
    app.state.cursor_key = cursor_key
    app.add_middleware(RequestContextMiddleware)

    @app.middleware("http")
    async def catalog_no_store(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(("/api/v1/assets", "/api/v1/creators", "/api/v1/boards")):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(health_router)
    app.include_router(search_router)
    app.include_router(boards_router)
    return app


# Uvicorn imports app.main:app and must fail fast when .env or the process
# environment is invalid. Pytest collection also imports this module; building
# the app here would validate the real environment and fail the unit suite when
# .env is absent or contains a typo the test is not about. Tests call create_app
# with an explicit Settings and never use this object.
if "pytest" not in sys.modules:
    app = create_app()

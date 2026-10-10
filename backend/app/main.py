from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.config import Settings, get_settings
from app.core.logging import RequestContextMiddleware, configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()
    app = FastAPI(title="cc0", version="0.1.0")
    app.state.settings = settings or get_settings()
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health_router)
    return app


app = create_app()

"""Connection pools. One per process, because a pool cannot cross a fork.

The API shares a single engine between request handlers and the readiness probe.
The worker runs as its own process ([ADR-0004]) and builds its own engine from
WORKER_DATABASE_URL; it must never import the API engine.

`statement_timeout` is deliberately absent from connect_args: setting it per
connection would leak the readiness budget into every later checkout of that
connection. The readiness probe sets it with SET LOCAL inside its transaction.

[ADR-0004]: ../../../docs/decisions/0004-host-processes.md
"""

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url

from app.core.config import Settings

# One API and one worker use 14 of roughly 97 usable PostgreSQL connections.
# Each extra API instance adds 10, not 1. See docs/m2-handoff.md.
API_POOL_SIZE = 5
API_MAX_OVERFLOW = 5
WORKER_POOL_SIZE = 2
WORKER_MAX_OVERFLOW = 2

POOL_RECYCLE_SECONDS = 1800
POOL_CHECKOUT_TIMEOUT_SECONDS = 10.0
# psycopg counts connect_timeout in whole seconds.
CONNECT_TIMEOUT_SECONDS = 5


def _create_engine(url: str, pool_size: int, max_overflow: int) -> Engine:
    return create_engine(
        make_url(url).set(drivername="postgresql+psycopg"),
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=POOL_CHECKOUT_TIMEOUT_SECONDS,
        pool_pre_ping=True,
        pool_recycle=POOL_RECYCLE_SECONDS,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )


def create_api_engine(settings: Settings) -> Engine:
    """The API pool, shared by endpoints and GET /api/v1/ready."""
    return _create_engine(settings.database_url, API_POOL_SIZE, API_MAX_OVERFLOW)


def create_worker_engine(settings: Settings) -> Engine:
    """The worker pool. Disposed when the worker process exits."""
    return _create_engine(settings.worker_database_url, WORKER_POOL_SIZE, WORKER_MAX_OVERFLOW)

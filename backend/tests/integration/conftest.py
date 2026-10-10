from socket import create_connection
from urllib.parse import urlsplit

import pytest

from app.core.config import Settings

COMPOSE_HINT = "docker compose up -d postgres rustfs rustfs-init"


def _require_tcp(name: str, url: str, default_port: int) -> None:
    endpoint = urlsplit(url)
    host = endpoint.hostname
    port = endpoint.port or default_port
    try:
        with create_connection((host, port), timeout=2):
            pass
    except OSError:
        # Host and port only. A database URL also carries the password.
        pytest.fail(f"{name} is not reachable at {host}:{port}. Start it with: {COMPOSE_HINT}")


@pytest.fixture
def running_stack() -> Settings:
    """Fail early and clearly when the local data services are down.

    Probing the ports separately keeps a stopped container from looking like bad
    credentials, a missing extension, or a missing bucket.
    """
    settings = Settings()
    _require_tcp("Postgres", settings.database_url, 5432)
    store = settings.object_store_endpoint
    _require_tcp("RustFS", store, 443 if urlsplit(store).scheme == "https" else 80)
    return settings

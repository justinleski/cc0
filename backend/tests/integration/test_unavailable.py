import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.engine import CONNECT_TIMEOUT_SECONDS
from app.integrations.readiness import DependencyUnavailable, check_object_store, check_postgres
from app.main import create_app

# TEST-NET-1. A closed 127.0.0.1 port returns connection refused immediately and
# does not exercise the timeout. This address should not RST.
_BLACKHOLE = "192.0.2.1"


def test_unreachable_dependencies_return_redacted_503():
    settings = Settings(
        _env_file=None,
        app_env="local",
        database_url=f"postgresql://api:secret-db-password@{_BLACKHOLE}:5432/cc0",
        object_store_endpoint=f"http://{_BLACKHOLE}:9000",
        object_store_access_key="distinctive-access-key",
        object_store_secret_key="secret-object-key",
        cursor_signing_key="0123456789abcdef" * 4,
        # Bounds the probe statement, not the connect: the shared API pool owns
        # connect_timeout now, and statement_timeout is SET LOCAL per probe.
        readiness_timeout_seconds=0.2,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        with pytest.raises(DependencyUnavailable):
            check_postgres(app.state.db_engine, settings.readiness_timeout_seconds)
        with pytest.raises(DependencyUnavailable):
            check_object_store(app.state.s3_client, settings.object_store_bucket)
        started = time.perf_counter()
        response = client.get("/api/v1/ready")
        elapsed = time.perf_counter() - started
        # Checks run one after another: the database waits out the pool's
        # connect_timeout, then the object store uses the readiness budget. An
        # unbounded connect would run far longer than this.
        assert elapsed < CONNECT_TIMEOUT_SECONDS + settings.readiness_timeout_seconds + 3
    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"postgres": "error", "object_store": "error"},
    }
    for secret in (
        "secret-db-password",
        "secret-object-key",
        "distinctive-access-key",
        _BLACKHOLE,
        "postgresql://",
        "http://",
    ):
        assert secret not in response.text

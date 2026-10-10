import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
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
        # Sub-second on purpose. psycopg connect_timeout still floors at 1 second.
        readiness_timeout_seconds=0.2,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        with pytest.raises(DependencyUnavailable):
            check_postgres(app.state.db_engine)
        with pytest.raises(DependencyUnavailable):
            check_object_store(app.state.s3_client, settings.object_store_bucket)
        started = time.perf_counter()
        response = client.get("/api/v1/ready")
        elapsed = time.perf_counter() - started
        # Checks run one after another. Postgres connect_timeout floors at 1s
        # and a blackhole can take a little longer than that; the object store
        # uses the fractional budget. Two probes plus slack stay under 6s.
        # An unlimited connect would run much longer than this bound.
        assert elapsed < 6
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

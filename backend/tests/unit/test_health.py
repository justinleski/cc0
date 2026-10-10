from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.readiness import DependencyUnavailable
from app.main import create_app


def _settings() -> Settings:
    return Settings(
        database_url="postgresql://cc0_api:secret-db-password@127.0.0.1:5433/cc0",
        object_store_secret_key="secret-object-key",
    )


def test_health_does_not_touch_dependencies(monkeypatch) -> None:
    def fail_postgres(_settings: Settings) -> None:
        raise AssertionError("health must not query postgres")

    def fail_object_store(_settings: Settings) -> None:
        raise AssertionError("health must not call object storage")

    monkeypatch.setattr("app.api.health.check_postgres", fail_postgres)
    monkeypatch.setattr("app.api.health.check_object_store", fail_object_store)

    response = TestClient(create_app(_settings())).get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"]


def test_ready_succeeds_when_both_checks_pass(monkeypatch) -> None:
    monkeypatch.setattr("app.api.health.check_postgres", lambda _settings: None)
    monkeypatch.setattr("app.api.health.check_object_store", lambda _settings: None)

    response = TestClient(create_app(_settings())).get(
        "/api/v1/ready",
        headers={"X-Request-ID": "req-ready"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"postgres": "ok", "object_store": "ok"},
    }
    assert response.headers["x-request-id"] == "req-ready"


def test_ready_fails_when_one_dependency_is_down(monkeypatch) -> None:
    def down(_settings: Settings) -> None:
        raise DependencyUnavailable("OperationalError password=secret-db-password")

    monkeypatch.setattr("app.api.health.check_postgres", down)
    monkeypatch.setattr("app.api.health.check_object_store", lambda _settings: None)

    response = TestClient(create_app(_settings())).get("/api/v1/ready")
    body = response.text

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"postgres": "error", "object_store": "ok"},
    }
    assert "secret-db-password" not in body
    assert "secret-object-key" not in body


def test_ready_reports_both_failures(monkeypatch) -> None:
    def down(_settings: Settings) -> None:
        raise DependencyUnavailable("unavailable")

    monkeypatch.setattr("app.api.health.check_postgres", down)
    monkeypatch.setattr("app.api.health.check_object_store", down)

    response = TestClient(create_app(_settings())).get("/api/v1/ready")

    assert response.status_code == 503
    assert response.json()["checks"] == {"postgres": "error", "object_store": "error"}

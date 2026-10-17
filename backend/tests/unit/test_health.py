from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.readiness import DependencyUnavailable
from app.main import create_app


def _settings(**overrides) -> Settings:
    values = {
        "app_env": "local",
        "database_url": "postgresql://cc0_api:secret-db-password@127.0.0.1:5432/cc0",
        "object_store_access_key": "secret-access-key",
        "object_store_secret_key": "secret-object-key",
        "cursor_signing_key": "0123456789abcdef" * 4,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_health_does_not_touch_dependencies(monkeypatch) -> None:
    def fail_postgres(_settings: Settings) -> None:
        raise AssertionError("health must not query postgres")

    def fail_object_store(_settings: Settings) -> None:
        raise AssertionError("health must not call object storage")

    monkeypatch.setattr("app.api.health.check_postgres", fail_postgres)
    monkeypatch.setattr("app.api.health.check_object_store", fail_object_store)

    with TestClient(create_app(_settings())) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"]


def test_ready_succeeds_when_both_checks_pass(monkeypatch) -> None:
    monkeypatch.setattr("app.api.health.check_postgres", lambda *args: None)
    monkeypatch.setattr("app.api.health.check_object_store", lambda *args: None)

    with TestClient(create_app(_settings())) as client:
        response = client.get(
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
    def down(*args) -> None:
        raise DependencyUnavailable("OperationalError password=secret-db-password")

    monkeypatch.setattr("app.api.health.check_postgres", down)
    monkeypatch.setattr("app.api.health.check_object_store", lambda *args: None)

    with TestClient(create_app(_settings())) as client:
        response = client.get("/api/v1/ready")
    body = response.text

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"postgres": "error", "object_store": "ok"},
    }
    assert "secret-db-password" not in body
    assert "secret-object-key" not in body


def test_ready_reports_both_failures(monkeypatch) -> None:
    def down(*args) -> None:
        raise DependencyUnavailable("unavailable")

    monkeypatch.setattr("app.api.health.check_postgres", down)
    monkeypatch.setattr("app.api.health.check_object_store", down)

    with TestClient(create_app(_settings())) as client:
        response = client.get("/api/v1/ready")

    assert response.status_code == 503
    assert response.json()["checks"] == {"postgres": "error", "object_store": "error"}


def test_unexpected_readiness_error_is_500_without_driver_text(monkeypatch, caplog):
    def broken(*args):
        raise RuntimeError("password=secret-db-password driver text")

    monkeypatch.setattr("app.api.health.check_postgres", broken)
    with TestClient(create_app(_settings()), raise_server_exceptions=False) as client:
        response = client.get("/api/v1/ready", headers={"X-Request-ID": "req-bug"})
    assert response.status_code == 500
    assert "request_id=req-bug" in caplog.text
    assert "check=postgres" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "secret-db-password" not in caplog.text
    assert "driver text" not in caplog.text


def test_readiness_logs_cause_class_not_message(monkeypatch, caplog):
    class OperationalError(Exception):
        pass

    def down(*args):
        cause = OperationalError("password=secret-db-password")
        raise DependencyUnavailable(type(cause).__name__) from cause

    monkeypatch.setattr("app.api.health.check_postgres", down)
    monkeypatch.setattr("app.api.health.check_object_store", lambda *args: None)
    with TestClient(create_app(_settings())) as client:
        client.get("/api/v1/ready", headers={"X-Request-ID": "req-down"})
    assert "request_id=req-down check=postgres error_type=OperationalError" in caplog.text
    assert "error_type=DependencyUnavailable" not in caplog.text
    assert "secret-db-password" not in caplog.text


def _connect_args(engine) -> dict:
    # SQLAlchemy keeps connect_args on the pool creator, not on Engine.
    return next(
        cell.cell_contents
        for cell in engine.pool._creator.__closure__
        if isinstance(cell.cell_contents, dict) and "connect_timeout" in cell.cell_contents
    )


def test_api_and_worker_pools_are_separate_and_carry_no_statement_timeout():
    from app.db.engine import (
        API_MAX_OVERFLOW,
        API_POOL_SIZE,
        CONNECT_TIMEOUT_SECONDS,
        POOL_CHECKOUT_TIMEOUT_SECONDS,
        WORKER_MAX_OVERFLOW,
        WORKER_POOL_SIZE,
        create_api_engine,
        create_worker_engine,
    )

    settings = _settings(
        readiness_timeout_seconds=0.2,
        worker_database_url="postgresql://cc0_worker:secret-worker@127.0.0.1:5432/cc0",
    )
    api = create_api_engine(settings)
    worker = create_worker_engine(settings)
    try:
        assert (api.pool.size(), api.pool._max_overflow) == (API_POOL_SIZE, API_MAX_OVERFLOW)
        assert (worker.pool.size(), worker.pool._max_overflow) == (
            WORKER_POOL_SIZE,
            WORKER_MAX_OVERFLOW,
        )
        # Each process has its own login, so the pools cannot be swapped.
        assert api.url.username == "cc0_api"
        assert worker.url.username == "cc0_worker"
        for engine in (api, worker):
            kwargs = _connect_args(engine)
            assert kwargs["connect_timeout"] == CONNECT_TIMEOUT_SECONDS
            # A connection-level statement_timeout would outlive the readiness
            # probe that wants it and apply to later request work.
            assert "options" not in kwargs
            assert "statement_timeout" not in kwargs
            # The checkout budget is a request budget, not the readiness budget.
            assert engine.pool.timeout() == POOL_CHECKOUT_TIMEOUT_SECONDS
    finally:
        api.dispose()
        worker.dispose()


def test_lifespan_reuses_and_closes_resources(monkeypatch):
    from unittest.mock import Mock

    engine, s3 = Mock(), Mock()
    build_engine, build_s3 = Mock(return_value=engine), Mock(return_value=s3)
    monkeypatch.setattr("app.main.create_api_engine", build_engine)
    monkeypatch.setattr("app.main.create_s3_client", build_s3)
    postgres, store = Mock(), Mock()
    monkeypatch.setattr("app.api.health.check_postgres", postgres)
    monkeypatch.setattr("app.api.health.check_object_store", store)
    settings = _settings()
    with TestClient(create_app(settings)) as client:
        client.get("/api/v1/ready")
        client.get("/api/v1/ready")
        postgres.assert_called_with(engine, settings.readiness_timeout_seconds)
        store.assert_called_with(s3, settings.object_store_bucket)
        engine.dispose.assert_not_called()
    build_engine.assert_called_once_with(settings)
    build_s3.assert_called_once_with(settings)
    engine.dispose.assert_called_once()
    s3.close.assert_called_once()

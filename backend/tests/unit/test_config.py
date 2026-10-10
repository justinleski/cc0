import pytest

from app.core.config import Settings


def _production(**overrides: str) -> Settings:
    values: dict[str, str] = {
        "app_env": "production",
        "migration_database_url": "postgresql://admin:real-migrator@db.example/cc0",
        "database_url": "postgresql://api:real-api@db.example/cc0",
        "worker_database_url": "postgresql://worker:real-worker@db.example/cc0",
        "cc0_migrator_password": "real-migrator",
        "cc0_api_password": "real-api",
        "cc0_worker_password": "real-worker",
        "object_store_endpoint": "https://objects.example.com",
        "object_store_access_key": "real-access",
        "object_store_secret_key": "real-secret",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_env_file_rejects_typo_but_allows_compose_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text("POSTGRES_USER=cc0\nAPI_PORT=8000\nDATABSE_URL=secret\n")
    with pytest.raises(ValueError, match="DATABSE_URL"):
        Settings(_env_file=env)
    env.write_text("POSTGRES_USER=cc0\nAPI_PORT=8000\nDATABASE_URL=postgresql://custom/db\n")
    assert Settings(_env_file=env).database_url == "postgresql://custom/db"


def test_process_environment_rejects_settings_typo(monkeypatch):
    monkeypatch.setenv("OBJECT_STORE_ENDPOIN", "http://127.0.0.1:9")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("POSTGRES_USER", "cc0")
    with pytest.raises(ValueError, match="OBJECT_STORE_ENDPOIN"):
        Settings(_env_file=None)


def test_process_environment_allows_compose_allowlist_and_path(monkeypatch):
    monkeypatch.setenv("POSTGRES_USER", "cc0")
    monkeypatch.setenv("POSTGRES_PASSWORD", "local-only")
    monkeypatch.setenv("API_PORT", "8000")
    monkeypatch.setenv("S3_BUCKET", "cc0")
    monkeypatch.setenv("PATH", "/tmp/custom-path")
    monkeypatch.setenv("HOME", "/tmp/custom-home")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert Settings(_env_file=None).app_env == "local"


def test_nonlocal_defaults_are_rejected():
    with pytest.raises(ValueError, match="dummy credentials"):
        Settings(_env_file=None, app_env="production")


def test_production_rejects_dummy_password_empty_url_password_and_loopback_endpoint():
    with pytest.raises(ValueError, match="dummy credentials"):
        _production(cc0_api_password="cc0_api_local_password")
    with pytest.raises(ValueError, match="password in every database URL"):
        _production(database_url="postgresql://api@db.example/cc0")
    with pytest.raises(ValueError, match="password in every database URL"):
        _production(worker_database_url="postgresql://worker:@db.example/cc0")
    with pytest.raises(ValueError, match="non-local object store endpoint"):
        _production(object_store_endpoint="http://127.0.0.1:9000")
    with pytest.raises(ValueError, match="non-local object store endpoint"):
        _production(object_store_endpoint="http://localhost:9000")


def test_production_accepts_explicit_credentials_and_nonlocal_endpoint():
    settings = _production(
        cc0_api_password="prefix-cc0_api_local_password-suffix",
        object_store_endpoint="https://s3.amazonaws.com",
    )
    assert settings.app_env == "production"
    assert settings.object_store_endpoint == "https://s3.amazonaws.com"


def test_app_env_local_ignores_case_and_whitespace(monkeypatch):
    settings = Settings(_env_file=None, app_env="Local")
    assert settings.object_store_endpoint == "http://127.0.0.1:9000"
    spaced = Settings(_env_file=None, app_env=" local ")
    assert spaced.app_env == " local "
    monkeypatch.setenv("APP_ENV", "Local")
    assert Settings(_env_file=None).app_env == "Local"

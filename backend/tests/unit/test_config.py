import hashlib
from uuid import UUID

import pytest

from app.core.config import Settings
from app.main import create_app
from app.search.cursor import InvalidCursor, RankSnapshot, expect_cursor, rank_cursor


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
    monkeypatch.setenv("DATABASE_PASSWORD", "ambient-not-read")
    monkeypatch.setenv("API_PORT", "8000")
    monkeypatch.setenv("S3_BUCKET", "cc0")
    monkeypatch.setenv("PATH", "/tmp/custom-path")
    monkeypatch.setenv("HOME", "/tmp/custom-home")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    settings = Settings(_env_file=None)
    assert settings.app_env == "local"
    assert "ambient-not-read" not in settings.database_url


def test_database_url_typo_is_still_rejected(monkeypatch):
    monkeypatch.setenv("DATABASE_URLL", "postgresql://api:secret@127.0.0.1/cc0")
    with pytest.raises(ValueError, match="DATABASE_URLL"):
        Settings(_env_file=None)


def test_catalog_vector_identity_is_independent_of_inference_provider():
    settings = Settings(
        _env_file=None,
        embedding_provider="fake",
        catalog_embedding_model_key="ViT-B-32",
        catalog_embedding_model_version="laion2b_s34b_b79k",
    )
    assert settings.catalog_embedding_identity("fake", "deterministic-v1") == (
        "ViT-B-32", "laion2b_s34b_b79k"
    )
    assert Settings(_env_file=None).catalog_embedding_identity("fake", "deterministic-v1") == (
        "fake", "deterministic-v1"
    )
    with pytest.raises(ValueError, match="must be set together"):
        Settings(_env_file=None, catalog_embedding_model_key="ViT-B-32")


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


def test_api_requires_an_independent_256_bit_cursor_secret():
    settings = Settings(_env_file=None)
    with pytest.raises(ValueError, match="CURSOR_SIGNING_KEY"):
        create_app(settings)
    for value in ("cc0_api_local_password", "0" * 64, "short", "x" * 64):
        with pytest.raises(ValueError, match="CURSOR_SIGNING_KEY"):
            create_app(Settings(_env_file=None, cursor_signing_key=value))
    key = "0123456789abcdef" * 4
    with pytest.raises(ValueError, match="independent"):
        create_app(Settings(
            _env_file=None,
            database_url=f"postgresql://cc0_api:{key}@localhost/cc0",
            cursor_signing_key=key,
        ))
    configured = Settings(_env_file=None, cursor_signing_key=key)
    assert create_app(configured).state.cursor_key == bytes.fromhex(key)
    assert key not in repr(configured)


def test_published_database_password_cannot_sign_a_ranked_cursor():
    configured = Settings(_env_file=None, cursor_signing_key="0123456789abcdef" * 4)
    app = create_app(configured)
    legacy_key = hashlib.sha256(
        b"cc0-ranked-cursor-v1\0cc0_api_local_password"
    ).digest()
    forged = rank_cursor(
        RankSnapshot(
            applied_query="coastal", phrases=(), unquoted="coastal",
            creator_id=None, location=None, year_from=None, year_to=None,
            model_key="fake", model_version="deterministic-v1",
            ids=(UUID(int=1), UUID(int=2)), offset=1,
        ),
        legacy_key,
    )
    with pytest.raises(InvalidCursor):
        expect_cursor(forged, "rank", app.state.cursor_key)

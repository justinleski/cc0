from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import jwt
import pytest
from fastapi.testclient import TestClient

from app.auth.local import (
    AUDIENCE,
    ISSUER,
    AuthenticationUnavailable,
    InvalidToken,
    identity,
    issue_token,
    verify_token,
)
from app.core.config import Settings
from app.main import create_app

KEY = "unit-tests-only-signing-secret-32-bytes"


def local_settings(**overrides):
    return Settings(
        _env_file=None, app_env="local", auth_mode="local", auth_local_signing_key=KEY,
        cursor_signing_key="0123456789abcdef" * 4, **overrides
    )


def test_local_accounts_are_deterministic_and_tokens_are_verified():
    settings = local_settings()
    assert verify_token(settings, issue_token(settings, "alice")) == identity("alice")
    assert identity("alice") != identity("bob")
    with pytest.raises(InvalidToken):
        verify_token(settings, str(identity("alice").id))
    with pytest.raises(InvalidToken):
        issue_token(settings, "administrator")


@pytest.mark.parametrize(
    "change",
    [
        {"exp": 0},
        {"iss": "other"},
        {"aud": "other"},
        {"sub": "local:mallory"},
        {"iat": 9999999999},
        {"exp": None},
    ],
)
def test_invalid_claims_are_rejected(change):
    now = datetime.now(UTC)
    claims = {
        "sub": "local:alice",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + timedelta(hours=1),
        **change,
    }
    token = jwt.encode(claims, KEY, algorithm="HS256")
    with pytest.raises(InvalidToken):
        verify_token(local_settings(), token)


def test_signature_algorithm_and_missing_claims_are_rejected():
    settings = local_settings()
    valid = issue_token(settings, "alice")
    claims = jwt.decode(valid, KEY, algorithms=["HS256"], audience=AUDIENCE)
    for token in [
        jwt.encode(claims, "wrong-secret", algorithm="HS256"),
        jwt.encode(claims, "", algorithm="none"),
        jwt.encode(claims, KEY, algorithm="HS384"),
        jwt.encode({"sub": "local:alice"}, KEY, algorithm="HS256"),
    ]:
        with pytest.raises(InvalidToken):
            verify_token(settings, token)


def test_auth_requires_opt_in_explicit_environment_and_secret(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    with pytest.raises(ValueError, match="explicitly configured"):
        Settings(_env_file=None, auth_mode="local", auth_local_signing_key=KEY)
    for env in ["production", "staging", "locla"]:
        with pytest.raises(ValueError, match="explicitly configured"):
            Settings(_env_file=None, app_env=env, auth_mode="local", auth_local_signing_key=KEY)
    with pytest.raises(ValueError, match="32 bytes"):
        Settings(_env_file=None, app_env="local", auth_mode="local", auth_local_signing_key="short")
    assert Settings(_env_file=None).auth_mode == "disabled"
    assert KEY not in repr(local_settings())


def test_test_environment_is_permitted_without_weakening_existing_credential_rules():
    from tests.unit.test_config import _production

    settings = _production(app_env="test", auth_mode="local", auth_local_signing_key=KEY)
    assert verify_token(settings, issue_token(settings, "bob")) == identity("bob")


def test_disabled_or_production_auth_never_reaches_database():
    settings = local_settings()
    for blocked in [
        settings.model_copy(update={"auth_mode": "disabled"}),
        settings.model_copy(update={"app_env": "production"}),
    ]:
        with pytest.raises(AuthenticationUnavailable):
            issue_token(blocked, "alice")
        app = create_app(blocked)
        app.state.db_engine = Mock()
        with TestClient(app) as client:
            database = Mock()
            app.state.db_engine = database
            response = client.get(
                "/api/v1/boards",
                headers={"Authorization": f"Bearer {issue_token(settings, 'alice')}"},
            )
        assert response.status_code == 503
        database.begin.assert_not_called()


@pytest.mark.parametrize(
    "headers", [{}, {"X-User-ID": str(identity("alice").id)}, {"Authorization": "Bearer forged"}]
)
def test_untrusted_credentials_do_not_reach_database(headers):
    app = create_app(local_settings())
    with TestClient(app) as client:
        database = Mock()
        app.state.db_engine = database
        response = client.get("/api/v1/boards", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"
    database.begin.assert_not_called()

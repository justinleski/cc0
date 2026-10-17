"""Signed local accounts. Token issuance is an operator CLI, never a public route."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

import jwt

from app.core.config import Settings

ISSUER = "cc0-local"
AUDIENCE = "cc0-api"
ACCOUNTS = ("alice", "bob")


@dataclass(frozen=True)
class Identity:
    id: UUID
    subject: str


class AuthenticationUnavailable(Exception):
    pass


class InvalidToken(Exception):
    pass


def require_local(settings: Settings) -> None:
    # Defense in depth for test overrides/model_copy as well as normal Settings.
    if (
        settings.auth_mode != "local"
        or "app_env" not in settings.model_fields_set
        or settings.app_env.strip().lower() not in {"local", "test"}
        or len(settings.auth_local_signing_key.get_secret_value().encode()) < 32
    ):
        raise AuthenticationUnavailable("authentication unavailable")


def identity(account: str) -> Identity:
    if account not in ACCOUNTS:
        raise InvalidToken("invalid credentials")
    subject = f"local:{account}"
    return Identity(uuid5(NAMESPACE_URL, f"cc0:{subject}"), subject)


def issue_token(settings: Settings, account: str) -> str:
    require_local(settings)
    user = identity(account)
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": user.subject,
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        settings.auth_local_signing_key.get_secret_value(),
        algorithm="HS256",
    )


def verify_token(settings: Settings, token: str) -> Identity:
    require_local(settings)
    try:
        claims = jwt.decode(
            token,
            settings.auth_local_signing_key.get_secret_value(),
            algorithms=["HS256"],
            issuer=ISSUER,
            audience=AUDIENCE,
            options={"require": ["sub", "iss", "aud", "iat", "exp"]},
        )
        if claims["sub"] not in {f"local:{name}" for name in ACCOUNTS}:
            raise InvalidToken("invalid credentials")
        return identity(claims["sub"].removeprefix("local:"))
    except jwt.InvalidTokenError as exc:
        raise InvalidToken("invalid credentials") from exc

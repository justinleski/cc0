"""Authenticate before any board repository operation; never trust a client user ID."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.local import (
    AuthenticationUnavailable,
    Identity,
    InvalidToken,
    require_local,
    verify_token,
)
from app.auth.repository import UserRepository

bearer = HTTPBearer(auto_error=False)


def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Identity:
    try:
        require_local(request.app.state.settings)
        if credentials is None:
            raise InvalidToken("invalid credentials")
        user = verify_token(request.app.state.settings, credentials.credentials)
    except AuthenticationUnavailable as exc:
        raise HTTPException(503, "authentication unavailable") from exc
    except InvalidToken as exc:
        raise HTTPException(
            401, "invalid credentials", headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    return UserRepository(request.app.state.db_engine).resolve(user)

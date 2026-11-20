"""Public contract of the identity module. The ONLY file other modules may import."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.shared import errors
from app.shared.events import Event
from app.shared.principal import Principal

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class UserRegistered(Event):
    user_id: str
    email: str
    name: str


def authenticated(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    """FastAPI dependency: verifies the bearer access token, returns the caller."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise errors.Unauthorized("missing bearer token")
    principal: Principal = request.app.state.platform.identity.verify(credentials.credentials)
    return principal


CurrentPrincipal = Annotated[Principal, Depends(authenticated)]


def require_group(group: str) -> Callable[[Principal], Principal]:
    """Action-level authorization: caller must belong to the Cognito group."""

    def _check(principal: CurrentPrincipal) -> Principal:
        if not principal.in_group(group):
            raise errors.Forbidden(f"requires group {group}")
        return principal

    return _check

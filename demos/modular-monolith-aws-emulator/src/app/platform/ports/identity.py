"""Identity provider port (Cognito on AWS and on the emulator)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.shared.principal import Principal


@dataclass(frozen=True, slots=True)
class SignUpResult:
    subject: str
    confirmed: bool


@dataclass(frozen=True, slots=True)
class Tokens:
    access_token: str
    id_token: str
    refresh_token: str | None
    expires_in: int
    token_type: str = "Bearer"  # noqa: S105


@dataclass(frozen=True, slots=True)
class UserInfo:
    subject: str
    email: str
    name: str


class IdentityProvider(Protocol):
    def sign_up(self, email: str, password: str, name: str) -> SignUpResult: ...
    def confirm(self, email: str, code: str) -> None: ...
    def authenticate(self, email: str, password: str) -> Tokens: ...
    def refresh(self, refresh_token: str) -> Tokens: ...
    def sign_out(self, access_token: str) -> None: ...
    def add_to_group(self, email: str, group: str) -> None: ...
    def user_info(self, access_token: str) -> UserInfo: ...


class TokenVerifier(Protocol):
    def verify(self, access_token: str) -> Principal:
        """Verify signature, issuer, expiry, audience. Raise Unauthorized on any failure."""
        ...

"""In-memory identity provider + verifier (tests only, never wired on AWS)."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass, field

from app.platform.ports.identity import SignUpResult, Tokens, UserInfo
from app.shared import errors
from app.shared.principal import Principal


@dataclass
class _User:
    subject: str
    email: str
    name: str
    salt: bytes
    digest: bytes
    confirmed: bool
    groups: set[str] = field(default_factory=set)


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)


class InMemoryIdentity:
    """Implements both IdentityProvider and TokenVerifier."""

    def __init__(self, *, auto_confirm: bool = True, min_password: int = 8) -> None:
        self._users: dict[str, _User] = {}
        self._access: dict[str, str] = {}
        self._refresh: dict[str, str] = {}
        self._auto_confirm = auto_confirm
        self._min_password = min_password
        self.codes: dict[str, str] = {}

    def sign_up(self, email: str, password: str, name: str) -> SignUpResult:
        if email in self._users:
            raise errors.Conflict("User already exists")
        if len(password) < self._min_password:
            raise errors.ValidationFailed("Password did not conform with policy")
        salt = secrets.token_bytes(16)
        user = _User(
            str(uuid.uuid4()), email, name, salt, _hash(password, salt), self._auto_confirm
        )
        self._users[email] = user
        self.codes[email] = f"{secrets.randbelow(10**6):06d}"
        return SignUpResult(user.subject, user.confirmed)

    def confirm(self, email: str, code: str) -> None:
        if self.codes.get(email) != code:
            raise errors.ValidationFailed("Invalid verification code")
        self._users[email].confirmed = True

    def authenticate(self, email: str, password: str) -> Tokens:
        user = self._users.get(email)
        if not user or not secrets.compare_digest(user.digest, _hash(password, user.salt)):
            raise errors.Unauthorized("invalid credentials")
        if not user.confirmed:
            raise errors.Forbidden("user not confirmed")
        refresh = secrets.token_urlsafe(32)
        self._refresh[refresh] = email
        return self._issue(email, refresh)

    def refresh(self, refresh_token: str) -> Tokens:
        email = self._refresh.get(refresh_token)
        if email is None:
            raise errors.Unauthorized("invalid credentials")
        return self._issue(email, refresh_token)

    def sign_out(self, access_token: str) -> None:
        email = self._access.get(access_token)
        if email is None:
            raise errors.Unauthorized("invalid token")
        self._access = {t: e for t, e in self._access.items() if e != email}
        self._refresh = {t: e for t, e in self._refresh.items() if e != email}

    def add_to_group(self, email: str, group: str) -> None:
        self._users[email].groups.add(group)

    def user_info(self, access_token: str) -> UserInfo:
        email = self._access.get(access_token)
        if email is None:
            raise errors.Unauthorized("invalid token")
        user = self._users[email]
        return UserInfo(user.subject, user.email, user.name)

    def admin_create(self, email: str, password: str, name: str) -> str:
        """Test helper: a user that exists in the provider but never went through /register."""
        salt = secrets.token_bytes(16)
        user = _User(str(uuid.uuid4()), email, name, salt, _hash(password, salt), True)
        self._users[email] = user
        return user.subject

    def verify(self, access_token: str) -> Principal:
        email = self._access.get(access_token)
        if email is None:
            raise errors.Unauthorized("invalid token")
        user = self._users[email]
        return Principal(user.subject, user.subject, frozenset(user.groups))

    def _issue(self, email: str, refresh: str) -> Tokens:
        access = secrets.token_urlsafe(32)
        self._access[access] = email
        return Tokens(access, secrets.token_urlsafe(32), refresh, 3600)

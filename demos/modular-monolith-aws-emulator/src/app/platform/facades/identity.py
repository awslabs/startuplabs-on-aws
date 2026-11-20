from __future__ import annotations

from app.platform.ports.identity import (
    IdentityProvider,
    SignUpResult,
    Tokens,
    TokenVerifier,
    UserInfo,
)
from app.shared.principal import Principal


class IdentityFacade:
    def __init__(self, provider: IdentityProvider, verifier: TokenVerifier) -> None:
        self._provider = provider
        self._verifier = verifier

    def register(self, email: str, password: str, name: str) -> SignUpResult:
        return self._provider.sign_up(email, password, name)

    def confirm(self, email: str, code: str) -> None:
        self._provider.confirm(email, code)

    def login(self, email: str, password: str) -> Tokens:
        return self._provider.authenticate(email, password)

    def refresh(self, refresh_token: str) -> Tokens:
        return self._provider.refresh(refresh_token)

    def logout(self, access_token: str) -> None:
        self._provider.sign_out(access_token)

    def grant_group(self, email: str, group: str) -> None:
        self._provider.add_to_group(email, group)

    def user_info(self, access_token: str) -> UserInfo:
        return self._provider.user_info(access_token)

    def verify(self, access_token: str) -> Principal:
        return self._verifier.verify(access_token)

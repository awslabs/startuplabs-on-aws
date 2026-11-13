"""Use cases. Orchestrate facade calls + repository + events; no HTTP, no boto3."""

from __future__ import annotations

import logging

from app.modules.identity.contract import UserRegistered
from app.modules.identity.domain import User
from app.modules.identity.infrastructure import UserRepository
from app.platform.facades import IdentityFacade
from app.platform.ports.identity import Tokens
from app.shared import errors
from app.shared.events import EventBus
from app.shared.principal import Principal

log = logging.getLogger(__name__)


class IdentityService:
    def __init__(self, identity: IdentityFacade, users: UserRepository, events: EventBus) -> None:
        self._identity = identity
        self._users = users
        self._events = events

    def register(self, email: str, password: str, name: str) -> User:
        email = email.lower()
        result = self._identity.register(email, password, name)
        user = User.register(result.subject, email, name, confirmed=result.confirmed)
        # Upsert: a retried registration after a partial failure converges.
        self._users.save(user)
        self._events.publish(UserRegistered(user.id, user.email, user.name))
        log.info("user registered", extra={"user_id": user.id})
        return user

    def confirm(self, email: str, code: str) -> None:
        self._identity.confirm(email.lower(), code)
        user = self._users.by_email(email)
        if user:
            user.activate()
            self._users.save(user)

    def login(self, email: str, password: str) -> Tokens:
        tokens = self._identity.login(email.lower(), password)
        principal = self._identity.verify(tokens.access_token)
        user = self._users.get(principal.subject)
        if user is None:
            # The provider has the user but the projection does not: registration
            # failed after sign-up, or an admin created the user. Rebuild it.
            info = self._identity.user_info(tokens.access_token)
            user = User.register(info.subject, info.email, info.name or info.email, confirmed=True)
            self._users.save(user)
            self._events.publish(UserRegistered(user.id, user.email, user.name))
            log.info("user projection rebuilt on login", extra={"user_id": user.id})
        elif user.status != "active":  # confirmed out of band (code, admin, auto-confirm)
            user.activate()
            self._users.save(user)
        return tokens

    def refresh(self, refresh_token: str) -> Tokens:
        return self._identity.refresh(refresh_token)

    def logout(self, access_token: str) -> None:
        self._identity.logout(access_token)

    def me(self, principal: Principal) -> User:
        user = self._users.get(principal.subject)
        if user is None:
            raise errors.NotFound("user profile not found")
        return user

    def list_users(self, limit: int, offset: int) -> list[User]:
        return self._users.list(limit=limit, offset=offset)

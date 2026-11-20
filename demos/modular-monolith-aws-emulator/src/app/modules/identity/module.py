from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from app.modules.base import ModuleSpec
from app.modules.identity.api import build_router
from app.modules.identity.application import IdentityService
from app.modules.identity.infrastructure import UserRepository
from app.platform.facades import Platform


def _router(platform: Platform) -> APIRouter:
    service = IdentityService(
        platform.identity, UserRepository(platform.database.sql), platform.events
    )
    return build_router(service)


MODULE = ModuleSpec(
    name="identity",
    summary="register, confirm, login, refresh, logout, me, admin user list (Cognito + SQL)",
    build_router=_router,
    migrations=Path(__file__).parent / "migrations",
)

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, EmailStr, Field

from app.modules.identity.application import IdentityService
from app.modules.identity.contract import CurrentPrincipal, require_group
from app.modules.identity.domain import User
from app.platform.ports.identity import Tokens
from app.shared import errors
from app.shared.principal import Principal


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    name: str = Field(min_length=1, max_length=120)


class ConfirmIn(BaseModel):
    email: EmailStr
    code: str = Field(min_length=1, max_length=16)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=8192)


class UserOut(BaseModel):
    id: str
    email: str
    name: str
    status: str
    created_at: datetime

    @classmethod
    def of(cls, user: User) -> UserOut:
        return cls(
            id=user.id,
            email=user.email,
            name=user.name,
            status=user.status.value,
            created_at=user.created_at,
        )


class TokensOut(BaseModel):
    access_token: str
    id_token: str
    refresh_token: str | None
    expires_in: int
    token_type: str

    @classmethod
    def of(cls, t: Tokens) -> TokensOut:
        return cls(
            access_token=t.access_token,
            id_token=t.id_token,
            refresh_token=t.refresh_token,
            expires_in=t.expires_in,
            token_type=t.token_type,
        )


def build_router(service: IdentityService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth", tags=["identity"])

    @router.post("/register", status_code=status.HTTP_201_CREATED)
    def register(body: RegisterIn) -> UserOut:
        return UserOut.of(service.register(body.email, body.password, body.name))

    @router.post("/confirm", status_code=status.HTTP_204_NO_CONTENT)
    def confirm(body: ConfirmIn) -> None:
        service.confirm(body.email, body.code)

    @router.post("/login")
    def login(body: LoginIn) -> TokensOut:
        return TokensOut.of(service.login(body.email, body.password))

    @router.post("/refresh")
    def refresh(body: RefreshIn) -> TokensOut:
        return TokensOut.of(service.refresh(body.refresh_token))

    @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    def logout(request: Request, _principal: CurrentPrincipal) -> None:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise errors.Unauthorized("missing bearer token")
        service.logout(header[7:])

    @router.get("/me")
    def me(principal: CurrentPrincipal) -> UserOut:
        return UserOut.of(service.me(principal))

    @router.get("/users")
    def list_users(
        _admin: Annotated[Principal, Depends(require_group("admin"))],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[UserOut]:
        return [UserOut.of(u) for u in service.list_users(limit, offset)]

    return router

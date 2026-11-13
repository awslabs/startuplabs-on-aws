from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.modules.base import ModuleSpec
from app.modules.identity.contract import CurrentPrincipal, UserRegistered
from app.modules.profiles.application import ProfileService
from app.modules.profiles.domain import Profile
from app.modules.profiles.infrastructure import ProfileRepository
from app.platform.facades import Platform


class ProfileOut(BaseModel):
    user_id: str
    display_name: str
    bio: str
    has_avatar: bool

    @classmethod
    def of(cls, p: Profile) -> ProfileOut:
        return cls(
            user_id=p.user_id, display_name=p.display_name, bio=p.bio, has_avatar=bool(p.avatar_key)
        )


class ProfileIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=60)
    bio: str | None = Field(default=None, max_length=500)


class AvatarUploadIn(BaseModel):
    content_type: str = Field(max_length=64)


class UrlOut(BaseModel):
    url: str
    key: str | None = None


def _router(platform: Platform) -> APIRouter:
    store, table = platform.database.documents("profiles")
    service = ProfileService(ProfileRepository(store, table), platform.storage)
    platform.events.subscribe(UserRegistered, service.on_user_registered)

    router = APIRouter(prefix="/api/v1/profiles", tags=["profiles"])

    @router.get("/me")
    def get_me(principal: CurrentPrincipal) -> ProfileOut:
        return ProfileOut.of(service.get(principal))

    @router.put("/me")
    def put_me(principal: CurrentPrincipal, body: ProfileIn) -> ProfileOut:
        return ProfileOut.of(service.update(principal, body.display_name, body.bio))

    @router.post("/me/avatar")
    def avatar_upload(principal: CurrentPrincipal, body: AvatarUploadIn) -> UrlOut:
        key, url = service.avatar_upload(principal, body.content_type)
        return UrlOut(url=url, key=key)

    @router.get("/me/avatar")
    def avatar_download(principal: CurrentPrincipal) -> UrlOut:
        return UrlOut(url=service.avatar_download(principal))

    return router


MODULE = ModuleSpec(
    name="profiles",
    summary="profile document (DynamoDB) + avatar via presigned S3 URLs; reacts to UserRegistered",
    build_router=_router,
    document_tables=("profiles",),
)

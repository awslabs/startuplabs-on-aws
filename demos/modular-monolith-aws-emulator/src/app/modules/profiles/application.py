from __future__ import annotations

from app.modules.identity.contract import UserRegistered
from app.modules.profiles.domain import Profile
from app.modules.profiles.infrastructure import ProfileRepository
from app.platform.facades import StorageFacade
from app.shared import errors
from app.shared.principal import Principal


class ProfileService:
    def __init__(self, profiles: ProfileRepository, storage: StorageFacade) -> None:
        self._profiles = profiles
        self._storage = storage

    def on_user_registered(self, event: UserRegistered) -> None:
        if self._profiles.get(event.user_id) is None:
            self._profiles.save(Profile(user_id=event.user_id, display_name=event.name[:60]))

    def get(self, principal: Principal) -> Profile:
        return self._profiles.require(principal.subject)

    def update(self, principal: Principal, display_name: str | None, bio: str | None) -> Profile:
        profile = self._profiles.require(principal.subject)
        profile.update(display_name, bio)
        self._profiles.save(profile)
        return profile

    def avatar_upload(self, principal: Principal, content_type: str) -> tuple[str, str]:
        """Returns (key, presigned PUT url). Client uploads straight to storage."""
        profile = self._profiles.require(principal.subject)
        key = profile.avatar_key_for(content_type)
        url = self._storage.upload_url(key, content_type)
        profile.avatar_key = key
        self._profiles.save(profile)
        return key, url

    def avatar_download(self, principal: Principal) -> str:
        profile = self._profiles.require(principal.subject)
        if not profile.avatar_key:
            raise errors.NotFound("no avatar")
        return self._storage.download_url(profile.avatar_key)

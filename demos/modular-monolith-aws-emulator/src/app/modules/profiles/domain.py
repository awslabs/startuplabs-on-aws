from __future__ import annotations

from dataclasses import dataclass

from app.shared import errors

AVATAR_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


@dataclass(slots=True)
class Profile:
    user_id: str
    display_name: str
    bio: str = ""
    avatar_key: str | None = None

    def update(self, display_name: str | None, bio: str | None) -> None:
        if display_name is not None:
            if not 1 <= len(display_name.strip()) <= 60:
                raise errors.ValidationFailed("display_name must have 1 to 60 characters")
            self.display_name = display_name.strip()
        if bio is not None:
            if len(bio) > 500:
                raise errors.ValidationFailed("bio must have at most 500 characters")
            self.bio = bio

    def avatar_key_for(self, content_type: str) -> str:
        ext = AVATAR_TYPES.get(content_type)
        if ext is None:
            raise errors.ValidationFailed(f"content type must be one of {sorted(AVATAR_TYPES)}")
        return f"avatars/{self.user_id}/avatar.{ext}"

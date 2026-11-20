from __future__ import annotations

from app.modules.profiles.domain import Profile
from app.platform.ports.database import Item
from app.repository import DocumentRepository


class ProfileRepository(DocumentRepository[Profile]):
    key_attribute = "pk"

    def to_item(self, entity: Profile) -> Item:
        item: Item = {
            "pk": entity.user_id,
            "display_name": entity.display_name,
            "bio": entity.bio,
        }
        if entity.avatar_key:
            item["avatar_key"] = entity.avatar_key
        return item

    def from_item(self, item: Item) -> Profile:
        return Profile(
            user_id=item["pk"],
            display_name=item["display_name"],
            bio=item.get("bio", ""),
            avatar_key=item.get("avatar_key"),
        )

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from app.shared import errors


class UserStatus(StrEnum):
    PENDING = "pending_confirmation"
    ACTIVE = "active"


@dataclass(slots=True)
class User:
    id: str
    email: str
    name: str
    status: UserStatus
    created_at: datetime

    @classmethod
    def register(cls, user_id: str, email: str, name: str, *, confirmed: bool) -> User:
        name = name.strip()
        if not 1 <= len(name) <= 120:
            raise errors.ValidationFailed("name must have 1 to 120 characters")
        return cls(
            id=user_id,
            email=email.lower(),
            name=name,
            status=UserStatus.ACTIVE if confirmed else UserStatus.PENDING,
            created_at=datetime.now(UTC).replace(microsecond=0),
        )

    def activate(self) -> None:
        self.status = UserStatus.ACTIVE

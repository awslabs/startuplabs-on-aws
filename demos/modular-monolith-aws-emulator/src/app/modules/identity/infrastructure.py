from __future__ import annotations

from datetime import UTC, datetime

from app.modules.identity.domain import User, UserStatus
from app.platform.ports.database import Row
from app.repository import SqlRepository


class UserRepository(SqlRepository[User]):
    table = "users"

    def to_row(self, entity: User) -> Row:
        return {
            "id": entity.id,
            "email": entity.email,
            "name": entity.name,
            "status": entity.status.value,
            "created_at": entity.created_at,
        }

    def from_row(self, row: Row) -> User:
        created = datetime.fromisoformat(str(row["created_at"])[:19]).replace(tzinfo=UTC)
        # RDS Data returns 'YYYY-MM-DD HH:MM:SS[.ffffff]'; SQLite returns the same text.
        return User(
            id=row["id"],
            email=row["email"],
            name=row["name"],
            status=UserStatus(row["status"]),
            created_at=created,
        )

    def by_email(self, email: str) -> User | None:
        return self.find_one("email", email.lower())

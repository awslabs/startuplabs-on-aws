from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.modules.identity.domain import User, UserStatus
from app.modules.identity.infrastructure import UserRepository
from app.platform.adapters.memory.stores import SqliteSqlDatabase
from app.repository import migrations
from app.shared import errors

MIGRATIONS = Path(__file__).resolve().parents[2] / "src/app/modules/identity/migrations"


@pytest.fixture
def repo() -> UserRepository:
    db = SqliteSqlDatabase()
    assert migrations.apply(db, [MIGRATIONS]) == ["identity/001_users.sql"]
    assert migrations.apply(db, [MIGRATIONS]) == []  # idempotent
    return UserRepository(db)


def _user(uid: str = "u1", email: str = "a@b.com") -> User:
    return User(uid, email, "A", UserStatus.PENDING, datetime(2026, 1, 1, tzinfo=UTC))


def test_crud(repo: UserRepository) -> None:
    repo.add(_user())
    assert repo.get("u1") == _user()
    u = _user()
    u.activate()
    repo.save(u)
    assert repo.by_email("A@B.com") == u  # lookups are case-insensitive
    assert [x.id for x in repo.list()] == ["u1"]
    repo.remove("u1")
    assert repo.get("u1") is None


def test_unique_email_is_conflict(repo: UserRepository) -> None:
    repo.add(_user("u1"))
    with pytest.raises(errors.Conflict):
        repo.add(_user("u2"))


def test_transaction_rolls_back() -> None:
    db = SqliteSqlDatabase()
    db.execute("CREATE TABLE t (id TEXT PRIMARY KEY)")
    with pytest.raises(RuntimeError), db.transaction() as tx:
        tx.execute("INSERT INTO t (id) VALUES (:id)", {"id": "x"})
        raise RuntimeError("boom")
    assert db.execute("SELECT * FROM t") == []


def test_identifiers_are_validated() -> None:
    class Bad(UserRepository):
        table = "users; DROP TABLE users"

    with pytest.raises(ValueError, match="invalid identifier"):
        Bad(SqliteSqlDatabase())


def test_split_statements() -> None:
    script = "-- c\nCREATE TABLE a (x int);\nCREATE TABLE b (y int);\n"
    assert migrations.split_statements(script) == [
        "CREATE TABLE a (x int)",
        "CREATE TABLE b (y int)",
    ]

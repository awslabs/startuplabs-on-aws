"""Generic repositories over the database ports.

Subclass and implement the two mapping functions; get/add/save/remove/list come
for free. Repositories depend on ports, so the same class runs on the emulator,
on AWS and on the in-memory adapters.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Protocol

from app.platform.ports.database import DocumentStore, Item, Row, SqlDatabase
from app.shared import errors

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


class Repository[T](Protocol):
    def get(self, entity_id: str) -> T | None: ...
    def add(self, entity: T) -> None: ...
    def save(self, entity: T) -> None: ...
    def remove(self, entity_id: str) -> None: ...


class SqlRepository[T](ABC):
    """Table with a text primary key `id_column`. Column names come from `to_row`."""

    table: str
    id_column: str = "id"

    def __init__(self, db: SqlDatabase) -> None:
        for name in (self.table, self.id_column):
            if not _IDENT.match(name):
                raise ValueError(f"invalid identifier {name!r}")
        self.db = db

    @abstractmethod
    def to_row(self, entity: T) -> Row: ...

    @abstractmethod
    def from_row(self, row: Row) -> T: ...

    def get(self, entity_id: str) -> T | None:
        rows = self.db.execute(
            f"SELECT * FROM {self.table} WHERE {self.id_column} = :id",  # noqa: S608
            {"id": entity_id},
        )
        return self.from_row(rows[0]) if rows else None

    def find_one(self, column: str, value: Any) -> T | None:
        if not _IDENT.match(column):
            raise ValueError(f"invalid identifier {column!r}")
        rows = self.db.execute(
            f"SELECT * FROM {self.table} WHERE {column} = :v LIMIT 1",  # noqa: S608
            {"v": value},
        )
        return self.from_row(rows[0]) if rows else None

    def add(self, entity: T) -> None:
        row = self._checked(self.to_row(entity))
        cols = ", ".join(row)
        params = ", ".join(f":{c}" for c in row)
        self.db.execute(f"INSERT INTO {self.table} ({cols}) VALUES ({params})", row)  # noqa: S608

    def save(self, entity: T) -> None:
        row = self._checked(self.to_row(entity))
        cols = ", ".join(row)
        params = ", ".join(f":{c}" for c in row)
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in row if c != self.id_column)
        self.db.execute(
            f"INSERT INTO {self.table} ({cols}) VALUES ({params}) "  # noqa: S608
            f"ON CONFLICT ({self.id_column}) DO UPDATE SET {updates}",
            row,
        )

    def remove(self, entity_id: str) -> None:
        self.db.execute(
            f"DELETE FROM {self.table} WHERE {self.id_column} = :id",  # noqa: S608
            {"id": entity_id},
        )

    def list(self, limit: int = 100, offset: int = 0) -> list[T]:
        rows = self.db.execute(
            f"SELECT * FROM {self.table} ORDER BY {self.id_column} LIMIT :l OFFSET :o",  # noqa: S608
            {"l": limit, "o": offset},
        )
        return [self.from_row(r) for r in rows]

    @staticmethod
    def _checked(row: Row) -> Row:
        for col in row:
            if not _IDENT.match(col):
                raise ValueError(f"invalid column {col!r}")
        return row


class DocumentRepository[T](ABC):
    """DynamoDB-style table with partition key `key_attribute`."""

    key_attribute: str = "pk"

    def __init__(self, store: DocumentStore, table: str) -> None:
        self.store = store
        self.table = table

    @abstractmethod
    def to_item(self, entity: T) -> Item: ...

    @abstractmethod
    def from_item(self, item: Item) -> T: ...

    def get(self, entity_id: str) -> T | None:
        item = self.store.get(self.table, {self.key_attribute: entity_id})
        return self.from_item(item) if item else None

    def add(self, entity: T) -> None:
        self.store.put(self.table, self.to_item(entity), if_absent=self.key_attribute)

    def save(self, entity: T) -> None:
        self.store.put(self.table, self.to_item(entity))

    def remove(self, entity_id: str) -> None:
        self.store.delete(self.table, {self.key_attribute: entity_id})

    def require(self, entity_id: str) -> T:
        entity = self.get(entity_id)
        if entity is None:
            raise errors.NotFound(f"{self.table}/{entity_id}")
        return entity

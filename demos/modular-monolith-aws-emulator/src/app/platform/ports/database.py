"""Database ports: a key/document store and a SQL database."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any, Protocol

Item = dict[str, Any]
Row = dict[str, Any]


class DocumentStore(Protocol):
    """Single-table key/value documents (DynamoDB)."""

    def put(self, table: str, item: Item, *, if_absent: str | None = None) -> None:
        """Write `item`. With `if_absent=<key attr>`, raise Conflict if it exists."""
        ...

    def get(self, table: str, key: Mapping[str, Any]) -> Item | None: ...
    def delete(self, table: str, key: Mapping[str, Any]) -> None: ...
    def scan(self, table: str, limit: int = 100) -> list[Item]: ...


class SqlDatabase(Protocol):
    """PostgreSQL dialect (Aurora via RDS Data API)."""

    def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> list[Row]:
        """Run one statement with `:name` parameters. Returns rows (empty for DML/DDL)."""
        ...

    def execute_many(self, sql: str, params: Sequence[Mapping[str, Any]]) -> None: ...

    def transaction(self) -> AbstractContextManager[SqlDatabase]:
        """Context manager: commit on success, roll back on exception."""
        ...

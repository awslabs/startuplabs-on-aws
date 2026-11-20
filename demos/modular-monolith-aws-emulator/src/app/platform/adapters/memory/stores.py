"""In-memory DocumentStore, SqlDatabase (SQLite), ObjectStorage, FunctionRuntime."""

from __future__ import annotations

import copy
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any

from app.platform.ports.compute import InvocationResult
from app.platform.ports.database import Item, Row
from app.platform.ports.storage import StoredObject
from app.shared import errors


class InMemoryDocumentStore:
    """Tables keyed by a single partition-key attribute (default `pk`)."""

    def __init__(self, key_attributes: Mapping[str, str] | None = None) -> None:
        self._key_attributes = dict(key_attributes or {})
        self._tables: dict[str, dict[Any, Item]] = {}

    def _attr(self, table: str) -> str:
        return self._key_attributes.get(table, "pk")

    def put(self, table: str, item: Item, *, if_absent: str | None = None) -> None:
        rows = self._tables.setdefault(table, {})
        key = item[self._attr(table)]
        if if_absent and key in rows:
            raise errors.Conflict("The conditional request failed")
        rows[key] = copy.deepcopy(item)

    def get(self, table: str, key: Mapping[str, Any]) -> Item | None:
        row = self._tables.get(table, {}).get(key[self._attr(table)])
        return copy.deepcopy(row) if row else None

    def delete(self, table: str, key: Mapping[str, Any]) -> None:
        self._tables.get(table, {}).pop(key[self._attr(table)], None)

    def scan(self, table: str, limit: int = 100) -> list[Item]:
        return [copy.deepcopy(r) for r in list(self._tables.get(table, {}).values())[:limit]]


# Same wire format the RDS Data adapter sends for TIMESTAMP/DATE parameters.
sqlite3.register_adapter(datetime, lambda d: d.strftime("%Y-%m-%d %H:%M:%S.%f"))
sqlite3.register_adapter(date, lambda d: d.isoformat())


class SqliteSqlDatabase:
    """SQLite with `:name` parameters, the same placeholder style as the Data API."""

    def __init__(self, conn: sqlite3.Connection | None = None, *, nested: bool = False) -> None:
        self._conn = conn or sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._nested = nested
        self._lock = threading.RLock()

    def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> list[Row]:
        with self._lock:
            try:
                cur = self._conn.execute(sql, dict(params or {}))
            except sqlite3.IntegrityError as exc:
                raise errors.Conflict("unique constraint violated") from exc
            rows = [dict(r) for r in cur.fetchall()]
            if not self._nested:
                self._conn.commit()
            return rows

    def execute_many(self, sql: str, params: Sequence[Mapping[str, Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, [dict(p) for p in params])
            if not self._nested:
                self._conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[SqliteSqlDatabase]:
        with self._lock:
            scoped = SqliteSqlDatabase(self._conn, nested=True)
            try:
                yield scoped
            except BaseException:
                self._conn.rollback()
                raise
            self._conn.commit()


class InMemoryObjectStorage:
    def __init__(self) -> None:
        self._objects: dict[tuple[str, str], StoredObject] = {}

    def put(self, bucket: str, key: str, body: bytes, content_type: str) -> None:
        self._objects[(bucket, key)] = StoredObject(key, body, content_type)

    def get(self, bucket: str, key: str) -> StoredObject | None:
        return self._objects.get((bucket, key))

    def delete(self, bucket: str, key: str) -> None:
        self._objects.pop((bucket, key), None)

    def list_keys(self, bucket: str, prefix: str = "") -> list[str]:
        return sorted(k for b, k in self._objects if b == bucket and k.startswith(prefix))

    def presign_get(self, bucket: str, key: str, expires_in: int = 300) -> str:
        return f"memory://{bucket}/{key}?method=GET&expires={expires_in}"

    def presign_put(self, bucket: str, key: str, content_type: str, expires_in: int = 300) -> str:
        return f"memory://{bucket}/{key}?method=PUT&expires={expires_in}"


class InMemoryFunctionRuntime:
    def __init__(self, functions: Mapping[str, Callable[[Any], Any]] | None = None) -> None:
        self._functions = dict(functions or {})

    def invoke(self, function: str, payload: Any) -> InvocationResult:
        handler = self._functions.get(function)
        if handler is None:
            raise errors.NotFound(f"function {function}")
        try:
            return InvocationResult(200, handler(payload))
        except Exception as exc:
            return InvocationResult(200, {"errorMessage": str(exc)}, "Unhandled")

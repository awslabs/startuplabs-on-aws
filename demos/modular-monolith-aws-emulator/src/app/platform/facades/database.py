from __future__ import annotations

from collections.abc import Mapping

from app.platform.ports.database import DocumentStore, SqlDatabase


class DatabaseFacade:
    """`sql` for relational data, `documents(<logical table>)` for key/value data."""

    def __init__(
        self, sql: SqlDatabase, documents: DocumentStore, tables: Mapping[str, str]
    ) -> None:
        self.sql = sql
        self._documents = documents
        self._tables = dict(tables)

    def documents(self, logical_table: str) -> tuple[DocumentStore, str]:
        """Returns the store and the physical table name for a logical one."""
        return self._documents, self._tables[logical_table]

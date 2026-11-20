"""Forward-only SQL migrations, one folder per module: `<module>/migrations/NNN_name.sql`.

Applied in order (module, file) and recorded in `schema_migrations`. Statements are
separated by `;` at end of line. Keep them portable PostgreSQL (the in-memory
adapter runs them on SQLite).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from app.platform.ports.database import SqlDatabase

_LEDGER = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "id TEXT PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
)


def split_statements(script: str) -> list[str]:
    lines = [ln for ln in script.splitlines() if not ln.strip().startswith("--")]
    chunks = "\n".join(lines).split(";\n")
    return [c.strip().rstrip(";").strip() for c in chunks if c.strip().rstrip(";").strip()]


def apply(db: SqlDatabase, folders: Iterable[Path]) -> list[str]:
    db.execute(_LEDGER)
    done = {r["id"] for r in db.execute("SELECT id FROM schema_migrations")}
    applied: list[str] = []
    for folder in sorted(folders):
        for file in sorted(folder.glob("*.sql")):
            mig_id = f"{folder.parent.name}/{file.name}"
            if mig_id in done:
                continue
            with db.transaction() as tx:
                for stmt in split_statements(file.read_text()):
                    tx.execute(stmt)
                tx.execute("INSERT INTO schema_migrations (id) VALUES (:id)", {"id": mig_id})
            applied.append(mig_id)
    return applied

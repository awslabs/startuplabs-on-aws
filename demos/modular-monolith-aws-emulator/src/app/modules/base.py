"""What every module exports from its `module.py`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter

from app.platform.facades import Platform


@dataclass(frozen=True, slots=True)
class ModuleSpec:
    name: str
    summary: str
    build_router: Callable[[Platform], APIRouter]
    migrations: Path | None = None
    document_tables: tuple[str, ...] = ()

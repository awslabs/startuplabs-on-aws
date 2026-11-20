from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

# Served by the monolith itself regardless of the table.
ALWAYS_MODERN = ("/healthz", "/docs", "/redoc", "/openapi.json")


class Owner(StrEnum):
    MODERN = "modern"
    LEGACY = "legacy"


@dataclass(frozen=True, slots=True)
class Route:
    prefix: str
    owner: Owner

    def matches(self, path: str) -> bool:
        p = self.prefix.rstrip("/")
        return path == p or path.startswith(p + "/") or p == ""


@dataclass(frozen=True, slots=True)
class RouteTable:
    routes: tuple[Route, ...]
    default: Owner = Owner.MODERN

    @classmethod
    def load(cls, path: Path) -> RouteTable:
        if not path.exists():
            return cls(routes=())
        data = yaml.safe_load(path.read_text()) or {}
        routes = tuple(
            Route(prefix=str(r["prefix"]), owner=Owner(r["owner"])) for r in data.get("routes", [])
        )
        for r in routes:
            if not r.prefix.startswith("/"):
                raise ValueError(f"route prefix must start with '/': {r.prefix!r}")
        return cls(routes=routes, default=Owner(data.get("default", Owner.MODERN)))

    def owner_of(self, path: str) -> Owner:
        if any(path == p or path.startswith(p + "/") for p in ALWAYS_MODERN):
            return Owner.MODERN
        best: Route | None = None
        for route in self.routes:
            if route.matches(path) and (best is None or len(route.prefix) > len(best.prefix)):
                best = route
        return best.owner if best else self.default

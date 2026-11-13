from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.strangler import Owner, Route, RouteTable, StranglerMiddleware

TABLE = RouteTable(
    routes=(
        Route("/api/v1/orders", Owner.LEGACY),
        Route("/api/v1/orders/new", Owner.MODERN),
        Route("/api/v1/auth", Owner.MODERN),
    ),
    default=Owner.LEGACY,
)


@pytest.mark.parametrize(
    ("path", "owner"),
    [
        ("/api/v1/orders", Owner.LEGACY),
        ("/api/v1/orders/42", Owner.LEGACY),
        ("/api/v1/orders/new", Owner.MODERN),  # longest prefix wins
        ("/api/v1/ordersX", Owner.LEGACY),  # falls to default, not to /orders
        ("/api/v1/auth/login", Owner.MODERN),
        ("/healthz", Owner.MODERN),  # always modern
        ("/somewhere", Owner.LEGACY),
    ],
)
def test_owner_of(path: str, owner: Owner) -> None:
    assert TABLE.owner_of(path) is owner


def test_load_rejects_relative_prefix(tmp_path: Path) -> None:
    f = tmp_path / "s.yaml"
    f.write_text("routes:\n  - prefix: api\n    owner: legacy\n")
    with pytest.raises(ValueError, match="must start"):
        RouteTable.load(f)


def _app(handler: httpx.MockTransport | None, legacy: str | None) -> TestClient:
    app = FastAPI()

    @app.get("/api/v1/auth/me")
    def me() -> dict[str, str]:
        return {"served_by": "modern"}

    client = httpx.AsyncClient(transport=handler) if handler else None
    app.add_middleware(
        StranglerMiddleware,  # type: ignore[arg-type]
        table=TABLE,
        legacy_base_url=legacy,
        client=client,
    )
    return TestClient(app)


def test_proxies_legacy_paths_keeping_host_fixed() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"served_by": "legacy"}, headers={"connection": "close"})

    client = _app(httpx.MockTransport(handle), "http://legacy.internal:9000/base")
    r = client.get("/api/v1/orders/42?x=1", headers={"authorization": "Bearer t"})
    assert r.json() == {"served_by": "legacy"}
    assert r.headers["x-strangler-route"] == "legacy"
    assert str(seen[0].url) == "http://legacy.internal:9000/base/api/v1/orders/42?x=1"
    assert seen[0].headers["authorization"] == "Bearer t"
    assert seen[0].headers["host"] == "legacy.internal:9000"


def test_modern_paths_never_reach_legacy() -> None:
    def handle(_r: httpx.Request) -> httpx.Response:
        raise AssertionError("must not proxy")

    client = _app(httpx.MockTransport(handle), "http://legacy.internal")
    assert client.get("/api/v1/auth/me").json() == {"served_by": "modern"}


def test_legacy_route_without_upstream_is_502() -> None:
    r = _app(None, None).get("/api/v1/orders")
    assert r.status_code == 502
    assert r.json()["error"] == "not_migrated"


def test_upstream_failure_is_502() -> None:
    def handle(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    r = _app(httpx.MockTransport(handle), "http://legacy.internal").get("/api/v1/orders")
    assert r.status_code == 502
    assert r.json()["error"] == "legacy_unavailable"

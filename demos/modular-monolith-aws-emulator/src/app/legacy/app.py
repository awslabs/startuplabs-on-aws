"""Fake legacy system: owns /api/v1/orders until it is migrated.

Run: `just legacy` (127.0.0.1:8081). It only checks that a bearer header is
present: it is a demo stub, bound to loopback, holding fake data. A real legacy
system keeps its own authentication; the strangler proxy forwards the
Authorization header unchanged.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, Header, HTTPException

app = FastAPI(title="legacy-system")

_ORDERS = [
    {"id": "o-1001", "status": "shipped", "total": 129.9},
    {"id": "o-1002", "status": "processing", "total": 49.0},
]


def _require_bearer(authorization: str | None) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="legacy: bearer token required")


@app.get("/api/v1/orders")
def list_orders(authorization: Annotated[str | None, Header()] = None) -> dict[str, object]:
    _require_bearer(authorization)
    return {"served_by": "legacy", "orders": _ORDERS}


@app.get("/api/v1/orders/{order_id}")
def get_order(
    order_id: str, authorization: Annotated[str | None, Header()] = None
) -> dict[str, object]:
    _require_bearer(authorization)
    for order in _ORDERS:
        if order["id"] == order_id:
            return {"served_by": "legacy", "order": order}
    raise HTTPException(status_code=404, detail="order not found")

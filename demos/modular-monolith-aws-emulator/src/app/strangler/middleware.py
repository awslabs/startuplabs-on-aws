"""ASGI middleware that proxies `legacy`-owned paths to LEGACY_BASE_URL."""

from __future__ import annotations

import logging

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.strangler.routes import Owner, RouteTable

log = logging.getLogger(__name__)

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
_MAX_BODY = 10 * 1024 * 1024


class StranglerMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        table: RouteTable,
        legacy_base_url: str | None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.app = app
        self.table = table
        self.legacy = httpx.URL(legacy_base_url) if legacy_base_url else None
        self._client = client or httpx.AsyncClient(timeout=10.0, follow_redirects=False)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.table.owner_of(scope["path"]) is Owner.MODERN:
            await self.app(scope, receive, send)
            return
        response = await self._proxy(Request(scope, receive))
        await response(scope, receive, send)

    async def _proxy(self, request: Request) -> Response:
        if self.legacy is None:
            return JSONResponse(
                {"error": "not_migrated", "message": "route owned by legacy, none configured"},
                status_code=502,
            )
        body = await request.body()
        if len(body) > _MAX_BODY:
            return JSONResponse({"error": "payload_too_large"}, status_code=413)
        # Only the path and query come from the caller; scheme/host/port are fixed.
        url = self.legacy.copy_with(
            path=self.legacy.path.rstrip("/") + request.url.path,
            query=request.url.query.encode() or None,
        )
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
        client_host = request.client.host if request.client else ""
        headers["x-forwarded-for"] = client_host
        try:
            upstream = await self._client.request(
                request.method, url, headers=headers, content=body
            )
        except httpx.HTTPError as exc:
            log.warning("legacy upstream failed: %s", type(exc).__name__)
            return JSONResponse({"error": "legacy_unavailable"}, status_code=502)
        out = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}
        out.pop("content-encoding", None)  # httpx already decoded the body
        out["x-strangler-route"] = "legacy"
        return Response(upstream.content, status_code=upstream.status_code, headers=out)

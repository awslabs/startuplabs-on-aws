"""HTTP entry point: FastAPI app + strangler fig middleware + module routers."""

from __future__ import annotations

import ipaddress
import logging

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.config import Settings, get_settings
from app.container import build_platform, load_modules
from app.platform.facades import Platform
from app.shared import errors
from app.strangler import RouteTable, StranglerMiddleware

log = logging.getLogger("app")

_STATUS = {
    errors.ValidationFailed: 422,
    errors.NotFound: 404,
    errors.Conflict: 409,
    errors.Unauthorized: 401,
    errors.Forbidden: 403,
    errors.PlatformUnavailable: 503,
}


def create_app(settings: Settings | None = None, platform: Platform | None = None) -> FastAPI:
    settings = settings or get_settings()
    modules = load_modules()
    platform = platform or build_platform(settings, modules)

    docs = settings.docs_enabled
    app = FastAPI(
        title="modular-monolith-aws-emulator",
        version=__version__,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.platform = platform
    app.state.modules = modules
    app.state.settings = settings

    @app.exception_handler(errors.DomainError)
    async def _domain_error(_request: Request, exc: errors.DomainError) -> JSONResponse:
        status = next((s for t, s in _STATUS.items() if isinstance(exc, t)), 400)
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        message = exc.message if status < 500 else "platform unavailable"
        if status >= 500:
            log.error("platform error: %s", exc.message)
        return JSONResponse(
            {"error": exc.code, "message": message}, status_code=status, headers=headers
        )

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "target": platform.target}

    for module in modules:
        app.include_router(module.build_router(platform))

    table = RouteTable.load(settings.strangler_config)
    app.state.routes = table
    app.add_middleware(
        StranglerMiddleware,
        table=table,
        legacy_base_url=settings.legacy_base_url,
    )
    return app


def run() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())
    try:
        loopback = ipaddress.ip_address(settings.host).is_loopback
    except ValueError:
        loopback = settings.host == "localhost"
    if not loopback:
        log.warning("binding %s: the API is reachable beyond this machine", settings.host)
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    run()

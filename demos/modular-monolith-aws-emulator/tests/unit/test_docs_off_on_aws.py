from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import PlatformTarget, Settings
from app.container import load_modules
from app.main import create_app
from app.platform.facades import Platform


def test_docs_disabled_on_aws(settings: Settings, platform: Platform) -> None:
    aws = settings.model_copy(update={"platform_target": PlatformTarget.AWS})
    load_modules()
    with TestClient(create_app(aws, platform)) as c:
        assert c.get("/openapi.json").status_code == 404
        assert c.get("/docs").status_code == 404
        assert c.get("/healthz").status_code == 200

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from functions.word_count.handler import handler as word_count

from app.config import Settings
from app.container import build_memory_platform, load_modules
from app.main import create_app
from app.platform.facades import Platform
from tests.credentials import PASSWORD

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        outputs_file=tmp_path / "none.json",
        strangler_config=ROOT / "strangler.yaml",
    )


@pytest.fixture
def platform(settings: Settings) -> Platform:
    return build_memory_platform(
        settings, load_modules(), {"word-count": lambda p: word_count(p, None)}
    )


@pytest.fixture
def client(settings: Settings, platform: Platform) -> Iterator[TestClient]:
    with TestClient(create_app(settings, platform)) as c:
        yield c


def register_and_login(client: TestClient, email: str = "ana@example.com") -> dict[str, str]:
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "name": "Ana"},
    )
    assert r.status_code == 201, r.text
    r = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"authorization": f"Bearer {r.json()['access_token']}"}

"""The whole app, wired to the emulator, exercised through HTTP."""

from __future__ import annotations

import ssl
import uuid

import httpx
from fastapi.testclient import TestClient

from tests.credentials import PASSWORD


def test_full_flow(api: TestClient) -> None:
    email = f"e2e-{uuid.uuid4().hex[:8]}@example.com"
    r = api.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD, "name": "E2E"}
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "active"  # auto-confirm (emulator only)

    tokens = api.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).json()
    h = {"authorization": f"Bearer {tokens['access_token']}"}

    me = api.get("/api/v1/auth/me", headers=h)
    assert me.status_code == 200 and me.json()["email"] == email

    profile = api.get("/api/v1/profiles/me", headers=h)
    assert profile.status_code == 200 and profile.json()["display_name"] == "E2E"

    up = api.post("/api/v1/profiles/me/avatar", headers=h, json={"content_type": "image/png"})
    settings = api.app.state.settings  # type: ignore[attr-defined]
    bundle = str(settings.emulator_home / str(settings.emulator_port) / "ca.pem")
    put = httpx.put(
        up.json()["url"],
        content=b"\x89PNG",
        headers={"content-type": "image/png"},
        verify=ssl.create_default_context(cafile=bundle),
    )
    assert put.status_code == 200
    down = api.get("/api/v1/profiles/me/avatar", headers=h).json()["url"]
    assert httpx.get(down, verify=ssl.create_default_context(cafile=bundle)).content == b"\x89PNG"

    job = api.post("/api/v1/jobs/word-count", headers=h, json={"text": "a b a"})
    assert job.status_code == 200 and job.json()["result"]["words"] == 3

    assert api.get("/api/v1/auth/users", headers=h).status_code == 403
    assert api.post("/api/v1/auth/logout", headers=h).status_code == 204
    refreshed = api.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 401  # global sign-out revoked the refresh token

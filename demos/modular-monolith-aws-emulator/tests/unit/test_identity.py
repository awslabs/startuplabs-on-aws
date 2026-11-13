from __future__ import annotations

from fastapi.testclient import TestClient

from app.platform.facades import Platform
from tests.conftest import register_and_login
from tests.credentials import PASSWORD, SHORT_PASSWORD, WRONG_PASSWORD


def test_register_login_me(client: TestClient) -> None:
    headers = register_and_login(client)
    r = client.get("/api/v1/auth/me", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "ana@example.com"
    assert body["status"] == "active"


def test_email_is_case_insensitive(client: TestClient) -> None:
    register_and_login(client, "Bia@Example.com")
    r = client.post("/api/v1/auth/login", json={"email": "bia@example.com", "password": PASSWORD})
    assert r.status_code == 200


def test_duplicate_registration_is_conflict(client: TestClient) -> None:
    register_and_login(client)
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "ana@example.com", "password": PASSWORD, "name": "Ana"},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "conflict"


def test_short_password_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/v1/auth/register",
        json={"email": "c@example.com", "password": SHORT_PASSWORD, "name": "C"},
    )
    assert r.status_code == 422


def test_wrong_password_is_401(client: TestClient) -> None:
    register_and_login(client)
    r = client.post(
        "/api/v1/auth/login", json={"email": "ana@example.com", "password": WRONG_PASSWORD}
    )
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_protected_routes_require_a_verified_token(client: TestClient) -> None:
    assert client.get("/api/v1/auth/me").status_code == 401
    bad = {"authorization": "Bearer not-a-real-token"}
    assert client.get("/api/v1/auth/me", headers=bad).status_code == 401
    assert client.get("/api/v1/auth/me", headers={"authorization": "Basic x"}).status_code == 401


def test_logout_revokes_tokens(client: TestClient) -> None:
    headers = register_and_login(client)
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_refresh(client: TestClient) -> None:
    client.post(
        "/api/v1/auth/register",
        json={"email": "r@example.com", "password": PASSWORD, "name": "R"},
    )
    tokens = client.post(
        "/api/v1/auth/login", json={"email": "r@example.com", "password": PASSWORD}
    ).json()
    r = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200
    assert r.json()["access_token"] != tokens["access_token"]


def test_admin_route_needs_admin_group(client: TestClient, platform: Platform) -> None:
    headers = register_and_login(client)
    assert client.get("/api/v1/auth/users", headers=headers).status_code == 403
    platform.identity.grant_group("ana@example.com", "admin")
    r = client.get("/api/v1/auth/users", headers=headers)
    assert r.status_code == 200
    assert [u["email"] for u in r.json()] == ["ana@example.com"]


def test_login_rebuilds_missing_projection(client: TestClient, platform: Platform) -> None:
    # A user that exists only in the identity provider (admin-created, or a
    # registration that failed after sign-up) gets its projection and profile on login.
    provider = platform.identity._provider  # type: ignore[attr-defined]
    provider.admin_create("ops@example.com", PASSWORD, "Ops")
    r = client.post("/api/v1/auth/login", json={"email": "ops@example.com", "password": PASSWORD})
    headers = {"authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get("/api/v1/auth/me", headers=headers).json()["name"] == "Ops"
    assert client.get("/api/v1/profiles/me", headers=headers).json()["display_name"] == "Ops"


def test_docs_follow_target(client: TestClient) -> None:
    # memory/emulator default: docs on
    assert client.get("/openapi.json").status_code == 200

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import register_and_login


def test_profile_is_created_from_user_registered_event(client: TestClient) -> None:
    headers = register_and_login(client)
    r = client.get("/api/v1/profiles/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["display_name"] == "Ana"
    assert r.json()["has_avatar"] is False


def test_update_profile(client: TestClient) -> None:
    headers = register_and_login(client)
    r = client.put("/api/v1/profiles/me", headers=headers, json={"bio": "hello"})
    assert r.status_code == 200
    assert r.json()["bio"] == "hello"


def test_avatar_flow_and_type_validation(client: TestClient) -> None:
    headers = register_and_login(client)
    bad = client.post("/api/v1/profiles/me/avatar", headers=headers, json={"content_type": "x/y"})
    assert bad.status_code == 422
    ok = client.post(
        "/api/v1/profiles/me/avatar", headers=headers, json={"content_type": "image/png"}
    )
    assert ok.status_code == 200
    assert ok.json()["key"].endswith("/avatar.png")
    assert client.get("/api/v1/profiles/me/avatar", headers=headers).status_code == 200


def test_word_count_job(client: TestClient) -> None:
    headers = register_and_login(client)
    r = client.post("/api/v1/jobs/word-count", headers=headers, json={"text": "a b a"})
    assert r.status_code == 200
    assert r.json()["result"]["words"] == 3
    assert r.json()["result"]["unique"] == 2


def test_jobs_require_auth(client: TestClient) -> None:
    assert client.post("/api/v1/jobs/word-count", json={"text": "a"}).status_code == 401


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json()["target"] == "memory"

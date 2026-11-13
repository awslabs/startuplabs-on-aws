from __future__ import annotations

import json
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from app.platform.adapters.aws.jwt_verifier import CognitoJwtVerifier
from app.shared import errors

ISS = "https://127.0.0.1:9297/us-east-1_test"
CLIENT = "client-123"
KEY = ec.generate_private_key(ec.SECP256R1())
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())


@pytest.fixture(autouse=True)
def jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    public = json.loads(ECAlgorithm.to_jwk(KEY.public_key()))
    public |= {"kid": "k1", "alg": "ES256", "use": "sig"}

    def fake_get(url: str, **_kw: Any) -> httpx.Response:
        assert url == f"{ISS}/.well-known/jwks.json"
        return httpx.Response(200, json={"keys": [public]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)


def _token(key: Any = KEY, kid: str = "k1", alg: str = "ES256", **claims: Any) -> str:
    now = int(time.time())
    body = {
        "sub": "user-1",
        "username": "user-1",
        "iss": ISS,
        "client_id": CLIENT,
        "token_use": "access",
        "iat": now,
        "exp": now + 300,
        "cognito:groups": ["admin"],
    } | claims
    return jwt.encode(body, key, algorithm=alg, headers={"kid": kid})


def verifier() -> CognitoJwtVerifier:
    return CognitoJwtVerifier(ISS, CLIENT)


def test_valid_token() -> None:
    p = verifier().verify(_token())
    assert p.subject == "user-1"
    assert p.in_group("admin")


@pytest.mark.parametrize(
    "token",
    [
        _token(key=OTHER_KEY),  # bad signature
        _token(iss="https://evil.example/pool"),
        _token(client_id="other-client"),
        _token(token_use="id"),
        _token(exp=int(time.time()) - 60),
        _token(kid="unknown"),
        _token(key="a-shared-secret-of-sufficient-length-32b", alg="HS256"),
        jwt.encode({"sub": "x"}, key=None, algorithm="none"),
        "not.a.jwt",
    ],
)
def test_rejected(token: str) -> None:
    with pytest.raises(errors.Unauthorized):
        verifier().verify(token)

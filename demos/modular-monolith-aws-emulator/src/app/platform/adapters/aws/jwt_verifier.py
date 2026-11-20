"""Cognito access-token verification against the pool's JWKS (TokenVerifier port).

Checks: signature (key from JWKS, by `kid`), `iss`, `exp`/`iat`, `token_use=access`
and `client_id`. Real Cognito signs with RS256; the emulator with ES256. Both are
asymmetric and both are accepted; `none` and HMAC algorithms never are.
"""

from __future__ import annotations

import ssl
import threading
import time
from typing import Any

import httpx
import jwt
from jwt import PyJWK, PyJWKSet

from app.shared import errors
from app.shared.principal import Principal

_ALLOWED_ALGORITHMS = ["RS256", "ES256"]
_JWKS_TTL_SECONDS = 3600


class CognitoJwtVerifier:
    def __init__(self, issuer: str, client_id: str, ca_bundle: str | None = None) -> None:
        self._issuer = issuer
        self._client_id = client_id
        self._jwks_url = f"{issuer}/.well-known/jwks.json"
        self._verify: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=ca_bundle) if ca_bundle else True
        )
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def verify(self, access_token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(access_token)
        except jwt.PyJWTError as exc:
            raise errors.Unauthorized("malformed token") from exc
        if header.get("alg") not in _ALLOWED_ALGORITHMS:
            raise errors.Unauthorized("unsupported token algorithm")
        key = self._key(str(header.get("kid", "")))
        try:
            claims: dict[str, Any] = jwt.decode(
                access_token,
                key=key.key,
                algorithms=_ALLOWED_ALGORITHMS,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "iss", "sub", "token_use"]},
                leeway=5,
            )
        except jwt.PyJWTError as exc:
            raise errors.Unauthorized("invalid token") from exc
        if claims.get("token_use") != "access":
            raise errors.Unauthorized("not an access token")
        if claims.get("client_id") != self._client_id:
            raise errors.Unauthorized("token issued for another client")
        return Principal(
            subject=str(claims["sub"]),
            username=str(claims.get("username", claims["sub"])),
            groups=frozenset(claims.get("cognito:groups", [])),
        )

    def _key(self, kid: str) -> PyJWK:
        with self._lock:
            stale = time.monotonic() - self._fetched_at > _JWKS_TTL_SECONDS
            if kid not in self._keys or stale:
                self._refresh()
            if kid not in self._keys:
                raise errors.Unauthorized("unknown signing key")
            return self._keys[kid]

    def _refresh(self) -> None:
        try:
            resp = httpx.get(self._jwks_url, verify=self._verify, timeout=5.0)
            resp.raise_for_status()
            jwks = PyJWKSet.from_dict(resp.json())
        except (httpx.HTTPError, jwt.PyJWTError) as exc:
            raise errors.PlatformUnavailable("cannot fetch JWKS") from exc
        self._keys = {k.key_id: k for k in jwks.keys if k.key_id}
        self._fetched_at = time.monotonic()

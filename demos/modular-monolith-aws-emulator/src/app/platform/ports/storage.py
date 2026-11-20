"""Object storage port (S3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    body: bytes
    content_type: str


class ObjectStorage(Protocol):
    def put(self, bucket: str, key: str, body: bytes, content_type: str) -> None: ...
    def get(self, bucket: str, key: str) -> StoredObject | None: ...
    def delete(self, bucket: str, key: str) -> None: ...
    def list_keys(self, bucket: str, prefix: str = "") -> list[str]: ...
    def presign_get(self, bucket: str, key: str, expires_in: int = 300) -> str: ...
    def presign_put(
        self, bucket: str, key: str, content_type: str, expires_in: int = 300
    ) -> str: ...

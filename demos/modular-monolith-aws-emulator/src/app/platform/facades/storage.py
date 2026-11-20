from __future__ import annotations

from app.platform.ports.storage import ObjectStorage, StoredObject


class StorageFacade:
    """Object storage bound to the application's assets bucket."""

    def __init__(self, storage: ObjectStorage, bucket: str) -> None:
        self._storage = storage
        self._bucket = bucket

    def save(self, key: str, body: bytes, content_type: str) -> None:
        self._storage.put(self._bucket, key, body, content_type)

    def load(self, key: str) -> StoredObject | None:
        return self._storage.get(self._bucket, key)

    def remove(self, key: str) -> None:
        self._storage.delete(self._bucket, key)

    def keys(self, prefix: str = "") -> list[str]:
        return self._storage.list_keys(self._bucket, prefix)

    def upload_url(self, key: str, content_type: str, expires_in: int = 300) -> str:
        return self._storage.presign_put(self._bucket, key, content_type, expires_in)

    def download_url(self, key: str, expires_in: int = 300) -> str:
        return self._storage.presign_get(self._bucket, key, expires_in)

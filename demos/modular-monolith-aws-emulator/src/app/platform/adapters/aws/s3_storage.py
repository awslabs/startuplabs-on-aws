"""S3 adapter (ObjectStorage port)."""

from __future__ import annotations

from app.platform.adapters.aws.errors import translate
from app.platform.adapters.aws.session import AwsClientFactory
from app.platform.ports.storage import StoredObject
from app.shared import errors


class S3ObjectStorage:
    def __init__(self, clients: AwsClientFactory) -> None:
        self._s3 = clients.client("s3")

    def put(self, bucket: str, key: str, body: bytes, content_type: str) -> None:
        with translate():
            self._s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)

    def get(self, bucket: str, key: str) -> StoredObject | None:
        try:
            with translate():
                resp = self._s3.get_object(Bucket=bucket, Key=key)
        except errors.NotFound:
            return None
        return StoredObject(
            key=key,
            body=resp["Body"].read(),
            content_type=resp.get("ContentType", "application/octet-stream"),
        )

    def delete(self, bucket: str, key: str) -> None:
        with translate():
            self._s3.delete_object(Bucket=bucket, Key=key)

    def list_keys(self, bucket: str, prefix: str = "") -> list[str]:
        keys: list[str] = []
        with translate():
            for page in self._s3.get_paginator("list_objects_v2").paginate(
                Bucket=bucket, Prefix=prefix
            ):
                keys.extend(o["Key"] for o in page.get("Contents", []))
        return keys

    def presign_get(self, bucket: str, key: str, expires_in: int = 300) -> str:
        return str(
            self._s3.generate_presigned_url(
                "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_in
            )
        )

    def presign_put(self, bucket: str, key: str, content_type: str, expires_in: int = 300) -> str:
        return str(
            self._s3.generate_presigned_url(
                "put_object",
                Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
                ExpiresIn=expires_in,
            )
        )

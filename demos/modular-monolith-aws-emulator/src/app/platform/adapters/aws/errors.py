"""Translate botocore errors into domain errors."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

from botocore.exceptions import BotoCoreError, ClientError

from app.shared import errors

ErrorFactory = Callable[[str], errors.DomainError]


@contextmanager
def translate(mapping: Mapping[str, ErrorFactory] | None = None) -> Iterator[None]:
    try:
        yield
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        message = exc.response.get("Error", {}).get("Message", code)
        if mapping and code in mapping:
            raise mapping[code](message) from exc
        if code in {"ResourceNotFoundException", "NoSuchKey", "NotFound", "404"}:
            raise errors.NotFound(message) from exc
        if code in {"ConditionalCheckFailedException", "ConflictException"}:
            raise errors.Conflict(message) from exc
        if code in {"ValidationException", "InvalidParameterException"}:
            raise errors.ValidationFailed(message) from exc
        raise errors.PlatformUnavailable(f"{code}: {message}") from exc
    except BotoCoreError as exc:
        raise errors.PlatformUnavailable(type(exc).__name__) from exc

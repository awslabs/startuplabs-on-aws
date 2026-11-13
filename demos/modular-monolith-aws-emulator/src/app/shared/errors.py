"""Domain error hierarchy shared by every module.

Adapters translate provider errors into these; the HTTP layer maps them to
status codes. Business code never sees botocore exceptions.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class. `code` is stable and safe to return to clients."""

    code = "domain_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ValidationFailed(DomainError):
    code = "validation_failed"


class NotFound(DomainError):
    code = "not_found"


class Conflict(DomainError):
    code = "conflict"


class Unauthorized(DomainError):
    code = "unauthorized"


class Forbidden(DomainError):
    code = "forbidden"


class PlatformUnavailable(DomainError):
    """The provider (AWS or the emulator) failed in a way the caller cannot fix."""

    code = "platform_unavailable"

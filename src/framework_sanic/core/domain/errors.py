"""Domain errors. The HTTP error-mapping middleware translates these to status codes."""

from __future__ import annotations


class DomainError(Exception):
    code = "error"


class NotFound(DomainError):
    code = "not_found"


class Unauthorized(DomainError):
    code = "unauthorized"


class Forbidden(DomainError):
    code = "forbidden"


class InvalidInput(DomainError):
    code = "invalid_input"


class Conflict(DomainError):
    """Optimistic concurrency conflict; the caller should retry."""

    code = "conflict"


class RateLimited(DomainError):
    code = "rate_limited"

    def __init__(self, message: str = "rate limited", retry_after: float = 1.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class NotConnected(DomainError):
    """No usable credential for the parent platform or external service."""

    code = "not_connected"


class ServiceUnavailable(DomainError):
    code = "unavailable"


class ConfigurationError(Exception):
    """Raised at startup when configuration is invalid (e.g. missing scopes)."""

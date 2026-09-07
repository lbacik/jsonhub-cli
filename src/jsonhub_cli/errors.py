"""Exceptions the CLI turns into clean, single-line stderr messages."""

from __future__ import annotations


class JsonHubCliError(Exception):
    """Base class for every error the CLI reports without a traceback.

    ``hint`` is printed as a follow-up line and should tell the user what to do
    next (``run jsonhub auth login``, ...) rather than restate the problem.
    """

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class ConfigError(JsonHubCliError):
    """The on-disk configuration is missing, unreadable or malformed."""


class AuthError(JsonHubCliError):
    """No usable credentials, or the server rejected the ones we had."""

    exit_code = 4


class ApiError(JsonHubCliError):
    """The API answered with an RFC 9457 problem document."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        detail: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.status = status
        self.detail = detail


class ValidationError(JsonHubCliError):
    """The API rejected the payload with a 422 constraint violation."""

    exit_code = 22

    def __init__(self, message: str, *, violations: list[tuple[str, str]] | None = None) -> None:
        super().__init__(message)
        self.violations = violations or []


class NotFoundError(JsonHubCliError):
    """The addressed resource does not exist, or is not visible to us."""

    exit_code = 3

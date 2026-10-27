"""Base class for errors that the API reports to clients.

Each subclass declares the HTTP status and a stable `error_type` string, so the
API layer can render every expected failure the same way. Messages are shown to
clients and must never contain secrets.
"""

from typing import ClassVar


class AppError(Exception):
    http_status: ClassVar[int] = 500
    error_type: ClassVar[str] = "internal_error"
    retryable: ClassVar[bool] = False

    def __init__(
        self, message: str, *, provider: str | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.retry_after = retry_after

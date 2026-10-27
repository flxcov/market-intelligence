"""Protection for state-changing endpoints.

Write endpoints require `Authorization: Bearer <API_WRITE_TOKEN>`. If no token
is configured, write endpoints are disabled rather than left open: secure by
default, even though this service is intended to run locally.
"""

import secrets
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from packages.shared.errors import AppError

_bearer = HTTPBearer(auto_error=False, description="API_WRITE_TOKEN from your .env")


class WritesDisabledError(AppError):
    http_status = 403
    error_type = "writes_disabled"


class UnauthorizedError(AppError):
    http_status = 401
    error_type = "unauthorized"


def require_write_access(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    expected = request.app.state.settings.api_write_token
    if expected is None:
        raise WritesDisabledError(
            "Write endpoints are disabled: set API_WRITE_TOKEN in .env and restart the server."
        )
    if credentials is None or not secrets.compare_digest(
        credentials.credentials.encode(), expected.get_secret_value().encode()
    ):
        raise UnauthorizedError("Missing or invalid bearer token.")

"""Maps expected errors to consistent JSON error responses.

Error body shape: {"error": {"type": str, "message": str, "provider": str | null}}
"""

import logging
import math

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from packages.database.errors import DatabaseUnavailableError
from packages.shared.errors import AppError

logger = logging.getLogger(__name__)


def _error_response(exc: AppError) -> JSONResponse:
    headers = {"Retry-After": str(math.ceil(exc.retry_after))} if exc.retry_after else None
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "error": {"type": exc.error_type, "message": exc.message, "provider": exc.provider}
        },
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.warning(
            "request failed",
            extra={
                "path": request.url.path,
                "error_type": exc.error_type,
                "provider": exc.provider,
            },
        )
        return _error_response(exc)

    @app.exception_handler(DBAPIError)
    async def _database_error(request: Request, exc: DBAPIError) -> JSONResponse:
        # Connection-level failures are reported as "unavailable"; anything else
        # (e.g. a constraint violation) is a bug and stays a generic 500.
        is_connection_error = exc.connection_invalidated or isinstance(
            exc, OperationalError | InterfaceError
        )
        if not is_connection_error:
            logger.error("database error", extra={"path": request.url.path}, exc_info=exc)
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "type": "internal_error",
                        "message": "Internal server error.",
                        "provider": None,
                    }
                },
            )
        logger.warning(
            "database unavailable",
            extra={"path": request.url.path, "error_class": type(exc.orig).__name__},
        )
        return _error_response(DatabaseUnavailableError("Database is unavailable. Is it running?"))

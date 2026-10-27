from packages.shared.errors import AppError


class DatabaseNotConfiguredError(AppError):
    http_status = 503
    error_type = "database_not_configured"


class DatabaseUnavailableError(AppError):
    """The database could not be reached or rejected the connection."""

    http_status = 503
    error_type = "database_unavailable"
    retryable = True

"""Typed SEC EDGAR errors. Messages never contain the configured User-Agent contact."""

from packages.shared.errors import AppError


class SecError(AppError):
    http_status = 502
    error_type = "sec_error"


class SecConfigurationError(SecError):
    http_status = 503
    error_type = "sec_not_configured"


class SecNotFoundError(SecError):
    http_status = 404
    error_type = "sec_not_found"


class SecAccessError(SecError):
    error_type = "sec_access_denied"


class SecUnavailableError(SecError):
    http_status = 503
    error_type = "sec_unavailable"
    retryable = True


class SecResponseError(SecError):
    error_type = "sec_invalid_response"

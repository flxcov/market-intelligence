"""Typed market data errors.

Each error carries the HTTP status the API should report and whether a provider
call that failed this way is worth retrying. Messages must never contain secrets.
"""

from packages.shared.errors import AppError


class MarketDataError(AppError):
    http_status = 502
    error_type = "market_data_error"


class ProviderConfigurationError(MarketDataError):
    """The provider cannot be used because configuration (e.g. credentials) is missing."""

    http_status = 503
    error_type = "provider_not_configured"


class ProviderAccessError(MarketDataError):
    """The provider rejected our credentials or our plan does not cover the request."""

    http_status = 502
    error_type = "provider_access_denied"


class ProviderRequestError(MarketDataError):
    """The request is not something this provider supports or accepts."""

    http_status = 400
    error_type = "provider_rejected_request"


class ProviderRateLimitError(MarketDataError):
    http_status = 429
    error_type = "provider_rate_limited"
    retryable = True


class ProviderUnavailableError(MarketDataError):
    """Timeouts, network failures and transient provider-side errors."""

    http_status = 503
    error_type = "provider_unavailable"
    retryable = True


class ProviderResponseError(MarketDataError):
    """The provider answered, but with something we cannot trust or parse."""

    http_status = 502
    error_type = "invalid_provider_response"


class DataValidationError(ProviderResponseError):
    """Parsed data failed integrity checks (e.g. conflicting duplicate bars)."""

    error_type = "data_validation_failed"

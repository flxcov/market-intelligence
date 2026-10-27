"""Structured logging with secret redaction.

Log records are rendered as one JSON object per line (or a readable console
line). Pass extra context with `logger.info("msg", extra={"symbol": "AAPL"})`.

Redaction is defense in depth: code must never log secrets in the first place,
but if a configured secret value ever reaches a log line it is replaced.
"""

import json
import logging
import sys
from collections.abc import Iterable
from datetime import UTC, datetime

REDACTED = "***REDACTED***"
_HANDLER_NAME = "market-intelligence"
_MIN_SECRET_LENGTH = 4  # very short values would redact unrelated text
_STANDARD_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
    "taskName",
    "color_message",  # uvicorn adds an ANSI-colored duplicate of msg
}


class _RedactingFormatter(logging.Formatter):
    def __init__(self, secrets: Iterable[str] = (), fmt: str | None = None) -> None:
        super().__init__(fmt)
        self._secrets = tuple(s for s in secrets if s and len(s) >= _MIN_SECRET_LENGTH)

    def format(self, record: logging.LogRecord) -> str:
        text = self.render(record)
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    def render(self, record: logging.LogRecord) -> str:
        return super().format(record)


class JsonFormatter(_RedactingFormatter):
    def render(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(_RedactingFormatter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__(secrets, fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s")


def configure_logging(
    *, level: str = "INFO", fmt: str = "json", secrets: Iterable[str] = ()
) -> None:
    """Install a single stdout handler on the root logger.

    Safe to call repeatedly: only the handler installed by this function is
    replaced, so handlers added by other tools (e.g. pytest) are left alone.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler.get_name() == _HANDLER_NAME:
            root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(JsonFormatter(secrets) if fmt == "json" else ConsoleFormatter(secrets))
    root.addHandler(handler)
    root.setLevel(level)

    # Route uvicorn's own loggers through the same handler and format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

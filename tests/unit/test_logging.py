import json
import logging

import pytest

from packages.shared.logging_setup import REDACTED, ConsoleFormatter, JsonFormatter


def make_record(msg: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("test.logger", logging.INFO, __file__, 1, msg, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_structured_fields() -> None:
    line = JsonFormatter().format(make_record("bars retrieved", symbol="AAPL", bar_count=3))
    payload = json.loads(line)
    assert payload["msg"] == "bars retrieved"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert payload["symbol"] == "AAPL"
    assert payload["bar_count"] == 3
    assert payload["ts"].endswith("+00:00")


@pytest.mark.parametrize("formatter_cls", [JsonFormatter, ConsoleFormatter])
def test_secrets_are_redacted(formatter_cls: type[JsonFormatter | ConsoleFormatter]) -> None:
    formatter = formatter_cls(secrets=["super-secret-value"])
    line = formatter.format(make_record("leaked super-secret-value", detail="super-secret-value"))
    assert "super-secret-value" not in line
    assert REDACTED in line


def test_short_secrets_are_ignored_to_avoid_over_redaction() -> None:
    line = JsonFormatter(secrets=["a"]).format(make_record("a message"))
    assert json.loads(line)["msg"] == "a message"


def test_no_logging_call_uses_a_reserved_extra_key() -> None:
    """`logger.info(..., extra={"created": ...})` raises KeyError at runtime because
    `created` is a LogRecord attribute. Scan every literal `extra={...}` in the code."""
    import ast
    from pathlib import Path

    reserved = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}
    root = Path(__file__).resolve().parents[2]
    offenders = []
    for path in [*root.glob("apps/**/*.py"), *root.glob("packages/**/*.py")]:
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "extra" and isinstance(keyword.value, ast.Dict):
                    for key in keyword.value.keys:
                        if isinstance(key, ast.Constant) and key.value in reserved:
                            offenders.append(f"{path.relative_to(root)}:{node.lineno} {key.value}")
    assert offenders == []

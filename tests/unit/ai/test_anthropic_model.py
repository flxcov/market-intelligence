"""The Anthropic adapter against a mocked HTTP transport (no network, no real key)."""

import json
import logging

import httpx2
import pytest
from pydantic import SecretStr

from packages.ai.explanations import DRAFT_SCHEMA
from packages.ai.model import (
    AIError,
    AIRefusedError,
    AIResponseError,
    AIUnavailableError,
    AnthropicExplanationModel,
)

pytestmark = pytest.mark.anyio

KEY = "sk-ant-test-0000-SECRET"
ANSWER = {"summary": "s", "points": [{"text": "t", "fact_ids": ["a"]}], "caveats": []}


def message(content=None, stop_reason="end_turn") -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content
        if content is not None
        else [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": json.dumps(ANSWER)},
        ],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def model(recorder: Recorder) -> AnthropicExplanationModel:
    return AnthropicExplanationModel(
        api_key=SecretStr(KEY),
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(recorder)),
    )


async def generate(recorder: Recorder):
    return await model(recorder).generate(system="SYS", user="USER", schema=DRAFT_SCHEMA)


async def test_sends_a_structured_low_effort_request_and_parses_json() -> None:
    recorder = Recorder(httpx2.Response(200, json=message(), headers={"request-id": "req_123"}))
    reply = await generate(recorder)
    assert reply.data == ANSWER
    assert reply.model == "claude-opus-5-5"
    assert reply.request_id == "req_123"

    [request] = recorder.requests
    assert request.url.path == "/v1/messages"
    assert KEY in request.headers["authorization"]
    assert "server-side-fallback-2026-07-01" in request.headers["anthropic-beta"]
    body = json.loads(request.content)
    assert body["model"] == "claude-opus-5-5"
    assert body["system"] == "SYS"
    assert body["messages"] == [{"role": "user", "content": "USER"}]
    assert body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": DRAFT_SCHEMA}
    assert "thinking" not in body and "temperature" not in body


@pytest.mark.parametrize(
    ("response", "error", "match"),
    [
        (httpx2.Response(200, json=message(stop_reason="refusal")), AIRefusedError, "declined"),
        (httpx2.Response(200, json=message(stop_reason="max_tokens")), AIResponseError, "cut off"),
        (
            httpx2.Response(200, json=message([{"type": "text", "text": "not json"}])),
            AIResponseError,
            "valid JSON",
        ),
        (
            httpx2.Response(200, json=message([{"type": "text", "text": "[1]"}])),
            AIResponseError,
            "object",
        ),
        (
            httpx2.Response(
                401,
                json={"type": "error", "error": {"type": "authentication_error", "message": "bad"}},
            ),
            AIError,
            "API key",
        ),
        (
            httpx2.Response(
                429,
                json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
            ),
            AIUnavailableError,
            "rate limit",
        ),
        (
            httpx2.Response(
                529,
                json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
            ),
            AIUnavailableError,
            "529",
        ),
        (
            httpx2.Response(
                400,
                json={"type": "error", "error": {"type": "invalid_request_error", "message": "no"}},
            ),
            AIError,
            "HTTP 400",
        ),
        (httpx2.ConnectError("down"), AIUnavailableError, "could not reach"),
    ],
)
async def test_failures_map_to_typed_errors(response, error, match: str) -> None:
    with pytest.raises(error, match=match) as raised:
        await generate(Recorder(response))
    assert KEY not in raised.value.message


async def test_transient_errors_are_retried_a_bounded_number_of_times() -> None:
    overloaded = httpx2.Response(
        529,
        json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
        headers={"retry-after-ms": "1"},
    )
    recorder = Recorder(overloaded, overloaded, overloaded)
    client = AnthropicExplanationModel(
        api_key=SecretStr(KEY),
        max_retries=2,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(recorder)),
    )
    with pytest.raises(AIUnavailableError):
        await client.generate(system="s", user="u", schema=DRAFT_SCHEMA)
    assert len(recorder.requests) == 3  # 1 attempt + 2 retries, then it gives up


async def test_the_key_never_reaches_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    await generate(Recorder(httpx2.Response(200, json=message())))
    assert KEY not in caplog.text

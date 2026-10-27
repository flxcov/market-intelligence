"""The language-model boundary. Callers depend on `ExplanationModel`, not a vendor SDK."""

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from pydantic import SecretStr

from packages.shared.errors import AppError

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
# Server-side refusal fallback: a declined request is re-run on a model Anthropic picks
# by refusal category, inside the same call.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AIError(AppError):
    http_status = 502
    error_type = "ai_error"


class AIUnavailableError(AIError):
    http_status = 503
    error_type = "ai_unavailable"
    retryable = True


class AIRefusedError(AIError):
    error_type = "ai_refused"


class AIResponseError(AIError):
    error_type = "ai_invalid_response"


@dataclass(frozen=True)
class ModelReply:
    data: dict
    model: str
    request_id: str | None = None


class ExplanationModel(ABC):
    name: str

    @abstractmethod
    async def generate(self, *, system: str, user: str, schema: dict) -> ModelReply:
        """Return JSON matching `schema`, or raise an AIError."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Release network resources."""


class AnthropicExplanationModel(ExplanationModel):
    """Claude via the official SDK: structured JSON output, bounded retries, low effort."""

    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        http_client=None,
    ) -> None:
        import anthropic  # imported here so the rest of the app never needs it

        self._anthropic = anthropic
        # The key is passed explicitly: the SDK must not silently pick up some other
        # credential (profile, token) from the machine.
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key.get_secret_value(),
            timeout=timeout_seconds,
            max_retries=max_retries,  # SDK retries 408/409/429/5xx with backoff
            http_client=http_client,
        )
        self.model = model

    async def aclose(self) -> None:
        await self._client.close()

    async def generate(self, *, system: str, user: str, schema: dict) -> ModelReply:
        a = self._anthropic
        try:
            response = await self._client.beta.messages.create(
                model=self.model,
                max_tokens=8000,
                system=system,
                messages=[{"role": "user", "content": user}],
                # Explaining given facts is easy; keep spend low.
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": schema},
                },
                betas=[_FALLBACK_BETA],
                fallbacks="default",
            )
        except a.APITimeoutError:
            raise AIUnavailableError("the AI provider timed out", provider=self.name) from None
        except a.APIConnectionError:
            raise AIUnavailableError(
                "could not reach the AI provider", provider=self.name
            ) from None
        except a.AuthenticationError:
            raise AIError(
                "the AI provider rejected the API key (check ANTHROPIC_API_KEY)",
                provider=self.name,
            ) from None
        except a.RateLimitError:
            raise AIUnavailableError(
                "the AI provider rate limit was hit", provider=self.name
            ) from None
        except a.APIStatusError as exc:
            if exc.status_code >= 500:
                raise AIUnavailableError(
                    f"the AI provider returned HTTP {exc.status_code}", provider=self.name
                ) from None
            raise AIError(
                f"the AI provider rejected the request (HTTP {exc.status_code})",
                provider=self.name,
            ) from None

        request_id = getattr(response, "_request_id", None)
        if response.stop_reason == "refusal":
            raise AIRefusedError("the model declined to answer", provider=self.name)
        if response.stop_reason == "max_tokens":
            raise AIResponseError("the model's answer was cut off", provider=self.name)
        text = "".join(b.text for b in response.content if b.type == "text")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise AIResponseError(
                "the model did not return valid JSON", provider=self.name
            ) from None
        if not isinstance(data, dict):
            raise AIResponseError("the model did not return a JSON object", provider=self.name)
        logger.info(
            "ai explanation generated",
            extra={"provider": self.name, "model": response.model, "request_id": request_id},
        )
        return ModelReply(data=data, model=response.model, request_id=request_id)

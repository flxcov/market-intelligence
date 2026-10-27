"""SEC EDGAR JSON APIs (public, no key). Contract taken from SEC's published API pages
and verified against real responses recorded by an open-source EDGAR library, because
sec.gov was unreachable from the build environment:

    GET https://www.sec.gov/files/company_tickers.json
        {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    GET https://data.sec.gov/submissions/CIK##########.json
        {"cik", "name", "tickers", "exchanges", "sic", "sicDescription", "entityType",
         "fiscalYearEnd", "filings": {"recent": {"accessionNumber": [...], "form": [...],
         "filingDate": [...], "reportDate": [...], "primaryDocument": [...], "items": [...]}}}
    GET https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
        {"cik", "entityName", "facts": {"us-gaap": {CONCEPT: {"label", "units":
         {"USD": [{"start"?, "end", "val", "accn", "fy", "fp", "form", "filed", "frame"?}]}}},
         "dei": {...}}}

SEC fair access: a descriptive User-Agent with a contact address is REQUIRED, and
clients must stay under 10 requests per second.
"""

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Self

import httpx

from packages.sec.errors import (
    SecAccessError,
    SecConfigurationError,
    SecError,
    SecNotFoundError,
    SecResponseError,
    SecUnavailableError,
)
from packages.shared.text import html_to_text
from packages.shared.timeutils import utc_now

logger = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
MAX_DOCUMENT_BYTES = 20_000_000
MIN_INTERVAL_SECONDS = 0.125  # 8 requests/second, under SEC's 10/second limit
_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 16.0
_RETRYABLE = frozenset({429, 500, 502, 503, 504})

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class SecDocument:
    """One fetched JSON document with its provenance (kept for the audit trail)."""

    url: str
    retrieved_at: datetime
    sha256: str
    payload: dict[str, Any]


class SecEdgarClient:
    name = "sec_edgar"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        max_retries: int = 3,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        min_interval: float = MIN_INTERVAL_SECONDS,
    ) -> None:
        self._client = client
        self._max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last_request: float | None = None

    @classmethod
    def create(
        cls,
        *,
        user_agent: str,
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
        sleep: Sleep = asyncio.sleep,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> Self:
        if "@" not in user_agent:
            raise SecConfigurationError(
                "SEC_USER_AGENT must identify you with a contact email, e.g. "
                "'Example Research admin@example.com' (SEC fair-access policy)",
                provider=cls.name,
            )
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            transport=transport,
        )
        return cls(client=client, max_retries=max_retries, sleep=sleep)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ticker_map(self) -> SecDocument:
        return await self.get_json(TICKERS_URL)

    async def submissions(self, cik: int) -> SecDocument:
        return await self.get_json(SUBMISSIONS_URL.format(cik=cik))

    async def company_facts(self, cik: int) -> SecDocument:
        return await self.get_json(COMPANY_FACTS_URL.format(cik=cik))

    async def get_json(self, url: str) -> SecDocument:
        return await self._get(url, _document)

    async def get_text(self, url: str) -> SecDocument:
        """A filing document (HTML or text) as plain text in payload["text"]. Untrusted."""
        if not url.startswith("https://www.sec.gov/Archives/"):
            raise SecResponseError(
                "only sec.gov Archives documents are fetched", provider=self.name
            )
        return await self._get(url, _text_document)

    async def _get(self, url: str, build) -> SecDocument:
        attempts = self._max_retries + 1
        for attempt in range(1, attempts + 1):
            error: SecError
            await self._pace()
            try:
                response = await self._client.get(url)
            except httpx.TimeoutException:
                error = SecUnavailableError("SEC request timed out", provider=self.name)
            except httpx.TransportError as exc:
                error = SecUnavailableError(
                    f"network error contacting SEC ({type(exc).__name__})", provider=self.name
                )
            else:
                if response.status_code == 200:
                    return build(url, response)
                error = _error_for(response, url)
            if not error.retryable or attempt == attempts:
                raise error
            delay = min(_BACKOFF_BASE * 2 ** (attempt - 1), _BACKOFF_MAX)
            logger.warning(
                "retrying SEC request",
                extra={"attempt": attempt, "delay_seconds": delay, "reason": error.error_type},
            )
            await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _pace(self) -> None:
        async with self._lock:
            now = self._clock()
            if self._last_request is not None:
                wait = self._min_interval - (now - self._last_request)
                if wait > 0:
                    await self._sleep(wait)
            self._last_request = self._clock()


def _document(url: str, response: httpx.Response) -> SecDocument:
    body = response.content
    try:
        payload = json.loads(body, parse_float=Decimal)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise SecResponseError(
            "SEC returned a response that is not JSON", provider="sec_edgar"
        ) from None
    if not isinstance(payload, dict):
        raise SecResponseError("SEC returned JSON that is not an object", provider="sec_edgar")
    return SecDocument(
        url=url, retrieved_at=utc_now(), sha256=hashlib.sha256(body).hexdigest(), payload=payload
    )


def _text_document(url: str, response: httpx.Response) -> SecDocument:
    body = response.content
    if len(body) > MAX_DOCUMENT_BYTES:
        raise SecResponseError("SEC document is larger than the 20 MB limit", provider="sec_edgar")
    text = html_to_text(body.decode(response.encoding or "utf-8", errors="replace"))
    return SecDocument(
        url=url,
        retrieved_at=utc_now(),
        sha256=hashlib.sha256(body).hexdigest(),
        payload={"text": text},
    )


def _error_for(response: httpx.Response, url: str) -> SecError:
    status = response.status_code
    if status == 404:
        return SecNotFoundError(f"SEC has no document at {url}", provider="sec_edgar")
    if status == 403:
        return SecAccessError(
            "SEC refused the request (HTTP 403). Check SEC_USER_AGENT names you and a "
            "contact email, and that requests stay under 10 per second.",
            provider="sec_edgar",
        )
    if status in _RETRYABLE:
        return SecUnavailableError(f"SEC returned HTTP {status}", provider="sec_edgar")
    return SecResponseError(f"unexpected HTTP {status} from SEC", provider="sec_edgar")

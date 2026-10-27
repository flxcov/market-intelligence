"""The interface every market data provider adapter implements.

The API and future business logic depend only on this interface, so a provider
can be swapped (or faked in tests) without touching callers.
"""

from abc import ABC, abstractmethod
from typing import ClassVar

from packages.market_data.errors import ProviderRequestError
from packages.market_data.models import BarSeries, BarsRequest
from packages.market_data.news import NewsBatch, NewsRequest
from packages.market_data.options import OptionChainRequest, OptionChainSnapshot


class MarketDataProvider(ABC):
    name: ClassVar[str]

    @abstractmethod
    async def get_bars(self, request: BarsRequest) -> BarSeries:
        """Return validated bars for the request, or raise a MarketDataError.

        Implementations must never fabricate or fill in data: missing data is
        returned as missing (fewer or no bars), and failures raise.
        """

    async def get_option_chain(self, request: OptionChainRequest) -> OptionChainSnapshot:
        """Latest quotes for an underlying's option contracts, or raise a MarketDataError.

        Same rule as bars: contracts without a usable quote are reported as skipped,
        never filled in. Providers without options data keep this default.
        """
        raise ProviderRequestError(
            f"the {self.name} provider does not supply option quotes", provider=self.name
        )

    async def get_news(self, request: NewsRequest) -> NewsBatch:
        """News articles mentioning a symbol. Default: this provider has no news."""
        raise ProviderRequestError(
            f"the {self.name} provider does not supply news", provider=self.name
        )

    async def aclose(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Release network resources. Called on application shutdown."""

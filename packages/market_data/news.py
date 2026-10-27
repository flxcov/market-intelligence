"""News articles from a licensed provider. Article text is UNTRUSTED input: it is stored
and analysed as data, never followed as instructions."""

from datetime import datetime
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from packages.market_data.models import Symbol

MAX_NEWS_ARTICLES = 500


class NewsArticle(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    provider_id: str
    headline: str
    source: str | None = None
    author: str | None = None
    url: str | None = None
    summary: str = ""
    content: str = ""  # may contain HTML
    symbols: list[str] = Field(default_factory=list)
    created_at: AwareDatetime
    updated_at: AwareDatetime


class NewsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: Symbol
    start: AwareDatetime
    end: AwareDatetime
    max_articles: int = Field(default=200, ge=1, le=MAX_NEWS_ARTICLES)

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.start > self.end:
            raise ValueError("start must not be after end")
        return self


class NewsBatch(BaseModel):
    symbol: str
    provider: str
    start: datetime
    end: datetime
    retrieved_at: AwareDatetime
    articles: list[NewsArticle]
    truncated: bool = False
    notices: list[str] = Field(default_factory=list)

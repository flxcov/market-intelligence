"""Evidence: every number in a research report traces to a source or a computation."""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel


class Verification(StrEnum):
    PRIMARY = "primary_source"  # read from an SEC filing's XBRL data
    MARKET_DATA = "market_data"  # stored provider bars (timestamped)
    COMPUTED = "computed"  # deterministic arithmetic over other evidence
    ASSUMPTION = "assumption"  # a stated scenario input, not a fact


class Evidence(BaseModel):
    key: str
    claim: str
    value: Decimal | None
    unit: str | None = None
    verification: Verification
    source_url: str | None = None
    source_title: str | None = None
    publisher: str | None = None
    concept: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    form: str | None = None
    accession: str | None = None
    filed: date | None = None
    retrieved_at: datetime | None = None
    inputs: list[str] = []  # keys of the evidence a computed value was derived from

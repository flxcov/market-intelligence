"""News verification: deduplicate, classify, flag, and corroborate against SEC filings.

News is a LEAD, not proof. An article only counts as corroborated when a primary-source
filing of the matching kind exists close to the article's publication time; an article
about an event filed long before is marked as a re-report, not a new catalyst.
All article text is untrusted data.
"""

import hashlib
import re
from datetime import datetime, timedelta
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel

from packages.market_data.news import NewsArticle
from packages.sec.parsing import Filing
from packages.shared.text import html_to_text

NEAR_DUPLICATE_JACCARD = 0.8
NEAR_DUPLICATE_WINDOW = timedelta(hours=48)
CORROBORATION_BEFORE = timedelta(days=3)  # filing up to 3 days before the article
CORROBORATION_AFTER = timedelta(days=1)  # or 1 day after (article ahead of the filing)
RE_REPORT_AFTER = timedelta(days=7)

_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|ocid$)", re.I)
_WORD = re.compile(r"[a-z0-9]+")
_MONEY_OR_PCT = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?\s?(?:[mb]illion|[mbk]\b)?|\d[\d,]*(?:\.\d+)?\s?%", re.I
)

PROMOTIONAL = re.compile(
    r"\b(skyrocket\w*|to the moon|massive upside|next big thing|don'?t miss|huge gains|"
    r"\d+x (?:return|gain)|must[- ]buy|explod(?:e|es|ing) (?:higher|upward)|"
    r"life[- ]changing|millionaire|guaranteed)\b",
    re.I,
)
ANONYMOUS = re.compile(
    r"\b(people familiar with|sources said|according to (?:people|sources)|"
    r"reportedly|rumou?r(?:ed|s)?|unconfirmed)\b",
    re.I,
)
OPINION = re.compile(
    r"\b(price target|maintains|reiterates|upgrades?|downgrades?|initiates coverage|"
    r"(?:buy|sell|hold|outperform|underperform|overweight|neutral) rating)\b",
    re.I,
)
INSTRUCTION_LIKE = re.compile(
    r"(ignore (?:all |any )?(?:previous|prior|above) (?:instructions|rules)|"
    r"\b(?:system|assistant)\s*:|disregard (?:the )?(?:above|previous))",
    re.I,
)

# Event kinds and the SEC filings that would corroborate them.
CATEGORIES: list[tuple[str, re.Pattern, set[str], set[str]]] = [
    # (category, pattern, forms, 8-K items)
    (
        "earnings",
        re.compile(r"\b(earnings|quarterly results|q[1-4] results|revenue of|eps)\b", re.I),
        {"10-Q", "10-K"},
        {"2.02"},
    ),
    (
        "contract",
        re.compile(r"\b(contract|award(?:ed)?|agreement|partnership)\b", re.I),
        set(),
        {"1.01", "8.01", "7.01"},
    ),
    (
        "offering",
        re.compile(r"\b(offering|private placement|at-the-market|warrants?|dilut\w+)\b", re.I),
        {"424B5", "424B3", "S-3", "S-1"},
        {"3.02", "1.01", "8.01"},
    ),
    (
        "merger",
        re.compile(r"\b(acqui\w+|merger|to buy|takeover)\b", re.I),
        {"DEFM14A", "S-4"},
        {"1.01", "2.01"},
    ),
    (
        "leadership",
        re.compile(r"\b(ceo|cfo|chief executive|resign\w*|appoint\w*)\b", re.I),
        set(),
        {"5.02"},
    ),
    (
        "regulatory",
        re.compile(r"\b(fda|approval|clearance|regulator\w*)\b", re.I),
        set(),
        {"8.01", "7.01"},
    ),
    (
        "debt",
        re.compile(r"\b(notes due|credit facility|loan|refinanc\w+|debt)\b", re.I),
        set(),
        {"2.03", "1.01"},
    ),
]


class Verification(StrEnum):
    CORROBORATED = "corroborated_by_filing"
    RE_REPORT = "re_report_of_older_filing"
    UNCORROBORATED = "uncorroborated"
    OPINION = "opinion_not_fact"
    PROMOTIONAL = "promotional_downgraded"


class AnalysedArticle(BaseModel):
    provider: str
    provider_id: str
    canonical_url: str | None
    headline: str
    source: str | None
    author: str | None
    published_at: datetime
    updated_at: datetime
    fingerprint: str
    category: str | None
    flags: list[str]
    verification: Verification
    corroborating_filing: str | None = None
    duplicate_of: str | None = None  # provider_id of the first copy


def canonical_url(url: str | None) -> str | None:
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k)])
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", parts.netloc.lower().removeprefix("www."), path, query, ""))


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def fingerprint(article: NewsArticle) -> str:
    """Headline AND body: two different stories can share a headline."""
    body = " ".join(_words(html_to_text(article.content or article.summary)))[:1000]
    key = " ".join(_words(article.headline)) + "|" + body
    return hashlib.sha256(key.encode()).hexdigest()


def _jaccard(a: str, b: str) -> float:
    x, y = set(_words(a)), set(_words(b))
    return len(x & y) / len(x | y) if x and y else 0.0


def classify(text: str) -> str | None:
    for name, pattern, _, _ in CATEGORIES:
        if pattern.search(text):
            return name
    return None


def flags_for(article: NewsArticle) -> list[str]:
    body = html_to_text(article.content or "") or article.summary
    text = f"{article.headline} {body}"
    found = []
    if PROMOTIONAL.search(text):
        found.append("promotional language")
    if ANONYMOUS.search(text):
        found.append("anonymous or unconfirmed sourcing")
    if OPINION.search(article.headline):
        found.append("analyst opinion, not a reported fact")
    if INSTRUCTION_LIKE.search(text):
        found.append("contains instruction-like text (treated as data)")
    if body:
        normalised = body.replace(",", "").lower()
        missing = [
            n
            for n in _MONEY_OR_PCT.findall(article.headline)
            if re.sub(r"[\s,$]", "", n).lower().rstrip("%") not in normalised.replace(" ", "")
        ]
        if missing:
            found.append(f"headline figures not found in the body: {', '.join(missing)}")
    if not body:
        found.append("no body text: headline only")
    if article.updated_at - article.created_at > timedelta(hours=24):
        found.append("updated more than a day after publication")
    return found


def corroborate(
    category: str | None, published: datetime, filings: list[Filing]
) -> tuple[Verification, str | None]:
    if category is None:
        return Verification.UNCORROBORATED, None
    _, _, forms, items = next(c for c in CATEGORIES if c[0] == category)

    def matches(f: Filing) -> bool:
        if f.form in forms:
            return True
        return f.form.startswith("8-K") and bool(items & set(f.items.split(",")))

    day = published.date()
    candidates = [f for f in filings if matches(f)]
    near = [
        f
        for f in candidates
        if day - CORROBORATION_BEFORE <= f.filing_date <= day + CORROBORATION_AFTER
    ]
    if near:
        return Verification.CORROBORATED, near[0].url
    older = [
        f for f in candidates if day - timedelta(days=90) <= f.filing_date < day - RE_REPORT_AFTER
    ]
    if older:
        return Verification.RE_REPORT, older[0].url
    return Verification.UNCORROBORATED, None


def analyse(
    articles: list[NewsArticle], filings: list[Filing], as_of: datetime
) -> list[AnalysedArticle]:
    """Point-in-time: articles created, and filings filed, after `as_of` are ignored."""
    filings = [f for f in filings if f.filing_date <= as_of.date()]
    visible = sorted(
        (a for a in articles if a.created_at <= as_of), key=lambda a: (a.created_at, a.provider_id)
    )
    out: list[AnalysedArticle] = []
    by_url: dict[str, str] = {}
    by_print: dict[str, str] = {}
    for article in visible:
        url = canonical_url(article.url)
        prints = fingerprint(article)
        duplicate = (url and by_url.get(url)) or by_print.get(prints)
        if duplicate is None:
            for earlier in out:
                if (
                    earlier.duplicate_of is None
                    and article.created_at - earlier.published_at <= NEAR_DUPLICATE_WINDOW
                    and _jaccard(article.headline, earlier.headline) >= NEAR_DUPLICATE_JACCARD
                ):
                    duplicate = earlier.provider_id
                    break
        flags = flags_for(article)
        category = classify(article.headline)
        if "promotional language" in flags:
            status, filing = Verification.PROMOTIONAL, None
        elif "analyst opinion, not a reported fact" in flags:
            status, filing = Verification.OPINION, None
        else:
            status, filing = corroborate(category, article.created_at, filings)
        out.append(
            AnalysedArticle(
                provider=article.provider,
                provider_id=article.provider_id,
                canonical_url=url,
                headline=article.headline,
                source=article.source,
                author=article.author,
                published_at=article.created_at,
                updated_at=article.updated_at,
                fingerprint=prints,
                category=category,
                flags=flags,
                verification=status,
                corroborating_filing=filing,
                duplicate_of=duplicate or None,
            )
        )
        if duplicate is None:
            if url:
                by_url[url] = article.provider_id
            by_print[prints] = article.provider_id
    return out

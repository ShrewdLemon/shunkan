"""News feeds via Google News RSS — no API key, India-focused queries.

Stdlib XML parsing only; one HTTP call per query with a short cache so
repeated panel refreshes don't hammer the endpoint.
"""

from __future__ import annotations

import email.utils
import html
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

_CACHE: dict[str, tuple[float, list["NewsItem"]]] = {}
_CACHE_TTL = 120.0  # seconds

DEFAULT_MARKET_QUERY = (
    "nifty OR sensex OR \"bank nifty\" OR RBI OR SEBI OR \"FII\" OR \"Indian stock market\""
)


@dataclass
class NewsItem:
    title: str
    link: str
    source: str
    published: datetime | None
    description: str = ""
    # filled by the intel pipeline:
    sentiment: float = 0.0
    summary: str = ""
    impact: dict = field(default_factory=dict)

    @property
    def age_hours(self) -> float:
        if self.published is None:
            return 999.0
        return max((datetime.now(timezone.utc) - self.published).total_seconds() / 3600.0, 0.0)


def fetch_news(
    query: str | None = None, limit: int = 25, timeout: float = 10.0
) -> list[NewsItem]:
    """Fetch headlines for a query (defaults to Indian-market macro feed)."""
    q = (query or DEFAULT_MARKET_QUERY).strip()
    now = time.time()
    cached = _CACHE.get(q)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1][:limit]

    url = "https://news.google.com/rss/search"
    params = {"q": q, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
    resp = httpx.get(url, params=params, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    items = _parse_rss(resp.text)
    _CACHE[q] = (now, items)
    return items[:limit]


def symbol_news(symbol: str, limit: int = 15) -> list[NewsItem]:
    """Headlines for one instrument, biased to Indian financial press."""
    name = symbol.upper().removesuffix(".NS")
    pretty = {
        "^NSEI": "nifty", "NIFTY": "nifty 50", "BANKNIFTY": "bank nifty",
        "^NSEBANK": "bank nifty", "SENSEX": "sensex", "^BSESN": "sensex",
    }.get(name, f"{name} stock NSE")
    return fetch_news(pretty, limit=limit)


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _parse_rss(xml_text: str) -> list[NewsItem]:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out: list[NewsItem] = []
    for item in root.findall(".//item"):
        title = item.findtext("title") or ""
        source = item.findtext("source") or ""
        # Google appends " - Publisher" to titles; strip if it echoes source.
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        published = None
        pub = item.findtext("pubDate")
        if pub:
            try:
                published = email.utils.parsedate_to_datetime(pub)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
        desc = html.unescape(re.sub(r"<[^>]+>", " ", item.findtext("description") or ""))
        desc = re.sub(r"\s+", " ", desc).replace("\xa0", " ").strip()
        title = html.unescape(title).strip()
        # Google News descriptions often just repeat "title source" — drop those.
        if desc and _normalize(desc).startswith(_normalize(title)[:60]):
            desc = ""
        out.append(
            NewsItem(
                title=title,
                link=item.findtext("link") or "",
                source=source.strip(),
                published=published,
                description=desc,
            )
        )
    return out

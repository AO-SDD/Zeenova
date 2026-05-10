"""Crypto news aggregator for ``/news``.

Pulls the latest headlines from a handful of mainstream English-language
crypto outlets via their public RSS feeds — no API key required. Each
source is fetched in parallel, parsed once, and deduplicated by URL
before being merged into a single timeline ordered newest-first.

The result is cached for a short TTL because headlines update on the
order of minutes and a 5-minute window keeps a busy group from
hammering the upstream feeds.

Why RSS, not a JSON news API:
* No auth/API-key plumbing (most JSON APIs gate behind a key now —
  CryptoCompare, CryptoPanic, Newsdata all require one).
* RSS feeds are short, stable, and CDN-cached upstream, so fetch
  latency stays low.
* Three independent feeds give us redundancy: if one outlet 5xxs or
  rate-limits us, the other two still produce a usable list.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final
from xml.etree import ElementTree as ET

import httpx
from cachetools import TTLCache

from .http import shared_async_client

logger = logging.getLogger(__name__)

# Mainstream English-language crypto news outlets that publish full RSS
# feeds without auth. Order matters only for tie-breaks on identical
# timestamps — newer items always win first.
_FEEDS: Final[tuple[tuple[str, str], ...]] = (
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
)

# A friendly UA so the feeds' WAFs don't 403 us.
_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Strip tracking params before deduplicating + presenting URLs.
_TRACKING_PARAMS: Final[tuple[str, ...]] = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "ref",
    "feed",
)

# How many items per source we keep before merging. Each feed publishes
# ~15-30 items; we don't need them all.
_MAX_PER_FEED: Final[int] = 15


@dataclass(slots=True)
class NewsArticle:
    """One parsed RSS item ready for rendering."""

    title: str
    url: str
    source: str
    published_at: datetime


class NewsClient:
    """Multi-source crypto news fetcher with a short response cache."""

    def __init__(
        self,
        timeout: float = 8.0,
        cache_ttl_s: float = 300.0,
    ) -> None:
        # Single shared client across feeds: keeps the TCP+TLS pool warm
        # so the parallel fan-out reuses keepalive sockets to each host.
        self._client = shared_async_client(
            timeout=timeout,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/rss+xml, application/xml, text/xml",
            },
            follow_redirects=True,
        )
        # Single-row cache: the result of ``fetch_latest`` is the same
        # for every caller within the TTL, so we don't need to key it.
        self._cache: TTLCache[str, list[NewsArticle]] = TTLCache(
            maxsize=1, ttl=cache_ttl_s
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_latest(self, limit: int = 8) -> list[NewsArticle]:
        """Return up to ``limit`` newest headlines, deduplicated by URL.

        Returns an empty list when every upstream feed fails — callers
        are expected to render that as "couldn't load news" rather than
        propagating an exception.
        """
        cached = self._cache.get("_")
        if cached is not None:
            return cached[:limit]
        # Fetch every feed in parallel; one slow source can't hold up
        # the rest. ``gather`` with ``return_exceptions`` so a single
        # failure doesn't tank the whole call.
        results = await asyncio.gather(
            *(self._fetch_feed(name, url) for name, url in _FEEDS),
            return_exceptions=True,
        )
        merged: list[NewsArticle] = []
        for res in results:
            if isinstance(res, BaseException):
                logger.debug("news feed failed: %s", res)
                continue
            merged.extend(res)
        if not merged:
            return []
        # Dedup by canonical URL, keeping the first (== latest) occurrence
        # since we sort below.
        merged.sort(key=lambda a: a.published_at, reverse=True)
        seen: set[str] = set()
        out: list[NewsArticle] = []
        for art in merged:
            if art.url in seen:
                continue
            seen.add(art.url)
            out.append(art)
        self._cache["_"] = out
        return out[:limit]

    async def _fetch_feed(self, source: str, url: str) -> list[NewsArticle]:
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            body = resp.text
        except httpx.HTTPError as exc:
            logger.warning("news: fetching %s failed: %s", source, exc)
            return []
        return _parse_rss(source, body)


def _parse_rss(source: str, body: str) -> list[NewsArticle]:
    """Parse an RSS 2.0 / Atom-ish feed into :class:`NewsArticle` rows.

    Designed to be tolerant: items missing a parseable ``pubDate`` are
    dropped instead of crashing, and namespaced tags (Atom, dublincore)
    are accepted alongside their plain RSS counterparts.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        logger.warning("news: parse error for %s: %s", source, exc)
        return []
    out: list[NewsArticle] = []
    # Walk every <item> regardless of how the feed namespaces its tags.
    for item in root.iter():
        tag = _localname(item.tag)
        if tag != "item" and tag != "entry":
            continue
        title = _text(item, "title")
        link = _text(item, "link")
        pub = (
            _text(item, "pubDate")
            or _text(item, "published")
            or _text(item, "updated")
            or _text(item, "date")
        )
        if not title or not link or not pub:
            continue
        ts = _parse_date(pub)
        if ts is None:
            continue
        out.append(
            NewsArticle(
                title=_clean_text(title),
                url=_clean_url(link),
                source=source,
                published_at=ts,
            )
        )
        if len(out) >= _MAX_PER_FEED:
            break
    return out


def _localname(tag: str) -> str:
    """Return the local name from a possibly namespaced tag (``{ns}name``)."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _text(item: ET.Element, name: str) -> str | None:
    """Get the first child text matching ``name`` (case-insensitive, ns-agnostic).

    Atom feeds put the URL in ``<link href="…"/>`` rather than as text;
    we transparently fall back to the ``href`` attribute when the
    element body is empty.
    """
    name_lower = name.lower()
    for child in item:
        if _localname(child.tag).lower() != name_lower:
            continue
        if child.text and child.text.strip():
            return child.text.strip()
        href = child.attrib.get("href")
        if href:
            return href.strip()
    return None


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean_text(value: str) -> str:
    """Strip embedded HTML + collapse whitespace from a feed title."""
    cleaned = _TAG_RE.sub("", value)
    cleaned = (
        cleaned.replace("&amp;", "&")
        .replace("&#039;", "'")
        .replace("&apos;", "'")
        .replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return _WS_RE.sub(" ", cleaned).strip()


def _clean_url(value: str) -> str:
    """Drop tracking query params so dedup works across feeds."""
    if "?" not in value:
        return value
    base, _, query = value.partition("?")
    keep_pairs: list[str] = []
    for pair in query.split("&"):
        if not pair:
            continue
        key = pair.split("=", 1)[0].lower()
        if key in _TRACKING_PARAMS:
            continue
        keep_pairs.append(pair)
    if not keep_pairs:
        return base
    return f"{base}?{'&'.join(keep_pairs)}"


def _parse_date(value: str) -> datetime | None:
    """Parse an RFC-822 or ISO-8601 pubDate string into UTC datetime."""
    # RFC 822 is the RSS 2.0 standard; Atom uses ISO 8601. Try the
    # cheap path first.
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    # Normalise to aware UTC so sorts and comparisons stay consistent
    # regardless of the feed's published offset.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)

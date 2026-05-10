"""Tests for the news aggregator (RSS parser + multi-feed client)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from zeenova_bot.news import (
    NewsArticle,
    NewsClient,
    _clean_text,
    _clean_url,
    _parse_date,
    _parse_rss,
)

RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Sample Feed</title>
    <item>
      <title><![CDATA[Bitcoin holds $80K into weekly close]]></title>
      <link>https://example.com/btc-80k?utm_source=rss&amp;ref=x</link>
      <pubDate>Sun, 10 May 2026 14:56:00 +0000</pubDate>
      <description>x</description>
    </item>
    <item>
      <title>Ethereum down 35% vs &amp;amp; Bitcoin</title>
      <link><![CDATA[https://example.com/eth-vs-btc]]></link>
      <pubDate>Sun, 10 May 2026 16:13:00 +0000</pubDate>
    </item>
    <item>
      <title>Missing date</title>
      <link>https://example.com/no-date</link>
    </item>
  </channel>
</rss>
"""


ATOM_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atomic</title>
  <entry>
    <title>Quantum-proof wallets</title>
    <link href="https://example.com/quantum" />
    <updated>2026-05-10T16:49:28Z</updated>
  </entry>
</feed>
"""


def test_parse_rss_extracts_items() -> None:
    rows = _parse_rss("TestSource", RSS_SAMPLE)
    assert len(rows) == 2  # the dateless item is dropped
    titles = [r.title for r in rows]
    assert "Bitcoin holds $80K into weekly close" in titles
    # HTML entity unescaping kicked in.
    assert any("&" in t and "amp" not in t for t in titles)
    # Tracking params stripped from the URL.
    assert rows[0].url == "https://example.com/btc-80k"
    # Source name preserved.
    assert {r.source for r in rows} == {"TestSource"}


def test_parse_rss_handles_atom_namespace_and_link_href() -> None:
    rows = _parse_rss("Atomic", ATOM_SAMPLE)
    assert len(rows) == 1
    assert rows[0].title == "Quantum-proof wallets"
    # Atom uses <link href="..."/>; we fall back to the attribute.
    assert rows[0].url == "https://example.com/quantum"


def test_parse_rss_tolerates_garbage() -> None:
    assert _parse_rss("Junk", "<not xml") == []
    assert _parse_rss("Junk", "<rss><channel/></rss>") == []


def test_clean_text_strips_html_and_collapses_whitespace() -> None:
    assert _clean_text("<b>Hello</b>   world") == "Hello world"
    assert _clean_text("&amp;quot;hi&amp;quot;") == '"hi"'


def test_clean_url_strips_tracking_params() -> None:
    assert (
        _clean_url("https://x.com/a?utm_source=rss&utm_medium=feed&id=42")
        == "https://x.com/a?id=42"
    )
    assert (
        _clean_url("https://x.com/a?utm_source=rss")
        == "https://x.com/a"
    )
    # Already clean: untouched.
    assert _clean_url("https://x.com/a") == "https://x.com/a"


def test_parse_date_accepts_rfc822_and_iso() -> None:
    rfc = _parse_date("Sun, 10 May 2026 14:56:00 +0000")
    iso = _parse_date("2026-05-10T16:49:28Z")
    assert rfc == datetime(2026, 5, 10, 14, 56, tzinfo=UTC)
    assert iso == datetime(2026, 5, 10, 16, 49, 28, tzinfo=UTC)
    assert _parse_date("nope") is None


@pytest.mark.asyncio
async def test_fetch_latest_merges_dedupes_and_sorts() -> None:
    """The client merges multiple sources, dedupes by URL, and sorts newest-first."""
    feed_a = RSS_SAMPLE
    feed_b = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item>
        <title>Fresh news</title>
        <link>https://example.com/fresh</link>
        <pubDate>Mon, 11 May 2026 00:00:00 +0000</pubDate>
      </item>
      <item>
        <title>Dup of A</title>
        <link>https://example.com/btc-80k</link>
        <pubDate>Sun, 10 May 2026 14:56:00 +0000</pubDate>
      </item>
    </channel></rss>"""

    def handler(req: httpx.Request) -> httpx.Response:
        body = feed_a if "feed-a" in req.url.path else feed_b
        return httpx.Response(200, text=body)

    transport = httpx.MockTransport(handler)
    client = NewsClient()
    # Swap the underlying httpx client for one that hits our mock transport
    # while keeping the rest of the wiring unchanged.
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=transport)
    # Monkey-patch the feed list for this test.
    import zeenova_bot.news as news_mod

    original = news_mod._FEEDS
    news_mod._FEEDS = (
        ("FeedA", "https://example.com/feed-a"),
        ("FeedB", "https://example.com/feed-b"),
    )
    try:
        articles = await client.fetch_latest(limit=10)
    finally:
        news_mod._FEEDS = original
        await client.aclose()

    titles = [a.title for a in articles]
    # Sorted newest-first.
    assert titles[0] == "Fresh news"
    # URL dedup removed the duplicate of the btc-80k story.
    btc_count = sum(1 for a in articles if a.url == "https://example.com/btc-80k")
    assert btc_count == 1


@pytest.mark.asyncio
async def test_fetch_latest_tolerates_one_failing_feed() -> None:
    """A single feed failure must not kill the whole call."""

    def handler(req: httpx.Request) -> httpx.Response:
        if "good" in req.url.path:
            return httpx.Response(200, text=RSS_SAMPLE)
        return httpx.Response(503, text="boom")

    transport = httpx.MockTransport(handler)
    client = NewsClient()
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=transport)
    import zeenova_bot.news as news_mod

    original = news_mod._FEEDS
    news_mod._FEEDS = (
        ("Good", "https://example.com/good"),
        ("Bad", "https://example.com/bad"),
    )
    try:
        articles = await client.fetch_latest(limit=5)
    finally:
        news_mod._FEEDS = original
        await client.aclose()

    assert articles  # we got the good feed's items
    assert all(a.source == "Good" for a in articles)


@pytest.mark.asyncio
async def test_fetch_latest_caches_subsequent_calls() -> None:
    """A second call within the TTL must reuse the cached result."""
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=RSS_SAMPLE)

    transport = httpx.MockTransport(handler)
    client = NewsClient(cache_ttl_s=60.0)
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=transport)
    import zeenova_bot.news as news_mod

    original = news_mod._FEEDS
    news_mod._FEEDS = (("Only", "https://example.com/only"),)
    try:
        first = await client.fetch_latest(limit=5)
        second = await client.fetch_latest(limit=5)
    finally:
        news_mod._FEEDS = original
        await client.aclose()

    assert first == second
    # One HTTP call total — the second fetch_latest hit the cache.
    assert calls["n"] == 1


def test_news_article_dataclass_round_trip() -> None:
    """Sanity check: NewsArticle is a frozen-friendly dataclass."""
    art = NewsArticle(
        title="t",
        url="https://x",
        source="s",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert art.title == "t"
    assert art.source == "s"

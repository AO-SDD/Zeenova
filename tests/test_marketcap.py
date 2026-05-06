"""Tests for the marketcap aggregator and CoinPaprika client."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from zeenova_bot.coinpaprika import CoinPaprikaClient
from zeenova_bot.marketcap import MarketcapAggregator


@pytest.mark.asyncio
async def test_aggregator_returns_first_hit() -> None:
    a = AsyncMock()
    a.fetch_marketcap = AsyncMock(return_value=42.0)
    a.aclose = AsyncMock()
    b = AsyncMock()
    b.fetch_marketcap = AsyncMock(return_value=99.0)
    b.aclose = AsyncMock()

    agg = MarketcapAggregator(a, b)
    assert await agg.fetch_marketcap("BTC") == 42.0
    a.fetch_marketcap.assert_awaited_once()
    b.fetch_marketcap.assert_not_awaited()


@pytest.mark.asyncio
async def test_aggregator_falls_through_on_none() -> None:
    a = AsyncMock()
    a.fetch_marketcap = AsyncMock(return_value=None)
    a.aclose = AsyncMock()
    b = AsyncMock()
    b.fetch_marketcap = AsyncMock(return_value=12345.0)
    b.aclose = AsyncMock()

    agg = MarketcapAggregator(a, b)
    assert await agg.fetch_marketcap("ETH") == 12345.0
    a.fetch_marketcap.assert_awaited_once()
    b.fetch_marketcap.assert_awaited_once()


@pytest.mark.asyncio
async def test_aggregator_falls_through_on_exception() -> None:
    a = AsyncMock()
    a.fetch_marketcap = AsyncMock(side_effect=RuntimeError("boom"))
    a.aclose = AsyncMock()
    b = AsyncMock()
    b.fetch_marketcap = AsyncMock(return_value=1.0)
    b.aclose = AsyncMock()

    agg = MarketcapAggregator(a, b)
    assert await agg.fetch_marketcap("ABC") == 1.0


@pytest.mark.asyncio
async def test_aggregator_caches_hit() -> None:
    a = AsyncMock()
    a.fetch_marketcap = AsyncMock(return_value=7.0)
    a.aclose = AsyncMock()

    agg = MarketcapAggregator(a)
    assert await agg.fetch_marketcap("XYZ") == 7.0
    assert await agg.fetch_marketcap("XYZ") == 7.0
    # Second call should hit the cache, not the source.
    a.fetch_marketcap.assert_awaited_once()


@pytest.mark.asyncio
async def test_aggregator_caches_miss_briefly() -> None:
    a = AsyncMock()
    a.fetch_marketcap = AsyncMock(return_value=None)
    a.aclose = AsyncMock()

    agg = MarketcapAggregator(a)
    assert await agg.fetch_marketcap("UNK") is None
    assert await agg.fetch_marketcap("UNK") is None
    a.fetch_marketcap.assert_awaited_once()


@pytest.mark.asyncio
async def test_aggregator_requires_at_least_one_source() -> None:
    with pytest.raises(ValueError):
        MarketcapAggregator()


@pytest.mark.asyncio
async def test_aggregator_returns_rank_from_first_source_that_has_one() -> None:
    # First source has marketcap but no fetch_rank attribute → skipped.
    a = AsyncMock(spec=["fetch_marketcap", "aclose"])
    a.fetch_marketcap = AsyncMock(return_value=1.0)
    a.aclose = AsyncMock()
    # Second source exposes fetch_rank.
    b = AsyncMock(spec=["fetch_marketcap", "fetch_rank", "aclose"])
    b.fetch_marketcap = AsyncMock(return_value=1.0)
    b.fetch_rank = AsyncMock(return_value=7)
    b.aclose = AsyncMock()

    agg = MarketcapAggregator(a, b)
    assert await agg.fetch_rank("BTC") == 7
    # Cached on second call.
    assert await agg.fetch_rank("BTC") == 7
    b.fetch_rank.assert_awaited_once()


@pytest.mark.asyncio
async def test_aggregator_rank_returns_none_when_no_source_has_it() -> None:
    a = AsyncMock(spec=["fetch_marketcap", "aclose"])
    a.fetch_marketcap = AsyncMock(return_value=1.0)
    a.aclose = AsyncMock()
    agg = MarketcapAggregator(a)
    assert await agg.fetch_rank("XYZ") is None


@pytest.mark.asyncio
async def test_aggregator_aclose_closes_all() -> None:
    a = AsyncMock()
    a.fetch_marketcap = AsyncMock(return_value=None)
    a.aclose = AsyncMock()
    b = AsyncMock()
    b.fetch_marketcap = AsyncMock(return_value=None)
    b.aclose = AsyncMock()

    await MarketcapAggregator(a, b).aclose()
    a.aclose.assert_awaited_once()
    b.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_paprika_picks_lowest_rank_per_symbol() -> None:
    client = CoinPaprikaClient()
    coins_payload = [
        {
            "id": "btc-bitcoin",
            "symbol": "BTC",
            "rank": 1,
            "is_active": True,
            "type": "coin",
        },
        {
            "id": "btcb-bitcoin-bep2",
            "symbol": "BTC",  # collision
            "rank": 1500,
            "is_active": True,
            "type": "coin",
        },
        {
            "id": "inactive-coin",
            "symbol": "OLD",
            "rank": 50,
            "is_active": False,
            "type": "coin",
        },
        {
            "id": "fiat-usd",
            "symbol": "USD",
            "rank": 1,
            "is_active": True,
            "type": "fiat",  # filtered out
        },
    ]

    ticker_payload: dict[str, Any] = {
        "rank": 1,
        # Real CoinPaprika /tickers responses always carry the live price
        # alongside the marketcap; we mock that shape here so
        # ``fetch_marketcap`` (which now goes through the snapshot path)
        # gets a usable row.
        "quotes": {"USD": {"price": 60_000.0, "market_cap": 1_500_000_000_000.0}},
    }

    async def fake_get(path: str, params: dict[str, Any] | None = None) -> Any:
        if path == "/coins":
            return coins_payload
        if path == "/tickers/btc-bitcoin":
            return ticker_payload
        if path == "/coins/btc-bitcoin/ohlcv/today":
            return []  # high/low not available is fine here
        raise AssertionError(f"unexpected path {path}")

    client._get = fake_get  # type: ignore[method-assign]

    cap = await client.fetch_marketcap("btc")
    assert cap == 1_500_000_000_000.0

    # Symbol that doesn't match anything → cached miss.
    assert await client.fetch_marketcap("ZZZNOPE") is None

    await client.aclose()


@pytest.mark.asyncio
async def test_paprika_returns_none_when_marketcap_zero() -> None:
    client = CoinPaprikaClient()

    async def fake_get(path: str, params: dict[str, Any] | None = None) -> Any:
        if path == "/coins":
            return [
                {
                    "id": "deadcoin",
                    "symbol": "DEAD",
                    "rank": 5,
                    "is_active": True,
                    "type": "coin",
                }
            ]
        return {"quotes": {"USD": {"market_cap": 0}}}

    client._get = fake_get  # type: ignore[method-assign]
    assert await client.fetch_marketcap("DEAD") is None
    await client.aclose()


@pytest.mark.asyncio
async def test_paprika_handles_http_error_gracefully() -> None:
    import httpx

    client = CoinPaprikaClient()

    async def fake_get(path: str, params: dict[str, Any] | None = None) -> Any:
        raise httpx.HTTPError("boom")

    client._get = fake_get  # type: ignore[method-assign]
    assert await client.fetch_marketcap("BTC") is None
    await client.aclose()


@pytest.mark.asyncio
async def test_paprika_fetch_price_snapshot_returns_full_payload() -> None:
    client = CoinPaprikaClient()
    coins_payload = [
        {
            "id": "oct-octra",
            "symbol": "OCT",
            "rank": 657,
            "is_active": True,
            "type": "coin",
        }
    ]
    ticker_payload = {
        "rank": 657,
        "quotes": {
            "USD": {
                "price": 0.057,
                "percent_change_24h": 40.1,
                "market_cap": 34_260_000.0,
                "volume_24h": 1_200_000.0,
            }
        },
    }

    ohlcv_payload = [
        {
            "time_open": "2026-05-06T00:00:00Z",
            "high": 0.060,
            "low": 0.039,
            "open": 0.045,
            "close": 0.057,
            "volume": 918395,
        }
    ]

    async def fake_get(path: str, params: dict[str, Any] | None = None) -> Any:
        if path == "/coins":
            return coins_payload
        if path == "/tickers/oct-octra":
            return ticker_payload
        if path == "/coins/oct-octra/ohlcv/today":
            return ohlcv_payload
        raise AssertionError(f"unexpected path {path}")

    client._get = fake_get  # type: ignore[method-assign]
    snap = await client.fetch_price_snapshot("OCT")
    assert snap is not None
    assert snap.symbol == "OCT"
    assert snap.price_usd == pytest.approx(0.057)
    assert snap.change_pct_24h == pytest.approx(40.1)
    assert snap.market_cap_usd == pytest.approx(34_260_000.0)
    assert snap.volume_quote_24h == pytest.approx(1_200_000.0)
    assert snap.rank == 657
    assert snap.high_24h == pytest.approx(0.060)
    assert snap.low_24h == pytest.approx(0.039)
    await client.aclose()


@pytest.mark.asyncio
async def test_paprika_fetch_price_snapshot_unknown_symbol_returns_none() -> None:
    client = CoinPaprikaClient()

    async def fake_get(path: str, params: dict[str, Any] | None = None) -> Any:
        if path == "/coins":
            return []
        raise AssertionError(f"unexpected path {path}")

    client._get = fake_get  # type: ignore[method-assign]
    assert await client.fetch_price_snapshot("ZZZNOPE") is None
    await client.aclose()


@pytest.mark.asyncio
async def test_paprika_fetch_price_snapshot_zero_price_returns_none() -> None:
    client = CoinPaprikaClient()
    coins_payload = [
        {"id": "dead-coin", "symbol": "DEAD", "rank": 5, "is_active": True, "type": "coin"}
    ]

    async def fake_get(path: str, params: dict[str, Any] | None = None) -> Any:
        if path == "/coins":
            return coins_payload
        return {"quotes": {"USD": {"price": 0.0}}}

    client._get = fake_get  # type: ignore[method-assign]
    assert await client.fetch_price_snapshot("DEAD") is None
    await client.aclose()

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
        "quotes": {"USD": {"market_cap": 1_500_000_000_000.0}}
    }

    async def fake_get(path: str, params: dict[str, Any] | None = None) -> Any:
        if path == "/coins":
            return coins_payload
        if path == "/tickers/btc-bitcoin":
            return ticker_payload
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

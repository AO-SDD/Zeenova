"""Tests for the data-layer composition."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from zeenova_bot.services import CoinNotFound, CoinRef, CoinService
from zeenova_bot.timeframes import get_timeframe


def _service(
    *,
    binance_pairs: set[str] | None = None,
    bybit_pairs: set[str] | None = None,
    binance_ticker: dict[str, float | None] | None = None,
    bybit_ticker: dict[str, float | None] | None = None,
    binance_klines: list[list[float]] | None = None,
    bybit_klines: list[list[float]] | None = None,
    marketcap: float | None = 12345.0,
) -> CoinService:
    bn = AsyncMock()
    bn.has_pair = AsyncMock(side_effect=lambda s: s in (binance_pairs or set()))
    bn.fetch_ticker = AsyncMock(return_value=binance_ticker)
    bn.fetch_klines = AsyncMock(return_value=binance_klines)
    bn.aclose = AsyncMock()

    bb = AsyncMock()
    bb.has_pair = AsyncMock(side_effect=lambda s: s in (bybit_pairs or set()))
    bb.fetch_ticker = AsyncMock(return_value=bybit_ticker)
    bb.fetch_klines = AsyncMock(return_value=bybit_klines)
    bb.aclose = AsyncMock()

    mc = AsyncMock()
    mc.fetch_marketcap = AsyncMock(return_value=marketcap)
    mc.aclose = AsyncMock()

    return CoinService(binance=bn, bybit=bb, marketcap=mc)


@pytest.mark.asyncio
async def test_resolve_prefers_binance() -> None:
    svc = _service(binance_pairs={"BTC"}, bybit_pairs={"BTC", "MEGA"})
    ref = await svc.resolve("BTC")
    assert ref == CoinRef(symbol="BTC", pair="BTCUSDT", source="binance")


@pytest.mark.asyncio
async def test_resolve_falls_back_to_bybit() -> None:
    svc = _service(binance_pairs=set(), bybit_pairs={"MEGA"})
    ref = await svc.resolve("MEGA")
    assert ref == CoinRef(symbol="MEGA", pair="MEGAUSDT", source="bybit")


@pytest.mark.asyncio
async def test_resolve_returns_none_when_unknown() -> None:
    svc = _service()
    assert await svc.resolve("DOESNOTEXIST") is None


@pytest.mark.asyncio
async def test_resolve_strips_usdt_suffix_and_dollar() -> None:
    svc = _service(binance_pairs={"ETH"})
    ref = await svc.resolve("$ethusdt")
    assert ref is not None and ref.symbol == "ETH"


@pytest.mark.asyncio
async def test_market_uses_source_from_ref() -> None:
    ticker: dict[str, float | None] = {
        "price": 100.0,
        "change_pct": 1.5,
        "high": 110.0,
        "low": 95.0,
        "volume_quote": 1_000_000.0,
    }
    svc = _service(binance_ticker=ticker, marketcap=999.0)
    ref = CoinRef(symbol="BTC", pair="BTCUSDT", source="binance")
    md = await svc.market(ref)
    assert md.price_usd == 100.0
    assert md.market_cap_usd == 999.0
    assert md.source == "binance"


@pytest.mark.asyncio
async def test_market_raises_when_ticker_unavailable() -> None:
    svc = _service(binance_ticker=None)
    ref = CoinRef(symbol="BTC", pair="BTCUSDT", source="binance")
    with pytest.raises(CoinNotFound):
        await svc.market(ref)


@pytest.mark.asyncio
async def test_candles_dispatches_to_correct_source() -> None:
    rows = [[float(i), 1.0, 2.0, 0.5, 1.5] for i in range(5)]
    svc = _service(bybit_klines=rows)
    tf = get_timeframe("1h")
    out = await svc.candles(
        ref=CoinRef(symbol="MEGA", pair="MEGAUSDT", source="bybit"), timeframe=tf
    )
    assert out is rows


@pytest.mark.asyncio
async def test_candles_raises_when_empty() -> None:
    svc = _service(binance_klines=[])
    with pytest.raises(CoinNotFound):
        await svc.candles(
            ref=CoinRef(symbol="BTC", pair="BTCUSDT", source="binance"),
            timeframe=get_timeframe("1d"),
        )


def test_clean_symbol_helper_strips_suffixes() -> None:
    from zeenova_bot.services import _clean_symbol

    assert _clean_symbol("$btcusdt") == "BTC"
    assert _clean_symbol("ethusd") == "ETH"
    assert _clean_symbol("usdt") == "USDT"  # too short to strip USDT
    assert _clean_symbol("usd") == "USD"  # too short to strip USD
    assert _clean_symbol("  btc  ") == "BTC"

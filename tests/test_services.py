"""Tests for the data-layer composition."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from zeenova_bot.services import (
    OFF_EXCHANGE_SOURCE,
    CoinNotFound,
    CoinRef,
    CoinService,
    PriceSnapshot,
)
from zeenova_bot.timeframes import get_timeframe


def _service(
    *,
    binance_pairs: set[str] | None = None,
    bybit_pairs: set[str] | None = None,
    mexc_pairs: set[str] | None = None,
    binance_ticker: dict[str, float | None] | None = None,
    bybit_ticker: dict[str, float | None] | None = None,
    mexc_ticker: dict[str, float | None] | None = None,
    binance_klines: list[list[float]] | None = None,
    bybit_klines: list[list[float]] | None = None,
    mexc_klines: list[list[float]] | None = None,
    marketcap: float | None = 12345.0,
    off_exchange_snapshots: dict[str, PriceSnapshot | None] | None = None,
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

    mx = AsyncMock()
    mx.has_pair = AsyncMock(side_effect=lambda s: s in (mexc_pairs or set()))
    mx.fetch_ticker = AsyncMock(return_value=mexc_ticker)
    mx.fetch_klines = AsyncMock(return_value=mexc_klines)
    mx.aclose = AsyncMock()

    mc = AsyncMock()
    mc.fetch_marketcap = AsyncMock(return_value=marketcap)
    mc.fetch_rank = AsyncMock(return_value=None)
    mc.aclose = AsyncMock()

    off: AsyncMock | None = None
    if off_exchange_snapshots is not None:
        snapshots = off_exchange_snapshots
        off = AsyncMock()
        off.fetch_price_snapshot = AsyncMock(side_effect=lambda s: snapshots.get(s))

    return CoinService(
        binance=bn, bybit=bb, mexc=mx, marketcap=mc, off_exchange=off
    )


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
async def test_resolve_falls_back_to_mexc() -> None:
    svc = _service(binance_pairs=set(), bybit_pairs=set(), mexc_pairs={"BILL"})
    ref = await svc.resolve("BILL")
    assert ref == CoinRef(symbol="BILL", pair="BILLUSDT", source="mexc")


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
async def test_candles_dispatches_to_mexc() -> None:
    rows = [[float(i), 1.0, 2.0, 0.5, 1.5] for i in range(5)]
    svc = _service(mexc_klines=rows)
    tf = get_timeframe("15m")
    out = await svc.candles(
        ref=CoinRef(symbol="BILL", pair="BILLUSDT", source="mexc"), timeframe=tf
    )
    assert out is rows


@pytest.mark.asyncio
async def test_market_dispatches_to_mexc() -> None:
    ticker: dict[str, float | None] = {
        "price": 0.038,
        "change_pct": 6.7,
        "high": 0.042,
        "low": 0.005,
        "volume_quote": 19_900_000.0,
    }
    svc = _service(mexc_ticker=ticker, marketcap=None)
    md = await svc.market(CoinRef(symbol="BILL", pair="BILLUSDT", source="mexc"))
    assert md.price_usd == 0.038
    assert md.source == "mexc"
    assert md.market_cap_usd is None


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


@pytest.mark.asyncio
async def test_usd_rate_returns_price_when_listed() -> None:
    ticker: dict[str, float | None] = {
        "price": 0.42,
        "change_pct": None,
        "high": None,
        "low": None,
        "volume_quote": None,
    }
    svc = _service(mexc_pairs={"OPG"}, mexc_ticker=ticker)
    assert await svc.usd_rate("OPG") == pytest.approx(0.42)


@pytest.mark.asyncio
async def test_usd_rate_returns_none_when_unlisted() -> None:
    svc = _service()
    assert await svc.usd_rate("NOPE") is None


@pytest.mark.asyncio
async def test_usd_rate_returns_none_when_price_invalid() -> None:
    ticker: dict[str, float | None] = {
        "price": 0.0,  # Garbage price → don't bridge through it.
        "change_pct": None,
        "high": None,
        "low": None,
        "volume_quote": None,
    }
    svc = _service(binance_pairs={"BAD"}, binance_ticker=ticker)
    assert await svc.usd_rate("BAD") is None


# ---------------------------------------------------------------------------
# Off-exchange (CoinPaprika) fallback for thinly-listed coins.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_falls_back_to_off_exchange_when_no_listing() -> None:
    snap = PriceSnapshot(symbol="OCT", price_usd=0.057, market_cap_usd=34_260_000)
    svc = _service(off_exchange_snapshots={"OCT": snap})
    ref = await svc.resolve("OCT")
    assert ref == CoinRef(symbol="OCT", pair="OCT/USD", source=OFF_EXCHANGE_SOURCE)


@pytest.mark.asyncio
async def test_resolve_skips_off_exchange_when_an_exchange_lists_it() -> None:
    snap = PriceSnapshot(symbol="BTC", price_usd=999.0)  # should never be used
    svc = _service(binance_pairs={"BTC"}, off_exchange_snapshots={"BTC": snap})
    ref = await svc.resolve("BTC")
    assert ref == CoinRef(symbol="BTC", pair="BTCUSDT", source="binance")


@pytest.mark.asyncio
async def test_resolve_returns_none_when_off_exchange_also_unknown() -> None:
    svc = _service(off_exchange_snapshots={})
    assert await svc.resolve("ZZZNOPE") is None


@pytest.mark.asyncio
async def test_resolve_skips_off_exchange_for_zero_price() -> None:
    snap = PriceSnapshot(symbol="DEAD", price_usd=0.0)
    svc = _service(off_exchange_snapshots={"DEAD": snap})
    assert await svc.resolve("DEAD") is None


@pytest.mark.asyncio
async def test_market_uses_off_exchange_snapshot() -> None:
    snap = PriceSnapshot(
        symbol="OCT",
        price_usd=0.057,
        change_pct_24h=40.1,
        market_cap_usd=34_260_000.0,
        volume_quote_24h=1_200_000.0,
        rank=657,
    )
    svc = _service(off_exchange_snapshots={"OCT": snap})
    ref = await svc.resolve("OCT")
    assert ref is not None
    md = await svc.market(ref)
    assert md.price_usd == pytest.approx(0.057)
    assert md.price_change_pct_24h == pytest.approx(40.1)
    assert md.market_cap_usd == pytest.approx(34_260_000.0)
    assert md.market_cap_rank == 657
    assert md.source == OFF_EXCHANGE_SOURCE
    assert md.pair == "OCT/USD"


@pytest.mark.asyncio
async def test_market_off_exchange_falls_back_to_marketcap_aggregator() -> None:
    snap = PriceSnapshot(
        symbol="THIN", price_usd=1.23, market_cap_usd=None, rank=None
    )
    svc = _service(
        off_exchange_snapshots={"THIN": snap}, marketcap=999_000.0
    )
    ref = await svc.resolve("THIN")
    assert ref is not None
    md = await svc.market(ref)
    # Snapshot didn't include marketcap → fall back to aggregator's value.
    assert md.market_cap_usd == pytest.approx(999_000.0)


@pytest.mark.asyncio
async def test_candles_raises_for_off_exchange_source() -> None:
    snap = PriceSnapshot(symbol="OCT", price_usd=0.057)
    svc = _service(off_exchange_snapshots={"OCT": snap})
    ref = await svc.resolve("OCT")
    assert ref is not None
    with pytest.raises(CoinNotFound):
        await svc.candles(ref=ref, timeframe=get_timeframe("1d"))


@pytest.mark.asyncio
async def test_usd_rate_uses_off_exchange_snapshot() -> None:
    snap = PriceSnapshot(symbol="OCT", price_usd=0.057)
    svc = _service(off_exchange_snapshots={"OCT": snap})
    assert await svc.usd_rate("OCT") == pytest.approx(0.057)


@pytest.mark.asyncio
async def test_usd_rate_off_exchange_rejects_bad_price() -> None:
    snap = PriceSnapshot(symbol="OCT", price_usd=float("nan"))
    svc = _service(off_exchange_snapshots={"OCT": snap})
    assert await svc.usd_rate("OCT") is None

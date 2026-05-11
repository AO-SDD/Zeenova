"""Tests for the ATH/ATL CoinGecko helper used by ``/ath``."""

from __future__ import annotations

import httpx
import pytest

from zeenova_bot.coingecko import AthAtl, MarketcapClient, _row_to_ath_atl

# A representative ``/coins/markets`` row for BTC. Mirrors the real
# response shape exactly so the parser is exercised against the actual
# CoinGecko schema.
_BTC_ROW = {
    "id": "bitcoin",
    "symbol": "btc",
    "name": "Bitcoin",
    "current_price": 81611.0,
    "market_cap": 1_635_335_939_257,
    "market_cap_rank": 1,
    "ath": 126080.0,
    "ath_change_percentage": -35.27061,
    "ath_date": "2025-10-06T18:57:42.558Z",
    "atl": 67.81,
    "atl_change_percentage": 120253.84742,
    "atl_date": "2013-07-06T00:00:00.000Z",
}


def test_row_to_ath_atl_happy_path() -> None:
    snap = _row_to_ath_atl([_BTC_ROW], "BTC")
    assert snap == AthAtl(
        symbol="BTC",
        name="Bitcoin",
        current_price=81611.0,
        ath=126080.0,
        ath_change_pct=-35.27061,
        ath_date="2025-10-06T18:57:42.558Z",
        atl=67.81,
        atl_change_pct=120253.84742,
        atl_date="2013-07-06T00:00:00.000Z",
        rank=1,
    )


def test_row_to_ath_atl_returns_none_for_empty_payload() -> None:
    assert _row_to_ath_atl([], "BTC") is None
    assert _row_to_ath_atl(None, "BTC") is None
    assert _row_to_ath_atl("not a list", "BTC") is None


def test_row_to_ath_atl_returns_none_when_required_fields_missing() -> None:
    row = dict(_BTC_ROW)
    del row["ath"]
    assert _row_to_ath_atl([row], "BTC") is None


def test_row_to_ath_atl_handles_missing_rank() -> None:
    row = dict(_BTC_ROW)
    row["market_cap_rank"] = None
    snap = _row_to_ath_atl([row], "BTC")
    assert snap is not None
    assert snap.rank is None


def test_row_to_ath_atl_handles_string_rank() -> None:
    row = dict(_BTC_ROW)
    row["market_cap_rank"] = "42"
    snap = _row_to_ath_atl([row], "BTC")
    assert snap is not None
    assert snap.rank == 42


@pytest.mark.asyncio
async def test_fetch_ath_atl_returns_none_for_blank_symbol() -> None:
    mc = MarketcapClient()
    try:
        assert await mc.fetch_ath_atl("") is None
        assert await mc.fetch_ath_atl("   ") is None
    finally:
        await mc.aclose()


@pytest.mark.asyncio
async def test_fetch_ath_atl_caches_hits() -> None:
    """A second lookup for the same symbol must not hit the API.
    Mirrors the marketcap-cache behaviour to keep CoinGecko quotas
    intact under repeated user clicks."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=[_BTC_ROW])

    mc = MarketcapClient()
    mc._client = httpx.AsyncClient(
        base_url="https://api.coingecko.com/api/v3",
        transport=httpx.MockTransport(handler),
    )
    try:
        first = await mc.fetch_ath_atl("BTC")
        second = await mc.fetch_ath_atl("BTC")
    finally:
        await mc.aclose()
    assert first is second
    assert len(calls) == 1
    assert first is not None
    assert first.symbol == "BTC"


@pytest.mark.asyncio
async def test_fetch_ath_atl_caches_misses() -> None:
    """Unknown symbols cache as ``None`` so we don't keep hammering
    CoinGecko for typos."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=[])

    mc = MarketcapClient()
    mc._client = httpx.AsyncClient(
        base_url="https://api.coingecko.com/api/v3",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await mc.fetch_ath_atl("ZZZ") is None
        assert await mc.fetch_ath_atl("ZZZ") is None
    finally:
        await mc.aclose()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_fetch_ath_atl_returns_none_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    mc = MarketcapClient()
    mc._client = httpx.AsyncClient(
        base_url="https://api.coingecko.com/api/v3",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await mc.fetch_ath_atl("BTC") is None
    finally:
        await mc.aclose()

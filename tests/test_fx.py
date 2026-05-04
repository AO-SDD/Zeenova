"""Tests for the FX (currency conversion) client."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from zeenova_bot.fx import FxClient


def _mock_transport(
    handler: Any,
) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_convert_uses_primary_endpoint() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            json={"date": "2024-01-01", "usd": {"egp": 50.0, "btc": 0.00002, "eur": 0.9}},
        )

    fx = FxClient()
    fx._client = httpx.AsyncClient(transport=_mock_transport(handler))
    try:
        assert await fx.convert(2.0, "usd", "egp") == pytest.approx(100.0)
        assert await fx.convert(1.0, "USD", "BTC") == pytest.approx(0.00002)
    finally:
        await fx.aclose()
    # Cached after the first call.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_convert_falls_back_to_secondary_endpoint() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "jsdelivr.net" in url:
            return httpx.Response(503, text="primary down")
        return httpx.Response(
            200, json={"usd": {"egp": 50.0}}
        )

    fx = FxClient()
    fx._client = httpx.AsyncClient(transport=_mock_transport(handler))
    try:
        assert await fx.convert(1.0, "usd", "egp") == pytest.approx(50.0)
    finally:
        await fx.aclose()
    assert len(calls) == 2  # primary failed, secondary succeeded.


@pytest.mark.asyncio
async def test_convert_returns_none_when_currency_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"usd": {"egp": 50.0}})

    fx = FxClient()
    fx._client = httpx.AsyncClient(transport=_mock_transport(handler))
    try:
        assert await fx.convert(1.0, "usd", "xyz") is None
    finally:
        await fx.aclose()


@pytest.mark.asyncio
async def test_convert_returns_none_when_both_endpoints_fail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="all down")

    fx = FxClient()
    fx._client = httpx.AsyncClient(transport=_mock_transport(handler))
    try:
        assert await fx.convert(1.0, "usd", "egp") is None
    finally:
        await fx.aclose()


@pytest.mark.asyncio
async def test_convert_same_currency_no_call() -> None:
    """Same-currency conversion shouldn't even hit the network."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"usd": {}})

    fx = FxClient()
    fx._client = httpx.AsyncClient(transport=_mock_transport(handler))
    try:
        assert await fx.convert(42.5, "USD", "usd") == 42.5
    finally:
        await fx.aclose()
    assert calls == []


@pytest.mark.asyncio
async def test_supports_checks_against_usd_index() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"usd": {"egp": 50.0, "btc": 0.00002}}
        )

    fx = FxClient()
    fx._client = httpx.AsyncClient(transport=_mock_transport(handler))
    try:
        assert await fx.supports("EGP") is True
        assert await fx.supports("btc") is True
        assert await fx.supports("zzz") is False
    finally:
        await fx.aclose()

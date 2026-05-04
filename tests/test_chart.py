"""Smoke tests for the candlestick chart renderer."""

from __future__ import annotations

import math
import time

import pytest

from zeenova_bot.chart import render_candles
from zeenova_bot.timeframes import DEFAULT_TIMEFRAME, get_timeframe


def _fake_candles(n: int = 60) -> list[list[float]]:
    now_ms = int(time.time() * 1000)
    out: list[list[float]] = []
    price = 100.0
    for i in range(n):
        ts = now_ms - (n - i) * 15 * 60 * 1000
        open_ = price
        close_ = price + math.sin(i / 4.0) * 1.5
        high_ = max(open_, close_) + 0.8
        low_ = min(open_, close_) - 0.8
        out.append([ts, open_, high_, low_, close_])
        price = close_
    return out


def test_render_candles_returns_png_bytes() -> None:
    png = render_candles(
        candles=_fake_candles(),
        symbol="TEST",
        timeframe=DEFAULT_TIMEFRAME,
        brand_name="Zeenova",
    )
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 5_000


def test_render_candles_supports_all_timeframes() -> None:
    for code in ("15m", "1h", "4h", "1d"):
        png = render_candles(
            candles=_fake_candles(),
            symbol="BTC",
            timeframe=get_timeframe(code),
            brand_name="Zeenova",
        )
        assert png.startswith(b"\x89PNG")


def test_render_candles_rejects_short_input() -> None:
    with pytest.raises(ValueError):
        render_candles(
            candles=[],
            symbol="BTC",
            timeframe=DEFAULT_TIMEFRAME,
            brand_name="Zeenova",
        )

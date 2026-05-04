"""Tests for timeframe lookup helpers."""

from __future__ import annotations

from zeenova_bot.timeframes import DEFAULT_TIMEFRAME, TIMEFRAMES, get_timeframe


def test_get_timeframe_known_codes() -> None:
    assert get_timeframe("15m").label == "15M"
    assert get_timeframe("1h").label == "1H"
    assert get_timeframe("4h").label == "4H"
    assert get_timeframe("1d").label == "1D"


def test_get_timeframe_falls_back_to_default() -> None:
    assert get_timeframe("nonsense") is DEFAULT_TIMEFRAME


def test_timeframes_have_unique_codes() -> None:
    codes = [tf.code for tf in TIMEFRAMES]
    assert len(codes) == len(set(codes))

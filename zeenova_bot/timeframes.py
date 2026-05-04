"""Timeframe definitions used by the bot's chart buttons."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Timeframe:
    code: str
    label: str
    binance_interval: str
    coingecko_days: int


TIMEFRAMES: tuple[Timeframe, ...] = (
    Timeframe(code="15m", label="15M", binance_interval="15m", coingecko_days=1),
    Timeframe(code="1h", label="1H", binance_interval="1h", coingecko_days=7),
    Timeframe(code="4h", label="4H", binance_interval="4h", coingecko_days=30),
    Timeframe(code="1d", label="1D", binance_interval="1d", coingecko_days=180),
)

TIMEFRAMES_BY_CODE: dict[str, Timeframe] = {tf.code: tf for tf in TIMEFRAMES}
DEFAULT_TIMEFRAME: Timeframe = TIMEFRAMES_BY_CODE["15m"]


def get_timeframe(code: str) -> Timeframe:
    return TIMEFRAMES_BY_CODE.get(code.lower(), DEFAULT_TIMEFRAME)

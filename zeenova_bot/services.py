"""Composes Binance, Bybit, MEXC, and the marketcap helper into a single API."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from .binance import BinanceClient
from .bybit import BybitClient
from .mexc import MexcClient
from .timeframes import Timeframe

logger = logging.getLogger(__name__)


class MarketcapSource(Protocol):
    """Anything that can resolve a ticker symbol to a USD marketcap."""

    async def fetch_marketcap(self, symbol: str) -> float | None: ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class CoinRef:
    """Where to pull live data for a given symbol."""

    symbol: str  # uppercase, e.g. ``BTC``
    pair: str  # e.g. ``BTCUSDT``
    source: str  # ``"binance"``, ``"bybit"``, or ``"mexc"``


@dataclass(slots=True)
class MarketData:
    """Snapshot used to render the price card."""

    symbol: str
    pair: str
    source: str
    price_usd: float
    price_change_pct_24h: float | None
    high_24h: float | None
    low_24h: float | None
    market_cap_usd: float | None
    total_volume_usd_24h: float | None


class CoinNotFoundError(LookupError):
    """Raised when no exchange we know about lists ``<SYMBOL>USDT``."""


# Backwards-compat alias used in earlier drafts.
CoinNotFound = CoinNotFoundError


class CoinService:
    """High-level data layer used by the Telegram handlers."""

    def __init__(
        self,
        binance: BinanceClient,
        bybit: BybitClient,
        mexc: MexcClient,
        marketcap: MarketcapSource,
    ) -> None:
        self.binance = binance
        self.bybit = bybit
        self.mexc = mexc
        self.marketcap = marketcap

    async def aclose(self) -> None:
        await self.binance.aclose()
        await self.bybit.aclose()
        await self.mexc.aclose()
        await self.marketcap.aclose()

    async def resolve(self, symbol: str) -> CoinRef | None:
        """Resolve a symbol to the first exchange that lists ``<symbol>USDT``.

        Order: Binance (deepest liquidity) → Bybit → MEXC. MEXC has the
        widest catalogue of newly listed / low-cap coins so it acts as
        a long tail fallback.
        """
        s = _clean_symbol(symbol)
        if not s:
            return None
        if await self.binance.has_pair(s):
            return CoinRef(symbol=s, pair=f"{s}USDT", source="binance")
        if await self.bybit.has_pair(s):
            return CoinRef(symbol=s, pair=f"{s}USDT", source="bybit")
        if await self.mexc.has_pair(s):
            return CoinRef(symbol=s, pair=f"{s}USDT", source="mexc")
        return None

    async def market(self, ref: CoinRef) -> MarketData:
        """Fetch a current ticker snapshot + cached marketcap, in parallel."""
        ticker, cap = await asyncio.gather(
            self._fetch_ticker(ref),
            self.marketcap.fetch_marketcap(ref.symbol),
            return_exceptions=False,
        )
        if ticker is None:
            raise CoinNotFoundError(f"ticker unavailable for {ref.pair} on {ref.source}")
        return MarketData(
            symbol=ref.symbol,
            pair=ref.pair,
            source=ref.source,
            price_usd=float(ticker["price"] or 0.0),
            price_change_pct_24h=_maybe_float(ticker.get("change_pct")),
            high_24h=_maybe_float(ticker.get("high")),
            low_24h=_maybe_float(ticker.get("low")),
            market_cap_usd=cap,
            total_volume_usd_24h=_maybe_float(ticker.get("volume_quote")),
        )

    async def candles(self, *, ref: CoinRef, timeframe: Timeframe) -> list[list[float]]:
        """Fetch klines from the exchange that ``ref`` was resolved against.

        Newly-listed coins legitimately have very short history (e.g. a
        single ``1d`` candle on day one), so we only treat a completely
        empty response as "not found" — anything with at least one row
        renders fine.
        """
        rows: list[list[float]] | None
        if ref.source == "binance":
            rows = await self.binance.fetch_klines(ref.symbol, timeframe.binance_interval)
        elif ref.source == "bybit":
            rows = await self.bybit.fetch_klines(ref.symbol, timeframe.code)
        elif ref.source == "mexc":
            rows = await self.mexc.fetch_klines(ref.symbol, timeframe.binance_interval)
        else:  # pragma: no cover - guarded by resolve()
            rows = None
        if not rows:
            raise CoinNotFoundError(f"no candles for {ref.pair} on {ref.source}")
        return rows

    async def _fetch_ticker(self, ref: CoinRef) -> dict[str, float | None] | None:
        if ref.source == "binance":
            return await self.binance.fetch_ticker(ref.symbol)
        if ref.source == "bybit":
            return await self.bybit.fetch_ticker(ref.symbol)
        if ref.source == "mexc":
            return await self.mexc.fetch_ticker(ref.symbol)
        return None


def _clean_symbol(symbol: str) -> str:
    s = symbol.strip().upper().lstrip("$")
    if s.endswith("USDT") and len(s) > 4:
        s = s[:-4]
    elif s.endswith("USD") and len(s) > 3:
        s = s[:-3]
    return s


def _maybe_float(v: float | int | None) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

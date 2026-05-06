"""Composes Binance, Bybit, MEXC, and the marketcap helper into a single API."""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Final, Protocol

from .binance import BinanceClient
from .bybit import BybitClient
from .mexc import MexcClient
from .timeframes import Timeframe

logger = logging.getLogger(__name__)


class MarketcapSource(Protocol):
    """Anything that can resolve a ticker symbol to a USD marketcap + rank."""

    async def fetch_marketcap(self, symbol: str) -> float | None: ...

    async def fetch_rank(self, symbol: str) -> int | None: ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class PriceSnapshot:
    """Off-exchange ticker info, e.g. from CoinPaprika.

    Used for thinly-listed coins that aren't quoted on Binance, Bybit,
    or MEXC but are tracked by an aggregator. ``high_24h`` and
    ``low_24h`` are optional because the cheap CoinPaprika ``/tickers``
    endpoint doesn't include them.
    """

    symbol: str
    price_usd: float
    change_pct_24h: float | None = None
    market_cap_usd: float | None = None
    volume_quote_24h: float | None = None
    rank: int | None = None
    high_24h: float | None = None
    low_24h: float | None = None


class OffExchangePriceSource(Protocol):
    """A data source that can price coins not listed on our exchanges."""

    async def fetch_price_snapshot(self, symbol: str) -> PriceSnapshot | None: ...


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
    market_cap_rank: int | None = None


class CoinNotFoundError(LookupError):
    """Raised when no exchange we know about lists ``<SYMBOL>USDT``."""


# Backwards-compat alias used in earlier drafts.
CoinNotFound = CoinNotFoundError


# Pseudo-source name for off-exchange data. Surfaced on :class:`CoinRef`
# so callers (handlers, callback queries) can branch on it without
# importing the off-exchange client directly.
OFF_EXCHANGE_SOURCE: Final[str] = "offexch"


class CoinService:
    """High-level data layer used by the Telegram handlers."""

    def __init__(
        self,
        binance: BinanceClient,
        bybit: BybitClient,
        mexc: MexcClient,
        marketcap: MarketcapSource,
        off_exchange: OffExchangePriceSource | None = None,
    ) -> None:
        self.binance = binance
        self.bybit = bybit
        self.mexc = mexc
        self.marketcap = marketcap
        self.off_exchange = off_exchange

    async def aclose(self) -> None:
        await self.binance.aclose()
        await self.bybit.aclose()
        await self.mexc.aclose()
        await self.marketcap.aclose()

    async def resolve(self, symbol: str) -> CoinRef | None:
        """Resolve a symbol to the first data source that prices it.

        Order: Binance (deepest liquidity) → Bybit → MEXC → the
        off-exchange aggregator (e.g. CoinPaprika) if configured. The
        off-exchange tail catches small/new coins like ``OCT`` that no
        major spot exchange lists yet but that aggregators still track.
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
        if self.off_exchange is not None:
            snapshot = await self.off_exchange.fetch_price_snapshot(s)
            if snapshot is not None and snapshot.price_usd > 0:
                return CoinRef(symbol=s, pair=f"{s}/USD", source=OFF_EXCHANGE_SOURCE)
        return None

    async def market(self, ref: CoinRef) -> MarketData:
        """Fetch ticker + marketcap + rank in parallel for the price card."""
        if ref.source == OFF_EXCHANGE_SOURCE:
            return await self._off_exchange_market(ref)
        ticker, cap, rank = await asyncio.gather(
            self._fetch_ticker(ref),
            self.marketcap.fetch_marketcap(ref.symbol),
            self.marketcap.fetch_rank(ref.symbol),
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
            market_cap_rank=rank,
        )

    async def _off_exchange_market(self, ref: CoinRef) -> MarketData:
        if self.off_exchange is None:
            raise CoinNotFoundError(
                f"{ref.symbol}: off-exchange source unavailable"
            )
        snap = await self.off_exchange.fetch_price_snapshot(ref.symbol)
        if snap is None or snap.price_usd <= 0:
            raise CoinNotFoundError(f"{ref.symbol}: no off-exchange price")
        # Fall back to the marketcap aggregator only when the off-exchange
        # snapshot didn't already include those fields.
        cap = snap.market_cap_usd
        rank = snap.rank
        if cap is None:
            cap = await self.marketcap.fetch_marketcap(ref.symbol)
        if rank is None:
            rank = await self.marketcap.fetch_rank(ref.symbol)
        return MarketData(
            symbol=ref.symbol,
            pair=ref.pair,
            source=ref.source,
            price_usd=snap.price_usd,
            price_change_pct_24h=snap.change_pct_24h,
            high_24h=snap.high_24h,
            low_24h=snap.low_24h,
            market_cap_usd=cap,
            total_volume_usd_24h=snap.volume_quote_24h,
            market_cap_rank=rank,
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
        elif ref.source == OFF_EXCHANGE_SOURCE:
            # Off-exchange aggregators give us a snapshot price but no
            # OHLC history we can chart. Callers handle this by sending
            # a text-only price card.
            raise CoinNotFoundError(f"no candles available for {ref.symbol}")
        else:  # pragma: no cover - guarded by resolve()
            rows = None
        if not rows:
            raise CoinNotFoundError(f"no candles for {ref.pair} on {ref.source}")
        return rows

    async def usd_rate(self, symbol: str) -> float | None:
        """Return the USD price of ``symbol`` from the first source that
        prices it, or ``None`` if the symbol isn't on any of our feeds.
        Used as a fallback for the FX layer when the upstream currency
        feed doesn't know about a coin (e.g. small-cap altcoins).
        """
        ref = await self.resolve(symbol)
        if ref is None:
            return None
        if ref.source == OFF_EXCHANGE_SOURCE:
            assert self.off_exchange is not None  # guaranteed by resolve()
            snap = await self.off_exchange.fetch_price_snapshot(ref.symbol)
            if snap is None:
                return None
            value = snap.price_usd
        else:
            ticker = await self._fetch_ticker(ref)
            if ticker is None:
                return None
            raw = ticker.get("price")
            value_opt = _maybe_float(raw)
            if value_opt is None:
                return None
            value = value_opt
        if value <= 0 or not math.isfinite(value):
            return None
        return value

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

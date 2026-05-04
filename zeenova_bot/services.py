"""High-level service helpers used by the Telegram handlers."""

from __future__ import annotations

import logging

from .binance import BinanceClient
from .coingecko import CoinGeckoClient, CoinSummary, MarketData
from .timeframes import Timeframe

logger = logging.getLogger(__name__)


class CoinService:
    """Combines CoinGecko (universe + market data) and Binance (clean klines)."""

    def __init__(self, gecko: CoinGeckoClient, binance: BinanceClient) -> None:
        self.gecko = gecko
        self.binance = binance

    async def aclose(self) -> None:
        await self.gecko.aclose()
        await self.binance.aclose()

    async def resolve(self, symbol: str) -> CoinSummary | None:
        return await self.gecko.resolve_symbol(symbol)

    async def market(self, coin_id: str) -> MarketData:
        return await self.gecko.fetch_market(coin_id)

    async def candles(
        self, *, summary: CoinSummary, timeframe: Timeframe
    ) -> list[list[float]]:
        """Fetch candles for the requested timeframe.

        Tries Binance first (proper interval), then falls back to CoinGecko's
        coarse OHLC endpoint when the coin is not listed on Binance.
        """
        rows = await self.binance.fetch_klines(
            summary.symbol, timeframe.binance_interval
        )
        if rows and len(rows) >= 2:
            return rows
        return await self.gecko.fetch_ohlc(summary.id, timeframe.coingecko_days)

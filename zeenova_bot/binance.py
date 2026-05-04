"""Optional Binance kline fallback for proper-interval charts.

CoinGecko's free OHLC endpoint only exposes coarse, auto-chosen candle
granularities (30min / 4h / 4d). For coins with a Binance USDT pair we can
fetch real candles at exactly 15m / 1h / 4h / 1d, which is what users expect
from the timeframe buttons.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.binance.com"


class BinanceClient:
    def __init__(self, timeout: float = 10.0) -> None:
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_klines(
        self, symbol: str, interval: str, limit: int = 80
    ) -> list[list[float]] | None:
        """Return klines as ``[ts_ms, open, high, low, close]`` rows, or ``None``.

        Returns ``None`` when the symbol does not exist on Binance so callers
        can fall back to a different data source.
        """
        pair = f"{symbol.upper()}USDT"
        try:
            resp = await self._client.get(
                "/api/v3/klines",
                params={"symbol": pair, "interval": interval, "limit": limit},
            )
        except httpx.HTTPError as exc:
            logger.debug("Binance network error for %s: %s", pair, exc)
            return None
        if resp.status_code == 400:
            return None
        if resp.status_code >= 400:
            logger.debug("Binance HTTP %d for %s: %s", resp.status_code, pair, resp.text[:120])
            return None
        try:
            data: list[list[Any]] = resp.json()
        except ValueError:
            return None
        out: list[list[float]] = []
        for row in data:
            try:
                out.append(
                    [
                        float(row[0]),
                        float(row[1]),
                        float(row[2]),
                        float(row[3]),
                        float(row[4]),
                    ]
                )
            except (TypeError, ValueError, IndexError):
                continue
        return out or None

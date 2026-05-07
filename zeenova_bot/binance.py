"""Binance public-data client.

Used as the **primary** source for tickers and klines because the public
endpoints are free and very generous (1200 req/min per IP).

We default to ``data-api.binance.vision`` — Binance's official public
market-data mirror. It serves the same responses as ``api.binance.com``
but is hosted on a CDN that's reachable from regions where the main
domain returns HTTP 451 ("restricted location"). The base URL can be
overridden via the ``BINANCE_BASE_URL`` env var if needed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

from .http import shared_async_client

logger = logging.getLogger(__name__)

BASE_URL = os.environ.get("BINANCE_BASE_URL", "https://data-api.binance.vision")


class BinanceClient:
    def __init__(self, timeout: float = 10.0) -> None:
        self._client = shared_async_client(base_url=BASE_URL, timeout=timeout)
        self._pairs: set[str] = set()
        self._pairs_loaded_at: float = 0.0
        self._pairs_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = await self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            logger.debug("Binance network error %s: %s", path, exc)
            raise
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"Binance HTTP {resp.status_code}: {resp.text[:200]}",
                request=resp.request,
                response=resp,
            )
        return resp.json()

    async def _ensure_pairs(self, *, max_age_s: float = 3600) -> None:
        now = time.time()
        if self._pairs and now - self._pairs_loaded_at < max_age_s:
            return
        async with self._pairs_lock:
            now = time.time()
            if self._pairs and now - self._pairs_loaded_at < max_age_s:
                return
            try:
                data = await self._get("/api/v3/exchangeInfo", params={"permissions": "SPOT"})
            except (httpx.HTTPError, ValueError):
                logger.warning("Binance: failed to load exchangeInfo", exc_info=True)
                return
            pairs: set[str] = set()
            for s in data.get("symbols", []):
                status = s.get("status")
                quote = s.get("quoteAsset")
                base = s.get("baseAsset")
                pair = s.get("symbol")
                if status != "TRADING" or quote != "USDT" or not base or not pair:
                    continue
                pairs.add(pair)
            self._pairs = pairs
            self._pairs_loaded_at = now
            logger.info("Binance: loaded %d USDT pairs", len(pairs))

    async def has_pair(self, symbol: str) -> bool:
        """Return True iff Binance lists ``<symbol>USDT`` as a TRADING spot pair."""
        await self._ensure_pairs()
        return f"{symbol.upper()}USDT" in self._pairs

    async def fetch_ticker(self, symbol: str) -> dict[str, float | None] | None:
        """Return ``{price, change_pct, high, low, volume_quote}`` or ``None``."""
        pair = f"{symbol.upper()}USDT"
        try:
            row = await self._get("/api/v3/ticker/24hr", params={"symbol": pair})
        except (httpx.HTTPError, ValueError):
            return None
        try:
            return {
                "price": float(row["lastPrice"]),
                "change_pct": float(row["priceChangePercent"]),
                "high": float(row["highPrice"]),
                "low": float(row["lowPrice"]),
                "volume_quote": float(row["quoteVolume"]),
            }
        except (KeyError, ValueError, TypeError):
            return None

    async def fetch_klines(
        self, symbol: str, interval: str, limit: int = 80
    ) -> list[list[float]] | None:
        """Return ``[ts_ms, open, high, low, close]`` rows or ``None``."""
        pair = f"{symbol.upper()}USDT"
        try:
            data = await self._get(
                "/api/v3/klines",
                params={"symbol": pair, "interval": interval, "limit": limit},
            )
        except (httpx.HTTPError, ValueError):
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

"""MEXC public-data client.

MEXC's spot REST API is wire-compatible with Binance's: ``/api/v3``
endpoints with the same response shapes. We use it as the **second
fallback** (after Binance) because:

- It lists ~2,000 USDT spot pairs — one of the widest free catalogues
  available, including most low-cap and newly-listed coins that don't
  reach Binance.
- The public endpoints are reachable from the regions where Bybit's
  CloudFront returns HTTP 403, so MEXC fills the gap left by the
  Bybit fallback when geo-blocked.

Docs: https://mexcdevelop.github.io/apidocs/spot_v3_en/
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .http import shared_async_client

logger = logging.getLogger(__name__)

BASE_URL = "https://api.mexc.com"


# MEXC's klines endpoint accepts a different interval vocabulary than Binance:
# in particular it expects ``60m`` for hourly klines and rejects ``1h`` with
# ``Invalid interval``. Map the internal codes the rest of the bot uses to
# MEXC's accepted strings.
_MEXC_INTERVALS: dict[str, str] = {
    "15m": "15m",
    "1h": "60m",
    "4h": "4h",
    "1d": "1d",
}


class MexcClient:
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
            logger.debug("MEXC network error %s: %s", path, exc)
            raise
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"MEXC HTTP {resp.status_code}: {resp.text[:200]}",
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
                data = await self._get("/api/v3/exchangeInfo")
            except (httpx.HTTPError, ValueError):
                logger.warning("MEXC: failed to load exchangeInfo", exc_info=True)
                return
            pairs: set[str] = set()
            for s in data.get("symbols", []):
                # MEXC encodes "TRADING" as the string "1".
                if str(s.get("status")) != "1":
                    continue
                if s.get("quoteAsset") != "USDT":
                    continue
                if not s.get("isSpotTradingAllowed", False):
                    continue
                pair = s.get("symbol")
                base = s.get("baseAsset")
                if not pair or not base:
                    continue
                pairs.add(pair)
            self._pairs = pairs
            self._pairs_loaded_at = now
            logger.info("MEXC: loaded %d USDT pairs", len(pairs))

    async def has_pair(self, symbol: str) -> bool:
        await self._ensure_pairs()
        return f"{symbol.upper()}USDT" in self._pairs

    async def fetch_ticker(self, symbol: str) -> dict[str, float | None] | None:
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
        """Fetch klines from MEXC, translating Binance-style interval codes."""
        pair = f"{symbol.upper()}USDT"
        mexc_interval = _MEXC_INTERVALS.get(interval, interval)
        try:
            data = await self._get(
                "/api/v3/klines",
                params={"symbol": pair, "interval": mexc_interval, "limit": limit},
            )
        except (httpx.HTTPError, ValueError):
            return None
        out: list[list[float]] = []
        for row in data or []:
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

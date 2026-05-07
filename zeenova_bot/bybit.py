"""Bybit public-market client.

Used as the secondary source so we can serve symbols that aren't listed on
Binance. Bybit's spot API is free (~5 req/sec per IP) and exposes the same
shape of ticker + kline data we need.

Docs: https://bybit-exchange.github.io/docs/v5/market/instrument
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .http import shared_async_client

logger = logging.getLogger(__name__)

BASE_URL = "https://api.bybit.com"


# Map our internal interval codes ("15m" / "1h" / "4h" / "1d") to Bybit's.
_BYBIT_INTERVALS: dict[str, str] = {
    "15m": "15",
    "1h": "60",
    "4h": "240",
    "1d": "D",
}


class BybitClient:
    def __init__(self, timeout: float = 10.0) -> None:
        self._client = shared_async_client(base_url=BASE_URL, timeout=timeout)
        self._pairs: set[str] = set()
        self._pairs_loaded_at: float = 0.0
        self._pairs_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._client.get(path, params=params)
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"Bybit HTTP {resp.status_code}: {resp.text[:200]}",
                request=resp.request,
                response=resp,
            )
        body = resp.json()
        if body.get("retCode", 0) != 0:
            raise RuntimeError(f"Bybit error: {body.get('retMsg', body)}")
        return body.get("result", {})

    async def _ensure_pairs(self, *, max_age_s: float = 3600) -> None:
        now = time.time()
        if self._pairs and now - self._pairs_loaded_at < max_age_s:
            return
        # If we've already failed once, back off so we don't spam logs every
        # request when Bybit's CDN geo-blocks the host.
        if (
            not self._pairs
            and self._pairs_loaded_at
            and now - self._pairs_loaded_at < max_age_s
        ):
            return
        async with self._pairs_lock:
            now = time.time()
            if self._pairs and now - self._pairs_loaded_at < max_age_s:
                return
            try:
                result = await self._get(
                    "/v5/market/instruments-info",
                    params={"category": "spot"},
                )
            except httpx.HTTPStatusError as exc:
                # Bybit's CDN returns 403 from blocked regions — log once
                # at info level and remember the attempt so we don't
                # retry on every lookup.
                if exc.response is not None and exc.response.status_code == 403:
                    logger.info(
                        "Bybit: API unreachable from this region (HTTP 403); "
                        "skipping Bybit fallback"
                    )
                else:
                    logger.warning("Bybit: failed to load instruments-info: %s", exc)
                self._pairs_loaded_at = now
                return
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                logger.warning("Bybit: failed to load instruments-info: %s", exc)
                self._pairs_loaded_at = now
                return
            pairs: set[str] = set()
            for inst in result.get("list", []):
                status = inst.get("status")
                quote = inst.get("quoteCoin")
                base = inst.get("baseCoin")
                pair = inst.get("symbol")
                if status != "Trading" or quote != "USDT" or not base or not pair:
                    continue
                pairs.add(pair)
            self._pairs = pairs
            self._pairs_loaded_at = now
            logger.info("Bybit: loaded %d USDT pairs", len(pairs))

    async def has_pair(self, symbol: str) -> bool:
        await self._ensure_pairs()
        return f"{symbol.upper()}USDT" in self._pairs

    async def fetch_ticker(self, symbol: str) -> dict[str, float | None] | None:
        pair = f"{symbol.upper()}USDT"
        try:
            result = await self._get(
                "/v5/market/tickers",
                params={"category": "spot", "symbol": pair},
            )
        except (httpx.HTTPError, ValueError, RuntimeError):
            return None
        rows = result.get("list", [])
        if not rows:
            return None
        row = rows[0]
        try:
            price = float(row["lastPrice"])
            high = float(row["highPrice24h"])
            low = float(row["lowPrice24h"])
            volume_quote = float(row.get("turnover24h", 0.0))
            change_pct = float(row.get("price24hPcnt", 0.0)) * 100.0
        except (KeyError, ValueError, TypeError):
            return None
        return {
            "price": price,
            "change_pct": change_pct,
            "high": high,
            "low": low,
            "volume_quote": volume_quote,
        }

    async def fetch_klines(
        self, symbol: str, interval_code: str, limit: int = 80
    ) -> list[list[float]] | None:
        pair = f"{symbol.upper()}USDT"
        bybit_interval = _BYBIT_INTERVALS.get(interval_code)
        if bybit_interval is None:
            return None
        try:
            result = await self._get(
                "/v5/market/kline",
                params={
                    "category": "spot",
                    "symbol": pair,
                    "interval": bybit_interval,
                    "limit": limit,
                },
            )
        except (httpx.HTTPError, ValueError, RuntimeError):
            return None
        rows = result.get("list", [])
        if not rows:
            return None
        # Bybit returns newest-first; our renderer wants chronological order.
        rows = list(reversed(rows))
        out: list[list[float]] = []
        for row in rows:
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

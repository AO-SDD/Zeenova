"""Async CoinGecko API client.

Uses the public free API by default; if a Pro key is configured we use the
``pro-api.coingecko.com`` host with the ``x-cg-pro-api-key`` header.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from cachetools import TTLCache

logger = logging.getLogger(__name__)

PUBLIC_BASE = "https://api.coingecko.com/api/v3"
PRO_BASE = "https://pro-api.coingecko.com/api/v3"


@dataclass(slots=True)
class CoinSummary:
    """Resolved info for a coin symbol."""

    id: str
    symbol: str
    name: str
    market_cap_rank: int | None


@dataclass(slots=True)
class MarketData:
    """Current market snapshot for a coin."""

    id: str
    symbol: str
    name: str
    price_usd: float
    price_change_pct_24h: float | None
    high_24h: float | None
    low_24h: float | None
    market_cap_usd: float | None
    total_volume_usd_24h: float | None
    image_url: str | None


class CoinGeckoError(RuntimeError):
    """Raised when CoinGecko returns an error or is unreachable."""


class CoinGeckoClient:
    """Async wrapper around a small subset of the CoinGecko API."""

    def __init__(self, api_key: str = "", timeout: float = 15.0) -> None:
        self._api_key = api_key.strip()
        self._base = PRO_BASE if self._api_key else PUBLIC_BASE
        headers: dict[str, str] = {"accept": "application/json"}
        if self._api_key:
            headers["x-cg-pro-api-key"] = self._api_key
        self._client = httpx.AsyncClient(
            base_url=self._base, headers=headers, timeout=timeout
        )

        self._symbol_index: dict[str, list[CoinSummary]] = {}
        self._symbol_index_loaded_at: float = 0.0
        self._symbol_index_lock = asyncio.Lock()

        self._market_cache: TTLCache[str, MarketData] = TTLCache(maxsize=2048, ttl=30.0)
        self._ohlc_cache: TTLCache[tuple[str, int], list[list[float]]] = TTLCache(
            maxsize=512, ttl=60.0
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = await self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise CoinGeckoError(f"network error: {exc}") from exc
        if resp.status_code == 429:
            raise CoinGeckoError("rate limited by CoinGecko")
        if resp.status_code >= 400:
            raise CoinGeckoError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise CoinGeckoError(f"invalid JSON response: {exc}") from exc

    async def _ensure_symbol_index(self, *, max_age_s: float = 6 * 3600) -> None:
        """Build a symbol → coin list index, refreshed every few hours."""
        now = time.time()
        if self._symbol_index and now - self._symbol_index_loaded_at < max_age_s:
            return
        async with self._symbol_index_lock:
            now = time.time()
            if self._symbol_index and now - self._symbol_index_loaded_at < max_age_s:
                return
            logger.info("Loading CoinGecko coins list")
            data = await self._get("/coins/list", params={"include_platform": "false"})
            index: dict[str, list[CoinSummary]] = {}
            for item in data:
                sym = str(item.get("symbol", "")).strip().lower()
                cid = str(item.get("id", "")).strip()
                name = str(item.get("name", "")).strip()
                if not sym or not cid:
                    continue
                index.setdefault(sym, []).append(
                    CoinSummary(id=cid, symbol=sym, name=name, market_cap_rank=None)
                )
            self._symbol_index = index
            self._symbol_index_loaded_at = now
            logger.info("Loaded %d unique symbols", len(index))

    async def resolve_symbol(self, symbol: str) -> CoinSummary | None:
        """Resolve a ticker symbol (e.g. ``BTC``) to a single CoinGecko coin.

        When multiple coins share the symbol, the one with the highest market cap
        wins. The result is determined by querying ``/coins/markets`` with all
        candidate ids, which also primes the market-data cache.
        """
        sym = symbol.strip().lower()
        if not sym:
            return None
        if sym.endswith("usdt") and len(sym) > 4:
            sym = sym[:-4]
        elif sym.endswith("usd") and len(sym) > 3:
            sym = sym[:-3]

        await self._ensure_symbol_index()
        candidates = self._symbol_index.get(sym, [])
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        ids = ",".join(c.id for c in candidates[:50])
        try:
            rows = await self._get(
                "/coins/markets",
                params={
                    "vs_currency": "usd",
                    "ids": ids,
                    "order": "market_cap_desc",
                    "per_page": 50,
                    "page": 1,
                    "sparkline": "false",
                },
            )
        except CoinGeckoError:
            return candidates[0]
        if not rows:
            return candidates[0]
        top = rows[0]
        for row in rows:
            md = self._row_to_market_data(row)
            self._market_cache[md.id] = md
        return CoinSummary(
            id=str(top["id"]),
            symbol=str(top.get("symbol", sym)).lower(),
            name=str(top.get("name", "")),
            market_cap_rank=top.get("market_cap_rank"),
        )

    @staticmethod
    def _row_to_market_data(row: dict[str, Any]) -> MarketData:
        return MarketData(
            id=str(row["id"]),
            symbol=str(row.get("symbol", "")).upper(),
            name=str(row.get("name", "")),
            price_usd=float(row.get("current_price") or 0.0),
            price_change_pct_24h=_maybe_float(row.get("price_change_percentage_24h")),
            high_24h=_maybe_float(row.get("high_24h")),
            low_24h=_maybe_float(row.get("low_24h")),
            market_cap_usd=_maybe_float(row.get("market_cap")),
            total_volume_usd_24h=_maybe_float(row.get("total_volume")),
            image_url=row.get("image"),
        )

    async def fetch_market(self, coin_id: str) -> MarketData:
        cached = self._market_cache.get(coin_id)
        if cached is not None:
            return cached
        rows = await self._get(
            "/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": coin_id,
                "sparkline": "false",
            },
        )
        if not rows:
            raise CoinGeckoError(f"no market data for {coin_id}")
        md = self._row_to_market_data(rows[0])
        self._market_cache[coin_id] = md
        return md

    async def fetch_ohlc(self, coin_id: str, days: int) -> list[list[float]]:
        """Fetch OHLC candles for a coin.

        Returns a list of ``[ts_ms, open, high, low, close]`` rows. CoinGecko
        chooses the candle granularity automatically based on ``days``:
        1 → 30min, 7-30 → 4h, 31+ → 4d.
        """
        key = (coin_id, days)
        cached = self._ohlc_cache.get(key)
        if cached is not None:
            return cached
        data = await self._get(
            f"/coins/{coin_id}/ohlc",
            params={"vs_currency": "usd", "days": days},
        )
        if not isinstance(data, list):
            raise CoinGeckoError(f"unexpected OHLC payload for {coin_id}")
        self._ohlc_cache[key] = data
        return data


def _maybe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

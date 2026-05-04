"""CoinPaprika public-API client — primary marketcap source.

CoinPaprika offers a free, key-less public API with generous limits
(~25k calls/month per IP, no per-second cap published). Unlike CoinGecko's
free tier — which rate-limits aggressively from shared IP ranges — Paprika
has consistently served us live data, so we use it as the **primary**
marketcap source. CoinGecko remains as a cached fallback in
:mod:`zeenova_bot.coingecko`.

Lookup flow:

1. Build a symbol → coin-id map from ``/v1/coins`` (cached 6h, ~60k coins).
   When multiple coins share a symbol we keep the one with the lowest rank
   (i.e. highest marketcap), matching how CoinGecko's ``/coins/markets``
   sorted by ``market_cap_desc``.
2. Call ``/v1/tickers/{id}`` to get the current marketcap in USD
   (cached 1h per symbol).

Docs: https://api.coinpaprika.com/
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from cachetools import TTLCache

logger = logging.getLogger(__name__)

BASE_URL = "https://api.coinpaprika.com/v1"

# After a 429 we cool down for this long before retrying.
_COOLDOWN_S: float = 60.0


class CoinPaprikaClient:
    """Free marketcap source backed by CoinPaprika's public API."""

    def __init__(
        self,
        timeout: float = 10.0,
        cache_ttl_s: float = 3600,
        coins_ttl_s: float = 6 * 3600,
    ) -> None:
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout)
        self._cap_cache: TTLCache[str, float | None] = TTLCache(
            maxsize=4096, ttl=cache_ttl_s
        )
        # symbol (uppercase) -> coinpaprika coin id (e.g. "btc-bitcoin")
        self._id_map: dict[str, str] = {}
        self._id_map_loaded_at: float = 0.0
        self._id_map_ttl_s = coins_ttl_s
        self._id_map_lock = asyncio.Lock()
        self._cooldown_until: float = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._client.get(path, params=params)
        if resp.status_code == 429:
            self._cooldown_until = time.time() + _COOLDOWN_S
            raise httpx.HTTPStatusError(
                "CoinPaprika rate limited",
                request=resp.request,
                response=resp,
            )
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"CoinPaprika HTTP {resp.status_code}",
                request=resp.request,
                response=resp,
            )
        return resp.json()

    async def _ensure_id_map(self) -> None:
        now = time.time()
        if self._id_map and now - self._id_map_loaded_at < self._id_map_ttl_s:
            return
        if now < self._cooldown_until:
            return
        async with self._id_map_lock:
            now = time.time()
            if self._id_map and now - self._id_map_loaded_at < self._id_map_ttl_s:
                return
            try:
                rows = await self._get("/coins")
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("CoinPaprika: failed to load /coins: %s", exc)
                return
            best: dict[str, tuple[int, str]] = {}
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                if not row.get("is_active", False):
                    continue
                # Paprika tags layer-1 chains as "coin" and ERC-20 / SPL
                # / etc. as "token". We want both — otherwise meme/DeFi
                # tokens like PEPE, WIF, and BILL are excluded.
                if row.get("type") not in {"coin", "token"}:
                    continue
                sym = (row.get("symbol") or "").strip().upper()
                cid = (row.get("id") or "").strip()
                rank = row.get("rank")
                if not sym or not cid or not isinstance(rank, int) or rank <= 0:
                    continue
                # Prefer the lowest rank (highest marketcap) per symbol.
                cur = best.get(sym)
                if cur is None or rank < cur[0]:
                    best[sym] = (rank, cid)
            self._id_map = {sym: cid for sym, (_r, cid) in best.items()}
            self._id_map_loaded_at = now
            logger.info("CoinPaprika: indexed %d ranked symbols", len(self._id_map))

    async def fetch_marketcap(self, symbol: str) -> float | None:
        """Best-effort marketcap lookup. Returns ``None`` on any failure."""
        sym = symbol.strip().upper()
        if not sym:
            return None
        if sym in self._cap_cache:
            return self._cap_cache[sym]
        if time.time() < self._cooldown_until:
            return None
        await self._ensure_id_map()
        cid = self._id_map.get(sym)
        if not cid:
            self._cap_cache[sym] = None
            return None
        try:
            row = await self._get(f"/tickers/{cid}")
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("CoinPaprika ticker fetch failed for %s: %s", sym, exc)
            return None
        cap: float | None = None
        if isinstance(row, dict):
            quotes = row.get("quotes") or {}
            usd = quotes.get("USD") if isinstance(quotes, dict) else None
            if isinstance(usd, dict):
                raw = usd.get("market_cap")
                try:
                    cap = float(raw) if raw is not None else None
                except (TypeError, ValueError):
                    cap = None
                if cap is not None and cap <= 0:
                    cap = None
        self._cap_cache[sym] = cap
        return cap

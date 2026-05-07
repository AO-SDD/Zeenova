"""Tiny CoinGecko client used **only** for marketcap enrichment.

Binance and Bybit cover ticker + kline data with very generous rate
limits, but neither exposes market capitalisation. We therefore call
CoinGecko's free public API at most once per coin per hour to fill in
the ``Marketcap`` line on the price card. If CoinGecko is rate-limited
or unreachable we silently fall back to ``None`` and the card simply
shows ``Marketcap: —``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from cachetools import TTLCache

from .http import shared_async_client

logger = logging.getLogger(__name__)

PUBLIC_BASE = "https://api.coingecko.com/api/v3"
PRO_BASE = "https://pro-api.coingecko.com/api/v3"

# After a 429 we cool down for this long before trying CoinGecko again.
_COOLDOWN_S: float = 90.0


@dataclass(slots=True)
class CoinSummary:
    """Resolved CoinGecko coin id, kept for backwards-compatibility."""

    id: str
    symbol: str
    name: str


class MarketcapClient:
    """Marketcap-only async client with aggressive caching."""

    def __init__(
        self, api_key: str = "", timeout: float = 10.0, cache_ttl_s: float = 3600
    ) -> None:
        self._api_key = api_key.strip()
        base = PRO_BASE if self._api_key else PUBLIC_BASE
        headers: dict[str, str] = {"accept": "application/json"}
        if self._api_key:
            headers["x-cg-pro-api-key"] = self._api_key
        self._client = shared_async_client(base_url=base, headers=headers, timeout=timeout)

        # symbol (uppercase) -> marketcap_usd
        self._cache: TTLCache[str, float | None] = TTLCache(
            maxsize=2048, ttl=cache_ttl_s
        )
        self._cooldown_until: float = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._client.get(path, params=params)
        if resp.status_code == 429:
            self._cooldown_until = time.time() + _COOLDOWN_S
            raise httpx.HTTPStatusError(
                "CoinGecko rate limited",
                request=resp.request,
                response=resp,
            )
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"CoinGecko HTTP {resp.status_code}",
                request=resp.request,
                response=resp,
            )
        return resp.json()

    async def fetch_marketcap(self, symbol: str) -> float | None:
        """Best-effort marketcap lookup for a ticker symbol.

        Returns ``None`` instead of raising. A ``None`` result is **also
        cached** so we don't keep hammering CoinGecko for unknown
        symbols.
        """
        sym = symbol.strip().upper()
        if not sym:
            return None
        if sym in self._cache:
            return self._cache[sym]
        if time.time() < self._cooldown_until:
            return None
        try:
            rows = await self._get(
                "/coins/markets",
                params={
                    "vs_currency": "usd",
                    "symbols": sym.lower(),
                    "order": "market_cap_desc",
                    "per_page": 5,
                    "page": 1,
                    "sparkline": "false",
                },
            )
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("marketcap lookup failed for %s: %s", sym, exc)
            return None
        cap: float | None = None
        if isinstance(rows, list) and rows:
            try:
                cap = float(rows[0].get("market_cap") or 0.0) or None
            except (TypeError, ValueError):
                cap = None
        self._cache[sym] = cap
        return cap

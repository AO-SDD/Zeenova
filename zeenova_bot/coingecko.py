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


@dataclass(slots=True, frozen=True)
class AthAtl:
    """All-time high / all-time low snapshot for a single coin.

    Returned by :meth:`MarketcapClient.fetch_ath_atl`. All prices are in
    USD; ``ath_change_pct`` and ``atl_change_pct`` are signed percentages
    (e.g. ``-35.0`` means the current price sits 35% below the ATH).
    ``*_date`` are raw ISO-8601 strings as returned by CoinGecko —
    callers format them however they need.
    """

    symbol: str  # uppercase, e.g. "BTC"
    name: str
    current_price: float
    ath: float
    ath_change_pct: float
    ath_date: str
    atl: float
    atl_change_pct: float
    atl_date: str
    rank: int | None


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
        # symbol (uppercase) -> AthAtl. ATH/ATL data moves rarely so we
        # cache it for ``cache_ttl_s`` like marketcap. ``None`` is also
        # cached so unknown tickers don't keep hammering the API.
        self._ath_cache: TTLCache[str, AthAtl | None] = TTLCache(
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

    async def fetch_ath_atl(self, symbol: str) -> AthAtl | None:
        """Fetch ATH/ATL snapshot for a ticker symbol.

        Hits the same ``/coins/markets`` endpoint as
        :meth:`fetch_marketcap` — every row already contains the ``ath``
        / ``atl`` fields, so this is a single API call per uncached
        symbol. Returns ``None`` instead of raising (mirroring the rest
        of this client) when the symbol is unknown, the API errors, or
        we're inside the rate-limit cooldown window.
        """
        sym = symbol.strip().upper()
        if not sym:
            return None
        if sym in self._ath_cache:
            return self._ath_cache[sym]
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
            logger.debug("ath/atl lookup failed for %s: %s", sym, exc)
            return None
        snapshot = _row_to_ath_atl(rows, sym)
        self._ath_cache[sym] = snapshot
        return snapshot


def _row_to_ath_atl(rows: Any, sym: str) -> AthAtl | None:
    """Convert a CoinGecko ``/coins/markets`` row to an :class:`AthAtl`.

    Returns ``None`` if the response shape is unexpected or required
    numeric fields are missing — callers treat ``None`` as "not
    available" and the calc/handler layer formats it as a friendly
    error.
    """
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[0]
    if not isinstance(row, dict):
        return None
    try:
        ath = float(row["ath"])
        atl = float(row["atl"])
        current = float(row.get("current_price") or 0.0)
        ath_pct = float(row.get("ath_change_percentage") or 0.0)
        atl_pct = float(row.get("atl_change_percentage") or 0.0)
    except (KeyError, TypeError, ValueError):
        return None
    rank_raw = row.get("market_cap_rank")
    rank: int | None
    try:
        rank = int(rank_raw) if rank_raw is not None else None
    except (TypeError, ValueError):
        rank = None
    return AthAtl(
        symbol=sym,
        name=str(row.get("name") or sym),
        current_price=current,
        ath=ath,
        ath_change_pct=ath_pct,
        ath_date=str(row.get("ath_date") or ""),
        atl=atl,
        atl_change_pct=atl_pct,
        atl_date=str(row.get("atl_date") or ""),
        rank=rank,
    )

"""CoinPaprika public-API client — primary marketcap source.

CoinPaprika offers a free, key-less public API with generous limits
(~25k calls/month per IP, no per-second cap published). Unlike CoinGecko's
free tier — which rate-limits aggressively from shared IP ranges — Paprika
has consistently served us live data, so we use it as the **primary**
marketcap source. CoinGecko remains as a cached fallback in
:mod:`zeenova_bot.coingecko`.

The same ``/v1/tickers/{id}`` response also carries the live USD price,
24h volume, and 24h percent change, so this module doubles as our
"off-exchange" price source for thinly-listed coins that aren't quoted
on Binance, Bybit, or MEXC (see :meth:`fetch_price_snapshot`).

Lookup flow:

1. Build a symbol → coin-id map from ``/v1/coins`` (cached 6h, ~60k coins).
   When multiple coins share a symbol we keep the one with the lowest rank
   (i.e. highest marketcap), matching how CoinGecko's ``/coins/markets``
   sorted by ``market_cap_desc``.
2. Call ``/v1/tickers/{id}`` to get the current marketcap + price in USD
   (cached 1h per symbol for marketcap, ~60s for the live price snapshot).

Docs: https://api.coinpaprika.com/
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from cachetools import TTLCache

from .http import shared_async_client
from .services import PriceSnapshot

logger = logging.getLogger(__name__)

BASE_URL = "https://api.coinpaprika.com/v1"

# After a 429 we cool down for this long before retrying.
_COOLDOWN_S: float = 60.0


@dataclass(slots=True)
class GlobalSnapshot:
    """Aggregate market stats from CoinPaprika's ``/global`` endpoint."""

    market_cap_usd: float | None
    volume_24h_usd: float | None
    bitcoin_dominance_pct: float | None
    cryptocurrencies_number: int | None
    market_cap_change_24h_pct: float | None


@dataclass(slots=True)
class TickerSnapshot:
    """Compact view of a CoinPaprika ``/tickers`` row used by ``/top``."""

    symbol: str
    name: str
    rank: int
    price_usd: float
    change_pct_24h: float
    market_cap_usd: float | None


class CoinPaprikaClient:
    """Free marketcap source backed by CoinPaprika's public API."""

    def __init__(
        self,
        timeout: float = 10.0,
        cache_ttl_s: float = 3600,
        coins_ttl_s: float = 6 * 3600,
        snapshot_ttl_s: float = 60.0,
        global_ttl_s: float = 60.0,
        top_tickers_ttl_s: float = 60.0,
    ) -> None:
        self._client = shared_async_client(base_url=BASE_URL, timeout=timeout)
        self._cap_cache: TTLCache[str, float | None] = TTLCache(
            maxsize=4096, ttl=cache_ttl_s
        )
        # Live-price cache. Shorter TTL than the marketcap cache because
        # the price moves second-to-second; 60s keeps us well under
        # CoinPaprika's monthly limit even with chatty channels.
        self._snapshot_cache: TTLCache[str, PriceSnapshot | None] = TTLCache(
            maxsize=2048, ttl=snapshot_ttl_s
        )
        # /global is one row of aggregate stats — cache by a fixed key.
        # 60s matches the snapshot cache: short enough to stay live, long
        # enough that bursty /market traffic doesn't hammer the API.
        self._global_cache: TTLCache[str, GlobalSnapshot | None] = TTLCache(
            maxsize=1, ttl=global_ttl_s
        )
        # /tickers (the top-N list used by /top) is large (~150 KB for
        # the first 100 rows). Cache by ``limit`` so repeated /top
        # commands within the TTL share a single API call.
        self._top_cache: TTLCache[int, list[TickerSnapshot]] = TTLCache(
            maxsize=8, ttl=top_tickers_ttl_s
        )
        # symbol (uppercase) -> coinpaprika coin id (e.g. "btc-bitcoin")
        self._id_map: dict[str, str] = {}
        # symbol (uppercase) -> marketcap rank from /coins (refreshed every
        # ``coins_ttl_s`` along with the id map). Lets ``fetch_rank`` answer
        # without an extra round-trip for the most common case (top-N coins).
        self._rank_map: dict[str, int] = {}
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
            self._rank_map = {sym: r for sym, (r, _cid) in best.items()}
            self._id_map_loaded_at = now
            logger.info("CoinPaprika: indexed %d ranked symbols", len(self._id_map))

    async def fetch_rank(self, symbol: str) -> int | None:
        """Best-effort marketcap rank lookup. Returns ``None`` on any failure."""
        sym = symbol.strip().upper()
        if not sym:
            return None
        await self._ensure_id_map()
        return self._rank_map.get(sym)

    async def fetch_marketcap(self, symbol: str) -> float | None:
        """Best-effort marketcap lookup. Returns ``None`` on any failure."""
        sym = symbol.strip().upper()
        if not sym:
            return None
        if sym in self._cap_cache:
            return self._cap_cache[sym]
        snap = await self.fetch_price_snapshot(sym)
        cap = snap.market_cap_usd if snap is not None else None
        self._cap_cache[sym] = cap
        return cap

    async def fetch_price_snapshot(self, symbol: str) -> PriceSnapshot | None:
        """Live price + marketcap + rank + today's high/low for ``symbol``.

        Calls ``/tickers/{id}`` (price, change, marketcap, volume, rank)
        and ``/coins/{id}/ohlcv/today`` (today's high/low) in parallel
        and merges the results. Returns ``None`` when the symbol isn't
        in CoinPaprika's catalogue or the API is rate-limited /
        unreachable.

        Note: ``today``'s OHLC resets at UTC midnight, so the high/low
        we report is "since 00:00 UTC" rather than a strict 24h-rolling
        window. For the price card it's close enough and matches what
        most aggregator UIs show.
        """
        sym = symbol.strip().upper()
        if not sym:
            return None
        cached = self._snapshot_cache.get(sym, _MISSING)
        if cached is not _MISSING:
            return cached  # type: ignore[return-value]
        if time.time() < self._cooldown_until:
            return None
        await self._ensure_id_map()
        cid = self._id_map.get(sym)
        if not cid:
            self._snapshot_cache[sym] = None
            return None
        ticker_task = asyncio.create_task(self._safe_get(f"/tickers/{cid}", sym))
        ohlcv_task = asyncio.create_task(
            self._safe_get(f"/coins/{cid}/ohlcv/today", sym)
        )
        ticker_row, ohlcv_row = await asyncio.gather(ticker_task, ohlcv_task)
        snap = _parse_snapshot(sym, ticker_row)
        if snap is not None:
            high, low = _parse_today_high_low(ohlcv_row)
            if high is not None:
                snap.high_24h = high
            if low is not None:
                snap.low_24h = low
        self._snapshot_cache[sym] = snap
        return snap

    async def fetch_global(self) -> GlobalSnapshot | None:
        """Aggregate market stats from CoinPaprika's ``/global`` endpoint.

        Returns ``None`` when the API is unreachable or rate-limiting us.
        Cached for ``global_ttl_s`` (default 60s) so a flood of
        ``/market`` invocations only costs one request per minute.
        """
        cached = self._global_cache.get("_", _MISSING)
        if cached is not _MISSING:
            return cached  # type: ignore[return-value]
        if time.time() < self._cooldown_until:
            return None
        try:
            row = await self._get("/global")
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("CoinPaprika /global failed: %s", exc)
            return None
        snap = _parse_global(row)
        self._global_cache["_"] = snap
        return snap

    async def fetch_top_tickers(self, limit: int = 100) -> list[TickerSnapshot]:
        """Top ``limit`` coins by marketcap, ordered as the API returns.

        Used by ``/top`` to surface the day's biggest movers. The
        endpoint is paged with ``limit`` (max 5000); we keep ``limit``
        modest to bound bandwidth and rely on the cache to amortise
        repeated calls. Returns ``[]`` when the API is unreachable so
        callers can degrade gracefully.
        """
        # Clamp to CoinPaprika's documented bounds — anything else gets
        # 400'd by the API.
        limit = max(1, min(limit, 5000))
        cached = self._top_cache.get(limit)
        if cached is not None:
            return cached
        if time.time() < self._cooldown_until:
            return []
        try:
            rows = await self._get(f"/tickers?limit={limit}")
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("CoinPaprika /tickers?limit=%d failed: %s", limit, exc)
            return []
        out: list[TickerSnapshot] = []
        for row in rows or []:
            t = _parse_ticker(row)
            if t is not None:
                out.append(t)
        self._top_cache[limit] = out
        return out

    async def _safe_get(self, path: str, sym: str) -> Any:
        """Like :meth:`_get` but swallows exceptions into ``None``.

        Used by the snapshot fan-out so a transient OHLCV failure
        doesn't mask the price we already have.
        """
        try:
            return await self._get(path)
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("CoinPaprika fetch %s failed for %s: %s", path, sym, exc)
            return None


# Sentinel so we can distinguish "cached as None" from "not in cache".
_MISSING: object = object()


def _parse_today_high_low(row: Any) -> tuple[float | None, float | None]:
    """Pull today's high/low out of a /coins/{id}/ohlcv/today response.

    The endpoint returns a single-element list with one OHLC bucket
    covering ``[00:00 UTC, now)``. We accept anything that looks like a
    list of dicts with positive numeric ``high`` / ``low`` keys.
    """
    if not isinstance(row, list) or not row:
        return None, None
    bucket = row[0]
    if not isinstance(bucket, dict):
        return None, None
    return _to_positive_float(bucket.get("high")), _to_positive_float(bucket.get("low"))


def _parse_snapshot(symbol: str, row: Any) -> PriceSnapshot | None:
    """Pull the bits we care about out of a CoinPaprika /tickers row."""
    if not isinstance(row, dict):
        return None
    quotes = row.get("quotes")
    usd = quotes.get("USD") if isinstance(quotes, dict) else None
    if not isinstance(usd, dict):
        return None
    price = _to_float(usd.get("price"))
    if price is None or price <= 0:
        return None
    rank_raw = row.get("rank")
    rank = int(rank_raw) if isinstance(rank_raw, int) and rank_raw > 0 else None
    return PriceSnapshot(
        symbol=symbol,
        price_usd=price,
        change_pct_24h=_to_float(usd.get("percent_change_24h")),
        market_cap_usd=_to_positive_float(usd.get("market_cap")),
        volume_quote_24h=_to_positive_float(usd.get("volume_24h")),
        rank=rank,
    )


def _parse_global(row: Any) -> GlobalSnapshot | None:
    """Build a :class:`GlobalSnapshot` from a /global response row.

    Returns ``None`` only when the response is structurally unusable
    (not a dict). Individual fields fall back to ``None`` on parse
    errors so the renderer can still show whatever did come through.
    """
    if not isinstance(row, dict):
        return None
    n_raw = row.get("cryptocurrencies_number")
    n = int(n_raw) if isinstance(n_raw, int) and n_raw > 0 else None
    return GlobalSnapshot(
        market_cap_usd=_to_positive_float(row.get("market_cap_usd")),
        volume_24h_usd=_to_positive_float(row.get("volume_24h_usd")),
        bitcoin_dominance_pct=_to_float(row.get("bitcoin_dominance_percentage")),
        cryptocurrencies_number=n,
        market_cap_change_24h_pct=_to_float(row.get("market_cap_change_24h")),
    )


def _parse_ticker(row: Any) -> TickerSnapshot | None:
    """Compact parse of one CoinPaprika /tickers row for the ``/top`` list.

    Skips rows missing a positive rank, USD quote, or 24h change — those
    are typically stale or de-listed coins that would just clutter the
    movers list.
    """
    if not isinstance(row, dict):
        return None
    rank_raw = row.get("rank")
    if not isinstance(rank_raw, int) or rank_raw <= 0:
        return None
    sym = (row.get("symbol") or "").strip().upper()
    name = (row.get("name") or "").strip()
    if not sym or not name:
        return None
    quotes = row.get("quotes")
    usd = quotes.get("USD") if isinstance(quotes, dict) else None
    if not isinstance(usd, dict):
        return None
    price = _to_positive_float(usd.get("price"))
    change = _to_float(usd.get("percent_change_24h"))
    if price is None or change is None:
        return None
    return TickerSnapshot(
        symbol=sym,
        name=name,
        rank=rank_raw,
        price_usd=price,
        change_pct_24h=change,
        market_cap_usd=_to_positive_float(usd.get("market_cap")),
    )


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_positive_float(v: Any) -> float | None:
    f = _to_float(v)
    if f is None or f <= 0:
        return None
    return f

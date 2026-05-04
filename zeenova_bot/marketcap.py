"""Marketcap aggregator — tries each source in order until one returns a value.

The price card needs a ``Marketcap`` value but Binance and Bybit don't
expose it, so we layer multiple free providers:

1. **CoinPaprika** — primary; key-less, generous limits.
2. **CoinGecko (cached)** — fallback when Paprika doesn't know the symbol
   or is temporarily down.

Each provider has its own internal cache and 429 cooldown, so the
aggregator stays cheap even under bursts. A ``None`` result from all
sources is also cached briefly to avoid hammering them for symbols
they don't know.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol, runtime_checkable

from cachetools import TTLCache

logger = logging.getLogger(__name__)


@runtime_checkable
class _MarketcapSource(Protocol):
    async def fetch_marketcap(self, symbol: str) -> float | None: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class _RankSource(Protocol):
    """Optional capability — sources may also expose the marketcap rank."""

    async def fetch_rank(self, symbol: str) -> int | None: ...


class MarketcapAggregator:
    """Tries each source in order, caches the final result for ``cache_ttl_s``."""

    def __init__(
        self,
        *sources: _MarketcapSource,
        cache_ttl_s: float = 3600,
        miss_ttl_s: float = 600,
    ) -> None:
        if not sources:
            raise ValueError("MarketcapAggregator requires at least one source")
        self._sources = list(sources)
        self._cache: TTLCache[str, float] = TTLCache(maxsize=4096, ttl=cache_ttl_s)
        self._rank_cache: TTLCache[str, int] = TTLCache(
            maxsize=4096, ttl=cache_ttl_s
        )
        self._miss_until: dict[str, float] = {}
        self._miss_ttl_s = miss_ttl_s

    async def aclose(self) -> None:
        for src in self._sources:
            try:
                await src.aclose()
            except Exception:  # noqa: BLE001
                logger.debug("source close failed", exc_info=True)

    async def fetch_marketcap(self, symbol: str) -> float | None:
        sym = symbol.strip().upper()
        if not sym:
            return None
        cached = self._cache.get(sym)
        if cached is not None:
            return cached
        miss_until = self._miss_until.get(sym, 0.0)
        if time.time() < miss_until:
            return None
        for src in self._sources:
            try:
                cap = await src.fetch_marketcap(sym)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "marketcap source %s raised", type(src).__name__, exc_info=True
                )
                continue
            if cap is not None and cap > 0:
                self._cache[sym] = cap
                self._miss_until.pop(sym, None)
                return cap
        # All sources missed — remember briefly to avoid re-fetching every call.
        self._miss_until[sym] = time.time() + self._miss_ttl_s
        return None

    async def fetch_rank(self, symbol: str) -> int | None:
        """Best-effort marketcap rank. Sources that don't expose ranks are skipped."""
        sym = symbol.strip().upper()
        if not sym:
            return None
        cached = self._rank_cache.get(sym)
        if cached is not None:
            return cached
        for src in self._sources:
            if not isinstance(src, _RankSource):
                continue
            try:
                rank = await src.fetch_rank(sym)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "rank source %s raised", type(src).__name__, exc_info=True
                )
                continue
            if rank is not None and rank > 0:
                self._rank_cache[sym] = rank
                return rank
        return None

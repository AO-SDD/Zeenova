"""Crypto Fear & Greed Index client (alternative.me).

A tiny, key-less client for the canonical Crypto Fear & Greed Index
hosted at https://api.alternative.me/fng/. The same numbers power
TradingView's widget and most aggregator dashboards.

The index is refreshed once per day (UTC), so we cache it for 5
minutes by default — short enough to pick up the daily roll-over
quickly, long enough to make ``/market`` essentially free.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from cachetools import TTLCache

logger = logging.getLogger(__name__)

BASE_URL = "https://api.alternative.me"


@dataclass(slots=True)
class FearGreed:
    """Snapshot of the Crypto Fear & Greed Index."""

    value: int  # 0..100
    classification: str  # e.g. "Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed"


class FearGreedClient:
    """Read-only client for alternative.me's Fear & Greed Index."""

    def __init__(self, timeout: float = 10.0, cache_ttl_s: float = 300.0) -> None:
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout)
        # Single-row cache (the index has exactly one current value).
        self._cache: TTLCache[str, FearGreed | None] = TTLCache(
            maxsize=1, ttl=cache_ttl_s
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_current(self) -> FearGreed | None:
        """Return the latest index value, or ``None`` on any failure."""
        cached = self._cache.get("_", _MISSING)
        if cached is not _MISSING:
            return cached  # type: ignore[return-value]
        try:
            resp = await self._client.get("/fng/", params={"limit": 1})
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Fear & Greed fetch failed: %s", exc)
            return None
        snap = _parse(payload)
        self._cache["_"] = snap
        return snap


# Sentinel so we can cache a "fetched but unparseable" result as None
# without confusing it with "never fetched".
_MISSING: object = object()


def _parse(payload: Any) -> FearGreed | None:
    """Pull the latest reading out of a /fng/ response.

    The response shape is ``{"data": [{"value": "55",
    "value_classification": "Greed", ...}], "metadata": {...}}``.
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return None
    row = data[0]
    if not isinstance(row, dict):
        return None
    raw_value = row.get("value")
    raw_classification = row.get("value_classification")
    try:
        # ``value`` arrives as a string in the public API, but be defensive.
        value = int(float(raw_value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not 0 <= value <= 100:
        return None
    classification = (
        raw_classification.strip()
        if isinstance(raw_classification, str) and raw_classification.strip()
        else _classify(value)
    )
    return FearGreed(value=value, classification=classification)


def _classify(value: int) -> str:
    """Bucket a 0..100 score into the standard label.

    Used as a fallback when the API omits ``value_classification``. The
    cut-offs mirror alternative.me's own buckets.
    """
    if value <= 24:
        return "Extreme Fear"
    if value <= 49:
        return "Fear"
    if value == 50:
        return "Neutral"
    if value <= 74:
        return "Greed"
    return "Extreme Greed"

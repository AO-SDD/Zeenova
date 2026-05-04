"""Free, no-key currency conversion via ``fawazahmed0/currency-api``.

The endpoint returns rates for ~200 fiat currencies *and* common crypto
tickers in a single document, so a single client handles fiat↔fiat,
fiat↔crypto, and crypto↔crypto conversions transparently.

We hit the jsdelivr CDN first and the Cloudflare Pages mirror second so a
single CDN outage doesn't take the feature down. Rates update once a day
upstream, so we cache aggressively (1 h) to stay well below the CDNs'
rate-limits.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from cachetools import TTLCache

logger = logging.getLogger(__name__)

# These return the same JSON; we just need a fallback if the primary CDN
# hiccups. ``@latest`` always points at the most recent rates document.
_PRIMARY = (
    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest"
    "/v1/currencies/{}.json"
)
_FALLBACK = (
    "https://latest.currency-api.pages.dev/v1/currencies/{}.json"
)


class FxClient:
    """Async client for fiat + crypto currency conversion."""

    def __init__(
        self,
        *,
        cache_ttl_s: int = 3600,
        http_timeout_s: float = 6.0,
    ) -> None:
        self._client = httpx.AsyncClient(timeout=http_timeout_s)
        # One TTLCache slot per "from" currency. The payload itself is a
        # dict of ~200 entries, so 64 slots ≈ 64 × 200 = 12 800 cached rates.
        self._cache: TTLCache[str, dict[str, float]] = TTLCache(
            maxsize=64, ttl=cache_ttl_s
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _rates(self, from_ccy: str) -> dict[str, float] | None:
        from_ccy = from_ccy.lower()
        cached = self._cache.get(from_ccy)
        if cached is not None:
            return cached
        for url_tpl in (_PRIMARY, _FALLBACK):
            url = url_tpl.format(from_ccy)
            try:
                response = await self._client.get(url)
                response.raise_for_status()
                payload: dict[str, Any] = response.json()
            except httpx.HTTPError as exc:
                logger.debug("fx: %s failed: %s", url, exc)
                continue
            except ValueError as exc:
                logger.debug("fx: invalid JSON from %s: %s", url, exc)
                continue
            rates = payload.get(from_ccy)
            if not isinstance(rates, dict):
                logger.debug("fx: unexpected payload shape for %s", from_ccy)
                continue
            clean = {
                str(k).lower(): float(v)
                for k, v in rates.items()
                if isinstance(v, int | float)
            }
            self._cache[from_ccy] = clean
            return clean
        return None

    async def convert(
        self, amount: float, from_ccy: str, to_ccy: str
    ) -> float | None:
        """Convert ``amount`` from one currency to another. Returns ``None``
        if either side isn't supported or the upstream API is unreachable."""
        from_ccy = from_ccy.lower()
        to_ccy = to_ccy.lower()
        if from_ccy == to_ccy:
            return amount
        rates = await self._rates(from_ccy)
        if rates is None:
            return None
        rate = rates.get(to_ccy)
        if rate is None:
            return None
        return amount * rate

    async def supports(self, ccy: str) -> bool:
        """Check whether ``ccy`` is a known currency on the upstream feed."""
        rates = await self._rates("usd")
        return rates is not None and ccy.lower() in rates

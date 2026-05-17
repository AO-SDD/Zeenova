"""Lightweight ENS name resolver.

``/wallet`` accepts both raw ``0x…`` addresses and ENS names like
``vitalik.eth``. Resolving the latter to an address is a separate
concern — Etherscan's API doesn't speak ENS — so we delegate to the
public ENSIdeas resolver (`api.ensideas.com/ens/resolve/{name}`), a
zero-dependency JSON gateway widely used by wallet UIs. The gateway is
free, doesn't need an API key, and accepts both forward (``name → addr``)
and reverse lookups.

We keep the surface tiny:

* :func:`looks_like_ens` — cheap syntactic check the handler uses to
  decide whether to route the input through the resolver instead of
  rejecting it as "invalid address".
* :class:`EnsClient` — the actual resolver, with a TTL cache and the
  same 90-second cooldown convention as the other HTTP clients.

ENSIdeas covers all canonical ENS names (``.eth``, ``.xyz``,
sub-names, etc.). Cross-chain names (``.bnb``, ``.crypto``, …) are not
supported by this gateway and will surface a clean "not resolvable"
error to the user.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx
from cachetools import TTLCache

from .http import shared_async_client

logger = logging.getLogger(__name__)

# Public free resolver — same one wallet UIs (e.g. Rainbow's docs)
# point users at. No API key, single endpoint, returns ``{"address": …}``.
RESOLVER_URL = "https://api.ensideas.com/ens/resolve"

# Cooldown after a 429 / 5xx so a hot bot doesn't hammer the gateway.
_COOLDOWN_S: float = 90.0

# ENS records don't change often; a successful resolution is stable for
# minutes. Five minutes is a good "cheap repeat clicks" budget without
# masking an actual on-chain rename.
_CACHE_TTL_S: float = 300.0

# A practical name shape: one or more labels separated by dots, each
# label is letters/digits/hyphens. We let the resolver handle the
# canonical TLD list — this is just a fast pre-filter so we don't ship
# every typo to the network.
_ENS_RE = re.compile(
    r"^(?=.{3,253}$)(?:[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,63}$"
)


def looks_like_ens(value: str) -> bool:
    """True when ``value`` looks like a domain name we should try to
    resolve. False for raw ``0x…`` addresses and random garbage.

    The check is intentionally permissive: anything that has at least
    one dot and matches the standard label syntax passes. The actual
    "is this a real ENS name?" decision is the resolver's job.
    """
    candidate = value.strip().lower()
    if not candidate or candidate.startswith("0x"):
        return False
    return bool(_ENS_RE.match(candidate))


class EnsClient:
    """Resolve ENS-style names to 0x… addresses via ENSIdeas."""

    def __init__(self, timeout: float = 6.0) -> None:
        self._client = shared_async_client(timeout=timeout)
        self._cache: TTLCache[str, str] = TTLCache(maxsize=512, ttl=_CACHE_TTL_S)
        self._cooldown_until: float = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def resolve(self, name: str) -> str | None:
        """Return the lowercased 0x address ``name`` resolves to, or
        ``None`` when the name doesn't resolve / the gateway is down.

        Caches successes only — a transient lookup failure should not
        stick around for five minutes.
        """
        key = name.strip().lower()
        if not key:
            return None
        if key in self._cache:
            return self._cache[key]
        if time.time() < self._cooldown_until:
            return None
        try:
            resp = await self._client.get(f"{RESOLVER_URL}/{key}")
        except httpx.HTTPError as exc:
            logger.debug("ens: resolve %r failed: %s", key, exc)
            return None
        if resp.status_code == 429 or resp.status_code >= 500:
            self._cooldown_until = time.time() + _COOLDOWN_S
            return None
        if resp.status_code >= 400:
            return None
        try:
            body: Any = resp.json()
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        addr = body.get("address")
        if not isinstance(addr, str) or not addr.startswith("0x"):
            return None
        # ENSIdeas returns the EIP-55 checksum form; lower-case it so
        # downstream callers (Etherscan, the wallet cache) see a single
        # canonical shape per address.
        resolved = addr.lower()
        self._cache[key] = resolved
        return resolved


__all__ = ["EnsClient", "RESOLVER_URL", "looks_like_ens"]

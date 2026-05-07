"""Shared httpx defaults.

Every outbound HTTP client in the bot is funnelled through this module so
they share the same connection-pool sizing and read/connect timeout
budget. Bumping the limits here gives the whole bot more concurrent
upstream capacity in one place.

Defaults are tuned for "the bot is in many busy groups all calling the
price card at once":

* ``max_connections=200`` — 5x httpx's default of 100, so a burst of
  concurrent requests can each open their own socket without queueing
  behind an in-flight one.
* ``max_keepalive_connections=100`` — 10x default, keeps sockets warm
  between bursts so we don't pay the TCP+TLS handshake cost on every
  call (typical providers like Binance/CoinPaprika want sustained TLS
  reuse for best p99 latency).
* ``keepalive_expiry=60s`` — long enough to span quiet periods between
  /market polls without dropping the connection.
* Timeouts: 5s connect, 10s read by default. Connect is short on
  purpose so a flaky provider can't hold up the resolve fan-out.
"""

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_LIMITS: httpx.Limits = httpx.Limits(
    max_connections=200,
    max_keepalive_connections=100,
    keepalive_expiry=60.0,
)


def shared_async_client(
    *,
    base_url: str | None = None,
    timeout: float = 10.0,
    connect_timeout: float = 5.0,
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` wired to the shared pool defaults."""
    timeout_cfg = httpx.Timeout(timeout, connect=connect_timeout)
    return httpx.AsyncClient(
        base_url=base_url or "",
        timeout=timeout_cfg,
        limits=DEFAULT_LIMITS,
        headers=headers or {},
        **kwargs,
    )

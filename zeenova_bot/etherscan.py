"""Etherscan V2 API client used by the ``/wallet`` command.

Only covers the small handful of endpoints we need to render a wallet
summary card: native ETH balance, recent transaction list, and (for the
"transactions sent" counter) the account nonce. Everything is keyed by
a single ``ETHERSCAN_API_KEY``; one free key from etherscan.io now
works for all 60+ chains via the V2 multichain API
(`docs.etherscan.io/v2-migration`).

The client follows the same pattern as the other thin HTTP clients in
this package: a shared ``httpx.AsyncClient`` from
:mod:`zeenova_bot.http`, a 90-second cooldown after a 429 / 5xx, and a
TTL cache so repeated lookups of the same address are cheap.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
from cachetools import TTLCache

from .http import shared_async_client

logger = logging.getLogger(__name__)

# V2 base URL. Always pass ``chainid`` explicitly; we hard-code 1 here
# because the bot only surfaces Ethereum mainnet wallets for now. Other
# chains can be added by parameterising ``CHAIN_ID``.
BASE_URL = "https://api.etherscan.io/v2/api"
CHAIN_ID = 1

# Wei per ETH. ``1 ETH = 10**18 wei`` — the smallest unit on Ethereum.
WEI_PER_ETH = 10**18

# After a 429 / 5xx we cool down for this long before retrying. Same
# convention as :mod:`zeenova_bot.coingecko`.
_COOLDOWN_S: float = 90.0

# How long the per-address summary stays in the in-memory cache. Wallet
# data changes when a tx lands (~12 s on Ethereum), but we don't want
# repeated ``/wallet`` calls within seconds to hammer Etherscan either.
# 60 s strikes a balance — frequently-checked wallets stay snappy while
# fresh data is never older than a block confirmation or two.
_CACHE_TTL_S: float = 60.0

# Etherscan rejects anything that isn't a 0x-prefixed 40-hex-char string
# (case-insensitive). Validating client-side avoids a wasted round-trip
# and gives users a clearer error message than the upstream "invalid
# address format".
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def is_valid_address(address: str) -> bool:
    """True if ``address`` is a syntactically valid Ethereum address."""
    return bool(_ADDR_RE.match(address.strip()))


@dataclass(slots=True, frozen=True)
class WalletTx:
    """One row from Etherscan's ``account/txlist``."""

    hash: str
    timestamp: int  # unix seconds
    from_addr: str
    to_addr: str
    value_wei: int
    is_incoming: bool  # True when ``to_addr`` matches the queried wallet


@dataclass(slots=True, frozen=True)
class WalletInfo:
    """Aggregated, render-ready wallet summary."""

    address: str  # lowercased, 0x-prefixed
    balance_wei: int
    balance_eth: float
    txs_sent: int  # account nonce — total outgoing txs ever
    last_tx_at: int | None  # unix seconds; None for a wallet with no history
    recent: tuple[WalletTx, ...]


class EtherscanClient:
    """Minimal Etherscan V2 client tailored to the ``/wallet`` command.

    Construct without an api_key for tests / dry-runs and call
    :meth:`is_configured` to short-circuit at the handler level.
    """

    def __init__(self, api_key: str = "", timeout: float = 10.0) -> None:
        self._api_key = api_key.strip()
        self._client = shared_async_client(timeout=timeout)
        self._cache: TTLCache[str, WalletInfo] = TTLCache(
            maxsize=512, ttl=_CACHE_TTL_S
        )
        self._cooldown_until: float = 0.0

    def is_configured(self) -> bool:
        """True when an API key is set. ``/wallet`` short-circuits on
        ``False`` so users get a clear "configure ETHERSCAN_API_KEY"
        message instead of a generic upstream error."""
        return bool(self._api_key)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, params: dict[str, Any]) -> Any:
        """Issue one V2 request. Returns the decoded JSON or raises.

        Etherscan signals errors two ways:

        * Transport-level (timeout / 5xx) → ``httpx`` raises an
          :class:`httpx.HTTPError`. Callers catch it and return ``None``.
        * Application-level (rate-limited, bad key, no transactions) →
          HTTP 200 with ``{"status":"0","message":"…","result":"…"}``.
          We let callers inspect the body since "no transactions"
          (``message="No transactions found"``) is a valid outcome we
          must not treat as a hard error.
        """
        merged: dict[str, Any] = {"chainid": CHAIN_ID, **params}
        if self._api_key:
            merged["apikey"] = self._api_key
        resp = await self._client.get(BASE_URL, params=merged)
        if resp.status_code == 429:
            self._cooldown_until = time.time() + _COOLDOWN_S
            raise httpx.HTTPStatusError(
                "Etherscan rate limited",
                request=resp.request,
                response=resp,
            )
        if resp.status_code >= 500:
            self._cooldown_until = time.time() + _COOLDOWN_S
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"Etherscan HTTP {resp.status_code}",
                request=resp.request,
                response=resp,
            )
        return resp.json()

    async def _eth_balance(self, address: str) -> int:
        body = await self._get(
            {"module": "account", "action": "balance", "address": address, "tag": "latest"}
        )
        if not isinstance(body, dict):
            return 0
        if str(body.get("status")) != "1":
            return 0
        try:
            return int(body.get("result") or 0)
        except (TypeError, ValueError):
            return 0

    async def _tx_count(self, address: str) -> int:
        """Total outgoing tx count via ``eth_getTransactionCount``.

        Etherscan exposes the JSON-RPC nonce through its
        ``module=proxy`` shim. Nonce is the count of transactions the
        account has *sent*, which matches what a typical block-explorer
        UI calls "Txns Sent". It does NOT include inbound transfers or
        internal calls — we surface this as "Sent" rather than a
        misleading "Total".
        """
        body = await self._get(
            {
                "module": "proxy",
                "action": "eth_getTransactionCount",
                "address": address,
                "tag": "latest",
            }
        )
        if not isinstance(body, dict):
            return 0
        raw = body.get("result")
        if not isinstance(raw, str):
            return 0
        try:
            return int(raw, 16)
        except (TypeError, ValueError):
            return 0

    async def _recent_txs(
        self, address: str, *, limit: int = 5
    ) -> tuple[WalletTx, ...]:
        body = await self._get(
            {
                "module": "account",
                "action": "txlist",
                "address": address,
                "startblock": 0,
                "endblock": 99999999,
                "page": 1,
                "offset": max(1, limit),
                "sort": "desc",
            }
        )
        if not isinstance(body, dict):
            return ()
        # ``status=0`` with message "No transactions found" is a happy
        # path for fresh wallets — return ``()`` rather than raising.
        if str(body.get("status")) != "1":
            return ()
        rows = body.get("result")
        if not isinstance(rows, list):
            return ()
        out: list[WalletTx] = []
        wallet_lc = address.lower()
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                tx_hash = str(row["hash"])
                ts = int(row["timeStamp"])
                value = int(row["value"])
                from_addr = str(row["from"]).lower()
                to_addr = str(row["to"]).lower()
            except (KeyError, TypeError, ValueError):
                continue
            out.append(
                WalletTx(
                    hash=tx_hash,
                    timestamp=ts,
                    from_addr=from_addr,
                    to_addr=to_addr,
                    value_wei=value,
                    is_incoming=(to_addr == wallet_lc),
                )
            )
        return tuple(out)

    async def fetch_wallet(self, address: str) -> WalletInfo | None:
        """Aggregate the three calls into a single :class:`WalletInfo`.

        Returns ``None`` when the API key is missing, the address is
        malformed, we're inside the rate-limit cooldown window, or any
        of the three upstream calls raises a transport error.
        """
        if not self.is_configured():
            return None
        addr = address.strip().lower()
        if not is_valid_address(addr):
            return None
        if addr in self._cache:
            return self._cache[addr]
        if time.time() < self._cooldown_until:
            return None
        try:
            balance_wei = await self._eth_balance(addr)
            txs_sent = await self._tx_count(addr)
            recent = await self._recent_txs(addr)
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("etherscan lookup failed for %s: %s", addr, exc)
            return None
        last_tx_at = recent[0].timestamp if recent else None
        info = WalletInfo(
            address=addr,
            balance_wei=balance_wei,
            balance_eth=balance_wei / WEI_PER_ETH,
            txs_sent=txs_sent,
            last_tx_at=last_tx_at,
            recent=recent,
        )
        self._cache[addr] = info
        return info

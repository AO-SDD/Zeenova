"""Etherscan V2 API client used by ``/wallet`` and ``/gas``.

Covers the small handful of endpoints we need:

* Native balance (``account/balance``) — for the per-chain balance grid
  in :func:`fetch_wallet`.
* Account nonce (``proxy/eth_getTransactionCount``) — the "txs sent"
  counter.
* Recent transactions (``account/txlist``) — the last few rows on the
  most-active chain.
* Gas oracle (``gastracker/gasoracle``) — for the ``/gas`` command.

Everything is keyed by a single ``ETHERSCAN_API_KEY``; one free key
from etherscan.io now works for all 60+ chains via the V2 multichain
API (`docs.etherscan.io/v2-migration`). Each request sends
``chainid=X`` explicitly so swapping chains is a per-call concern.

The client follows the same pattern as the other thin HTTP clients in
this package: a shared ``httpx.AsyncClient`` from
:mod:`zeenova_bot.http`, a 90-second cooldown after a 429 / 5xx, and a
TTL cache so repeated lookups are cheap.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
from cachetools import TTLCache

from .http import shared_async_client

logger = logging.getLogger(__name__)

# V2 base URL. Always pass ``chainid`` explicitly per call.
BASE_URL = "https://api.etherscan.io/v2/api"

# Wei per native token. All supported chains use 18 decimals (EVM
# convention); kept as a constant to make the math obvious in renderers.
WEI_PER_UNIT = 10**18

# After a 429 / 5xx we cool down for this long before retrying. Same
# convention as :mod:`zeenova_bot.coingecko`.
_COOLDOWN_S: float = 90.0

# How long the per-address wallet summary stays in the in-memory cache.
# Native balances change when a tx lands (~12 s on Ethereum, faster on
# L2s) — 60 s keeps repeat clicks snappy without serving stale data.
_WALLET_CACHE_TTL_S: float = 60.0

# Gas prices move every block, but the user-visible difference between
# two adjacent block samples is usually <1 gwei. 20 s feels live without
# burning the per-chain quota.
_GAS_CACHE_TTL_S: float = 20.0

# Cap parallel chain fan-out below the free-tier 5 req/s ceiling so a
# single ``/wallet`` invocation never trips the rate limiter for the
# whole bot.
_CHAIN_FANOUT: int = 4

# Etherscan rejects anything that isn't a 0x-prefixed 40-hex-char string
# (case-insensitive). Validating client-side avoids a wasted round-trip
# and gives users a clearer error message than the upstream "invalid
# address format".
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def is_valid_address(address: str) -> bool:
    """True if ``address`` is a syntactically valid Ethereum address."""
    return bool(_ADDR_RE.match(address.strip()))


@dataclass(slots=True, frozen=True)
class Chain:
    """Metadata for one supported chain.

    ``price_symbol`` is the ticker we send to CoinPaprika to look up
    the native token's USD price. It's a separate field because some
    chains share a native token (Arbitrum, Optimism, Base all use ETH)
    and some chains' native tokens have an alias (Polygon's MATIC
    became POL in 2024 but most listings still surface as MATIC).
    """

    id: int
    name: str
    native_symbol: str
    price_symbol: str
    explorer_url: str  # base URL for ``/address/{addr}`` deep links


# Curated list of chains surfaced in the multichain wallet + /gas
# command. Order is also the rendering order, with the L1/L2 majors
# first and the long tail of EVM chains after. Every chain ID here
# must be supported by the Etherscan V2 multichain API — see
# https://docs.etherscan.io/etherscan-v2/getting-started/supported-chains
# for the canonical list.
CHAINS: tuple[Chain, ...] = (
    Chain(1, "Ethereum", "ETH", "ETH", "https://etherscan.io"),
    Chain(56, "BSC", "BNB", "BNB", "https://bscscan.com"),
    Chain(137, "Polygon", "POL", "MATIC", "https://polygonscan.com"),
    Chain(42161, "Arbitrum", "ETH", "ETH", "https://arbiscan.io"),
    Chain(10, "Optimism", "ETH", "ETH", "https://optimistic.etherscan.io"),
    Chain(8453, "Base", "ETH", "ETH", "https://basescan.org"),
    Chain(43114, "Avalanche", "AVAX", "AVAX", "https://snowtrace.io"),
    Chain(59144, "Linea", "ETH", "ETH", "https://lineascan.build"),
    Chain(81457, "Blast", "ETH", "ETH", "https://blastscan.io"),
    Chain(5000, "Mantle", "MNT", "MNT", "https://mantlescan.xyz"),
    Chain(146, "Sonic", "S", "S", "https://sonicscan.org"),
    Chain(130, "Unichain", "ETH", "ETH", "https://uniscan.xyz"),
    Chain(80094, "Berachain", "BERA", "BERA", "https://berascan.com"),
    Chain(100, "Gnosis", "xDAI", "DAI", "https://gnosisscan.io"),
    Chain(42220, "Celo", "CELO", "CELO", "https://celoscan.io"),
    Chain(1329, "Sei", "SEI", "SEI", "https://seitrace.com"),
    Chain(1284, "Moonbeam", "GLMR", "GLMR", "https://moonscan.io"),
    Chain(999, "HyperEVM", "HYPE", "HYPE", "https://hyperevmscan.io"),
    Chain(2741, "Abstract", "ETH", "ETH", "https://abscan.org"),
    Chain(9745, "Plasma", "XPL", "XPL", "https://plasmascan.to"),
)


def _chain_by_id(chain_id: int) -> Chain | None:
    for c in CHAINS:
        if c.id == chain_id:
            return c
    return None


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
class ChainBalance:
    """Per-chain native balance + tx counters.

    Always present for every chain in :data:`CHAINS`; balances of zero
    are rendered too so users can see "where I'm not active" at a
    glance.
    """

    chain: Chain
    balance_wei: int
    balance: float  # human-friendly amount in native units
    txs_sent: int
    last_tx_at: int | None


@dataclass(slots=True, frozen=True)
class WalletInfo:
    """Aggregated, render-ready multichain wallet summary."""

    address: str  # lowercased, 0x-prefixed
    balances: tuple[ChainBalance, ...]
    # Recent transactions are only fetched on the most-active chain to
    # keep the API budget reasonable. The picked chain is exposed so
    # the renderer can label the section.
    recent_chain: Chain | None
    recent: tuple[WalletTx, ...]
    # Unix timestamp of the wallet's first outgoing transaction on the
    # most-active chain. ``None`` for brand-new / never-active wallets
    # (or when the lookup was rate-limited).
    first_tx_at: int | None = None

    # Convenience accessors used by both renderers and tests so the
    # "primary" (Ethereum) balance line stays trivial to read.

    @property
    def primary(self) -> ChainBalance | None:
        for cb in self.balances:
            if cb.chain.id == 1:
                return cb
        return None

    @property
    def balance_eth(self) -> float:
        p = self.primary
        return p.balance if p is not None else 0.0

    @property
    def balance_wei(self) -> int:
        p = self.primary
        return p.balance_wei if p is not None else 0

    @property
    def txs_sent(self) -> int:
        p = self.primary
        return p.txs_sent if p is not None else 0

    @property
    def last_tx_at(self) -> int | None:
        p = self.primary
        return p.last_tx_at if p is not None else None


@dataclass(slots=True, frozen=True)
class GasTier:
    """One row from the gas oracle response."""

    safe_gwei: float
    standard_gwei: float
    fast_gwei: float


@dataclass(slots=True, frozen=True)
class ChainGas:
    """Gas oracle reading for a single chain."""

    chain: Chain
    tier: GasTier


class EtherscanClient:
    """Multichain Etherscan V2 client.

    Construct without an api_key for tests / dry-runs and call
    :meth:`is_configured` to short-circuit at the handler level.
    """

    def __init__(self, api_key: str = "", timeout: float = 10.0) -> None:
        self._api_key = api_key.strip()
        self._client = shared_async_client(timeout=timeout)
        self._wallet_cache: TTLCache[str, WalletInfo] = TTLCache(
            maxsize=512, ttl=_WALLET_CACHE_TTL_S
        )
        # Keyed by chain id so callers can fetch a single chain's gas
        # without dragging the rest along.
        self._gas_cache: TTLCache[int, ChainGas] = TTLCache(
            maxsize=64, ttl=_GAS_CACHE_TTL_S
        )
        self._cooldown_until: float = 0.0
        self._fanout = asyncio.Semaphore(_CHAIN_FANOUT)

    def is_configured(self) -> bool:
        """True when an API key is set. ``/wallet`` and ``/gas``
        short-circuit on ``False`` so users get a clear "configure
        ETHERSCAN_API_KEY" message instead of a generic upstream error.
        """
        return bool(self._api_key)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, chain_id: int, params: dict[str, Any]) -> Any:
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
        merged: dict[str, Any] = {"chainid": chain_id, **params}
        if self._api_key:
            merged["apikey"] = self._api_key
        async with self._fanout:
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

    async def _balance(self, chain_id: int, address: str) -> int:
        body = await self._get(
            chain_id,
            {
                "module": "account",
                "action": "balance",
                "address": address,
                "tag": "latest",
            },
        )
        if not isinstance(body, dict):
            return 0
        if str(body.get("status")) != "1":
            return 0
        try:
            return int(body.get("result") or 0)
        except (TypeError, ValueError):
            return 0

    async def _tx_count(self, chain_id: int, address: str) -> int:
        """Total outgoing tx count via ``eth_getTransactionCount``.

        Etherscan exposes the JSON-RPC nonce through its
        ``module=proxy`` shim. Nonce is the count of transactions the
        account has *sent*, which matches what a typical block-explorer
        UI calls "Txns Sent". It does NOT include inbound transfers or
        internal calls — we surface this as "Sent" rather than a
        misleading "Total".
        """
        body = await self._get(
            chain_id,
            {
                "module": "proxy",
                "action": "eth_getTransactionCount",
                "address": address,
                "tag": "latest",
            },
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

    async def _first_tx_at(self, chain_id: int, address: str) -> int | None:
        """Unix timestamp of the wallet's first outgoing tx on this
        chain, or ``None`` if it has none / the call fails.

        Same ``txlist`` endpoint as :meth:`_recent_txs` but sorted
        ascending with ``offset=1`` so we only pay for one row. This
        is what powers the "Active since" line in the wallet card.
        """
        try:
            body = await self._get(
                chain_id,
                {
                    "module": "account",
                    "action": "txlist",
                    "address": address,
                    "startblock": 0,
                    "endblock": 99999999,
                    "page": 1,
                    "offset": 1,
                    "sort": "asc",
                },
            )
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(body, dict) or str(body.get("status")) != "1":
            return None
        rows = body.get("result")
        if not isinstance(rows, list) or not rows:
            return None
        first = rows[0]
        if not isinstance(first, dict):
            return None
        try:
            return int(first["timeStamp"])
        except (KeyError, TypeError, ValueError):
            return None

    async def _recent_txs(
        self, chain_id: int, address: str, *, limit: int = 5
    ) -> tuple[WalletTx, ...]:
        body = await self._get(
            chain_id,
            {
                "module": "account",
                "action": "txlist",
                "address": address,
                "startblock": 0,
                "endblock": 99999999,
                "page": 1,
                "offset": max(1, limit),
                "sort": "desc",
            },
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

    async def _chain_balance(
        self, chain: Chain, address: str
    ) -> ChainBalance | None:
        """One row of :class:`ChainBalance` — balance plus the cheap
        ``txs_sent`` counter that lets us pick the "most active" chain
        without a third call per row.
        """
        try:
            balance_wei = await self._balance(chain.id, address)
            txs_sent = await self._tx_count(chain.id, address)
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug(
                "etherscan: chain %d (%s) failed: %s", chain.id, chain.name, exc
            )
            return None
        return ChainBalance(
            chain=chain,
            balance_wei=balance_wei,
            balance=balance_wei / WEI_PER_UNIT,
            txs_sent=txs_sent,
            last_tx_at=None,
        )

    async def fetch_wallet(self, address: str) -> WalletInfo | None:
        """Fan out balance + nonce queries across every supported chain.

        Recent transactions are only pulled for the chain with the
        highest outgoing-tx count (proxy for "most-used") to keep the
        per-call API budget around 2 × N + 1 instead of 3 × N.

        Returns ``None`` when the API key is missing, the address is
        malformed, we're inside the rate-limit cooldown window, or
        *every* chain call raises (i.e. complete network failure).
        Partial failures (some chains down, others ok) still return a
        populated :class:`WalletInfo` — the failed chains are simply
        omitted from the balance grid.
        """
        if not self.is_configured():
            return None
        addr = address.strip().lower()
        if not is_valid_address(addr):
            return None
        if addr in self._wallet_cache:
            return self._wallet_cache[addr]
        if time.time() < self._cooldown_until:
            return None
        results = await asyncio.gather(
            *(self._chain_balance(c, addr) for c in CHAINS),
            return_exceptions=False,
        )
        balances: tuple[ChainBalance, ...] = tuple(
            cb for cb in results if cb is not None
        )
        if not balances:
            return None
        # Pick the chain with the highest nonce as the "primary" for the
        # tx-list query. Falls back to Ethereum if every chain has a
        # zero nonce (brand new wallet) so the user still sees the
        # familiar "no transactions yet" footer rather than nothing.
        active = max(balances, key=lambda cb: cb.txs_sent)
        recent_chain: Chain | None = None
        recent: tuple[WalletTx, ...] = ()
        first_tx_at: int | None = None
        if active.txs_sent > 0:
            try:
                # One round-trip each: latest 5 + earliest 1. Running
                # them concurrently keeps the extra call's latency
                # invisible — the gather waits on the slower of the two.
                recent, first_tx_at = await asyncio.gather(
                    self._recent_txs(active.chain.id, addr),
                    self._first_tx_at(active.chain.id, addr),
                )
                recent_chain = active.chain
            except (httpx.HTTPError, ValueError) as exc:
                logger.debug("etherscan: recent txs failed: %s", exc)
        # Patch ``last_tx_at`` on the primary-chain row so the header
        # "Activity" block can read it straight off the model.
        if recent_chain is not None and recent:
            patched: list[ChainBalance] = []
            for cb in balances:
                if cb.chain.id == recent_chain.id:
                    patched.append(
                        ChainBalance(
                            chain=cb.chain,
                            balance_wei=cb.balance_wei,
                            balance=cb.balance,
                            txs_sent=cb.txs_sent,
                            last_tx_at=recent[0].timestamp,
                        )
                    )
                else:
                    patched.append(cb)
            balances = tuple(patched)
        info = WalletInfo(
            address=addr,
            balances=balances,
            recent_chain=recent_chain,
            recent=recent,
            first_tx_at=first_tx_at,
        )
        self._wallet_cache[addr] = info
        return info

    async def _gas_from_oracle(self, chain: Chain) -> GasTier | None:
        """Try the gastracker oracle. Returns ``None`` when the chain
        doesn't expose one or the oracle is offline.
        """
        try:
            body = await self._get(
                chain.id,
                {"module": "gastracker", "action": "gasoracle"},
            )
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug(
                "etherscan: gas oracle failed for chain %d: %s", chain.id, exc
            )
            return None
        if not isinstance(body, dict) or str(body.get("status")) != "1":
            return None
        result = body.get("result")
        if not isinstance(result, dict):
            return None
        try:
            tier = GasTier(
                safe_gwei=float(result.get("SafeGasPrice") or 0),
                standard_gwei=float(result.get("ProposeGasPrice") or 0),
                fast_gwei=float(result.get("FastGasPrice") or 0),
            )
        except (TypeError, ValueError):
            return None
        if tier.safe_gwei <= 0 and tier.standard_gwei <= 0 and tier.fast_gwei <= 0:
            return None
        return tier

    async def _gas_from_eth_gas_price(self, chain: Chain) -> GasTier | None:
        """Fallback for chains without a gastracker oracle.

        ``proxy/eth_gasPrice`` is exposed on every EVM chain Etherscan
        V2 covers. It returns a single "current" gas price; we synthesise
        Safe/Standard/Fast tiers as multiples around it so the card stays
        consistent with oracle-backed chains.
        """
        try:
            body = await self._get(
                chain.id,
                {"module": "proxy", "action": "eth_gasPrice"},
            )
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug(
                "etherscan: eth_gasPrice failed for chain %d: %s", chain.id, exc
            )
            return None
        if not isinstance(body, dict):
            return None
        raw = body.get("result")
        if not isinstance(raw, str):
            return None
        try:
            wei = int(raw, 16)
        except (TypeError, ValueError):
            return None
        if wei <= 0:
            return None
        # 1 gwei == 1e9 wei. eth_gasPrice is the network's current
        # baseline; render 0.9x/1.0x/1.15x as Safe/Standard/Fast — that
        # roughly mirrors the spread real gas oracles report on L2s.
        base_gwei = wei / 1e9
        return GasTier(
            safe_gwei=base_gwei * 0.9,
            standard_gwei=base_gwei,
            fast_gwei=base_gwei * 1.15,
        )

    async def _fetch_gas_one(self, chain: Chain) -> ChainGas | None:
        if chain.id in self._gas_cache:
            return self._gas_cache[chain.id]
        tier = await self._gas_from_oracle(chain)
        if tier is None:
            tier = await self._gas_from_eth_gas_price(chain)
        if tier is None:
            return None
        snap = ChainGas(chain=chain, tier=tier)
        self._gas_cache[chain.id] = snap
        return snap

    async def fetch_all_gas(self) -> tuple[ChainGas, ...]:
        """Fan out gas-oracle queries across every supported chain.

        Returns rows in :data:`CHAINS` order. Chains where the oracle
        is unavailable are simply dropped (we never raise — partial
        success is the rule on L2s).
        """
        if not self.is_configured():
            return ()
        if time.time() < self._cooldown_until:
            # Surface whatever we already cached so a transient 429
            # doesn't blank the entire ``/gas`` card.
            return tuple(self._gas_cache.values())
        results = await asyncio.gather(
            *(self._fetch_gas_one(c) for c in CHAINS),
            return_exceptions=False,
        )
        return tuple(g for g in results if g is not None)


# Re-export so callers can import the supported chain list directly
# without crossing the ``Chain`` boundary.
__all__ = [
    "CHAINS",
    "BASE_URL",
    "WEI_PER_UNIT",
    "Chain",
    "ChainBalance",
    "ChainGas",
    "EtherscanClient",
    "GasTier",
    "WalletInfo",
    "WalletTx",
    "is_valid_address",
]

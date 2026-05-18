"""Solana JSON-RPC client used by the non-EVM branch of ``/wallet``.

Solana isn't an Etherscan-family chain, so it sits on its own
integration. Addresses are base58 (32-44 chars, no ``0x`` prefix) and
the network speaks JSON-RPC at ``api.mainnet-beta.solana.com``. We
keep the surface tiny — just what ``/wallet`` actually needs:

* :func:`is_valid_solana_address` — base58 syntactic check.
* :class:`SolanaClient.fetch_wallet` — native SOL balance plus the
  five most recent signatures (timestamp + success/fail status).
  Solana doesn't expose a cheap "total tx count" so the renderer
  reports last-seen + recent signatures instead.

The public RPC has loose, undocumented rate limits. Operators who
need higher throughput can point the bot at a paid endpoint (Helius,
QuickNode, Triton, etc.) by setting ``SOLANA_RPC_URL`` in the
environment. The default value works out of the box.
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

DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"

# Solana represents SOL in lamports (1 SOL = 10^9 lamports). Kept as a
# constant so renderers don't sprinkle magic numbers.
LAMPORTS_PER_SOL = 1_000_000_000

# Same 90-second cooldown convention as the EVM client; on 429 / 5xx
# the next call short-circuits instead of hammering the upstream.
_COOLDOWN_S: float = 90.0

# Per-address cache TTL. SOL transactions land in ~400 ms slots, but
# the user-visible balance only matters at the second level — 60 s
# matches the EVM client and keeps repeated clicks snappy.
_WALLET_CACHE_TTL_S: float = 60.0

# Base58 alphabet (Bitcoin variant — Solana uses the same one). 0, O,
# I and l are excluded. Pubkeys decode to 32 bytes, which in base58
# fall in the 32-44 char range. This is a syntactic pre-filter — the
# canonical "is this a real account?" check is the RPC's job.
_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def is_valid_solana_address(address: str) -> bool:
    """True if ``address`` is syntactically a Solana public key.

    Conservative: requires the base58 alphabet and a 32-44 char
    length. The exact 32-byte decode is left to the RPC so that a
    valid-looking-but-non-existent address still surfaces a clean
    upstream error to the user.
    """
    return bool(_BASE58_RE.match(address.strip()))


@dataclass(slots=True, frozen=True)
class SolanaTx:
    """One row from ``getSignaturesForAddress``."""

    signature: str
    timestamp: int  # unix seconds; 0 when ``blockTime`` is null
    is_failed: bool  # True when the transaction errored on-chain
    fee_lamports: int  # 0 when the RPC doesn't return it


@dataclass(slots=True, frozen=True)
class SolanaWalletInfo:
    """Aggregated render-ready Solana wallet summary."""

    address: str  # exact base58 the user supplied (case-sensitive)
    balance_lamports: int
    balance_sol: float  # human-friendly amount, lamports / 10^9
    # Most recent first. Empty tuple when the account has never
    # signed anything (genuinely new wallet) or when the RPC returned
    # a transport-level error for the signatures call.
    recent: tuple[SolanaTx, ...]
    last_tx_at: int | None  # unix seconds; ``None`` for fresh wallets


class SolanaClient:
    """Thin Solana JSON-RPC client.

    Construct with the default ``DEFAULT_RPC_URL`` for the public
    mainnet endpoint, or pass ``rpc_url`` to use a paid provider.
    The client never sends an API key in headers — providers that
    require one expose the secret in the URL itself (e.g.
    ``https://mainnet.helius-rpc.com/?api-key=…``), which the
    operator just drops into ``SOLANA_RPC_URL``.
    """

    def __init__(
        self,
        rpc_url: str = DEFAULT_RPC_URL,
        timeout: float = 10.0,
    ) -> None:
        self._rpc_url = rpc_url.strip() or DEFAULT_RPC_URL
        self._client = shared_async_client(timeout=timeout)
        self._wallet_cache: TTLCache[str, SolanaWalletInfo] = TTLCache(
            maxsize=256, ttl=_WALLET_CACHE_TTL_S
        )
        self._cooldown_until: float = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        """Issue one JSON-RPC call. Returns the decoded ``result``
        field or raises :class:`httpx.HTTPError` / :class:`ValueError`.

        Application-level errors (``{"error": …}``) raise
        :class:`ValueError` so callers can decide whether to swallow
        them (e.g. "account not found" is a clean ``None`` upstream).
        """
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        resp = await self._client.post(self._rpc_url, json=payload)
        if resp.status_code == 429:
            self._cooldown_until = time.time() + _COOLDOWN_S
            raise httpx.HTTPStatusError(
                "Solana RPC rate limited",
                request=resp.request,
                response=resp,
            )
        if resp.status_code >= 500:
            self._cooldown_until = time.time() + _COOLDOWN_S
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"Solana RPC HTTP {resp.status_code}",
                request=resp.request,
                response=resp,
            )
        body = resp.json()
        if not isinstance(body, dict):
            raise ValueError("Solana RPC returned a non-object body")
        if "error" in body and body["error"] is not None:
            err = body["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise ValueError(f"Solana RPC error: {msg}")
        return body.get("result")

    async def _balance(self, address: str) -> int:
        """Return native lamport balance for ``address``."""
        result = await self._rpc("getBalance", [address])
        if isinstance(result, dict):
            value = result.get("value")
            if isinstance(value, int):
                return value
        if isinstance(result, int):
            return result
        return 0

    async def _signatures(
        self, address: str, *, limit: int = 5
    ) -> tuple[SolanaTx, ...]:
        """Last ``limit`` signatures for ``address``, most recent first."""
        result = await self._rpc(
            "getSignaturesForAddress",
            [address, {"limit": max(1, min(limit, 1000))}],
        )
        if not isinstance(result, list):
            return ()
        out: list[SolanaTx] = []
        for entry in result:
            if not isinstance(entry, dict):
                continue
            sig = entry.get("signature")
            if not isinstance(sig, str):
                continue
            block_time = entry.get("blockTime")
            ts = int(block_time) if isinstance(block_time, int) else 0
            err = entry.get("err")
            fee_raw = entry.get("fee")
            fee = int(fee_raw) if isinstance(fee_raw, int) else 0
            out.append(
                SolanaTx(
                    signature=sig,
                    timestamp=ts,
                    is_failed=err is not None,
                    fee_lamports=fee,
                )
            )
        return tuple(out)

    async def fetch_wallet(self, address: str) -> SolanaWalletInfo | None:
        """Return a :class:`SolanaWalletInfo` for ``address`` or ``None``.

        ``None`` is returned when the address is malformed, we're
        inside the rate-limit cooldown window, or every upstream call
        raises. Account-doesn't-exist (balance 0, no signatures) is a
        valid result — those wallets render with a zero balance and
        no recent activity.
        """
        addr = address.strip()
        if not is_valid_solana_address(addr):
            return None
        if addr in self._wallet_cache:
            return self._wallet_cache[addr]
        if time.time() < self._cooldown_until:
            return None
        try:
            lamports = await self._balance(addr)
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("solana: balance failed: %s", exc)
            return None
        recent: tuple[SolanaTx, ...] = ()
        try:
            recent = await self._signatures(addr)
        except (httpx.HTTPError, ValueError) as exc:
            # Signatures are best-effort — the balance line still
            # makes the card useful.
            logger.debug("solana: signatures failed: %s", exc)
        last_tx_at = recent[0].timestamp if recent and recent[0].timestamp else None
        info = SolanaWalletInfo(
            address=addr,
            balance_lamports=lamports,
            balance_sol=lamports / LAMPORTS_PER_SOL,
            recent=recent,
            last_tx_at=last_tx_at,
        )
        self._wallet_cache[addr] = info
        return info


__all__ = [
    "DEFAULT_RPC_URL",
    "LAMPORTS_PER_SOL",
    "SolanaClient",
    "SolanaTx",
    "SolanaWalletInfo",
    "is_valid_solana_address",
]

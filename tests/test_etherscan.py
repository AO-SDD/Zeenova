"""Tests for the Etherscan V2 multichain client used by ``/wallet`` and ``/gas``."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from zeenova_bot.etherscan import (
    CHAINS,
    EtherscanClient,
    is_valid_address,
)

VITALIK = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"

# Number of supported chains. Every multichain fan-out scales by this.
N_CHAINS = len(CHAINS)


def test_is_valid_address_accepts_canonical_form() -> None:
    assert is_valid_address(VITALIK)
    assert is_valid_address(VITALIK.lower())
    assert is_valid_address(VITALIK.upper().replace("X", "x"))


def test_is_valid_address_rejects_garbage() -> None:
    assert not is_valid_address("")
    assert not is_valid_address("0x")
    assert not is_valid_address("0x123")  # too short
    assert not is_valid_address(VITALIK + "00")  # too long
    assert not is_valid_address("d8dA6BF26964aF9D7eEd9e03E53415D37aA96045")  # no 0x
    assert not is_valid_address("0xZZZZ6BF26964aF9D7eEd9e03E53415D37aA96045")  # non-hex


def test_is_configured_reflects_api_key() -> None:
    assert EtherscanClient(api_key="").is_configured() is False
    assert EtherscanClient(api_key=" ").is_configured() is False
    assert EtherscanClient(api_key="abc123").is_configured() is True


@pytest.mark.asyncio
async def test_fetch_wallet_returns_none_without_api_key() -> None:
    client = EtherscanClient(api_key="")
    try:
        assert await client.fetch_wallet(VITALIK) is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_fetch_wallet_returns_none_for_malformed_address() -> None:
    client = EtherscanClient(api_key="dummy")
    try:
        assert await client.fetch_wallet("not-an-address") is None
    finally:
        await client.aclose()


def _route_handler(
    *,
    balance_by_chain: dict[int, str] | None = None,
    nonce_by_chain: dict[int, str] | None = None,
    txlist_response: dict[str, Any] | None = None,
    gas_response: dict[str, Any] | None = None,
) -> Any:
    """Build a MockTransport handler routing on ``action`` + ``chainid``.

    Per-chain balance and nonce maps fall back to "zero balance, zero
    nonce" when a chain id is absent so callers can opt-in to non-zero
    rows just for the chains they care about.
    """
    balances = balance_by_chain or {}
    nonces = nonce_by_chain or {}

    def handler(request: httpx.Request) -> httpx.Response:
        parsed = urlparse(str(request.url))
        params = parse_qs(parsed.query)
        action = (params.get("action") or [""])[0]
        chain_id = int((params.get("chainid") or ["1"])[0])
        if action == "balance":
            return httpx.Response(
                200,
                json={
                    "status": "1",
                    "message": "OK",
                    "result": balances.get(chain_id, "0"),
                },
            )
        if action == "eth_getTransactionCount":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": nonces.get(chain_id, "0x0"),
                },
            )
        if action == "txlist":
            if txlist_response is None:
                return httpx.Response(
                    200,
                    json={
                        "status": "0",
                        "message": "No transactions found",
                        "result": [],
                    },
                )
            return httpx.Response(200, json=txlist_response)
        if action == "gasoracle":
            if gas_response is None:
                return httpx.Response(
                    200, json={"status": "0", "message": "unavailable", "result": ""}
                )
            return httpx.Response(200, json=gas_response)
        return httpx.Response(
            404, json={"status": "0", "message": "unknown action"}
        )

    return handler


@pytest.mark.asyncio
async def test_fetch_wallet_aggregates_balances_and_picks_active_chain() -> None:
    """Happy path: ETH has a real balance + nonce + tx list; other
    chains return zero. The wallet card surfaces the ETH balance row
    and the recent-txs section is keyed off Ethereum."""
    handler = _route_handler(
        balance_by_chain={
            1: "3450000000000000000",  # 3.45 ETH on Ethereum
            56: "1500000000000000000",  # 1.5 BNB on BSC
        },
        nonce_by_chain={1: "0x4d2"},  # 1234 sent from Ethereum
        txlist_response={
            "status": "1",
            "message": "OK",
            "result": [
                {
                    "hash": "0xaaa",
                    "timeStamp": "1730000000",
                    "from": VITALIK.lower(),
                    "to": "0x" + "1" * 40,
                    "value": "500000000000000000",
                },
                {
                    "hash": "0xbbb",
                    "timeStamp": "1729999000",
                    "from": "0x" + "2" * 40,
                    "to": VITALIK.lower(),
                    "value": "1200000000000000000",
                },
            ],
        },
    )
    client = EtherscanClient(api_key="dummy")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        info = await client.fetch_wallet(VITALIK)
    finally:
        await client.aclose()
    assert info is not None
    assert info.address == VITALIK.lower()
    # Convenience accessors still work and surface the Ethereum row.
    assert info.balance_wei == 3_450_000_000_000_000_000
    assert info.balance_eth == pytest.approx(3.45)
    assert info.txs_sent == 1234
    # The recent-txs section is fetched once on the most-active chain.
    assert info.recent_chain is not None
    assert info.recent_chain.id == 1
    assert info.last_tx_at == 1_730_000_000
    assert len(info.recent) == 2
    outgoing, incoming = info.recent
    assert outgoing.is_incoming is False
    assert outgoing.value_wei == 500_000_000_000_000_000
    assert incoming.is_incoming is True
    assert incoming.value_wei == 1_200_000_000_000_000_000
    # Every chain returned a row (even those with zero balance).
    chain_ids = {cb.chain.id for cb in info.balances}
    assert chain_ids == {c.id for c in CHAINS}
    # BSC row carries the non-zero balance we wired in.
    bsc = next(cb for cb in info.balances if cb.chain.id == 56)
    assert bsc.balance_wei == 1_500_000_000_000_000_000


@pytest.mark.asyncio
async def test_fetch_wallet_handles_no_transactions_on_any_chain() -> None:
    """Fresh wallet: every chain returns zero balance and zero nonce.
    We still produce a populated :class:`WalletInfo` (with all chain
    rows at zero) so the renderer can say "no balance" instead of
    crashing."""
    handler = _route_handler()
    client = EtherscanClient(api_key="dummy")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        info = await client.fetch_wallet(VITALIK)
    finally:
        await client.aclose()
    assert info is not None
    assert info.balance_wei == 0
    assert info.txs_sent == 0
    assert info.last_tx_at is None
    assert info.recent == ()
    # Zero-nonce on every chain → we skip the tx-list call entirely.
    assert info.recent_chain is None
    assert len(info.balances) == N_CHAINS


@pytest.mark.asyncio
async def test_fetch_wallet_caches_per_address() -> None:
    """Two consecutive lookups for the same address only hit the API
    once. The cache TTL is 60 s so the second call returns the cached
    record without any HTTP I/O."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        parsed = urlparse(str(request.url))
        params = parse_qs(parsed.query)
        action = (params.get("action") or [""])[0]
        calls.append(action)
        if action == "balance":
            return httpx.Response(
                200, json={"status": "1", "message": "OK", "result": "100"}
            )
        if action == "eth_getTransactionCount":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "result": "0x1"}
            )
        return httpx.Response(
            200, json={"status": "0", "message": "No transactions found", "result": []}
        )

    client = EtherscanClient(api_key="dummy")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        first = await client.fetch_wallet(VITALIK)
        second = await client.fetch_wallet(VITALIK)
    finally:
        await client.aclose()
    assert first is second
    # First call: 2 × N (balance + nonce per chain) + 1 (txlist on the
    # single most-active chain). Second call hits the cache → no
    # extra requests.
    assert len(calls) == 2 * N_CHAINS + 1


@pytest.mark.asyncio
async def test_fetch_wallet_returns_none_on_total_transport_error() -> None:
    """When *every* chain call raises we surface ``None`` so the
    handler renders a generic "try again" message."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    client = EtherscanClient(api_key="dummy")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await client.fetch_wallet(VITALIK) is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_request_includes_chainid_and_apikey() -> None:
    """Every request must carry ``chainid`` and the api key alongside
    the per-call params. We assert this on each captured request."""
    seen: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        parsed = urlparse(str(request.url))
        params = parse_qs(parsed.query)
        seen.append(params)
        action = (params.get("action") or [""])[0]
        if action == "balance":
            return httpx.Response(
                200, json={"status": "1", "message": "OK", "result": "0"}
            )
        if action == "eth_getTransactionCount":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "result": "0x0"}
            )
        return httpx.Response(
            200, json={"status": "0", "message": "No transactions found", "result": []}
        )

    client = EtherscanClient(api_key="my-secret-key")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await client.fetch_wallet(VITALIK)
    finally:
        await client.aclose()
    # 2 requests per chain (balance + nonce) — every chain has zero
    # nonce so the txlist call is skipped.
    assert len(seen) == 2 * N_CHAINS
    chain_ids_seen: set[str] = set()
    for params in seen:
        assert "chainid" in params
        chain_ids_seen.add(params["chainid"][0])
        assert params["apikey"] == ["my-secret-key"]
    assert chain_ids_seen == {str(c.id) for c in CHAINS}


@pytest.mark.asyncio
async def test_fetch_all_gas_returns_rows_per_chain() -> None:
    """``/gas`` happy path: every chain returns a gasoracle envelope
    and we surface one :class:`ChainGas` per chain."""
    gas_response = {
        "status": "1",
        "message": "OK",
        "result": {
            "LastBlock": "21000000",
            "SafeGasPrice": "18",
            "ProposeGasPrice": "22",
            "FastGasPrice": "28",
            "suggestBaseFee": "17.5",
            "gasUsedRatio": "0.5",
        },
    }
    handler = _route_handler(gas_response=gas_response)
    client = EtherscanClient(api_key="dummy")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        snaps = await client.fetch_all_gas()
    finally:
        await client.aclose()
    assert len(snaps) == N_CHAINS
    # Rendering order matches CHAINS order; Ethereum first.
    assert snaps[0].chain.id == 1
    assert snaps[0].tier.safe_gwei == pytest.approx(18.0)
    assert snaps[0].tier.standard_gwei == pytest.approx(22.0)
    assert snaps[0].tier.fast_gwei == pytest.approx(28.0)


@pytest.mark.asyncio
async def test_fetch_all_gas_returns_empty_without_key() -> None:
    """No API key means no API call. /gas short-circuits via
    ``is_configured()`` at the handler level."""
    client = EtherscanClient(api_key="")
    try:
        assert await client.fetch_all_gas() == ()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_fetch_all_gas_drops_unavailable_chains() -> None:
    """Chains where the gas oracle is offline (status=0 / all-zero
    tiers) are silently dropped — we never render "0 gwei" rows."""
    # Etherscan returns status=0 for chains without a gas oracle.
    handler = _route_handler()
    client = EtherscanClient(api_key="dummy")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        snaps = await client.fetch_all_gas()
    finally:
        await client.aclose()
    assert snaps == ()

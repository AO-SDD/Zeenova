"""Tests for the Etherscan V2 client used by ``/wallet``."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from zeenova_bot.etherscan import (
    EtherscanClient,
    is_valid_address,
)

VITALIK = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"


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


def _route_by_action(
    responses: dict[str, httpx.Response],
) -> Any:
    """Build a MockTransport handler that dispatches on the ``action`` query
    parameter so each test can wire its own balance / proxy / txlist
    response without needing a giant if/else."""

    def handler(request: httpx.Request) -> httpx.Response:
        parsed = urlparse(str(request.url))
        params = parse_qs(parsed.query)
        action = (params.get("action") or [""])[0]
        if action not in responses:
            return httpx.Response(404, json={"status": "0", "message": "unknown action"})
        return responses[action]

    return handler


@pytest.mark.asyncio
async def test_fetch_wallet_aggregates_balance_nonce_and_txs() -> None:
    """Happy path: a wallet with both incoming and outgoing transactions."""
    responses = {
        "balance": httpx.Response(
            200,
            json={"status": "1", "message": "OK", "result": "3450000000000000000"},
        ),
        "eth_getTransactionCount": httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": "0x4d2"},  # 1234 in hex
        ),
        "txlist": httpx.Response(
            200,
            json={
                "status": "1",
                "message": "OK",
                "result": [
                    {
                        "hash": "0xaaa",
                        "timeStamp": "1730000000",
                        "from": VITALIK.lower(),
                        "to": "0x" + "1" * 40,
                        "value": "500000000000000000",  # 0.5 ETH outgoing
                    },
                    {
                        "hash": "0xbbb",
                        "timeStamp": "1729999000",
                        "from": "0x" + "2" * 40,
                        "to": VITALIK.lower(),
                        "value": "1200000000000000000",  # 1.2 ETH incoming
                    },
                ],
            },
        ),
    }
    client = EtherscanClient(api_key="dummy")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(_route_by_action(responses)))
    try:
        info = await client.fetch_wallet(VITALIK)
    finally:
        await client.aclose()
    assert info is not None
    assert info.address == VITALIK.lower()
    assert info.balance_wei == 3_450_000_000_000_000_000
    assert info.balance_eth == pytest.approx(3.45)
    assert info.txs_sent == 1234
    assert info.last_tx_at == 1_730_000_000
    assert len(info.recent) == 2
    outgoing, incoming = info.recent
    assert outgoing.is_incoming is False
    assert outgoing.value_wei == 500_000_000_000_000_000
    assert incoming.is_incoming is True
    assert incoming.value_wei == 1_200_000_000_000_000_000


@pytest.mark.asyncio
async def test_fetch_wallet_handles_no_transactions() -> None:
    """Fresh wallet: balance + nonce succeed, txlist returns the upstream
    "No transactions found" envelope. We must surface ``recent=()`` and
    ``last_tx_at=None`` rather than treating the empty result as an
    error."""
    responses = {
        "balance": httpx.Response(
            200, json={"status": "1", "message": "OK", "result": "0"}
        ),
        "eth_getTransactionCount": httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": "0x0"}
        ),
        "txlist": httpx.Response(
            200,
            json={"status": "0", "message": "No transactions found", "result": []},
        ),
    }
    client = EtherscanClient(api_key="dummy")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(_route_by_action(responses)))
    try:
        info = await client.fetch_wallet(VITALIK)
    finally:
        await client.aclose()
    assert info is not None
    assert info.balance_wei == 0
    assert info.balance_eth == 0.0
    assert info.txs_sent == 0
    assert info.last_tx_at is None
    assert info.recent == ()


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
    # 3 requests for the first call, none for the second.
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_fetch_wallet_returns_none_on_transport_error() -> None:
    """Any ``httpx.HTTPError`` raised by the upstream short-circuits
    the aggregator — the handler renders a generic "try again" message
    rather than crashing the chat."""

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
    """The V2 API rejects requests without an explicit ``chainid``. We
    must always pass ``chainid=1`` (Ethereum mainnet) and the api key
    alongside the user-supplied params."""
    seen: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        parsed = urlparse(str(request.url))
        seen.append(parse_qs(parsed.query))
        action = (parse_qs(parsed.query).get("action") or [""])[0]
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
    assert len(seen) == 3
    for params in seen:
        assert params["chainid"] == ["1"]
        assert params["apikey"] == ["my-secret-key"]

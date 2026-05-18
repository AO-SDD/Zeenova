"""Unit tests for the Solana JSON-RPC client used by ``/wallet``."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from zeenova_bot.solana import (
    DEFAULT_RPC_URL,
    LAMPORTS_PER_SOL,
    SolanaClient,
    is_valid_solana_address,
)

# A real Solana mainnet pubkey (Solana Foundation treasury). Picked
# over a synthetic string so the regex stays honest.
_SOL_ADDR = "GThUX1Atko4tqhN2NaiTazWSeFWMuiUvfFnyJyUghFMJ"


def _rpc_response(result: object) -> MagicMock:
    """Build a fake httpx.Response with the given JSON-RPC ``result``."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.request = MagicMock()
    resp.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": result}
    return resp


def _rpc_error_response(message: str, *, status_code: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.request = MagicMock()
    resp.json.return_value = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32000, "message": message},
    }
    return resp


class TestIsValidSolanaAddress:
    """Pure-function sanity checks on the base58 validator."""

    def test_accepts_known_mainnet_pubkey(self) -> None:
        assert is_valid_solana_address(_SOL_ADDR)

    def test_strips_surrounding_whitespace(self) -> None:
        assert is_valid_solana_address(f"  {_SOL_ADDR}  ")

    def test_rejects_evm_address(self) -> None:
        # 0x-prefixed hex contains ``0`` which isn't in base58, and the
        # length (42) is in range but the prefix is fatal.
        assert not is_valid_solana_address(
            "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
        )

    def test_rejects_ens_name(self) -> None:
        assert not is_valid_solana_address("vitalik.eth")

    def test_rejects_short_string(self) -> None:
        # 31 chars: one below the lower bound. base58 alphabet
        # otherwise correct.
        assert not is_valid_solana_address("Asdkfjasldkfjasldkfjasldkfjasdf")

    def test_rejects_long_string(self) -> None:
        # 45 chars: one above the upper bound.
        assert not is_valid_solana_address("A" * 45)

    def test_rejects_invalid_base58_characters(self) -> None:
        # ``0`` is not in the base58 alphabet (Solana uses Bitcoin's
        # variant). The length is fine but the character poisons it.
        assert not is_valid_solana_address(
            "0ThUX1Atko4tqhN2NaiTazWSeFWMuiUvfFnyJyUghFMJ"
        )

    def test_rejects_empty_string(self) -> None:
        assert not is_valid_solana_address("")
        assert not is_valid_solana_address("   ")


@pytest.mark.asyncio
async def test_fetch_wallet_combines_balance_and_signatures() -> None:
    """Happy path: ``fetch_wallet`` issues two RPCs (balance +
    signatures) and stitches the results into a single
    :class:`SolanaWalletInfo`."""
    client = SolanaClient()
    balance_resp = _rpc_response({"value": int(2.5 * LAMPORTS_PER_SOL)})
    sigs_resp = _rpc_response(
        [
            {
                "signature": "sigA" + "x" * 60,
                "blockTime": 1_700_000_500,
                "err": None,
                "fee": 5000,
            },
            {
                "signature": "sigB" + "y" * 60,
                "blockTime": 1_700_000_000,
                "err": {"InstructionError": [0, "Custom"]},
                "fee": 5000,
            },
        ]
    )
    client._client = MagicMock()
    client._client.post = AsyncMock(side_effect=[balance_resp, sigs_resp])
    try:
        info = await client.fetch_wallet(_SOL_ADDR)
    finally:
        # Bypass the real ``aclose`` since we replaced the client.
        pass
    assert info is not None
    assert info.address == _SOL_ADDR
    assert info.balance_lamports == int(2.5 * LAMPORTS_PER_SOL)
    assert info.balance_sol == pytest.approx(2.5)
    assert info.last_tx_at == 1_700_000_500
    assert len(info.recent) == 2
    assert info.recent[0].is_failed is False
    assert info.recent[1].is_failed is True


@pytest.mark.asyncio
async def test_fetch_wallet_falls_back_when_signatures_fail() -> None:
    """If the signatures RPC fails after the balance call succeeded,
    the wallet info still renders the balance and an empty recent
    tuple — the user shouldn't lose the whole card over one upstream
    blip."""
    client = SolanaClient()
    balance_resp = _rpc_response({"value": 1_000_000_000})
    sigs_resp = _rpc_error_response("upstream borked")
    client._client = MagicMock()
    client._client.post = AsyncMock(side_effect=[balance_resp, sigs_resp])
    info = await client.fetch_wallet(_SOL_ADDR)
    assert info is not None
    assert info.balance_sol == pytest.approx(1.0)
    assert info.recent == ()
    assert info.last_tx_at is None


@pytest.mark.asyncio
async def test_fetch_wallet_returns_none_on_balance_failure() -> None:
    """If the balance RPC itself errors, ``fetch_wallet`` returns
    ``None`` so the handler can render a clean retry message instead
    of a partial card."""
    client = SolanaClient()
    client._client = MagicMock()
    client._client.post = AsyncMock(
        side_effect=httpx.ConnectError("name resolution failed")
    )
    info = await client.fetch_wallet(_SOL_ADDR)
    assert info is None


@pytest.mark.asyncio
async def test_fetch_wallet_returns_none_for_invalid_address() -> None:
    """Garbage in → ``None`` out — without even hitting the RPC."""
    client = SolanaClient()
    client._client = MagicMock()
    client._client.post = AsyncMock(
        side_effect=AssertionError("should not be called")
    )
    info = await client.fetch_wallet("not-a-real-address")
    assert info is None


@pytest.mark.asyncio
async def test_fetch_wallet_caches_repeated_lookups() -> None:
    """Two calls for the same address only fire one round of RPCs —
    the second comes straight from the TTL cache."""
    client = SolanaClient()
    balance_resp = _rpc_response({"value": LAMPORTS_PER_SOL})
    sigs_resp = _rpc_response([])
    post = AsyncMock(side_effect=[balance_resp, sigs_resp])
    client._client = MagicMock()
    client._client.post = post

    first = await client.fetch_wallet(_SOL_ADDR)
    second = await client.fetch_wallet(_SOL_ADDR)
    assert first is second
    # 2 calls = 1 balance + 1 signatures. A second fetch would have
    # bumped this to 4.
    assert post.call_count == 2


@pytest.mark.asyncio
async def test_default_rpc_url_used_when_unset() -> None:
    """An empty ``rpc_url`` falls back to the public mainnet RPC."""
    client = SolanaClient(rpc_url="")
    assert client._rpc_url == DEFAULT_RPC_URL


@pytest.mark.asyncio
async def test_rpc_payload_includes_method_and_params() -> None:
    """The JSON-RPC body has the canonical ``method`` and ``params``
    fields so a real RPC will accept it."""
    client = SolanaClient(rpc_url="https://example.invalid/rpc")
    balance_resp = _rpc_response({"value": 0})
    sigs_resp = _rpc_response([])
    captured: list[dict[str, Any]] = []

    async def _post(url: str, *, json: dict[str, Any]) -> MagicMock:  # noqa: A002
        captured.append(json)
        return balance_resp if len(captured) == 1 else sigs_resp

    client._client = MagicMock()
    client._client.post = AsyncMock(side_effect=_post)
    await client.fetch_wallet(_SOL_ADDR)
    assert captured[0]["method"] == "getBalance"
    assert captured[0]["params"] == [_SOL_ADDR]
    assert captured[1]["method"] == "getSignaturesForAddress"
    # ``limit`` is clamped into [1, 1000].
    assert captured[1]["params"][0] == _SOL_ADDR
    assert 1 <= captured[1]["params"][1]["limit"] <= 1000

"""Tests for the ENS name resolver."""

from __future__ import annotations

import httpx
import pytest

from zeenova_bot.ens import RESOLVER_URL, EnsClient, looks_like_ens


def test_looks_like_ens_accepts_real_names() -> None:
    assert looks_like_ens("vitalik.eth")
    assert looks_like_ens("zeen.eth")
    assert looks_like_ens("Vitalik.ETH")  # case-insensitive
    assert looks_like_ens("sub.deep.example.eth")
    assert looks_like_ens("foo-bar.eth")


def test_looks_like_ens_rejects_addresses_and_garbage() -> None:
    assert not looks_like_ens("")
    assert not looks_like_ens("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
    assert not looks_like_ens("0xabc")  # short 0x prefix
    assert not looks_like_ens("just_text")  # no dot
    assert not looks_like_ens(".eth")  # missing label
    assert not looks_like_ens("foo .eth")  # whitespace in label
    assert not looks_like_ens("foo..eth")  # empty label


@pytest.mark.asyncio
async def test_resolve_returns_lowercased_address() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
                "name": "vitalik.eth",
            },
        )

    client = EnsClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        resolved = await client.resolve("Vitalik.eth")
    finally:
        await client.aclose()
    assert resolved == "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
    assert len(captured) == 1
    assert captured[0] == f"{RESOLVER_URL}/vitalik.eth"


@pytest.mark.asyncio
async def test_resolve_caches_successful_lookups() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200, json={"address": "0x" + "a" * 40, "name": "foo.eth"}
        )

    client = EnsClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        first = await client.resolve("foo.eth")
        second = await client.resolve("foo.eth")
    finally:
        await client.aclose()
    assert first == second
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_resolve_returns_none_when_gateway_has_no_address() -> None:
    """ENSIdeas returns ``{"address": null}`` for unknown names."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"address": None, "name": "nope.eth"})

    client = EnsClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await client.resolve("nope.eth") is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_resolve_returns_none_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = EnsClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await client.resolve("foo.eth") is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_resolve_returns_none_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    client = EnsClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await client.resolve("foo.eth") is None
    finally:
        await client.aclose()

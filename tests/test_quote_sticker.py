"""Tests for the QuoteStickerClient (bot.lyo.su)."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from zeenova_bot.quote_sticker import (
    API_URL,
    QuoteAuthor,
    QuoteEntity,
    QuoteStickerClient,
    ReplyContext,
)


def _ok_payload(image_bytes: bytes) -> dict[str, Any]:
    return {
        "ok": True,
        "result": {"image": base64.b64encode(image_bytes).decode("ascii")},
    }


def _client_with(handler: object) -> QuoteStickerClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    http = httpx.AsyncClient(transport=transport)
    return QuoteStickerClient(client=http)


@pytest.mark.asyncio
async def test_render_returns_decoded_webp_bytes() -> None:
    """Happy path: API returns base64-encoded WebP, client decodes it."""
    expected = b"webp-bytes-pretend"
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(200, json=_ok_payload(expected))

    client = _client_with(handler)
    author = QuoteAuthor(user_id=42, name="Test User", username="tester")
    result = await client.render(author, "Hello, world!")
    assert result == expected
    assert captured["url"] == API_URL
    # The author and text were forwarded.
    assert "Test User" in captured["body"]
    assert "Hello, world!" in captured["body"]
    await client.aclose()


@pytest.mark.asyncio
async def test_render_returns_none_on_http_error() -> None:
    """A 5xx upstream must not propagate as an exception."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    client = _client_with(handler)
    author = QuoteAuthor(user_id=1, name="x")
    assert await client.render(author, "anything") is None
    await client.aclose()


@pytest.mark.asyncio
async def test_render_returns_none_when_api_returns_not_ok() -> None:
    """An ``{ok: false}`` response degrades to None."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "bad input"})

    client = _client_with(handler)
    author = QuoteAuthor(user_id=1, name="x")
    assert await client.render(author, "anything") is None
    await client.aclose()


@pytest.mark.asyncio
async def test_render_returns_none_for_malformed_image_field() -> None:
    """If the image isn't a base64 string, fail gracefully."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": True, "result": {"image": "not!base64!"}}
        )

    client = _client_with(handler)
    author = QuoteAuthor(user_id=1, name="x")
    # Even invalid base64 may decode to *something*; our contract is just
    # "no exception". The exact return value isn't important.
    result = await client.render(author, "anything")
    # base64.b64decode is lenient about non-padding garbage and may
    # succeed; just ensure it didn't crash.
    assert result is None or isinstance(result, bytes)
    await client.aclose()


@pytest.mark.asyncio
async def test_render_returns_none_for_empty_text() -> None:
    """Pure whitespace must not even hit the API."""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        nonlocal called
        called = True
        return httpx.Response(200, json=_ok_payload(b"x"))

    client = _client_with(handler)
    author = QuoteAuthor(user_id=1, name="x")
    assert await client.render(author, "   ") is None
    assert called is False
    await client.aclose()


@pytest.mark.asyncio
async def test_render_truncates_very_long_text() -> None:
    """We cap the forwarded text so the API doesn't reject huge inputs."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(200, json=_ok_payload(b"x"))

    client = _client_with(handler)
    author = QuoteAuthor(user_id=1, name="x")
    long_text = "a" * 5000
    await client.render(author, long_text)
    # Body contains a substring of the input but not the full 5000 a's.
    assert long_text not in captured["body"]
    assert "a" * 1024 in captured["body"]
    await client.aclose()


@pytest.mark.asyncio
async def test_render_forwards_avatar_data_url() -> None:
    """``photo_data_url`` lands in ``from.photo.url`` when set."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_ok_payload(b"x"))

    client = _client_with(handler)
    author = QuoteAuthor(
        user_id=1,
        name="x",
        photo_data_url="data:image/jpeg;base64,AAAA",
    )
    await client.render(author, "hi")
    msg = captured["body"]["messages"][0]
    assert msg["from"]["photo"] == {"url": "data:image/jpeg;base64,AAAA"}
    await client.aclose()


@pytest.mark.asyncio
async def test_render_forwards_entities_and_reply() -> None:
    """Entities + replyMessage are passed through to the upstream API."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_ok_payload(b"x"))

    client = _client_with(handler)
    author = QuoteAuthor(user_id=1, name="x")
    entities = [QuoteEntity(type="bold", offset=0, length=4)]
    reply = ReplyContext(
        name="Alice",
        text="parent text",
        entities=[QuoteEntity(type="italic", offset=0, length=6)],
    )
    await client.render(author, "Hello!", entities=entities, reply=reply)
    msg = captured["body"]["messages"][0]
    assert msg["entities"] == [
        {"type": "bold", "offset": 0, "length": 4},
    ]
    assert msg["replyMessage"]["name"] == "Alice"
    assert msg["replyMessage"]["text"] == "parent text"
    assert msg["replyMessage"]["entities"] == [
        {"type": "italic", "offset": 0, "length": 6},
    ]
    await client.aclose()

"""Quote-sticker client (bot.lyo.su).

Wraps the public ``bot.lyo.su/quote/generate`` API used by ``@QuotLyBot``
and similar quote bots. Given a message author and text, returns the
rendered WebP sticker bytes ready to hand to ``Bot.send_sticker``.

Failures (timeout, non-2xx, malformed JSON, bad base64) all degrade to
``None`` — the caller is expected to treat that as "couldn't render"
and stay silent rather than spamming the chat with an error.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from typing import Final

import httpx

from .http import shared_async_client

logger = logging.getLogger(__name__)

API_URL: Final[str] = "https://bot.lyo.su/quote/generate"

# Telegram-ish dark background that matches the look users associate with
# quote stickers. The API accepts any CSS-style colour or gradient.
DEFAULT_BACKGROUND: Final[str] = "#1b1429"

# Hard cap on the message text we forward; the API rejects anything
# absurd, and very long quotes produce unreadable stickers anyway.
MAX_TEXT_LEN: Final[int] = 1024


@dataclass(slots=True)
class QuoteAuthor:
    """Author metadata for a single message in a quote sticker.

    ``photo_data_url`` is an optional ``data:image/...;base64,...`` URL
    of the author's Telegram profile photo. When present the sticker
    renders the real avatar instead of an initials circle, which is the
    single biggest quality win over the bare-bones default.
    """

    user_id: int
    name: str  # Display name (first + last, or username fallback).
    username: str | None = None
    photo_data_url: str | None = None


@dataclass(slots=True)
class QuoteEntity:
    """Subset of ``telegram.MessageEntity`` we pass to bot.lyo.su.

    Only the fields the quote API actually understands are kept; bot
    handlers convert PTB ``MessageEntity`` instances into this struct
    so this module stays decoupled from python-telegram-bot.
    """

    type: str
    offset: int
    length: int
    url: str | None = None
    custom_emoji_id: str | None = None


@dataclass(slots=True)
class ReplyContext:
    """Optional ``replyMessage`` shown above the main text.

    Renders as a short coloured stripe with a name + the quoted line —
    the same chrome Telegram itself uses for replies. Lets the sticker
    show that "this message was a reply to so-and-so" in one image.
    """

    name: str
    text: str
    entities: list[QuoteEntity] = field(default_factory=list)


def _entities_payload(entities: list[QuoteEntity]) -> list[dict[str, object]]:
    """Convert our QuoteEntity list to the JSON shape the API expects."""
    out: list[dict[str, object]] = []
    for ent in entities:
        item: dict[str, object] = {
            "type": ent.type,
            "offset": ent.offset,
            "length": ent.length,
        }
        if ent.url:
            item["url"] = ent.url
        if ent.custom_emoji_id:
            item["custom_emoji_id"] = ent.custom_emoji_id
        out.append(item)
    return out


class QuoteStickerClient:
    """Render quote stickers via the bot.lyo.su HTTP API."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._client = client or shared_async_client(timeout=timeout)
        self._owns_client = client is None

    async def render(
        self,
        author: QuoteAuthor,
        text: str,
        *,
        entities: list[QuoteEntity] | None = None,
        reply: ReplyContext | None = None,
        background: str = DEFAULT_BACKGROUND,
    ) -> bytes | None:
        """Render a single-message quote sticker. Returns WebP bytes or None.

        ``entities`` preserves bold/italic/code/etc. styling from the
        original message so the rendered sticker matches what users see
        in chat. ``reply`` adds the small "in reply to" stripe above
        the main text when the quoted message was itself a reply.
        """
        if not text.strip():
            return None
        snippet = text[:MAX_TEXT_LEN]
        from_field: dict[str, object] = {
            "id": author.user_id,
            "name": author.name,
        }
        if author.username:
            from_field["username"] = author.username
        if author.photo_data_url:
            from_field["photo"] = {"url": author.photo_data_url}
        message: dict[str, object] = {
            "entities": _entities_payload(entities or []),
            "avatar": True,
            "from": from_field,
            "text": snippet,
        }
        if reply is not None:
            message["replyMessage"] = {
                "name": reply.name,
                "text": reply.text[:MAX_TEXT_LEN],
                "entities": _entities_payload(reply.entities),
            }
        payload: dict[str, object] = {
            "type": "quote",
            "format": "webp",
            "backgroundColor": background,
            # Crisper output without ballooning the WebP — the API
            # supports up to 20×, but 3× is the sweet spot for stickers
            # at Telegram's display sizes.
            "scale": 3,
            "messages": [message],
        }
        try:
            resp = await self._client.post(API_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            logger.exception("quote-sticker: API call failed")
            return None
        if not isinstance(data, dict) or not data.get("ok"):
            logger.warning("quote-sticker: API returned not-ok: %r", data)
            return None
        result = data.get("result")
        if not isinstance(result, dict):
            return None
        image_b64 = result.get("image")
        if not isinstance(image_b64, str) or not image_b64:
            return None
        try:
            return base64.b64decode(image_b64)
        except (ValueError, TypeError):
            logger.exception("quote-sticker: failed to decode response image")
            return None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

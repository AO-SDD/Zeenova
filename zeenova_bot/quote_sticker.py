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
from dataclasses import dataclass
from typing import Final

import httpx

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
    """Author metadata for a single message in a quote sticker."""

    user_id: int
    name: str  # Display name (first + last, or username fallback).
    username: str | None = None


class QuoteStickerClient:
    """Render quote stickers via the bot.lyo.su HTTP API."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def render(
        self,
        author: QuoteAuthor,
        text: str,
        *,
        background: str = DEFAULT_BACKGROUND,
    ) -> bytes | None:
        """Render a single-message quote sticker. Returns WebP bytes or None."""
        if not text.strip():
            return None
        snippet = text[:MAX_TEXT_LEN]
        from_field: dict[str, object] = {
            "id": author.user_id,
            "name": author.name,
        }
        if author.username:
            from_field["username"] = author.username
        payload: dict[str, object] = {
            "type": "quote",
            "format": "webp",
            "backgroundColor": background,
            "messages": [
                {
                    "entities": [],
                    "avatar": True,
                    "from": from_field,
                    "text": snippet,
                }
            ],
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

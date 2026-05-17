"""Helpers for substituting in-body emojis with Telegram Premium
custom emojis.

Telegram supports Premium custom emojis inside HTML message bodies via
the ``<tg-emoji emoji-id="...">FALLBACK</tg-emoji>`` tag. The fallback
character inside the tag is what non-Premium clients (or very old
clients that don't speak the ``tg-emoji`` extension) will render. The
Telegram server enforces the same Premium-ownership requirement as the
``icon_custom_emoji_id`` field on inline-keyboard buttons.

The :class:`PremiumEmojis` dataclass below holds **pre-rendered HTML
strings** for every static emoji slot we care about. Render functions
(``card.render_price_card``, ``_render_market`` etc.) just splice these
strings into their f-strings — they don't need to know whether each
slot was promoted to Premium or not.

Dynamic emojis (e.g. the Fear & Greed face whose glyph depends on the
index value) aren't pre-resolved into the dataclass; callers should
use :func:`premium_emoji` directly with the live fallback and the
configured ID.

A :func:`default_premium_emojis` factory returns a :class:`PremiumEmojis`
whose slots are the raw fallback emojis, so existing callers and tests
that don't care about Premium icons keep working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape


def premium_emoji(emoji: str, custom_emoji_id: str) -> str:
    """Return an HTML snippet for ``emoji`` that uses the Premium
    custom emoji whose ID is ``custom_emoji_id`` when set.

    With an empty / whitespace-only ``custom_emoji_id``, the raw
    ``emoji`` character is returned unchanged. With a non-empty ID,
    the emoji is wrapped in a ``<tg-emoji emoji-id="...">…</tg-emoji>``
    tag — Premium clients show the custom emoji, others fall back to
    ``emoji``.
    """
    cleaned = custom_emoji_id.strip()
    if not cleaned:
        return emoji
    return f'<tg-emoji emoji-id="{escape(cleaned, quote=True)}">{emoji}</tg-emoji>'


@dataclass(frozen=True)
class PremiumEmojis:
    """Resolved HTML for every static in-body emoji slot.

    Each field is either a plain emoji character or an HTML
    ``<tg-emoji>`` tag wrapping the fallback emoji. Render functions
    splice the value directly into their output; no further escaping
    is needed.
    """

    up: str  # 🟢 24H change up / Gainers
    down: str  # 🔴 24H change down / Losers
    # Optional direction-agnostic icon for the 24H Change row. When
    # this is an empty string the price card falls back to ``up``/
    # ``down`` based on direction. Anything else (a plain glyph or a
    # ``<tg-emoji>`` HTML wrapper) replaces both 🟢 and 🔴 on that row.
    change: str
    rank: str  # 🏆 Coin rank
    price: str  # 💵 Price
    high: str  # 🔼 24H high
    low: str  # 🔽 24H low
    mcap: str  # 🏛 Marketcap
    volume: str  # 📊 24H volume
    globe: str  # 🌐 /market header
    btc: str  # 🟠 BTC dominance
    coins: str  # 🪙 Active coins
    top: str  # 📈 /top header
    news: str  # 📰 /news header
    # /ath card slots
    ath_header: str  # 🏆 /ath header trophy
    diamond: str  # 💎 Current price / wallet balance
    ath_up: str  # 🚀 ATH section header
    ath_down: str  # 🩸 ATL section header
    date: str  # 📅 calendar date
    pct_down: str  # 📉 "% from ATH" arrow
    atl_gain: str  # 🚀 "+X% from ATL" gain row (falls back to ath_up if unset)
    # /wallet card slots
    wallet: str  # 🔍 wallet header
    clock: str  # 🕐 recent-transactions header
    gas: str  # ⛽ /gas header
    # /help & /start section icons
    help_header: str  # 📈 brand header
    help_prices: str  # 💹 Live prices & charts
    help_market: str  # 📊 Market overview
    help_calc: str  # 🧮 Calculator & conversion
    help_fiat: str  # 🌍 Worldwide currencies
    help_group: str  # ⚡ Group-friendly


def default_premium_emojis() -> PremiumEmojis:
    """Return a :class:`PremiumEmojis` with the plain emoji fallbacks
    in every slot.

    Used by tests and any caller that doesn't have a ``Settings``
    object on hand.
    """
    return PremiumEmojis(
        up="🟢",
        down="🔴",
        change="",
        rank="🏆",
        price="💵",
        high="🔼",
        low="🔽",
        mcap="🏛",
        volume="📊",
        globe="🌐",
        btc="🟠",
        coins="🪙",
        top="📈",
        news="📰",
        ath_header="🏆",
        diamond="💎",
        ath_up="🚀",
        ath_down="🩸",
        date="📅",
        pct_down="📉",
        atl_gain="🚀",
        wallet="🔍",
        clock="🕐",
        gas="⛽",
        help_header="📈",
        help_prices="💹",
        help_market="📊",
        help_calc="🧮",
        help_fiat="🌍",
        help_group="⚡",
    )

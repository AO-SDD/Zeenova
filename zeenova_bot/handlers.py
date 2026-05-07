"""Telegram command, message, and callback handlers."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from html import escape
from typing import Final

from cachetools import TTLCache
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputMediaPhoto,
    InputTextMessageContent,
    Message,
    MessageEntity,
    Update,
    User,
)
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from .calc import CalcError, safe_eval
from .calc import parse_input as parse_calc_input
from .card import render_price_card
from .chart import render_candles
from .coinpaprika import CoinPaprikaClient, GlobalSnapshot, TickerSnapshot
from .config import Settings
from .fear_greed import FearGreed, FearGreedClient, render_dial
from .fx import FxClient
from .quote_sticker import (
    QuoteAuthor,
    QuoteEntity,
    QuoteStickerClient,
    ReplyContext,
)
from .services import (
    OFF_EXCHANGE_SOURCE,
    CoinNotFound,
    CoinRef,
    CoinService,
    MarketData,
)
from .timeframes import DEFAULT_TIMEFRAME, TIMEFRAMES, Timeframe, get_timeframe

__all__ = [
    "build_application",
    "convert_with_fallback",
]

logger = logging.getLogger(__name__)

# Free-text symbols are short alphanumeric tokens. Anything with spaces or
# punctuation is ignored to keep group chatter from triggering the bot.
_SYMBOL_RE = re.compile(r"^\$?([A-Za-z][A-Za-z0-9]{1,11})$")

# Quote-sticker trigger: a one-character reply of "z" / "Z" turns the
# message we're replying to into a quote sticker. Matches QuotLyBot's
# behaviour. Trailing whitespace is tolerated; anything else (e.g. ``zz``,
# ``z!``) intentionally doesn't trigger.
_QUOTE_STICKER_TRIGGER_RE = re.compile(r"^[zZ]$")

# Operators that signal a clear calculator intent. If a free-text message
# contains at least one of these, we always reply with either a result or
# a parse error — silently dropping it would leave the user wondering.
_CALC_OPS = set("+-*/%^")
# A "strong" calc operator unambiguously signals the user is doing math
# (as opposed to typing a casual number with a percent sign). ``%`` is
# excluded because people commonly write "50%" in chat to mean "fifty
# percent" without expecting a calculation.
_STRONG_CALC_OPS = set("+-*/^")

# matplotlib + mplfinance touch pyplot's global figure manager, which is not
# thread-safe. We render charts off the event loop on a single worker thread
# so concurrent updates serialize safely while still freeing the loop.
_CHART_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="zeenova-chart")

# PIL is thread-safe for independent images, so the F&G dial can render on a
# small dedicated pool. A handful of workers are plenty — the result is
# memoised inside :func:`render_dial`, so under normal load nearly every call
# is a cache hit anyway.
_DIAL_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="zeenova-dial")

# Telegram profile photos rarely change. Cache the rendered ``data:image``
# URL so the bot doesn't hit Bot API + downloads the JPEG on every quote
# sticker. ``None`` is also memoised so users without a photo (or with one
# we can't read) don't get retried on every reply.
_AVATAR_CACHE: TTLCache[int, str | None] = TTLCache(maxsize=2048, ttl=60 * 60 * 12)
# A single in-flight request per user so a flurry of replies doesn't fan
# out into N concurrent Bot API calls for the same avatar.
_AVATAR_LOCKS: dict[int, asyncio.Lock] = {}

# Symbols whose USD value we know directly without hitting any feed. Two
# kinds live here:
#  * ``USDT`` — treated as 1 USD (close enough; saves a useless FX/exchange
#    round-trip for the very common case of pricing things in tether).
#  * ``STAR``/``STARS`` — Telegram Stars at the standard purchase rate of
#    1000 stars = $15 (= $0.015/star). Hard-coded because there isn't a
#    public price feed for them.
_KNOWN_USD_RATES: Final[dict[str, float]] = {
    "usdt": 1.0,
    "star": 0.015,
    "stars": 0.015,
}

def _help_text(settings: Settings) -> str:
    """Build the /start and /help message body from runtime settings."""
    return (
        f"<b>📈 {escape(settings.brand_name)} — your all-in-one crypto desk</b>\n"
        "<i>Real-time prices, candlestick charts, and a smart calculator. "
        "Built for traders, analysts, and crypto communities.</i>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>💹 Live prices &amp; charts</b>\n"
        "• Send any coin symbol to get its price card and 1D chart — "
        "tap <b>15M</b> · <b>1H</b> · <b>4H</b> · <b>1D</b> to switch timeframe.\n"
        "  Examples: <code>BTC</code> · <code>$ETH</code> · "
        "<code>MEGA</code> · <code>OCT</code>\n"
        "• Use <code>/p SYMBOL</code> for an explicit lookup.\n"
        "• Coverage includes every major exchange listing <b>plus</b> "
        "thin-listed coins tracked by aggregators (so coins like OCT or "
        "OPG still resolve).\n\n"
        "<b>📊 Market overview</b>\n"
        "• <code>/market</code> — total marketcap, 24h volume, BTC "
        "dominance, and the active coin count.\n"
        "• <code>/top</code> — the day's biggest gainers and losers from "
        "the top 100 coins by marketcap.\n\n"
        "<b>🧮 Calculator &amp; conversion</b>\n"
        "• Plain math with full operator precedence — "
        "<code>2+2/4</code>, <code>(1+2)*3</code>, <code>2^10</code>, "
        "<code>1k+1</code>\n"
        "• Calculator-style percent — <code>100+10%</code> → 110, "
        "<code>1000-0.1%</code> → 999 (great for fees)\n"
        "• Price any currency in USD — <code>300 btc</code>, "
        "<code>2+2 eth</code>, <code>20 mnt</code>\n"
        "• Convert between any two currencies — <code>1 usd egp</code>, "
        "<code>5000 egp btc</code>, <code>1 eth btc</code>\n"
        "• Telegram Stars — <code>300 star</code>, "
        "<code>3 usd star</code>, <code>3 usdt star</code>\n\n"
        "<b>🌍 Worldwide currencies</b>\n"
        "Every major fiat (USD, EUR, EGP, GBP, AED, …) plus thousands of "
        "cryptocurrencies and stablecoins.\n\n"
        "<b>⚡ Group-friendly</b>\n"
        "Add me to your channel or chat. I stay quiet on casual numbers "
        "(<code>50%</code>, <code>1k</code>) and only reply when you "
        "actually want a quote or a calc."
    )


def _help_keyboard(
    bot_username: str | None, settings: Settings
) -> InlineKeyboardMarkup | None:
    """Build the inline keyboard shown under ``/start`` and ``/help``.

    Top row: a single "Add me to your chat" button that opens
    Telegram's group-picker via the ``?startgroup=true`` deep-link.
    Bottom row: shortcuts to the brand's announcement channel and
    community group. Returns ``None`` when the bot username is
    unavailable (e.g. during very early startup) so we can degrade
    gracefully to a text-only message.
    """
    if not bot_username:
        return None
    add_url = f"https://t.me/{bot_username}?startgroup=true"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Add me to your chat", url=add_url)],
            [
                InlineKeyboardButton(
                    f"📣 {settings.channel_name}",
                    url=settings.telegram_channel_url,
                ),
                InlineKeyboardButton(
                    f"💬 {settings.group_name}",
                    url=settings.telegram_group_url,
                ),
            ],
        ]
    )


def build_application(
    settings: Settings, service: CoinService, fx: FxClient
) -> Application:  # type: ignore[type-arg]
    # AIORateLimiter respects Telegram's per-chat (1 msg/s) and global
    # (30 msg/s) caps automatically, so a busy group can't trigger a
    # FloodWait that delays every other chat. ``concurrent_updates`` lets
    # PTB schedule unrelated messages in parallel rather than serialising
    # the entire bot through a single coroutine.
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(True)
        .rate_limiter(AIORateLimiter())
        .build()
    )
    app.bot_data["settings"] = settings
    app.bot_data["service"] = service
    app.bot_data["fx"] = fx

    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler(["p", "price"], cmd_price))
    app.add_handler(CommandHandler(["top", "movers"], cmd_top))
    app.add_handler(CommandHandler(["market", "global"], cmd_market))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED, on_text
        )
    )
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^tf:"))
    app.add_handler(InlineQueryHandler(on_inline_query))
    app.add_error_handler(on_error)
    return app


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    settings: Settings = context.application.bot_data["settings"]
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=_help_text(settings),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=_help_keyboard(context.bot.username, settings),
    )


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or update.effective_chat is None:
        return
    args = context.args or []
    if not args:
        await msg.reply_text("Usage: /p SYMBOL  (e.g. /p BTC)")
        return
    symbol = args[0].lstrip("$").strip()
    # Explicit command → keep a short "not found" reply so users know the
    # command was received.
    await _send_card(
        update,
        context,
        symbol=symbol,
        timeframe=DEFAULT_TIMEFRAME,
        notify_if_unknown=True,
    )


# ---------------------------------------------------------------------------
# /top and /market — global market snapshot + day's biggest movers.
# ---------------------------------------------------------------------------


# Pull this many rows from /tickers before sorting. 100 is the typical
# "top by marketcap" cut-off and keeps obscure microcaps with insane
# percent swings out of the result.
_TOP_UNIVERSE = 100
# How many gainers / losers to surface in /top.
_TOP_N = 5


def _fmt_usd_compact(v: float | None) -> str:
    """Compact USD amount with K/M/B/T suffix, e.g. ``$1.27T``."""
    if v is None or v <= 0:
        return "—"
    for label, scale in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if v >= scale:
            return f"${v / scale:,.2f}{label}"
    return f"${v:,.2f}"


def _fmt_change_pct(pct: float | None) -> str:
    if pct is None:
        return "—"
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"


def _fmt_unit_price(v: float) -> str:
    """USD price for a single coin, scaled to the value's magnitude."""
    if v >= 1000:
        return f"${v:,.2f}"
    if v >= 1:
        return f"${v:,.4f}"
    if v >= 0.01:
        return f"${v:.4f}"
    if v >= 0.0001:
        return f"${v:.6f}"
    return f"${v:.8f}".rstrip("0").rstrip(".") or "$0"


def _fear_greed_emoji(value: int) -> str:
    """Pick an emoji for a 0..100 Fear & Greed reading.

    Mirrors alternative.me's own colour buckets so the emoji and
    classification stay in sync.
    """
    if value <= 24:
        return "😱"  # Extreme Fear
    if value <= 49:
        return "😨"  # Fear
    if value == 50:
        return "😐"  # Neutral
    if value <= 74:
        return "🙂"  # Greed
    return "🤑"  # Extreme Greed


def _render_market(
    snap: GlobalSnapshot,
    brand_name: str,
    *,
    fear_greed: FearGreed | None = None,
) -> str:
    """HTML body for ``/market``."""
    lines = [
        f"<b>🌐 {escape(brand_name)} — Global market</b>",
        "",
        f"🏛 <b>Total marketcap:</b> {_fmt_usd_compact(snap.market_cap_usd)} "
        f"({_fmt_change_pct(snap.market_cap_change_24h_pct)} 24h)",
        f"📊 <b>24H Volume:</b> {_fmt_usd_compact(snap.volume_24h_usd)}",
    ]
    if snap.bitcoin_dominance_pct is not None:
        lines.append(
            f"🟠 <b>BTC dominance:</b> {snap.bitcoin_dominance_pct:.2f}%"
        )
    if snap.cryptocurrencies_number is not None:
        lines.append(
            f"🪙 <b>Active coins:</b> {snap.cryptocurrencies_number:,}"
        )
    if fear_greed is not None:
        emoji = _fear_greed_emoji(fear_greed.value)
        lines.append(
            f"{emoji} <b>Fear &amp; Greed:</b> {fear_greed.value}/100 "
            f"<i>({escape(fear_greed.classification)})</i>"
        )
    return "\n".join(lines)


def _render_top(
    gainers: list[TickerSnapshot],
    losers: list[TickerSnapshot],
    *,
    universe: int,
) -> str:
    """HTML body for ``/top``: top gainers + top losers in 24h."""

    def _rows(rows: list[TickerSnapshot]) -> list[str]:
        out = []
        for t in rows:
            change = _fmt_change_pct(t.change_pct_24h)
            out.append(
                f"  <b>{escape(t.symbol)}</b> "
                f"<i>({escape(t.name)})</i> — "
                f"{escape(_fmt_unit_price(t.price_usd))}  ·  {escape(change)}"
            )
        return out

    lines = [f"<b>📈 Top movers — last 24h (top {universe} by mcap)</b>", ""]
    lines.append("🟢 <b>Gainers</b>")
    lines.extend(_rows(gainers) or ["  —"])
    lines.append("")
    lines.append("🔴 <b>Losers</b>")
    lines.extend(_rows(losers) or ["  —"])
    return "\n".join(lines)


async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with a snapshot of total marketcap, BTC dominance, etc."""
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    paprika: CoinPaprikaClient | None = context.bot_data.get("paprika")
    fear_greed_client: FearGreedClient | None = context.bot_data.get(
        "fear_greed"
    )
    settings: Settings = context.bot_data["settings"]
    if paprika is None:
        await msg.reply_text(
            "Market data is unavailable right now.",
            parse_mode=ParseMode.HTML,
        )
        return
    paprika_client = paprika  # closures below capture a non-Optional alias

    async def _safe_global() -> GlobalSnapshot | None:
        try:
            return await paprika_client.fetch_global()
        except Exception:  # noqa: BLE001
            logger.exception("/market: fetch_global failed")
            return None

    async def _safe_fng() -> FearGreed | None:
        if fear_greed_client is None:
            return None
        try:
            return await fear_greed_client.fetch_current()
        except Exception:  # noqa: BLE001
            logger.exception("/market: fetch_current (fear & greed) failed")
            return None

    # Fan out the network calls and the "uploading…" indicator together
    # so users see immediate feedback while the data loads.
    snap, fng, _ = await asyncio.gather(
        _safe_global(),
        _safe_fng(),
        _typing(context, chat.id, ChatAction.UPLOAD_PHOTO),
    )
    if snap is None:
        await msg.reply_text(
            "Couldn't reach the market data feed. Try again in a minute.",
            parse_mode=ParseMode.HTML,
        )
        return
    body = _render_market(
        snap, brand_name=settings.brand_name, fear_greed=fng
    )
    if fng is not None:
        # Render the dial in-process so the picture and the caption value
        # are guaranteed to agree (no race against a remote daily PNG).
        # Falls back to a plain text reply if Telegram rejects the
        # photo for any reason so /market never silently disappears.
        try:
            dial_png = await _render_dial_async(
                fng.value, fng.classification, brand=settings.brand_name
            )
            await msg.reply_photo(
                photo=io.BytesIO(dial_png),
                caption=body,
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception:  # noqa: BLE001
            logger.exception("/market: reply_photo failed; falling back to text")
    await msg.reply_text(
        body,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with the day's top gainers and losers from the top-N by mcap."""
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    paprika: CoinPaprikaClient | None = context.bot_data.get("paprika")
    if paprika is None:
        await msg.reply_text(
            "Top movers data is unavailable right now.",
            parse_mode=ParseMode.HTML,
        )
        return
    await _typing(context, chat.id)
    try:
        rows = await paprika.fetch_top_tickers(_TOP_UNIVERSE)
    except Exception:  # noqa: BLE001
        logger.exception("/top: fetch_top_tickers failed")
        rows = []
    if not rows:
        await msg.reply_text(
            "Couldn't reach the market data feed. Try again in a minute.",
            parse_mode=ParseMode.HTML,
        )
        return
    # Sort ascending → losers; descending → gainers. We sort the same
    # universe twice to keep the implementation obvious.
    by_change = sorted(rows, key=lambda t: t.change_pct_24h)
    losers = by_change[:_TOP_N]
    gainers = list(reversed(by_change[-_TOP_N:]))
    await msg.reply_text(
        _render_top(gainers, losers, universe=_TOP_UNIVERSE),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


def _convert_entities(
    entities: Sequence[MessageEntity] | None,
) -> list[QuoteEntity]:
    """Map PTB ``MessageEntity`` instances to our wire format.

    Only the styling-relevant fields the quote API supports are kept.
    Unknown entity types pass through as-is so the upstream renderer
    can decide whether to handle them.
    """
    if not entities:
        return []
    out: list[QuoteEntity] = []
    for ent in entities:
        out.append(
            QuoteEntity(
                type=str(ent.type),
                offset=int(ent.offset),
                length=int(ent.length),
                url=ent.url,
                custom_emoji_id=ent.custom_emoji_id,
            )
        )
    return out


def _display_name(user: User) -> str:
    """Best human-readable label for a Telegram user."""
    return (
        user.full_name
        or user.first_name
        or user.username
        or "Unknown"
    )


async def _fetch_avatar_data_url(
    context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> str | None:
    """Return ``data:image/jpeg;base64,…`` for the user's avatar or ``None``.

    Cached for 12 hours per user. Failures (no profile photo, privacy
    settings, network error) are also cached as ``None`` so we don't
    keep retrying for the same user.
    """
    if user_id in _AVATAR_CACHE:
        return _AVATAR_CACHE[user_id]
    lock = _AVATAR_LOCKS.setdefault(user_id, asyncio.Lock())
    async with lock:
        if user_id in _AVATAR_CACHE:
            return _AVATAR_CACHE[user_id]
        data_url: str | None = None
        try:
            photos = await context.bot.get_user_profile_photos(
                user_id, limit=1
            )
            if photos.total_count and photos.photos:
                # Each "photo" is a list of sizes from smallest to
                # largest. Pick the largest for a sharp avatar at
                # sticker scale.
                sizes = photos.photos[0]
                if sizes:
                    file = await context.bot.get_file(sizes[-1].file_id)
                    raw = await file.download_as_bytearray()
                    encoded = base64.b64encode(bytes(raw)).decode("ascii")
                    data_url = f"data:image/jpeg;base64,{encoded}"
        except TelegramError:
            logger.debug("avatar fetch: bot API rejected user %s", user_id)
        except Exception:  # noqa: BLE001
            logger.exception("avatar fetch: unexpected error for user %s", user_id)
        _AVATAR_CACHE[user_id] = data_url
        return data_url


def _select_quote_text(
    msg: Message, parent: Message
) -> tuple[str, list[QuoteEntity]]:
    """Pick the text + entities to render in the sticker.

    If the user used Telegram's *Quote* feature (introduced in 2024) and
    only highlighted part of the parent message, prefer that snippet.
    Otherwise fall back to the parent's full text or caption. Entities
    track whichever source is chosen so styling stays accurate.
    """
    quote = msg.quote
    if quote is not None and (quote.text or "").strip():
        return quote.text.strip(), _convert_entities(quote.entities)
    if parent.text:
        return parent.text.strip(), _convert_entities(parent.entities)
    caption = (parent.caption or "").strip()
    if caption:
        return caption, _convert_entities(parent.caption_entities)
    return "", []


async def _maybe_make_quote_sticker(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> bool:
    """If ``text`` is the quote-sticker trigger, render & send a sticker.

    Returns True when we either rendered + sent the sticker or decided
    the message *was* a trigger but rendering failed (so the caller
    must not fall through to the calc / symbol handlers and reply with
    an unrelated card on top of the user's quote attempt).
    """
    if not _QUOTE_STICKER_TRIGGER_RE.match(text):
        return False
    msg = update.effective_message
    if msg is None:
        return False
    parent = msg.reply_to_message
    if parent is None:
        # ``z`` typed without replying to anything. Treat as casual chat
        # and fall through silently.
        return False
    quote_text, quote_entities = _select_quote_text(msg, parent)
    if not quote_text:
        return True  # nothing to quote; swallow the trigger
    parent_user = parent.from_user
    if parent_user is None:
        return True
    quote_client: QuoteStickerClient | None = context.bot_data.get(
        "quote_sticker"
    )
    if quote_client is None:
        return False
    avatar_url = await _fetch_avatar_data_url(context, parent_user.id)
    author = QuoteAuthor(
        user_id=parent_user.id,
        name=_display_name(parent_user),
        username=parent_user.username,
        photo_data_url=avatar_url,
    )
    # If the parent itself was a reply, surface that one-line context
    # above the main quote — same chrome Telegram uses natively.
    reply_ctx: ReplyContext | None = None
    grandparent = parent.reply_to_message
    if grandparent is not None and grandparent.from_user is not None:
        gp_text = (grandparent.text or grandparent.caption or "").strip()
        if gp_text:
            reply_ctx = ReplyContext(
                name=_display_name(grandparent.from_user),
                text=gp_text,
                entities=_convert_entities(
                    grandparent.entities or grandparent.caption_entities
                ),
            )
    image = await quote_client.render(
        author,
        quote_text,
        entities=quote_entities,
        reply=reply_ctx,
    )
    if image is None:
        return True  # claimed the trigger but couldn't render — stay silent
    try:
        await msg.reply_sticker(sticker=io.BytesIO(image))
    except Exception:  # noqa: BLE001 — never let a sticker send crash the handler
        logger.exception("quote-sticker: send failed")
    return True


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None or not msg.text:
        return
    settings: Settings = context.bot_data["settings"]
    if (
        chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}
        and settings.allowed_chat_id_set
        and chat.id not in settings.allowed_chat_id_set
    ):
        return

    text = msg.text.strip()

    # Quote-sticker trigger ("z" / "Z" in reply to another message). Runs
    # before calc / symbol so a one-letter reply doesn't accidentally fall
    # into the calculator's syntax-error path.
    if await _maybe_make_quote_sticker(update, context, text):
        return

    # Calc / FX first: anything with a digit may be ``2+2``, ``1 usd egp``,
    # ``2+2/4 btc``, etc. ``handle_calc`` returns True if it claimed the
    # message; otherwise we fall through to the bare-symbol handler.
    if await _handle_calc(update, context, text):
        return

    match = _SYMBOL_RE.match(text)
    if not match:
        return
    symbol = match.group(1)
    if symbol.lower() in _STOP_WORDS:
        return
    # Free-text matches stay silent on misses — a stray word in a chat
    # shouldn't generate a "not found" reply that pollutes the room.
    await _send_card(
        update,
        context,
        symbol=symbol,
        timeframe=DEFAULT_TIMEFRAME,
        notify_if_unknown=False,
    )


async def _resolve_for_price_card(
    service: CoinService, fx: FxClient, symbol: str
) -> CoinRef | None:
    """Resolve a price-card lookup, suppressing junk fiat clashes.

    Wraps :meth:`CoinService.resolve`. If the *only* match comes from
    the off-exchange aggregator (CoinPaprika) and the symbol is also a
    known FX currency code (EGP, TRY, PKR, …), we treat it as
    unknown — otherwise the user typing ``EGP`` would see a price
    card for a $200k-cap scam token instead of recognising that EGP
    is the Egyptian Pound. Coins that aren't on the FX feed (OCT,
    OPG, …) are unaffected.
    """
    ref = await service.resolve(symbol)
    if ref is None:
        return None
    if ref.source == OFF_EXCHANGE_SOURCE and await fx.supports(symbol):
        return None
    return ref


async def _to_usd_rate(
    fx: FxClient, service: CoinService, ccy: str
) -> float | None:
    """How many USD = 1 unit of ``ccy``.

    Resolution order:

    1. ``USD`` and the entries in :data:`_KNOWN_USD_RATES` short-circuit.
    2. **Major-exchange** crypto (Binance / Bybit / MEXC). We check
       this before the FX feed because some 3-letter codes are reused
       between fiat and crypto — e.g. ``MNT`` is both Mongolian Tugrik
       (fiat, ~$0.0003) and Mantle (crypto on MEXC, ~$0.66); ``20 mnt``
       to a price bot almost always means the crypto.
    3. The fawazahmed0 FX feed for legitimate fiats — including the
       ones that *also* have a junk crypto with the same ticker on a
       small aggregator (``EGP`` and ``TRY`` are the worst offenders:
       a $200k-cap scam token sits at the same ticker as the Egyptian
       Pound and the Turkish Lira respectively).
    4. Off-exchange aggregator (CoinPaprika) for thin-listed coins
       like ``OCT`` that no major spot exchange and no FX feed list.
    """
    lower = ccy.lower()
    if lower == "usd":
        return 1.0
    if lower in _KNOWN_USD_RATES:
        return _KNOWN_USD_RATES[lower]
    sym = ccy.upper()
    # If a major exchange lists this symbol, trust the crypto price —
    # only Binance/Bybit/MEXC count, not the off-exchange aggregator.
    ref = await service.resolve(sym)
    if ref is not None and ref.source != OFF_EXCHANGE_SOURCE:
        crypto = await service.usd_rate(sym)
        if crypto is not None:
            return crypto
    # Try the FX feed before any off-exchange match. This is the
    # crucial step: it stops a $200k-cap "EGP" token from masking the
    # Egyptian Pound, while still letting OCT (no FX listing) through
    # to the off-exchange tail below.
    fx_rate = await fx.convert(1.0, ccy, "usd")
    if fx_rate is not None:
        return fx_rate
    # Fall back to the off-exchange aggregator (CoinPaprika) for thin
    # coins like OCT.
    if ref is not None and ref.source == OFF_EXCHANGE_SOURCE:
        crypto = await service.usd_rate(sym)
        if crypto is not None:
            return crypto
    return None


async def convert_with_fallback(
    fx: FxClient,
    service: CoinService,
    amount: float,
    from_ccy: str,
    to_ccy: str,
) -> float | None:
    """Convert ``amount`` from ``from_ccy`` to ``to_ccy``.

    Both sides are priced in USD via :func:`_to_usd_rate` and the result
    is the cross-rate. Bridging through USD (rather than a single direct
    FX call) keeps the answer consistent regardless of which side is
    fiat, crypto, or a hard-coded asset like Telegram Stars — and avoids
    the FX feed silently returning the wrong currency for ambiguous
    3-letter codes.
    """
    if from_ccy.lower() == to_ccy.lower():
        return amount
    from_usd = await _to_usd_rate(fx, service, from_ccy)
    to_usd = await _to_usd_rate(fx, service, to_ccy)
    if from_usd is None or to_usd is None or to_usd == 0:
        return None
    return amount * from_usd / to_usd


def _fmt_amount(value: float) -> str:
    """Pretty-print a numeric amount with sensible precision per magnitude.

    Avoids the common pitfalls — fixed-precision strings either truncating
    interesting decimals on small values or producing a wall of trailing
    zeros on round numbers.
    """
    abs_v = abs(value)
    if abs_v == 0:
        return "0"
    if abs_v >= 1000:
        return f"{value:,.2f}"
    if abs_v >= 1:
        text = f"{value:,.4f}"
    elif abs_v >= 0.0001:
        text = f"{value:.6f}"
    else:
        text = f"{value:.8f}"
    # Strip trailing zeros (e.g. ``2.5000`` → ``2.5``) but keep at least
    # one digit after the decimal point.
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


async def _handle_calc(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> bool:
    """Try to interpret ``text`` as math/conversion. Returns True if handled."""
    msg = update.effective_message
    if msg is None:
        return False

    parsed = parse_calc_input(text)
    if parsed is None:
        return False
    expr, ccy1, ccy2 = parsed

    has_op = any(c in expr for c in _CALC_OPS)
    has_strong_op = any(c in expr for c in _STRONG_CALC_OPS)
    has_ccy = ccy1 is not None
    if not has_op and not has_ccy:
        # Bare number with no operator and no currency — nothing useful to
        # echo back; let it drop silently.
        return False

    # In groups, a bare ``50%`` (no other operator, no currency) is almost
    # always casual conversation rather than a math request. Stay silent
    # so the bot doesn't blurt "= 0.5" into every chat where someone
    # mentions a percentage.
    chat = update.effective_chat
    in_group = chat is not None and chat.type in {
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    }
    if in_group and not has_strong_op and not has_ccy:
        return False

    try:
        value = safe_eval(expr)
    except CalcError as exc:
        # In groups, swallow parse errors silently when the message
        # didn't have a strong calc signal — otherwise the bot blurts
        # "Math error: invalid syntax" at every stray ``50%`` or ``5/``
        # someone types in chat.
        if in_group and not has_strong_op and not has_ccy:
            return False
        await msg.reply_text(f"⚠️ Math error: {exc}")
        return True

    expr_pretty = " ".join(expr.split())
    if not has_ccy:
        # Pure math.
        body = f"<code>{escape(expr_pretty)}</code>\n= <code>{_fmt_amount(value)}</code>"
        await msg.reply_text(body, parse_mode=ParseMode.HTML)
        return True

    fx: FxClient = context.bot_data["fx"]
    service: CoinService = context.bot_data["service"]
    # 1 currency given → ``ccy1 → USD`` (the named currency is what the user
    # has; USD is the implicit quote). 2 given → ``ccy1 → ccy2``.
    if ccy2 is None:
        from_ccy, to_ccy = ccy1.lower(), "usd"  # type: ignore[union-attr]
    else:
        from_ccy, to_ccy = ccy1.lower(), ccy2.lower()  # type: ignore[union-attr]

    try:
        converted = await convert_with_fallback(fx, service, value, from_ccy, to_ccy)
    except Exception:  # noqa: BLE001
        logger.exception("fx: convert raised for %s -> %s", from_ccy, to_ccy)
        await msg.reply_text(
            "⚠️ Couldn't reach the currency rates service. Try again in a moment."
        )
        return True

    if converted is None:
        await msg.reply_text(
            f"⚠️ Unsupported currency pair: <b>{escape(from_ccy.upper())}</b> → "
            f"<b>{escape(to_ccy.upper())}</b>",
            parse_mode=ParseMode.HTML,
        )
        return True

    from_str = f"{_fmt_amount(value)} {from_ccy.upper()}"
    to_str = f"{_fmt_amount(converted)} {to_ccy.upper()}"
    if has_op:
        header = f"<code>{escape(expr_pretty)}</code> = {escape(from_str)}"
    else:
        header = escape(from_str)
    body = f"{header}\n≈ <code>{escape(to_str)}</code>"
    await msg.reply_text(body, parse_mode=ParseMode.HTML)
    return True


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 4 or parts[0] != "tf":
        return
    tf = get_timeframe(parts[1])
    source = parts[2]
    symbol = parts[3].upper()
    if source not in {"binance", "bybit", "mexc"}:
        return
    ref = CoinRef(symbol=symbol, pair=f"{symbol}USDT", source=source)

    service: CoinService = context.bot_data["service"]
    settings: Settings = context.bot_data["settings"]

    try:
        md, candles = await asyncio.gather(
            service.market(ref),
            service.candles(ref=ref, timeframe=tf),
        )
    except CoinNotFound as exc:
        logger.warning("callback: data error for %s/%s: %s", symbol, tf.code, exc)
        await query.answer(f"Could not refresh {symbol}: {exc}", show_alert=False)
        return

    png = await _render_candles_async(
        candles=candles,
        symbol=symbol,
        timeframe=tf,
        brand_name=settings.brand_name,
    )
    caption = render_price_card(
        md,
        channel_name=settings.channel_name,
        channel_url=settings.telegram_channel_url,
        group_name=settings.group_name,
        group_url=settings.telegram_group_url,
    )
    keyboard = _build_keyboard(ref=ref, active=tf)
    try:
        await query.edit_message_media(
            media=InputMediaPhoto(
                media=png, caption=caption, parse_mode=ParseMode.HTML
            ),
            reply_markup=keyboard,
        )
    except BadRequest as exc:
        # Telegram returns 400 "message is not modified" when the new
        # photo is byte-identical to the one already in the message —
        # safe to ignore. Anything else (markup decode errors, expired
        # photo, deleted message, permission issue) is logged.
        msg_text = str(exc).lower()
        if "message is not modified" in msg_text:
            logger.debug("callback: edit ignored (identical content)")
        else:
            logger.warning(
                "callback: edit_message_media failed for %s/%s: %s",
                symbol,
                tf.code,
                exc,
            )
            await query.answer(
                f"Couldn't update {symbol}; try sending the symbol again.",
                show_alert=False,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "callback: unexpected error editing message for %s/%s: %s",
            symbol,
            tf.code,
            exc,
        )
        await query.answer(
            "Sorry, something went wrong. Try again.", show_alert=False
        )


# ---------------------------------------------------------------------------
# Inline mode: ``@zeenovabot btc`` in any chat returns a tappable price card.
# ---------------------------------------------------------------------------


# Cap how long the inline query can be. Telegram already limits inline
# queries to 256 chars, but symbols never go past ~12 so a tighter local
# cap helps short-circuit junk like pasted addresses.
_INLINE_QUERY_MAX = 32

# Cache inline answers for ~30s. Telegram caches the result list per query
# *string* across all users, so a stale snapshot wouldn't survive long even
# without this — but 30s is a sweet spot between freshness and not hammering
# our exchanges every keystroke.
_INLINE_CACHE_SECONDS = 30


def _inline_title(md: MarketData) -> str:
    """One-line summary shown in the inline result picker."""
    pct = md.price_change_pct_24h
    arrow = "▲" if pct is not None and pct >= 0 else "▼"
    change = f" {arrow} {pct:+.2f}%" if pct is not None else ""
    return f"{md.symbol} — ${md.price_usd:,.6g}{change}"


def _inline_description(md: MarketData) -> str:
    """Sub-line in the inline result picker (rank + marketcap)."""
    parts: list[str] = []
    if md.market_cap_rank is not None and md.market_cap_rank > 0:
        parts.append(f"#{md.market_cap_rank}")
    if md.market_cap_usd is not None and md.market_cap_usd > 0:
        cap = md.market_cap_usd
        for label, scale in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
            if cap >= scale:
                parts.append(f"MCap {cap / scale:.2f}{label}")
                break
        else:
            parts.append(f"MCap ${cap:,.0f}")
    parts.append("Tap to send the full price card")
    return " · ".join(parts)


async def on_inline_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Answer ``@zeenovabot <symbol>`` queries with a tappable price card.

    The result is a single :class:`InlineQueryResultArticle` whose body
    is the same HTML price-card we send for ``/p`` lookups. We resolve
    the symbol through the regular :class:`CoinService` pipeline so
    coverage matches the rest of the bot (Binance/Bybit/MEXC + the
    CoinPaprika off-exchange fallback).
    """
    iq = update.inline_query
    if iq is None:
        return
    raw = iq.query.strip()
    if not raw or len(raw) > _INLINE_QUERY_MAX:
        # Empty or absurdly long queries get an empty-but-cached answer.
        # Returning [] still answers the query so Telegram doesn't keep
        # spinning.
        await iq.answer([], cache_time=_INLINE_CACHE_SECONDS, is_personal=False)
        return

    # Allow leading ``$`` (``$btc``) and any extra whitespace, then
    # validate against the same shape used by free-text symbol matches
    # so we don't accidentally try to "resolve" an English sentence.
    candidate = raw.lstrip("$").strip()
    if not _SYMBOL_RE.match(candidate):
        await iq.answer([], cache_time=_INLINE_CACHE_SECONDS, is_personal=False)
        return

    service: CoinService = context.bot_data["service"]
    settings: Settings = context.bot_data["settings"]
    fx: FxClient = context.bot_data["fx"]

    try:
        ref = await _resolve_for_price_card(service, fx, candidate)
    except Exception:  # noqa: BLE001 — never let an inline query crash the bot
        logger.exception("inline: resolve failed for %r", candidate)
        await iq.answer([], cache_time=_INLINE_CACHE_SECONDS, is_personal=False)
        return
    if ref is None:
        await iq.answer([], cache_time=_INLINE_CACHE_SECONDS, is_personal=False)
        return

    try:
        md = await service.market(ref)
    except CoinNotFound:
        await iq.answer([], cache_time=_INLINE_CACHE_SECONDS, is_personal=False)
        return
    except Exception:  # noqa: BLE001
        logger.exception("inline: market fetch failed for %s", ref.symbol)
        await iq.answer([], cache_time=_INLINE_CACHE_SECONDS, is_personal=False)
        return

    body = render_price_card(
        md,
        channel_name=settings.channel_name,
        channel_url=settings.telegram_channel_url,
        group_name=settings.group_name,
        group_url=settings.telegram_group_url,
    )
    result = InlineQueryResultArticle(
        # Stable per (symbol, source) so Telegram dedups multiple users
        # picking the same suggestion within the cache window.
        id=f"price:{md.source}:{md.symbol}",
        title=_inline_title(md),
        description=_inline_description(md),
        input_message_content=InputTextMessageContent(
            message_text=body,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        ),
    )
    await iq.answer(
        [result], cache_time=_INLINE_CACHE_SECONDS, is_personal=False
    )


async def _send_card(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    symbol: str,
    timeframe: Timeframe,
    notify_if_unknown: bool,
) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return

    service: CoinService = context.bot_data["service"]
    settings: Settings = context.bot_data["settings"]
    fx: FxClient = context.bot_data["fx"]

    # Show a "uploading photo…" hint immediately so the user knows the
    # bot received the request, even when upstream feeds are slow.
    await _typing(context, chat.id, ChatAction.UPLOAD_PHOTO)

    ref = await _resolve_for_price_card(service, fx, symbol)
    if ref is None:
        if notify_if_unknown:
            await msg.reply_text(
                f"<b>{escape(symbol.upper())}</b> not found on any of our data sources.",
                parse_mode=ParseMode.HTML,
            )
        return

    # Off-exchange coins (resolved via CoinPaprika) have no OHLC history
    # we can render — send a text-only price card instead of a chart.
    if ref.source == OFF_EXCHANGE_SOURCE:
        try:
            md = await service.market(ref)
        except CoinNotFound as exc:
            await msg.reply_text(
                f"Data error: {escape(str(exc))}",
                parse_mode=ParseMode.HTML,
            )
            return
        caption = render_price_card(
            md,
            channel_name=settings.channel_name,
            channel_url=settings.telegram_channel_url,
            group_name=settings.group_name,
            group_url=settings.telegram_group_url,
        )
        await msg.reply_text(
            caption,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    try:
        md, candles = await asyncio.gather(
            service.market(ref),
            service.candles(ref=ref, timeframe=timeframe),
        )
    except CoinNotFound as exc:
        await msg.reply_text(
            f"Data error: {escape(str(exc))}",
            parse_mode=ParseMode.HTML,
        )
        return

    png = await _render_candles_async(
        candles=candles,
        symbol=ref.symbol,
        timeframe=timeframe,
        brand_name=settings.brand_name,
    )
    caption = render_price_card(
        md,
        channel_name=settings.channel_name,
        channel_url=settings.telegram_channel_url,
        group_name=settings.group_name,
        group_url=settings.telegram_group_url,
    )
    keyboard = _build_keyboard(ref=ref, active=timeframe)
    await context.bot.send_photo(
        chat_id=chat.id,
        photo=png,
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        reply_to_message_id=msg.message_id,
    )


def _build_keyboard(*, ref: CoinRef, active: Timeframe) -> InlineKeyboardMarkup:
    row: list[InlineKeyboardButton] = []
    for tf in TIMEFRAMES:
        label = f"✅ {tf.label}" if tf.code == active.code else tf.label
        row.append(
            InlineKeyboardButton(
                label, callback_data=f"tf:{tf.code}:{ref.source}:{ref.symbol}"
            )
        )
    return InlineKeyboardMarkup([row])


async def _render_candles_async(
    *,
    candles: list[list[float]],
    symbol: str,
    timeframe: Timeframe,
    brand_name: str,
) -> bytes:
    """Run the synchronous matplotlib renderer in a thread to keep
    the event loop responsive under bursts of concurrent updates.
    """
    loop = asyncio.get_running_loop()
    func = partial(
        render_candles,
        candles=candles,
        symbol=symbol,
        timeframe=timeframe,
        brand_name=brand_name,
    )
    return await loop.run_in_executor(_CHART_EXECUTOR, func)


async def _render_dial_async(
    value: int, classification: str | None, *, brand: str | None
) -> bytes:
    """Render the F&G dial off the event loop. Cache hits are still cheap
    and stay in-process; cache misses (rare) get pushed to the dial
    executor so the loop never blocks on PIL's bytestream encoding.
    """
    loop = asyncio.get_running_loop()
    func = partial(render_dial, value, classification, brand=brand)
    return await loop.run_in_executor(_DIAL_EXECUTOR, func)


async def _typing(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, action: str = ChatAction.TYPING
) -> None:
    """Best-effort 'typing/uploading…' indicator. Failures are swallowed —
    the indicator is purely a UX nicety and must never block the real reply.
    """
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=action)
    except Exception:  # noqa: BLE001
        logger.debug("send_chat_action failed", exc_info=True)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error in handler", exc_info=context.error)


# Common English / Arabic chat words that look like symbols but should not
# trigger lookups. Kept short on purpose; false positives are easy to avoid by
# falling back to /p.
_STOP_WORDS: frozenset[str] = frozenset(
    {
        "ok", "okay", "yes", "no", "lol", "hi", "hello", "hey", "bye",
        "the", "and", "but", "for", "you", "yo", "ya", "wow", "nice",
        "thanks", "thx", "ty", "gm", "gn", "wtf", "omg", "bro", "sir",
    }
)

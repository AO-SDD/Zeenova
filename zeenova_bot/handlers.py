"""Telegram command, message, and callback handlers."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import logging
import re
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
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
from .coingecko import AthAtl
from .coingecko import MarketcapClient as CoinGeckoMarketcap
from .coinpaprika import CoinPaprikaClient, GlobalSnapshot, TickerSnapshot
from .config import Settings
from .edit_state import EditableReplyStore, ReplyKind
from .emojis import PremiumEmojis, default_premium_emojis, premium_emoji
from .etherscan import (
    CHAINS,
    ChainBalance,
    ChainGas,
    EtherscanClient,
    WalletInfo,
    WalletTx,
    is_valid_address,
)
from .fear_greed import FearGreed, FearGreedClient, render_dial
from .fx import FxClient
from .news import NewsArticle, NewsClient
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
# Single-letter symbols are allowed (e.g. ``S`` for Sonic, ``H`` for Hivemapper)
# — a one-letter message in chat is a deliberate lookup intent, not noise.
_SYMBOL_RE = re.compile(r"^\$?([A-Za-z][A-Za-z0-9]{0,11})$")

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
        "the top 100 coins by marketcap.\n"
        "• <code>/news</code> — latest crypto headlines from major "
        "outlets (CoinDesk, Cointelegraph, Decrypt).\n"
        "• <code>/ath SYMBOL</code> — all-time-high and all-time-low "
        "records (e.g. <code>/ath btc</code>).\n"
        "• <code>/wallet 0x…</code> — multichain wallet summary: native "
        "balance + USD on Ethereum, BSC, Polygon, Arbitrum, Optimism, "
        "Base &amp; Avalanche.\n"
        "• <code>/gas</code> — live gas rates across every supported "
        "chain with a USD estimate per tier.\n\n"
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
    # Tracks the bot's reply for each user message so we can edit it in
    # place when the user edits the original (calc result re-computes,
    # price card swaps to the new symbol, etc.). LRU-bounded; see
    # :mod:`zeenova_bot.edit_state`.
    app.bot_data["edit_store"] = EditableReplyStore()

    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler(["p", "price"], cmd_price))
    app.add_handler(CommandHandler(["top", "movers"], cmd_top))
    app.add_handler(CommandHandler(["market", "global"], cmd_market))
    app.add_handler(CommandHandler(["news"], cmd_news))
    app.add_handler(CommandHandler(["ath"], cmd_ath))
    app.add_handler(CommandHandler(["wallet", "addr"], cmd_wallet))
    app.add_handler(CommandHandler(["gas"], cmd_gas))
    app.add_handler(CommandHandler(["emojiid"], cmd_emojiid))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED_MESSAGE,
            on_text,
        )
    )
    # Edited messages route through a dedicated handler that re-runs the
    # calc / price-card pipeline and edits the bot's previous reply
    # instead of sending a new one. Covers both free-text edits and
    # ``/p <symbol>`` edits (CommandHandler ignores edits by default).
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.UpdateType.EDITED_MESSAGE,
            on_edited_text,
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
        await _send_text_reply(
            update, context, "Usage: /p SYMBOL  (e.g. /p BTC)"
        )
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


def _resolve_premium_emojis(settings: Settings) -> PremiumEmojis:
    """Build a :class:`PremiumEmojis` from the current ``Settings``.

    Each slot is wrapped in a ``<tg-emoji>`` tag when the corresponding
    ``PREMIUM_EMOJI_*_ID`` env var is non-empty; otherwise the plain
    fallback emoji is used.
    """
    # The ``change`` slot is intentionally not wrapped via
    # ``premium_emoji(emoji, …)`` — it's an *override* slot. When the
    # operator hasn't set ``PREMIUM_EMOJI_CHANGE_ID`` we leave it
    # empty so ``card.render_price_card`` falls back to the up/down
    # dot. When the ID is set we wrap a neutral fallback (📊) inside
    # the tag so non-Premium clients still see a sensible glyph.
    change_id = settings.premium_emoji_change_id.strip()
    change_html = premium_emoji("📊", change_id) if change_id else ""
    return PremiumEmojis(
        up=premium_emoji("🟢", settings.premium_emoji_up_id),
        down=premium_emoji("🔴", settings.premium_emoji_down_id),
        change=change_html,
        rank=premium_emoji("🏆", settings.premium_emoji_rank_id),
        price=premium_emoji("💵", settings.premium_emoji_price_id),
        high=premium_emoji("🔼", settings.premium_emoji_high_id),
        low=premium_emoji("🔽", settings.premium_emoji_low_id),
        mcap=premium_emoji("🏛", settings.premium_emoji_mcap_id),
        volume=premium_emoji("📊", settings.premium_emoji_volume_id),
        globe=premium_emoji("🌐", settings.premium_emoji_globe_id),
        btc=premium_emoji("🟠", settings.premium_emoji_btc_id),
        coins=premium_emoji("🪙", settings.premium_emoji_coins_id),
        top=premium_emoji("📈", settings.premium_emoji_top_id),
        news=premium_emoji("📰", settings.premium_emoji_news_id),
        ath_header=premium_emoji("🏆", settings.premium_emoji_ath_header_id),
        diamond=premium_emoji("💎", settings.premium_emoji_diamond_id),
        ath_up=premium_emoji("🚀", settings.premium_emoji_ath_up_id),
        ath_down=premium_emoji("🩸", settings.premium_emoji_ath_down_id),
        date=premium_emoji("📅", settings.premium_emoji_date_id),
        pct_down=premium_emoji("📉", settings.premium_emoji_pct_down_id),
        wallet=premium_emoji("🔍", settings.premium_emoji_wallet_id),
        clock=premium_emoji("🕐", settings.premium_emoji_clock_id),
        gas=premium_emoji("⛽", settings.premium_emoji_gas_id),
    )


def _render_market(
    snap: GlobalSnapshot,
    brand_name: str,
    *,
    fear_greed: FearGreed | None = None,
    emojis: PremiumEmojis,
    fng_id: str = "",
) -> str:
    """HTML body for ``/market``."""
    lines = [
        f"<b>{emojis.globe} {escape(brand_name)} — Global market</b>",
        "",
        f"{emojis.mcap} <b>Total marketcap:</b> "
        f"{_fmt_usd_compact(snap.market_cap_usd)} "
        f"({_fmt_change_pct(snap.market_cap_change_24h_pct)} 24h)",
        f"{emojis.volume} <b>24H Volume:</b> "
        f"{_fmt_usd_compact(snap.volume_24h_usd)}",
    ]
    if snap.bitcoin_dominance_pct is not None:
        lines.append(
            f"{emojis.btc} <b>BTC dominance:</b> "
            f"{snap.bitcoin_dominance_pct:.2f}%"
        )
    if snap.cryptocurrencies_number is not None:
        lines.append(
            f"{emojis.coins} <b>Active coins:</b> "
            f"{snap.cryptocurrencies_number:,}"
        )
    if fear_greed is not None:
        face = _fear_greed_emoji(fear_greed.value)
        # F&G face is dynamic (5 possible glyphs). We wrap the live
        # face in the configured Premium custom-emoji tag so the
        # fallback shown to non-Premium clients still matches the
        # current index value.
        face_html = premium_emoji(face, fng_id)
        lines.append(
            f"{face_html} <b>Fear &amp; Greed:</b> {fear_greed.value}/100 "
            f"<i>({escape(fear_greed.classification)})</i>"
        )
    return "\n".join(lines)


def _render_top(
    gainers: list[TickerSnapshot],
    losers: list[TickerSnapshot],
    *,
    universe: int,
    emojis: PremiumEmojis,
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

    lines = [
        f"<b>{emojis.top} Top movers — last 24h (top {universe} by mcap)</b>",
        "",
    ]
    lines.append(f"{emojis.up} <b>Gainers</b>")
    lines.extend(_rows(gainers) or ["  —"])
    lines.append("")
    lines.append(f"{emojis.down} <b>Losers</b>")
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
    emojis = _resolve_premium_emojis(settings)
    body = _render_market(
        snap,
        brand_name=settings.brand_name,
        fear_greed=fng,
        emojis=emojis,
        fng_id=settings.premium_emoji_fng_id,
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
                reply_markup=_brand_keyboard(settings),
            )
            return
        except Exception:  # noqa: BLE001
            logger.exception("/market: reply_photo failed; falling back to text")
    await msg.reply_text(
        body,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=_brand_keyboard(settings),
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
    settings: Settings = context.bot_data["settings"]
    emojis = _resolve_premium_emojis(settings)
    await msg.reply_text(
        _render_top(
            gainers, losers, universe=_TOP_UNIVERSE, emojis=emojis
        ),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=_brand_keyboard(settings),
    )


# How many headlines /news surfaces by default. Telegram's message body
# stays comfortably under the 4096-char cap at this size and the user
# isn't drowned in low-signal articles.
_NEWS_LIMIT: Final[int] = 6


def _render_news(
    articles: list[NewsArticle],
    brand_name: str,
    *,
    emojis: PremiumEmojis,
) -> str:
    """HTML body for ``/news`` — a compact, link-rich headline digest."""
    lines = [
        f"<b>{emojis.news} {escape(brand_name)} — latest crypto news</b>",
        "",
    ]
    for art in articles:
        title = escape(art.title)
        # Each item is a single line: the title is the clickable link
        # and the source is shown in muted italic right after.
        lines.append(
            f'• <a href="{escape(art.url)}">{title}</a>  '
            f"<i>— {escape(art.source)}</i>"
        )
    lines.append("")
    lines.append("<i>Sources: CoinDesk · Cointelegraph · Decrypt</i>")
    return "\n".join(lines)


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with the latest crypto headlines from major outlets."""
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    news: NewsClient | None = context.bot_data.get("news")
    if news is None:
        await msg.reply_text(
            "News feed is unavailable right now.",
            parse_mode=ParseMode.HTML,
        )
        return
    await _typing(context, chat.id)
    try:
        articles = await news.fetch_latest(limit=_NEWS_LIMIT)
    except Exception:  # noqa: BLE001
        logger.exception("/news: fetch_latest failed")
        articles = []
    if not articles:
        await msg.reply_text(
            "Couldn't load the news feed right now. Try again in a minute.",
            parse_mode=ParseMode.HTML,
        )
        return
    settings: Settings = context.bot_data["settings"]
    emojis = _resolve_premium_emojis(settings)
    await msg.reply_text(
        _render_news(articles, settings.brand_name, emojis=emojis),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=_brand_keyboard(settings),
    )


# ---------------------------------------------------------------------------
# /ath — all-time-high / all-time-low snapshot for a coin.
# ---------------------------------------------------------------------------


def _humanize_age(now_ts: int, then_ts: int) -> str:
    """Compact "x ago" string for ``then_ts`` relative to ``now_ts``.

    Mirrors the style used in the rest of the bot (short, no leading
    zeros). We pick the largest unit that still produces a single
    integer ≥ 1 — e.g. 3,700 s renders as ``1h``, 90,000 s as ``1d``,
    400 d as ``1y``. Future timestamps clamp to ``"just now"`` so a
    minor clock skew between Etherscan / CoinGecko and the bot never
    surfaces a negative duration.
    """
    delta = max(0, now_ts - then_ts)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86_400:
        return f"{delta // 3600}h ago"
    if delta < 86_400 * 30:
        return f"{delta // 86_400}d ago"
    if delta < 86_400 * 365:
        return f"{delta // (86_400 * 30)}mo ago"
    return f"{delta // (86_400 * 365)}y ago"


def _parse_iso_ts(value: str) -> datetime | None:
    """Parse a CoinGecko-style ISO 8601 timestamp.

    CoinGecko emits trailing ``Z`` for UTC; :func:`datetime.fromisoformat`
    only learned to accept it in 3.11+. We do the swap explicitly so
    behaviour stays predictable across runtime versions.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt_iso_date(value: str) -> str:
    """Render a CoinGecko ISO timestamp as ``Mon DD, YYYY``."""
    parsed = _parse_iso_ts(value)
    if parsed is None:
        return "—"
    return parsed.strftime("%b %-d, %Y")


def _render_ath(
    snap: AthAtl,
    brand_name: str,
    emojis: PremiumEmojis | None = None,
) -> str:
    """HTML body for ``/ath``: ATH/ATL snapshot with relative dates."""
    e = emojis if emojis is not None else default_premium_emojis()
    now = int(datetime.now(UTC).timestamp())
    ath_dt = _parse_iso_ts(snap.ath_date)
    atl_dt = _parse_iso_ts(snap.atl_date)
    ath_ago = (
        _humanize_age(now, int(ath_dt.timestamp())) if ath_dt is not None else "—"
    )
    atl_ago = (
        _humanize_age(now, int(atl_dt.timestamp())) if atl_dt is not None else "—"
    )
    rank_line = (
        f"{e.rank} <b>Rank:</b> #{snap.rank}\n" if snap.rank is not None else ""
    )
    return (
        f"{e.ath_header} <b>{escape(snap.name)} ({escape(snap.symbol)})</b> — "
        f"<i>All-Time Records</i>\n"
        f"\n"
        f"{e.diamond} <b>Current:</b> {escape(_fmt_unit_price(snap.current_price))}\n"
        f"{rank_line}"
        f"\n"
        f"{e.ath_up} <b>All-Time High</b>\n"
        f"  {escape(_fmt_unit_price(snap.ath))}\n"
        f"  {e.date} {escape(_fmt_iso_date(snap.ath_date))} "
        f"<i>({escape(ath_ago)})</i>\n"
        f"  {e.pct_down} <b>{_fmt_change_pct(snap.ath_change_pct)}</b> from ATH\n"
        f"\n"
        f"{e.ath_down} <b>All-Time Low</b>\n"
        f"  {escape(_fmt_unit_price(snap.atl))}\n"
        f"  {e.date} {escape(_fmt_iso_date(snap.atl_date))} "
        f"<i>({escape(atl_ago)})</i>\n"
        f"  {e.ath_up} <b>{_fmt_change_pct(snap.atl_change_pct)}</b> from ATL\n"
        f"\n"
        f"<i>Data: CoinGecko · {escape(brand_name)}</i>"
    )


async def cmd_ath(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with ATH/ATL snapshot for a coin from CoinGecko."""
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    args = context.args or []
    if not args:
        await msg.reply_text(
            "Usage: <code>/ath SYMBOL</code>  (e.g. <code>/ath BTC</code>)",
            parse_mode=ParseMode.HTML,
        )
        return
    coingecko: CoinGeckoMarketcap | None = context.bot_data.get("coingecko")
    if coingecko is None:
        await msg.reply_text(
            "ATH lookup is unavailable right now.",
            parse_mode=ParseMode.HTML,
        )
        return
    symbol = args[0].lstrip("$").strip().upper()
    if not symbol:
        await msg.reply_text(
            "Usage: <code>/ath SYMBOL</code>  (e.g. <code>/ath BTC</code>)",
            parse_mode=ParseMode.HTML,
        )
        return
    await _typing(context, chat.id)
    snap = await coingecko.fetch_ath_atl(symbol)
    if snap is None:
        await msg.reply_text(
            f"No ATH/ATL data for <b>{escape(symbol)}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return
    settings: Settings = context.bot_data["settings"]
    emojis = _resolve_premium_emojis(settings)
    await msg.reply_text(
        _render_ath(snap, settings.brand_name, emojis),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=_brand_keyboard(settings),
    )


# ---------------------------------------------------------------------------
# /wallet — Multichain wallet summary via Etherscan V2.
# ---------------------------------------------------------------------------


def _short_addr(address: str) -> str:
    """Shorten an Ethereum address to ``0xABCD…1234`` form for display."""
    if len(address) < 10:
        return address
    return f"{address[:6]}…{address[-4:]}"


def _fmt_native(value: float) -> str:
    """Format a native-token amount with magnitude-appropriate precision."""
    if value >= 1_000_000:
        return f"{value:,.2f}"
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:,.4f}"
    if value >= 0.0001:
        return f"{value:.6f}"
    if value == 0:
        return "0"
    return f"{value:.8f}".rstrip("0").rstrip(".") or "0"


# Kept under the old name for any out-of-tree callers / tests; renderers
# inside this module use :func:`_fmt_native` directly.
_fmt_eth = _fmt_native


def _render_wallet_tx(tx: WalletTx, native_symbol: str, now_ts: int) -> str:
    """One ``recent transactions`` line."""
    amount = tx.value_wei / 10**18
    counterparty = tx.from_addr if tx.is_incoming else tx.to_addr
    arrow = "↙" if tx.is_incoming else "↗"
    sign = "+" if tx.is_incoming else "-"
    direction = "from" if tx.is_incoming else "to"
    age = _humanize_age(now_ts, tx.timestamp)
    return (
        f"  {arrow} <code>{sign}{escape(_fmt_native(amount))} "
        f"{escape(native_symbol)}</code> "
        f"{direction} <code>{escape(_short_addr(counterparty))}</code> "
        f"<i>({escape(age)})</i>"
    )


def _render_wallet(
    info: WalletInfo,
    prices: dict[str, float],
    brand_name: str,
    emojis: PremiumEmojis | None = None,
) -> str:
    """HTML body for ``/wallet``.

    ``prices`` maps a chain's ``price_symbol`` (e.g. ``"ETH"``,
    ``"BNB"``) to its current USD price. Unknown / missing entries
    just drop the USD column for that row.
    """
    e = emojis if emojis is not None else default_premium_emojis()
    now = int(datetime.now(UTC).timestamp())
    nonzero: list[ChainBalance] = [cb for cb in info.balances if cb.balance_wei > 0]
    total_usd = 0.0
    for cb in nonzero:
        px = prices.get(cb.chain.price_symbol, 0.0)
        if px > 0:
            total_usd += cb.balance * px

    lines: list[str] = [
        f"{e.wallet} <b>Wallet</b> <code>{escape(_short_addr(info.address))}</code>",
        "",
    ]
    if total_usd > 0:
        lines.append(
            f"{e.diamond} <b>Total:</b> ≈ {escape(_fmt_usd_compact(total_usd))}"
        )
        lines.append("")

    if nonzero:
        lines.append(f"{e.diamond} <b>Balances</b>")
        for cb in nonzero:
            px = prices.get(cb.chain.price_symbol, 0.0)
            usd_str = (
                f"  ≈ {escape(_fmt_usd_compact(cb.balance * px))}"
                if px > 0
                else ""
            )
            lines.append(
                f"  <b>{escape(cb.chain.name):<10}</b> "
                f"<code>{escape(_fmt_native(cb.balance))} "
                f"{escape(cb.chain.native_symbol)}</code>{usd_str}"
            )
    else:
        lines.append(
            f"{e.diamond} <i>No native-token balance on any tracked chain.</i>"
        )

    # Activity counters come from the most-active chain (whichever has
    # the highest nonce). Falls back to "no activity" for fresh wallets.
    active = info.recent_chain
    total_sent = sum(cb.txs_sent for cb in info.balances)
    lines.append("")
    lines.append("📊 <b>Activity</b>")
    lines.append(f"  Outgoing txs (all chains): <b>{total_sent:,}</b>")
    if active is not None and info.last_tx_at is not None:
        lines.append(
            f"  Last seen: <i>{escape(_humanize_age(now, info.last_tx_at))}</i> "
            f"<i>on {escape(active.name)}</i>"
        )
    else:
        lines.append("  Last seen: <i>never</i>")

    if info.recent and active is not None:
        lines.append("")
        lines.append(
            f"{e.clock} <b>Recent transactions</b> <i>· {escape(active.name)}</i>"
        )
        lines.extend(
            _render_wallet_tx(tx, active.native_symbol, now) for tx in info.recent
        )
    lines.append("")
    lines.append(f"<i>Data: Etherscan · {escape(brand_name)}</i>")
    return "\n".join(lines)


async def _fetch_native_prices(
    paprika: CoinPaprikaClient | None, symbols: Iterable[str]
) -> dict[str, float]:
    """Fetch USD prices for a set of native-token symbols in parallel.

    Returns a ``{symbol: price}`` map; symbols whose lookup fails are
    simply omitted so the caller can render "—" for them.
    """
    if paprika is None:
        return {}
    unique = sorted({s.upper() for s in symbols if s})
    if not unique:
        return {}

    async def _one(sym: str) -> tuple[str, float] | None:
        try:
            snap = await paprika.fetch_price_snapshot(sym)
        except Exception:  # noqa: BLE001
            logger.exception("native price lookup failed for %s", sym)
            return None
        if snap is None or snap.price_usd <= 0:
            return None
        return sym, float(snap.price_usd)

    results = await asyncio.gather(*(_one(s) for s in unique))
    return {sym: price for r in results if r is not None for sym, price in (r,)}


async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with a multichain wallet summary card."""
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    args = context.args or []
    if not args:
        await msg.reply_text(
            "Usage: <code>/wallet 0x…</code>  "
            "(e.g. <code>/wallet 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045</code>)",
            parse_mode=ParseMode.HTML,
        )
        return
    address = args[0].strip()
    if not is_valid_address(address):
        await msg.reply_text(
            "That doesn't look like a valid Ethereum address. "
            "Expected a 0x-prefixed 40-character hex string.",
            parse_mode=ParseMode.HTML,
        )
        return
    etherscan: EtherscanClient | None = context.bot_data.get("etherscan")
    if etherscan is None or not etherscan.is_configured():
        await msg.reply_text(
            "Wallet lookup needs an Etherscan API key.\n"
            "Set <code>ETHERSCAN_API_KEY</code> in your environment "
            "(free key at <a href=\"https://etherscan.io/apis\">"
            "etherscan.io/apis</a>) and restart the bot.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return
    paprika: CoinPaprikaClient | None = context.bot_data.get("paprika")
    await _typing(context, chat.id)

    needed_symbols = {c.price_symbol for c in CHAINS}
    info, prices = await asyncio.gather(
        etherscan.fetch_wallet(address),
        _fetch_native_prices(paprika, needed_symbols),
    )
    if info is None:
        await msg.reply_text(
            "Couldn't reach Etherscan for that wallet. Try again in a minute.",
            parse_mode=ParseMode.HTML,
        )
        return
    settings: Settings = context.bot_data["settings"]
    emojis = _resolve_premium_emojis(settings)
    await msg.reply_text(
        _render_wallet(info, prices, settings.brand_name, emojis),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=_brand_keyboard(settings),
    )


# ---------------------------------------------------------------------------
# /gas — gas oracle across every supported Etherscan chain.
# ---------------------------------------------------------------------------


# Native-transfer gas budget; used to convert gwei → USD estimate for
# the "(~$X.XX)" hint on each tier. Real-world tx cost on dapps is
# usually 5–10× higher (~150k gas for a Uniswap swap) but a plain
# transfer is the universally relatable benchmark.
_NATIVE_TRANSFER_GAS = 21_000


def _gwei_to_usd(gwei: float, native_price_usd: float) -> float:
    """USD cost of a 21k-gas native transfer at ``gwei`` and price."""
    if gwei <= 0 or native_price_usd <= 0:
        return 0.0
    return gwei * _NATIVE_TRANSFER_GAS * 1e-9 * native_price_usd


def _fmt_gas_usd(usd: float) -> str:
    if usd <= 0:
        return ""
    if usd >= 1:
        return f" <i>(~${usd:,.2f})</i>"
    if usd >= 0.01:
        return f" <i>(~${usd:.2f})</i>"
    return f" <i>(~${usd:.4f})</i>"


def _fmt_gwei(value: float) -> str:
    """Trim trailing zeros so 22.0 reads as ``22`` and 0.05 stays ``0.05``."""
    s = f"{value:,.2f}" if value >= 1 else f"{value:.4f}"
    return s.rstrip("0").rstrip(".") or "0"


def _render_gas(
    snaps: tuple[ChainGas, ...],
    prices: dict[str, float],
    brand_name: str,
    emojis: PremiumEmojis | None = None,
) -> str:
    """HTML body for ``/gas``: live oracle readings across chains."""
    e = emojis if emojis is not None else default_premium_emojis()
    lines: list[str] = [
        f"{e.gas} <b>Gas</b> — <i>Live Rates</i>",
        "",
    ]
    if not snaps:
        lines.append("<i>No gas data available right now.</i>")
        lines.append("")
        lines.append(f"<i>Data: Etherscan · {escape(brand_name)}</i>")
        return "\n".join(lines)
    for snap in snaps:
        px = prices.get(snap.chain.price_symbol, 0.0)
        safe_usd = _gwei_to_usd(snap.tier.safe_gwei, px)
        std_usd = _gwei_to_usd(snap.tier.standard_gwei, px)
        fast_usd = _gwei_to_usd(snap.tier.fast_gwei, px)
        lines.append(f"<b>{escape(snap.chain.name)}</b>")
        lines.append(
            f"  Safe: <code>{_fmt_gwei(snap.tier.safe_gwei)}</code> gwei"
            f"{_fmt_gas_usd(safe_usd)}"
        )
        lines.append(
            f"  Standard: <code>{_fmt_gwei(snap.tier.standard_gwei)}</code> gwei"
            f"{_fmt_gas_usd(std_usd)}"
        )
        lines.append(
            f"  Fast: <code>{_fmt_gwei(snap.tier.fast_gwei)}</code> gwei"
            f"{_fmt_gas_usd(fast_usd)}"
        )
        lines.append("")
    lines.append(
        "<i>USD estimates assume a 21k-gas native transfer.</i>"
    )
    lines.append(f"<i>Data: Etherscan · {escape(brand_name)}</i>")
    return "\n".join(lines)


async def cmd_gas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with a multichain gas-oracle card."""
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    etherscan: EtherscanClient | None = context.bot_data.get("etherscan")
    if etherscan is None or not etherscan.is_configured():
        await msg.reply_text(
            "Gas lookup needs an Etherscan API key.\n"
            "Set <code>ETHERSCAN_API_KEY</code> in your environment "
            "(free key at <a href=\"https://etherscan.io/apis\">"
            "etherscan.io/apis</a>) and restart the bot.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return
    paprika: CoinPaprikaClient | None = context.bot_data.get("paprika")
    await _typing(context, chat.id)
    needed_symbols = {c.price_symbol for c in CHAINS}
    snaps, prices = await asyncio.gather(
        etherscan.fetch_all_gas(),
        _fetch_native_prices(paprika, needed_symbols),
    )
    settings: Settings = context.bot_data["settings"]
    emojis = _resolve_premium_emojis(settings)
    await msg.reply_text(
        _render_gas(snaps, prices, settings.brand_name, emojis),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=_brand_keyboard(settings),
    )


def _extract_custom_emoji_ids(msg: Message | None) -> list[tuple[str, str]]:
    """Return ``(emoji_char, custom_emoji_id)`` pairs from ``msg``.

    Looks at both ``entities`` (regular messages) and ``caption_entities``
    (media captions). The emoji character is sliced out of the text or
    caption using the entity's offset/length so the user can see which
    emoji each ID belongs to in the reply.
    """
    if msg is None:
        return []
    pairs: list[tuple[str, str]] = []
    for text, entities in (
        (msg.text or "", msg.entities or ()),
        (msg.caption or "", msg.caption_entities or ()),
    ):
        if not text or not entities:
            continue
        for entity in entities:
            if entity.type != MessageEntity.CUSTOM_EMOJI:
                continue
            if not entity.custom_emoji_id:
                continue
            char = text[entity.offset : entity.offset + entity.length] or "?"
            pairs.append((char, entity.custom_emoji_id))
    return pairs


async def cmd_emojiid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with the ``custom_emoji_id`` of any Premium emoji in the
    quoted/current message.

    Usage: reply to a message that contains one or more Telegram
    Premium custom emojis with ``/emojiid``. The bot replies with each
    emoji and its numeric ID so the operator can paste it into the
    ``BRAND_*_EMOJI_ID`` environment variables.
    """
    msg = update.effective_message
    if msg is None:
        return
    target = msg.reply_to_message if msg.reply_to_message is not None else msg
    pairs = _extract_custom_emoji_ids(target)
    if not pairs:
        await msg.reply_text(
            "No Telegram Premium custom emoji found.\n"
            "Reply to a message that contains a premium emoji with "
            "<code>/emojiid</code> to get its ID.",
            parse_mode=ParseMode.HTML,
        )
        return
    # De-duplicate while preserving order so repeated emojis don't
    # print the same ID twice in a row.
    seen: set[str] = set()
    lines: list[str] = []
    for char, eid in pairs:
        if eid in seen:
            continue
        seen.add(eid)
        lines.append(f"{char} → <code>{escape(eid)}</code>")
    body = "<b>Custom emoji IDs</b>\n" + "\n".join(lines)
    await msg.reply_text(body, parse_mode=ParseMode.HTML)


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


# ---------------------------------------------------------------------------
# Edit-to-edit helpers: send a new reply on first message, edit the prior
# reply on edited-message updates. Plain ``msg.reply_*`` would create
# duplicate replies whenever the user fixes a typo in the original.
# ---------------------------------------------------------------------------


def _is_edited_update(update: Update) -> bool:
    """True when the update represents an edited user message."""
    return update.edited_message is not None


async def _delete_silently(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int
) -> None:
    # Reply might have been deleted by the user already; ignore.
    with contextlib.suppress(TelegramError):
        await context.bot.delete_message(chat_id, message_id)


def _record_reply(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_msg_id: int,
    bot_msg_id: int,
    kind: ReplyKind,
) -> None:
    store: EditableReplyStore | None = context.bot_data.get("edit_store")
    if store is None:
        return
    store.record(chat_id, user_msg_id, bot_msg_id=bot_msg_id, kind=kind)


async def _send_text_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    body: str,
    *,
    parse_mode: str | None = None,
    disable_web_page_preview: bool = False,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | None:
    """Send a text reply, editing the bot's prior reply on EDITED updates.

    When the update is an edit of a message we previously replied to:

    * If the prior reply was also text → edit it in place via
      ``edit_message_text``.
    * If the prior reply was a photo → delete it and send a fresh
      text reply so the message type can change.

    On a brand-new (non-edit) message, just sends a normal reply.
    Either way, records the resulting bot message id so future edits
    can find it.
    """
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return None

    store: EditableReplyStore | None = context.bot_data.get("edit_store")
    if _is_edited_update(update) and store is not None:
        existing = store.get(chat.id, msg.message_id)
        if existing is not None:
            bot_msg_id, kind = existing
            if kind == "text":
                try:
                    edited = await context.bot.edit_message_text(
                        chat_id=chat.id,
                        message_id=bot_msg_id,
                        text=body,
                        parse_mode=parse_mode,
                        disable_web_page_preview=disable_web_page_preview,
                        reply_markup=reply_markup,
                    )
                    if isinstance(edited, Message):
                        return edited
                    return None
                except BadRequest as exc:
                    # "message is not modified" just means the body
                    # didn't change. Treat as a no-op success.
                    if "not modified" in str(exc).lower():
                        return None
                    logger.warning("edit_message_text failed: %s", exc)
                    # Fall through to send a fresh reply below.
                except TelegramError as exc:
                    logger.warning("edit_message_text errored: %s", exc)
            else:
                # Was a photo; replying with text means deleting the
                # photo and sending a new text message in its place.
                await _delete_silently(context, chat.id, bot_msg_id)
                store.pop(chat.id, msg.message_id)

    try:
        sent = await msg.reply_text(
            body,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
            reply_markup=reply_markup,
        )
    except TelegramError as exc:
        logger.warning("reply_text failed: %s", exc)
        return None
    _record_reply(context, chat.id, msg.message_id, sent.message_id, "text")
    return sent


async def _send_photo_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    photo: bytes,
    *,
    caption: str,
    parse_mode: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | None:
    """Photo equivalent of :func:`_send_text_reply` — on edits, swaps the
    photo and caption of the prior reply via ``edit_message_media``."""
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return None

    store: EditableReplyStore | None = context.bot_data.get("edit_store")
    if _is_edited_update(update) and store is not None:
        existing = store.get(chat.id, msg.message_id)
        if existing is not None:
            bot_msg_id, kind = existing
            if kind == "photo":
                try:
                    media = InputMediaPhoto(
                        media=io.BytesIO(photo),
                        caption=caption,
                        parse_mode=parse_mode,
                    )
                    edited = await context.bot.edit_message_media(
                        chat_id=chat.id,
                        message_id=bot_msg_id,
                        media=media,
                        reply_markup=reply_markup,
                    )
                    if isinstance(edited, Message):
                        return edited
                    return None
                except TelegramError as exc:
                    logger.warning("edit_message_media failed: %s", exc)
                    # Fall through to send a fresh reply below.
            else:
                await _delete_silently(context, chat.id, bot_msg_id)
                store.pop(chat.id, msg.message_id)

    try:
        sent = await context.bot.send_photo(
            chat_id=chat.id,
            photo=io.BytesIO(photo),
            caption=caption,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            reply_to_message_id=msg.message_id,
        )
    except TelegramError as exc:
        logger.warning("send_photo failed: %s", exc)
        return None
    _record_reply(context, chat.id, msg.message_id, sent.message_id, "photo")
    return sent


# Matches ``/p btc``, ``/price BTC``, ``/p@MyBot eth`` — used by the
# edited-message handler to spot a price-command edit (CommandHandler
# itself doesn't fire on EDITED_MESSAGE updates).
_PRICE_CMD_RE = re.compile(
    r"^/(?:p|price)(?:@\w+)?\s+(?P<symbol>\S+)\s*$", re.IGNORECASE
)


def _parse_price_command(text: str) -> str | None:
    """Return the symbol from ``/p SYMBOL`` / ``/price SYMBOL`` or None."""
    match = _PRICE_CMD_RE.match(text.strip())
    if not match:
        return None
    return match.group("symbol").lstrip("$").strip() or None


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


async def on_edited_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Re-run calc / symbol / ``/p`` logic on an edited user message and
    edit the bot's previous reply instead of sending a new one.

    Telegram's ``CommandHandler`` ignores edited messages, so we handle
    ``/p SYMBOL`` here too rather than registering a second
    CommandHandler that would still need its own dispatch.
    """
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

    # ``/p SYMBOL`` edits route through the same price-card pipeline as
    # the normal command.
    cmd_symbol = _parse_price_command(text)
    if cmd_symbol is not None:
        await _send_card(
            update,
            context,
            symbol=cmd_symbol,
            timeframe=DEFAULT_TIMEFRAME,
            notify_if_unknown=True,
        )
        return

    # Calc / FX edits.
    if await _handle_calc(update, context, text):
        return

    # Free-text symbol edits (e.g. ``btx`` → ``btc``).
    match = _SYMBOL_RE.match(text)
    if not match:
        return
    symbol = match.group(1)
    if symbol.lower() in _STOP_WORDS:
        return
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


async def _compute_calc_line(
    parsed: tuple[str, str | None, str | None],
    *,
    in_group: bool,
    fx: FxClient,
    service: CoinService,
) -> tuple[str | None, bool]:
    """Evaluate one parsed calc line.

    Returns ``(body_html, claimed)``:

    * ``body_html`` is the HTML reply body for this line (or ``None`` when
      the line is silently dropped — e.g. a bare ``50%`` in a group).
    * ``claimed`` is ``True`` when the calc layer should be considered to
      have handled the line (success, math error, FX failure, …) and
      ``False`` when the caller should fall through to other handlers.

    The original :func:`_handle_calc` body called ``reply_text`` directly
    at six different points; this helper consolidates that into "return
    the body string" so callers can choose between sending a single
    reply (single-line input) or joining multiple bodies (multi-line
    input) before hitting Telegram.
    """
    expr, ccy1, ccy2 = parsed

    has_op = any(c in expr for c in _CALC_OPS)
    has_strong_op = any(c in expr for c in _STRONG_CALC_OPS)
    has_ccy = ccy1 is not None
    if not has_op and not has_ccy:
        # Bare number with no operator and no currency — nothing useful to
        # echo back; let it drop silently.
        return None, False

    if in_group and not has_strong_op and not has_ccy:
        # In groups, a bare ``50%`` (no other operator, no currency) is
        # almost always casual conversation rather than a math request.
        return None, False

    try:
        value = safe_eval(expr)
    except CalcError as exc:
        if in_group and not has_strong_op and not has_ccy:
            return None, False
        return f"⚠️ Math error: {escape(str(exc))}", True

    expr_pretty = " ".join(expr.split())
    if not has_ccy:
        body = (
            f"<code>{escape(expr_pretty)}</code>\n"
            f"= <code>{_fmt_amount(value)}</code>"
        )
        return body, True

    # 1 currency given → ``ccy1 → USD`` (the named currency is what the
    # user has; USD is the implicit quote). 2 given → ``ccy1 → ccy2``.
    assert ccy1 is not None
    if ccy2 is None:
        from_ccy, to_ccy = ccy1.lower(), "usd"
    else:
        from_ccy, to_ccy = ccy1.lower(), ccy2.lower()

    try:
        converted = await convert_with_fallback(
            fx, service, value, from_ccy, to_ccy
        )
    except Exception:  # noqa: BLE001
        logger.exception("fx: convert raised for %s -> %s", from_ccy, to_ccy)
        return (
            "⚠️ Couldn't reach the currency rates service. "
            "Try again in a moment."
        ), True

    if converted is None:
        return (
            f"⚠️ Unsupported currency pair: "
            f"<b>{escape(from_ccy.upper())}</b> → "
            f"<b>{escape(to_ccy.upper())}</b>"
        ), True

    from_str = f"{_fmt_amount(value)} {from_ccy.upper()}"
    to_str = f"{_fmt_amount(converted)} {to_ccy.upper()}"
    if has_op:
        header = f"<code>{escape(expr_pretty)}</code> = {escape(from_str)}"
    else:
        header = escape(from_str)
    return f"{header}\n≈ <code>{escape(to_str)}</code>", True


async def _handle_calc(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> bool:
    """Try to interpret ``text`` as math/conversion. Returns True if handled.

    Supports both single-line input (``2+2``, ``100 usd egp``,
    ``2+2/4 btc``) and multi-line input where each non-empty line parses
    independently (``2/1\\n2*2`` → both results in one reply).
    """
    msg = update.effective_message
    if msg is None:
        return False

    chat = update.effective_chat
    in_group = chat is not None and chat.type in {
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    }
    fx: FxClient = context.bot_data["fx"]
    service: CoinService = context.bot_data["service"]

    # Multi-line: only kick in when every non-empty line independently
    # parses as a calc expression. Otherwise fall through to single-line
    # handling so messages like "Hey 2+2" don't get half-interpreted.
    raw_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(raw_lines) > 1:
        parsed_lines = [parse_calc_input(ln) for ln in raw_lines]
        if all(p is not None for p in parsed_lines):
            bodies: list[str] = []
            for parsed in parsed_lines:
                assert parsed is not None
                body, claimed = await _compute_calc_line(
                    parsed, in_group=in_group, fx=fx, service=service
                )
                if claimed and body is not None:
                    bodies.append(body)
            if bodies:
                # Blank line between each line's result so the reply is
                # easy to scan when the user submitted several
                # operations at once.
                await _send_text_reply(
                    update,
                    context,
                    "\n\n".join(bodies),
                    parse_mode=ParseMode.HTML,
                )
                return True
            # No line produced a body (every line dropped silently);
            # fall through to single-line which will also drop silently.

    parsed = parse_calc_input(text)
    if parsed is None:
        return False

    body, claimed = await _compute_calc_line(
        parsed, in_group=in_group, fx=fx, service=service
    )
    if not claimed:
        return False
    if body is None:
        return True
    await _send_text_reply(update, context, body, parse_mode=ParseMode.HTML)
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
    caption = render_price_card(md, _resolve_premium_emojis(settings))
    keyboard = _build_keyboard(ref=ref, active=tf, settings=settings)
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

    body = render_price_card(md, _resolve_premium_emojis(settings))
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
        reply_markup=_brand_keyboard(settings),
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
            await _send_text_reply(
                update,
                context,
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
            await _send_text_reply(
                update,
                context,
                f"Data error: {escape(str(exc))}",
                parse_mode=ParseMode.HTML,
            )
            return
        caption = render_price_card(md, _resolve_premium_emojis(settings))
        await _send_text_reply(
            update,
            context,
            caption,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=_brand_keyboard(settings),
        )
        return

    try:
        md, candles = await asyncio.gather(
            service.market(ref),
            service.candles(ref=ref, timeframe=timeframe),
        )
    except CoinNotFound as exc:
        await _send_text_reply(
            update,
            context,
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
    caption = render_price_card(md, _resolve_premium_emojis(settings))
    keyboard = _build_keyboard(ref=ref, active=timeframe, settings=settings)
    await _send_photo_reply(
        update,
        context,
        png,
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# Unicode "Mathematical Sans-Serif Bold" code-point bases. Telegram
# inline-keyboard button labels are plain strings — there's no HTML/MD
# parse_mode — so the only way to render the name in bold is to swap
# each ASCII letter/digit for its bold-look-alike in this block.
# Modern Telegram clients render these glyphs as a clean bold sans-serif
# on every platform (iOS/Android/Desktop/Web).
_BOLD_UPPER_BASE = 0x1D5D4  # '𝗔'
_BOLD_LOWER_BASE = 0x1D5EE  # '𝗮'
_BOLD_DIGIT_BASE = 0x1D7EC  # '𝟬'


def _bold_label(text: str) -> str:
    """Return ``text`` with ASCII letters and digits promoted to
    Unicode sans-serif bold so the label looks heavier inside an
    inline-keyboard button.

    Spaces, punctuation, emoji and any non-ASCII characters pass
    through unchanged so the function is safe to call on labels that
    already contain emoji or Arabic text.
    """
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if 0x41 <= code <= 0x5A:  # 'A'..'Z'
            out.append(chr(_BOLD_UPPER_BASE + code - 0x41))
        elif 0x61 <= code <= 0x7A:  # 'a'..'z'
            out.append(chr(_BOLD_LOWER_BASE + code - 0x61))
        elif 0x30 <= code <= 0x39:  # '0'..'9'
            out.append(chr(_BOLD_DIGIT_BASE + code - 0x30))
        else:
            out.append(ch)
    return "".join(out)


def _brand_button(
    name: str,
    *,
    fallback_emoji: str,
    url: str,
    custom_emoji_id: str,
) -> InlineKeyboardButton:
    """Build a single brand-row button.

    The label is always rendered in Unicode bold for visual weight.
    When ``custom_emoji_id`` is non-empty, ``icon_custom_emoji_id`` is
    attached and the leading ``fallback_emoji`` is omitted from the
    text so we don't show two emojis side by side. With no custom
    emoji configured, the fallback emoji stays in front of the name as
    a visual marker.

    Bot API 9.4 (Feb 9 2026) added ``icon_custom_emoji_id`` to
    ``InlineKeyboardButton``. PTB 21.6 doesn't expose it as a named
    kwarg yet, but anything in ``api_kwargs`` is serialised straight
    onto the wire — so we slip it in there. The field is only honoured
    when the bot owner has Telegram Premium (or the bot has a
    Fragment-purchased username); without that the Telegram server
    ignores the field, so the empty fallback is always safe.
    """
    bold_name = _bold_label(name)
    cleaned = custom_emoji_id.strip()
    if cleaned:
        text = bold_name
        api_kwargs: dict[str, str] | None = {"icon_custom_emoji_id": cleaned}
    else:
        text = f"{fallback_emoji} {bold_name}"
        api_kwargs = None
    return InlineKeyboardButton(text, url=url, api_kwargs=api_kwargs)


def _brand_buttons(settings: Settings) -> list[InlineKeyboardButton]:
    """Two URL buttons that surface the brand's channel and chat shortcuts.

    Returned as a flat list so callers can compose them with their own
    rows (e.g. timeframe buttons above brand buttons on price cards).
    When ``BRAND_CHANNEL_EMOJI_ID`` / ``BRAND_GROUP_EMOJI_ID`` are set
    in the environment, each button shows the corresponding Telegram
    Premium custom emoji as its leading icon and drops the fallback
    text emoji (📣 / 💬) to avoid duplicate-icon clutter.
    """
    return [
        _brand_button(
            settings.channel_name,
            fallback_emoji="📣",
            url=settings.telegram_channel_url,
            custom_emoji_id=settings.brand_channel_emoji_id,
        ),
        _brand_button(
            settings.group_name,
            fallback_emoji="💬",
            url=settings.telegram_group_url,
            custom_emoji_id=settings.brand_group_emoji_id,
        ),
    ]


def _brand_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    """Standalone brand keyboard for messages without their own buttons."""
    return InlineKeyboardMarkup([_brand_buttons(settings)])


def _build_keyboard(
    *, ref: CoinRef, active: Timeframe, settings: Settings
) -> InlineKeyboardMarkup:
    row: list[InlineKeyboardButton] = []
    for tf in TIMEFRAMES:
        label = f"✅ {tf.label}" if tf.code == active.code else tf.label
        row.append(
            InlineKeyboardButton(
                label, callback_data=f"tf:{tf.code}:{ref.source}:{ref.symbol}"
            )
        )
    # Timeframe row on top, brand row underneath, so the most-used
    # interaction (switching timeframe) stays the first thing the user's
    # thumb lands on.
    return InlineKeyboardMarkup([row, _brand_buttons(settings)])


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

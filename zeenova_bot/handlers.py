"""Telegram command, message, and callback handlers."""

from __future__ import annotations

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from html import escape

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .card import render_price_card
from .chart import render_candles
from .config import Settings
from .services import CoinNotFound, CoinRef, CoinService
from .timeframes import DEFAULT_TIMEFRAME, TIMEFRAMES, Timeframe, get_timeframe

logger = logging.getLogger(__name__)

# Free-text symbols are short alphanumeric tokens. Anything with spaces or
# punctuation is ignored to keep group chatter from triggering the bot.
_SYMBOL_RE = re.compile(r"^\$?([A-Za-z][A-Za-z0-9]{1,11})$")

# matplotlib + mplfinance touch pyplot's global figure manager, which is not
# thread-safe. We render charts off the event loop on a single worker thread
# so concurrent updates serialize safely while still freeing the loop.
_CHART_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="zeenova-chart")

_HELP_TEXT = (
    "<b>Zeenova Coin Info Bot</b>\n\n"
    "Send a coin symbol (e.g. <code>BTC</code>, <code>$ETH</code>, <code>MEGA</code>) "
    "and I'll reply with a chart and live market data.\n\n"
    "Commands:\n"
    "• <code>/p SYMBOL</code> — explicit lookup (works in DM and groups)\n"
    "• <code>/start</code>, <code>/help</code> — this message\n\n"
    "Tip for groups: ask the admin to disable my Privacy Mode in @BotFather "
    "so I can react to plain symbols, or just use <code>/p SYMBOL</code>."
)


def build_application(
    settings: Settings, service: CoinService
) -> Application:  # type: ignore[type-arg]
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(True)
        .build()
    )
    app.bot_data["settings"] = settings
    app.bot_data["service"] = service

    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler(["p", "price"], cmd_price))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED, on_text
        )
    )
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^tf:"))
    app.add_error_handler(on_error)
    return app


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=_HELP_TEXT,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
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
    await _send_card(update, context, symbol=symbol, timeframe=DEFAULT_TIMEFRAME)


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
    match = _SYMBOL_RE.match(text)
    if not match:
        return
    symbol = match.group(1)
    if symbol.lower() in _STOP_WORDS:
        return
    await _send_card(update, context, symbol=symbol, timeframe=DEFAULT_TIMEFRAME)


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
        brand_name=settings.brand_name,
        channel_url=settings.telegram_channel_url,
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
    except Exception:  # noqa: BLE001
        # Telegram refuses edits when content is identical; ignore.
        logger.debug("edit_message_media failed", exc_info=True)


async def _send_card(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    symbol: str,
    timeframe: Timeframe,
) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return

    service: CoinService = context.bot_data["service"]
    settings: Settings = context.bot_data["settings"]

    ref = await service.resolve(symbol)
    if ref is None:
        await msg.reply_text(
            "Couldn't find a USDT pair for "
            f"<b>{escape(symbol.upper())}</b> on Binance, Bybit, or MEXC.",
            parse_mode=ParseMode.HTML,
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
        brand_name=settings.brand_name,
        channel_url=settings.telegram_channel_url,
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

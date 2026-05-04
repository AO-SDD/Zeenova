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
from telegram.error import BadRequest
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

def _help_text(settings: Settings) -> str:
    """Build the /start and /help message body from runtime settings."""
    return (
        f"<b>📈 {escape(settings.brand_name)}</b>\n"
        "<i>Real-time crypto prices &amp; candlestick charts.</i>\n\n"
        "<b>Usage</b>\n"
        "• Send any coin symbol — e.g. <code>BTC</code>, <code>$ETH</code>, <code>MEGA</code>\n"
        "• Or use <code>/p SYMBOL</code> for an explicit lookup\n"
        "• Tap <b>15M</b> · <b>1H</b> · <b>4H</b> · <b>1D</b> to switch timeframe\n\n"
        "<b>Data sources</b>\n"
        "Binance · Bybit · MEXC · CoinPaprika\n\n"
        f'📣 <a href="{escape(settings.telegram_channel_url, quote=True)}">'
        f"<b>{escape(settings.channel_name)}</b></a>"
        "   |   "
        f'💬 <a href="{escape(settings.telegram_group_url, quote=True)}">'
        f"<b>{escape(settings.group_name)}</b></a>"
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
    settings: Settings = context.application.bot_data["settings"]
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=_help_text(settings),
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
    # Explicit command → keep a short "not found" reply so users know the
    # command was received.
    await _send_card(
        update,
        context,
        symbol=symbol,
        timeframe=DEFAULT_TIMEFRAME,
        notify_if_unknown=True,
    )


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
    # Free-text matches stay silent on misses — a stray word in a chat
    # shouldn't generate a "not found" reply that pollutes the room.
    await _send_card(
        update,
        context,
        symbol=symbol,
        timeframe=DEFAULT_TIMEFRAME,
        notify_if_unknown=False,
    )


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

    ref = await service.resolve(symbol)
    if ref is None:
        if notify_if_unknown:
            await msg.reply_text(
                f"<b>{escape(symbol.upper())}</b> not listed on Binance, Bybit, or MEXC.",
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

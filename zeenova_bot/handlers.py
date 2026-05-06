"""Telegram command, message, and callback handlers."""

from __future__ import annotations

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from html import escape
from typing import Final

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

from .calc import CalcError, safe_eval
from .calc import parse_input as parse_calc_input
from .card import render_price_card
from .chart import render_candles
from .config import Settings
from .fx import FxClient
from .services import CoinNotFound, CoinRef, CoinService
from .timeframes import DEFAULT_TIMEFRAME, TIMEFRAMES, Timeframe, get_timeframe

__all__ = [
    "build_application",
    "convert_with_fallback",
]

logger = logging.getLogger(__name__)

# Free-text symbols are short alphanumeric tokens. Anything with spaces or
# punctuation is ignored to keep group chatter from triggering the bot.
_SYMBOL_RE = re.compile(r"^\$?([A-Za-z][A-Za-z0-9]{1,11})$")

# Operators that signal a clear calculator intent. If a free-text message
# contains at least one of these, we always reply with either a result or
# a parse error — silently dropping it would leave the user wondering.
_CALC_OPS = set("+-*/%^")

# matplotlib + mplfinance touch pyplot's global figure manager, which is not
# thread-safe. We render charts off the event loop on a single worker thread
# so concurrent updates serialize safely while still freeing the loop.
_CHART_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="zeenova-chart")

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
        f"<b>📈 {escape(settings.brand_name)}</b>\n"
        "<i>Real-time crypto prices, candlestick charts, "
        "and a built-in calculator with currency conversion.</i>\n\n"
        "<b>Prices &amp; charts</b>\n"
        "• Send any coin symbol — e.g. <code>BTC</code>, <code>$ETH</code>, <code>MEGA</code>\n"
        "• Or use <code>/p SYMBOL</code> for an explicit lookup\n"
        "• Tap <b>15M</b> · <b>1H</b> · <b>4H</b> · <b>1D</b> to switch timeframe\n\n"
        "<b>Calculator &amp; conversion</b>\n"
        "• Math — <code>2+2/4</code>, <code>(1+2)*3</code>\n"
        "• Price a currency in USD — <code>300 btc</code> (BTC → USD), "
        "<code>2+2/4 eth</code>\n"
        "• Between any two currencies — <code>1 usd egp</code>, "
        "<code>5000 egp btc</code>, <code>1 eth btc</code>\n"
        "• Telegram Stars — <code>300 star</code>, "
        "<code>3 usd star</code>, <code>3 usdt star</code>\n\n"
        "<b>Data sources</b>\n"
        "Binance · Bybit · MEXC · CoinPaprika · fawazahmed0/currency-api\n\n"
        f'📣 <a href="{escape(settings.telegram_channel_url, quote=True)}">'
        f"<b>{escape(settings.channel_name)}</b></a>"
        "   |   "
        f'💬 <a href="{escape(settings.telegram_group_url, quote=True)}">'
        f"<b>{escape(settings.group_name)}</b></a>"
    )


def build_application(
    settings: Settings, service: CoinService, fx: FxClient
) -> Application:  # type: ignore[type-arg]
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(True)
        .build()
    )
    app.bot_data["settings"] = settings
    app.bot_data["service"] = service
    app.bot_data["fx"] = fx

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


async def _to_usd_rate(
    fx: FxClient, service: CoinService, ccy: str
) -> float | None:
    """How many USD = 1 unit of ``ccy``.

    Resolution order:

    1. ``USD`` and the entries in :data:`_KNOWN_USD_RATES` short-circuit.
    2. Live crypto exchange price (Binance/Bybit/MEXC). We check this
       *before* the FX feed because some 3-letter codes are reused
       between fiat and crypto — e.g. ``MNT`` is both Mongolian tugrik
       (fiat, ~$0.0003) and Mantle (crypto, ~$0.66). Users sending
       ``20 mnt`` to a price bot almost always mean the crypto.
    3. The fawazahmed0 FX feed for fiat-only symbols.
    """
    lower = ccy.lower()
    if lower == "usd":
        return 1.0
    if lower in _KNOWN_USD_RATES:
        return _KNOWN_USD_RATES[lower]
    crypto = await service.usd_rate(ccy.upper())
    if crypto is not None:
        return crypto
    return await fx.convert(1.0, ccy, "usd")


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
    has_ccy = ccy1 is not None
    if not has_op and not has_ccy:
        # Bare number with no operator and no currency — nothing useful to
        # echo back; let it drop silently.
        return False

    try:
        value = safe_eval(expr)
    except CalcError as exc:
        await msg.reply_text(f"⚠️ Math error: {exc}")
        return True

    expr_pretty = " ".join(expr.split())
    if not has_ccy:
        # Pure math.
        body = f"🧮 <code>{escape(expr_pretty)}</code>\n= <code>{_fmt_amount(value)}</code>"
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
        header = f"🧮 <code>{escape(expr_pretty)}</code> = {escape(from_str)}"
    else:
        header = f"💱 {escape(from_str)}"
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

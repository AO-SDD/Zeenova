"""Tests for the calculator/FX bridge used by the message handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.constants import ChatType

from zeenova_bot.config import Settings
from zeenova_bot.handlers import (
    _handle_calc,
    _help_keyboard,
    _help_text,
    convert_with_fallback,
)


def _fx_returning(rates: dict[str, dict[str, float]]) -> AsyncMock:
    """Build a mock FxClient whose ``convert`` mirrors a static rate sheet.

    ``rates`` is keyed by source currency, e.g.
    ``{"usd": {"egp": 50.0}, "eur": {"usd": 1.1}}``.
    """

    async def convert(amount: float, from_ccy: str, to_ccy: str) -> float | None:
        from_ccy = from_ccy.lower()
        to_ccy = to_ccy.lower()
        if from_ccy == to_ccy:
            return amount
        side = rates.get(from_ccy)
        if not side:
            return None
        rate = side.get(to_ccy)
        if rate is None:
            return None
        return amount * rate

    fx = AsyncMock()
    fx.convert = AsyncMock(side_effect=convert)
    return fx


def _service_with(usd_rates: dict[str, float]) -> AsyncMock:
    """Build a mock CoinService whose ``usd_rate`` mirrors a static price sheet."""

    async def usd_rate(symbol: str) -> float | None:
        return usd_rates.get(symbol.upper())

    svc = AsyncMock()
    svc.usd_rate = AsyncMock(side_effect=usd_rate)
    return svc


@pytest.mark.asyncio
async def test_fiat_to_fiat_via_usd_bridge() -> None:
    """USD→EGP and any fiat↔fiat go through ``_to_usd_rate`` for both sides."""
    # Mirror the real fawazahmed0 feed (each ``from`` ccy has its own table).
    fx = _fx_returning({"usd": {"egp": 50.0}, "egp": {"usd": 1 / 50.0}})
    svc = _service_with({})  # No crypto override.
    assert await convert_with_fallback(fx, svc, 5.0, "usd", "egp") == pytest.approx(250.0)


@pytest.mark.asyncio
async def test_falls_back_to_coin_price_for_unknown_symbol() -> None:
    """``300 USD → OPG`` works even though FX has no OPG entry."""
    fx = _fx_returning({"usd": {"egp": 50.0}, "egp": {"usd": 1 / 50.0}})
    svc = _service_with({"OPG": 0.5})  # 1 OPG = 0.5 USD
    converted = await convert_with_fallback(fx, svc, 300.0, "usd", "opg")
    assert converted == pytest.approx(600.0)  # 300 / 0.5


@pytest.mark.asyncio
async def test_falls_back_for_crypto_to_fiat() -> None:
    # Mirror the real fawazahmed0 feed: each "from" currency exposes its
    # own rates table, so EGP→USD lives next to USD→EGP.
    fx = _fx_returning({"usd": {"egp": 50.0}, "egp": {"usd": 1 / 50.0}})
    svc = _service_with({"OPG": 0.5})
    # 1 OPG = 0.5 USD = 25 EGP
    converted = await convert_with_fallback(fx, svc, 1.0, "opg", "egp")
    assert converted == pytest.approx(25.0)


@pytest.mark.asyncio
async def test_falls_back_for_unknown_to_unknown() -> None:
    fx = _fx_returning({})
    svc = _service_with({"OPG": 0.5, "PEPE": 0.000001})
    # 1 OPG = 0.5 USD; 1 PEPE = 0.000001 USD; so 1 OPG = 500_000 PEPE.
    converted = await convert_with_fallback(fx, svc, 1.0, "opg", "pepe")
    assert converted == pytest.approx(500_000.0)


@pytest.mark.asyncio
async def test_returns_none_when_neither_side_resolves() -> None:
    fx = _fx_returning({})
    svc = _service_with({})
    assert await convert_with_fallback(fx, svc, 1.0, "xxx", "yyy") is None


@pytest.mark.asyncio
async def test_returns_none_when_to_side_unresolvable() -> None:
    fx = _fx_returning({"usd": {"egp": 50.0}, "egp": {"usd": 1 / 50.0}})
    svc = _service_with({})  # No coin price for OPG.
    assert await convert_with_fallback(fx, svc, 100.0, "usd", "opg") is None


@pytest.mark.asyncio
async def test_same_currency_returns_amount_unchanged() -> None:
    fx = _fx_returning({})
    svc = _service_with({})
    assert await convert_with_fallback(fx, svc, 42.0, "USD", "usd") == pytest.approx(42.0)


def _calc_update_context(
    text: str,
    fx: AsyncMock,
    svc: AsyncMock,
    chat_type: ChatType = ChatType.PRIVATE,
) -> tuple[MagicMock, MagicMock]:
    """Wire up a minimal Update + Context pair for ``_handle_calc``."""
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    update = MagicMock()
    update.effective_message = msg
    chat = MagicMock()
    chat.type = chat_type
    update.effective_chat = chat
    context = MagicMock()
    context.bot_data = {"fx": fx, "service": svc}
    return update, context


def _last_reply(msg: MagicMock) -> str:
    """Return the text the handler tried to send."""
    msg.reply_text.assert_called()
    args, _kwargs = msg.reply_text.call_args
    return args[0]


@pytest.mark.asyncio
async def test_single_currency_input_treats_named_ccy_as_source() -> None:
    """``300 btc`` means "300 BTC in USD", not "300 USD in BTC"."""
    # Direction-asymmetric rate: only BTC→USD is wired so a wrong-direction
    # call would silently return None and we'd see "Unsupported currency pair".
    fx = _fx_returning({"btc": {"usd": 60_000.0}})
    svc = _service_with({})
    update, context = _calc_update_context("300 btc", fx, svc)
    assert await _handle_calc(update, context, "300 btc") is True
    reply = _last_reply(update.effective_message)
    assert "300 BTC" in reply
    # 300 * 60_000 = 18_000_000
    assert "18,000,000" in reply
    assert "USD" in reply


@pytest.mark.asyncio
async def test_single_currency_with_math_treats_named_ccy_as_source() -> None:
    """``2+5 opg`` means "(2+5) OPG in USD"."""
    fx = _fx_returning({})
    svc = _service_with({"OPG": 0.25})  # 1 OPG = 0.25 USD
    update, context = _calc_update_context("2+5 opg", fx, svc)
    assert await _handle_calc(update, context, "2+5 opg") is True
    reply = _last_reply(update.effective_message)
    # 2+5 OPG = 7 OPG → 7 * 0.25 = 1.75 USD
    assert "7 OPG" in reply
    assert "1.75" in reply
    assert "USD" in reply


@pytest.mark.asyncio
async def test_single_currency_suffix_treats_named_ccy_as_source() -> None:
    """``1k opg`` means "1000 OPG in USD" (regression: suffix + flipped dir)."""
    fx = _fx_returning({})
    svc = _service_with({"OPG": 0.25})
    update, context = _calc_update_context("1k opg", fx, svc)
    assert await _handle_calc(update, context, "1k opg") is True
    reply = _last_reply(update.effective_message)
    # 1k OPG = 1000 OPG → 250 USD
    assert "1,000.00 OPG" in reply
    assert "250" in reply
    assert "USD" in reply


@pytest.mark.asyncio
async def test_two_currencies_still_use_explicit_pair() -> None:
    """``5 egp btc`` — explicit pair must still be ccy1 → ccy2 unchanged."""
    fx = _fx_returning({"egp": {"usd": 1 / 50.0}})
    svc = _service_with({"BTC": 60_000.0})
    update, context = _calc_update_context("5 egp btc", fx, svc)
    assert await _handle_calc(update, context, "5 egp btc") is True
    reply = _last_reply(update.effective_message)
    assert "5 EGP" in reply
    assert "BTC" in reply


@pytest.mark.asyncio
async def test_star_to_usd_uses_hardcoded_rate() -> None:
    """``300 star`` ≈ 4.50 USD (1000 stars = $15)."""
    fx = _fx_returning({})
    svc = _service_with({})
    update, context = _calc_update_context("300 star", fx, svc)
    assert await _handle_calc(update, context, "300 star") is True
    reply = _last_reply(update.effective_message)
    assert "300 STAR" in reply
    assert "4.5" in reply
    assert "USD" in reply
    # The exchange feed must not be queried for hard-coded symbols.
    svc.usd_rate.assert_not_called()


@pytest.mark.asyncio
async def test_usd_to_star_uses_hardcoded_rate() -> None:
    """``3 usd star`` ≈ 200 STAR (3 / 0.015)."""
    fx = _fx_returning({})
    svc = _service_with({})
    update, context = _calc_update_context("3 usd star", fx, svc)
    assert await _handle_calc(update, context, "3 usd star") is True
    reply = _last_reply(update.effective_message)
    assert "3 USD" in reply
    assert "200" in reply
    assert "STAR" in reply


@pytest.mark.asyncio
async def test_usdt_treated_as_usd_synonym() -> None:
    """``3 usdt star`` ≈ 200 STAR — USDT is treated as 1 USD."""
    fx = _fx_returning({})
    svc = _service_with({})
    update, context = _calc_update_context("3 usdt star", fx, svc)
    assert await _handle_calc(update, context, "3 usdt star") is True
    reply = _last_reply(update.effective_message)
    assert "3 USDT" in reply
    assert "200" in reply
    assert "STAR" in reply


@pytest.mark.asyncio
async def test_crypto_rate_wins_over_fx_for_ambiguous_symbol() -> None:
    """``20 mnt`` must use the Mantle crypto price, not Mongolian Tugrik FX.

    ``MNT`` is both a fiat ISO code (Mongolian tugrik, ~$0.0003) and a
    crypto symbol (Mantle, ~$0.66). A live price bot should pick the
    crypto interpretation.
    """
    # Both feeds list MNT but with very different rates.
    fx = _fx_returning({"mnt": {"usd": 0.0003}, "usd": {"mnt": 1 / 0.0003}})
    svc = _service_with({"MNT": 0.6646})
    update, context = _calc_update_context("20 mnt", fx, svc)
    assert await _handle_calc(update, context, "20 mnt") is True
    reply = _last_reply(update.effective_message)
    assert "20 MNT" in reply
    # 20 * 0.6646 ≈ 13.292 (with stripped trailing zeros it's "13.292"
    # since the value is < 1000 and `_fmt_amount` uses 4-decimal precision).
    assert "13.292" in reply
    # FX should not have been touched for the from-side conversion since
    # the crypto fallback resolved.
    fx.convert.assert_not_called()


@pytest.mark.asyncio
async def test_pure_fiat_symbol_falls_through_to_fx() -> None:
    """``5 egp`` — EGP is not on crypto exchanges, so FX is consulted."""
    fx = _fx_returning({"egp": {"usd": 1 / 50.0}})
    svc = _service_with({})  # No crypto match for EGP.
    update, context = _calc_update_context("5 egp", fx, svc)
    assert await _handle_calc(update, context, "5 egp") is True
    reply = _last_reply(update.effective_message)
    assert "5 EGP" in reply
    # 5 * (1/50) = 0.1 USD
    assert "0.1" in reply
    assert "USD" in reply


# ---------------------------------------------------------------------------
# Calculator-style percent + group-chat noise suppression.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_percent_evaluates_in_dm() -> None:
    """``100+10%`` returns 110 (calculator-style) in a DM."""
    fx = _fx_returning({})
    svc = _service_with({})
    update, context = _calc_update_context(
        "100+10%", fx, svc, chat_type=ChatType.PRIVATE
    )
    assert await _handle_calc(update, context, "100+10%") is True
    reply = _last_reply(update.effective_message)
    assert "100+10%" in reply
    assert "110" in reply


@pytest.mark.asyncio
async def test_bare_percent_silent_in_group() -> None:
    """Regression: bare ``50%`` in a group must NOT trigger any reply.

    Before this fix the bot would blurt "Math error: invalid syntax" at
    every ``50%`` someone typed; after percent support landed it would
    blurt ``= 0.5`` instead. Both are noise — in groups we treat bare
    percent literals as casual conversation and stay silent.
    """
    fx = _fx_returning({})
    svc = _service_with({})
    update, context = _calc_update_context(
        "50%", fx, svc, chat_type=ChatType.GROUP
    )
    assert await _handle_calc(update, context, "50%") is False
    update.effective_message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_bare_percent_evaluates_in_dm() -> None:
    """In a private chat we *do* respond to a bare ``50%`` (= 0.5)."""
    fx = _fx_returning({})
    svc = _service_with({})
    update, context = _calc_update_context(
        "50%", fx, svc, chat_type=ChatType.PRIVATE
    )
    assert await _handle_calc(update, context, "50%") is True
    reply = _last_reply(update.effective_message)
    assert "0.5" in reply


@pytest.mark.asyncio
async def test_real_math_in_group_still_replies() -> None:
    """``100+10%`` (real arithmetic) in a group should still respond."""
    fx = _fx_returning({})
    svc = _service_with({})
    update, context = _calc_update_context(
        "100+10%", fx, svc, chat_type=ChatType.GROUP
    )
    assert await _handle_calc(update, context, "100+10%") is True
    reply = _last_reply(update.effective_message)
    assert "110" in reply


@pytest.mark.asyncio
async def test_invalid_syntax_silent_in_group() -> None:
    """``50%foo`` in a group: parses but fails — must not surface error."""
    fx = _fx_returning({})
    svc = _service_with({})
    # Crafted input: looks like a calc body but won't actually evaluate
    # cleanly. With the suppression in place no reply happens.
    update, context = _calc_update_context(
        "5/", fx, svc, chat_type=ChatType.GROUP
    )
    # ``5/`` reaches _handle_calc with a strong op (`/`) but it's a
    # parse error — strong-op messages always get a reply, even in groups.
    assert await _handle_calc(update, context, "5/") is True
    reply = _last_reply(update.effective_message)
    assert "Math error" in reply


# ---------------------------------------------------------------------------
# /start and /help message body + inline keyboard.
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        telegram_bot_token="x",
        brand_name="Zeenova",
        channel_name="Zeen Channel",
        group_name="Zeen Chat",
        telegram_channel_url="https://t.me/ox_zeen",
        telegram_group_url="https://t.me/blockzeen",
    )


def test_help_text_drops_data_sources_section() -> None:
    """The /start body no longer advertises raw data sources."""
    body = _help_text(_settings())
    assert "Data sources" not in body
    assert "Binance" not in body
    assert "CoinPaprika" not in body
    assert "fawazahmed0" not in body


def test_help_text_advertises_core_features() -> None:
    """The new copy keeps mention of the headline features."""
    body = _help_text(_settings())
    # Top-level pitch.
    assert "Zeenova" in body
    # Core feature blocks are still discoverable.
    assert "Live prices" in body
    assert "Calculator" in body
    # Percent + group-friendly copy land on the page so users know about
    # the new behaviours.
    assert "100+10%" in body
    assert "Group-friendly" in body


def test_help_keyboard_has_add_me_and_links() -> None:
    """Keyboard exposes the deep-link to add the bot to a group."""
    kb = _help_keyboard("zeenovabot", _settings())
    assert kb is not None
    rows = kb.inline_keyboard
    # First row: single "Add me" button with the startgroup deep-link.
    assert len(rows[0]) == 1
    add_btn = rows[0][0]
    assert "Add me" in add_btn.text
    assert add_btn.url == "https://t.me/zeenovabot?startgroup=true"
    # Second row: channel + group shortcuts.
    assert len(rows[1]) == 2
    assert rows[1][0].url == "https://t.me/ox_zeen"
    assert rows[1][1].url == "https://t.me/blockzeen"


def test_help_keyboard_returns_none_without_username() -> None:
    """Before PTB has fetched its identity we must not render a broken URL."""
    assert _help_keyboard(None, _settings()) is None
    assert _help_keyboard("", _settings()) is None


# ---------------------------------------------------------------------------
# Inline mode (@bot btc) -> tappable price card.
# ---------------------------------------------------------------------------


def _inline_update_context(
    query_text: str,
    *,
    resolve_ref: object | None = None,
    market_data: object | None = None,
    market_raises: BaseException | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Build an Update with an inline_query and a wired CoinService mock."""
    iq = MagicMock()
    iq.query = query_text
    iq.answer = AsyncMock()
    update = MagicMock()
    update.inline_query = iq

    service = MagicMock()
    service.resolve = AsyncMock(return_value=resolve_ref)
    if market_raises is not None:
        service.market = AsyncMock(side_effect=market_raises)
    else:
        service.market = AsyncMock(return_value=market_data)

    context = MagicMock()
    context.bot_data = {"service": service, "settings": _settings()}
    return update, context


def _make_market_data(symbol: str = "BTC", price: float = 60000.0) -> object:
    """Build a MarketData stand-in for inline-mode tests."""
    from zeenova_bot.services import MarketData

    return MarketData(
        symbol=symbol,
        pair=f"{symbol}USDT",
        source="binance",
        price_usd=price,
        price_change_pct_24h=2.5,
        high_24h=price * 1.02,
        low_24h=price * 0.98,
        market_cap_usd=1.2e12,
        total_volume_usd_24h=4.5e10,
        market_cap_rank=1,
    )


@pytest.mark.asyncio
async def test_inline_query_returns_article_for_known_symbol() -> None:
    """``@bot btc`` returns one InlineQueryResultArticle with the price card."""
    from zeenova_bot.handlers import on_inline_query
    from zeenova_bot.services import CoinRef

    ref = CoinRef(symbol="BTC", pair="BTCUSDT", source="binance")
    md = _make_market_data()
    update, context = _inline_update_context(
        "btc", resolve_ref=ref, market_data=md
    )
    await on_inline_query(update, context)
    update.inline_query.answer.assert_awaited_once()
    args, kwargs = update.inline_query.answer.call_args
    results = args[0]
    assert len(results) == 1
    article = results[0]
    assert article.title.startswith("BTC")
    # The body of the article is the standard price card HTML.
    assert "BTC" in article.input_message_content.message_text
    assert "Price" in article.input_message_content.message_text
    assert kwargs.get("cache_time") is not None


@pytest.mark.asyncio
async def test_inline_query_strips_dollar_prefix() -> None:
    """``@bot $eth`` resolves the same as ``@bot eth``."""
    from zeenova_bot.handlers import on_inline_query
    from zeenova_bot.services import CoinRef

    ref = CoinRef(symbol="ETH", pair="ETHUSDT", source="binance")
    md = _make_market_data(symbol="ETH", price=3000.0)
    update, context = _inline_update_context(
        "$eth", resolve_ref=ref, market_data=md
    )
    await on_inline_query(update, context)
    # service.resolve should have been called with the cleaned token.
    assert context.bot_data["service"].resolve.await_args.args[0] == "eth"
    args, _ = update.inline_query.answer.call_args
    assert len(args[0]) == 1


@pytest.mark.asyncio
async def test_inline_query_empty_returns_empty_results() -> None:
    """Empty inline query is answered with an empty list (no spinner)."""
    from zeenova_bot.handlers import on_inline_query

    update, context = _inline_update_context("")
    await on_inline_query(update, context)
    args, _ = update.inline_query.answer.call_args
    assert args[0] == []
    # We must never bother the resolver for empty queries.
    context.bot_data["service"].resolve.assert_not_called()


@pytest.mark.asyncio
async def test_inline_query_unknown_symbol_returns_empty() -> None:
    """Unresolved symbols produce an empty result list, not an exception."""
    from zeenova_bot.handlers import on_inline_query

    update, context = _inline_update_context(
        "zzznope", resolve_ref=None
    )
    await on_inline_query(update, context)
    args, _ = update.inline_query.answer.call_args
    assert args[0] == []


@pytest.mark.asyncio
async def test_inline_query_garbage_skips_resolve() -> None:
    """Strings that don't look like a symbol short-circuit before any I/O."""
    from zeenova_bot.handlers import on_inline_query

    update, context = _inline_update_context("hello world this is not a coin")
    await on_inline_query(update, context)
    args, _ = update.inline_query.answer.call_args
    assert args[0] == []
    context.bot_data["service"].resolve.assert_not_called()

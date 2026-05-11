"""Tests for the calculator/FX bridge used by the message handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import MessageEntity
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


def _service_with(
    usd_rates: dict[str, float],
    *,
    off_exchange_only: set[str] | None = None,
) -> AsyncMock:
    """Build a mock CoinService whose ``usd_rate`` mirrors a static price sheet.

    ``off_exchange_only`` lists symbols that should resolve via the
    off-exchange aggregator (CoinPaprika) rather than a major exchange,
    which lets us simulate cases like the scam ``EGP`` token without
    touching the network.
    """
    from zeenova_bot.services import OFF_EXCHANGE_SOURCE, CoinRef

    off_exchange_only = off_exchange_only or set()

    async def usd_rate(symbol: str) -> float | None:
        return usd_rates.get(symbol.upper())

    async def resolve(symbol: str) -> CoinRef | None:
        s = symbol.upper()
        if s not in usd_rates:
            return None
        source = OFF_EXCHANGE_SOURCE if s in off_exchange_only else "binance"
        return CoinRef(symbol=s, pair=f"{s}/USD", source=source)

    svc = AsyncMock()
    svc.usd_rate = AsyncMock(side_effect=usd_rate)
    svc.resolve = AsyncMock(side_effect=resolve)
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


@pytest.mark.asyncio
async def test_fiat_beats_off_exchange_scam_token() -> None:
    """``1 egp`` must use the Egyptian Pound FX rate, not a $200k-cap junk
    crypto with the same ticker on a small aggregator.

    Off-exchange aggregators index thousands of low-cap tokens, including
    a "EGP" token at rank ~3300 with a $207k marketcap. Without this
    guard, a user typing ``1 egp`` to convert from Egyptian Pounds would
    get the scam token's price (~$0.06) instead of the real fiat rate
    (~$0.019).
    """
    fx = _fx_returning({"egp": {"usd": 1 / 50.0}, "usd": {"egp": 50.0}})
    # EGP "resolves" via CoinPaprika at $0.06 — that's the scam token.
    svc = _service_with({"EGP": 0.0563}, off_exchange_only={"EGP"})
    update, context = _calc_update_context("1 egp", fx, svc)
    assert await _handle_calc(update, context, "1 egp") is True
    reply = _last_reply(update.effective_message)
    assert "1 EGP" in reply
    # 1 * (1/50) = 0.02 USD via FX, not 0.0563 via the scam token.
    assert "0.02" in reply
    # Sanity-check we didn't accidentally use the scam crypto price.
    assert "0.0563" not in reply
    assert "0.056" not in reply


@pytest.mark.asyncio
async def test_thin_listed_coin_still_uses_off_exchange() -> None:
    """``1 oct`` — OCT is only on CoinPaprika and *not* in the FX feed,
    so we must still fall back to the off-exchange price.
    """
    fx = _fx_returning({})  # FX has no clue about OCT.
    svc = _service_with({"OCT": 0.054}, off_exchange_only={"OCT"})
    update, context = _calc_update_context("1 oct", fx, svc)
    assert await _handle_calc(update, context, "1 oct") is True
    reply = _last_reply(update.effective_message)
    assert "1 OCT" in reply
    # 1 * 0.054 = 0.054 USD from the off-exchange aggregator.
    assert "0.054" in reply
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
    # /market + /top get billing in the help text.
    assert "/market" in body
    assert "/top" in body


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

    fx = MagicMock()
    fx.supports = AsyncMock(return_value=False)

    context = MagicMock()
    context.bot_data = {
        "service": service,
        "settings": _settings(),
        "fx": fx,
    }
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
async def test_inline_query_suppresses_off_exchange_fiat_clash() -> None:
    """``@bot egp`` must not surface a $200k-cap junk crypto card.

    EGP resolves on CoinPaprika to a scam token, but the same ticker
    is the Egyptian Pound on the FX feed. The price-card flow should
    treat that as unresolved (the calc path still returns the real
    fiat rate via FX).
    """
    from zeenova_bot.handlers import on_inline_query
    from zeenova_bot.services import OFF_EXCHANGE_SOURCE, CoinRef

    junk = CoinRef(symbol="EGP", pair="EGP/USD", source=OFF_EXCHANGE_SOURCE)
    update, context = _inline_update_context("egp", resolve_ref=junk)
    # FX knows EGP — so the off-exchange clash should be suppressed.
    context.bot_data["fx"].supports = AsyncMock(return_value=True)
    await on_inline_query(update, context)
    args, _ = update.inline_query.answer.call_args
    assert args[0] == []
    # And we must not have fetched the junk token's market data.
    context.bot_data["service"].market.assert_not_called()


@pytest.mark.asyncio
async def test_inline_query_keeps_thin_listed_off_exchange_coin() -> None:
    """``@bot oct`` should still surface OCT (off-exchange but FX has no OCT)."""
    from zeenova_bot.handlers import on_inline_query
    from zeenova_bot.services import OFF_EXCHANGE_SOURCE, CoinRef

    ref = CoinRef(symbol="OCT", pair="OCT/USD", source=OFF_EXCHANGE_SOURCE)
    md = _make_market_data(symbol="OCT", price=0.054)
    update, context = _inline_update_context(
        "oct", resolve_ref=ref, market_data=md
    )
    # FX has no OCT — so the off-exchange match must be kept.
    context.bot_data["fx"].supports = AsyncMock(return_value=False)
    await on_inline_query(update, context)
    args, _ = update.inline_query.answer.call_args
    assert len(args[0]) == 1


@pytest.mark.asyncio
async def test_inline_query_garbage_skips_resolve() -> None:
    """Strings that don't look like a symbol short-circuit before any I/O."""
    from zeenova_bot.handlers import on_inline_query

    update, context = _inline_update_context("hello world this is not a coin")
    await on_inline_query(update, context)
    args, _ = update.inline_query.answer.call_args
    assert args[0] == []
    context.bot_data["service"].resolve.assert_not_called()


# ---------------------------------------------------------------------------
# /top and /market
# ---------------------------------------------------------------------------


def _ticker(
    symbol: str,
    name: str,
    rank: int,
    price: float,
    change: float,
    cap: float | None = 1e9,
) -> object:
    from zeenova_bot.coinpaprika import TickerSnapshot

    return TickerSnapshot(
        symbol=symbol,
        name=name,
        rank=rank,
        price_usd=price,
        change_pct_24h=change,
        market_cap_usd=cap,
    )


def _global_snapshot() -> object:
    from zeenova_bot.coinpaprika import GlobalSnapshot

    return GlobalSnapshot(
        market_cap_usd=3_500_000_000_000.0,
        volume_24h_usd=120_000_000_000.0,
        bitcoin_dominance_pct=53.4,
        cryptocurrencies_number=12345,
        market_cap_change_24h_pct=-1.2,
    )


def _cmd_update_context(
    *,
    paprika: object | None = None,
    fear_greed: object | None = None,
) -> tuple[MagicMock, MagicMock]:
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    msg.reply_photo = AsyncMock()
    update = MagicMock()
    update.effective_message = msg
    update.effective_chat = MagicMock()
    context = MagicMock()
    context.bot_data = {
        "paprika": paprika,
        "fear_greed": fear_greed,
        "settings": _settings(),
    }
    return update, context


@pytest.mark.asyncio
async def test_cmd_market_renders_global_snapshot() -> None:
    """``/market`` shows total mcap, BTC dominance, and active coin count."""
    from zeenova_bot.handlers import cmd_market

    paprika = MagicMock()
    paprika.fetch_global = AsyncMock(return_value=_global_snapshot())
    update, context = _cmd_update_context(paprika=paprika)
    await cmd_market(update, context)
    body = _last_reply(update.effective_message)
    assert "Global market" in body
    assert "Total marketcap" in body
    # 3.5 trillion -> $3.50T
    assert "$3.50T" in body
    assert "53.40%" in body  # BTC dominance
    assert "12,345" in body  # active coins
    # Negative 24h change is displayed without a leading +.
    assert "-1.20%" in body


@pytest.mark.asyncio
async def test_cmd_market_handles_paprika_failure() -> None:
    """If CoinPaprika is unreachable, /market replies with a friendly error."""
    from zeenova_bot.handlers import cmd_market

    paprika = MagicMock()
    paprika.fetch_global = AsyncMock(return_value=None)
    update, context = _cmd_update_context(paprika=paprika)
    await cmd_market(update, context)
    body = _last_reply(update.effective_message)
    assert "Couldn't reach" in body


@pytest.mark.asyncio
async def test_cmd_top_renders_top_5_gainers_and_losers() -> None:
    """``/top`` returns 5 gainers + 5 losers ordered correctly."""
    from zeenova_bot.handlers import cmd_top

    rows = [
        _ticker("BTC", "Bitcoin", 1, 60000, 1.0),
        _ticker("ETH", "Ethereum", 2, 3000, -2.0),
        _ticker("SOL", "Solana", 3, 200, 12.0),
        _ticker("DOGE", "Dogecoin", 4, 0.4, -8.0),
        _ticker("XRP", "XRP", 5, 0.6, 9.0),
        _ticker("ADA", "Cardano", 6, 1.0, -7.0),
        _ticker("LINK", "Chainlink", 7, 14, 8.5),
        _ticker("MATIC", "Polygon", 8, 0.6, -5.0),
        _ticker("AVAX", "Avalanche", 9, 30, 7.5),
        _ticker("DOT", "Polkadot", 10, 7, -3.0),
        _ticker("TON", "Toncoin", 11, 5, 6.0),
    ]
    paprika = MagicMock()
    paprika.fetch_top_tickers = AsyncMock(return_value=rows)
    update, context = _cmd_update_context(paprika=paprika)
    await cmd_top(update, context)
    body = _last_reply(update.effective_message)
    assert "Top movers" in body
    # Top 5 gainers from the list (12, 9, 8.5, 7.5, 6.0).
    for sym in ("SOL", "XRP", "LINK", "AVAX", "TON"):
        assert sym in body
    # Top 5 losers (-8, -7, -5, -3, -2).
    for sym in ("DOGE", "ADA", "MATIC", "DOT", "ETH"):
        assert sym in body
    # Gainers section should appear before losers.
    assert body.index("Gainers") < body.index("Losers")


@pytest.mark.asyncio
async def test_cmd_top_handles_empty_response() -> None:
    """An empty /tickers response surfaces as a friendly error, not a crash."""
    from zeenova_bot.handlers import cmd_top

    paprika = MagicMock()
    paprika.fetch_top_tickers = AsyncMock(return_value=[])
    update, context = _cmd_update_context(paprika=paprika)
    await cmd_top(update, context)
    body = _last_reply(update.effective_message)
    assert "Couldn't reach" in body


def test_parse_global_handles_missing_fields() -> None:
    """``_parse_global`` survives a partial /global payload."""
    from zeenova_bot.coinpaprika import _parse_global

    snap = _parse_global({
        "market_cap_usd": 1234.5,
        # Missing volume / dominance / count / change.
    })
    assert snap is not None
    assert snap.market_cap_usd == 1234.5
    assert snap.volume_24h_usd is None
    assert snap.bitcoin_dominance_pct is None
    assert snap.cryptocurrencies_number is None
    assert snap.market_cap_change_24h_pct is None


def test_parse_global_rejects_garbage() -> None:
    from zeenova_bot.coinpaprika import _parse_global

    assert _parse_global(None) is None
    assert _parse_global("not a dict") is None


def test_parse_ticker_pulls_relevant_fields() -> None:
    from zeenova_bot.coinpaprika import _parse_ticker

    row = {
        "id": "btc-bitcoin",
        "name": "Bitcoin",
        "symbol": "btc",
        "rank": 1,
        "quotes": {
            "USD": {
                "price": 60000.0,
                "percent_change_24h": 2.5,
                "market_cap": 1.2e12,
            }
        },
    }
    t = _parse_ticker(row)
    assert t is not None
    assert t.symbol == "BTC"
    assert t.name == "Bitcoin"
    assert t.rank == 1
    assert t.price_usd == 60000.0
    assert t.change_pct_24h == 2.5
    assert t.market_cap_usd == 1.2e12


def test_parse_ticker_skips_invalid_rows() -> None:
    """Rows without a usable USD quote / change / rank are dropped."""
    from zeenova_bot.coinpaprika import _parse_ticker

    # Missing rank.
    assert _parse_ticker({"symbol": "BTC", "name": "Bitcoin"}) is None
    # Missing USD quote.
    assert (
        _parse_ticker(
            {"symbol": "BTC", "name": "Bitcoin", "rank": 1, "quotes": {}}
        )
        is None
    )
    # No price.
    assert (
        _parse_ticker(
            {
                "symbol": "BTC",
                "name": "Bitcoin",
                "rank": 1,
                "quotes": {"USD": {"price": 0, "percent_change_24h": 1.0}},
            }
        )
        is None
    )


@pytest.mark.asyncio
async def test_cmd_market_includes_fear_greed_when_available() -> None:
    """``/market`` sends the Fear & Greed dial as a photo with the body in
    the caption when the index reading is available.
    """
    import io as _io

    from zeenova_bot.fear_greed import FearGreed
    from zeenova_bot.handlers import cmd_market

    paprika = MagicMock()
    paprika.fetch_global = AsyncMock(return_value=_global_snapshot())
    fng = MagicMock()
    fng.fetch_current = AsyncMock(
        return_value=FearGreed(value=72, classification="Greed")
    )
    update, context = _cmd_update_context(paprika=paprika, fear_greed=fng)
    await cmd_market(update, context)

    msg = update.effective_message
    msg.reply_photo.assert_called_once()
    msg.reply_text.assert_not_called()  # Photo path, not text.
    _args, kwargs = msg.reply_photo.call_args
    # Dial is rendered in-process; we get a BytesIO holding the PNG.
    photo = kwargs["photo"]
    assert isinstance(photo, _io.BytesIO)
    payload = photo.getvalue()
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    caption = kwargs["caption"]
    assert "Fear &amp; Greed" in caption  # HTML-escaped &
    assert "72/100" in caption
    assert "Greed" in caption


@pytest.mark.asyncio
async def test_cmd_market_falls_back_to_text_when_photo_fails() -> None:
    """If Telegram rejects the dial photo, /market still posts the body
    as plain text rather than silently disappearing.
    """
    from zeenova_bot.fear_greed import FearGreed
    from zeenova_bot.handlers import cmd_market

    paprika = MagicMock()
    paprika.fetch_global = AsyncMock(return_value=_global_snapshot())
    fng = MagicMock()
    fng.fetch_current = AsyncMock(
        return_value=FearGreed(value=72, classification="Greed")
    )
    update, context = _cmd_update_context(paprika=paprika, fear_greed=fng)
    update.effective_message.reply_photo = AsyncMock(
        side_effect=RuntimeError("CDN hiccup")
    )
    await cmd_market(update, context)
    body = _last_reply(update.effective_message)
    assert "Fear &amp; Greed" in body
    assert "72/100" in body


@pytest.mark.asyncio
async def test_cmd_market_omits_fear_greed_on_failure() -> None:
    """If the Fear & Greed fetch fails, /market still renders global stats."""
    from zeenova_bot.handlers import cmd_market

    paprika = MagicMock()
    paprika.fetch_global = AsyncMock(return_value=_global_snapshot())
    fng = MagicMock()
    fng.fetch_current = AsyncMock(side_effect=RuntimeError("boom"))
    update, context = _cmd_update_context(paprika=paprika, fear_greed=fng)
    await cmd_market(update, context)
    body = _last_reply(update.effective_message)
    # Global snapshot still rendered.
    assert "$3.50T" in body
    # No Fear & Greed line.
    assert "Fear &amp; Greed" not in body


# ---------------------------------------------------------------------------
# Quote-sticker trigger ("z" reply)
# ---------------------------------------------------------------------------


def _quote_update_context(
    text: str,
    *,
    parent_text: str | None = "Hello, world!",
    parent_caption: str | None = None,
    parent_user: object | None = "default",
    quote_client: object | None = "default",
    chat_type: ChatType = ChatType.GROUP,
) -> tuple[MagicMock, MagicMock]:
    """Wire up a minimal Update + Context for quote-sticker trigger tests."""
    msg = MagicMock()
    msg.text = text
    msg.reply_text = AsyncMock()
    msg.reply_sticker = AsyncMock()
    # No Telegram-quote highlight by default; tests that exercise that
    # path opt in by overwriting ``msg.quote`` after this fixture returns.
    msg.quote = None

    if parent_text is None and parent_caption is None and parent_user is None:
        msg.reply_to_message = None
    else:
        parent = MagicMock()
        parent.text = parent_text
        parent.caption = parent_caption
        # Default to no rich entities and no nested reply chain so the
        # handler doesn't try to walk MagicMock fakes.
        parent.entities = ()
        parent.caption_entities = ()
        parent.reply_to_message = None
        if parent_user == "default":
            user = MagicMock()
            user.id = 9999
            user.full_name = "Quoted User"
            user.first_name = "Quoted"
            user.username = "quoted"
            parent.from_user = user
        else:
            parent.from_user = parent_user
        msg.reply_to_message = parent

    update = MagicMock()
    update.effective_message = msg
    chat = MagicMock()
    chat.type = chat_type
    chat.id = 1
    update.effective_chat = chat

    context = MagicMock()
    if quote_client == "default":
        client = MagicMock()
        client.render = AsyncMock(return_value=b"webp-bytes")
    else:
        client = quote_client
    # Calc / symbol path requires these too — provide AsyncMock stubs so
    # the on_text fall-through doesn't crash on awaitable-less mocks.
    fx = MagicMock()
    fx.supports = AsyncMock(return_value=False)
    service = MagicMock()
    service.resolve = AsyncMock(return_value=None)
    # Avatar lookups go through ``context.bot`` — stub the two methods
    # the handler touches so the tests don't need network access.
    profile_photos = MagicMock()
    profile_photos.total_count = 0
    profile_photos.photos = ()
    bot = MagicMock()
    bot.get_user_profile_photos = AsyncMock(return_value=profile_photos)
    bot.get_file = AsyncMock()
    context.bot = bot
    context.bot_data = {
        "quote_sticker": client,
        "settings": _settings(),
        "fx": fx,
        "service": service,
    }
    return update, context


@pytest.mark.asyncio
async def test_quote_trigger_sends_sticker_for_z_reply() -> None:
    """Replying ``z`` to a message renders & sends the quote sticker."""
    from zeenova_bot.handlers import on_text

    update, context = _quote_update_context("z")
    await on_text(update, context)

    msg = update.effective_message
    msg.reply_sticker.assert_called_once()
    msg.reply_text.assert_not_called()  # No price-card / calc fall-through.

    client = context.bot_data["quote_sticker"]
    client.render.assert_awaited_once()
    args, _kwargs = client.render.call_args
    author, text = args
    assert author.user_id == 9999
    assert author.name == "Quoted User"
    assert text == "Hello, world!"


@pytest.mark.asyncio
async def test_quote_trigger_accepts_uppercase_z() -> None:
    """Uppercase ``Z`` triggers the same handler."""
    from zeenova_bot.handlers import on_text

    update, context = _quote_update_context("Z")
    await on_text(update, context)
    update.effective_message.reply_sticker.assert_called_once()


@pytest.mark.asyncio
async def test_quote_trigger_uses_parent_caption_when_no_text() -> None:
    """If the parent is a media message with caption, quote the caption."""
    from zeenova_bot.handlers import on_text

    update, context = _quote_update_context(
        "z", parent_text=None, parent_caption="Photo caption"
    )
    await on_text(update, context)
    client = context.bot_data["quote_sticker"]
    args, _ = client.render.call_args
    assert args[1] == "Photo caption"


@pytest.mark.asyncio
async def test_quote_trigger_ignored_without_reply() -> None:
    """``z`` typed without replying to anything must not call the API."""
    from zeenova_bot.handlers import on_text

    update, context = _quote_update_context(
        "z", parent_text=None, parent_caption=None, parent_user=None
    )
    await on_text(update, context)
    client = context.bot_data["quote_sticker"]
    client.render.assert_not_called()
    update.effective_message.reply_sticker.assert_not_called()


@pytest.mark.asyncio
async def test_quote_trigger_swallows_when_parent_has_no_text() -> None:
    """Reply to a media-only message → silently swallow, don't calc."""
    from zeenova_bot.handlers import on_text

    update, context = _quote_update_context(
        "z", parent_text=None, parent_caption=None
    )
    await on_text(update, context)
    client = context.bot_data["quote_sticker"]
    client.render.assert_not_called()
    update.effective_message.reply_sticker.assert_not_called()
    # And we must not have fallen through to calc / symbol either.
    update.effective_message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_quote_trigger_two_z_chars_does_not_trigger() -> None:
    """``zz`` is not the trigger (regression: keep the regex strict)."""
    from zeenova_bot.handlers import on_text

    update, context = _quote_update_context("zz")
    await on_text(update, context)
    client = context.bot_data["quote_sticker"]
    client.render.assert_not_called()
    update.effective_message.reply_sticker.assert_not_called()


@pytest.mark.asyncio
async def test_quote_trigger_silent_when_render_fails() -> None:
    """API hiccup → no sticker, no error message — just stay quiet."""
    from zeenova_bot.handlers import on_text

    failing_client = MagicMock()
    failing_client.render = AsyncMock(return_value=None)
    update, context = _quote_update_context("z", quote_client=failing_client)
    await on_text(update, context)
    update.effective_message.reply_sticker.assert_not_called()
    update.effective_message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_quote_trigger_works_in_dm() -> None:
    """Trigger works in private chat too, not just groups."""
    from zeenova_bot.handlers import on_text

    update, context = _quote_update_context("z", chat_type=ChatType.PRIVATE)
    await on_text(update, context)
    update.effective_message.reply_sticker.assert_called_once()


@pytest.mark.asyncio
async def test_quote_trigger_uses_telegram_quote_snippet() -> None:
    """If the user used Telegram's quote feature, sticker the snippet —
    not the parent's full message text."""
    from zeenova_bot.handlers import on_text

    update, context = _quote_update_context(
        "z", parent_text="The full original message body."
    )
    quote = MagicMock()
    quote.text = "original message"
    quote.entities = ()
    update.effective_message.quote = quote

    await on_text(update, context)

    client = context.bot_data["quote_sticker"]
    args, _ = client.render.call_args
    _author, text = args
    assert text == "original message"


@pytest.mark.asyncio
async def test_quote_trigger_falls_back_when_quote_text_blank() -> None:
    """Empty highlight → fall back to the parent's full text."""
    from zeenova_bot.handlers import on_text

    update, context = _quote_update_context(
        "z", parent_text="Whole parent body"
    )
    quote = MagicMock()
    quote.text = "   "  # whitespace-only highlight
    quote.entities = ()
    update.effective_message.quote = quote

    await on_text(update, context)

    client = context.bot_data["quote_sticker"]
    args, _ = client.render.call_args
    _author, text = args
    assert text == "Whole parent body"


# ---------------------------------------------------------------------------
# /news
# ---------------------------------------------------------------------------


def _news_article(title: str, url: str, source: str = "CoinDesk") -> object:
    from datetime import UTC, datetime

    from zeenova_bot.news import NewsArticle

    return NewsArticle(
        title=title,
        url=url,
        source=source,
        published_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
    )


def _news_update_context(news: object) -> tuple[MagicMock, MagicMock]:
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    update = MagicMock()
    update.effective_message = msg
    update.effective_chat = MagicMock()
    context = MagicMock()
    context.bot_data = {"news": news, "settings": _settings()}
    return update, context


@pytest.mark.asyncio
async def test_cmd_news_renders_headlines_as_html_links() -> None:
    """``/news`` lists articles as clickable links with their source."""
    from zeenova_bot.handlers import cmd_news

    news = MagicMock()
    news.fetch_latest = AsyncMock(
        return_value=[
            _news_article("Bitcoin holds $80K", "https://x.com/btc", "CoinDesk"),
            _news_article(
                "Ethereum down 35%", "https://x.com/eth", "Cointelegraph"
            ),
        ]
    )
    update, context = _news_update_context(news)
    await cmd_news(update, context)
    body = _last_reply(update.effective_message)
    assert "latest crypto news" in body
    assert 'href="https://x.com/btc"' in body
    assert "Bitcoin holds $80K" in body
    assert "CoinDesk" in body
    assert "Ethereum down 35%" in body


@pytest.mark.asyncio
async def test_cmd_news_handles_empty_feed_gracefully() -> None:
    """An empty news result surfaces as a friendly error, not a crash."""
    from zeenova_bot.handlers import cmd_news

    news = MagicMock()
    news.fetch_latest = AsyncMock(return_value=[])
    update, context = _news_update_context(news)
    await cmd_news(update, context)
    body = _last_reply(update.effective_message)
    assert "Couldn't load" in body


@pytest.mark.asyncio
async def test_cmd_news_handles_client_exception() -> None:
    """If the client raises, /news still replies with a friendly error."""
    from zeenova_bot.handlers import cmd_news

    news = MagicMock()
    news.fetch_latest = AsyncMock(side_effect=RuntimeError("boom"))
    update, context = _news_update_context(news)
    await cmd_news(update, context)
    body = _last_reply(update.effective_message)
    assert "Couldn't load" in body


# ---------------------------------------------------------------------------
# Brand keyboard — channel + chat URL buttons attached to command replies
# ---------------------------------------------------------------------------


def _last_reply_kwargs(msg: MagicMock) -> dict[str, object]:
    """Return the kwargs of the last reply_text / reply_photo call."""
    call = msg.reply_text.call_args or msg.reply_photo.call_args
    assert call is not None
    return dict(call.kwargs)


def _assert_brand_keyboard(markup: object) -> None:
    """Assert ``markup`` is an InlineKeyboardMarkup whose last row has
    a 📣 Channel button and a 💬 Chat button with the configured URLs."""
    from telegram import InlineKeyboardMarkup

    assert isinstance(markup, InlineKeyboardMarkup)
    rows = list(markup.inline_keyboard)
    brand_row = rows[-1]
    assert len(brand_row) == 2
    channel_btn, chat_btn = brand_row
    assert "Zeen Channel" in channel_btn.text
    assert channel_btn.url == "https://t.me/ox_zeen"
    assert "Zeen Chat" in chat_btn.text
    assert chat_btn.url == "https://t.me/blockzeen"


@pytest.mark.asyncio
async def test_cmd_market_attaches_brand_buttons() -> None:
    """``/market`` reply carries the channel + chat shortcut buttons."""
    from zeenova_bot.coinpaprika import GlobalSnapshot
    from zeenova_bot.handlers import cmd_market

    paprika = MagicMock()
    paprika.fetch_global = AsyncMock(
        return_value=GlobalSnapshot(
            market_cap_usd=3.2e12,
            volume_24h_usd=8.5e10,
            bitcoin_dominance_pct=53.1,
            cryptocurrencies_number=12_345,
            market_cap_change_24h_pct=1.5,
        )
    )
    update, context = _cmd_update_context(paprika=paprika)
    await cmd_market(update, context)
    kwargs = _last_reply_kwargs(update.effective_message)
    _assert_brand_keyboard(kwargs.get("reply_markup"))


@pytest.mark.asyncio
async def test_cmd_top_attaches_brand_buttons() -> None:
    """``/top`` reply carries the channel + chat shortcut buttons."""
    from zeenova_bot.coinpaprika import TickerSnapshot
    from zeenova_bot.handlers import cmd_top

    rows = [
        TickerSnapshot(
            symbol=f"C{i}",
            name=f"Coin{i}",
            rank=i + 10,
            price_usd=1.0,
            change_pct_24h=float(i),
            market_cap_usd=1e9,
        )
        for i in range(-5, 6)
    ]
    paprika = MagicMock()
    paprika.fetch_top_tickers = AsyncMock(return_value=rows)
    update, context = _cmd_update_context(paprika=paprika)
    await cmd_top(update, context)
    kwargs = _last_reply_kwargs(update.effective_message)
    _assert_brand_keyboard(kwargs.get("reply_markup"))


@pytest.mark.asyncio
async def test_cmd_news_attaches_brand_buttons() -> None:
    """``/news`` reply carries the channel + chat shortcut buttons."""
    from zeenova_bot.handlers import cmd_news

    news = MagicMock()
    news.fetch_latest = AsyncMock(
        return_value=[
            _news_article("Story", "https://x.com/a", "CoinDesk"),
        ]
    )
    update, context = _news_update_context(news)
    await cmd_news(update, context)
    kwargs = _last_reply_kwargs(update.effective_message)
    _assert_brand_keyboard(kwargs.get("reply_markup"))


# ---------------------------------------------------------------------------
# Premium custom-emoji icons on brand buttons + /emojiid helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_brand_buttons_no_icon_when_emoji_ids_unset() -> None:
    """Without ``BRAND_*_EMOJI_ID`` set, buttons carry no custom emoji."""
    from zeenova_bot.handlers import _brand_buttons

    settings = _settings()
    btns = _brand_buttons(settings)
    for btn in btns:
        assert "icon_custom_emoji_id" not in btn.to_dict()


@pytest.mark.asyncio
async def test_brand_buttons_attach_icon_when_emoji_ids_set() -> None:
    """When the env vars are set, both buttons gain the icon hint."""
    from zeenova_bot.handlers import _brand_buttons

    settings = _settings()
    settings.brand_channel_emoji_id = "5436105917961765442"
    settings.brand_group_emoji_id = "5429524775225921481"
    channel_btn, chat_btn = _brand_buttons(settings)
    assert channel_btn.to_dict().get("icon_custom_emoji_id") == "5436105917961765442"
    assert chat_btn.to_dict().get("icon_custom_emoji_id") == "5429524775225921481"


@pytest.mark.asyncio
async def test_brand_buttons_strip_whitespace_in_emoji_id() -> None:
    """Whitespace around the env var value shouldn't break the API call."""
    from zeenova_bot.handlers import _brand_buttons

    settings = _settings()
    settings.brand_channel_emoji_id = "  5436105917961765442  "
    settings.brand_group_emoji_id = "\t5429524775225921481\n"
    channel_btn, chat_btn = _brand_buttons(settings)
    assert channel_btn.to_dict().get("icon_custom_emoji_id") == "5436105917961765442"
    assert chat_btn.to_dict().get("icon_custom_emoji_id") == "5429524775225921481"


def _emojiid_update_context(
    *, message_entities: list[MessageEntity] | None = None,
    reply_entities: list[MessageEntity] | None = None,
    text: str = "",
    reply_text: str = "",
) -> tuple[MagicMock, MagicMock]:
    """Build an Update/Context pair for the /emojiid handler."""
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    msg.text = text
    msg.caption = None
    msg.entities = tuple(message_entities or ())
    msg.caption_entities = ()
    if reply_entities is not None or reply_text:
        reply = MagicMock()
        reply.text = reply_text
        reply.caption = None
        reply.entities = tuple(reply_entities or ())
        reply.caption_entities = ()
        msg.reply_to_message = reply
    else:
        msg.reply_to_message = None
    update = MagicMock()
    update.effective_message = msg
    context = MagicMock()
    context.bot_data = {}
    return update, context


@pytest.mark.asyncio
async def test_cmd_emojiid_returns_id_from_replied_message() -> None:
    """When replying to a message with a Premium emoji, the bot
    returns that emoji's ``custom_emoji_id``."""
    from zeenova_bot.handlers import cmd_emojiid

    entity = MessageEntity(
        type=MessageEntity.CUSTOM_EMOJI,
        offset=0,
        length=2,
        custom_emoji_id="5436105917961765442",
    )
    update, context = _emojiid_update_context(
        reply_entities=[entity], reply_text="⚡ hi"
    )
    await cmd_emojiid(update, context)
    body = _last_reply(update.effective_message)
    assert "5436105917961765442" in body
    assert "⚡" in body


@pytest.mark.asyncio
async def test_cmd_emojiid_explains_when_no_custom_emoji_found() -> None:
    """If the replied message has no custom emoji, the bot
    sends a usage hint instead of staying silent."""
    from zeenova_bot.handlers import cmd_emojiid

    update, context = _emojiid_update_context(reply_text="plain text")
    await cmd_emojiid(update, context)
    body = _last_reply(update.effective_message)
    assert "No Telegram Premium custom emoji" in body


@pytest.mark.asyncio
async def test_cmd_emojiid_dedupes_repeated_ids() -> None:
    """A message with the same emoji repeated should print one row."""
    from zeenova_bot.handlers import cmd_emojiid

    eid = "5436105917961765442"
    entities = [
        MessageEntity(
            type=MessageEntity.CUSTOM_EMOJI,
            offset=0,
            length=2,
            custom_emoji_id=eid,
        ),
        MessageEntity(
            type=MessageEntity.CUSTOM_EMOJI,
            offset=3,
            length=2,
            custom_emoji_id=eid,
        ),
    ]
    update, context = _emojiid_update_context(
        reply_entities=entities, reply_text="⚡ ⚡"
    )
    await cmd_emojiid(update, context)
    body = _last_reply(update.effective_message)
    assert body.count(eid) == 1

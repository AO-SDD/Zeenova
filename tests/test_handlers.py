"""Tests for the calculator/FX bridge used by the message handlers."""

from __future__ import annotations

from dataclasses import replace
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
# Comma-grouped numbers and multi-line input through the calc handler.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thousands_separator_evaluates_in_dm() -> None:
    """``37,632.00+30%`` evaluates to 48 921.60 (= 37,632 * 1.30)."""
    fx = _fx_returning({})
    svc = _service_with({})
    update, context = _calc_update_context(
        "37,632.00+30%", fx, svc, chat_type=ChatType.PRIVATE
    )
    assert (
        await _handle_calc(update, context, "37,632.00+30%") is True
    )
    reply = _last_reply(update.effective_message)
    # ``_fmt_amount`` shows ``48,921.60`` for values >= 1000.
    assert "48,921.60" in reply


@pytest.mark.asyncio
async def test_multi_line_two_calcs_one_reply() -> None:
    """``2/1\\n2*2`` → one reply containing both results."""
    fx = _fx_returning({})
    svc = _service_with({})
    text = "2/1\n2*2"
    update, context = _calc_update_context(
        text, fx, svc, chat_type=ChatType.PRIVATE
    )
    assert await _handle_calc(update, context, text) is True
    msg = update.effective_message
    # One Telegram round-trip, not two.
    assert msg.reply_text.call_count == 1
    reply = _last_reply(msg)
    # Both expressions and both results appear in the same body.
    assert "2/1" in reply
    assert "2*2" in reply
    assert "= <code>2</code>" in reply  # 2/1
    assert "= <code>4</code>" in reply  # 2*2


@pytest.mark.asyncio
async def test_multi_line_with_currency_lines() -> None:
    """Mixed multi-line: pure math + a single-currency line both evaluate."""
    fx = _fx_returning({"egp": {"usd": 1 / 50.0}})
    svc = _service_with({})
    text = "10+5\n100 egp"
    update, context = _calc_update_context(
        text, fx, svc, chat_type=ChatType.PRIVATE
    )
    assert await _handle_calc(update, context, text) is True
    msg = update.effective_message
    assert msg.reply_text.call_count == 1
    reply = _last_reply(msg)
    assert "10+5" in reply
    assert "100 EGP" in reply


@pytest.mark.asyncio
async def test_multi_line_falls_through_when_any_line_unparseable() -> None:
    """If even one line isn't calc-shaped, multi-line mode is skipped.

    Here the second line has more tokens than the single-line regex can
    swallow as a currency pair, so the whole message doesn't parse and
    the handler returns False without sending a reply.
    """
    fx = _fx_returning({})
    svc = _service_with({})
    text = "2+2\nfoo bar baz"
    update, context = _calc_update_context(
        text, fx, svc, chat_type=ChatType.PRIVATE
    )
    assert await _handle_calc(update, context, text) is False
    update.effective_message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_multi_line_three_calcs() -> None:
    """``1+1\\n2+2\\n3+3`` — three results in one reply."""
    fx = _fx_returning({})
    svc = _service_with({})
    text = "1+1\n2+2\n3+3"
    update, context = _calc_update_context(
        text, fx, svc, chat_type=ChatType.PRIVATE
    )
    assert await _handle_calc(update, context, text) is True
    msg = update.effective_message
    assert msg.reply_text.call_count == 1
    reply = _last_reply(msg)
    assert "= <code>2</code>" in reply
    assert "= <code>4</code>" in reply
    assert "= <code>6</code>" in reply


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


def test_help_text_wraps_section_icons_in_premium_emoji_tags() -> None:
    """Every section icon in /help is wrapped in a ``<tg-emoji>`` tag
    when the operator configures a Premium ID for it."""
    from zeenova_bot.emojis import default_premium_emojis, premium_emoji

    base = default_premium_emojis()
    emojis = replace(
        base,
        help_header=premium_emoji("📈", "111"),
        help_prices=premium_emoji("💹", "222"),
        help_market=premium_emoji("📊", "333"),
        help_calc=premium_emoji("🧮", "444"),
        help_fiat=premium_emoji("🌍", "555"),
        help_group=premium_emoji("⚡", "666"),
    )
    body = _help_text(_settings(), emojis)
    # Each section icon is wrapped in a Premium tag with the right ID.
    assert 'emoji-id="111">📈</tg-emoji>' in body
    assert 'emoji-id="222">💹</tg-emoji>' in body
    assert 'emoji-id="333">📊</tg-emoji>' in body
    assert 'emoji-id="444">🧮</tg-emoji>' in body
    assert 'emoji-id="555">🌍</tg-emoji>' in body
    assert 'emoji-id="666">⚡</tg-emoji>' in body


def test_help_text_uses_plain_emojis_when_premium_ids_unset() -> None:
    """Default (no Premium IDs set) renders raw emoji glyphs, not
    ``<tg-emoji>`` tags."""
    body = _help_text(_settings())
    assert "<tg-emoji" not in body
    # The plain glyphs are still in the body, untouched.
    for glyph in ("📈", "💹", "📊", "🧮", "🌍", "⚡"):
        assert glyph in body


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
    a Channel button and a Chat button with the configured URLs.

    Labels go through the Unicode sans-serif bold transform, so the
    plain ASCII names won't appear verbatim — they're checked via the
    bolded form (``𝗭𝗲𝗲𝗻 𝗖𝗵𝗮𝗻𝗻𝗲𝗹`` etc.).
    """
    from telegram import InlineKeyboardMarkup

    from zeenova_bot.handlers import _bold_label

    assert isinstance(markup, InlineKeyboardMarkup)
    rows = list(markup.inline_keyboard)
    brand_row = rows[-1]
    assert len(brand_row) == 2
    channel_btn, chat_btn = brand_row
    assert _bold_label("Zeen Channel") in channel_btn.text
    assert channel_btn.url == "https://t.me/ox_zeen"
    assert _bold_label("Zeen Chat") in chat_btn.text
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


# ---------------------------------------------------------------------------
# Bold-label transform + duplicate-emoji stripping on brand buttons
# ---------------------------------------------------------------------------


def test_bold_label_promotes_ascii_to_sans_serif_bold() -> None:
    """ASCII letters and digits map to Unicode sans-serif bold; spaces
    and non-ASCII characters pass through unchanged."""
    from zeenova_bot.handlers import _bold_label

    assert _bold_label("Zeen Channel") == "𝗭𝗲𝗲𝗻 𝗖𝗵𝗮𝗻𝗻𝗲𝗹"
    assert _bold_label("ABC 123 xyz") == "𝗔𝗕𝗖 𝟭𝟮𝟯 𝘅𝘆𝘇"
    # Emoji and Arabic pass through unchanged.
    assert _bold_label("hi! 📣 مرحبا") == "𝗵𝗶! 📣 مرحبا"


@pytest.mark.asyncio
async def test_brand_buttons_drop_fallback_emoji_when_premium_icon_set() -> None:
    """With a Premium emoji configured, the leading 📣 / 💬 are removed
    from the label so the button doesn't show two icons in a row."""
    from zeenova_bot.handlers import _bold_label, _brand_buttons

    settings = _settings()
    settings.brand_channel_emoji_id = "6260052174089229782"
    settings.brand_group_emoji_id = "6159083200971805825"
    channel_btn, chat_btn = _brand_buttons(settings)
    assert channel_btn.text == _bold_label("Zeen Channel")
    assert "📣" not in channel_btn.text
    assert chat_btn.text == _bold_label("Zeen Chat")
    assert "💬" not in chat_btn.text


@pytest.mark.asyncio
async def test_brand_buttons_keep_fallback_emoji_without_premium_icon() -> None:
    """Without a Premium icon, the fallback emoji stays as the visual
    marker so plain-token users still see something in front of the
    name."""
    from zeenova_bot.handlers import _bold_label, _brand_buttons

    settings = _settings()
    channel_btn, chat_btn = _brand_buttons(settings)
    assert channel_btn.text == f"📣 {_bold_label('Zeen Channel')}"
    assert chat_btn.text == f"💬 {_bold_label('Zeen Chat')}"


# ---------------------------------------------------------------------------
# Premium custom-emoji substitution in body text
# ---------------------------------------------------------------------------


def test_premium_emoji_returns_raw_when_id_blank() -> None:
    """An empty / whitespace-only ID leaves the emoji unchanged."""
    from zeenova_bot.emojis import premium_emoji

    assert premium_emoji("🏆", "") == "🏆"
    assert premium_emoji("🏆", "   ") == "🏆"


def test_premium_emoji_wraps_with_tg_emoji_when_id_set() -> None:
    """A non-empty ID wraps the emoji in a ``<tg-emoji>`` tag."""
    from zeenova_bot.emojis import premium_emoji

    assert premium_emoji("🏆", "5436105917961765442") == (
        '<tg-emoji emoji-id="5436105917961765442">🏆</tg-emoji>'
    )


def test_premium_emoji_escapes_id_attribute() -> None:
    """The ID is HTML-escaped so a hostile value can't break out of
    the attribute (defensive — IDs come from user-controlled env vars)."""
    from zeenova_bot.emojis import premium_emoji

    out = premium_emoji("🏆", '" onmouseover="x')
    assert "<tg-emoji" in out
    assert '" ' not in out
    assert "onmouseover" in out  # the literal value is preserved but escaped
    assert "&quot;" in out


def test_resolve_premium_emojis_default_returns_plain_glyphs() -> None:
    """With no env vars set, every slot is the raw fallback emoji."""
    from zeenova_bot.handlers import _resolve_premium_emojis

    emojis = _resolve_premium_emojis(_settings())
    assert emojis.up == "🟢"
    assert emojis.down == "🔴"
    assert emojis.rank == "🏆"
    assert emojis.price == "💵"
    assert emojis.high == "🔼"
    assert emojis.low == "🔽"
    assert emojis.mcap == "🏛"
    assert emojis.volume == "📊"
    assert emojis.globe == "🌐"
    assert emojis.btc == "🟠"
    assert emojis.coins == "🪙"
    assert emojis.top == "📈"
    assert emojis.news == "📰"


def test_resolve_premium_emojis_wraps_configured_slots() -> None:
    """Configured slots come back as ``<tg-emoji>`` HTML; unset slots
    stay as the raw glyph."""
    from zeenova_bot.handlers import _resolve_premium_emojis

    settings = _settings()
    settings.premium_emoji_rank_id = "5436"
    settings.premium_emoji_volume_id = "5437"
    emojis = _resolve_premium_emojis(settings)
    assert emojis.rank == '<tg-emoji emoji-id="5436">🏆</tg-emoji>'
    assert emojis.volume == '<tg-emoji emoji-id="5437">📊</tg-emoji>'
    # Untouched slots stay plain.
    assert emojis.price == "💵"
    assert emojis.high == "🔼"


def test_resolve_premium_emojis_atl_gain_falls_back_to_ath_up() -> None:
    """``atl_gain`` reuses ``ath_up`` when its own ID is not set."""
    from zeenova_bot.handlers import _resolve_premium_emojis

    settings = _settings()
    settings.premium_emoji_ath_up_id = "7777"
    emojis = _resolve_premium_emojis(settings)
    assert emojis.ath_up == '<tg-emoji emoji-id="7777">🚀</tg-emoji>'
    assert emojis.atl_gain == '<tg-emoji emoji-id="7777">🚀</tg-emoji>'


def test_resolve_premium_emojis_atl_gain_overrides_ath_up() -> None:
    """A configured ``atl_gain`` ID decorates only the ATL gain row."""
    from zeenova_bot.handlers import _resolve_premium_emojis

    settings = _settings()
    settings.premium_emoji_ath_up_id = "7777"
    settings.premium_emoji_atl_gain_id = "8888"
    emojis = _resolve_premium_emojis(settings)
    assert emojis.ath_up == '<tg-emoji emoji-id="7777">🚀</tg-emoji>'
    assert emojis.atl_gain == '<tg-emoji emoji-id="8888">🚀</tg-emoji>'


def test_resolve_premium_emojis_atl_gain_default_plain() -> None:
    """Without any ID set, ``atl_gain`` stays the raw fallback glyph."""
    from zeenova_bot.handlers import _resolve_premium_emojis

    emojis = _resolve_premium_emojis(_settings())
    assert emojis.atl_gain == "🚀"


def test_render_price_card_uses_configured_premium_emojis() -> None:
    """Setting a Premium emoji ID wraps the matching body emoji."""
    from zeenova_bot.card import render_price_card
    from zeenova_bot.handlers import _resolve_premium_emojis
    from zeenova_bot.services import MarketData

    md = MarketData(
        symbol="BTC",
        pair="BTCUSDT",
        source="binance",
        price_usd=80_000.0,
        price_change_pct_24h=1.5,
        high_24h=82_000.0,
        low_24h=79_000.0,
        market_cap_usd=1.6e12,
        total_volume_usd_24h=4.2e10,
        market_cap_rank=1,
    )

    settings = _settings()
    settings.premium_emoji_rank_id = "111"
    settings.premium_emoji_price_id = "222"
    settings.premium_emoji_mcap_id = "333"
    body = render_price_card(md, _resolve_premium_emojis(settings))
    assert '<tg-emoji emoji-id="111">🏆</tg-emoji>' in body
    assert '<tg-emoji emoji-id="222">💵</tg-emoji>' in body
    assert '<tg-emoji emoji-id="333">🏛</tg-emoji>' in body
    # 24H High wasn't configured → still the raw glyph.
    assert "🔼 <b>24H High:</b>" in body


def test_render_price_card_defaults_to_plain_emojis_without_override() -> None:
    """Calling render_price_card without emojis keeps the legacy plain
    glyphs (backward compatible)."""
    from zeenova_bot.card import render_price_card
    from zeenova_bot.services import MarketData

    md = MarketData(
        symbol="BTC",
        pair="BTCUSDT",
        source="binance",
        price_usd=80_000.0,
        price_change_pct_24h=-2.5,
        high_24h=82_000.0,
        low_24h=79_000.0,
        market_cap_usd=1.6e12,
        total_volume_usd_24h=4.2e10,
        market_cap_rank=1,
    )
    body = render_price_card(md)
    assert "🏆" in body
    assert "💵" in body
    assert "🔴" in body  # 24H change negative → down
    assert "<tg-emoji" not in body


def test_change_slot_empty_by_default_so_24h_change_follows_direction() -> None:
    """Without ``PREMIUM_EMOJI_CHANGE_ID``, the 24H Change row uses the
    same up/down dot as the header — same behaviour as before this slot
    existed."""
    from zeenova_bot.card import render_price_card
    from zeenova_bot.handlers import _resolve_premium_emojis
    from zeenova_bot.services import MarketData

    md = MarketData(
        symbol="BTC",
        pair="BTCUSDT",
        source="binance",
        price_usd=80_000.0,
        price_change_pct_24h=-2.5,
        high_24h=82_000.0,
        low_24h=79_000.0,
        market_cap_usd=1.6e12,
        total_volume_usd_24h=4.2e10,
    )
    emojis = _resolve_premium_emojis(_settings())
    assert emojis.change == ""
    body = render_price_card(md, emojis)
    # Both header dot and 24H Change row should be the down dot.
    assert body.count("🔴") == 2
    assert "🟢" not in body


def test_change_id_pins_24h_change_emoji_regardless_of_direction() -> None:
    """With ``PREMIUM_EMOJI_CHANGE_ID`` set, the 24H Change row uses
    that one Premium emoji for both up and down moves; the header dot
    keeps tracking direction independently."""
    from zeenova_bot.card import render_price_card
    from zeenova_bot.handlers import _resolve_premium_emojis
    from zeenova_bot.services import MarketData

    settings = _settings()
    settings.premium_emoji_change_id = "9999"
    emojis = _resolve_premium_emojis(settings)
    assert emojis.change == '<tg-emoji emoji-id="9999">📊</tg-emoji>'

    # Negative move: header should be 🔴, 24H Change should be the
    # wrapped Premium emoji (not 🔴).
    md_down = MarketData(
        symbol="BTC",
        pair="BTCUSDT",
        source="binance",
        price_usd=80_000.0,
        price_change_pct_24h=-2.5,
        high_24h=82_000.0,
        low_24h=79_000.0,
        market_cap_usd=1.6e12,
        total_volume_usd_24h=4.2e10,
    )
    body_down = render_price_card(md_down, emojis)
    assert body_down.count("🔴") == 1  # only the header dot
    assert '<tg-emoji emoji-id="9999">📊</tg-emoji> <b>24H Change:</b>' in body_down

    # Positive move: header should be 🟢, 24H Change should still be
    # the wrapped Premium emoji (not 🟢).
    md_up = MarketData(
        symbol="BTC",
        pair="BTCUSDT",
        source="binance",
        price_usd=80_000.0,
        price_change_pct_24h=1.5,
        high_24h=82_000.0,
        low_24h=79_000.0,
        market_cap_usd=1.6e12,
        total_volume_usd_24h=4.2e10,
    )
    body_up = render_price_card(md_up, emojis)
    assert body_up.count("🟢") == 1  # only the header dot
    assert '<tg-emoji emoji-id="9999">📊</tg-emoji> <b>24H Change:</b>' in body_up


# ---------------------------------------------------------------------------
# /p command parser + edit-to-edit reply helpers.
# ---------------------------------------------------------------------------


class TestParsePriceCommand:
    """``/p SYMBOL`` parsing used by the edited-message dispatcher."""

    def test_short_alias(self) -> None:
        from zeenova_bot.handlers import _parse_price_command

        assert _parse_price_command("/p btc") == "btc"

    def test_full_alias(self) -> None:
        from zeenova_bot.handlers import _parse_price_command

        assert _parse_price_command("/price BTC") == "BTC"

    def test_strips_leading_dollar(self) -> None:
        from zeenova_bot.handlers import _parse_price_command

        assert _parse_price_command("/p $eth") == "eth"

    def test_with_bot_handle(self) -> None:
        from zeenova_bot.handlers import _parse_price_command

        assert _parse_price_command("/p@MyBot doge") == "doge"

    def test_extra_args_ignored(self) -> None:
        # The price command only takes one symbol; ``/p btc eth`` doesn't
        # match (the regex requires the line to end after the symbol).
        from zeenova_bot.handlers import _parse_price_command

        assert _parse_price_command("/p btc eth") is None

    def test_no_command_returns_none(self) -> None:
        from zeenova_bot.handlers import _parse_price_command

        assert _parse_price_command("hello there") is None

    def test_missing_symbol(self) -> None:
        from zeenova_bot.handlers import _parse_price_command

        assert _parse_price_command("/p") is None
        assert _parse_price_command("/price ") is None


def _editable_update_context(
    text: str,
    *,
    is_edit: bool,
    edit_store: object | None = None,
    chat_id: int = 1,
    user_msg_id: int = 100,
    bot_msg_id: int = 999,
) -> tuple[MagicMock, MagicMock]:
    """Wire up a minimal Update + Context for the edit-aware send helpers."""
    msg = MagicMock()
    msg.text = text
    msg.message_id = user_msg_id
    sent = MagicMock()
    sent.message_id = bot_msg_id
    msg.reply_text = AsyncMock(return_value=sent)
    update = MagicMock()
    update.effective_message = msg
    chat = MagicMock()
    chat.id = chat_id
    chat.type = ChatType.PRIVATE
    update.effective_chat = chat
    # ``_is_edited_update`` reads ``update.edited_message is not None``.
    update.edited_message = msg if is_edit else None
    context = MagicMock()
    context.bot = MagicMock()
    context.bot.edit_message_text = AsyncMock(return_value=sent)
    context.bot.edit_message_media = AsyncMock(return_value=sent)
    context.bot.delete_message = AsyncMock()
    context.bot.send_photo = AsyncMock(return_value=sent)
    context.bot_data = {"edit_store": edit_store} if edit_store is not None else {}
    return update, context


@pytest.mark.asyncio
async def test_send_text_reply_records_bot_msg_for_first_send() -> None:
    """On a brand-new message, ``_send_text_reply`` sends a normal reply
    and records the resulting bot message id for later edits."""
    from telegram import Message

    from zeenova_bot.edit_state import EditableReplyStore
    from zeenova_bot.handlers import _send_text_reply

    store = EditableReplyStore()
    update, context = _editable_update_context(
        "2+2", is_edit=False, edit_store=store
    )
    # ``msg.reply_text`` returns a real-looking Message-like object so we
    # can pull ``message_id`` off it after the await.
    update.effective_message.reply_text.return_value = MagicMock(
        spec=Message, message_id=42
    )
    await _send_text_reply(update, context, "hello")
    update.effective_message.reply_text.assert_awaited_once()
    assert store.get(1, 100) == (42, "text")


@pytest.mark.asyncio
async def test_send_text_reply_edits_prior_text_reply() -> None:
    """When the update is an edit and the prior reply was text, the
    helper calls ``edit_message_text`` instead of ``reply_text``."""
    from zeenova_bot.edit_state import EditableReplyStore
    from zeenova_bot.handlers import _send_text_reply

    store = EditableReplyStore()
    store.record(1, 100, bot_msg_id=42, kind="text")
    update, context = _editable_update_context(
        "2+3", is_edit=True, edit_store=store
    )
    await _send_text_reply(update, context, "updated")
    context.bot.edit_message_text.assert_awaited_once()
    update.effective_message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_text_reply_deletes_and_resends_when_kind_changes() -> None:
    """If the prior reply was a photo but we now want to send text, the
    photo is deleted and a fresh text reply takes its place."""
    from telegram import Message

    from zeenova_bot.edit_state import EditableReplyStore
    from zeenova_bot.handlers import _send_text_reply

    store = EditableReplyStore()
    store.record(1, 100, bot_msg_id=42, kind="photo")
    update, context = _editable_update_context(
        "2+3", is_edit=True, edit_store=store
    )
    update.effective_message.reply_text.return_value = MagicMock(
        spec=Message, message_id=43
    )
    await _send_text_reply(update, context, "now text")
    context.bot.delete_message.assert_awaited_once_with(1, 42)
    update.effective_message.reply_text.assert_awaited_once()
    # New entry records the new bot message id under the same key.
    assert store.get(1, 100) == (43, "text")


# ---------------------------------------------------------------------------
# /ath — all-time high / low from CoinGecko
# ---------------------------------------------------------------------------


def _ath_snapshot() -> object:
    from zeenova_bot.coingecko import AthAtl

    return AthAtl(
        symbol="BTC",
        name="Bitcoin",
        current_price=81611.0,
        ath=126080.0,
        ath_change_pct=-35.27,
        ath_date="2025-10-06T18:57:42.558Z",
        atl=67.81,
        atl_change_pct=120253.85,
        atl_date="2013-07-06T00:00:00.000Z",
        rank=1,
    )


def _ath_update_context(coingecko: object) -> tuple[MagicMock, MagicMock]:
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    update = MagicMock()
    update.effective_message = msg
    update.effective_chat = MagicMock()
    context = MagicMock()
    context.bot_data = {"coingecko": coingecko, "settings": _settings()}
    context.args = ["btc"]
    return update, context


@pytest.mark.asyncio
async def test_cmd_ath_renders_snapshot_card() -> None:
    from zeenova_bot.handlers import cmd_ath

    coingecko = MagicMock()
    coingecko.fetch_ath_atl = AsyncMock(return_value=_ath_snapshot())
    update, context = _ath_update_context(coingecko)
    await cmd_ath(update, context)
    body = _last_reply(update.effective_message)
    assert "Bitcoin" in body
    assert "BTC" in body
    assert "All-Time High" in body
    assert "All-Time Low" in body
    assert "Rank:</b> #1" in body
    coingecko.fetch_ath_atl.assert_awaited_once_with("BTC")


@pytest.mark.asyncio
async def test_cmd_ath_strips_dollar_prefix_and_uppercases() -> None:
    """``$eth`` becomes ``ETH`` — same normalisation as /p."""
    from zeenova_bot.handlers import cmd_ath

    coingecko = MagicMock()
    coingecko.fetch_ath_atl = AsyncMock(return_value=None)
    update, context = _ath_update_context(coingecko)
    context.args = ["$eth"]
    await cmd_ath(update, context)
    coingecko.fetch_ath_atl.assert_awaited_once_with("ETH")


@pytest.mark.asyncio
async def test_cmd_ath_replies_with_usage_when_no_args() -> None:
    from zeenova_bot.handlers import cmd_ath

    coingecko = MagicMock()
    coingecko.fetch_ath_atl = AsyncMock()
    update, context = _ath_update_context(coingecko)
    context.args = []
    await cmd_ath(update, context)
    body = _last_reply(update.effective_message)
    assert "/ath SYMBOL" in body
    coingecko.fetch_ath_atl.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_ath_reports_unknown_symbol() -> None:
    from zeenova_bot.handlers import cmd_ath

    coingecko = MagicMock()
    coingecko.fetch_ath_atl = AsyncMock(return_value=None)
    update, context = _ath_update_context(coingecko)
    await cmd_ath(update, context)
    body = _last_reply(update.effective_message)
    assert "No ATH/ATL data" in body


# ---------------------------------------------------------------------------
# /wallet — Ethereum wallet summary from Etherscan
# ---------------------------------------------------------------------------


def _wallet_info(
    address: str = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045",
    *,
    eth_balance_wei: int = 3_450_000_000_000_000_000,
    bnb_balance_wei: int = 0,
    txs_sent: int = 1234,
    with_recent: bool = True,
) -> object:
    """Build a multichain :class:`WalletInfo` for handler tests.

    Defaults mimic a "mostly Ethereum-active" wallet: a real ETH
    balance + nonce on Ethereum, zero on every other chain, with a
    short recent-tx list rendered against Ethereum.
    """
    from zeenova_bot.etherscan import CHAINS, ChainBalance, WalletInfo, WalletTx

    recent: tuple[WalletTx, ...] = ()
    if with_recent:
        recent = (
            WalletTx(
                hash="0xaaa",
                timestamp=1_730_000_000,
                from_addr=address,
                to_addr="0x" + "1" * 40,
                value_wei=500_000_000_000_000_000,
                is_incoming=False,
            ),
            WalletTx(
                hash="0xbbb",
                timestamp=1_729_999_000,
                from_addr="0x" + "2" * 40,
                to_addr=address,
                value_wei=1_200_000_000_000_000_000,
                is_incoming=True,
            ),
        )
    eth_chain = next(c for c in CHAINS if c.id == 1)
    balances_list: list[ChainBalance] = []
    for chain in CHAINS:
        if chain.id == 1:
            wei = eth_balance_wei
            sent = txs_sent
            last = recent[0].timestamp if recent else None
        elif chain.id == 56:
            wei = bnb_balance_wei
            sent = 0
            last = None
        else:
            wei = 0
            sent = 0
            last = None
        balances_list.append(
            ChainBalance(
                chain=chain,
                balance_wei=wei,
                balance=wei / 10**18,
                txs_sent=sent,
                last_tx_at=last,
            )
        )
    return WalletInfo(
        address=address,
        balances=tuple(balances_list),
        recent_chain=eth_chain if recent else None,
        recent=recent,
    )


def _wallet_update_context(
    etherscan: object,
    paprika: object | None,
    *,
    args: list[str] | None = None,
    solana: object | None = None,
) -> tuple[MagicMock, MagicMock]:
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    update = MagicMock()
    update.effective_message = msg
    update.effective_chat = MagicMock()
    context = MagicMock()
    bot_data: dict[str, object] = {"etherscan": etherscan, "settings": _settings()}
    if paprika is not None:
        bot_data["paprika"] = paprika
    if solana is not None:
        bot_data["solana"] = solana
    context.bot_data = bot_data
    context.args = args if args is not None else [
        "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    ]
    return update, context


@pytest.mark.asyncio
async def test_cmd_wallet_renders_full_card_when_configured() -> None:
    from zeenova_bot.handlers import cmd_wallet

    etherscan = MagicMock()
    etherscan.is_configured = MagicMock(return_value=True)
    etherscan.fetch_wallet = AsyncMock(return_value=_wallet_info())
    paprika = MagicMock()
    paprika.fetch_price_snapshot = AsyncMock(
        return_value=MagicMock(price_usd=2800.0)
    )
    update, context = _wallet_update_context(etherscan, paprika)
    await cmd_wallet(update, context)
    body = _last_reply(update.effective_message)
    assert "Wallet" in body
    # Address is normalised to lowercase before storage so the shortened
    # form is also lowercase. (Etherscan addresses are case-insensitive;
    # the EIP-55 mixed-case checksum is only a display affordance.)
    assert "0xd8da…6045" in body
    assert "ETH" in body
    # Total line is rendered when at least one chain has a USD price.
    assert "Total:" in body
    assert "Ethereum" in body
    assert "Recent transactions" in body
    # Outgoing tx renders with a leading minus sign.
    assert "-0.5" in body
    # Incoming tx renders with a leading plus sign.
    assert "+1.2" in body


@pytest.mark.asyncio
async def test_cmd_wallet_replies_with_usage_when_no_args() -> None:
    from zeenova_bot.handlers import cmd_wallet

    etherscan = MagicMock()
    etherscan.is_configured = MagicMock(return_value=True)
    etherscan.fetch_wallet = AsyncMock()
    update, context = _wallet_update_context(etherscan, None, args=[])
    await cmd_wallet(update, context)
    body = _last_reply(update.effective_message)
    assert "/wallet 0x" in body
    etherscan.fetch_wallet.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_wallet_rejects_invalid_address() -> None:
    from zeenova_bot.handlers import cmd_wallet

    etherscan = MagicMock()
    etherscan.is_configured = MagicMock(return_value=True)
    etherscan.fetch_wallet = AsyncMock()
    update, context = _wallet_update_context(
        etherscan, None, args=["not-an-address"]
    )
    await cmd_wallet(update, context)
    body = _last_reply(update.effective_message)
    assert "valid wallet address" in body
    etherscan.fetch_wallet.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_wallet_resolves_ens_name_to_address() -> None:
    """``/wallet vitalik.eth`` resolves to the canonical 0x address and
    renders the same card. The ENS name is shown next to the short
    address in the header."""
    from zeenova_bot.handlers import cmd_wallet

    etherscan = MagicMock()
    etherscan.is_configured = MagicMock(return_value=True)
    etherscan.fetch_wallet = AsyncMock(return_value=_wallet_info())
    paprika = MagicMock()
    paprika.fetch_price_snapshot = AsyncMock(
        return_value=MagicMock(price_usd=2800.0)
    )
    ens = MagicMock()
    ens.resolve = AsyncMock(
        return_value="0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
    )
    update, context = _wallet_update_context(
        etherscan, paprika, args=["vitalik.eth"]
    )
    context.bot_data["ens"] = ens
    await cmd_wallet(update, context)
    ens.resolve.assert_awaited_once_with("vitalik.eth")
    etherscan.fetch_wallet.assert_awaited_once_with(
        "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
    )
    body = _last_reply(update.effective_message)
    assert "vitalik.eth" in body
    assert "0xd8da…6045" in body


@pytest.mark.asyncio
async def test_cmd_wallet_reports_when_ens_does_not_resolve() -> None:
    from zeenova_bot.handlers import cmd_wallet

    etherscan = MagicMock()
    etherscan.is_configured = MagicMock(return_value=True)
    etherscan.fetch_wallet = AsyncMock()
    ens = MagicMock()
    ens.resolve = AsyncMock(return_value=None)
    update, context = _wallet_update_context(
        etherscan, None, args=["bogus-name.eth"]
    )
    context.bot_data["ens"] = ens
    await cmd_wallet(update, context)
    body = _last_reply(update.effective_message)
    assert "Couldn't resolve" in body
    assert "bogus-name.eth" in body
    etherscan.fetch_wallet.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_wallet_renders_every_supported_chain() -> None:
    """Every supported chain appears in the Balances grid even when
    its native balance is zero — the user should see the full
    footprint, including the long tail of EVM chains."""
    from zeenova_bot.etherscan import CHAINS
    from zeenova_bot.handlers import cmd_wallet

    etherscan = MagicMock()
    etherscan.is_configured = MagicMock(return_value=True)
    etherscan.fetch_wallet = AsyncMock(return_value=_wallet_info())
    paprika = MagicMock()
    paprika.fetch_price_snapshot = AsyncMock(
        return_value=MagicMock(price_usd=2800.0)
    )
    update, context = _wallet_update_context(etherscan, paprika)
    await cmd_wallet(update, context)
    body = _last_reply(update.effective_message)
    # The Balances grid lists every chain in CHAINS — old majors,
    # newer L2s and the long-tail of EVM chains alike.
    for chain in CHAINS:
        assert chain.name in body, f"expected {chain.name!r} in /wallet card"


@pytest.mark.asyncio
async def test_cmd_wallet_drops_data_source_footer() -> None:
    """The /wallet card no longer carries a trailing "Data: …" footer."""
    from zeenova_bot.handlers import cmd_wallet

    etherscan = MagicMock()
    etherscan.is_configured = MagicMock(return_value=True)
    etherscan.fetch_wallet = AsyncMock(return_value=_wallet_info())
    paprika = MagicMock()
    paprika.fetch_price_snapshot = AsyncMock(
        return_value=MagicMock(price_usd=2800.0)
    )
    update, context = _wallet_update_context(etherscan, paprika)
    await cmd_wallet(update, context)
    body = _last_reply(update.effective_message)
    assert "Data:" not in body
    assert "Etherscan" not in body  # neither in body nor footer


@pytest.mark.asyncio
async def test_cmd_wallet_activity_icon_uses_premium_slot() -> None:
    """Setting PREMIUM_EMOJI_WALLET_ACTIVITY_ID wraps the Activity
    header glyph in a ``<tg-emoji>`` tag — independent of every other
    Premium slot."""
    from zeenova_bot.handlers import cmd_wallet

    etherscan = MagicMock()
    etherscan.is_configured = MagicMock(return_value=True)
    etherscan.fetch_wallet = AsyncMock(return_value=_wallet_info())
    paprika = MagicMock()
    paprika.fetch_price_snapshot = AsyncMock(
        return_value=MagicMock(price_usd=2800.0)
    )
    settings = _settings()
    settings = settings.model_copy(
        update={"premium_emoji_wallet_activity_id": "5555555555555555555"}
    )
    update, context = _wallet_update_context(etherscan, paprika)
    context.bot_data["settings"] = settings
    await cmd_wallet(update, context)
    body = _last_reply(update.effective_message)
    assert 'emoji-id="5555555555555555555">📊</tg-emoji> <b>Activity</b>' in body


@pytest.mark.asyncio
async def test_cmd_wallet_hint_when_api_key_missing() -> None:
    """Without an API key, the bot replies with a setup hint instead of
    a confusing upstream error."""
    from zeenova_bot.handlers import cmd_wallet

    etherscan = MagicMock()
    etherscan.is_configured = MagicMock(return_value=False)
    etherscan.fetch_wallet = AsyncMock()
    update, context = _wallet_update_context(etherscan, None)
    await cmd_wallet(update, context)
    body = _last_reply(update.effective_message)
    assert "ETHERSCAN_API_KEY" in body
    etherscan.fetch_wallet.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_wallet_handles_upstream_failure() -> None:
    """When Etherscan returns None (transport error), the card collapses
    to a friendly "try again" message."""
    from zeenova_bot.handlers import cmd_wallet

    etherscan = MagicMock()
    etherscan.is_configured = MagicMock(return_value=True)
    etherscan.fetch_wallet = AsyncMock(return_value=None)
    paprika = MagicMock()
    paprika.fetch_price_snapshot = AsyncMock(return_value=None)
    update, context = _wallet_update_context(etherscan, paprika)
    await cmd_wallet(update, context)
    body = _last_reply(update.effective_message)
    assert "Couldn't reach Etherscan" in body


@pytest.mark.asyncio
async def test_cmd_wallet_renders_card_without_eth_price() -> None:
    """If every native-token price lookup fails, the card still
    renders — just without the Total line or per-row USD column."""
    from zeenova_bot.handlers import cmd_wallet

    etherscan = MagicMock()
    etherscan.is_configured = MagicMock(return_value=True)
    etherscan.fetch_wallet = AsyncMock(return_value=_wallet_info())
    paprika = MagicMock()
    paprika.fetch_price_snapshot = AsyncMock(return_value=None)
    update, context = _wallet_update_context(etherscan, paprika)
    await cmd_wallet(update, context)
    body = _last_reply(update.effective_message)
    # Without prices we never render the "Total: ≈ $X" summary line.
    assert "Total:" not in body
    # Balances grid still renders the per-chain row with the native
    # symbol and amount.
    assert "ETH" in body
    assert "Ethereum" in body


# ---------------------------------------------------------------------------
# /wallet — Solana branch.
# ---------------------------------------------------------------------------


# A real Solana pubkey (the Solana Foundation treasury) — valid base58,
# 44 chars long. Picking a real one over a synthetic string keeps the
# is_valid_solana_address regex honest.
_SOL_ADDR = "GThUX1Atko4tqhN2NaiTazWSeFWMuiUvfFnyJyUghFMJ"


def _sol_wallet_info(
    address: str = _SOL_ADDR,
    *,
    balance_sol: float = 12.5,
    recent: tuple[object, ...] = (),
    last_tx_at: int | None = None,
) -> object:
    from zeenova_bot.solana import LAMPORTS_PER_SOL, SolanaWalletInfo

    return SolanaWalletInfo(
        address=address,
        balance_lamports=int(balance_sol * LAMPORTS_PER_SOL),
        balance_sol=balance_sol,
        recent=recent,  # type: ignore[arg-type]
        last_tx_at=last_tx_at,
    )


def _sol_tx(
    signature: str = "5h3GthZ9X9wM4LhCpHyJ8Lc1aBcDeFgHiJkLmNoPqRsTuVwXyZaBcDeFgHiJkLmNoPqRsTuVwXyZ",
    *,
    timestamp: int = 1_700_000_000,
    is_failed: bool = False,
    fee_lamports: int = 5000,
) -> object:
    from zeenova_bot.solana import SolanaTx

    return SolanaTx(
        signature=signature,
        timestamp=timestamp,
        is_failed=is_failed,
        fee_lamports=fee_lamports,
    )


@pytest.mark.asyncio
async def test_cmd_wallet_routes_base58_address_to_solana_client() -> None:
    """A base58 address bypasses Etherscan entirely and hits the Solana
    RPC client instead — proving the dispatch happens in
    :func:`cmd_wallet` itself rather than being a lucky upstream
    accident."""
    from zeenova_bot.handlers import cmd_wallet

    etherscan = MagicMock()
    etherscan.is_configured = MagicMock(return_value=True)
    etherscan.fetch_wallet = AsyncMock()
    solana = MagicMock()
    solana.fetch_wallet = AsyncMock(return_value=_sol_wallet_info())
    paprika = MagicMock()
    paprika.fetch_price_snapshot = AsyncMock(
        return_value=MagicMock(price_usd=150.0)
    )
    update, context = _wallet_update_context(
        etherscan, paprika, args=[_SOL_ADDR], solana=solana
    )
    await cmd_wallet(update, context)
    # Etherscan is never touched for a base58 address.
    etherscan.fetch_wallet.assert_not_awaited()
    # Solana RPC receives the raw, case-sensitive base58 address.
    solana.fetch_wallet.assert_awaited_once_with(_SOL_ADDR)
    body = _last_reply(update.effective_message)
    assert "Solana" in body
    assert "SOL" in body
    # Total uses the USD price returned by paprika.
    assert "Total:" in body


@pytest.mark.asyncio
async def test_cmd_wallet_solana_renders_recent_signatures() -> None:
    """The recent-transactions grid shows the short signature and the
    success/fail status icon for each signature."""
    from zeenova_bot.handlers import cmd_wallet

    info = _sol_wallet_info(
        recent=(
            _sol_tx(
                signature="aaaaaaaaaaaaXYZ",
                timestamp=1_700_000_000,
                is_failed=False,
            ),
            _sol_tx(
                signature="bbbbbbbbbbbbWVU",
                timestamp=1_699_990_000,
                is_failed=True,
            ),
        ),
        last_tx_at=1_700_000_000,
    )
    etherscan = MagicMock()
    etherscan.is_configured = MagicMock(return_value=True)
    solana = MagicMock()
    solana.fetch_wallet = AsyncMock(return_value=info)
    paprika = MagicMock()
    paprika.fetch_price_snapshot = AsyncMock(
        return_value=MagicMock(price_usd=150.0)
    )
    update, context = _wallet_update_context(
        etherscan, paprika, args=[_SOL_ADDR], solana=solana
    )
    await cmd_wallet(update, context)
    body = _last_reply(update.effective_message)
    assert "Recent transactions" in body
    # Both signatures appear in short form.
    assert "aaaaaa…0XYZ" in body or "aaaaaa…aXYZ" in body
    # Success row shows the check icon; failed row shows the cross.
    assert "✓" in body
    assert "✗" in body
    assert "(failed)" in body


@pytest.mark.asyncio
async def test_cmd_wallet_solana_handles_upstream_failure() -> None:
    """When the Solana RPC returns ``None``, the bot replies with a
    friendly "try again" message — no Python traceback escapes."""
    from zeenova_bot.handlers import cmd_wallet

    etherscan = MagicMock()
    etherscan.is_configured = MagicMock(return_value=True)
    solana = MagicMock()
    solana.fetch_wallet = AsyncMock(return_value=None)
    paprika = MagicMock()
    paprika.fetch_price_snapshot = AsyncMock(return_value=None)
    update, context = _wallet_update_context(
        etherscan, paprika, args=[_SOL_ADDR], solana=solana
    )
    await cmd_wallet(update, context)
    body = _last_reply(update.effective_message)
    assert "Couldn't reach Solana RPC" in body


@pytest.mark.asyncio
async def test_cmd_wallet_solana_renders_without_sol_price() -> None:
    """When paprika has no SOL price, the card still renders — just
    without the Total summary or per-row USD column."""
    from zeenova_bot.handlers import cmd_wallet

    etherscan = MagicMock()
    etherscan.is_configured = MagicMock(return_value=True)
    solana = MagicMock()
    solana.fetch_wallet = AsyncMock(return_value=_sol_wallet_info())
    paprika = MagicMock()
    paprika.fetch_price_snapshot = AsyncMock(return_value=None)
    update, context = _wallet_update_context(
        etherscan, paprika, args=[_SOL_ADDR], solana=solana
    )
    await cmd_wallet(update, context)
    body = _last_reply(update.effective_message)
    assert "Total:" not in body
    # Balance row still renders the native amount + SOL symbol.
    assert "SOL" in body
    assert "Solana" in body


# ---------------------------------------------------------------------------
# /gas — live gas rates across chains.
# ---------------------------------------------------------------------------


def _gas_snap(chain_id: int, safe: float, std: float, fast: float) -> object:
    from zeenova_bot.etherscan import CHAINS, ChainGas, GasTier

    chain = next(c for c in CHAINS if c.id == chain_id)
    return ChainGas(
        chain=chain,
        tier=GasTier(safe_gwei=safe, standard_gwei=std, fast_gwei=fast),
    )


def _gas_update_context(
    etherscan: object,
    paprika: object | None,
) -> tuple[MagicMock, MagicMock]:
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    update = MagicMock()
    update.effective_message = msg
    update.effective_chat = MagicMock()
    context = MagicMock()
    bot_data: dict[str, object] = {"etherscan": etherscan, "settings": _settings()}
    if paprika is not None:
        bot_data["paprika"] = paprika
    context.bot_data = bot_data
    context.args = []
    return update, context


@pytest.mark.asyncio
async def test_cmd_gas_renders_card_with_chain_rows() -> None:
    """Happy path: every chain returns a gas snapshot. We render
    Safe/Standard/Fast rows and the chain headers."""
    from zeenova_bot.handlers import cmd_gas

    etherscan = MagicMock()
    etherscan.is_configured = MagicMock(return_value=True)
    etherscan.fetch_all_gas = AsyncMock(
        return_value=(
            _gas_snap(1, 18, 22, 28),
            _gas_snap(56, 3, 3, 5),
            _gas_snap(137, 30, 35, 40),
        )
    )
    paprika = MagicMock()
    paprika.fetch_price_snapshot = AsyncMock(
        return_value=MagicMock(price_usd=2800.0)
    )
    update, context = _gas_update_context(etherscan, paprika)
    await cmd_gas(update, context)
    body = _last_reply(update.effective_message)
    assert "Gas" in body
    assert "Ethereum" in body
    assert "BSC" in body
    assert "Polygon" in body
    assert "Safe:" in body
    assert "Standard:" in body
    assert "Fast:" in body
    assert "gwei" in body


@pytest.mark.asyncio
async def test_cmd_gas_hint_when_api_key_missing() -> None:
    """Without an API key the bot replies with a setup hint."""
    from zeenova_bot.handlers import cmd_gas

    etherscan = MagicMock()
    etherscan.is_configured = MagicMock(return_value=False)
    etherscan.fetch_all_gas = AsyncMock()
    update, context = _gas_update_context(etherscan, None)
    await cmd_gas(update, context)
    body = _last_reply(update.effective_message)
    assert "ETHERSCAN_API_KEY" in body
    etherscan.fetch_all_gas.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_gas_handles_empty_response() -> None:
    """When every chain's gas oracle is unavailable, the card still
    renders but with a friendly placeholder line."""
    from zeenova_bot.handlers import cmd_gas

    etherscan = MagicMock()
    etherscan.is_configured = MagicMock(return_value=True)
    etherscan.fetch_all_gas = AsyncMock(return_value=())
    paprika = MagicMock()
    paprika.fetch_price_snapshot = AsyncMock(return_value=None)
    update, context = _gas_update_context(etherscan, paprika)
    await cmd_gas(update, context)
    body = _last_reply(update.effective_message)
    assert "No gas data available" in body


@pytest.mark.asyncio
async def test_cmd_gas_renders_without_paprika_prices() -> None:
    """If price lookups fail, gas rows still render in gwei without
    the USD estimate."""
    from zeenova_bot.handlers import cmd_gas

    etherscan = MagicMock()
    etherscan.is_configured = MagicMock(return_value=True)
    etherscan.fetch_all_gas = AsyncMock(
        return_value=(_gas_snap(1, 18, 22, 28),)
    )
    paprika = MagicMock()
    paprika.fetch_price_snapshot = AsyncMock(return_value=None)
    update, context = _gas_update_context(etherscan, paprika)
    await cmd_gas(update, context)
    body = _last_reply(update.effective_message)
    assert "Ethereum" in body
    assert "gwei" in body
    # No USD estimate column when prices are unavailable.
    assert "~$" not in body

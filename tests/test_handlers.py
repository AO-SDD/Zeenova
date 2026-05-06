"""Tests for the calculator/FX bridge used by the message handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from zeenova_bot.handlers import _handle_calc, convert_with_fallback


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


def _calc_update_context(text: str, fx: AsyncMock, svc: AsyncMock) -> tuple[MagicMock, MagicMock]:
    """Wire up a minimal Update + Context pair for ``_handle_calc``."""
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    update = MagicMock()
    update.effective_message = msg
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

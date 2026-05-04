"""Tests for the textual price-card formatter."""

from __future__ import annotations

from zeenova_bot.card import _fmt_change, _fmt_compact, _fmt_price, render_price_card
from zeenova_bot.services import MarketData


def _md(**overrides: object) -> MarketData:
    base: dict[str, object] = dict(
        symbol="MEGA",
        pair="MEGAUSDT",
        source="binance",
        price_usd=0.1261,
        price_change_pct_24h=1.6,
        high_24h=0.1352,
        low_24h=0.1182,
        market_cap_usd=1_270_000_000,
        total_volume_usd_24h=20_570_000,
    )
    base.update(overrides)
    return MarketData(**base)  # type: ignore[arg-type]


_FOOTER_KW: dict[str, str] = dict(
    channel_name="Zeen Channel",
    channel_url="https://t.me/ox_zeen",
    group_name="Zeen Chat",
    group_url="https://t.me/blockzeen",
)


def test_fmt_price_picks_precision_by_magnitude() -> None:
    assert _fmt_price(12345.6789).startswith("$12,345.")
    assert _fmt_price(1.23) == "$1.2300"
    assert _fmt_price(0.0123) == "$0.0123"
    assert _fmt_price(0.00012345) == "$0.000123"
    assert _fmt_price(0.00000001) == "$0.00000001"


def test_fmt_compact_handles_scales_and_none() -> None:
    assert _fmt_compact(None) == "—"
    assert _fmt_compact(1_270_000_000) == "1.27B"
    assert _fmt_compact(20_570_000) == "20.57M"
    assert _fmt_compact(1_500) == "1.50K"
    assert _fmt_compact(42) == "42.00"


def test_fmt_change_signs() -> None:
    assert _fmt_change(None) == "—"
    assert _fmt_change(1.6) == "+1.60%"
    assert _fmt_change(-3.42) == "-3.42%"


def test_render_card_contains_expected_pieces() -> None:
    text = render_price_card(_md(), **_FOOTER_KW)
    # Pair styled as SYMBOL/USDT
    assert "MEGA/USDT" in text
    assert "Price:" in text
    assert "+1.60%" in text
    assert "1.27B" in text
    assert "20.57M USDT" in text
    # Footer carries the configured names + URLs
    assert "Zeen Channel" in text
    assert "Zeen Chat" in text
    assert "https://t.me/ox_zeen" in text
    assert "https://t.me/blockzeen" in text


def test_render_card_uses_red_dot_when_negative() -> None:
    text = render_price_card(_md(price_change_pct_24h=-2.5), **_FOOTER_KW)
    assert text.startswith("🔴")
    # Inline change line also flips to red.
    assert "🔴 <b>24H Change:</b> -2.50%" in text


def test_render_card_uses_green_dot_when_positive() -> None:
    text = render_price_card(_md(price_change_pct_24h=4.2), **_FOOTER_KW)
    assert text.startswith("🟢")
    assert "🟢 <b>24H Change:</b> +4.20%" in text


def test_render_card_includes_rank_when_present() -> None:
    text = render_price_card(_md(market_cap_rank=5), **_FOOTER_KW)
    # Rank now lives in the stats block (below Volume), not next to the title.
    assert "🏆 <b>Rank:</b> No: #5" in text
    # The title line should still be just the ticker, no rank attached.
    first_line = text.split("\n", 1)[0]
    assert "MEGA/USDT" in first_line
    assert "No:" not in first_line


def test_render_card_omits_rank_when_missing() -> None:
    text = render_price_card(_md(market_cap_rank=None), **_FOOTER_KW)
    assert "No: #" not in text
    assert "Rank:" not in text


def test_render_card_bolds_channel_and_group() -> None:
    text = render_price_card(_md(), **_FOOTER_KW)
    assert "<b>Zeen Channel</b>" in text
    assert "<b>Zeen Chat</b>" in text


def test_render_card_handles_missing_fields() -> None:
    text = render_price_card(
        _md(
            price_change_pct_24h=None,
            high_24h=None,
            low_24h=None,
            market_cap_usd=None,
            total_volume_usd_24h=None,
        ),
        **_FOOTER_KW,
    )
    assert text.count("—") >= 5

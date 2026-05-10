"""Render the textual price-card that accompanies the candlestick chart."""

from __future__ import annotations

from html import escape

from .services import MarketData


def _fmt_price(value: float) -> str:
    """Format a USD price with sensible precision."""
    if value <= 0:
        return "$0.00"
    if value >= 1000:
        return f"${value:,.2f}"
    if value >= 1:
        return f"${value:,.4f}"
    if value >= 0.01:
        return f"${value:.4f}"
    if value >= 0.0001:
        return f"${value:.6f}"
    return f"${value:.8f}".rstrip("0").rstrip(".") or "$0"


def _fmt_compact(value: float | None) -> str:
    """Compact human-readable amount: 1.27B, 20.57M, etc."""
    if value is None:
        return "—"
    abs_v = abs(value)
    for suffix, scale in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs_v >= scale:
            return f"{value / scale:,.2f}{suffix}"
    return f"{value:,.2f}"


def _fmt_change(pct: float | None) -> str:
    if pct is None:
        return "—"
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"


def render_price_card(md: MarketData) -> str:
    """Build the HTML message body for a price card.

    Layout mirrors the reference screenshot: a coloured ticker header,
    a vertical list of price stats with row-level emoji indicators.
    The channel + chat shortcuts that used to sit at the bottom are now
    attached as inline keyboard buttons by the caller, so the text body
    stays focused on the data.
    """
    pct = md.price_change_pct_24h
    is_up = pct is None or pct >= 0
    header_dot = "🟢" if is_up else "🔴"
    change_dot = "🟢" if is_up else "🔴"
    # Off-exchange pairs already arrive with a slash (e.g. "OCT/USD") so
    # we just trust whatever ``md.pair`` says when the slash is present.
    # Exchange pairs are concatenated (e.g. "BTCUSDT") and need a friendlier
    # display form.
    pair = md.pair if "/" in md.pair else f"{md.symbol}/USDT"

    price_str = _fmt_price(md.price_usd)
    change_str = _fmt_change(pct)
    high_str = _fmt_price(md.high_24h) if md.high_24h is not None else "—"
    low_str = _fmt_price(md.low_24h) if md.low_24h is not None else "—"
    cap_str = _fmt_compact(md.market_cap_usd) if md.market_cap_usd is not None else "—"
    if md.total_volume_usd_24h is not None:
        vol_str = f"{_fmt_compact(md.total_volume_usd_24h)} USDT"
    else:
        vol_str = "—"

    # Header is the ticker on its own line, then the stats block. Rank
    # sits at the top of the stats so the most-asked-for value is the
    # first thing the eye lands on.
    lines: list[str] = [
        f"{header_dot} <b>{escape(pair)}</b>",
        "",
    ]
    if md.market_cap_rank is not None and md.market_cap_rank > 0:
        lines.append(f"🏆 <b>Rank:</b> No: #{md.market_cap_rank}")
    lines.extend(
        [
            f"💵 <b>Price:</b> {escape(price_str)}",
            f"{change_dot} <b>24H Change:</b> {escape(change_str)}",
            f"🔼 <b>24H High:</b> {escape(high_str)}",
            f"🔽 <b>24H Low:</b> {escape(low_str)}",
            f"🏛 <b>Marketcap:</b> {escape(cap_str)}",
            f"📊 <b>24H Volume:</b> {escape(vol_str)}",
        ]
    )
    return "\n".join(lines)

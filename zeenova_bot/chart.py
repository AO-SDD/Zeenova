"""Candlestick chart rendering with a faint Zeenova watermark.

Renders an in-memory PNG that mirrors the dark style of the reference
screenshot: dark navy background, green/red candles, white axis labels, and
a large translucent ``Zeenova`` watermark across the middle.
"""

from __future__ import annotations

import io
import logging
from datetime import UTC, datetime

import matplotlib

matplotlib.use("Agg")  # noqa: E402

import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mticker  # noqa: E402
import mplfinance as mpf  # noqa: E402
import pandas as pd  # noqa: E402

from .timeframes import Timeframe  # noqa: E402

logger = logging.getLogger(__name__)


_BG = "#0f1622"
_GRID = "#1c2435"
_UP = "#26a69a"
_DOWN = "#ef5350"
_TEXT = "#d6deeb"
_ACCENT = "#5a90c9"


def _format_price(value: float) -> str:
    """Render ``value`` with enough precision for the magnitude.

    Big tickers like BTC at ~80,500 get a thousands separator and 2 dp
    so we always show "80,500.00" rather than "80500"; mid-cap tickers
    keep 4 dp; sub-dollar tokens keep up to 6 dp without losing zeros.
    """
    if value == 0:
        return "0.00"
    abs_v = abs(value)
    if abs_v >= 1000:
        return f"{value:,.2f}"
    if abs_v >= 1:
        return f"{value:,.4f}"
    if abs_v >= 0.01:
        return f"{value:,.5f}"
    return f"{value:,.8f}".rstrip("0").rstrip(".")


def _build_style() -> dict[str, object]:
    mc = mpf.make_marketcolors(
        up=_UP,
        down=_DOWN,
        edge={"up": _UP, "down": _DOWN},
        wick={"up": _UP, "down": _DOWN},
        volume="inherit",
    )
    style: dict[str, object] = mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        marketcolors=mc,
        facecolor=_BG,
        edgecolor=_GRID,
        figcolor=_BG,
        gridcolor=_GRID,
        gridstyle=":",
        rc={
            "axes.labelcolor": _TEXT,
            "axes.edgecolor": _GRID,
            "xtick.color": _TEXT,
            "ytick.color": _TEXT,
            "axes.titlecolor": _TEXT,
            "font.size": 10,
        },
    )
    return style


def render_candles(
    *,
    candles: list[list[float]],
    symbol: str,
    timeframe: Timeframe,
    brand_name: str,
) -> bytes:
    """Render a candlestick PNG and return its raw bytes.

    ``candles`` is a list of ``[ts_ms, open, high, low, close]`` rows in
    chronological order. At least 2 rows are required.
    """
    if len(candles) < 2:
        raise ValueError("need at least 2 candles to render a chart")

    df = pd.DataFrame(
        candles,
        columns=["ts", "Open", "High", "Low", "Close"],
    )
    df["Date"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("Date").drop(columns=["ts"])
    df = df.tail(80)

    style = _build_style()
    fig, axes = mpf.plot(
        df,
        type="candle",
        style=style,
        returnfig=True,
        figsize=(9.0, 5.0),
        tight_layout=False,
        ylabel="",
        xrotation=0,
        update_width_config={"candle_linewidth": 0.9, "candle_width": 0.6},
        datetime_format=_format_for(timeframe),
        axisoff=False,
    )
    ax = axes[0]
    ax.yaxis.tick_right()
    ax.yaxis.set_label_position("right")

    # Adaptive price formatter: thousands separator + magnitude-aware
    # precision so BTC shows "80,500.00" and BILL shows "0.038790".
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _p: _format_price(v)))
    ax.tick_params(axis="y", pad=2, labelsize=10)

    # mplfinance leaves a wide ~18% margin on every side. Override the
    # axes position(s) to fill the canvas almost edge-to-edge so the
    # candles use the full width. The right margin is just wide enough
    # to fit a "90,000.00"-style tick label without clipping.
    plot_box = (0.012, 0.085, 0.895, 0.905)
    for a in axes:
        a.set_position(plot_box)
        # Drop the box around the candles — frame-less look matches the
        # reference design.
        for spine in a.spines.values():
            spine.set_visible(False)

    # Header line: "SYMBOL | TIMEFRAME | brand"
    header = f"{symbol.upper()} | {timeframe.label} | {brand_name}"
    ax.text(
        0.012,
        0.965,
        header,
        transform=ax.transAxes,
        color=_ACCENT,
        fontsize=11,
        fontweight="bold",
        va="top",
        ha="left",
    )

    # Translucent Zeenova watermark centred over the candles
    ax.text(
        0.5,
        0.5,
        brand_name.upper(),
        transform=ax.transAxes,
        color=_TEXT,
        alpha=0.09,
        fontsize=72,
        fontweight="bold",
        ha="center",
        va="center",
        rotation=0,
    )

    # Footer: rendered timestamp (UTC) on the bottom-right
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    ax.text(
        0.988,
        0.018,
        now,
        transform=ax.transAxes,
        color=_TEXT,
        alpha=0.45,
        fontsize=8,
        ha="right",
        va="bottom",
    )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=_BG, dpi=150)
    plt.close(fig)
    return buf.getvalue()


def _format_for(tf: Timeframe) -> str:
    if tf.code in {"15m", "1h"}:
        return "%m-%d %H:%M"
    if tf.code == "4h":
        return "%m-%d %H:%M"
    return "%Y-%m-%d"

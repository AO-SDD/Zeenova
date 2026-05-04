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
        figsize=(8.5, 5.0),
        tight_layout=True,
        ylabel="",
        xrotation=0,
        update_width_config={"candle_linewidth": 0.9, "candle_width": 0.6},
        datetime_format=_format_for(timeframe),
        axisoff=False,
    )
    ax = axes[0]
    ax.yaxis.tick_right()
    ax.yaxis.set_label_position("right")

    # Header line: "SYMBOL | TIMEFRAME | brand"
    header = f"{symbol.upper()} | {timeframe.label} | {brand_name}"
    ax.text(
        0.012,
        0.965,
        header,
        transform=ax.transAxes,
        color=_ACCENT,
        fontsize=10,
        fontweight="bold",
        va="top",
        ha="left",
    )

    # Faint Zeenova watermark across the middle
    ax.text(
        0.5,
        0.5,
        brand_name.upper(),
        transform=ax.transAxes,
        color=_TEXT,
        alpha=0.08,
        fontsize=46,
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
    fig.savefig(buf, format="png", facecolor=_BG, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _format_for(tf: Timeframe) -> str:
    if tf.code in {"15m", "1h"}:
        return "%m-%d %H:%M"
    if tf.code == "4h":
        return "%m-%d %H:%M"
    return "%Y-%m-%d"

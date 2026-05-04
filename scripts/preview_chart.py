"""Render a sample candlestick chart for visual inspection."""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zeenova_bot.chart import render_candles  # noqa: E402
from zeenova_bot.timeframes import get_timeframe  # noqa: E402


def fake_candles(n: int = 60) -> list[list[float]]:
    now_ms = int(time.time() * 1000)
    out: list[list[float]] = []
    price = 0.1261
    for i in range(n):
        ts = now_ms - (n - i) * 15 * 60 * 1000
        open_ = price
        close_ = price * (1.0 + math.sin(i / 4.0) * 0.012)
        high_ = max(open_, close_) * 1.005
        low_ = min(open_, close_) * 0.995
        out.append([ts, open_, high_, low_, close_])
        price = close_
    return out


def main() -> None:
    out_dir = ROOT / "docs"
    out_dir.mkdir(parents=True, exist_ok=True)

    tf = get_timeframe("15m")
    png = render_candles(
        candles=fake_candles(),
        symbol="MEGA",
        timeframe=tf,
        brand_name="Zeenova",
    )
    out_path = out_dir / "example.png"
    out_path.write_bytes(png)
    print(f"wrote {out_path} ({len(png)} bytes)")


if __name__ == "__main__":
    main()

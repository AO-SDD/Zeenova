"""Crypto Fear & Greed Index client (CoinMarketCap).

A tiny, key-less client for the CMC Crypto Fear & Greed Index served at
``api.coinmarketcap.com/data-api/v3/fear-greed/chart``. This is the same
index Binance Square embeds on its app, so the value reported by
``/market`` lines up with what most users see on their other tools.

The dial PNG that goes with the value is rendered in-process by
:func:`render_dial` so the picture and the number are guaranteed to
agree (no race between a remote image and the API value).

The index is refreshed once per day, so we cache the last reading for
five minutes — short enough to pick up the daily roll-over quickly,
long enough to keep ``/market`` essentially free.
"""

from __future__ import annotations

import io
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Final

import httpx
from cachetools import TTLCache
from PIL import Image, ImageDraw, ImageFont

from .http import shared_async_client

logger = logging.getLogger(__name__)

# CoinMarketCap's data-api host. No key required for this endpoint, but
# we set a browser-y User-Agent so the WAF doesn't 403 us.
BASE_URL: Final[str] = "https://api.coinmarketcap.com"
_CHART_PATH: Final[str] = "/data-api/v3/fear-greed/chart"
_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass(slots=True)
class FearGreed:
    """Snapshot of the Crypto Fear & Greed Index."""

    value: int  # 0..100
    classification: str  # e.g. "Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed"


class FearGreedClient:
    """Read-only client for CoinMarketCap's Fear & Greed Index."""

    def __init__(self, timeout: float = 10.0, cache_ttl_s: float = 300.0) -> None:
        self._client = shared_async_client(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        # Single-row cache (the index has exactly one current value).
        self._cache: TTLCache[str, FearGreed | None] = TTLCache(
            maxsize=1, ttl=cache_ttl_s
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_current(self) -> FearGreed | None:
        """Return the latest index value, or ``None`` on any failure."""
        cached = self._cache.get("_", _MISSING)
        if cached is not _MISSING:
            return cached  # type: ignore[return-value]
        # Pull the last two days; CMC publishes one row per day, plus an
        # intraday "now" row, so two days is enough to always include
        # the most recent reading.
        end = int(time.time())
        start = end - 86400 * 2
        try:
            resp = await self._client.get(
                _CHART_PATH, params={"start": start, "end": end}
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Fear & Greed fetch failed: %s", exc)
            return None
        snap = _parse(payload)
        self._cache["_"] = snap
        return snap


# Sentinel so we can cache a "fetched but unparseable" result as None
# without confusing it with "never fetched".
_MISSING: object = object()


def _parse(payload: Any) -> FearGreed | None:
    """Pull the latest reading out of a CMC fear-greed/chart response.

    The response shape is::

        {"data": {"dataList": [
            {"score": 47, "name": "Neutral", "timestamp": "...", ...},
            ...,
            {"score": 50, "name": "Neutral", "timestamp": "..."}   # <- latest
        ]}}
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    rows = data.get("dataList")
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[-1]  # CMC orders ascending by timestamp
    if not isinstance(row, dict):
        return None
    raw_value = row.get("score")
    raw_classification = row.get("name")
    try:
        value = int(float(raw_value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not 0 <= value <= 100:
        return None
    classification = (
        raw_classification.strip()
        if isinstance(raw_classification, str) and raw_classification.strip()
        else _classify(value)
    )
    return FearGreed(value=value, classification=classification)


def _classify(value: int) -> str:
    """Bucket a 0..100 score into the standard label.

    Cut-offs match CMC + Binance Square: 0-24 Extreme Fear, 25-44 Fear,
    45-54 Neutral, 55-74 Greed, 75-100 Extreme Greed.
    """
    if value <= 24:
        return "Extreme Fear"
    if value <= 44:
        return "Fear"
    if value <= 54:
        return "Neutral"
    if value <= 74:
        return "Greed"
    return "Extreme Greed"


# ---------------------------------------------------------------------------
# Dial renderer
# ---------------------------------------------------------------------------

# Five-stop gradient tracing the standard Fear & Greed colour scale, the
# same one Binance Square uses on its app. ``(value_threshold, RGB)``;
# we linearly interpolate between adjacent stops.
_GRADIENT: Final[list[tuple[int, tuple[int, int, int]]]] = [
    (0, (234, 57, 67)),     # Extreme Fear — red
    (25, (238, 143, 28)),   # Fear — orange
    (50, (243, 212, 47)),   # Neutral — yellow
    (75, (147, 217, 0)),    # Greed — light green
    (100, (22, 199, 132)),  # Extreme Greed — green
]


def _color_for(value: int) -> tuple[int, int, int]:
    """Linearly interpolate the gradient at ``value``."""
    v = max(0, min(100, value))
    for i in range(len(_GRADIENT) - 1):
        v0, c0 = _GRADIENT[i]
        v1, c1 = _GRADIENT[i + 1]
        if v0 <= v <= v1:
            t = 0.0 if v1 == v0 else (v - v0) / (v1 - v0)
            return (
                int(c0[0] + (c1[0] - c0[0]) * t),
                int(c0[1] + (c1[1] - c0[1]) * t),
                int(c0[2] + (c1[2] - c0[2]) * t),
            )
    return _GRADIENT[-1][1]


def _try_load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Best-effort load of a bold sans-serif system font."""
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:\\Windows\\Fonts\\arialbd.ttf",
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


_DIAL_CACHE_SIZE: Final[int] = 128
_dial_cache: dict[tuple[int, str, str], bytes] = {}
_dial_cache_order: list[tuple[int, str, str]] = []


def render_dial(
    value: int,
    classification: str | None = None,
    *,
    brand: str | None = "Zeenova",
) -> bytes:
    """Render a Binance/CMC-style Fear & Greed dial as PNG bytes.

    Always succeeds; ``value`` is clamped to ``[0, 100]`` and the
    classification text falls back to :func:`_classify` if not provided.
    ``brand`` is shown as a watermark in the top-right corner; pass
    ``None`` or an empty string to disable.

    Output is memoised on ``(value, classification, brand)``; the
    Fear & Greed reading only changes every few minutes, so subsequent
    /market calls reuse the cached PNG bytes instead of paying ~50 ms
    of PIL rendering each time.
    """
    value = max(0, min(100, int(value)))
    label = (classification or _classify(value)).strip() or _classify(value)
    cache_key = (value, label, brand or "")
    cached = _dial_cache.get(cache_key)
    if cached is not None:
        return cached

    width, height = 800, 540
    bg = (15, 17, 21)  # near-black, blends into Telegram's dark theme
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    # Half-donut geometry. The arc spans 180° → 360° (the top half of a
    # circle in PIL coordinates). cx/cy is the centre of the *full*
    # circle; the visible arc lives in the upper half.
    cx, cy = width // 2, int(height * 0.78)
    outer_r = 240
    thickness = 56
    inner_r = outer_r - thickness

    # Draw the coloured arc as 100 thin segments — that gives a smooth
    # gradient without needing a real gradient brush.
    n_segments = 100
    span = 180.0  # degrees
    start_deg = 180.0
    seg_width = span / n_segments
    for i in range(n_segments):
        a0 = start_deg + i * seg_width
        a1 = a0 + seg_width + 0.5  # +0.5 to hide hairline gaps
        color = _color_for(i + 1)  # 1..100 maps to the gradient
        draw.pieslice(
            (cx - outer_r, cy - outer_r, cx + outer_r, cy + outer_r),
            start=a0,
            end=a1,
            fill=color,
        )
    # Punch the inner hole so we end up with a ring, not a pie.
    draw.ellipse(
        (cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r),
        fill=bg,
    )

    # Pointer triangle, sitting just inside the arc and pointing down at
    # the current value's position.
    angle_deg = 180.0 + (value / 100.0) * 180.0
    angle_rad = math.radians(angle_deg)
    tip_r = inner_r - 12
    tip_x = cx + tip_r * math.cos(angle_rad)
    tip_y = cy + tip_r * math.sin(angle_rad)
    base_r = tip_r - 28
    base_cx = cx + base_r * math.cos(angle_rad)
    base_cy = cy + base_r * math.sin(angle_rad)
    perp = angle_rad + math.pi / 2
    half_w = 14
    base_left = (
        base_cx + half_w * math.cos(perp),
        base_cy + half_w * math.sin(perp),
    )
    base_right = (
        base_cx - half_w * math.cos(perp),
        base_cy - half_w * math.sin(perp),
    )
    draw.polygon(
        [(tip_x, tip_y), base_left, base_right],
        fill=(220, 220, 220),
    )

    # Big centred value text + classification label underneath, with
    # comfortable breathing room between the two so they don't read as
    # one merged blob.
    value_text = str(value)
    label_text = label
    value_font = _try_load_font(120)
    label_font = _try_load_font(40)

    label_gap = 44  # px of empty space between the value and the label
    vw, vh = _text_size(draw, value_text, value_font)
    lw, lh = _text_size(draw, label_text, label_font)
    block_h = vh + label_gap + lh
    value_y = cy - block_h // 2
    label_y = value_y + vh + label_gap
    draw.text(
        (cx - vw // 2, value_y),
        value_text,
        font=value_font,
        fill=(255, 255, 255),
    )
    draw.text(
        (cx - lw // 2, label_y),
        label_text,
        font=label_font,
        fill=_color_for(value),
    )

    # Title at the top — keeps the image self-explanatory when forwarded.
    title_font = _try_load_font(36)
    title = "Fear & Greed Index"
    tw, _th = _text_size(draw, title, title_font)
    draw.text(
        ((width - tw) // 2, 28),
        title,
        font=title_font,
        fill=(200, 200, 200),
    )

    # Brand watermark in the bottom-left corner — quiet, no background
    # pill, sized to feel like a signature rather than competing with
    # the gauge for attention.
    if brand:
        brand_font = _try_load_font(20)
        bw, bh = _text_size(draw, brand, brand_font)
        margin = 18
        bx = margin
        by = height - bh - margin
        draw.text((bx, by), brand, font=brand_font, fill=(120, 128, 138))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    png = buf.getvalue()
    _dial_cache[cache_key] = png
    _dial_cache_order.append(cache_key)
    while len(_dial_cache_order) > _DIAL_CACHE_SIZE:
        evicted = _dial_cache_order.pop(0)
        _dial_cache.pop(evicted, None)
    return png


def _text_size(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> tuple[int, int]:
    """Return ``(width, height)`` of ``text`` rendered with ``font``.

    Pillow 10 dropped ``draw.textsize``; ``textbbox`` is the supported
    replacement.
    """
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return int(right - left), int(bottom - top)

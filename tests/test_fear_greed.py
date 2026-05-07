"""Tests for the Fear & Greed Index client, parser, and dial renderer."""

from __future__ import annotations

import io
from typing import Any

import pytest
from PIL import Image

from zeenova_bot.fear_greed import FearGreed, _classify, _parse, render_dial


def _payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"data": {"dataList": rows}}


def test_parse_extracts_latest_value_and_classification() -> None:
    """CMC orders rows ascending; we take the last one."""
    payload = _payload(
        [
            {"score": 47, "name": "Neutral", "timestamp": "1778000000"},
            {"score": 50, "name": "Neutral", "timestamp": "1778100000"},
            {"score": 55, "name": "Greed", "timestamp": "1778200000"},
        ]
    )
    snap = _parse(payload)
    assert snap == FearGreed(value=55, classification="Greed")


def test_parse_falls_back_to_local_classification() -> None:
    """If the API drops ``name`` we still classify locally."""
    payload = _payload([{"score": 10}])
    snap = _parse(payload)
    assert snap is not None
    assert snap.value == 10
    assert snap.classification == "Extreme Fear"


def test_parse_accepts_string_score() -> None:
    payload = _payload([{"score": "50", "name": "Neutral"}])
    snap = _parse(payload)
    assert snap == FearGreed(value=50, classification="Neutral")


def test_parse_rejects_garbage() -> None:
    assert _parse(None) is None
    assert _parse("nope") is None
    assert _parse({}) is None
    assert _parse({"data": {}}) is None
    assert _parse(_payload([])) is None
    assert _parse(_payload([{"score": "abc"}])) is None
    # Out of range.
    assert _parse(_payload([{"score": 150}])) is None
    assert _parse(_payload([{"score": -1}])) is None


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "Extreme Fear"),
        (24, "Extreme Fear"),
        (25, "Fear"),
        (44, "Fear"),
        (45, "Neutral"),
        (50, "Neutral"),
        (54, "Neutral"),
        (55, "Greed"),
        (74, "Greed"),
        (75, "Extreme Greed"),
        (100, "Extreme Greed"),
    ],
)
def test_classify_buckets(value: int, expected: str) -> None:
    assert _classify(value) == expected


# ---------------------------------------------------------------------------
# Dial renderer
# ---------------------------------------------------------------------------


def test_render_dial_returns_valid_png_bytes() -> None:
    data = render_dial(50, "Neutral")
    assert isinstance(data, bytes)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(io.BytesIO(data))
    assert img.format == "PNG"
    assert img.size[0] > 0 and img.size[1] > 0


@pytest.mark.parametrize("value", [-10, 0, 25, 50, 75, 100, 150])
def test_render_dial_clamps_value(value: int) -> None:
    """Out-of-range scores must not crash the renderer."""
    data = render_dial(value, "Greed")
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_dial_falls_back_when_classification_missing() -> None:
    data = render_dial(80)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_dial_handles_empty_classification() -> None:
    data = render_dial(50, "   ")
    assert data[:8] == b"\x89PNG\r\n\x1a\n"

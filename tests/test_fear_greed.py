"""Tests for the Fear & Greed Index client and parser."""

from __future__ import annotations

from typing import Any

import pytest

from zeenova_bot.fear_greed import FearGreed, _classify, _parse


def test_parse_extracts_value_and_classification() -> None:
    payload: dict[str, Any] = {
        "name": "Fear and Greed Index",
        "data": [
            {
                "value": "55",
                "value_classification": "Greed",
                "timestamp": "1700000000",
                "time_until_update": "12345",
            }
        ],
        "metadata": {"error": None},
    }
    snap = _parse(payload)
    assert snap == FearGreed(value=55, classification="Greed")


def test_parse_falls_back_to_local_classification() -> None:
    """If the API drops ``value_classification`` we still classify locally."""
    payload: dict[str, Any] = {"data": [{"value": "10"}]}
    snap = _parse(payload)
    assert snap is not None
    assert snap.value == 10
    assert snap.classification == "Extreme Fear"


def test_parse_rejects_garbage() -> None:
    assert _parse(None) is None
    assert _parse("nope") is None
    assert _parse({}) is None
    assert _parse({"data": []}) is None
    assert _parse({"data": [{"value": "abc"}]}) is None
    # Out of range.
    assert _parse({"data": [{"value": "150"}]}) is None
    assert _parse({"data": [{"value": "-1"}]}) is None


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "Extreme Fear"),
        (24, "Extreme Fear"),
        (25, "Fear"),
        (49, "Fear"),
        (50, "Neutral"),
        (51, "Greed"),
        (74, "Greed"),
        (75, "Extreme Greed"),
        (100, "Extreme Greed"),
    ],
)
def test_classify_buckets(value: int, expected: str) -> None:
    assert _classify(value) == expected

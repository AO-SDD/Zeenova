"""Verify the MEXC client maps Binance-style interval codes correctly.

MEXC's klines endpoint rejects ``1h`` with ``Invalid interval``; the
client must translate it to ``60m`` before sending the request.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from zeenova_bot.mexc import _MEXC_INTERVALS, MexcClient


def test_interval_map_translates_1h_to_60m() -> None:
    assert _MEXC_INTERVALS["1h"] == "60m"
    # Other codes pass through unchanged because MEXC accepts them as-is.
    assert _MEXC_INTERVALS["15m"] == "15m"
    assert _MEXC_INTERVALS["4h"] == "4h"
    assert _MEXC_INTERVALS["1d"] == "1d"


@pytest.mark.asyncio
async def test_fetch_klines_translates_1h_for_mexc() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json=[
                [1_700_000_000_000, "1.0", "1.1", "0.9", "1.05", "100", 0, "0"],
            ],
        )

    transport = httpx.MockTransport(handler)
    client = MexcClient()
    # Swap the underlying httpx client for one that uses our mock transport.
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://api.mexc.com", transport=transport
    )

    rows = await client.fetch_klines("BILL", "1h", limit=80)

    await client.aclose()
    assert rows is not None and len(rows) == 1
    # The bot asks for "1h" but MEXC must receive "60m".
    assert "interval=60m" in captured["url"]
    assert "interval=1h" not in captured["url"]

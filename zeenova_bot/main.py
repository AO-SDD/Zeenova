"""Bot entrypoint."""

from __future__ import annotations

import asyncio
import logging
import sys

from telegram.ext import Application

from .binance import BinanceClient
from .bybit import BybitClient
from .coingecko import MarketcapClient as CoinGeckoMarketcap
from .coinpaprika import CoinPaprikaClient
from .config import load_settings
from .handlers import build_application
from .marketcap import MarketcapAggregator
from .mexc import MexcClient
from .services import CoinService


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    settings = load_settings()
    _configure_logging(settings.log_level)
    log = logging.getLogger(__name__)

    binance = BinanceClient()
    bybit = BybitClient()
    mexc = MexcClient()
    paprika = CoinPaprikaClient()
    coingecko = CoinGeckoMarketcap(api_key=settings.coingecko_api_key)
    marketcap = MarketcapAggregator(paprika, coingecko)
    service = CoinService(
        binance=binance, bybit=bybit, mexc=mexc, marketcap=marketcap
    )

    app = build_application(settings, service)

    async def _post_init(_: Application) -> None:  # type: ignore[type-arg]
        # Warm Binance + MEXC pair caches so the first user click doesn't
        # pay the ~1-2 s ``exchangeInfo`` round-trip. Bybit is allowed to
        # fail (it's geo-blocked from this region) — its client logs the
        # 403 once and stops retrying.
        async def _warm(name: str, coro: object) -> None:
            try:
                await coro  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001
                log.warning("warm-up: %s failed: %s", name, exc)

        await asyncio.gather(
            _warm("binance", binance.has_pair("BTC")),
            _warm("mexc", mexc.has_pair("BTC")),
            _warm("bybit", bybit.has_pair("BTC")),
        )

    async def _post_shutdown(_: Application) -> None:  # type: ignore[type-arg]
        await service.aclose()

    # PTB v21 exposes ``post_init`` / ``post_shutdown`` as configurable hooks.
    app.post_init = _post_init
    app.post_shutdown = _post_shutdown

    log.info(
        "Zeenova bot starting up "
        "(sources: Binance + Bybit + MEXC, marketcap: CoinPaprika -> CoinGecko cached)"
    )
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Shutdown requested")
    except Exception:  # noqa: BLE001
        logging.exception("Fatal error in main loop")
        sys.exit(1)

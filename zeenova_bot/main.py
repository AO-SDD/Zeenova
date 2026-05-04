"""Bot entrypoint."""

from __future__ import annotations

import logging
import sys

from telegram.ext import Application

from .binance import BinanceClient
from .bybit import BybitClient
from .coingecko import MarketcapClient
from .config import load_settings
from .handlers import build_application
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
    marketcap = MarketcapClient(api_key=settings.coingecko_api_key)
    service = CoinService(binance=binance, bybit=bybit, marketcap=marketcap)

    app = build_application(settings, service)

    async def _post_shutdown(_: Application) -> None:  # type: ignore[type-arg]
        await service.aclose()

    # PTB v21 exposes ``post_shutdown`` as a configurable hook on Application
    app.post_shutdown = _post_shutdown

    log.info("Zeenova bot starting up (sources: Binance + Bybit, marketcap: CoinGecko cached)")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Shutdown requested")
    except Exception:  # noqa: BLE001
        logging.exception("Fatal error in main loop")
        sys.exit(1)

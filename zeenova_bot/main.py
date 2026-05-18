"""Bot entrypoint."""

from __future__ import annotations

import asyncio
import logging
import sys

from telegram import BotCommand
from telegram.ext import Application

from .binance import BinanceClient
from .bybit import BybitClient
from .coingecko import MarketcapClient as CoinGeckoMarketcap
from .coinpaprika import CoinPaprikaClient
from .config import load_settings
from .ens import EnsClient
from .etherscan import EtherscanClient
from .fear_greed import FearGreedClient
from .fx import FxClient
from .handlers import build_application
from .marketcap import MarketcapAggregator
from .mexc import MexcClient
from .news import NewsClient
from .quote_sticker import QuoteStickerClient
from .services import CoinService
from .solana import DEFAULT_RPC_URL as DEFAULT_SOLANA_RPC_URL
from .solana import SolanaClient

# Slash-command menu published to Telegram on startup. Telegram shows
# these in the autocomplete drawer when users type ``/`` and in the
# attach-menu button next to the input field. Keep descriptions short
# (Telegram caps them at 256 chars but truncates much earlier in the UI).
_BOT_COMMANDS: tuple[BotCommand, ...] = (
    BotCommand("p", "Price card for a coin (e.g. /p btc)"),
    BotCommand("market", "Global marketcap, BTC dominance, Fear & Greed"),
    BotCommand("top", "Today's top gainers and losers"),
    BotCommand("news", "Latest crypto headlines"),
    BotCommand("ath", "All-time high / low for a coin (e.g. /ath btc)"),
    BotCommand("wallet", "Multichain wallet summary (e.g. /wallet 0x…)"),
    BotCommand("gas", "Live gas rates across EVM chains"),
    BotCommand("help", "How to use the bot"),
)


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
    # CoinPaprika doubles as the off-exchange price source — when a coin
    # isn't on Binance/Bybit/MEXC, we still get a USD price + marketcap
    # from the same /tickers call we already use for marketcap data.
    service = CoinService(
        binance=binance,
        bybit=bybit,
        mexc=mexc,
        marketcap=marketcap,
        off_exchange=paprika,
    )
    fx = FxClient()
    fear_greed = FearGreedClient()
    quote_sticker = QuoteStickerClient()
    news = NewsClient()
    etherscan = EtherscanClient(api_key=settings.etherscan_api_key)
    ens = EnsClient()
    solana = SolanaClient(
        rpc_url=settings.solana_rpc_url or DEFAULT_SOLANA_RPC_URL,
    )

    app = build_application(settings, service, fx)
    # /top and /market need the raw CoinPaprika client (the rest of the
    # bot only sees the marketcap aggregator).
    app.bot_data["paprika"] = paprika
    # /market also overlays the Fear & Greed Index from alternative.me.
    app.bot_data["fear_greed"] = fear_greed
    # Reply-with-"z" turns the parent message into a quote sticker via
    # bot.lyo.su.
    app.bot_data["quote_sticker"] = quote_sticker
    # /news pulls latest headlines from a few mainstream crypto outlets'
    # public RSS feeds and dedupes them client-side.
    app.bot_data["news"] = news
    # /ath calls CoinGecko directly for ATH/ATL data (one shared client
    # with marketcap; the cache is keyed separately).
    app.bot_data["coingecko"] = coingecko
    # /wallet uses Etherscan V2 (one key, 60+ chains, only ETH for now).
    app.bot_data["etherscan"] = etherscan
    # /wallet also accepts ENS names (e.g. ``vitalik.eth``) resolved
    # through a public free gateway. No API key required.
    app.bot_data["ens"] = ens
    # /wallet routes base58 addresses to the Solana JSON-RPC client.
    # No API key required for the default public RPC.
    app.bot_data["solana"] = solana

    async def _post_init(application: Application) -> None:  # type: ignore[type-arg]
        # Publish the slash-command menu so Telegram's native autocomplete
        # surfaces our commands when users type ``/`` in a chat.
        try:
            await application.bot.set_my_commands(_BOT_COMMANDS)
        except Exception as exc:  # noqa: BLE001
            log.warning("set_my_commands failed: %s", exc)

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
        await fx.aclose()
        await fear_greed.aclose()
        await quote_sticker.aclose()
        await news.aclose()
        await etherscan.aclose()
        await ens.aclose()

    # PTB v21 exposes ``post_init`` / ``post_shutdown`` as configurable hooks.
    app.post_init = _post_init
    app.post_shutdown = _post_shutdown

    log.info(
        "Zeenova bot starting up "
        "(sources: Binance + Bybit + MEXC, marketcap: CoinPaprika -> CoinGecko cached, "
        "fx: fawazahmed0/currency-api)"
    )
    # ``edited_message`` is required for edit-to-edit replies: when the
    # user edits a calc/price-card message we re-run the handler and
    # edit the bot's previous reply in place (see
    # :func:`zeenova_bot.handlers.on_edited_text`). Telegram does NOT
    # deliver edited messages unless the bot explicitly subscribes to
    # them via ``allowed_updates``.
    app.run_polling(
        allowed_updates=[
            "message",
            "edited_message",
            "callback_query",
            "inline_query",
        ]
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Shutdown requested")
    except Exception:  # noqa: BLE001
        logging.exception("Fatal error in main loop")
        sys.exit(1)

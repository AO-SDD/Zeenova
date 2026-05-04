# Zeenova Coin Info Bot

Telegram bot that replies to a coin symbol in any chat (or via `/p SYMBOL`)
with a candlestick chart and a live market-data card — price, 24h change,
high / low, market cap and volume.

Built for the **Zeenova** community channels:

- Channel: <https://t.me/ox_zeen>
- Group:   <https://t.me/blockzeen>

![example card](docs/example.png)

## Features

- 🟢 / 🔴 price card matching Zeenova's branding (compact `1.27B` / `20.57M`).
- Candlestick chart on a dark theme with a faint **Zeenova** watermark.
- Inline timeframe buttons: **15M / 1H / 4H / 1D**.
- CoinGecko-powered universe — supports thousands of coins, not just Binance pairs.
- Binance fallback for the chart when a `<SYMBOL>USDT` pair exists, so
  intervals are exact (15m / 1h / 4h / 1d) instead of CoinGecko's coarse
  auto-bucketed candles.
- Works in DMs and groups. In groups, supports both free-text symbols
  (e.g. `BTC`, `$ETH`, `MEGA`) and the `/p SYMBOL` command.

## Requirements

- Python **3.11+**
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- (Optional) A CoinGecko Pro API key — only needed if you outgrow the
  free tier's rate limits.

## Setup

```bash
git clone https://github.com/AO-SD/Zeenova.git
cd Zeenova

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and paste your TELEGRAM_BOT_TOKEN

python -m zeenova_bot.main
```

### Telegram setup

1. Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → paste the
   token into `.env` as `TELEGRAM_BOT_TOKEN`.
2. To make the bot react to plain coin symbols (not just `/p`) inside a
   group, **disable Privacy Mode**:
   `/setprivacy` → choose your bot → **Disable**.
3. Add the bot to your group / channel as an admin (or member, if Privacy
   Mode is disabled it will see everything).

### Restricting the bot to specific chats

Set `ALLOWED_CHAT_IDS` in `.env` to a comma-separated list of chat IDs to
limit free-text triggers to those chats. Leave it blank to allow
everywhere.

## Docker

```bash
cp .env.example .env
# edit .env
docker compose up -d --build
```

## Development

```bash
make dev-install
make lint typecheck test
```

The repository ships with:

- `ruff` for linting / formatting (line length 100, strict rules)
- `mypy` in strict mode (`zeenova_bot` package only)
- `pytest` + `pytest-asyncio` for unit tests

CI runs the same three commands on every push (`.github/workflows/ci.yml`).

## Architecture

```
zeenova_bot/
├── main.py          # Bot entrypoint, wires everything together
├── config.py        # Pydantic settings loaded from .env
├── coingecko.py     # Async CoinGecko client + symbol→id resolver
├── binance.py       # Async Binance kline fetcher (chart upgrade)
├── services.py      # Combines CoinGecko + Binance for the handlers
├── timeframes.py    # 15M / 1H / 4H / 1D definitions
├── card.py          # Renders the HTML message body
├── chart.py         # Renders the candlestick PNG (mplfinance)
└── handlers.py      # python-telegram-bot wiring (commands, text, callbacks)
```

When a user sends a symbol:

1. `coingecko.resolve_symbol` finds the highest-marketcap coin matching
   that ticker (so `PEPE` → Pepe, not a meme-coin clone).
2. `services.market` fetches current price/marketcap/volume.
3. `services.candles` tries Binance for a clean 15m kline first, then
   falls back to CoinGecko's `/coins/{id}/ohlc` endpoint.
4. `chart.render_candles` produces a PNG with the Zeenova watermark.
5. `card.render_price_card` builds the HTML caption.
6. The bot sends the photo with the inline 15M / 1H / 4H / 1D keyboard.
   Tapping a button edits the same message in place.

## License

MIT — see `LICENSE`.

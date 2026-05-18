# Zeenova Coin Info Bot

Telegram bot that turns any chat into a crypto desk — live prices, charts,
calculator, conversions, and a market overview, all in one place.

Built for the **Zeen** community channels:

- Channel: <https://t.me/ox_zeen>
- Group:   <https://t.me/blockzeen>

![example card](docs/example.png)

## Features

### Live prices & charts
- Plain coin symbols (`btc`, `$eth`, `MEGA`) trigger a candlestick chart on
  a dark Zeenova-themed canvas plus a colour-coded price card (price, 24h
  change, high / low, marketcap, volume, rank).
- Inline timeframe buttons: **15M / 1H / 4H / 1D**.
- Resolves symbols across **Binance → Bybit → MEXC → CoinPaprika** so even
  small / off-exchange coins (`OCT`, `OPG`, …) get a price card. The
  off-exchange tail keeps things working for tickers that no major spot
  exchange lists yet.
- `/p SYMBOL` works in DMs and groups when free-text triggers are off.

### Inline mode
- Type `@<your_bot> btc` in **any** chat — even chats the bot isn't in —
  and pick a coin from the suggestions to forward a fresh price card.
  Requires inline mode to be enabled in BotFather (see *Telegram setup*).

### Market overview
- `/market` — total marketcap, 24h volume, BTC dominance, active coin
  count, plus a **Fear & Greed Index** dial rendered locally so the
  picture and caption value always agree (sourced from CoinMarketCap, the
  same index Binance Square uses).
- `/top` — the day's biggest gainers and losers from the top 100 by
  marketcap.
- `/news` — latest English-language crypto headlines aggregated from
  CoinDesk, Cointelegraph, and Decrypt via their public RSS feeds (no
  API key required). Deduplicated by URL and cached for 5 minutes.
- `/ath SYMBOL` — all-time-high / all-time-low snapshot for any coin:
  ATH price + date + how far below ATH the current price sits, plus the
  same for ATL. Sourced from CoinGecko, cached for an hour per coin.
- `/wallet 0x…`, `/wallet name.eth`, or `/wallet <solana base58>` —
  multichain wallet summary. EVM addresses (and resolved ENS names)
  show native-token balance and USD value across 20 EVM chains
  (Ethereum, BSC, Polygon, Arbitrum, Optimism, Base, Avalanche, Linea,
  Blast, Mantle, Sonic, Unichain, Berachain, Gnosis, Celo, Sei,
  Moonbeam, HyperEVM, Abstract, Plasma — every chain is shown, even
  zero-balance rows), with a combined total and the 5 most recent
  transactions on the wallet's most-active chain. Solana addresses are
  detected automatically (base58, 32–44 chars) and routed through the
  Solana JSON-RPC for the SOL balance, USD value, and 5 most recent
  signatures with their success/fail status. ENS names are resolved
  through a public free gateway so you can look up wallets by their
  ENS handle (e.g. `vitalik.eth`). EVM coverage is powered by the free
  Etherscan V2 API (one key, 60+ chains); Solana coverage uses the
  public mainnet RPC by default and can be pointed at a paid provider
  via `SOLANA_RPC_URL`.
- `/gas` — live gas rates (Safe / Standard / Fast in gwei) on every
  supported chain — chains without a gastracker oracle fall back to
  `eth_gasPrice` so you still get a reading. Each tier is annotated
  with an approximate USD cost for a 21k-gas native transfer. Uses the
  same Etherscan V2 key as `/wallet`; results are cached for 20 s per
  chain.

### Calculator & conversions
- Plain math with full operator precedence: `2+3*4`, `(1+2)*3`, `2^10`,
  `10%3`.
- Suffixes: `1k`, `1.5m`, `2.5b`, `1.2t`.
- Percent operator: `100+10%` = `110`, `1000-0.1%` = `999`, `5%` = `0.05`.
- Thousands separators in numeric literals: `37,632.00 + 30%` parses as
  `37,632.00 * 1.30`, `1,000,000 / 4` works as expected.
- Multi-line input: paste two calculations on separate lines (e.g.
  `2/1` then `2*2`) and the bot returns both results in a single reply.
- Currency conversion: `5 usd egp`, `300 star`, `3 usdt egp`,
  `1k mnt usd`. Worldwide fiat coverage; crypto symbols always win
  ambiguity (so `MNT` = Mantle, not Mongolian Tugrik).
- Group-friendly: stays silent on bare expressions like `50%` so it
  doesn't interrupt casual chat.
- **Edit-to-edit**: edit your original message (fix a number, swap a
  symbol like `btx` → `btc`, change `/p btc` → `/p eth`) and the bot
  edits its previous reply in place instead of sending a new one.

### Quote stickers
- Reply to any text message with just `z` (or `Z`) and the bot will turn
  the quoted message into a sticker — same idea as `@QuotLyBot`, in your
  own bot.

## Requirements

- Python **3.11+**
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- (Optional) A CoinGecko Pro API key — only needed if you outgrow the free
  tier's rate limits. The bot defaults to the public CoinPaprika /
  CoinGecko endpoints, no key required.

## Setup

```bash
git clone https://github.com/AO-SDD/Zeenova.git
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
3. To enable inline mode (`@<bot> btc` in any chat):
   `/setinline` → choose your bot → optionally set a placeholder like
   `Search a coin (BTC, ETH, OCT)…`.
4. Add the bot to your group / channel as an admin (or member, if Privacy
   Mode is disabled it will see everything).

### Restricting the bot to specific chats

Set `ALLOWED_CHAT_IDS` in `.env` to a comma-separated list of chat IDs to
limit free-text triggers to those chats. Leave it blank to allow
everywhere.

### Available environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | — | Token from BotFather. |
| `COINGECKO_API_KEY` | no | empty | Optional Pro key. |
| `ETHERSCAN_API_KEY` | no | empty | Etherscan V2 key (free at [etherscan.io/apis](https://etherscan.io/apis)) — powers `/wallet` and `/gas`. Without it the bot replies with a short setup hint when either command is invoked. |
| `SOLANA_RPC_URL` | no | empty (uses `https://api.mainnet-beta.solana.com`) | Solana JSON-RPC endpoint. Override to a paid provider (Helius, QuickNode, …) when the public RPC starts rate-limiting. Most providers embed the secret in the URL itself. |
| `PREMIUM_EMOJI_ATH_*_ID`, `PREMIUM_EMOJI_DIAMOND_ID`, `PREMIUM_EMOJI_DATE_ID`, `PREMIUM_EMOJI_PCT_DOWN_ID`, `PREMIUM_EMOJI_ATL_GAIN_ID` | no | empty | Premium custom-emoji IDs for the `/ath` card. `PREMIUM_EMOJI_ATL_GAIN_ID` decorates the "% from ATL" gain row independently of the ATH section header (falls back to `PREMIUM_EMOJI_ATH_UP_ID` when empty). See `.env.example` for the full list. |
| `PREMIUM_EMOJI_WALLET_ID`, `PREMIUM_EMOJI_CLOCK_ID`, `PREMIUM_EMOJI_WALLET_ACTIVITY_ID` | no | empty | Premium custom-emoji IDs for the `/wallet` header, recent-transactions section, and Activity section. |
| `PREMIUM_EMOJI_GAS_ID` | no | empty | Premium custom-emoji ID for the `/gas` header. |
| `PREMIUM_EMOJI_HELP_HEADER_ID`, `PREMIUM_EMOJI_HELP_PRICES_ID`, `PREMIUM_EMOJI_HELP_MARKET_ID`, `PREMIUM_EMOJI_HELP_CALC_ID`, `PREMIUM_EMOJI_HELP_FIAT_ID`, `PREMIUM_EMOJI_HELP_GROUP_ID` | no | empty | Premium custom-emoji IDs for the `/start` and `/help` section icons (brand header, Live prices, Market overview, Calculator, Worldwide currencies, Group-friendly). Each one falls back to its plain emoji when unset. |
| `ALLOWED_CHAT_IDS` | no | empty | Comma-separated chat IDs. |
| `BRAND_NAME` | no | `Zeenova` | Watermark on chart + F&G dial. |
| `CHANNEL_NAME` | no | `Zeen Channel` | Label on the channel shortcut button. |
| `GROUP_NAME` | no | `Zeen Chat` | Label on the chat shortcut button. |
| `TELEGRAM_CHANNEL_URL` | no | `https://t.me/ox_zeen` | URL behind the channel button. |
| `TELEGRAM_GROUP_URL` | no | `https://t.me/blockzeen` | URL behind the chat button. |
| `BRAND_CHANNEL_EMOJI_ID` | no | empty | Telegram Premium custom-emoji ID rendered on the channel button. Requires the bot owner to have Telegram Premium. Use `/emojiid` (reply to a message containing the emoji) to discover the ID. When set, the fallback 📣 emoji is dropped from the label so the button only shows the Premium icon. |
| `BRAND_GROUP_EMOJI_ID` | no | empty | Telegram Premium custom-emoji ID rendered on the chat button. Same Premium requirement as above. When set, the fallback 💬 emoji is dropped from the label. |
| `PREMIUM_EMOJI_UP_ID` | no | empty | Premium custom-emoji ID for 🟢 (positive 24h change / Gainers). Same Premium requirement as the brand buttons. Use `/emojiid` to discover. |
| `PREMIUM_EMOJI_DOWN_ID` | no | empty | Premium custom-emoji ID for 🔴 (negative 24h change / Losers). |
| `PREMIUM_EMOJI_CHANGE_ID` | no | empty | Optional override: when set, the **24H Change** row uses this single Premium custom-emoji ID regardless of whether the move is positive or negative. The header dot keeps tracking direction with `_UP_ID` / `_DOWN_ID`. Leave blank to keep 24H Change in sync with the header dot. |
| `PREMIUM_EMOJI_RANK_ID` | no | empty | Premium custom-emoji ID for 🏆 (coin rank line). |
| `PREMIUM_EMOJI_PRICE_ID` | no | empty | Premium custom-emoji ID for 💵 (Price line). |
| `PREMIUM_EMOJI_HIGH_ID` | no | empty | Premium custom-emoji ID for 🔼 (24H High line). |
| `PREMIUM_EMOJI_LOW_ID` | no | empty | Premium custom-emoji ID for 🔽 (24H Low line). |
| `PREMIUM_EMOJI_MCAP_ID` | no | empty | Premium custom-emoji ID for 🏛 (Marketcap line). |
| `PREMIUM_EMOJI_VOLUME_ID` | no | empty | Premium custom-emoji ID for 📊 (24H Volume line). |
| `PREMIUM_EMOJI_GLOBE_ID` | no | empty | Premium custom-emoji ID for 🌐 (`/market` header). |
| `PREMIUM_EMOJI_BTC_ID` | no | empty | Premium custom-emoji ID for 🟠 (BTC dominance line). |
| `PREMIUM_EMOJI_COINS_ID` | no | empty | Premium custom-emoji ID for 🪙 (Active coins line). |
| `PREMIUM_EMOJI_FNG_ID` | no | empty | Premium custom-emoji ID for the Fear & Greed face (one ID covers all five glyphs 😱/😨/😐/🙂/🤑). |
| `PREMIUM_EMOJI_TOP_ID` | no | empty | Premium custom-emoji ID for 📈 (`/top` header). |
| `PREMIUM_EMOJI_NEWS_ID` | no | empty | Premium custom-emoji ID for 📰 (`/news` header). |
| `LOG_LEVEL` | no | `INFO` | Standard Python logging level. |

## Docker

```bash
cp .env.example .env
# edit .env
docker compose up -d --build
```

> For a full step-by-step deployment guide (Docker, AWS EC2, plain
> Linux + systemd, local dev, Premium-emoji setup, updates, logs,
> troubleshooting) see [DEPLOYMENT.md](DEPLOYMENT.md).

## Development

```bash
make dev-install
make lint typecheck test
```

The repository ships with:

- `ruff` for linting / formatting (line length 100, strict rules)
- `mypy` in strict mode (`zeenova_bot` package only)
- `pytest` + `pytest-asyncio` for unit tests (235+ tests)

CI runs the same three commands on every push (`.github/workflows/ci.yml`).

## Architecture

```
zeenova_bot/
├── main.py          # Entrypoint; wires every client + handler together
├── config.py        # Pydantic settings loaded from .env
├── http.py          # Shared httpx client config (pool sizing, timeouts)
├── handlers.py      # python-telegram-bot wiring (commands, text, inline, callbacks)
├── services.py      # CoinService — resolves a symbol against all data sources
│
├── binance.py       # Primary price + kline source (deepest liquidity)
├── bybit.py         # First fallback (geo-blocked in some regions)
├── mexc.py          # Second fallback (widest free USDT catalogue)
├── coinpaprika.py   # Off-exchange snapshot for thinly-listed coins + /top + /market
├── coingecko.py     # Marketcap fallback when CoinPaprika doesn't know a ticker
├── marketcap.py     # CoinPaprika → CoinGecko marketcap aggregator
│
├── fear_greed.py    # CMC Fear & Greed client + local PIL dial renderer (cached)
├── news.py          # RSS news aggregator (CoinDesk, Cointelegraph, Decrypt)
├── fx.py            # Worldwide fiat + crypto conversion (cached)
├── calc.py          # Calculator: precedence, suffixes, %, conversions
├── quote_sticker.py # bot.lyo.su client for `z`-reply quote stickers
├── card.py          # Renders the HTML price-card body
├── chart.py         # Renders the candlestick PNG (mplfinance, off-loop)
└── timeframes.py    # 15M / 1H / 4H / 1D definitions
```

### Performance notes

- Every outbound HTTP client funnels through `http.shared_async_client`
  (200 max connections / 100 keepalive / 5 s connect timeout) so bursts
  of concurrent traffic reuse warm sockets.
- The Fear & Greed dial PNG is memoised on `(value, classification,
  brand)` and rendered in a dedicated thread pool, so /market answers
  almost instantly on repeat calls.
- Candlestick charts render in a single-worker thread pool because
  matplotlib's pyplot global state is not thread-safe.
- `AIORateLimiter` respects Telegram's per-chat (1 msg/s) and global
  (30 msg/s) caps automatically, so a single busy group can't trigger a
  global FloodWait.

## License

MIT — see `LICENSE`.

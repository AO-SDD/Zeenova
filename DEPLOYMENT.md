# Zeenova Deployment Guide

A practical, copy-paste guide for getting the bot running on a fresh
server. Covers Docker (the easy path), AWS EC2 from zero, plain Linux
with `systemd` (no Docker), and local development. All commands are
verified against Ubuntu 22.04 / 24.04 LTS but apply to most Debian-based
distributions.

> If you've already deployed the bot once and just need to update or
> tweak something, jump straight to [Updating](#updating) or
> [Troubleshooting](#troubleshooting).

## Table of contents

1. [Prerequisites](#1-prerequisites)
2. [Get a Telegram bot token from @BotFather](#2-get-a-telegram-bot-token-from-botfather)
3. [Deployment option A — Docker on any Linux host (recommended)](#3-deployment-option-a--docker-on-any-linux-host-recommended)
4. [Deployment option B — AWS EC2 from scratch](#4-deployment-option-b--aws-ec2-from-scratch)
5. [Deployment option C — Bare-metal Linux with `systemd` (no Docker)](#5-deployment-option-c--bare-metal-linux-with-systemd-no-docker)
6. [Deployment option D — Local development](#6-deployment-option-d--local-development)
7. [Environment variables — quick reference](#7-environment-variables--quick-reference)
8. [Premium custom-emoji setup](#8-premium-custom-emoji-setup)
9. [Etherscan API key setup (`/wallet`)](#8b-etherscan-api-key-setup-wallet)
10. [Updating](#9-updating)
11. [Logs and monitoring](#10-logs-and-monitoring)
12. [Troubleshooting](#11-troubleshooting)

---

## 1. Prerequisites

You need:

- **A Linux server** with public internet access. The bot is light — a
  small VPS (1 vCPU / 1 GB RAM) is enough for a busy community group.
  AWS `t3.micro` or `t4g.small`, DigitalOcean's `s-1vcpu-1gb`,
  Hetzner's `CX11`, Oracle Free Tier `VM.Standard.A1.Flex` (ARM) — any
  of these are fine.
- **A Telegram account** to talk to [@BotFather](https://t.me/BotFather)
  and create the bot. If you want Telegram **Premium** features
  (custom-emoji icons on price cards and buttons), the bot must be
  owned by an account that has Premium — see
  [section 8](#8-premium-custom-emoji-setup).
- **(Optional) A CoinGecko Pro API key.** The bot defaults to the free
  public CoinGecko / CoinPaprika endpoints and works fine without a
  paid key. Only add one if you outgrow the free tier (~30 req/min).

You do **not** need:

- A database — the bot is stateless.
- A web server / reverse proxy — it polls Telegram outbound.
- Any inbound ports open — the bot makes outbound HTTPS calls only.

---

## 2. Get a Telegram bot token from @BotFather

Same step for every deployment option.

1. Open Telegram, find [@BotFather](https://t.me/BotFather), press
   **Start**.
2. Send `/newbot`. Pick a display name, then a username ending in `bot`
   (e.g. `ZeenovaCoinBot`).
3. BotFather replies with a token that looks like
   `123456789:AAH-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`. **Save this** —
   it's `TELEGRAM_BOT_TOKEN` in `.env` later. Treat it like a password:
   anyone with the token controls your bot.
4. In the same chat, send these to unlock the bot's features:
   - `/setprivacy` → pick your bot → **Disable**. Lets the bot react to
     plain coin symbols (`btc`, `eth`) in groups, not just `/p`.
   - `/setinline` → pick your bot → set a placeholder like
     `Search a coin (BTC, ETH, OCT)…`. Enables `@<your_bot> btc` inline
     queries in any chat.
   - `/setcommands` → pick your bot → paste this block so the slash
     menu auto-populates in clients:
     ```
     p - Get price card for a coin (e.g. /p btc)
     market - Global crypto market overview + Fear & Greed
     top - Today's top gainers and losers
     news - Latest crypto news headlines
     emojiid - (Premium owners) reply with this to discover a custom-emoji ID
     help - How to use the bot
     ```

That's it for the Telegram side — the rest happens on your server.

---

## 3. Deployment option A — Docker on any Linux host (recommended)

This is the path used in production. Docker handles the Python
toolchain, system fonts, and process supervision in one shot.

### 3.1 Install Docker

On a fresh Ubuntu / Debian box:

```bash
# Add Docker's official GPG key and repo (one-time).
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Let your user run docker without sudo (re-login after this).
sudo usermod -aG docker $USER
newgrp docker
```

Verify:

```bash
docker --version            # → Docker version 27.x.x
docker compose version      # → Docker Compose version v2.x.x
```

### 3.2 Clone the repo and configure

```bash
git clone https://github.com/AO-SDD/Zeenova.git
cd Zeenova

cp .env.example .env
nano .env       # paste your TELEGRAM_BOT_TOKEN, optionally tweak BRAND_*
```

At minimum your `.env` needs `TELEGRAM_BOT_TOKEN=...`. Everything else
has sensible defaults — see [section 7](#7-environment-variables--quick-reference).

### 3.3 Start the bot

```bash
docker compose up -d --build
```

`--build` re-builds the image from the local `Dockerfile`, `-d` runs it
detached. `restart: unless-stopped` is already set in
`docker-compose.yml`, so the container will come back after a host
reboot automatically.

Check it's alive:

```bash
docker compose ps           # STATUS should say "Up X seconds"
docker compose logs --tail=50 -f
```

You should see a startup banner like:

```
Zeenova bot starting up (sources: Binance + Bybit + MEXC, marketcap: CoinPaprika -> CoinGecko cached, fx: fawazahmed0/currency-api)
Application started
```

Now send `/start` to your bot on Telegram. If you get a reply, you're
done. If not, jump to [Troubleshooting](#11-troubleshooting).

### 3.4 Daily operations cheat sheet

```bash
# Tail live logs.
docker compose logs -f

# Restart with new code (after git pull).
docker compose up -d --build

# Restart to pick up new .env values (NO --build needed, no .env reload on `restart`).
docker compose up -d

# Stop / start without rebuilding.
docker compose stop
docker compose start

# Nuke the container (config + image stay).
docker compose down
```

> ⚠️ **`docker compose restart` does NOT re-read `.env`.** It just sends
> SIGHUP to the existing process with the same env it was started with.
> After editing `.env`, always use `docker compose up -d` (with or
> without `--build`) so Compose recreates the container with the new
> env values.

---

## 4. Deployment option B — AWS EC2 from scratch

End-to-end walkthrough for getting the bot running on a fresh AWS
account. Roughly 10 minutes total.

### 4.1 Launch the instance

1. Sign in to the [AWS Console](https://console.aws.amazon.com/), open
   **EC2** → **Instances** → **Launch instances**.
2. **Name**: `zeenova-bot`.
3. **AMI**: *Ubuntu Server 24.04 LTS (HVM)* — free tier eligible.
4. **Instance type**: `t3.micro` (x86) or `t4g.small` (ARM/Graviton —
   slightly cheaper). The bot runs comfortably on either.
5. **Key pair**: create a new key pair if you don't have one. **Save
   the `.pem` file** — you'll need it to SSH in.
6. **Network settings** → **Edit**:
   - VPC: default is fine.
   - Auto-assign public IP: **Enable**.
   - Security group → **Create new** → name `zeenova-sg`. Allow
     **SSH (22)** from **My IP** only. **Do NOT open any other ports**
     — the bot polls Telegram outbound and needs zero inbound traffic.
7. **Configure storage**: 8 GB gp3 is plenty.
8. **Launch instance**, wait for State = *Running*, copy the **Public
   IPv4 address**.

### 4.2 SSH in and prep the box

From your laptop:

```bash
chmod 600 ~/Downloads/zeenova-key.pem    # required by ssh
ssh -i ~/Downloads/zeenova-key.pem ubuntu@<EC2_PUBLIC_IP>
```

On the EC2 box, run:

```bash
sudo apt-get update && sudo apt-get upgrade -y
```

### 4.3 Install Docker and deploy

Run the Docker install + clone + `docker compose up -d --build` steps
from [section 3](#3-deployment-option-a--docker-on-any-linux-host-recommended).
They work identically on EC2.

### 4.4 Make it survive instance reboot

`docker-compose.yml` already declares `restart: unless-stopped`, so the
container restarts on boot **once the Docker daemon is running**. Make
sure Docker starts on boot:

```bash
sudo systemctl enable docker
sudo systemctl status docker      # should say "enabled"
```

Reboot once to verify everything comes back automatically:

```bash
sudo reboot
# wait 30s, SSH back in
docker compose ps                 # bot should be Up
```

### 4.5 (Optional) Lock down SSH

Once the bot is running, you rarely need to SSH in. Tighten security:

- Use AWS **Systems Manager Session Manager** instead of SSH if you
  want to remove port 22 entirely. Attach the
  `AmazonSSMManagedInstanceCore` role to the instance, then connect
  from the EC2 console.
- Or restrict the security-group SSH rule to your office / VPN CIDR
  block instead of `0.0.0.0/0`.

### 4.6 Cost ballpark

- `t4g.small` on-demand in `us-east-1` ≈ **$12/month**. Free tier
  covers 750 h of `t3.micro` or `t4g.small` for the first 12 months.
- 8 GB gp3 EBS ≈ **$0.64/month**.
- Outbound traffic to Telegram + a few crypto APIs ≈ **a few cents/month**.

Total: under $15/month after free-tier, free for the first year.

---

## 5. Deployment option C — Bare-metal Linux with `systemd` (no Docker)

If you'd rather not run Docker (e.g. on a Raspberry Pi, or on a host
with custom security rules), the bot runs fine as a plain Python
service supervised by `systemd`.

### 5.1 Install system dependencies

```bash
sudo apt-get update
sudo apt-get install -y \
    python3.11 python3.11-venv python3-pip \
    git \
    fonts-dejavu-core libfreetype6 libpng16-16
```

> The `fonts-dejavu-core` package is what gives the candlestick chart
> watermark its bold label. Without it the chart still renders but the
> brand text falls back to a generic font.

If Python 3.11 isn't in your distro's repos (older Ubuntu / Debian),
use [deadsnakes](https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa)
or build from source. Anything 3.11 or newer works.

### 5.2 Set up the bot user and clone

```bash
sudo useradd --system --create-home --shell /bin/bash zeenova
sudo -u zeenova -i

# Now running as the zeenova user.
git clone https://github.com/AO-SDD/Zeenova.git
cd Zeenova

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env       # paste TELEGRAM_BOT_TOKEN

# Quick smoke test — Ctrl+C after you see "Application started".
python -m zeenova_bot.main
```

### 5.3 Create the systemd unit

Exit back to your sudo-capable user (`exit`) and write:

```bash
sudo tee /etc/systemd/system/zeenova-bot.service >/dev/null <<'EOF'
[Unit]
Description=Zeenova Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=zeenova
Group=zeenova
WorkingDirectory=/home/zeenova/Zeenova
EnvironmentFile=/home/zeenova/Zeenova/.env
ExecStart=/home/zeenova/Zeenova/.venv/bin/python -m zeenova_bot.main
Restart=always
RestartSec=10
# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/zeenova/Zeenova
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true

[Install]
WantedBy=multi-user.target
EOF
```

Then enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now zeenova-bot
sudo systemctl status zeenova-bot      # should say "active (running)"
```

### 5.4 systemd operations cheat sheet

```bash
# Live logs.
sudo journalctl -u zeenova-bot -f

# Last 200 lines.
sudo journalctl -u zeenova-bot -n 200 --no-pager

# Restart after editing .env or pulling new code.
sudo systemctl restart zeenova-bot

# Stop / start.
sudo systemctl stop zeenova-bot
sudo systemctl start zeenova-bot

# Disable autostart (won't come back after reboot).
sudo systemctl disable zeenova-bot
```

---

## 6. Deployment option D — Local development

For hacking on the code on your laptop. Same steps as option C minus
the `systemd` unit.

```bash
git clone https://github.com/AO-SDD/Zeenova.git
cd Zeenova

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

cp .env.example .env
# edit .env, paste your BOT TOKEN (use a *separate* test bot from BotFather
# so dev sessions don't conflict with your production bot).

# Run the full check suite before pushing.
make lint typecheck test

# Run the bot interactively.
python -m zeenova_bot.main
# Ctrl+C to stop.
```

Two bots are better than one for development: create a second bot in
BotFather (`@MyBotDevBot`) and point your local `.env` at it. That way
your production bot keeps running on the server uninterrupted.

---

## 7. Environment variables — quick reference

| Variable | Required | Default | Notes |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | **yes** | — | From [@BotFather](https://t.me/BotFather). |
| `COINGECKO_API_KEY` | no | empty | Optional CoinGecko Pro key. |
| `ETHERSCAN_API_KEY` | no | empty | Etherscan V2 key (free at [etherscan.io/apis](https://etherscan.io/apis)) — powers `/wallet 0x…`. One key works across 60+ chains via the V2 multichain API. Leave blank to disable `/wallet`. |
| `ALLOWED_CHAT_IDS` | no | empty | Comma-separated chat IDs to restrict free-text triggers. Leave blank to allow everywhere. |
| `BRAND_NAME` | no | `Zeenova` | Watermark on chart + F&G dial. |
| `CHANNEL_NAME` / `GROUP_NAME` | no | `Zeen Channel` / `Zeen Chat` | Labels on the channel/chat shortcut buttons. |
| `TELEGRAM_CHANNEL_URL` / `TELEGRAM_GROUP_URL` | no | Zeen links | URLs behind the shortcut buttons. |
| `BRAND_CHANNEL_EMOJI_ID` / `BRAND_GROUP_EMOJI_ID` | no | empty | Premium custom-emoji on the shortcut buttons. See [section 8](#8-premium-custom-emoji-setup). |
| `PREMIUM_EMOJI_*` (15 IDs) | no | empty | Premium custom-emoji icons across the price card, `/market`, `/top`, `/news`. See [section 8](#8-premium-custom-emoji-setup). |
| `LOG_LEVEL` | no | `INFO` | Standard Python logging level (`DEBUG` / `INFO` / `WARNING` / `ERROR`). |

The README has the full table with one row per `PREMIUM_EMOJI_*` ID.

---

## 8. Premium custom-emoji setup

Telegram Premium lets bots render **animated custom-emoji** icons
inside message bodies and on inline-keyboard buttons. The bot supports
this on the price card, the brand shortcut buttons, and the headers of
`/market`, `/top`, and `/news` — but it's **opt-in**, and requires:

1. **The bot owner's Telegram account has Premium.** Premium is checked
   per-bot, not per-user — the user who *talks to* the bot does not
   need Premium. The account that *created* the bot in BotFather does.
   If you transfer the bot to a non-Premium account later, the
   Premium emojis silently stop appearing.
2. **A valid custom-emoji ID for each icon you want to override.**

### 8.1 Discover a custom-emoji ID

The bot ships a helper command for this. Once it's running:

1. In a DM with your bot, send a single Premium emoji (any animated
   emoji with a small star ⭐ in its sticker thumbnail).
2. **Reply** to that message with `/emojiid`.
3. The bot replies with the numeric ID. Copy it.

Repeat for every icon you want to replace.

### 8.2 Wire the IDs into `.env`

Append the IDs you want (with **no `<` `>` angle brackets** — those
were placeholders in the chat instructions). Examples:

```bash
# Brand buttons
BRAND_CHANNEL_EMOJI_ID=6260052174089229782
BRAND_GROUP_EMOJI_ID=6260052174089229782

# Price-card row icons
PREMIUM_EMOJI_UP_ID=6084922627736996647
PREMIUM_EMOJI_DOWN_ID=6087173448298138241
PREMIUM_EMOJI_RANK_ID=6257777039718227924
PREMIUM_EMOJI_PRICE_ID=6158844873236551570
PREMIUM_EMOJI_HIGH_ID=6258028827880986221
PREMIUM_EMOJI_LOW_ID=6260016835098317800
PREMIUM_EMOJI_MCAP_ID=5359778044745622115
PREMIUM_EMOJI_VOLUME_ID=6084477132254218612

# (Optional) override 24H Change row with one fixed icon regardless of direction
PREMIUM_EMOJI_CHANGE_ID=6087173448298138241

# /market row icons
PREMIUM_EMOJI_GLOBE_ID=...
PREMIUM_EMOJI_BTC_ID=...
PREMIUM_EMOJI_COINS_ID=...
PREMIUM_EMOJI_FNG_ID=...

# /top, /news
PREMIUM_EMOJI_TOP_ID=...
PREMIUM_EMOJI_NEWS_ID=...
```

Any ID you leave blank keeps its default unicode emoji — they're fully
independent, you can enable just the ones you want.

After saving `.env`:

```bash
# Docker
docker compose up -d              # NOT `restart` — must recreate to reload .env

# systemd
sudo systemctl restart zeenova-bot
```

### 8.3 Remove a Premium emoji and go back to default

To restore the unicode emoji on a specific row, delete its line from
`.env` (or set it to empty), then `docker compose up -d` (or restart
the systemd service). To wipe **all** Premium emojis at once:

```bash
sed -i '/^PREMIUM_EMOJI_/d' .env
sed -i '/^BRAND_.*_EMOJI_ID=/d' .env
docker compose up -d
```

---

## 8b. Etherscan API key setup (`/wallet`)

The `/wallet 0x…` command needs a free Etherscan V2 API key to look up
Ethereum wallet balances and transactions. Without a key, the bot
replies to `/wallet` with a short setup hint and the command no-ops.

**Steps:**

1. Sign up at [etherscan.io/register](https://etherscan.io/register) —
   no payment info required.
2. Open the [API-KEYs dashboard](https://etherscan.io/myapikey) and
   click **Add**. Give the key a name (e.g. `zeenova-bot`).
3. Copy the generated key (40-character string) and add it to `.env`:
   ```bash
   echo "ETHERSCAN_API_KEY=YOUR_KEY_HERE" >> .env
   ```
4. Recreate the container so the new env var loads (`up -d`, **not**
   `restart`):
   ```bash
   docker compose up -d
   ```
5. Test in DM:
   ```
   /wallet 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045
   ```
   That's vitalik.eth — should return a wallet card with ETH balance,
   USD value, and recent transactions.

**Notes:**

- The free tier is **5 req/s and 100,000 req/day** — plenty for a
  community bot. Each `/wallet` call uses ~3 API requests.
- One key works across **60+ chains** via the V2 multichain API
  (Polygon, BSC, Arbitrum, Optimism, Base, etc.). The bot only queries
  Ethereum mainnet for now but the same key is forward-compatible for
  future chain support.
- Results are cached per-address for 60 seconds to avoid hammering
  Etherscan on rapid retries.

---

## 9. Updating

Whenever new code lands in `main`:

### Docker

```bash
cd /path/to/Zeenova
git fetch origin
git checkout main
git pull
docker compose up -d --build      # rebuild image + recreate container
```

`--build` is what picks up the new code. Without it, Compose just
recreates the container from the old image.

### systemd

```bash
cd /home/zeenova/Zeenova
sudo -u zeenova git fetch origin
sudo -u zeenova git checkout main
sudo -u zeenova git pull
sudo -u zeenova .venv/bin/pip install -r requirements.txt
sudo systemctl restart zeenova-bot
```

### Confirm the new code is running

```bash
# Docker
docker compose logs --tail=20

# systemd
sudo journalctl -u zeenova-bot -n 20 --no-pager
```

You should see a fresh "Zeenova bot starting up" line stamped with the
current time.

---

## 10. Logs and monitoring

### Docker

```bash
docker compose logs -f                       # live tail
docker compose logs --tail=200               # last 200 lines
docker compose logs --since=1h               # last hour
```

Docker rotates logs automatically. If you want a hard cap on disk
usage, edit `docker-compose.yml` and add:

```yaml
services:
  bot:
    # … existing fields …
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
```

Then `docker compose up -d` to apply.

### systemd

```bash
sudo journalctl -u zeenova-bot -f            # live tail
sudo journalctl -u zeenova-bot -n 200        # last 200 lines
sudo journalctl -u zeenova-bot --since "1 hour ago"
```

Journal logs are persisted by default on modern Ubuntu / Debian.

### What "healthy" looks like

A working bot logs about 1–10 lines per minute when traffic is light,
mostly HTTP status lines from upstream price feeds. You should see:

- One **"Zeenova bot starting up …"** line per start.
- Periodic `httpx` debug lines (only at `LOG_LEVEL=DEBUG`).
- Occasional warmup failures from `binance` / `mexc` / `bybit` during
  regional outages — these are non-fatal, the bot falls through to the
  next provider.

Errors worth caring about:

- `Conflict: terminated by other getUpdates request` — **two copies of
  the bot are running with the same token.** Kill one (e.g.
  `docker compose down` on the old host). The bot self-recovers once
  the duplicate is gone.
- `Unauthorized` — the token is wrong or was revoked in BotFather.
- `Telegram says: Forbidden: bot was blocked by the user` — harmless,
  just a user who blocked the bot.

---

## 11. Troubleshooting

### "I edited my `.env` and nothing changed"

`docker compose restart` does **not** re-read `.env`. Use
`docker compose up -d` instead (no `--build` if code didn't change).
On systemd, `sudo systemctl restart zeenova-bot` does re-read it
because `EnvironmentFile=` is consulted on every start.

### "The bot doesn't reply at all"

1. Check that the container / service is actually running:
   ```bash
   docker compose ps                       # or
   sudo systemctl status zeenova-bot
   ```
2. Check the logs for an `Unauthorized` error — that means a bad
   `TELEGRAM_BOT_TOKEN`. Grab a fresh token with `/revoke` in
   BotFather → repaste.
3. Check the logs for `Conflict: terminated by other getUpdates …`.
   You're running the bot in two places with the same token; stop one.
4. Try `/start` in a DM with the bot. If even that doesn't respond,
   the bot can't reach Telegram. Verify outbound HTTPS works:
   ```bash
   curl -s https://api.telegram.org/bot$TOKEN/getMe | head
   ```
   You should get a JSON blob with `"ok":true`.

### "Plain symbols like `btc` work in DM but not in groups"

You haven't disabled Privacy Mode. In BotFather:
`/setprivacy` → pick your bot → **Disable**. Remove and re-add the bot
to the group afterwards (so it gets the new permissions).

### "Premium custom emoji don't show up in the chat / group, only the bot's DM"

This is almost always one of three things:

1. **The bot owner doesn't have Telegram Premium.** Verify by asking
   another Premium user to view the message — if *they* see the
   custom emoji but free users don't, Premium ownership is fine and
   this is a viewer-side issue. If *nobody* sees them, the bot's
   owning account isn't Premium.
2. **`.env` has angle brackets around the ID** (e.g. `<6260...>`).
   Strip them — IDs must be bare numbers. Run
   `grep PREMIUM .env` to spot-check.
3. **You used `docker compose restart` instead of `up -d`** — the new
   IDs never reached the container. See *"I edited my `.env` and
   nothing changed"* above.

### "Editing a message doesn't update the bot's reply"

The bot must be subscribed to `edited_message` updates. This is the
default in modern versions of the bot — verify by running:

```bash
grep -A4 run_polling zeenova_bot/main.py
```

You should see `"edited_message"` in the `allowed_updates` list. If
it's missing, `git pull` to get the latest code.

### "ModuleNotFoundError" / "matplotlib font cache" errors at startup

You're running the bot directly with `python` (not Docker) and the
system fonts package is missing. Install it:

```bash
sudo apt-get install -y fonts-dejavu-core libfreetype6 libpng16-16
```

### "I want to wipe the bot and start over"

```bash
# Docker
docker compose down
docker image prune -f
git pull
docker compose up -d --build

# systemd
sudo systemctl stop zeenova-bot
sudo systemctl disable zeenova-bot
sudo rm /etc/systemd/system/zeenova-bot.service
sudo systemctl daemon-reload
sudo userdel -r zeenova       # also deletes /home/zeenova
```

### Still stuck?

- Open an issue with the last ~30 lines of `docker compose logs` (or
  `journalctl`) attached.
- For Telegram-specific quirks, the [Bot API docs](https://core.telegram.org/bots/api)
  are usually the answer.

# BadgerBot Hyper

Signal-driven perpetual trading bot for [Hyperliquid](https://hyperliquid.xyz). Receives live signals from [BadgerBot](https://badgerbot.io) via WebSocket, executes trades automatically, and lets you monitor and control everything through Telegram.

---

## Choose Your Path

| | Path A: Render | Path B: VPS / Ubuntu |
|---|---|---|
| Experience needed | None | Basic terminal |
| Setup time | ~15 min | ~20 min |
| Monthly cost | ~$8 | ~$4+ |
| Server management | None (browser only) | SSH access |
| Best for | Getting started fast | Lower cost, full control |

→ Jump to [Path A: Render (Cloud)](#path-a-render-cloud)\
→ Jump to [Path B: VPS / Ubuntu Server](#path-b-vps--ubuntu-server)

---

## Before You Start

Both paths need the same three things. Get these ready before you begin.

### 1. Hyperliquid API Wallet

Your main Hyperliquid wallet holds your funds. The API wallet is a separate key used only for placing orders — it has no withdrawal rights.

1. Go to [app.hyperliquid.xyz/API](https://app.hyperliquid.xyz/API)
2. Click **Generate** to create an API wallet
3. Copy the **private key** shown (starts with `0x`) → this is `HL_API_PRIVATE_KEY`
4. Your main wallet address (the one you log in with) → this is `HL_ACCOUNT_ADDRESS`

> **Start on testnet.** Set `HL_USE_TESTNET=true` and fund a testnet account at [app.hyperliquid.xyz/testnet](https://app.hyperliquid.xyz/testnet) with test USDC. Switch to `false` only after you've verified everything works.

### 2. BadgerBot API Key

Log in to your BadgerBot dashboard and copy your API key → this is `BADGERBOT_API_KEY`.

### 3. Telegram Bot + User ID

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`, pick a name and username
3. BotFather gives you a token like `110201543:AAHdqTcvCH1vGWJxfSeofSs4tDXtoAg` → this is `TELEGRAM_BOT_TOKEN`
4. To find your numeric user ID, message [@userinfobot](https://t.me/userinfobot) → the number shown is `TELEGRAM_AUTHORIZED_USER_ID`

---

## Path A: Render (Cloud)

Deploy the bot on Render — no server setup, managed entirely from your browser.

**Cost:** ~$7/month (Background Worker) + ~$1/month (Persistent Disk for trade history)

### Step 1: Fork the Repository

1. Log in to [GitHub](https://github.com)
2. Open the repository page and click **Fork → Create fork**

### Step 2: Create a Render Account

1. Go to [render.com](https://render.com) and sign up
2. In **Account Settings → Git**, connect your GitHub account

### Step 3: Create a Background Worker

1. In the Render dashboard, click **New → Background Worker**
2. Select your forked repository and click **Connect**
3. Fill in the service settings:
   - **Name:** `badgerbot-hyper`
   - **Branch:** `main`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python main.py`
   - **Instance Type:** Starter ($7/month)
4. Click **Advanced** and then **Add Disk**:
   - **Name:** `data`
   - **Mount Path:** `/data`
   - **Size:** 1 GB
5. Click **Create Background Worker**

The first deploy will fail — that's expected until you add your credentials in the next step.

### Step 4: Add Environment Variables

In your Render service, go to **Environment** and add each variable:

| Variable | Value |
|---|---|
| `HYPERBOT_DB_PATH` | `/data/hyperbot.db` |
| `HL_ACCOUNT_ADDRESS` | Your main wallet address |
| `HL_API_PRIVATE_KEY` | Your API wallet private key |
| `HL_USE_TESTNET` | `false` |
| `BADGERBOT_API_KEY` | Your BadgerBot API key |
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token |
| `TELEGRAM_AUTHORIZED_USER_ID` | Your numeric Telegram user ID |
| `POSITION_SIZE_PCT` | `0.10` |

### Step 5: Deploy

1. Click **Manual Deploy → Deploy latest commit**
2. Open the deploy log and wait for: `Starting signal consumer, position monitor, and Telegram bot...`
3. Send `/status` to your Telegram bot — it should respond with your account balance

Your bot is now live. It restarts automatically on crash and on new code deploys.

---

## Path B: VPS / Ubuntu Server

Run the bot on any Ubuntu 22.04+ machine — a VPS, a home server, or your local machine.

### Step 1: Get a Server (skip for local)

Any Ubuntu 22.04+ VPS works. A few options:

| Provider | Plan | Cost |
|---|---|---|
| [Hetzner](https://hetzner.com) | CX11 | ~€4/month |
| [DigitalOcean](https://digitalocean.com) | Basic Droplet | ~$6/month |
| [Contabo](https://contabo.com) | VPS S | ~€5/month |

SSH into your server:
```bash
ssh root@your-server-ip
```

### Step 2: Clone and Install

Install Python 3.12, clone the repo, and run the setup script:

```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv git

git clone https://github.com/YOUR_USERNAME/badgerbot-hyper.git
cd badgerbot-hyper

bash setup.sh
```

`setup.sh` creates a `.venv`, installs all four dependencies, and copies `.env.example` to `.env`.

### Step 3: Configure .env

```bash
nano .env
```

Fill in your credentials:

```env
HL_ACCOUNT_ADDRESS=0xYourMainWalletAddress
HL_API_PRIVATE_KEY=0xYourApiPrivateKey
HL_USE_TESTNET=false
BADGERBOT_API_KEY=your-badgerbot-api-key
TELEGRAM_BOT_TOKEN=110201543:AAHdqTcvCH1vGWJxfSeofSs4tDXtoAg
TELEGRAM_AUTHORIZED_USER_ID=123456789
POSITION_SIZE_PCT=0.10
```

Save with `Ctrl+O`, exit with `Ctrl+X`.

### Step 4: Test a Trade

Verify the bot can place and protect orders before leaving it running unattended:

```bash
.venv/bin/python simulate_signals.py
```

This opens one ETH LONG and one ETH SHORT at minimum size (~$11 notional each, ~$22 total). You'll receive a Telegram notification for each. Once confirmed, close them immediately:

```
/close all
```

```bash
# Test only one direction if needed:
.venv/bin/python simulate_signals.py --mode long
.venv/bin/python simulate_signals.py --mode short
```

> **This places real orders on your mainnet account.** Make sure your account has at least $25 USDC before running.

### Step 5: Start the Bot

```bash
.venv/bin/python main.py
```

### Step 6: Keep It Running

#### Option A: systemd (recommended)

Auto-restarts on crash and survives server reboots:

```bash
sudo tee /etc/systemd/system/badgerbot.service > /dev/null <<EOF
[Unit]
Description=BadgerBot Hyper
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/.venv/bin/python main.py
Restart=always
RestartSec=10
TimeoutStopSec=180

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable badgerbot
sudo systemctl start badgerbot
```

```bash
sudo systemctl status badgerbot     # check if running
sudo journalctl -u badgerbot -f     # live log stream
sudo systemctl restart badgerbot    # restart
sudo systemctl stop badgerbot       # stop
```

#### Option B: tmux (simpler)

Keeps the bot running after you disconnect, but does not survive reboots:

```bash
tmux new -s badgerbot
.venv/bin/python main.py
# Ctrl+B, D  — detach (bot keeps running)
# tmux attach -t badgerbot — reattach
# Ctrl+C  — stop the bot
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `HL_ACCOUNT_ADDRESS` | Yes | — | Main wallet address (`0x...`) |
| `HL_API_PRIVATE_KEY` | Yes | — | API wallet private key — no withdrawal access |
| `HL_USE_TESTNET` | No | `false` | `true` for testnet, `false` for mainnet |
| `BADGERBOT_API_KEY` | Yes | — | Signal stream key from BadgerBot dashboard |
| `TELEGRAM_BOT_TOKEN` | Yes | — | Token from @BotFather |
| `TELEGRAM_AUTHORIZED_USER_ID` | Yes | — | Your numeric Telegram user ID |
| `POSITION_SIZE_PCT` | No | `0.10` | Trade notional as fraction of equity |
| `POSITION_SIZE_USD` | No | — | Fixed margin per trade in USD — overrides PCT |
| `RISK_PCT` | No | — | Max loss per trade at SL as fraction of equity — overrides both above |
| `MAX_SIGNAL_AGE_SECONDS` | No | `60` | Drop signals older than this |
| `MAX_PRICE_DEVIATION_PCT` | No | `0.01` | Drop signal if mark price moved more than 1% |
| `POSITION_POLL_INTERVAL_SECONDS` | No | `15` | How often to check for filled TP/SL orders |
| `HYPERBOT_DB_PATH` | No | `./hyperbot.db` | Path to the trade database (set to `/data/hyperbot.db` on Render) |

---

## Position Sizing

Three modes, in priority order. Set only one at a time.

### Risk-Based (recommended)

Size each trade so a stop-loss hit costs exactly X% of your portfolio:

```env
RISK_PCT=0.01
```

With $1,000 equity and `RISK_PCT=0.01`, every SL hit costs at most $10 regardless of TP/SL distance. When multiple signals arrive at the same price within 3 seconds, the risk budget is split evenly.

### Fixed USD Margin

Same dollar margin every trade:

```env
POSITION_SIZE_USD=20
```

$20 margin at 10x leverage = $200 notional.

### Percentage of Equity (default)

Notional as a fraction of total equity:

```env
POSITION_SIZE_PCT=0.10
```

With $500 equity, each trade opens a $50 notional position. To disable a mode, comment it out or remove the line — do not set it to `false`.

---

## Telegram Commands

| Command | Description |
|---|---|
| `/status` | Open positions (size, entry, leverage, uPnL) or available balance if flat |
| `/position` | Individual trade records with TP/SL prices, funding, and liquidation price |
| `/pause` | Stop processing incoming signals (open positions are unaffected) |
| `/resume` | Resume signal processing |
| `/history` | Last 10 closed trades with net PnL after fees |
| `/close <N\|all>` | Close trade number N or all open positions |
| `/stats` | Performance dashboard: win rate, avg win/loss, best/worst trade, avg hold time |
| `/stats week` | Same, filtered to last 7 days |
| `/stats month` | Same, filtered to last 30 days |
| `/signal` | Recent signal log: filled, rejected, and errored entries |
| `/help` | List all commands |

---

## Leverage Configuration

Edit `config/coin_leverage.json` to set leverage per coin:

```json
{
  "BTC": 10,
  "ETH": 10,
  "SOL": 10,
  "BNB": 3,
  "AVAX": 3,
  "DEFAULT": 3
}
```

Any coin not listed uses the `DEFAULT` value. Restart the bot after making changes.

---

## Changelog

**v0.3 — March 30–31, 2026**

- Net PnL tracking: trading fees (entry + close) stored per trade. All PnL values in `/stats`, `/history`, `/close`, and close notifications show net PnL after fees.
- Existing TP/SL trades backfilled with estimated 0.025% taker fee. Entry fees captured automatically on new trades.
- Close fee captured from HL fill data on both auto-close (TP/SL) and manual close paths.
- Fixed fill-to-trade matching: now matches by fill size before TP/SL price proximity, preventing cross-assigned PnL when multiple trades close simultaneously.

**v0.2.3 — March 30, 2026**

- Sweep-cancel orphaned trigger orders after closing a residual position. Prevents stale SL orders from flipping the position.

**v0.2.2 — March 28, 2026**

- Auto-close residual positions: when all TP/SL orders fill but a small remainder exists due to size rounding, PositionMonitor closes it automatically.

**v0.2.1 — March 28, 2026**

- Auto-cancel counterpart TP/SL orders when one side fills.
- Exponential backoff on PositionMonitor fill retries.
- GitHub Actions CI: syntax and import checks on every push.

**v0.2 — March 2026**

- Risk-based position sizing (`RISK_PCT`) with 3-second signal batching.
- Fixed margin mode (`POSITION_SIZE_USD`).
- $10 minimum notional bump.
- PnL percentages based on account equity.
- Telegram command menu with autocomplete.

# HyperBot

Signal-driven perpetual trading bot for Hyperliquid with Telegram interface.

## Requirements

- Python 3.12+
- Hyperliquid API wallet (generate at https://app.hyperliquid.xyz/API)
- Badgerbot signal API key
- Telegram bot token (from @BotFather)

## Local Setup

```bash
cd hyperbot

python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# Edit .env with your credentials
```

## VPS Setup (Ubuntu/Debian)

```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv git

git clone https://github.com/sergisimi/hyperbot.git
cd hyperbot/hyperbot

python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env
```

## Configuration (.env)

| Variable | Description |
|---|---|
| `HL_ACCOUNT_ADDRESS` | Main wallet public address (0x...) |
| `HL_API_PRIVATE_KEY` | API wallet private key (NOT main wallet) |
| `HL_USE_TESTNET` | `true` for testnet, `false` for mainnet |
| `BADGERBOT_API_KEY` | Signal stream API key |
| `POSITION_SIZE_PCT` | Position size as multiple of equity (see sizing guide below) |
| `POSITION_SIZE_USD` | Fixed margin per trade in USD (overrides PCT if set) |
| `RISK_PCT` | Risk-based sizing: max portfolio loss at SL (e.g. 0.01 = 1%). Overrides PCT and USD |
| `MAX_SIGNAL_AGE_SECONDS` | Drop signals older than this (default: 60) |
| `MAX_PRICE_DEVIATION_PCT` | Drop signal if price moved more than this (default: 0.01) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from @BotFather |
| `TELEGRAM_AUTHORIZED_USER_ID` | Your numeric Telegram user ID |
| `POSITION_POLL_INTERVAL_SECONDS` | How often to check positions (default: 15) |

## Position Sizing Guide

Each trade's position size (notional) is calculated as:

```
notional = equity * POSITION_SIZE_PCT
margin_used = notional / leverage
```

Hyperliquid enforces a **$10 minimum notional** per order. Orders below this are rejected.

`POSITION_SIZE_PCT` is not capped at 1.0 — values above 1.0 use leverage to open positions larger than your equity.

Example with $26 equity and 10x leverage:

| POSITION_SIZE_PCT | Notional | Margin used | % of account |
|---|---|---|---|
| 0.50 | $13 | $1.30 | 5% |
| 1.00 | $26 | $2.60 | 10% |
| 2.00 | $52 | $5.20 | 20% |
| 5.00 | $130 | $13.00 | 50% |
| 10.00 | $260 | $26.00 | 100% |

Set it based on how much margin you want to risk per trade. For small accounts, use at least `0.50` to stay above the $10 minimum.

### Fixed Margin Mode

To use a fixed dollar amount of margin per trade instead of a percentage, set `POSITION_SIZE_USD`:

```
POSITION_SIZE_USD=10
```

This uses $10 margin per trade regardless of account size. The notional is calculated as `margin * leverage`, so $10 margin at 10x leverage = $100 notional. If both are set, `POSITION_SIZE_USD` takes priority over `POSITION_SIZE_PCT`.

### Risk-Based Sizing

To size each trade so that a stop-loss hit equals exactly X% of your portfolio:

```
RISK_PCT=0.01
```

This calculates position size as:

```
size = (equity * RISK_PCT) / abs(entry_price - sl_price)
```

With `RISK_PCT=0.01` and $1000 equity, each trade risks $10 max if SL is hit. The actual position size varies per trade based on the SL distance.

When multiple signals arrive at the same entry price within 3 seconds, the risk budget is split evenly among them. For example, 3 signals at the same price each get 0.33% risk instead of 1%.

Sizing priority: `RISK_PCT` > `POSITION_SIZE_USD` > `POSITION_SIZE_PCT`.

To switch between sizing modes, comment out or remove the line you don't want. For example, to disable risk-based sizing and fall back to USD or PCT:

```
# RISK_PCT=0.01
```

Setting `RISK_PCT=` (empty value) also disables it. Do not set it to `false` or other non-numeric values.

## Run

```bash
.venv/bin/python main.py
```

## Run as systemd Service

```bash
sudo tee /etc/systemd/system/hyperbot.service > /dev/null <<EOF
[Unit]
Description=HyperBot Trading Bot
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/.venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable hyperbot
sudo systemctl start hyperbot
```

```bash
sudo systemctl status hyperbot      # check status
sudo journalctl -u hyperbot -f      # live logs
sudo systemctl restart hyperbot     # restart
sudo systemctl stop hyperbot        # stop
```

## Run with tmux (alternative)

```bash
tmux new -s hyperbot
.venv/bin/python main.py
# Ctrl+B, D to detach
# tmux attach -t hyperbot to reconnect
# Ctrl+C to stop
```

## Telegram Commands

| Command | Description |
|---|---|
| `/status` | Open positions or available balance |
| `/position` | Individual trade records with TP/SL and funding |
| `/pause` | Stop processing new signals |
| `/resume` | Resume processing signals |
| `/history` | Last 10 closed trades |
| `/close <N\|all>` | Close a specific trade or all positions |
| `/stats` | Performance dashboard (also `/stats week`, `/stats month`) |
| `/signal` | Recent signal log (filled, rejected, errors) |
| `/help` | List all commands |

## Test Signal

```bash
.venv/bin/python simulate_signals.py
```

Opens 3 small ETH LONG positions with TP/SL on Hyperliquid to verify the full flow.

## Changelog

**v0.2.2 — March 28, 2026**

- Auto-close residual positions: when all TP/SL orders for a coin have filled but a small position remains due to size rounding across multiple trades, PositionMonitor now detects the orphaned remainder and closes it automatically via `market_close`

**v0.2.1 — March 28, 2026**

- Auto-cancel orphaned TP/SL orders: when a TP fills, the corresponding SL is now cancelled automatically (and vice versa). Previously, counterpart orders accumulated on Hyperliquid indefinitely.
- Exponential backoff on PositionMonitor fill retries: Hyperliquid API can delay reporting fill data after TP/SL triggers, previously causing hundreds of log warnings per incident. Now logs 5 entries in the first 2 minutes, then once per 30 minutes until resolved.
- GitHub Actions CI: syntax and import checks run on every push

**v0.2 — March 2026**

- Risk-based position sizing (`RISK_PCT`): each trade sized so SL hit = exactly X% portfolio loss, with 3-second signal batching to split risk across simultaneous entries
- Fixed margin mode (`POSITION_SIZE_USD`): set a flat dollar margin per trade
- $10 minimum notional bump: trades below Hyperliquid's minimum are bumped up instead of dropped
- PnL percentages now show return on account equity (not trade notional)
- Telegram command menu with autocomplete suggestions
- `<code>` formatting on all dynamic values for visual contrast
- SL close notifications use ⛔ emoji

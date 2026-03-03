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
| `POSITION_SIZE_PCT` | Position size as fraction of equity (e.g. 0.05 = 5%) |
| `MAX_SIGNAL_AGE_SECONDS` | Drop signals older than this (default: 60) |
| `MAX_PRICE_DEVIATION_PCT` | Drop signal if price moved more than this (default: 0.01) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from @BotFather |
| `TELEGRAM_AUTHORIZED_USER_ID` | Your numeric Telegram user ID |
| `POSITION_POLL_INTERVAL_SECONDS` | How often to check positions (default: 15) |

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
| `/help` | List all commands |

## Test Signal

```bash
.venv/bin/python simulate_signals.py
```

Opens 3 small ETH LONG positions with TP/SL on Hyperliquid to verify the full flow.

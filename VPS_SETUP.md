# VPS Setup

## Prerequisites

- Ubuntu/Debian VPS (1 vCPU, 512MB RAM is sufficient)
- Python 3.12+

## Install

```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv git

git clone https://github.com/sergisimi/hyperbot.git
cd hyperbot/hyperbot

python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env
```

## Run with systemd

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

## Manage

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

#!/usr/bin/env bash
set -euo pipefail

SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/badgerbot.service"
WORK_DIR="$(pwd)"
PYTHON="$WORK_DIR/.venv/bin/python"

if [ ! -f "$PYTHON" ]; then
    echo "ERROR: .venv not found. Run setup.sh first."
    exit 1
fi

mkdir -p "$SERVICE_DIR"

cat > "$SERVICE_FILE" << SERVICE
[Unit]
Description=BadgerBot Hyper
After=network.target

[Service]
WorkingDirectory=$WORK_DIR
ExecStart=$PYTHON main.py
Restart=always
RestartSec=10
TimeoutStopSec=180

[Install]
WantedBy=default.target
SERVICE

echo "Service file written to $SERVICE_FILE"

# systemctl --user requires the session bus, which SSH logins may not start.
# Exporting XDG_RUNTIME_DIR points it to the correct runtime directory.
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

if [ ! -d "$XDG_RUNTIME_DIR" ]; then
    echo ""
    echo "NOTE: $XDG_RUNTIME_DIR does not exist."
    echo "Enable linger first so the runtime directory is created on boot:"
    echo "  sudo loginctl enable-linger \$USER"
    echo "Then re-run this script."
    exit 1
fi

systemctl --user daemon-reload
systemctl --user enable badgerbot
systemctl --user start badgerbot

echo ""
echo "Bot is running as a user service."
echo ""
echo "  systemctl --user status badgerbot     # check status"
echo "  journalctl --user -u badgerbot -f     # live logs"
echo "  systemctl --user restart badgerbot    # restart"
echo "  systemctl --user stop badgerbot       # stop"
echo ""
echo "To keep it running after logout/reboot (requires sudo once):"
echo "  sudo loginctl enable-linger \$USER"

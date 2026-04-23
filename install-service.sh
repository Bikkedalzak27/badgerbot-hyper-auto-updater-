#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${1:-badgerbot}"

WORK_DIR="$(pwd)"
PYTHON="$WORK_DIR/.venv/bin/python"

if [ ! -f "$PYTHON" ]; then
    echo "ERROR: .venv not found. Run setup.sh first."
    exit 1
fi

if [ "$(id -u)" -eq 0 ]; then
    # --- Root: install as a system service ---
    SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

    cat > "$SERVICE_FILE" << SERVICE
[Unit]
Description=BadgerBot Hyper (${SERVICE_NAME})
After=network.target

[Service]
WorkingDirectory=$WORK_DIR
ExecStart=$PYTHON main.py
Restart=always
RestartSec=10
TimeoutStopSec=180

[Install]
WantedBy=multi-user.target
SERVICE

    echo "Service file written to $SERVICE_FILE"

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    systemctl start "$SERVICE_NAME"

    echo ""
    echo "Bot is running as a system service ($SERVICE_NAME). Check the readme to check the status and logs."

else
    # --- Non-root: install as a user service ---
    SERVICE_DIR="$HOME/.config/systemd/user"
    SERVICE_FILE="$SERVICE_DIR/${SERVICE_NAME}.service"

    mkdir -p "$SERVICE_DIR"

    cat > "$SERVICE_FILE" << SERVICE
[Unit]
Description=BadgerBot Hyper (${SERVICE_NAME})
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
    systemctl --user enable "$SERVICE_NAME"
    systemctl --user start "$SERVICE_NAME"

    echo ""
    echo "Bot is running as a user service ($SERVICE_NAME). Check the readme to check the status and logs."
fi

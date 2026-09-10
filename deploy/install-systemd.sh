#!/usr/bin/env bash
# One-shot installer: writes and enables the systemd service so the web app
# runs forever — starts on boot, restarts automatically if it crashes — without
# needing a terminal/SSH session left open.
#
# Run once on the VPS, with sudo, from inside the repo:
#   sudo bash deploy/install-systemd.sh [user-to-run-as]
#
# [user-to-run-as] defaults to whoever you sudo'd from, or root if run directly
# as root. Re-running this is safe — it just rewrites the unit and restarts it.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: run this with sudo (it writes to /etc/systemd/system)." >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." &>/dev/null && pwd)"
RUN_AS="${1:-${SUDO_USER:-root}}"
SERVICE_NAME="pocket-option-webapp"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

if [ ! -f "$SCRIPT_DIR/start_webapp_hosted.sh" ]; then
    echo "Error: couldn't find start_webapp_hosted.sh next to deploy/ — run this from inside the repo." >&2
    exit 1
fi
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "Error: $SCRIPT_DIR/.env not found — copy .env.example to .env and fill it in first." >&2
    exit 1
fi

# Read the real port from start_webapp_hosted.sh rather than assuming 8081 - it's 443
# for a direct-TLS setup (see deploy/setup.sh), 8081 behind a reverse proxy.
HOSTED_PORT="$(grep -oP '^PORT=\K[0-9]+' "$SCRIPT_DIR/start_webapp_hosted.sh" 2>/dev/null || echo 8081)"
port_in_use="$(ss -ltnH "sport = :${HOSTED_PORT}" 2>/dev/null | head -1 || true)"
if [ -n "$port_in_use" ]; then
    echo "Warning: something is already listening on port ${HOSTED_PORT} (likely a manually-" >&2
    echo "  started instance). Stop it first — e.g. pkill -f 'python examples/webapp.py' — or" >&2
    echo "  this service will fail to bind the port." >&2
fi

echo "Installing ${UNIT_PATH}"
echo "  WorkingDirectory=$SCRIPT_DIR"
echo "  User=$RUN_AS"

cat > "$UNIT_PATH" <<EOF
[Unit]
Description=PocketOption live prices web app
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_AS}
WorkingDirectory=${SCRIPT_DIR}
EnvironmentFile=${SCRIPT_DIR}/.env
ExecStart=/bin/bash ${SCRIPT_DIR}/start_webapp_hosted.sh
Restart=always
RestartSec=5

# Lets the app bind port 443 directly without running as root - see
# pocket-option-webapp.service.example for details. Unused/harmless if it ends up
# binding a non-privileged port instead.
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

# Hard, kernel-enforced resource ceiling - sized for a 1GB RAM / 2 vCPU VPS shared
# with other projects. See pocket-option-webapp.service.example for the reasoning
# behind these specific numbers.
MemoryHigh=300M
MemoryMax=400M
CPUQuota=100%
TasksMax=256

[Install]
WantedBy=multi-user.target
EOF

# So that if you later switch start_webapp_hosted.sh to Option A (in-process
# Let's Encrypt), certbot's renewal deploy-hook restarts this same service.
sed -i "s/^SYSTEMD_SERVICE_NAME=.*/SYSTEMD_SERVICE_NAME=\"${SERVICE_NAME}\"/" "$SCRIPT_DIR/start_webapp_hosted.sh"

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

echo
echo "Done — the app now runs as a systemd service and will survive crashes and reboots."
echo "  Status: systemctl status $SERVICE_NAME"
echo "  Logs:   journalctl -u $SERVICE_NAME -f"

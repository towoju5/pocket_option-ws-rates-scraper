#!/usr/bin/env bash
# One-shot installer: sets up a second, dedicated Redis instance for this app's tick
# history (WEBAPP_CANDLE_STORAGE=redis) - its own port (6380 by default), data
# directory, and memory cap, kept completely separate from any Redis already running
# on the box for other projects.
#
# Run once on the VPS, with sudo, from inside the repo:
#   sudo bash deploy/install-redis.sh [port]
#
# [port] defaults to 6380. Pick a different one if that's already taken.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: run this with sudo (it writes to /etc/systemd/system and /etc/redis)." >&2
    exit 1
fi

if ! command -v redis-server &>/dev/null; then
    echo "Error: redis-server not found. Install it first (e.g. 'sudo apt install redis-server')" >&2
    echo "  - the package also provides the 'redis' system user this script relies on." >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PORT="${1:-6380}"
SERVICE_NAME="redis-pocket-option"
CONF_PATH="/etc/redis/${SERVICE_NAME}.conf"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
DATA_DIR="/var/lib/${SERVICE_NAME}"

port_in_use="$(ss -ltnH "sport = :${PORT}" 2>/dev/null | head -1 || true)"
if [ -n "$port_in_use" ]; then
    echo "Error: something is already listening on port ${PORT}." >&2
    echo "  Pick a different port: sudo bash deploy/install-redis.sh <port>" >&2
    exit 1
fi

if ! id redis &>/dev/null; then
    echo "Error: system user 'redis' not found - expected to exist from the redis-server package install." >&2
    exit 1
fi

echo "Installing dedicated Redis instance on port ${PORT}"
echo "  Config: ${CONF_PATH}"
echo "  Data:   ${DATA_DIR}"

mkdir -p "$DATA_DIR"
chown redis:redis "$DATA_DIR"
chmod 750 "$DATA_DIR"
mkdir -p /var/log/redis
chown redis:redis /var/log/redis

sed "s/^port 6380\$/port ${PORT}/" "$SCRIPT_DIR/redis-pocket-option.conf.example" > "$CONF_PATH"
chown redis:redis "$CONF_PATH"
chmod 640 "$CONF_PATH"

sed "s/-p 6380/-p ${PORT}/" "$SCRIPT_DIR/redis-pocket-option.service.example" > "$UNIT_PATH"

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

echo
echo "Done. Set this in your .env to use it:"
echo "  WEBAPP_CANDLE_STORAGE=redis"
echo "  REDIS_URL=redis://localhost:${PORT}/0"
echo
echo "  Status: systemctl status $SERVICE_NAME"
echo "  Logs:   journalctl -u $SERVICE_NAME -f"
echo "  Ping:   redis-cli -p ${PORT} ping"

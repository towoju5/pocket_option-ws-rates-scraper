#!/usr/bin/env bash
# One-shot production setup for datafeedcl.xyz: adds an nginx site for it (nginx
# already runs on this VPS for other projects, so this app reverse-proxies through it
# rather than binding 80/443 itself), obtains a Let's Encrypt certificate via certbot's
# nginx plugin, installs a dedicated Redis instance, and installs+starts the app's
# systemd service - from a fresh checkout to "running forever at https://datafeedcl.xyz"
# in one command.
#
# Prerequisites this script can't do for you:
#   - datafeedcl.xyz's DNS must already point at this server's public IP.
#   - nginx must already be installed and running (it already is, for your other
#     projects) - this script adds a new site for datafeedcl.xyz, it doesn't touch
#     your existing ones.
#   - .env must exist with real PO_SESSION/PO_UID values (copy .env.example if not -
#     this script will tell you and stop if it's missing).
#
# Run once, with sudo, from inside the repo:
#   sudo bash deploy/setup.sh [deploy-user]
#
# [deploy-user] defaults to whoever you sudo'd from, or root if run directly as root.
# Re-running this is safe - each step skips work that's already done.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: run this with sudo." >&2
    exit 1
fi

if ! command -v nginx &>/dev/null; then
    echo "Error: nginx not found, but this script assumes it's already installed and" >&2
    echo "  running for your other projects. Install it first, or if you actually want" >&2
    echo "  this app to own port 443 directly instead (no nginx), that's a different" >&2
    echo "  setup - see the 'Alternative' section in deploy/README.md." >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." &>/dev/null && pwd)"
DEPLOY_USER="${1:-${SUDO_USER:-root}}"
DOMAIN="datafeedcl.xyz"
LE_EMAIL="tbash7676@gmail.com"
APP_PORT="$(grep -oP '^PORT=\K[0-9]+' "$SCRIPT_DIR/start_webapp_hosted.sh" 2>/dev/null || echo 3100)"
NGINX_SITE="/etc/nginx/sites-available/${DOMAIN}"

cd "$SCRIPT_DIR"

echo "=== [1/5] System packages ==="
NEED_INSTALL=()
command -v certbot &>/dev/null || NEED_INSTALL+=(certbot python3-certbot-nginx)
command -v redis-server &>/dev/null || NEED_INSTALL+=(redis-server)
if [ "${#NEED_INSTALL[@]}" -gt 0 ]; then
    if command -v apt-get &>/dev/null; then
        apt-get update -y
        apt-get install -y "${NEED_INSTALL[@]}"
    else
        echo "Error: apt-get not found - install these manually for your distro, then" >&2
        echo "  re-run this script: ${NEED_INSTALL[*]}" >&2
        exit 1
    fi
else
    echo "certbot (+ nginx plugin) and redis-server already installed, skipping."
fi

echo
echo "=== [2/5] .env ==="
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    if [ ! -f "$SCRIPT_DIR/.env.example" ]; then
        echo "Error: neither .env nor .env.example found in $SCRIPT_DIR." >&2
        exit 1
    fi
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    chown "$DEPLOY_USER" "$SCRIPT_DIR/.env"
    echo "Created .env from .env.example."
    echo "Fill in PO_SESSION and PO_UID (see README.md 'Getting Session ID and UID'),"
    echo "then re-run: sudo bash deploy/setup.sh ${DEPLOY_USER}"
    exit 0
fi
if ! grep -qE "^PO_SESSION=.+" "$SCRIPT_DIR/.env" || ! grep -qE "^PO_UID=.+" "$SCRIPT_DIR/.env"; then
    echo "Error: .env exists but PO_SESSION/PO_UID aren't filled in. Fill those in, then" >&2
    echo "  re-run this script." >&2
    exit 1
fi
echo ".env looks present and configured."

echo
echo "=== [3/5] Dedicated Redis instance (port 6380) ==="
bash "$SCRIPT_DIR/deploy/install-redis.sh"
if ! grep -q "^WEBAPP_CANDLE_STORAGE=" "$SCRIPT_DIR/.env"; then
    echo "WEBAPP_CANDLE_STORAGE=redis" >> "$SCRIPT_DIR/.env"
fi
if ! grep -q "^REDIS_URL=" "$SCRIPT_DIR/.env"; then
    echo "REDIS_URL=redis://localhost:6380/0" >> "$SCRIPT_DIR/.env"
fi

echo
echo "=== [4/5] nginx site + Let's Encrypt certificate for ${DOMAIN} ==="
DOMAIN_RE="${DOMAIN//./\\.}"
if grep -rq "server_name.*\b${DOMAIN_RE}\b" /etc/nginx/sites-enabled/ 2>/dev/null; then
    echo "An nginx server block for ${DOMAIN} already exists, leaving it as-is."
    echo "(If it doesn't proxy to 127.0.0.1:${APP_PORT}, edit it by hand - see"
    echo " deploy/nginx-datafeedcl.conf.example.)"
else
    echo "Writing ${NGINX_SITE} (proxies to 127.0.0.1:${APP_PORT})..."
    sed "s/127\.0\.0\.1:3100/127.0.0.1:${APP_PORT}/" \
        "$SCRIPT_DIR/deploy/nginx-datafeedcl.conf.example" \
        | grep -v '^#' | sed '/^$/N;/^\n$/D' > "$NGINX_SITE"
    ln -sf "$NGINX_SITE" "/etc/nginx/sites-enabled/${DOMAIN}"
    nginx -t
    systemctl reload nginx
    echo "nginx site added and reloaded (HTTP only for now - certbot adds HTTPS next)."
fi

if [ -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]; then
    echo "Certificate for ${DOMAIN} already exists, skipping issuance."
else
    echo "Requesting a certificate via certbot's nginx plugin (needs ${DOMAIN}'s DNS"
    echo "already pointing here)..."
    certbot --nginx -d "$DOMAIN" -m "$LE_EMAIL" --agree-tos --redirect --non-interactive
fi

echo
echo "=== [5/5] systemd service ==="
bash "$SCRIPT_DIR/deploy/install-systemd.sh" "$DEPLOY_USER"

echo
echo "Done. https://${DOMAIN} should be live within a few seconds."
echo "  App status:   systemctl status pocket-option-webapp redis-pocket-option"
echo "  App logs:     journalctl -u pocket-option-webapp -f"
echo "  nginx test:   sudo nginx -t"
echo "  Cert renewal: certbot's own systemd timer/cron handles this automatically -"
echo "                check with 'systemctl list-timers | grep certbot'."

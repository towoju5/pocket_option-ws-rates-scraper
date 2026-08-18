#!/usr/bin/env bash
# Production launcher for a hosted platform (VPS, cloud box, etc.): binds on all
# interfaces and exposes the WebSocket as wss://, either by auto-obtaining a free
# Let's Encrypt cert, pointing at one you already have, or behind a reverse proxy.
set -euo pipefail

# --- Edit these -------------------------------------------------------------
BASE_URL="https://your-domain.example.com"   # public URL this will be reachable at
PORT=8081                                     # port this process binds to

# --- TLS: pick at most ONE of the two options below ---------------------------
# Option A: auto-obtain (and, on renewal, auto-reload) a free cert from Let's
# Encrypt for the domain in BASE_URL. Requires: BASE_URL's DNS already points at
# this host, port 80 is free, certbot is installed, and root/sudo. Set an email
# to enable this — Let's Encrypt sends expiry notices to it, and issuing a cert
# means agreeing to their Subscriber Agreement (--agree-tos, applied below).
LETSENCRYPT_EMAIL="towojuads@gmail.com"

# Option B: point at a cert you already have (Let's Encrypt or otherwise).
# Leave both blank if using Option A, or if a reverse proxy in front handles TLS.
SSL_CERT_PATH=""
SSL_KEY_PATH=""

# Only used with Option A: if this app runs as a systemd service, name it here so
# certbot restarts it after future renewals (this process only reads the cert at
# startup, so a renewed cert needs a restart to actually take effect).
SYSTEMD_SERVICE_NAME=""
# -----------------------------------------------------------------------------

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
cd "$SCRIPT_DIR"

# shellcheck disable=SC1091
source "$SCRIPT_DIR/scripts/common.sh"

setup_venv "$SCRIPT_DIR"
load_env_file "$SCRIPT_DIR/.env"
require_session_env

if [ -z "${WEBAPP_ADMIN_PASSWORD:-}" ]; then
    echo "Warning: WEBAPP_ADMIN_PASSWORD is not set — the /admin whitelist panel will be disabled." >&2
fi
if [ -z "${WEBAPP_ALLOWED_CLIENTS:-}" ] && [ "${WEBAPP_TRUST_PROXY:-0}" != "1" ]; then
    echo "Warning: no WEBAPP_ALLOWED_CLIENTS set — the app will be reachable by anyone." >&2
fi

host="${BASE_URL#*://}"
host="${host%%/*}"

obtain_letsencrypt_cert() {
    local domain="$1" email="$2"

    if [ "$domain" = "your-domain.example.com" ] || [ "$domain" = "localhost" ] || [[ "$domain" =~ ^[0-9.]+$ ]] || [[ "$domain" =~ : ]]; then
        echo "Error: set BASE_URL to your real public domain before using Let's Encrypt (got '$domain')." >&2
        exit 1
    fi
    if ! command -v certbot &>/dev/null; then
        echo "Error: certbot is not installed. Install it, then re-run this script:" >&2
        echo "  Debian/Ubuntu: sudo apt-get install certbot" >&2
        echo "  Fedora/RHEL:   sudo dnf install certbot" >&2
        echo "  Any distro:    sudo snap install --classic certbot" >&2
        exit 1
    fi

    local live_dir="${LE_LIVE_ROOT:-/etc/letsencrypt/live}/${domain}"
    if [ -f "$live_dir/fullchain.pem" ] && [ -f "$live_dir/privkey.pem" ]; then
        echo "Using existing Let's Encrypt certificate for $domain (certbot's own timer/cron renews it; set SYSTEMD_SERVICE_NAME above so renewal reloads this app too)."
    else
        echo "Requesting a new Let's Encrypt certificate for $domain..."
        echo "This needs port 80 free right now and $domain's DNS already pointing at this host."
        local deploy_hook_args=()
        if [ -n "$SYSTEMD_SERVICE_NAME" ]; then
            deploy_hook_args=(--deploy-hook "systemctl restart $SYSTEMD_SERVICE_NAME")
        fi
        sudo certbot certonly --standalone \
            -d "$domain" \
            -m "$email" \
            --agree-tos --non-interactive \
            "${deploy_hook_args[@]}"
    fi

    SSL_CERT_PATH="$live_dir/fullchain.pem"
    SSL_KEY_PATH="$live_dir/privkey.pem"
}

if [ -n "$LETSENCRYPT_EMAIL" ] && [ -z "$SSL_CERT_PATH" ] && [ -z "$SSL_KEY_PATH" ]; then
    obtain_letsencrypt_cert "$host" "$LETSENCRYPT_EMAIL"
fi

export WEBAPP_HOST="0.0.0.0"
export WEBAPP_PORT="$PORT"
export WEBAPP_AUTO_OPEN="0"

if [ -n "$SSL_CERT_PATH" ] && [ -n "$SSL_KEY_PATH" ]; then
    export WEBAPP_SSL_CERT="$SSL_CERT_PATH"
    export WEBAPP_SSL_KEY="$SSL_KEY_PATH"
    echo "Terminating TLS in-process."
    echo "Dashboard: https://${host}:${PORT}/"
    echo "WebSocket: wss://${host}:${PORT}/ws"
else
    echo "No cert configured — expecting a reverse proxy to terminate TLS."
    echo "Set WEBAPP_TRUST_PROXY=1 in .env once that proxy is in place (see WEBAPP.md),"
    echo "so the IP allowlist sees real client IPs instead of the proxy's."
    echo "Dashboard: ${BASE_URL}/"
    echo "WebSocket: ${BASE_URL/https:/wss:}/ws"
fi

echo "Starting web app on 0.0.0.0:${PORT}..."
exec python examples/webapp.py

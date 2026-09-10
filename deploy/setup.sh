#!/usr/bin/env bash
# One-shot production setup for datafeedcl.xyz: installs a dedicated Redis instance,
# obtains a Let's Encrypt certificate, and installs+starts the systemd service - from
# a fresh checkout to "running forever at https://datafeedcl.xyz" in one command.
#
# Prerequisites this script can't do for you:
#   - datafeedcl.xyz's DNS must already point at this server's public IP.
#   - Port 80 must be free right now (certbot's standalone mode needs it briefly to
#     prove domain ownership) and port 443 free for the app itself afterwards.
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

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." &>/dev/null && pwd)"
DEPLOY_USER="${1:-${SUDO_USER:-root}}"
DOMAIN="datafeedcl.xyz"
LE_EMAIL="tbash7676@gmail.com"
CERT_GROUP="pocketoption-cert"
LIVE_DIR="/etc/letsencrypt/live/${DOMAIN}"
ARCHIVE_DIR="/etc/letsencrypt/archive/${DOMAIN}"

cd "$SCRIPT_DIR"

echo "=== [1/5] System packages ==="
if ! command -v certbot &>/dev/null || ! command -v redis-server &>/dev/null; then
    if command -v apt-get &>/dev/null; then
        apt-get update -y
        apt-get install -y certbot redis-server
    else
        echo "Error: apt-get not found - install certbot and redis-server manually for" >&2
        echo "  your distro, then re-run this script." >&2
        exit 1
    fi
else
    echo "certbot and redis-server already installed, skipping."
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
echo "=== [4/5] Let's Encrypt certificate for ${DOMAIN} ==="
port80_in_use="$(ss -ltnH "sport = :80" 2>/dev/null | head -1 || true)"
if [ -f "$LIVE_DIR/fullchain.pem" ]; then
    echo "Certificate already exists at $LIVE_DIR, skipping issuance."
elif [ -n "$port80_in_use" ]; then
    echo "Error: something is already listening on port 80 - certbot's standalone mode" >&2
    echo "  needs it free to prove domain ownership. Stop whatever that is, then re-run" >&2
    echo "  this script." >&2
    exit 1
else
    echo "Requesting a new certificate (needs ${DOMAIN}'s DNS already pointing here)..."
    certbot certonly --standalone -d "$DOMAIN" -m "$LE_EMAIL" --agree-tos --non-interactive
fi

# Let's Encrypt's privkey.pem defaults to 0600 root:root, unreadable by the app's own
# unprivileged systemd user - and every renewal regenerates it with those same
# permissions. This group grants read access now; the deploy-hook re-grants it after
# every future renewal (see certbot-deploy-hook.sh) so the app never needs to run as
# root just to read its own cert.
getent group "$CERT_GROUP" >/dev/null || groupadd "$CERT_GROUP"
usermod -aG "$CERT_GROUP" "$DEPLOY_USER"
chgrp "$CERT_GROUP" /etc/letsencrypt/live /etc/letsencrypt/archive "$LIVE_DIR" "$ARCHIVE_DIR"
chmod 750 /etc/letsencrypt/live /etc/letsencrypt/archive "$LIVE_DIR" "$ARCHIVE_DIR"
chgrp "$CERT_GROUP" "$ARCHIVE_DIR"/privkey*.pem
chmod 640 "$ARCHIVE_DIR"/privkey*.pem

mkdir -p /etc/letsencrypt/renewal-hooks/deploy
install -m 755 "$SCRIPT_DIR/deploy/certbot-deploy-hook.sh" /etc/letsencrypt/renewal-hooks/deploy/pocket-option-webapp.sh
echo "Certificate ready, permissions granted to group '${CERT_GROUP}', renewal hook installed."

echo
echo "=== [5/5] systemd service ==="
bash "$SCRIPT_DIR/deploy/install-systemd.sh" "$DEPLOY_USER"

echo
echo "Done. https://${DOMAIN} should be live within a few seconds."
echo "  Status: systemctl status pocket-option-webapp redis-pocket-option"
echo "  Logs:   journalctl -u pocket-option-webapp -f"
echo "  Note: ${DEPLOY_USER} was just added to the '${CERT_GROUP}' group - that's already"
echo "  applied to the service systemd just started, but if you personally log in as"
echo "  ${DEPLOY_USER} and want that group membership in your own shell too, you'll need"
echo "  to log out and back in."

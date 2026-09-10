#!/usr/bin/env bash
# Installed by deploy/setup.sh into /etc/letsencrypt/renewal-hooks/deploy/ - certbot
# runs everything in that directory after every successful renewal (not just the
# first issuance, which setup.sh also invokes this same logic for directly).
#
# Two things need to happen on every renewal, not just the first cert:
#   1. Let's Encrypt's privkey.pem is regenerated 0600 root:root each time - the
#      app's own systemd user can't read that, so re-grant the read group setup.sh
#      created (chgrp/chmod resets on every renewal, it doesn't stick).
#   2. The app only reads its cert at startup, so a renewed cert needs a restart to
#      actually take effect - certbot alone won't do that for you.
set -euo pipefail

DOMAIN="datafeedcl.xyz"
CERT_GROUP="pocketoption-cert"
SERVICE_NAME="pocket-option-webapp"

chgrp "$CERT_GROUP" "/etc/letsencrypt/archive/$DOMAIN"/privkey*.pem
chmod 640 "/etc/letsencrypt/archive/$DOMAIN"/privkey*.pem

systemctl restart "$SERVICE_NAME"

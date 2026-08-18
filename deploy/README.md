# Deploying to the VPS behind datafeedcl.xyz

`datafeedcl.xyz` already resolves to a separate VPS (not this machine) and already
serves HTTPS there via an existing reverse proxy — so this app doesn't need to (and
shouldn't try to) obtain its own certificate. It runs plain HTTP on `127.0.0.1:8081`
and that existing proxy forwards to it. `start_webapp_hosted.sh` and `.env` in this
repo are already configured for that setup.

## Steps, on the VPS

1. Get this repo onto the VPS (`git clone`/`git pull`, or `rsync`), and put a
   filled-in `.env` there (copy from `.env.example`, same values as your local one
   — at minimum `PO_SESSION`/`PO_UID`).
2. Add the `location` block from [nginx-datafeedcl.conf.example](nginx-datafeedcl.conf.example)
   to datafeedcl.xyz's existing nginx server block, then `sudo nginx -t && sudo
   systemctl reload nginx`.
3. Install the systemd service from [pocket-option-webapp.service.example](pocket-option-webapp.service.example)
   (fill in the two placeholders first) so the app starts on boot and restarts
   automatically if it ever crashes:
   ```bash
   sudo cp deploy/pocket-option-webapp.service.example /etc/systemd/system/pocket-option-webapp.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now pocket-option-webapp
   ```
4. Verify: `https://datafeedcl.xyz/` should load the dashboard, and prices should
   start streaming (confirms the `/ws` WebSocket upgrade is working through nginx).

## Why it's set up this way

- `start_webapp_hosted.sh`'s `LETSENCRYPT_EMAIL` is left blank and `SSL_CERT_PATH`/
  `SSL_KEY_PATH` are left blank too, so the script takes its "reverse proxy in
  front" path — it binds `127.0.0.1` only, never touches port 80/certbot, and
  can't conflict with whatever's already serving TLS on that VPS.
- `.env` sets `WEBAPP_TRUST_PROXY=1` so the IP allowlist and `/admin`'s "use my
  current IP" see real visitor IPs (from `X-Forwarded-For`) instead of nginx's.
- `.env`'s `WEBAPP_ALLOWED_CLIENTS` is blank (open access) — it previously held
  `.env.example`'s placeholder addresses, which would have 403'd every real
  visitor. Set it to a comma-separated allowlist later if you want to restrict
  who can reach the dashboard/API.
- `Restart=always` in the systemd unit is what makes it "run forever": survives
  crashes, network blips, and VPS reboots, without needing a terminal/SSH session
  to stay open.

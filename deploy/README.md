# Deploying to the VPS behind datafeedcl.xyz

nginx already runs on this VPS for other projects and owns ports 80/443, so this app
doesn't terminate TLS itself — it binds `127.0.0.1:3100` and nginx reverse-proxies
`datafeedcl.xyz` to it, obtaining its own Let's Encrypt certificate via certbot's nginx
plugin. `start_webapp_hosted.sh` and `.env` in this repo are already configured for that.

## One-command setup, on the VPS

Prerequisites `deploy/setup.sh` can't do for you:
- `datafeedcl.xyz`'s DNS must already point at this server's public IP.
- nginx must already be installed and running (it already is, for your other projects).
- `.env` must exist with real `PO_SESSION`/`PO_UID` filled in.

Steps, on the VPS:

```bash
git clone https://github.com/towoju5/pocket_option-ws-rates-scraper.git
cd pocket_option-ws-rates-scraper
cp .env.example .env       # fill in PO_SESSION/PO_UID
sudo bash deploy/setup.sh
```

(Already have it cloned there? `git pull`/`rsync` it up to date instead of cloning fresh,
`cd` into it, and run the same last two lines — `setup.sh` is safe to re-run, and each
step in it skips work that's already done, so if it stops partway — e.g. `.env` wasn't
filled in yet — just fill that in and re-run the same command.)

`sudo bash deploy/setup.sh` installs certbot (+ its nginx plugin) and a dedicated Redis
instance, adds an nginx site for `datafeedcl.xyz` (a new file in
`sites-available`/`sites-enabled` — your other projects' nginx sites are untouched),
obtains the Let's Encrypt certificate, installs the systemd service (with the resource
caps below), and starts everything.

Verify: `https://datafeedcl.xyz/` should load the dashboard, and prices should start
streaming (confirms the `/ws` WebSocket upgrade is passing through nginx).

`deploy/setup.sh` runs [install-redis.sh](install-redis.sh) and
[install-systemd.sh](install-systemd.sh) for you — see those (or
[redis-pocket-option.conf.example](redis-pocket-option.conf.example) /
[pocket-option-webapp.service.example](pocket-option-webapp.service.example) /
[nginx-datafeedcl.conf.example](nginx-datafeedcl.conf.example)) if you'd rather do any
of it by hand, or want to see exactly what gets written.

## Resource limits (shared VPS)

The systemd unit caps the app at 1.5GB RAM and 1 CPU core (`MemoryMax`/`CPUQuota` in
[pocket-option-webapp.service.example](pocket-option-webapp.service.example)), and the
dedicated Redis instance at 160MB / half a core — both kernel-enforced, so neither can
starve other projects on the same box no matter what goes wrong here. These are sized
generously against real measured usage (~110MB / ~9MB respectively) for a 24GB RAM /
4 vCPU box — the point is a real ceiling, not a tight squeeze. Tune both down if your
box is smaller than that, or up if you want even more headroom.

## Why it's set up this way

- The app binds `127.0.0.1:3100` — loopback-only, on a non-privileged, unlikely-to-
  collide port — and never touches port 80/443 or a TLS certificate at all. nginx (and
  only nginx) is internet-facing for this domain, same as it already is for your other
  projects.
- `certbot --nginx` (run by `setup.sh`) both obtains the certificate *and* edits the
  nginx site to add the HTTPS server block, redirect, and renewal hook automatically —
  nothing in this repo needs to know where the certificate files live or manage their
  permissions, unlike a setup where the app terminates TLS itself.
- `.env` sets `WEBAPP_TRUST_PROXY=1` so the IP allowlist and `/admin`'s "use my current
  IP" see real visitor IPs (from `X-Forwarded-For`) instead of nginx's. Only keep this
  "1" as long as nginx is genuinely the sole way to reach the app — port 3100 itself
  must not be directly internet-reachable, or this becomes spoofable.
- `.env`'s `WEBAPP_ALLOWED_CLIENTS` is blank (open access) — set it to a comma-
  separated allowlist later if you want to restrict who can reach the dashboard/API.
- `Restart=always` in the systemd unit is what makes it "run forever": survives
  crashes, network blips, and VPS reboots, without needing a terminal/SSH session to
  stay open.

## Alternative: this app owns port 443 directly (no nginx)

Only relevant if nginx (or anything else) is *not* already using ports 80/443 on the
target host — not the case for the current `datafeedcl.xyz` deployment, but kept here in
case you ever deploy this elsewhere without an existing reverse proxy:

1. Edit `start_webapp_hosted.sh`: set `PORT=443`, and either fill in `LETSENCRYPT_EMAIL`
   (if you'll run it directly/interactively — its `sudo certbot` call has no terminal to
   prompt on under systemd) or set `SSL_CERT_PATH`/`SSL_KEY_PATH` to a cert you obtain
   some other way (e.g. `certbot certonly --standalone`, run once by hand, up front).
2. Add `AmbientCapabilities=CAP_NET_BIND_SERVICE` / `CapabilityBoundingSet=CAP_NET_BIND_SERVICE`
   to the systemd unit so an unprivileged deploy user can still bind port 443.
3. If using a cert obtained separately, you'll also need to grant the deploy user read
   access to it (Let's Encrypt's private key defaults to `0600 root:root`, and every
   renewal resets that) — e.g. a dedicated group plus a certbot renewal deploy-hook that
   re-applies both the permissions and a service restart after each renewal.
4. Set `WEBAPP_TRUST_PROXY=0` in `.env` — there's no proxy in front to spoof.
5. Run `sudo bash deploy/install-systemd.sh` directly (skip `setup.sh` — its nginx/certbot
   steps assume the reverse-proxy path above).

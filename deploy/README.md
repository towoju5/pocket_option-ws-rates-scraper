# Deploying to the VPS behind datafeedcl.xyz

This app terminates TLS itself for `datafeedcl.xyz` (binds `0.0.0.0:443` directly,
owns its own Let's Encrypt certificate) — no reverse proxy in front. `start_webapp_hosted.sh`
and `.env` in this repo are already configured for that.

## One-command setup, on the VPS

Prerequisites `deploy/setup.sh` can't do for you:
- `datafeedcl.xyz`'s DNS must already point at this server's public IP.
- Port 80 must be free right now (certbot's standalone mode needs it briefly to prove
  domain ownership), and port 443 free for the app itself afterwards.
- `.env` must exist with real `PO_SESSION`/`PO_UID` filled in.

Steps:

1. Get this repo onto the VPS (`git clone`/`git pull`, or `rsync`).
2. Copy `.env.example` to `.env` and fill in at least `PO_SESSION`/`PO_UID`.
3. Run the setup script, with sudo, from inside the repo:
   ```bash
   sudo bash deploy/setup.sh
   ```
   This installs certbot + a dedicated Redis instance, obtains the Let's Encrypt
   certificate, installs the systemd service (with the resource caps below), and
   starts everything. It's safe to re-run — each step skips work already done, so if
   it stops partway (e.g. `.env` wasn't filled in yet) just fill that in and re-run
   the same command.
4. Verify: `https://datafeedcl.xyz/` should load the dashboard, and prices should
   start streaming.

`deploy/setup.sh` runs [install-redis.sh](install-redis.sh) and
[install-systemd.sh](install-systemd.sh) for you — see those (or
[redis-pocket-option.conf.example](redis-pocket-option.conf.example) /
[pocket-option-webapp.service.example](pocket-option-webapp.service.example)) if you'd
rather do any of it by hand, or want to see exactly what gets written.

## Resource limits (shared/resource-constrained VPS)

The systemd unit caps the app to 400MB RAM and 1 CPU core (`MemoryMax`/`CPUQuota` in
[pocket-option-webapp.service.example](pocket-option-webapp.service.example)), and the
dedicated Redis instance to 160MB / half a core — both kernel-enforced, so neither can
starve other projects on the same box no matter what goes wrong here. Tune both down
further if your box is tighter than 1GB RAM / 2 vCPU, or up if you have more room and
want less conservative limits.

## Why it's set up this way

- `start_webapp_hosted.sh` uses Option B (point at an already-obtained cert), not its
  own built-in Option A (auto-obtain via `sudo certbot` at startup) — Option A calls
  `sudo`, which has no terminal to prompt on when this runs as a systemd service under
  an unprivileged user, so it would just hang or fail on first start. `setup.sh`
  obtains the cert once, up front, as real root, then installs a certbot renewal hook
  ([certbot-deploy-hook.sh](certbot-deploy-hook.sh)) to keep it renewed automatically.
- Let's Encrypt's private key defaults to `0600 root:root`, unreadable by the app's own
  unprivileged systemd user, and every renewal resets those permissions. `setup.sh`
  creates a `pocketoption-cert` group with read access and adds the deploy user to it;
  the renewal hook re-applies that (and restarts the service, since it only reads its
  cert at startup) after every future renewal — so the app never needs to run as root
  just to read its own certificate.
- The systemd unit grants only `CAP_NET_BIND_SERVICE` (not full root) so the
  unprivileged deploy user can still bind port 443 directly.
- `.env` sets `WEBAPP_TRUST_PROXY=0` since there's no reverse proxy in front anymore —
  the app sees real client IPs directly. Only set this back to `1` if you put a proxy
  in front again (and make sure the app's own port isn't also directly reachable, or
  this becomes spoofable via a forged `X-Forwarded-For` header).
- `.env`'s `WEBAPP_ALLOWED_CLIENTS` is blank (open access) — set it to a comma-
  separated allowlist later if you want to restrict who can reach the dashboard/API.
- `Restart=always` in the systemd unit is what makes it "run forever": survives
  crashes, network blips, and VPS reboots, without needing a terminal/SSH session to
  stay open.

## Alternative: behind an existing reverse proxy

If you'd rather put nginx/Caddy in front instead (e.g. it's already terminating TLS
for other things on the same box), that path still exists:

1. Edit `start_webapp_hosted.sh`: blank out `SSL_CERT_PATH`/`SSL_KEY_PATH`, and set
   `PORT=8081` (or whatever the proxy should forward to).
2. Set `WEBAPP_TRUST_PROXY=1` in `.env` — but only once the proxy is genuinely the
   sole way to reach the app (its own port must not be directly internet-reachable),
   or the allowlist becomes spoofable.
3. Add the `location` block from [nginx-datafeedcl.conf.example](nginx-datafeedcl.conf.example)
   to the proxy's config and reload it.
4. Run `sudo bash deploy/install-systemd.sh` directly (skip `setup.sh`'s certbot step
   entirely - the proxy owns TLS, not this app).

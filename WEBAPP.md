# 🖥️ Live Assets & Prices Web App

`examples/webapp.py` is a small [aiohttp](https://docs.aiohttp.org) server that connects to
PocketOption using this SDK and exposes:

- `GET /` — a browser dashboard: pick assets from a searchable list, watch live prices.
- `GET /api/assets` — JSON snapshot of all known assets.
- `GET /ws` — a raw WebSocket you can connect to directly from your own client/script.
- `GET /admin` — a password-protected page for managing the IP allowlist at runtime.

## Running it

```bash
cp .env.example .env   # fill in PO_SESSION and PO_UID
./start_webapp.sh
```

By default it binds to `127.0.0.1:8081` and opens a browser tab automatically.

| Env var                | Default       | Purpose                                                    |
|-------------------------|---------------|--------------------------------------------------------------|
| `WEBAPP_HOST`            | `127.0.0.1`   | Interface to bind to. Use `0.0.0.0` to accept connections from other machines. |
| `WEBAPP_PORT`            | `8081`        | Port to listen on.                                            |
| `WEBAPP_AUTO_OPEN`       | `1`           | Set to `0` to skip auto-opening a browser tab (useful when running headless/over SSH). |
| `WEBAPP_ALLOWED_CLIENTS` | *(unset)*     | Comma-separated, always-allowed client list. See below. Unset (and no dynamic entries) = open access. |
| `WEBAPP_ADMIN_PASSWORD`  | *(unset)*     | Enables `/admin`. Unset = admin panel disabled (login returns 503). |
| `WEBAPP_ADMIN_SESSION_HOURS` | `12`      | How long an admin login session lasts before you must log in again. |
| `WEBAPP_ALWAYS_ON_ASSETS` | *(unset)* | Comma-separated asset symbols, or `all`, to keep streaming/buffered at all times — even with nobody connected. Unset = only streams what someone's actively watching, and stops the moment they all disconnect. See below. |
| `WEBAPP_SSL_CERT` / `WEBAPP_SSL_KEY` | *(unset)* | Paths to a cert/key PEM pair. Set both to have the app terminate TLS itself (serves `https://`/`wss://` directly, no reverse proxy needed). |
| `WEBAPP_TRUST_PROXY`     | `0`           | Set to `1` **only** when deployed behind a reverse proxy that sets `X-Forwarded-For`/`-Proto` — trusts those headers for the IP allowlist and secure-cookie detection. Never enable this without an actual proxy in front; otherwise anyone can spoof their IP and bypass the allowlist. |

## Restricting who can connect

`WEBAPP_HOST=0.0.0.0` alone would accept connections from anywhere. To only allow specific
clients, set `WEBAPP_ALLOWED_CLIENTS` to a comma-separated list mixing any of:

- **`localhost`** — matches `127.0.0.1` and `::1`.
- **An IPv4 address** — e.g. `203.0.113.5`.
- **An IPv6 address** — e.g. `2001:db8::1`.
- **A URL or hostname** — e.g. `https://bot.example.com` or `bot.example.com`. Resolved to
  its IP address(es) via DNS once at startup, then enforced the same as a literal IP.

The check is done against the actual TCP connection's source IP (`request.remote`), not a
spoofable header, and applies to every route (`/`, `/api/assets`, `/ws`) — not just the
WebSocket. Anything not on the list gets `403 Forbidden`.

```bash
# .env — allow only your own machine plus one remote server
WEBAPP_HOST=0.0.0.0
WEBAPP_ALLOWED_CLIENTS=localhost,203.0.113.5,bot.example.com
```

> If a hostname's IP changes after startup (dynamic DNS), restart the app to re-resolve it.
> For anything internet-facing, also put a firewall in front of this — the allowlist is
> app-level defense, not a substitute for one.

### Managing the allowlist at runtime, from the UI

`WEBAPP_ALLOWED_CLIENTS` entries are permanent and only load at startup. To add or remove
clients while the app is running — e.g. temporarily whitelist your current IP for a few
hours — use the admin panel at `/admin`. It's exempt from the allowlist itself (otherwise
you could never reach it to whitelist yourself), and is instead gated by a password:

```bash
# .env
WEBAPP_ADMIN_PASSWORD=some-strong-password
```

Without `WEBAPP_ADMIN_PASSWORD` set, `/admin` shows the login form but the login API refuses
every attempt (503) — so an internet-facing deployment doesn't accidentally expose whitelist
management unless you explicitly turn it on.

Once logged in (session lasts `WEBAPP_ADMIN_SESSION_HOURS`, default 12h), you can:

- Add an entry (same IPv4 / IPv6 / `localhost` / hostname forms as `WEBAPP_ALLOWED_CLIENTS`)
  with an expiry in minutes — or `0`/blank for no expiry.
- Click **"Use my current IP"** to prefill your own address (detected server-side).
- See and remove entries you've added, plus a read-only view of the static, env-configured
  list.

Login is rate-limited (5 failed attempts locks that IP out of `/admin` for 5 minutes) and
sessions are plain server-side tokens in an `HttpOnly`, `SameSite=Strict` cookie — there's no
CSRF token, since `SameSite=Strict` already blocks cross-site requests from carrying it.
Entries added via the UI live in memory only and are lost on restart (by design — restarting
the process is a reasonable way to revoke everything).

## Connecting to the WebSocket directly

The browser UI is just one client of `/ws` — you can connect to it yourself with any
WebSocket library. Protocol:

**You send** (JSON text frames):

```json
{"action": "subscribe", "asset": "EURUSD_otc"}
{"action": "unsubscribe", "asset": "EURUSD_otc"}
```

`asset` must be a valid symbol from `pocket_option.models.Asset` (see `GET /api/assets` for
the currently tradable ones).

**You receive** (JSON text frames):

```json
{"type": "asset", "asset": "EURUSD_otc", "label": "EUR/USD (OTC)", "payout": 92, "isOtc": true, "active": true, "expTime": 1755550000}
{"type": "history", "asset": "EURUSD_otc", "ticks": [{"value": "1.08420", "timestamp": 1755549961.0}, "... up to 50 entries ..."]}
{"type": "price", "asset": "EURUSD_otc", "value": "1.08423", "timestamp": 1755549991.2}
```

- `asset` messages are metadata broadcasts sent to **every** connected client (needed to
  populate an asset list), regardless of what they've subscribed to.
- `history` is sent to **you only**, once, right after you subscribe to an asset — the last
  50 ticks the server has buffered for it (fewer if it hasn't seen that many yet), oldest
  first. It arrives before any live `price` ticks for that asset.
- `price` ticks are only sent to connections currently subscribed to that specific asset —
  subscribing to `EURUSD_otc` will never deliver ticks for `GBPUSD_otc` on your connection,
  even though other clients might be watching it.

`expTime` is the Unix timestamp (seconds) the asset is scheduled to close, or `0`/`null` if
it doesn't have one (e.g. always-open OTC assets).

### Example: Python client

```python
import asyncio
import json
import websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:8081/ws") as ws:
        await ws.send(json.dumps({"action": "subscribe", "asset": "EURUSD_otc"}))
        async for message in ws:
            print(json.loads(message))

asyncio.run(main())
```

### Example: command line (websocat)

```bash
echo '{"action":"subscribe","asset":"EURUSD_otc"}' | websocat ws://127.0.0.1:8081/ws
```

If your client's IP isn't in `WEBAPP_ALLOWED_CLIENTS`, the initial connection is rejected
with an HTTP 403 before the WebSocket handshake completes.

## Keeping assets warm without a UI client

By default, this app only subscribes to an asset upstream while at least one browser/WS
client is actively watching it — the moment the last watcher disconnects, it unsubscribes.
That's fine for casual local use, but means a client connecting to `/ws` and subscribing
to a symbol nobody's currently watching gets a cold start: no buffered `history`, and a
short delay before the first `price` tick while the upstream subscribe completes.

Set `WEBAPP_ALWAYS_ON_ASSETS=all` to keep every currently-active asset streaming and
buffered at all times, regardless of connected clients — new clients get already-buffered
`history` immediately on subscribe. Set it to a comma-separated list instead (symbols from
`GET /api/assets`) to warm only specific assets. This trades a bit of constant upstream
bandwidth/connections for always-warm data; for a small watchlist it's negligible, for
"all" it's every tradable asset, all the time.

## Deploying to a hosted platform (making wss:// available)

`./start_webapp_hosted.sh` is a production launcher: it binds on all interfaces
(`WEBAPP_HOST=0.0.0.0`), skips auto-opening a browser, and prints the URLs you'll actually
use once it's up. Edit the variables at the top of the file — `BASE_URL` (your public
domain) and `PORT` — then run it. There are three ways to get `wss://`:

**Option A — auto-obtain a free Let's Encrypt cert.** Set `LETSENCRYPT_EMAIL` in the script;
the domain is taken from `BASE_URL`, so there's nothing else to configure. On first run it
calls `certbot certonly --standalone` (via `sudo`) to issue the cert, then points the app at
it; on later runs it reuses the existing cert instead of re-issuing. Requirements:

- `BASE_URL`'s DNS **already points at this host** — Let's Encrypt validates the domain by
  connecting back to it.
- Port 80 free (certbot briefly binds it for the validation challenge) and root/sudo.
- `certbot` installed (`sudo apt-get install certbot`, `dnf install certbot`, or
  `snap install --classic certbot`) — the script checks and tells you if it's missing.
- Issuing a cert means agreeing to Let's Encrypt's Subscriber Agreement, via `--agree-tos`.

```bash
# in start_webapp_hosted.sh
BASE_URL="https://prices.example.com"
LETSENCRYPT_EMAIL="you@example.com"
```

I verified the full flow (argument construction, existing-cert reuse, the placeholder/missing
BASE_URL guard, and the missing-certbot error) against a stubbed `certbot`/`sudo`, and
confirmed the resulting cert path is correctly wired through to Python's `ssl.SSLContext` — I
can't issue a real cert from here since that needs an actual public domain, root, and port 80.

One caveat: this process only reads the cert at startup, so a renewal (certbot's own
timer/cron handles those automatically once installed) won't take effect until it restarts.
If you run this under systemd (see below), set `SYSTEMD_SERVICE_NAME` in the script so
certbot's `--deploy-hook` restarts it for you after each renewal.

**Option B — you already have a cert.** Set `SSL_CERT_PATH`/`SSL_KEY_PATH` directly (Let's
Encrypt's own files work fine here too, or any other cert). This takes precedence over Option
A — if both are set, Option A's certbot logic is skipped entirely. I tested this path
end-to-end with a real TLS 1.3 handshake against a self-signed cert.

```bash
# in start_webapp_hosted.sh
SSL_CERT_PATH="/etc/letsencrypt/live/your-domain.example.com/fullchain.pem"
SSL_KEY_PATH="/etc/letsencrypt/live/your-domain.example.com/privkey.pem"
```

Either option means the app serves `https://` and `wss://` directly on `PORT` — no reverse
proxy needed.

**Option C — a reverse proxy terminates TLS** (nginx, Caddy, or your platform's own edge/load
balancer) and forwards plain HTTP/WS to this process. Leave `LETSENCRYPT_EMAIL` and
`SSL_CERT_PATH`/`SSL_KEY_PATH` all blank; the script then runs plain HTTP and prints a
reminder. The proxy **must** forward the WebSocket upgrade headers, or `/ws` will fail even
though `/` loads fine. Minimal nginx example:

```nginx
server {
    listen 443 ssl;
    server_name your-domain.example.com;
    ssl_certificate     /etc/letsencrypt/live/your-domain.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8081;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

With a proxy in front, also set `WEBAPP_TRUST_PROXY=1` in `.env` — otherwise the IP allowlist
and `/admin`'s "Use my current IP" will see the proxy's IP for every client instead of theirs.
Only turn this on when a proxy you control is actually the sole way to reach the app; if the
app is also directly reachable on its raw port, this lets anyone bypass the allowlist by just
setting the `X-Forwarded-For` header themselves.

## Running indefinitely (24/7)

Both `examples/main.py` and `examples/webapp.py` are safe to leave running long-term:

- `MemoryCandleStorage` caps stored price history at 10,000 points *per asset* (a bounded
  `deque`), so it plateaus rather than growing forever.
- `examples/main.py` uses a `BoundedMemoryDealsStorage` (capped at the last 5,000 deals)
  instead of the SDK's default unbounded deal history, so a bot trading continuously for
  weeks/months won't slowly leak memory.
- The client reconnects automatically on network drops (`reconnection=True` by default), and
  `examples/webapp.py` re-subscribes your active watchlist automatically after a reconnect.

To also survive process crashes or a host reboot, run it under a supervisor. Minimal
`systemd` example:

```ini
# /etc/systemd/system/pocket-option-webapp.service
[Unit]
Description=PocketOption live prices web app
After=network-online.target

[Service]
WorkingDirectory=/path/to/pocket_option-0.4.0
EnvironmentFile=/path/to/pocket_option-0.4.0/.env
ExecStart=/bin/bash /path/to/pocket_option-0.4.0/start_webapp_hosted.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now pocket-option-webapp
```

If you're using Let's Encrypt (Option A above), set `SYSTEMD_SERVICE_NAME="pocket-option-webapp"`
in `start_webapp_hosted.sh` to match, so certbot restarts this service after each renewal.

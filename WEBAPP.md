# 🖥️ Live Assets & Prices Web App

`examples/webapp.py` is a small [aiohttp](https://docs.aiohttp.org) server that connects to
PocketOption using this SDK and exposes:

- `GET /` — a browser dashboard: pick assets from a searchable list, watch live prices,
  pop out a live chart per asset.
- `GET /api/assets` — JSON snapshot of all known (enabled) assets.
- `GET /api/tick` — price closest to a given timestamp, from buffered history.
- `GET /api/candles` — OHLC candles over a time window, from the same buffered history.
- `GET /ws` — a raw WebSocket you can connect to directly from your own client/script.
- `GET /admin` — a password-protected page for managing the IP allowlist, which assets are
  enabled and their display labels, and a demo "gap-fill" display toggle, at runtime.

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
| `WEBAPP_CANDLE_HISTORY_SIZE` | `500` | Raw price updates kept per asset for `/api/tick`, `/api/candles`, and a new WS subscriber's `history` message. Raise it if you need `/api/tick`/`/api/candles` to reliably reach further back in time (see those sections below) — costs roughly proportional memory per asset. |
| `WEBAPP_CANDLE_STORAGE`  | `memory`     | `memory` keeps the above in process memory only (lost on every restart). `redis` persists it in Redis instead — same bounded shape, survives restarts. Needs `pip install pocket-option[redis]` and a reachable Redis (see `REDIS_URL` and `deploy/install-redis.sh`). |
| `REDIS_URL`              | `redis://localhost:6379/0` | Only used when `WEBAPP_CANDLE_STORAGE=redis`. Point this at a dedicated instance (e.g. `deploy/install-redis.sh`'s default `redis://localhost:6380/0`) if you already run Redis for something else, so this app's data doesn't mix with it. |
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

**You receive** (JSON text frames), five message types, distinguished by `type`:

```json
{"type": "assets", "assets": [{"type": "asset", "asset": "EURUSD_otc", "...": "one entry per known asset, same shape as a single 'asset' message below"}]}
{"type": "asset", "asset": "EURUSD_otc", "label": "EUR/USD (OTC)", "assetType": "currency", "payout": 92, "isOtc": true, "active": true, "expTime": 1755550000, "minExpiration": 30, "scheduledUntil": 0, "scheduledAt": -1, "schedule": {"state": "continuous", "at": null}}
{"type": "history", "asset": "EURUSD_otc", "ticks": [{"value": "1.08420", "timestamp": 1755549961.0}, "... up to WEBAPP_CANDLE_HISTORY_SIZE entries (500 by default) ..."]}
{"type": "price", "asset": "EURUSD_otc", "value": "1.08423", "timestamp": 1755549991.2}
{"type": "connection", "connected": true}
{"type": "config", "fakeDataEnabled": false}
```

- `assets` is sent **once, to you only**, immediately on connect — the full current asset
  list, so you don't need a separate `GET /api/assets` call just to know what exists and its
  current state. Only enabled assets appear (see `/admin`'s asset manager — a disabled asset
  is invisible here and can't be subscribed to at all, even by symbol).
- `asset` is sent to **every** connected client whenever a single asset's state changes
  (payout, active/closed, schedule) — same shape as an entry inside `assets` above.
  - `assetType` is the instrument category (`currency`, `stock`, `crypto`, `commodity`, `index`, ...) — not to be confused with the message envelope's own `type: "asset"`.
  - `expTime` is the next trade's expiration timestamp — not the same as the asset's own open/close schedule.
  - `schedule` is the one to actually use for "when does this open/close" — `state` is `"closes_at"`/`"opens_at"` (with `at` a real Unix timestamp) for a real-market asset with known schedule data, `"continuous"` for an always-open OTC/synthetic asset (never closes, so no timestamp), or `"open_unscheduled"`/`"closed_unscheduled"` for a real-market asset with no schedule data available right now. The raw `scheduledUntil`/`scheduledAt` fields exist for reference but use sentinel values (`0`, `-1`) for "no data" that mean different things depending on why there's no data — `schedule` already resolves that ambiguity for you.
- `history` is sent to **you only**, once, right after you subscribe to an asset — the
  buffered price history the server already has for it (fewer if it hasn't seen that many
  ticks yet), oldest first. Arrives before any live `price` ticks for that asset. See
  "Fetching tick/candle history per asset" below for the same data pulled on demand for any
  asset, not just ones you're actively subscribed to.
- `price` ticks are only sent to connections currently subscribed to that specific asset —
  subscribing to `EURUSD_otc` will never deliver ticks for `GBPUSD_otc` on your connection,
  even though other clients might be watching it. Never sent for a closed asset, even if the
  upstream server keeps trickling residual ticks after it closes.
- `connection` reflects the *backend's own* upstream connection to PocketOption, not your
  connection to `/ws` (that one you already know is up, or you wouldn't be receiving this) —
  `connected: false` means the server is between reconnects and no real ticks are flowing to
  anyone right now, for any asset.
- `config` currently carries one field: whether the admin-controlled "gap-fill" demo display
  is on (see `/admin`) — irrelevant unless you're building a UI that mimics the bundled
  dashboard's behavior during an outage; a raw API client can ignore it.

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

## Looking up a price at a specific time

`GET /api/tick?asset=<symbol>&timestamp=<unix-seconds>` returns the buffered price closest
to the timestamp you ask for — useful for "what was the price around time X" without
having to be connected over `/ws` when it happened.

```bash
curl "https://datafeedcl.xyz/api/tick?asset=EURUSD_otc&timestamp=1755550000"
```

```json
{"asset": "EURUSD_otc", "requested_timestamp": 1755550000.0, "value": "1.08423", "timestamp": 1755549998.4, "delta_seconds": -1.6}
```

`delta_seconds` (actual minus requested) tells you how far off the match is — check it if
you need to know whether the result is a close match or a stale fallback. A disabled asset
(see `/admin`'s asset manager) returns `404` even if it has buffered history.

This searches each asset's buffered price history — up to `WEBAPP_CANDLE_HISTORY_SIZE`
updates per asset (500 by default; see the env var table above) — so it only has data for
**assets that have streamed at least once since this process started** (or since Redis
storage was set up, if using it — see below), and only as far back as that many raw ticks
reach: a fast-ticking pair might only cover the last several minutes, a slow real-market
stock could cover hours. An asset nobody's ever watched, and that isn't in
`WEBAPP_ALWAYS_ON_ASSETS`, returns `404`. Setting `WEBAPP_ALWAYS_ON_ASSETS=all` (see below)
is the easiest way to make sure every asset has something to look up — and if you specifically
need "anywhere in the last 2 hours" to reliably have an answer, raise
`WEBAPP_CANDLE_HISTORY_SIZE` enough to cover that asset's tick rate over 2 hours, or use
`WEBAPP_CANDLE_STORAGE=redis` (below) so the buffer survives restarts instead of resetting
to empty each time.

By default this history lives in process memory only, and resets to empty on every
restart. Set `WEBAPP_CANDLE_STORAGE=redis` (plus `REDIS_URL`, and `pip install
pocket-option[redis]`) to persist it in Redis instead — same bounded-per-asset shape, just
durable across restarts. See `deploy/install-redis.sh` for setting up a dedicated instance.

## Fetching tick/candle history per asset

`GET /api/candles?asset=<symbol>&timeframe=<seconds>&count=<n>` aggregates the same buffered
price history into OHLC candles — useful for charting or backtesting a whole window, not
just one point in time.

```bash
# Last 2 hours as 1-minute candles (120 of them)
curl "https://datafeedcl.xyz/api/candles?asset=EURUSD_otc&timeframe=60&count=120"

# Per-second resolution instead
curl "https://datafeedcl.xyz/api/candles?asset=EURUSD_otc&timeframe=1&count=7200"
```

```json
[
  {"timestamp": 1755549960000, "open": 1.08418, "high": 1.08430, "low": 1.08412, "close": 1.08423},
  {"timestamp": 1755550020000, "open": 1.08423, "high": 1.08440, "low": 1.08420, "close": 1.08435}
]
```

- `timestamp` is milliseconds (not seconds — matches what charting libraries like
  [klinecharts](https://klinecharts.com), which the bundled dashboard uses, expect natively).
- `timeframe` is the candle bucket size in seconds (`60` = 1-minute candles, `1` = per-second,
  `300` = 5-minute, etc.) — ticks are grouped into `floor(timestamp / timeframe) * timeframe`
  buckets, so a bucket may be missing entirely if no tick landed in that window (a normal gap
  for a slow-ticking real-market asset, not an error).
- `count` caps how many of the *most recent* candles come back, not how far back in time to
  look — asking for `count=120` at `timeframe=60` (2 hours) only actually gets you 2 hours if
  the underlying raw-tick buffer reaches back that far (see `WEBAPP_CANDLE_HISTORY_SIZE`
  above); a high-volume asset can burn through that buffer in far less than 2 hours, so you
  may get fewer candles than asked for. Check the first candle's `timestamp` against what you
  expected rather than assuming a full window came back.
- Same 404-if-disabled and empty-if-never-streamed rules as `/api/tick` apply.
- This is exactly what powers the 📈 chart button in the bundled dashboard (`GET /` — click
  any watched asset's chart icon for a live example against real data).

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

For a real production deployment (systemd, resource limits, a dedicated Redis instance,
nginx + Let's Encrypt), see [deploy/README.md](deploy/README.md) and `deploy/setup.sh` — it
handles all of that in one command and is the current recommended path for `datafeedcl.xyz`,
which reverse-proxies through an nginx already running on that VPS for other projects (so
this app never touches port 80/443 or a certificate at all — see Option C below). The rest
of this section covers `start_webapp_hosted.sh` itself, and the other two options, for
deploying somewhere *without* an existing reverse proxy in front.

`./start_webapp_hosted.sh` is a production launcher: it binds on all interfaces
(`WEBAPP_HOST=0.0.0.0`), skips auto-opening a browser, and prints the URLs you'll actually
use once it's up. Edit the variables at the top of the file — `BASE_URL` (your public
domain) and `PORT` — then run it. There are three ways to get `wss://`:

**Option A — auto-obtain a free Let's Encrypt cert.** Set `LETSENCRYPT_EMAIL` in the script;
the domain is taken from `BASE_URL`, so there's nothing else to configure. On first run it
calls `certbot certonly --standalone` (via `sudo`) to issue the cert, then points the app at
it; on later runs it reuses the existing cert instead of re-issuing.

> **Don't use this option if you'll run the app under systemd** — `sudo` has no terminal to
> prompt on in that context, so it just hangs or fails on first start. It's also mutually
> exclusive with the `datafeedcl.xyz` setup above: `--standalone` needs port 80 completely
> free, which isn't true once nginx is already running. Only use Option A for a domain with
> no reverse proxy in front, run directly/interactively at a terminal with real sudo access.

Requirements:

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

- `examples/webapp.py`'s candle/tick storage (`WEBAPP_CANDLE_HISTORY_SIZE`, default 500 —
  see the env var table above) caps stored price history *per asset*, so it plateaus rather
  than growing forever; `MemoryCandleStorage` itself (used directly, outside the web app)
  defaults to a higher 10,000-point cap.
- `examples/main.py` uses a `BoundedMemoryDealsStorage` (capped at the last 5,000 deals)
  instead of the SDK's default unbounded deal history, so a bot trading continuously for
  weeks/months won't slowly leak memory.
- The client reconnects automatically on network drops (`reconnection=True` by default), and
  `examples/webapp.py` re-subscribes your active watchlist automatically after a reconnect,
  plus watches for and recovers from a couple of upstream states that don't resolve on their
  own (see `connection_watchdog`/`per_asset_watchdog` in `examples/webapp.py` if curious).

To also survive process crashes or a host reboot, run it under a supervisor — see
[deploy/README.md](deploy/README.md) for the full systemd setup (including hard resource caps,
so a bug here can't starve other things running on the same box).

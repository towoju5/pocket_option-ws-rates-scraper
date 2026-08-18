import asyncio
import functools
import hmac
import ipaddress
import logging
import os
import secrets
import socket
import ssl
import time
import webbrowser
from collections import defaultdict, deque
from urllib.parse import urlparse

from aiohttp import WSMsgType, web

from pocket_option import PocketOptionClient
from pocket_option.constants import Regions
from pocket_option.contrib.default_init import default_init
from pocket_option.models import Asset, AuthorizationData, ChangeAssetRequest, UpdateAssetItem, UpdateCloseValueItem

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)

HOST = os.environ.get("WEBAPP_HOST", "127.0.0.1")
PORT = int(os.environ.get("WEBAPP_PORT", "8081"))
AUTO_OPEN_BROWSER = os.environ.get("WEBAPP_AUTO_OPEN", "1") != "0"
STREAM_PERIOD = 5
TICK_HISTORY_SIZE = 50

# Native TLS (wss:// without a reverse proxy in front). Both must be set to enable it.
SSL_CERT_PATH = os.environ.get("WEBAPP_SSL_CERT", "")
SSL_KEY_PATH = os.environ.get("WEBAPP_SSL_KEY", "")
SSL_ENABLED = bool(SSL_CERT_PATH and SSL_KEY_PATH)

# Only trust X-Forwarded-For/-Proto when actually deployed behind a reverse proxy that
# sets them — otherwise any client could spoof them to bypass the IP allowlist below.
TRUST_PROXY = os.environ.get("WEBAPP_TRUST_PROXY", "0") == "1"

ADMIN_PASSWORD = os.environ.get("WEBAPP_ADMIN_PASSWORD", "")
ADMIN_SESSION_SECONDS = float(os.environ.get("WEBAPP_ADMIN_SESSION_HOURS", "12")) * 3600
ADMIN_COOKIE_NAME = "po_admin_session"
MAX_WHITELIST_TTL_MINUTES = 60 * 24 * 30
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300


def get_client_ip(request: web.Request) -> str | None:
    if TRUST_PROXY:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote


def is_request_secure(request: web.Request) -> bool:
    if SSL_ENABLED:
        return True
    if TRUST_PROXY:
        return request.headers.get("X-Forwarded-Proto", "").lower() == "https"
    return request.scheme == "https"


# ---------------------------------------------------------------------------
# IP allowlisting: a static, env-configured set plus a dynamic, admin-managed
# set with optional per-entry expiry.
# ---------------------------------------------------------------------------


def _resolve_host_ips(host: str) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        logger.warning("Could not resolve allowlist host %r, skipping", host)
        return set()
    return {ipaddress.ip_address(info[4][0]) for info in infos}


def resolve_entry_to_ips(entry: str) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolves a single allowlist entry (IPv4, IPv6, "localhost", or a hostname/URL) to IPs."""
    if entry.lower() == "localhost":
        return {ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")}
    try:
        return {ipaddress.ip_address(entry)}
    except ValueError:
        pass
    host = urlparse(entry if "://" in entry else f"//{entry}").hostname or entry
    return _resolve_host_ips(host)


def build_allowed_ips(raw: str) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    allowed: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for entry in filter(None, (part.strip() for part in raw.split(","))):
        allowed.update(resolve_entry_to_ips(entry))
    return allowed


ALLOWED_CLIENTS = build_allowed_ips(os.environ.get("WEBAPP_ALLOWED_CLIENTS", ""))
if ALLOWED_CLIENTS:
    logger.info("Static allowlist: %s", ", ".join(sorted(str(ip) for ip in ALLOWED_CLIENTS)))

# ip -> {"label": original entry text, "expires_at": float | None, "added_at": float}
dynamic_whitelist: dict[ipaddress.IPv4Address | ipaddress.IPv6Address, dict] = {}


def sweep_expired_whitelist() -> None:
    now = time.time()
    expired = [ip for ip, info in dynamic_whitelist.items() if info["expires_at"] is not None and info["expires_at"] <= now]
    for ip in expired:
        del dynamic_whitelist[ip]


def is_ip_allowed(remote_ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    candidates = {remote_ip}
    if isinstance(remote_ip, ipaddress.IPv6Address) and remote_ip.ipv4_mapped:
        candidates.add(remote_ip.ipv4_mapped)
    if candidates & ALLOWED_CLIENTS:
        return True
    now = time.time()
    for ip in candidates:
        info = dynamic_whitelist.get(ip)
        if info and (info["expires_at"] is None or info["expires_at"] > now):
            return True
    return False


ADMIN_ROUTE_PREFIXES = ("/admin", "/api/admin/", "/api/whoami")


@web.middleware
async def allowlist_middleware(request: web.Request, handler):
    if request.path.startswith(ADMIN_ROUTE_PREFIXES):
        return await handler(request)
    if not ALLOWED_CLIENTS and not dynamic_whitelist:
        return await handler(request)
    remote = get_client_ip(request)
    try:
        remote_ip = ipaddress.ip_address(remote)
    except (TypeError, ValueError):
        raise web.HTTPForbidden(text="Access denied") from None
    if not is_ip_allowed(remote_ip):
        logger.warning("Rejected connection from %s (not whitelisted)", remote)
        raise web.HTTPForbidden(text="Access denied")
    return await handler(request)


# ---------------------------------------------------------------------------
# Admin authentication: password login -> short-lived session cookie.
# ---------------------------------------------------------------------------

admin_sessions: dict[str, float] = {}
login_attempts: dict[str, list[float]] = defaultdict(list)


def is_locked_out(remote: str) -> bool:
    cutoff = time.time() - LOGIN_LOCKOUT_SECONDS
    recent = [t for t in login_attempts[remote] if t > cutoff]
    login_attempts[remote] = recent
    return len(recent) >= LOGIN_MAX_ATTEMPTS


def record_failed_attempt(remote: str) -> None:
    login_attempts[remote].append(time.time())


def get_valid_session(request: web.Request) -> str | None:
    token = request.cookies.get(ADMIN_COOKIE_NAME)
    if not token:
        return None
    expires_at = admin_sessions.get(token)
    if expires_at is None or expires_at < time.time():
        admin_sessions.pop(token, None)
        return None
    return token


def require_admin(handler):
    @functools.wraps(handler)
    async def wrapper(request: web.Request):
        if not get_valid_session(request):
            raise web.HTTPUnauthorized(text="Not authenticated")
        return await handler(request)

    return wrapper


# ---------------------------------------------------------------------------
# PocketOption client
# ---------------------------------------------------------------------------

client = PocketOptionClient(logger=True)

default_init(
    client,
    authorization=AuthorizationData.model_validate(
        {
            "session": os.environ["PO_SESSION"],
            "isDemo": int(os.environ.get("PO_IS_DEMO", "1")),
            "uid": int(os.environ["PO_UID"]),
            "platform": 2,
            "isFastHistory": True,
            "isOptimized": True,
        },
    ),
    sub_assets=[],
)

# ws -> set of assets that connection is currently watching
socket_watchlists: dict[web.WebSocketResponse, set[Asset]] = {}
subscriber_counts: dict[Asset, int] = {}
tick_history: dict[Asset, deque[dict]] = defaultdict(lambda: deque(maxlen=TICK_HISTORY_SIZE))


def asset_payload(item: UpdateAssetItem) -> dict:
    return {
        "type": "asset",
        "asset": item.asset.value,
        "label": item.label,
        "payout": item.payout,
        "isOtc": item.is_otc,
        "active": item.active,
        "expTime": item.exp_time,
        "type": item.type,
        "min_expiration": item.min_expiration,
        # 'item': item.model_dump(),
    }


@client.on.assets_update
async def on_assets_update(items: list[UpdateAssetItem]):
    for item in items:
        await broadcast_all(asset_payload(item))


@client.on.success_auth
async def on_success_auth(_):
    # Re-establish live subscriptions after (re)connecting, since the
    # server forgets them across a disconnect/reconnect cycle.
    for asset in list(subscriber_counts):
        try:
            await client.emit.subscribe_to_asset(asset)
            await client.emit.change_asset(ChangeAssetRequest(asset=asset, period=STREAM_PERIOD))
        except Exception:
            logger.exception("Failed to resubscribe to %s after reconnect", asset)


@client.on.update_close_value
async def on_update_close_value(items: list[UpdateCloseValueItem]):
    for item in items:
        tick = {"value": str(item.value), "timestamp": item.timestamp}
        tick_history[item.asset].append(tick)
        await broadcast_price(item.asset, {"type": "price", "asset": item.asset.value, **tick})


async def broadcast_all(message: dict):
    dead = []
    for ws in socket_watchlists:
        try:
            await ws.send_json(message)
        except ConnectionResetError:
            dead.append(ws)
    for ws in dead:
        socket_watchlists.pop(ws, None)


async def broadcast_price(asset: Asset, message: dict):
    dead = []
    for ws, watched in socket_watchlists.items():
        if asset not in watched:
            continue
        try:
            await ws.send_json(message)
        except ConnectionResetError:
            dead.append(ws)
    for ws in dead:
        socket_watchlists.pop(ws, None)


async def subscribe(asset: Asset):
    subscriber_counts[asset] = subscriber_counts.get(asset, 0) + 1
    if subscriber_counts[asset] != 1:
        return
    try:
        await client.wait_for_authorization(timeout=15)
        await client.emit.subscribe_to_asset(asset)
        await client.emit.change_asset(ChangeAssetRequest(asset=asset, period=STREAM_PERIOD))
    except Exception:
        logger.exception("Failed to subscribe to %s", asset)
        subscriber_counts.pop(asset, None)


async def unsubscribe(asset: Asset):
    if asset not in subscriber_counts:
        return
    subscriber_counts[asset] -= 1
    if subscriber_counts[asset] <= 0:
        del subscriber_counts[asset]
        try:
            await client.emit.unsubscribe_from_asset(asset)
        except Exception:
            logger.exception("Failed to unsubscribe from %s", asset)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def index(_: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


async def list_assets(_: web.Request) -> web.Response:
    items = await client.assets.get_assets()
    return web.json_response([asset_payload(item) for item in items])


async def whoami(request: web.Request) -> web.Response:
    return web.json_response({"ip": get_client_ip(request)})


async def admin_page(_: web.Request) -> web.Response:
    return web.Response(text=ADMIN_HTML, content_type="text/html")


async def admin_login(request: web.Request) -> web.Response:
    if not ADMIN_PASSWORD:
        raise web.HTTPServiceUnavailable(text="Set WEBAPP_ADMIN_PASSWORD to enable the admin panel")
    remote = get_client_ip(request) or "unknown"
    if is_locked_out(remote):
        raise web.HTTPTooManyRequests(text="Too many failed attempts, try again later")
    data = await request.json()
    password = str(data.get("password", ""))
    if not hmac.compare_digest(password, ADMIN_PASSWORD):
        record_failed_attempt(remote)
        raise web.HTTPUnauthorized(text="Incorrect password")
    login_attempts.pop(remote, None)
    token = secrets.token_urlsafe(32)
    admin_sessions[token] = time.time() + ADMIN_SESSION_SECONDS
    resp = web.json_response({"ok": True})
    resp.set_cookie(
        ADMIN_COOKIE_NAME,
        token,
        httponly=True,
        samesite="Strict",
        secure=is_request_secure(request),
        max_age=int(ADMIN_SESSION_SECONDS),
    )
    return resp


async def admin_logout(request: web.Request) -> web.Response:
    token = request.cookies.get(ADMIN_COOKIE_NAME)
    if token:
        admin_sessions.pop(token, None)
    resp = web.json_response({"ok": True})
    resp.del_cookie(ADMIN_COOKIE_NAME)
    return resp


@require_admin
async def admin_get_whitelist(_: web.Request) -> web.Response:
    sweep_expired_whitelist()
    dynamic = [
        {"ip": str(ip), "label": info["label"], "expiresAt": info["expires_at"], "addedAt": info["added_at"]}
        for ip, info in dynamic_whitelist.items()
    ]
    return web.json_response(
        {
            "static": sorted(str(ip) for ip in ALLOWED_CLIENTS),
            "dynamic": dynamic,
            "now": time.time(),
        },
    )


@require_admin
async def admin_add_whitelist(request: web.Request) -> web.Response:
    data = await request.json()
    entry = str(data.get("entry", "")).strip()
    if not entry:
        raise web.HTTPBadRequest(text="entry is required")
    try:
        ttl_minutes = float(data.get("ttlMinutes") or 0)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="ttlMinutes must be a number") from None
    ttl_minutes = max(0.0, min(ttl_minutes, MAX_WHITELIST_TTL_MINUTES))

    ips = resolve_entry_to_ips(entry)
    if not ips:
        raise web.HTTPBadRequest(text=f"Could not resolve {entry!r}")

    expires_at = time.time() + ttl_minutes * 60 if ttl_minutes > 0 else None
    added_at = time.time()
    for ip in ips:
        dynamic_whitelist[ip] = {"label": entry, "expires_at": expires_at, "added_at": added_at}
    logger.info(
        "Admin whitelisted %r -> %s (%s)",
        entry,
        ", ".join(str(ip) for ip in ips),
        "permanent" if expires_at is None else f"expires in {ttl_minutes:.0f}m",
    )
    return web.json_response({"ok": True, "resolved": [str(ip) for ip in ips], "expiresAt": expires_at})


@require_admin
async def admin_remove_whitelist(request: web.Request) -> web.Response:
    data = await request.json()
    try:
        ip = ipaddress.ip_address(str(data.get("ip", "")).strip())
    except ValueError:
        raise web.HTTPBadRequest(text="invalid ip") from None
    dynamic_whitelist.pop(ip, None)
    return web.json_response({"ok": True})


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    watched: set[Asset] = set()
    socket_watchlists[ws] = watched

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            data = msg.json()
            try:
                asset = Asset(data["asset"])
            except (KeyError, ValueError):
                continue
            if data.get("action") == "subscribe" and asset not in watched:
                watched.add(asset)
                # Send the buffered history immediately (it's already local); the
                # upstream (re)subscribe below may take longer if reconnecting.
                await ws.send_json(
                    {"type": "history", "asset": asset.value, "ticks": list(tick_history.get(asset, []))},
                )
                await subscribe(asset)
            elif data.get("action") == "unsubscribe" and asset in watched:
                watched.discard(asset)
                await unsubscribe(asset)
    finally:
        socket_watchlists.pop(ws, None)
        for asset in watched:
            await unsubscribe(asset)

    return ws


async def open_browser_when_ready():
    await asyncio.sleep(0.5)
    browse_host = "127.0.0.1" if HOST in ("0.0.0.0", "::") else HOST
    scheme = "https" if SSL_ENABLED else "http"
    try:
        webbrowser.open(f"{scheme}://{browse_host}:{PORT}")
    except Exception:
        logger.debug("Could not auto-open browser", exc_info=True)


async def start_client(app: web.Application):
    async def run():
        await client.connect(Regions.DEMO)
        await client.wait()

    app["client_task"] = asyncio.create_task(run())
    if AUTO_OPEN_BROWSER:
        asyncio.create_task(open_browser_when_ready())  # noqa: RUF006


async def stop_client(app: web.Application):
    app["client_task"].cancel()
    await client.disconnect()


def create_app() -> web.Application:
    app = web.Application(middlewares=[allowlist_middleware])
    app.router.add_get("/", index)
    app.router.add_get("/api/assets", list_assets)
    app.router.add_get("/api/whoami", whoami)
    app.router.add_get("/ws", ws_handler)

    app.router.add_get("/admin", admin_page)
    app.router.add_post("/api/admin/login", admin_login)
    app.router.add_post("/api/admin/logout", admin_logout)
    app.router.add_get("/api/admin/whitelist", admin_get_whitelist)
    app.router.add_post("/api/admin/whitelist/add", admin_add_whitelist)
    app.router.add_post("/api/admin/whitelist/remove", admin_remove_whitelist)

    app.on_startup.append(start_client)
    app.on_cleanup.append(stop_client)
    return app


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PocketOption Live Prices</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; font-family: system-ui, sans-serif; background: #0f1115; color: #e6e6e6; }
  header { padding: 1rem 1.5rem; border-bottom: 1px solid #262a33; display: flex; align-items: center; gap: 1rem; }
  header h1 { font-size: 1.1rem; margin: 0; font-weight: 600; }
  header a { margin-left: auto; color: #9aa1ac; font-size: 0.8rem; text-decoration: none; }
  header a:hover { color: #e6e6e6; }
  #status { font-size: 0.8rem; padding: 0.2rem 0.6rem; border-radius: 1rem; background: #3a2a2a; color: #e08080; }
  #status.connected { background: #234a2f; color: #6fd98a; }
  main { display: flex; gap: 1rem; padding: 1rem 1.5rem; height: calc(100vh - 4.5rem); box-sizing: border-box; }
  section { background: #161922; border: 1px solid #262a33; border-radius: 0.5rem; overflow: hidden; display: flex; flex-direction: column; }
  #assets-panel { width: 320px; }
  #watch-panel { flex: 1; }
  .panel-header { padding: 0.75rem 1rem; border-bottom: 1px solid #262a33; font-size: 0.85rem; color: #9aa1ac; }
  input[type=search] { width: 100%; box-sizing: border-box; padding: 0.5rem; margin: 0.5rem; background: #0f1115; border: 1px solid #262a33; border-radius: 0.35rem; color: inherit; }
  .panel-actions { display: flex; gap: 0.5rem; padding: 0 0.5rem 0.5rem; }
  button.action-btn { flex: 1; padding: 0.4rem 0.5rem; font-size: 0.78rem; background: #1c2029; border: 1px solid #262a33; border-radius: 0.35rem; color: #e6e6e6; cursor: pointer; }
  button.action-btn:hover { background: #262a33; }
  ul, table { list-style: none; margin: 0; padding: 0; width: 100%; }
  #asset-list { overflow-y: auto; flex: 1; }
  #asset-list li { padding: 0.5rem 1rem; cursor: pointer; display: flex; justify-content: space-between; font-size: 0.85rem; border-bottom: 1px solid #1c2029; }
  #asset-list li:hover { background: #1c2029; }
  #asset-list li.watching { color: #6fd98a; }
  #asset-list li.inactive { opacity: 0.45; }
  .closes { color: #9aa1ac; }
  .closes.soon { color: #e0b060; }
  .closes.closed { color: #e08080; }
  table { border-collapse: collapse; }
  th, td { text-align: left; padding: 0.6rem 1rem; font-size: 0.85rem; border-bottom: 1px solid #1c2029; }
  th { color: #9aa1ac; font-weight: 500; position: sticky; top: 0; background: #161922; }
  tbody tr:hover { background: #1c2029; }
  .price-up { color: #6fd98a; }
  .price-down { color: #e08080; }
  .remove-btn { cursor: pointer; color: #9aa1ac; }
  .remove-btn:hover { color: #e08080; }
  #watch-body-wrap { overflow-y: auto; flex: 1; }
  .empty { padding: 1rem; color: #6b7280; font-size: 0.85rem; }
  .sparkline { display: block; }
</style>
</head>
<body>
<header>
  <h1>PocketOption Live Prices</h1>
  <span id="status">connecting…</span>
  <a href="/admin">Admin / Whitelist</a>
</header>
<main>
  <section id="assets-panel">
    <div class="panel-header">Assets</div>
    <input type="search" id="search" placeholder="Search assets…">
    <div class="panel-actions">
      <button class="action-btn" id="watch-all-btn">Watch all active</button>
      <button class="action-btn" id="unwatch-all-btn">Unwatch all</button>
    </div>
    <ul id="asset-list"></ul>
  </section>
  <section id="watch-panel">
    <div class="panel-header">Watchlist — live price stream</div>
    <div id="watch-body-wrap">
      <table>
        <thead><tr><th>Asset</th><th>Label</th><th>Payout</th><th>Price</th><th>Trend</th><th>Updated</th><th>Closes</th><th></th></tr></thead>
        <tbody id="watch-body"></tbody>
      </table>
      <div class="empty" id="watch-empty">Click an asset on the left to start streaming its price.</div>
    </div>
  </section>
</main>
<script>
const assets = new Map();
const watched = new Map();
let ws;

function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  const status = document.getElementById("status");
  ws.onopen = () => { status.textContent = "connected"; status.classList.add("connected"); };
  ws.onclose = () => { status.textContent = "reconnecting…"; status.classList.remove("connected"); setTimeout(connect, 1500); };
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "asset") onAsset(msg);
    if (msg.type === "history") onHistory(msg);
    if (msg.type === "price") onPrice(msg);
  };
}

function onAsset(msg) {
  assets.set(msg.asset, msg);
  renderAssetList();
  if (watched.has(msg.asset)) {
    watched.get(msg.asset).label = msg.label;
    watched.get(msg.asset).payout = msg.payout;
    renderWatchRow(msg.asset);
  }
}

function onHistory(msg) {
  const row = watched.get(msg.asset);
  if (!row) return;
  row.history = msg.ticks.map((t) => Number(t.value));
  if (msg.ticks.length) {
    const last = msg.ticks[msg.ticks.length - 1];
    row.value = last.value;
    row.timestamp = last.timestamp;
  }
  renderWatchRow(msg.asset);
}

function onPrice(msg) {
  const row = watched.get(msg.asset);
  if (!row) return;
  row.prevValue = row.value;
  row.value = msg.value;
  row.timestamp = msg.timestamp;
  row.history.push(Number(msg.value));
  if (row.history.length > 50) row.history.shift();
  renderWatchRow(msg.asset);
}

function renderAssetList() {
  const filter = document.getElementById("search").value.trim().toLowerCase();
  const list = document.getElementById("asset-list");
  list.innerHTML = "";
  [...assets.values()]
    .filter((a) => a.asset.toLowerCase().includes(filter) || a.label.toLowerCase().includes(filter))
    .sort((a, b) => a.asset.localeCompare(b.asset))
    .forEach((a) => {
      const li = document.createElement("li");
      li.textContent = `${a.label} (${a.payout}%)`;
      if (watched.has(a.asset)) li.classList.add("watching");
      if (a.active === false) li.classList.add("inactive");
      li.onclick = () => toggleWatch(a.asset);
      list.appendChild(li);
    });
}

function toggleWatch(asset) {
  if (watched.has(asset)) {
    watched.delete(asset);
    ws.send(JSON.stringify({ action: "unsubscribe", asset }));
    document.getElementById(`row-${asset}`)?.remove();
  } else {
    const meta = assets.get(asset) || { label: asset, payout: "-", expTime: null };
    watched.set(asset, { label: meta.label, payout: meta.payout, expTime: meta.expTime, value: null, prevValue: null, timestamp: null, history: [] });
    ws.send(JSON.stringify({ action: "subscribe", asset }));
    renderWatchRow(asset);
  }
  renderAssetList();
  updateEmptyState();
}

function watchAllActive() {
  [...assets.values()]
    .filter((a) => a.active !== false && !watched.has(a.asset))
    .forEach((a) => toggleWatch(a.asset));
}

function unwatchAll() {
  [...watched.keys()].forEach((asset) => toggleWatch(asset));
}

function formatCloses(expTime) {
  if (!expTime) return { text: "—", cls: "" };
  const secondsLeft = expTime - Date.now() / 1000;
  if (secondsLeft <= 0) return { text: "closed", cls: "closed" };
  const hours = Math.floor(secondsLeft / 3600);
  const minutes = Math.floor((secondsLeft % 3600) / 60);
  const text = hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
  return { text: `in ${text}`, cls: secondsLeft < 900 ? "soon" : "" };
}

function sparkline(values) {
  if (!values || values.length < 2) return "";
  const w = 100, h = 26, pad = 2;
  const min = Math.min(...values), max = Math.max(...values);
  const span = max - min || 1;
  const points = values.map((v, i) => {
    const x = pad + (i / (values.length - 1)) * (w - pad * 2);
    const y = h - pad - ((v - min) / span) * (h - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const color = values[values.length - 1] >= values[0] ? "#6fd98a" : "#e08080";
  return `<svg class="sparkline" width="${w}" height="${h}"><polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.5"/></svg>`;
}

function renderWatchRow(asset) {
  const data = watched.get(asset);
  if (!data) return;
  let row = document.getElementById(`row-${asset}`);
  if (!row) {
    row = document.createElement("tr");
    row.id = `row-${asset}`;
    document.getElementById("watch-body").appendChild(row);
  }
  const direction = data.prevValue != null ? (Number(data.value) > Number(data.prevValue) ? "price-up" : Number(data.value) < Number(data.prevValue) ? "price-down" : "") : "";
  const time = data.timestamp ? new Date(data.timestamp * 1000).toLocaleTimeString() : "-";
  const closes = formatCloses(data.expTime);
  row.innerHTML = `
    <td>${asset}</td>
    <td>${data.label}</td>
    <td>${data.payout}%</td>
    <td class="${direction}">${data.value ?? "…"}</td>
    <td>${sparkline(data.history)}</td>
    <td>${time}</td>
    <td class="closes ${closes.cls}">${closes.text}</td>
    <td class="remove-btn" onclick="toggleWatch('${asset}')">✕</td>
  `;
  updateEmptyState();
}

function updateEmptyState() {
  document.getElementById("watch-empty").style.display = watched.size ? "none" : "block";
}

document.getElementById("search").addEventListener("input", renderAssetList);
document.getElementById("watch-all-btn").addEventListener("click", watchAllActive);
document.getElementById("unwatch-all-btn").addEventListener("click", unwatchAll);
setInterval(() => watched.forEach((_, asset) => renderWatchRow(asset)), 30000);

fetch("/api/assets").then((r) => r.json()).then((items) => {
  items.forEach((item) => assets.set(item.asset, item));
  renderAssetList();
});

connect();
</script>
</body>
</html>
"""


ADMIN_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PocketOption Admin — Whitelist</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; font-family: system-ui, sans-serif; background: #0f1115; color: #e6e6e6; }
  header { padding: 1rem 1.5rem; border-bottom: 1px solid #262a33; display: flex; align-items: center; gap: 1rem; }
  header h1 { font-size: 1.1rem; margin: 0; font-weight: 600; }
  header a { margin-left: auto; color: #9aa1ac; font-size: 0.8rem; text-decoration: none; }
  header a:hover { color: #e6e6e6; }
  main { max-width: 640px; margin: 2rem auto; padding: 0 1.5rem; }
  section { background: #161922; border: 1px solid #262a33; border-radius: 0.5rem; padding: 1.25rem; margin-bottom: 1.25rem; }
  h2 { font-size: 0.95rem; margin: 0 0 1rem; color: #9aa1ac; font-weight: 600; }
  label { display: block; font-size: 0.8rem; color: #9aa1ac; margin-bottom: 0.3rem; }
  input { width: 100%; box-sizing: border-box; padding: 0.5rem; margin-bottom: 0.75rem; background: #0f1115; border: 1px solid #262a33; border-radius: 0.35rem; color: inherit; font-size: 0.85rem; }
  button { padding: 0.5rem 1rem; font-size: 0.85rem; background: #234a2f; border: 1px solid #2f6b45; border-radius: 0.35rem; color: #6fd98a; cursor: pointer; }
  button:hover { background: #2a5638; }
  button.secondary { background: #1c2029; border-color: #262a33; color: #e6e6e6; }
  button.secondary:hover { background: #262a33; }
  button.danger { background: transparent; border: none; color: #9aa1ac; padding: 0.2rem 0.5rem; }
  button.danger:hover { color: #e08080; }
  .row { display: flex; gap: 0.5rem; align-items: flex-end; }
  .row > div { flex: 1; }
  table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
  th, td { text-align: left; padding: 0.5rem; border-bottom: 1px solid #1c2029; }
  th { color: #9aa1ac; font-weight: 500; }
  .muted { color: #6b7280; font-size: 0.8rem; }
  .error { color: #e08080; font-size: 0.82rem; margin-bottom: 0.75rem; min-height: 1em; }
  #app { display: none; }
  #login-error { color: #e08080; font-size: 0.82rem; }
</style>
</head>
<body>
<header>
  <h1>Admin — IP Whitelist</h1>
  <a href="/">Back to dashboard</a>
</header>
<main>
  <section id="login-section">
    <h2>Login</h2>
    <label for="password">Admin password</label>
    <input type="password" id="password" placeholder="Password">
    <div id="login-error" class="error"></div>
    <button id="login-btn">Log in</button>
  </section>

  <div id="app">
    <section>
      <h2>Add an allowed client</h2>
      <div class="row">
        <div>
          <label for="entry">IPv4, IPv6, "localhost", or a hostname/URL</label>
          <input type="text" id="entry" placeholder="203.0.113.5">
        </div>
        <div style="flex: 0 0 140px">
          <label for="ttl">Expires in (minutes)</label>
          <input type="number" id="ttl" min="0" placeholder="0 = never">
        </div>
      </div>
      <div id="add-error" class="error"></div>
      <button id="add-btn">Add to whitelist</button>
      <button class="secondary" id="use-my-ip-btn" type="button">Use my current IP</button>
    </section>

    <section>
      <h2>Dynamic entries (added here)</h2>
      <table>
        <thead><tr><th>IP</th><th>Entry</th><th>Expires</th><th></th></tr></thead>
        <tbody id="dynamic-body"></tbody>
      </table>
      <p class="muted" id="dynamic-empty">None yet.</p>
    </section>

    <section>
      <h2>Static entries (from WEBAPP_ALLOWED_CLIENTS, read-only)</h2>
      <table>
        <thead><tr><th>IP</th></tr></thead>
        <tbody id="static-body"></tbody>
      </table>
      <p class="muted" id="static-empty">None configured.</p>
      <button class="secondary" id="logout-btn" type="button" style="margin-top: 0.5rem">Log out</button>
    </section>
  </div>
</main>
<script>
let myIp = null;
fetch("/api/whoami").then((r) => r.json()).then((d) => { myIp = d.ip; });

async function api(path, options) {
  const resp = await fetch(path, { credentials: "same-origin", ...options });
  if (resp.status === 401) throw new Error("unauthorized");
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || resp.statusText);
  return data;
}

function fmtExpiry(expiresAt, now) {
  if (expiresAt == null) return "never";
  const secondsLeft = expiresAt - now;
  if (secondsLeft <= 0) return "expired";
  const hours = Math.floor(secondsLeft / 3600);
  const minutes = Math.floor((secondsLeft % 3600) / 60);
  return hours > 0 ? `in ${hours}h ${minutes}m` : `in ${minutes}m`;
}

async function refresh() {
  const data = await api("/api/admin/whitelist");
  const dynBody = document.getElementById("dynamic-body");
  dynBody.innerHTML = "";
  data.dynamic.forEach((entry) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${entry.ip}</td>
      <td>${entry.label}</td>
      <td>${fmtExpiry(entry.expiresAt, data.now)}</td>
      <td><button class="danger" data-ip="${entry.ip}">✕</button></td>
    `;
    tr.querySelector("button").onclick = () => removeEntry(entry.ip);
    dynBody.appendChild(tr);
  });
  document.getElementById("dynamic-empty").style.display = data.dynamic.length ? "none" : "block";

  const staticBody = document.getElementById("static-body");
  staticBody.innerHTML = "";
  data.static.forEach((ip) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${ip}</td>`;
    staticBody.appendChild(tr);
  });
  document.getElementById("static-empty").style.display = data.static.length ? "none" : "block";
}

async function removeEntry(ip) {
  await api("/api/admin/whitelist/remove", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ip }) });
  refresh();
}

document.getElementById("add-btn").onclick = async () => {
  const entry = document.getElementById("entry").value.trim();
  const ttlMinutes = document.getElementById("ttl").value;
  const errEl = document.getElementById("add-error");
  errEl.textContent = "";
  if (!entry) { errEl.textContent = "Enter an IP, hostname, or \\"localhost\\"."; return; }
  try {
    await api("/api/admin/whitelist/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entry, ttlMinutes: ttlMinutes || 0 }),
    });
    document.getElementById("entry").value = "";
    document.getElementById("ttl").value = "";
    refresh();
  } catch (e) {
    errEl.textContent = e.message;
  }
};

document.getElementById("use-my-ip-btn").onclick = () => {
  if (myIp) document.getElementById("entry").value = myIp;
};

document.getElementById("logout-btn").onclick = async () => {
  await fetch("/api/admin/logout", { method: "POST", credentials: "same-origin" });
  location.reload();
};

document.getElementById("login-btn").onclick = async () => {
  const password = document.getElementById("password").value;
  const errEl = document.getElementById("login-error");
  errEl.textContent = "";
  try {
    await api("/api/admin/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    showApp();
  } catch (e) {
    errEl.textContent = "Incorrect password or too many attempts.";
  }
};
document.getElementById("password").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("login-btn").click();
});

function showApp() {
  document.getElementById("login-section").style.display = "none";
  document.getElementById("app").style.display = "block";
  refresh();
  setInterval(refresh, 15000);
}

api("/api/admin/whitelist").then(showApp).catch(() => {});
</script>
</body>
</html>
"""


def build_ssl_context() -> ssl.SSLContext | None:
    if not SSL_ENABLED:
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(SSL_CERT_PATH, SSL_KEY_PATH)
    return ctx


if __name__ == "__main__":
    ssl_context = build_ssl_context()
    if ssl_context:
        logger.info("TLS enabled — serving https/wss on %s:%s", HOST, PORT)
    web.run_app(create_app(), host=HOST, port=PORT, ssl_context=ssl_context)

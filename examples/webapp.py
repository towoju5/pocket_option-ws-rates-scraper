import asyncio
import functools
import hmac
import ipaddress
import json
import logging
import logging.handlers
import os
import pathlib
import secrets
import socket
import ssl
import time
import webbrowser
from collections import defaultdict
from urllib.parse import urlparse

from aiohttp import WSMsgType, web

from pocket_option import PocketOptionClient
from pocket_option.constants import Regions
from pocket_option.contrib.candles import CandleStorage, MemoryCandleStorage, RedisCandleStorage
from pocket_option.contrib.default_init import default_init
from pocket_option.models import Asset, AuthorizationData, ChangeAssetRequest, UpdateAssetItem, UpdateCloseValueItem

# Kept small on purpose (a few MB total) - plenty for a 1GB RAM / 30GB disk VPS, and
# lets you `tail -f` or grep history without needing journald (e.g. when running via
# start_webapp.sh in a terminal instead of the systemd unit).
LOG_FILE = os.environ.get("WEBAPP_LOG_FILE", "webapp.log")

_log_handlers: list[logging.Handler] = [logging.StreamHandler()]
if LOG_FILE:
    _log_handlers.append(
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3),
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=_log_handlers,
)

logger = logging.getLogger(__name__)

# How long we'll tolerate zero price ticks (while assets are actively subscribed)
# before assuming the upstream Socket.IO connection is silently wedged - see
# connection_watchdog() below - and forcing a clean reconnect instead of waiting on
# the socketio/engineio library to notice on its own.
STALE_AFTER = float(os.environ.get("WEBAPP_STALE_AFTER", "60"))

HOST = os.environ.get("WEBAPP_HOST", "127.0.0.1")
PORT = int(os.environ.get("WEBAPP_PORT", "8081"))
AUTO_OPEN_BROWSER = os.environ.get("WEBAPP_AUTO_OPEN", "1") != "0"
STREAM_PERIOD = 5

# client.candles (MemoryCandleStorage) defaults to keeping 10,000 raw price updates
# per asset, forever, with no ceiling on the number of assets - fine for a handful of
# assets, but with WEBAPP_ALWAYS_ON_ASSETS=all (167+ assets) that's a per-process
# ceiling of 1.6M+ pydantic objects, which is what was behind the RSS climbing ~8MB/min
# with no plateau in a 30-minute run. 500 points/asset is enough for both /api/tick
# ("closest buffered point to a timestamp") and seeding a new WS subscriber's chart
# with recent history (see ws_handler below) - see set_max_len() call below.
CANDLE_HISTORY_SIZE = int(os.environ.get("WEBAPP_CANDLE_HISTORY_SIZE", "500"))

# "memory" (default) keeps ticks in the process only - fast, zero setup, but every
# restart loses the buffer (this happens a lot during development/deploys). "redis"
# persists them instead - same bounded-per-asset-history shape, just durable. Needs
# a reachable Redis (REDIS_URL, default redis://localhost:6379/0) and the 'redis'
# extra installed (`pip install pocket-option[redis]`).
CANDLE_STORAGE_BACKEND = os.environ.get("WEBAPP_CANDLE_STORAGE", "memory").strip().lower()
if CANDLE_STORAGE_BACKEND not in ("memory", "redis"):
    raise SystemExit(f"Invalid WEBAPP_CANDLE_STORAGE={CANDLE_STORAGE_BACKEND!r}, use 'memory' or 'redis'.")

# Assets to keep streaming (and buffering into client.candles) at all times, independent
# of whether any browser/WS client is currently watching them — so the feed stays warm
# for whoever connects next instead of only running while someone's got the UI open.
# Special value "all" keeps every currently-active asset streaming, kept in sync as
# assets go active/inactive (see on_assets_update below) — same set "Watch all active"
# subscribes to in the UI, just automatic instead of requiring someone to click it.
_ALWAYS_ON_RAW = os.environ.get("WEBAPP_ALWAYS_ON_ASSETS", "").strip()
ALWAYS_ON_ALL = _ALWAYS_ON_RAW.lower() in ("all", "*")
if ALWAYS_ON_ALL:
    ALWAYS_ON_ASSETS: set[Asset] = set()
else:
    try:
        ALWAYS_ON_ASSETS = {Asset(sym.strip()) for sym in _ALWAYS_ON_RAW.split(",") if sym.strip()}
    except ValueError as exc:
        raise SystemExit(
            f"Invalid WEBAPP_ALWAYS_ON_ASSETS: {exc}. Use symbols from GET /api/assets, "
            f"comma-separated, or 'all'.",
        ) from None

# Assets that must come back online with minimal latency after a reconnect - e.g. the
# specific instrument(s) actually being traded. See on_success_auth: with a large
# WEBAPP_ALWAYS_ON_ASSETS set, the bulk resubscribe loop is deliberately paced (30ms/
# asset) to avoid overwhelming the server, which otherwise silently drops a chunk of
# them - but that pacing means an asset near the end of a 167-asset list can wait
# several seconds to resubscribe. Priority assets skip the pacing entirely and are
# resubscribed first. Keep this list short - it bypasses the exact protection the
# pacing exists for, so a large priority list can reproduce the original problem.
_PRIORITY_RAW = os.environ.get("WEBAPP_PRIORITY_ASSETS", "").strip()
try:
    PRIORITY_ASSETS: set[Asset] = {Asset(sym.strip()) for sym in _PRIORITY_RAW.split(",") if sym.strip()}
except ValueError as exc:
    raise SystemExit(
        f"Invalid WEBAPP_PRIORITY_ASSETS: {exc}. Use symbols from GET /api/assets, comma-separated.",
    ) from None

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

# ---------------------------------------------------------------------------
# Admin-managed asset config: per-asset enable/disable + label overrides, and the
# gap-filling display toggle. Persisted to disk (unlike the whitelist/sessions above)
# since losing "which assets are even turned on" on every restart would defeat the
# point of curating them.
# ---------------------------------------------------------------------------

ADMIN_STATE_FILE = pathlib.Path(os.environ.get("WEBAPP_ADMIN_STATE_FILE", "admin_state.json"))

# asset symbol -> {"enabled": bool, "label": str | None}. Absent from this dict means
# "enabled, no label override" - the default, so existing deployments aren't suddenly
# empty until someone visits the admin panel.
asset_overrides: dict[str, dict] = {}
fake_data_enabled = False


def load_admin_state() -> None:
    global fake_data_enabled
    if not ADMIN_STATE_FILE.exists():
        return
    try:
        data = json.loads(ADMIN_STATE_FILE.read_text())
        asset_overrides.update(data.get("assetOverrides", {}))
        fake_data_enabled = bool(data.get("fakeDataEnabled", False))
    except Exception:
        logger.exception("Failed to load admin state from %s", ADMIN_STATE_FILE)


def save_admin_state() -> None:
    try:
        ADMIN_STATE_FILE.write_text(
            json.dumps({"assetOverrides": asset_overrides, "fakeDataEnabled": fake_data_enabled}, indent=2),
        )
    except Exception:
        logger.exception("Failed to save admin state to %s", ADMIN_STATE_FILE)


load_admin_state()


def is_asset_enabled(asset: Asset) -> bool:
    return asset_overrides.get(asset.value, {}).get("enabled", True)


def asset_label_override(asset: Asset) -> str | None:
    return asset_overrides.get(asset.value, {}).get("label") or None


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

candle_storage_cls: type[CandleStorage] = RedisCandleStorage if CANDLE_STORAGE_BACKEND == "redis" else MemoryCandleStorage
if CANDLE_STORAGE_BACKEND == "redis":
    logger.info("Using Redis-backed candle storage (REDIS_URL=%s)", os.environ.get("REDIS_URL", "redis://localhost:6379/0"))

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
    candle_storage_cls=candle_storage_cls,
)
client.candles.set_max_len(CANDLE_HISTORY_SIZE)

# ws -> set of assets that connection is currently watching
socket_watchlists: dict[web.WebSocketResponse, set[Asset]] = {}
subscriber_counts: dict[Asset, int] = {}

# Monotonic clock, so it's unaffected by system time changes. Reset on every tick and
# on every successful (re)auth - see connection_watchdog() and heartbeat_logger().
last_tick_at = time.monotonic()

# Per-asset version of the above - connection_watchdog() only sees "has *anything*
# ticked recently", which stays fresh as long as at least one asset is still flowing.
# That's blind to a real failure mode we hit: after a reconnect, one whole category of
# assets (real-market/non-OTC) went quiet for 1-2 minutes while everything else kept
# ticking normally - the global clock never would have caught that. See
# per_asset_watchdog() below.
last_tick_by_asset: dict[Asset, float] = {}
last_nudged_at: dict[Asset, float] = {}
PER_ASSET_STALE_AFTER = float(os.environ.get("WEBAPP_PER_ASSET_STALE_AFTER", "45"))
PER_ASSET_NUDGE_COOLDOWN = 300.0  # don't re-nudge the same asset more than once per 5min

# Last known active/inactive state per asset, kept fresh in on_assets_update. Used to
# stop relaying price ticks for an asset once it's known closed - the upstream server
# can keep trickling a few residual ticks after an asset goes inactive, which without
# this looked like "shows closed but still streaming" in the UI.
asset_active_cache: dict[Asset, bool] = {}


def schedule_info(item: UpdateAssetItem) -> dict:
    """Normalizes scheduledUntil into an unambiguous {state, at} pair.

    scheduledUntil is only meaningful as a genuine future timestamp. PocketOption uses
    at least two different sentinel values for "no schedule data" depending on *why*
    there isn't any: 0 for OTC/synthetic assets (they trade continuously, so there's
    never a real close to report) and -1 for a real-market asset that's currently
    closed with no known reopen time yet. Neither is a real timestamp, and they mean
    different things, so callers should use this instead of reading scheduledUntil
    directly.
    """
    has_real_schedule = bool(item.scheduled_until) and item.scheduled_until > time.time()
    if has_real_schedule:
        return {"state": "closes_at" if item.active else "opens_at", "at": item.scheduled_until}
    if item.is_otc:
        return {"state": "continuous", "at": None}
    return {"state": "open_unscheduled" if item.active else "closed_unscheduled", "at": None}


def asset_payload(item: UpdateAssetItem) -> dict:
    return {
        # WS message-envelope discriminator (see ws.onmessage in the frontend below) -
        # keep this the only "type" key: item.type (the asset category, e.g. "stock")
        # used to be assigned to this same dict key, silently winning over "asset"
        # since Python dict literals keep the last duplicate key. That meant every
        # asset-state broadcast over WS was mislabeled and the frontend's `msg.type ===
        # "asset"` check never matched a live update - only the initial REST fetch
        # worked. Asset category now lives under "assetType" instead.
        "type": "asset",
        "asset": item.asset.value,
        "label": asset_label_override(item.asset) or item.label,
        "assetType": item.type,
        "payout": item.payout,
        "isOtc": item.is_otc,
        "active": item.active,
        "expTime": item.exp_time,
        "minExpiration": item.min_expiration,
        # When active, scheduledUntil is when it closes; when inactive, it's when it
        # next opens - see formatCloses() in the frontend below.
        "scheduledUntil": item.scheduled_until,
        "scheduledAt": item.scheduled_at,
        "schedule": schedule_info(item),
    }


@client.on.assets_update
async def on_assets_update(items: list[UpdateAssetItem]):
    for item in items:
        asset_active_cache[item.asset] = item.active
    enabled_items = [item for item in items if is_asset_enabled(item.asset)]
    for item in enabled_items:
        await broadcast_all(asset_payload(item))
    if ALWAYS_ON_ALL:
        newly_active = [item.asset for item in enabled_items if item.active is not False]
        if newly_active:
            await asyncio.gather(*(subscribe(asset) for asset in newly_active), return_exceptions=True)


async def _resubscribe(asset: Asset) -> None:
    try:
        await client.emit.subscribe_to_asset(asset)
        await client.emit.change_asset(ChangeAssetRequest(asset=asset, period=STREAM_PERIOD))
    except Exception:
        logger.exception("Failed to resubscribe to %s after reconnect", asset)


@client.on.disconnect
async def on_upstream_disconnect():
    # No parameter here: the "disconnect" event carries no data, so the framework
    # calls this with zero args (see _Handler.wrapper in client.py - it only passes an
    # arg when new_data is not None). A `(_)` signature here silently crashed on every
    # single disconnect, logged as a rate-limited error, which meant this never ran.
    #
    # Tells connected browsers "this is a real outage, not just a quiet asset" - see
    # the "connection" message in the frontend, which gates the gap-fill display so it
    # only ever activates during an actual disconnect rather than normal per-asset
    # sparsity (a real-market stock quietly ticking every 10-20s is not an outage).
    await broadcast_all({"type": "connection", "connected": False})


@client.on.success_auth
async def on_success_auth(_):
    # Reset the staleness clock here too (not just on ticks): resubscribing below can
    # take a moment, and we don't want the watchdog tripping mid-resubscribe over a
    # window where no ticks have arrived yet simply because we just reconnected.
    global last_tick_at
    last_tick_at = time.monotonic()
    await broadcast_all({"type": "connection", "connected": True})

    # Re-establish live subscriptions after (re)connecting, since the server forgets
    # them across a disconnect/reconnect cycle.
    started_at = time.monotonic()
    pending = list(subscriber_counts)
    priority = [a for a in pending if a in PRIORITY_ASSETS]
    rest = [a for a in pending if a not in PRIORITY_ASSETS]

    if priority:
        # No pacing here - these need to come back online as fast as possible. Safe as
        # long as the list stays short (see PRIORITY_ASSETS above).
        await asyncio.gather(*(_resubscribe(a) for a in priority))
        logger.info(
            "Priority-resubscribed %d asset(s) in %.0fms: %s",
            len(priority),
            (time.monotonic() - started_at) * 1000,
            ", ".join(a.value for a in priority),
        )

    # The rest are paced with a small delay per asset - with WEBAPP_ALWAYS_ON_ASSETS=all
    # this is up to 167 assets x 2 emits each, and firing all ~334 back-to-back right as
    # the connection comes back up appears to cause the server to silently drop/delay a
    # subset of them: observed as one whole category of assets (real-market, non-OTC
    # symbols specifically) going quiet in lockstep for a minute or more after a
    # reconnect while everything else (OTC) kept ticking normally on the same
    # connection - i.e. not a dead connection, a lost subscription.
    for asset in rest:
        await _resubscribe(asset)
        await asyncio.sleep(0.03)

    logger.info(
        "Resubscribed %d asset(s) (%d priority, %d paced) in %.2fs",
        len(pending),
        len(priority),
        len(rest),
        time.monotonic() - started_at,
    )


@client.on.update_close_value
async def on_update_close_value(items: list[UpdateCloseValueItem]):
    global last_tick_at
    last_tick_at = time.monotonic()
    for item in items:
        last_tick_by_asset[item.asset] = last_tick_at
        if asset_active_cache.get(item.asset) is False:
            continue  # closed - residual upstream ticks shouldn't render as live movement
        tick = {"value": str(item.value), "timestamp": item.timestamp}
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
    if not is_asset_enabled(asset):
        # Single choke point for every subscribe path (WS clients, always-on, admin
        # re-enable races) - an admin-disabled asset never streams, even if something
        # still asks for it.
        return
    subscriber_counts[asset] = subscriber_counts.get(asset, 0) + 1
    if subscriber_counts[asset] != 1:
        return
    try:
        await client.wait_for_authorization(timeout=15)
        await client.emit.subscribe_to_asset(asset)
        await client.emit.change_asset(ChangeAssetRequest(asset=asset, period=STREAM_PERIOD))
        # Give it a fresh grace window before per_asset_watchdog can flag it - without
        # this, every newly-subscribed asset defaults to "never ticked" (age since
        # process start) and would look immediately stale on the watchdog's first pass.
        last_tick_by_asset[asset] = time.monotonic()
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
    return web.json_response([asset_payload(item) for item in items if is_asset_enabled(item.asset)])


async def whoami(request: web.Request) -> web.Response:
    return web.json_response({"ip": get_client_ip(request)})


async def tick_at(request: web.Request) -> web.Response:
    asset_param = request.query.get("asset")
    timestamp_param = request.query.get("timestamp")
    if not asset_param or not timestamp_param:
        raise web.HTTPBadRequest(text="Query params 'asset' and 'timestamp' (unix seconds) are required")
    try:
        asset = Asset(asset_param)
    except ValueError:
        raise web.HTTPBadRequest(text=f"Unknown asset {asset_param!r}. See GET /api/assets.") from None
    if not is_asset_enabled(asset):
        raise web.HTTPNotFound(text=f"{asset.value} is disabled in the admin panel.")
    try:
        target = float(timestamp_param)
    except ValueError:
        raise web.HTTPBadRequest(text="'timestamp' must be a unix timestamp in seconds") from None

    items = list(await client.candles.get_items(asset))
    if not items:
        raise web.HTTPNotFound(
            text=f"No buffered price data for {asset.value} yet (only assets that have streamed "
            f"since this process started have any — see WEBAPP_ALWAYS_ON_ASSETS).",
        )

    closest = min(items, key=lambda item: abs(item.timestamp - target))
    return web.json_response(
        {
            "asset": asset.value,
            "requested_timestamp": target,
            "value": str(closest.value),
            "timestamp": closest.timestamp,
            "delta_seconds": closest.timestamp - target,
        },
    )


async def candles_at(request: web.Request) -> web.Response:
    asset_param = request.query.get("asset")
    if not asset_param:
        raise web.HTTPBadRequest(text="Query param 'asset' is required")
    try:
        asset = Asset(asset_param)
    except ValueError:
        raise web.HTTPBadRequest(text=f"Unknown asset {asset_param!r}. See GET /api/assets.") from None
    if not is_asset_enabled(asset):
        raise web.HTTPNotFound(text=f"{asset.value} is disabled in the admin panel.")
    try:
        timeframe = int(request.query.get("timeframe", "60"))
        count = int(request.query.get("count", "200"))
    except ValueError:
        raise web.HTTPBadRequest(text="'timeframe' and 'count' must be integers") from None

    # Built from the same bounded raw-tick buffer as /api/tick (see CANDLE_HISTORY_SIZE)
    # - depth is limited by how far back that buffer reaches, not by 'count' alone.
    candles = await client.candles.get_candles(asset, timeframe=timeframe, count=count)
    return web.json_response(
        [
            {
                "timestamp": int(c.timestamp.timestamp() * 1000),
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
            }
            for c in candles
        ],
    )


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


@require_admin
async def admin_list_assets(_: web.Request) -> web.Response:
    items = await client.assets.get_assets()
    result = [
        {
            "asset": item.asset.value,
            "originalLabel": item.label,
            "label": asset_label_override(item.asset) or item.label,
            "assetType": item.type,
            "payout": item.payout,
            "active": item.active,
            "enabled": is_asset_enabled(item.asset),
        }
        for item in items
    ]
    return web.json_response({"fakeDataEnabled": fake_data_enabled, "assets": result})


@require_admin
async def admin_set_asset_enabled(request: web.Request) -> web.Response:
    data = await request.json()
    try:
        asset = Asset(str(data.get("asset", "")))
    except ValueError:
        raise web.HTTPBadRequest(text=f"Unknown asset {data.get('asset')!r}") from None
    enabled = bool(data.get("enabled", True))
    asset_overrides.setdefault(asset.value, {})["enabled"] = enabled
    save_admin_state()
    if not enabled:
        # Stop it immediately instead of waiting for the next reconnect cycle.
        subscriber_counts.pop(asset, None)
        try:
            await client.emit.unsubscribe_from_asset(asset)
        except Exception:
            logger.exception("Failed to unsubscribe disabled asset %s", asset.value)
    logger.info("Admin set %s enabled=%s", asset.value, enabled)
    return web.json_response({"ok": True})


@require_admin
async def admin_set_all_assets_enabled(request: web.Request) -> web.Response:
    data = await request.json()
    enabled = bool(data.get("enabled", True))
    items = await client.assets.get_assets()
    for item in items:
        asset_overrides.setdefault(item.asset.value, {})["enabled"] = enabled
    save_admin_state()
    if not enabled:
        for item in items:
            subscriber_counts.pop(item.asset, None)
        await asyncio.gather(
            *(client.emit.unsubscribe_from_asset(item.asset) for item in items),
            return_exceptions=True,
        )
    logger.info("Admin bulk-set %d asset(s) enabled=%s", len(items), enabled)
    return web.json_response({"ok": True, "count": len(items)})


@require_admin
async def admin_set_asset_label(request: web.Request) -> web.Response:
    data = await request.json()
    try:
        asset = Asset(str(data.get("asset", "")))
    except ValueError:
        raise web.HTTPBadRequest(text=f"Unknown asset {data.get('asset')!r}") from None
    label = str(data.get("label", "")).strip()
    override = asset_overrides.setdefault(asset.value, {})
    if label:
        override["label"] = label
    else:
        override.pop("label", None)  # empty label clears the override
    save_admin_state()
    # Broadcast immediately so any open dashboard picks up the rename live, not just
    # on next reconnect.
    items = await client.assets.get_assets()
    match = next((item for item in items if item.asset == asset), None)
    if match and is_asset_enabled(asset):
        await broadcast_all(asset_payload(match))
    return web.json_response({"ok": True})


@require_admin
async def admin_set_fake_data(request: web.Request) -> web.Response:
    global fake_data_enabled
    data = await request.json()
    fake_data_enabled = bool(data.get("enabled", False))
    save_admin_state()
    await broadcast_all({"type": "config", "fakeDataEnabled": fake_data_enabled})
    logger.info("Admin set fakeDataEnabled=%s", fake_data_enabled)
    return web.json_response({"ok": True, "enabled": fake_data_enabled})


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    watched: set[Asset] = set()
    socket_watchlists[ws] = watched

    # Push the full current asset list (open/closed state, payout, schedule) over the
    # socket itself on every (re)connect, so clients don't have to also poll /api/assets
    # to know asset state - and stay current afterwards via the per-item "asset"
    # broadcasts in on_assets_update above whenever anything changes upstream.
    try:
        asset_items = await client.assets.get_assets()
        payloads = [asset_payload(item) for item in asset_items if is_asset_enabled(item.asset)]
        await ws.send_json({"type": "assets", "assets": payloads})
        await ws.send_json({"type": "config", "fakeDataEnabled": fake_data_enabled})
        await ws.send_json({"type": "connection", "connected": client.is_authorized})
    except Exception:
        logger.exception("Failed to send initial asset list to new WS client")

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
                if not is_asset_enabled(asset):
                    continue  # admin-disabled - not available via this API at all
                watched.add(asset)
                # Send the buffered history immediately (it's already local, up to
                # CANDLE_HISTORY_SIZE points, oldest first); the upstream (re)subscribe
                # below may take longer if reconnecting.
                history_items = await client.candles.get_items(asset, count=CANDLE_HISTORY_SIZE)
                ticks = [{"value": str(item.value), "timestamp": item.timestamp} for item in history_items]
                await ws.send_json({"type": "history", "asset": asset.value, "ticks": ticks})
                await subscribe(asset)
            elif data.get("action") == "unsubscribe" and asset in watched:
                watched.discard(asset)
                await unsubscribe(asset)
    except ConnectionResetError:
        # The browser's end closed mid-write (a reload, a closed tab, a network blip) -
        # same race broadcast_all/broadcast_price already handle elsewhere. Nothing to
        # do but let the finally block below clean up; this is routine, not an error.
        pass
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


async def subscribe_always_on():
    for asset in ALWAYS_ON_ASSETS:
        await subscribe(asset)


last_watchdog_kick_at = 0.0
WATCHDOG_KICK_COOLDOWN = 30.0  # don't re-kick more often than this - give each kick a chance to work


async def connection_watchdog():
    """Forces recovery if ticks stop arriving despite an active subscription - covers
    two different stuck states, since they need different fixes:

    1. Connected but silently wedged (client.is_authorized is True, no data ever
       arrives) - garbled framing during its own reconnect handshake, visible in the
       logs as "Ignoring malformed 'updateStream' payload" or a "Task exception was
       never retrieved" ValueError from _handle_eio_message. Fixed by
       client.disconnect(), which the run() loop in start_client notices and
       reconnects from a clean slate.

    2. Not connected at all for a long stretch (client.is_authorized is False and
       stays False) - observed once for 6.5+ minutes straight with nothing in the
       logs to explain it: the library's own internal reconnect task appears to get
       stuck, and critically, our run() loop never notices because it's blocked
       inside client.wait() the whole time (that only returns once reconnection is
       exhausted or a disconnect() is issued) - so nothing was watching for this case
       at all before. Fixed by client.shutdown(), which aborts a stuck reconnect task
       specifically (unlike disconnect(), which only acts when already connected) and
       lets client.wait() return so run() retries fresh.

    Either kick is rate-limited (WATCHDOG_KICK_COOLDOWN) so a kick gets a real chance
    to work before we'd consider kicking again, rather than fighting a reconnect
    that's already legitimately in progress.
    """
    global last_watchdog_kick_at
    while True:
        await asyncio.sleep(10)
        if not subscriber_counts:
            continue  # nothing subscribed yet, so no ticks are expected either
        age = time.monotonic() - last_tick_at
        if age <= STALE_AFTER:
            continue
        if time.monotonic() - last_watchdog_kick_at < WATCHDOG_KICK_COOLDOWN:
            continue
        last_watchdog_kick_at = time.monotonic()
        if client.is_authorized:
            logger.warning(
                "No price ticks for %.0fs while subscribed to %d asset(s); forcing reconnect",
                age,
                len(subscriber_counts),
            )
            try:
                await client.disconnect()
            except Exception:
                logger.exception("connection_watchdog: error forcing reconnect")
        else:
            logger.warning(
                "Still disconnected after %.0fs while subscribed to %d asset(s); aborting stuck reconnect attempt",
                age,
                len(subscriber_counts),
            )
            try:
                await client.shutdown()
            except Exception:
                logger.exception("connection_watchdog: error aborting stuck reconnect")


async def per_asset_watchdog():
    """Re-nudges the subscription for any individual asset that's gone quiet, even
    while other assets are ticking fine (so connection_watchdog's global "has anything
    ticked" check stays green and never fires) - see the on_success_auth resubscribe
    comment above for the failure mode this covers: a reconnect's resubscribe burst
    appears to get partially dropped server-side, most visibly for real-market (non-OTC)
    symbols specifically, leaving them silently unsubscribed for a minute or more while
    OTC assets on the same connection keep streaming normally.
    """
    while True:
        await asyncio.sleep(PER_ASSET_STALE_AFTER)
        if not client.is_authorized:
            continue
        now = time.monotonic()
        for asset in list(subscriber_counts):
            last_tick = last_tick_by_asset.get(asset, 0.0)
            if now - last_tick < PER_ASSET_STALE_AFTER:
                continue
            if now - last_nudged_at.get(asset, 0.0) < PER_ASSET_NUDGE_COOLDOWN:
                continue  # already nudged recently - avoid hammering a genuinely closed asset
            last_nudged_at[asset] = now
            logger.warning(
                "%s has had no ticks for %.0fs while other assets kept flowing; re-nudging subscription",
                asset.value,
                now - last_tick,
            )
            try:
                await client.emit.subscribe_to_asset(asset)
                await client.emit.change_asset(ChangeAssetRequest(asset=asset, period=STREAM_PERIOD))
            except Exception:
                logger.exception("per_asset_watchdog: failed to re-nudge %s", asset.value)


async def heartbeat_logger():
    """Periodic proof-of-life line, so a stuck stream (age keeps growing) is visible
    in the logs and distinguishable from a real crash (process/log stops entirely)."""
    while True:
        await asyncio.sleep(20)
        logger.info(
            "heartbeat: authorized=%s subscribed_assets=%d last_tick_age=%.0fs",
            client.is_authorized,
            len(subscriber_counts),
            time.monotonic() - last_tick_at,
        )


async def start_client(app: web.Application):
    async def run():
        while True:
            try:
                await client.connect(Regions.DEMO)
                await client.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Upstream PocketOption connection failed; retrying in 5s")
            await asyncio.sleep(5)

    app["client_task"] = asyncio.create_task(run())
    asyncio.create_task(connection_watchdog())  # noqa: RUF006
    asyncio.create_task(per_asset_watchdog())  # noqa: RUF006
    asyncio.create_task(heartbeat_logger())  # noqa: RUF006
    if ALWAYS_ON_ASSETS:
        asyncio.create_task(subscribe_always_on())  # noqa: RUF006
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
    app.router.add_get("/api/tick", tick_at)
    app.router.add_get("/api/candles", candles_at)
    app.router.add_get("/ws", ws_handler)

    app.router.add_get("/admin", admin_page)
    app.router.add_post("/api/admin/login", admin_login)
    app.router.add_post("/api/admin/logout", admin_logout)
    app.router.add_get("/api/admin/whitelist", admin_get_whitelist)
    app.router.add_post("/api/admin/whitelist/add", admin_add_whitelist)
    app.router.add_post("/api/admin/whitelist/remove", admin_remove_whitelist)
    app.router.add_get("/api/admin/assets", admin_list_assets)
    app.router.add_post("/api/admin/assets/enabled", admin_set_asset_enabled)
    app.router.add_post("/api/admin/assets/enabled-bulk", admin_set_all_assets_enabled)
    app.router.add_post("/api/admin/assets/label", admin_set_asset_label)
    app.router.add_post("/api/admin/fake-data", admin_set_fake_data)

    app.on_startup.append(start_client)
    app.on_cleanup.append(stop_client)
    return app


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PocketOption Live Prices</title>
<script src="https://cdn.jsdelivr.net/npm/klinecharts@9.8.12/dist/umd/klinecharts.min.js"></script>
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
  #watch-body tr.inactive { opacity: 0.5; }
  .closes { color: #9aa1ac; }
  .closes.soon { color: #e0b060; }
  .closes.closed { color: #e08080; }
  .closes.opens { color: #6f9fd9; }
  table { border-collapse: collapse; }
  th, td { text-align: left; padding: 0.6rem 1rem; font-size: 0.85rem; border-bottom: 1px solid #1c2029; }
  th { color: #9aa1ac; font-weight: 500; position: sticky; top: 0; background: #161922; }
  tbody tr:hover { background: #1c2029; }
  .price-up { color: #6fd98a; }
  .price-down { color: #e08080; }
  /* Estimated (gap-fill) price: italic + a subtle tilde, deliberately still visually
     distinct from a real tick even though the number itself moves plausibly - see
     tickGapFill() in the frontend and schedule_info-style comments in the backend. */
  .estimated { color: #c9a86a; font-style: italic; }
  .estimated::after { content: " ~"; opacity: 0.7; }
  .remove-btn { cursor: pointer; color: #9aa1ac; }
  .remove-btn:hover { color: #e08080; }
  #watch-body-wrap { overflow-y: auto; flex: 1; }
  .empty { padding: 1rem; color: #6b7280; font-size: 0.85rem; }
  .sparkline { display: block; }
  .chart-btn { cursor: pointer; color: #9aa1ac; }
  .chart-btn:hover { color: #e6e6e6; }
  #chart-modal { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; z-index: 100; }
  #chart-modal[hidden] { display: none; }
  #chart-box { width: min(1100px, 94vw); height: min(720px, 90vh); background: #161922; border: 1px solid #262a33; border-radius: 0.5rem; display: flex; flex-direction: column; overflow: hidden; }
  #chart-header { padding: 0.75rem 1rem; border-bottom: 1px solid #262a33; display: flex; align-items: center; gap: 0.75rem; }
  #chart-title { font-size: 0.95rem; font-weight: 600; }
  #chart-timeframes { display: flex; gap: 0.35rem; margin-left: auto; }
  #chart-timeframes button { padding: 0.3rem 0.6rem; font-size: 0.78rem; background: #1c2029; border: 1px solid #262a33; border-radius: 0.3rem; color: #9aa1ac; cursor: pointer; }
  #chart-timeframes button.active { background: #234a2f; border-color: #2f6b45; color: #6fd98a; }
  #chart-close { cursor: pointer; color: #9aa1ac; background: none; border: none; font-size: 1.1rem; padding: 0.2rem 0.4rem; }
  #chart-close:hover { color: #e08080; }
  #chart-container { flex: 1; min-height: 0; }
  #chart-note { padding: 0.4rem 1rem; font-size: 0.75rem; color: #6b7280; border-top: 1px solid #262a33; }
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
        <thead><tr><th>Asset</th><th>Label</th><th>Payout</th><th>Price</th><th>Trend</th><th>Updated</th><th>Schedule</th><th></th><th></th></tr></thead>
        <tbody id="watch-body"></tbody>
      </table>
      <div class="empty" id="watch-empty">Click an asset on the left to start streaming its price.</div>
    </div>
  </section>
</main>

<div id="chart-modal" hidden>
  <div id="chart-box">
    <div id="chart-header">
      <span id="chart-title">-</span>
      <div id="chart-timeframes">
        <button data-tf="1">1s</button>
        <button data-tf="30">30s</button>
        <button data-tf="60">1m</button>
        <button data-tf="300">5m</button>
        <button data-tf="900">15m</button>
      </div>
      <button id="chart-close" type="button">✕</button>
    </div>
    <div id="chart-container"></div>
    <div id="chart-note">
      Built from this asset's buffered ticks (up to ~a few hours for slow-moving assets, less for
      fast-ticking ones) - live candle updates from here on while open.
    </div>
  </div>
</div>
<script>
const assets = new Map();
const watched = new Map();
const WATCHLIST_KEY = "po_watched_assets";
let ws;
// True once the backend's own upstream (PocketOption) connection is confirmed live -
// see the "connection" WS message. This, not a per-row timer, is what gates the
// gap-fill display below: a real-market stock quietly ticking every 10-20s is normal
// and must never be "filled" - only an actual outage should be.
let upstreamConnected = true;
let fakeDataEnabled = false;
// Timestamp of the most recent reconnect (connection: true after being false). Assets
// don't all resume ticking the instant this fires - see onConnectionState/tickGapFill.
let reconnectedAt = 0;
const RESUBSCRIBE_GRACE_MS = 20000; // covers the ~5-6s paced bulk resubscribe plus slack

// Fallback for when the "connection" event just doesn't fire - observed for real: a
// "packet queue is empty" disconnect left the backend's client.is_authorized stuck at
// True for the entire ~47s gap (confirmed in server logs), so on_upstream_disconnect
// never ran and upstreamConnected never flipped, even though nothing was ticking.
// Rather than depend on that event alone, also track time since ANY asset's last real
// tick - if it's been quiet platform-wide for GLOBAL_STALE_MS, assume an outage
// regardless of what the connection flag last said.
let lastAnyTickAt = Date.now();
const GLOBAL_STALE_MS = 20000;

// Chart modal state.
let chartInstance = null;
let chartAsset = null;
let chartTimeframe = 60;
let chartCurrentBar = null; // the currently-forming bar, updated tick-by-tick from onPrice

function saveWatchlist() {
  try {
    localStorage.setItem(WATCHLIST_KEY, JSON.stringify([...watched.keys()]));
  } catch {
    // localStorage can throw (private browsing, disabled storage, quota) - watchlist
    // persistence is a convenience, not something worth surfacing an error over.
  }
}

function loadWatchlist() {
  let saved = [];
  try {
    saved = JSON.parse(localStorage.getItem(WATCHLIST_KEY) || "[]");
  } catch {
    saved = [];
  }
  for (const asset of saved) {
    if (!watched.has(asset)) {
      watched.set(asset, { label: asset, payout: "-", active: null, isOtc: null, expTime: null, schedule: null, value: null, prevValue: null, timestamp: null, history: [] });
    }
  }
}

function connect() {
  const wsProtocol = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${wsProtocol}//${location.host}/ws`);
  const status = document.getElementById("status");
  ws.onopen = () => {
    status.textContent = "connected";
    status.classList.add("connected");
    // The server starts every new WS connection with an empty subscription set, so
    // anything we were already watching before this reconnect needs to be re-sent -
    // otherwise those rows just silently stop updating forever after any drop (a
    // backgrounded tab missing the heartbeat ping, a brief network blip, etc). This
    // also covers the very first connect, since loadWatchlist() (called before the
    // initial connect() below) already seeds `watched` from localStorage.
    for (const asset of watched.keys()) {
      ws.send(JSON.stringify({ action: "subscribe", asset }));
    }
  };
  ws.onclose = () => { status.textContent = "reconnecting…"; status.classList.remove("connected"); setTimeout(connect, 1500); };
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "assets") onAssetsList(msg);
    if (msg.type === "asset") onAsset(msg);
    if (msg.type === "history") onHistory(msg);
    if (msg.type === "price") onPrice(msg);
    if (msg.type === "connection") onConnectionState(msg);
    if (msg.type === "config") onConfig(msg);
  };
}

function onConnectionState(msg) {
  const wasConnected = upstreamConnected;
  upstreamConnected = !!msg.connected;
  if (upstreamConnected && !wasConnected) {
    // auth succeeded, but individual assets don't actually resume ticking the
    // instant this fires - the backend re-subscribes in a paced loop that can take
    // several seconds for 100+ always-on assets (see on_success_auth), and a given
    // asset's first real tick can lag further still. Snapping every row's fill off
    // right here (old behavior) meant the display flashed back to a stale, unstyled
    // price for that whole window. Instead, start a grace period and let each row's
    // own onPrice() clear its fill the moment ITS real tick actually shows up -
    // tickGapFill() below keeps filling anything still waiting until then.
    reconnectedAt = Date.now();
  }
}

function onConfig(msg) {
  if (typeof msg.fakeDataEnabled === "boolean") fakeDataEnabled = msg.fakeDataEnabled;
}

function applyAssetMeta(asset, meta) {
  assets.set(asset, meta);
  if (watched.has(asset)) {
    const row = watched.get(asset);
    row.label = meta.label;
    row.payout = meta.payout;
    row.active = meta.active;
    row.isOtc = meta.isOtc;
    row.expTime = meta.expTime;
    row.schedule = meta.schedule;
    renderWatchRow(asset);
  }
}

// Sent once per WS (re)connect with every known asset - see ws_handler in the backend.
function onAssetsList(msg) {
  msg.assets.forEach((meta) => applyAssetMeta(meta.asset, meta));
  renderAssetList();
}

// Sent whenever a single asset's state changes (payout, active/closed, schedule).
function onAsset(msg) {
  applyAssetMeta(msg.asset, msg);
  renderAssetList();
}

function onHistory(msg) {
  const row = watched.get(msg.asset);
  if (!row) return;
  row.history = msg.ticks.map((t) => Number(t.value));
  if (msg.ticks.length) {
    const last = msg.ticks[msg.ticks.length - 1];
    row.value = last.value;
    row.timestamp = last.timestamp;
    row.lastRealTickAt = Date.now();
  }
  renderWatchRow(msg.asset);
}

function onPrice(msg) {
  const row = watched.get(msg.asset);
  if (!row) return;
  row.prevValue = row.value;
  row.value = msg.value;
  row.timestamp = msg.timestamp;
  row.lastRealTickAt = Date.now();
  lastAnyTickAt = row.lastRealTickAt;
  row.history.push(Number(msg.value));
  if (row.history.length > 50) row.history.shift();
  // A real tick always wins - drop whatever the gap-filler had made up.
  row.filling = false;
  row.fillHistory = [];
  renderWatchRow(msg.asset);
  if (msg.asset === chartAsset && chartInstance) updateChartWithPrice(msg.value, msg.timestamp);
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
    const meta = assets.get(asset) || { label: asset, payout: "-", active: null, isOtc: null, expTime: null, schedule: null };
    watched.set(asset, { label: meta.label, payout: meta.payout, active: meta.active, isOtc: meta.isOtc, expTime: meta.expTime, schedule: meta.schedule, value: null, prevValue: null, timestamp: null, history: [] });
    ws.send(JSON.stringify({ action: "subscribe", asset }));
    renderWatchRow(asset);
  }
  saveWatchlist();
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

function formatDuration(seconds) {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
}

// The backend normalizes the raw scheduledUntil/scheduledAt sentinel mess (see
// schedule_info() in the Python backend for why it's ambiguous on its own) into
// data.schedule = { state, at }. state is one of: "closes_at" / "opens_at" (at is a
// real future timestamp), "continuous" (OTC/synthetic - never closes), or
// "open_unscheduled" / "closed_unscheduled" (real-market asset with no schedule data
// available right now). We just render whichever state we got.
function formatCloses(data) {
  const now = Date.now() / 1000;
  const sched = data.schedule;
  if (!sched) return { text: "—", cls: "" };
  switch (sched.state) {
    case "closes_at":
      { const secondsLeft = sched.at - now;
        return { text: `in ${formatDuration(secondsLeft)}`, cls: secondsLeft < 900 ? "soon" : "" }; }
    case "opens_at":
      return { text: `opens in ${formatDuration(sched.at - now)}`, cls: "opens" };
    case "continuous":
      return { text: "—", cls: "" };
    case "closed_unscheduled":
      return { text: "closed", cls: "closed" };
    default: // "open_unscheduled" - fall back to trade expiration as a rough proxy
      if (data.expTime && data.expTime > now) {
        const secondsLeft = data.expTime - now;
        return { text: `in ${formatDuration(secondsLeft)}`, cls: secondsLeft < 900 ? "soon" : "" };
      }
      return { text: "—", cls: "" };
  }
}

function agoText(ts) {
  if (!ts) return "-";
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s ago`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m ago`;
}

// fillValues (optional): estimated points continuing on from the last real one, drawn
// dashed so the chart keeps moving during an outage without looking like real ticks.
function sparkline(values, fillValues) {
  const real = values || [];
  const fill = fillValues && fillValues.length ? fillValues : [];
  const all = fill.length ? real.concat(fill) : real;
  if (all.length < 2) return "";
  const w = 100, h = 26, pad = 2;
  const min = Math.min(...all), max = Math.max(...all);
  const span = max - min || 1;
  const xAt = (i) => pad + (i / (all.length - 1)) * (w - pad * 2);
  const yAt = (v) => h - pad - ((v - min) / span) * (h - pad * 2);
  const color = all[all.length - 1] >= all[0] ? "#6fd98a" : "#e08080";
  let svg = `<svg class="sparkline" width="${w}" height="${h}">`;
  if (real.length >= 2) {
    const realPoints = real.map((v, i) => `${xAt(i).toFixed(1)},${yAt(v).toFixed(1)}`).join(" ");
    svg += `<polyline points="${realPoints}" fill="none" stroke="${color}" stroke-width="1.5"/>`;
  }
  if (fill.length && real.length >= 1) {
    const anchor = `${xAt(real.length - 1).toFixed(1)},${yAt(real[real.length - 1]).toFixed(1)}`;
    const fillPoints = fill.map((v, i) => `${xAt(real.length + i).toFixed(1)},${yAt(v).toFixed(1)}`);
    svg += `<polyline points="${[anchor, ...fillPoints].join(" ")}" fill="none" stroke="${color}" stroke-width="1.5" stroke-dasharray="2,3" opacity="0.6"/>`;
  }
  svg += `</svg>`;
  return svg;
}

function formatEstimated(value, referenceStr) {
  const decimals = (String(referenceStr).split(".")[1] || "").length || 2;
  return value.toFixed(decimals);
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
  row.classList.toggle("inactive", data.active === false);

  const fillValue = data.filling && data.fillHistory && data.fillHistory.length
    ? data.fillHistory[data.fillHistory.length - 1]
    : null;
  const displayValue = fillValue != null ? formatEstimated(fillValue, data.value) : (data.value ?? "…");
  const priceCls = fillValue != null
    ? "estimated"
    : (data.prevValue != null ? (Number(data.value) > Number(data.prevValue) ? "price-up" : Number(data.value) < Number(data.prevValue) ? "price-down" : "") : "");

  const closes = formatCloses(data);
  row.innerHTML = `
    <td>${asset}</td>
    <td>${data.label}</td>
    <td>${data.payout}%</td>
    <td class="${priceCls}">${displayValue}</td>
    <td>${sparkline(data.history, fillValue != null ? data.fillHistory : null)}</td>
    <td class="ago" data-ts="${data.timestamp ?? ""}">${agoText(data.timestamp)}</td>
    <td class="closes ${closes.cls}">${closes.text}</td>
    <td class="chart-btn" onclick="openChart('${asset}')" title="View chart">📈</td>
    <td class="remove-btn" onclick="toggleWatch('${asset}')">✕</td>
  `;
  updateEmptyState();
}

function updateEmptyState() {
  document.getElementById("watch-empty").style.display = watched.size ? "none" : "block";
}

// ---------------------------------------------------------------------------
// Chart modal (klinecharts) - large candlestick view for one asset at a time.
// ---------------------------------------------------------------------------

async function openChart(asset) {
  chartAsset = asset;
  chartCurrentBar = null;
  const meta = assets.get(asset);
  document.getElementById("chart-title").textContent = meta ? `${meta.label} (${asset})` : asset;
  document.querySelectorAll("#chart-timeframes button").forEach((b) => {
    b.classList.toggle("active", Number(b.dataset.tf) === chartTimeframe);
  });
  document.getElementById("chart-modal").hidden = false;
  if (!chartInstance) {
    chartInstance = klinecharts.init("chart-container", {
      styles: {
        grid: { horizontal: { color: "#1c2029" }, vertical: { color: "#1c2029" } },
        candle: {
          type: "area",
          area: {
            lineColor: "#6fd98a",
            lineSize: 2,
            value: "close",
            smooth: true,
            backgroundColor: [
              { offset: 0, color: "rgba(111, 217, 138, 0.28)" },
              { offset: 1, color: "rgba(111, 217, 138, 0)" },
            ],
          },
        },
        xAxis: { axisLine: { color: "#262a33" }, tickText: { color: "#9aa1ac" } },
        yAxis: { axisLine: { color: "#262a33" }, tickText: { color: "#9aa1ac" } },
      },
    });
  }
  await loadChartData();
  // The container may have been hidden (0 size) until just now, so force a resize
  // once the browser's actually laid it out.
  requestAnimationFrame(() => { if (chartInstance) chartInstance.resize(); });
}

async function loadChartData() {
  if (!chartAsset || !chartInstance) return;
  try {
    const resp = await fetch(`/api/candles?asset=${encodeURIComponent(chartAsset)}&timeframe=${chartTimeframe}&count=300`);
    const data = await resp.json();
    const bars = Array.isArray(data) ? data : [];
    chartInstance.applyNewData(bars);
    chartCurrentBar = bars.length ? { ...bars[bars.length - 1] } : null;
    // Center the loaded bars in the viewport instead of leaving them pinned to the
    // right edge with a big empty gap - most noticeable when there's only a handful
    // of bars (a freshly-opened chart, or a short timeframe with little history yet).
    if (bars.length) chartInstance.scrollToDataIndex(Math.floor(bars.length / 2), 0);
  } catch {
    // leave whatever was already shown - a failed refresh shouldn't blank the chart
  }
}

function updateChartWithPrice(value, timestampSeconds) {
  if (!chartInstance) return;
  const bucketMs = Math.floor(timestampSeconds / chartTimeframe) * chartTimeframe * 1000;
  const v = Number(value);
  if (!chartCurrentBar || chartCurrentBar.timestamp !== bucketMs) {
    chartCurrentBar = { timestamp: bucketMs, open: v, high: v, low: v, close: v };
  } else {
    chartCurrentBar.high = Math.max(chartCurrentBar.high, v);
    chartCurrentBar.low = Math.min(chartCurrentBar.low, v);
    chartCurrentBar.close = v;
  }
  chartInstance.updateData({ ...chartCurrentBar });
}

function closeChart() {
  document.getElementById("chart-modal").hidden = true;
  if (chartInstance) {
    klinecharts.dispose("chart-container");
    chartInstance = null;
  }
  chartAsset = null;
  chartCurrentBar = null;
}

document.getElementById("chart-close").addEventListener("click", closeChart);
document.getElementById("chart-modal").addEventListener("click", (e) => {
  if (e.target.id === "chart-modal") closeChart();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !document.getElementById("chart-modal").hidden) closeChart();
});
document.querySelectorAll("#chart-timeframes button").forEach((btn) => {
  btn.addEventListener("click", () => {
    chartTimeframe = Number(btn.dataset.tf);
    document.querySelectorAll("#chart-timeframes button").forEach((b) => b.classList.toggle("active", b === btn));
    loadChartData();
  });
});

// Admin-toggleable, and only ever active during a confirmed real outage (see
// upstreamConnected, set from the "connection" WS message) - never for an asset that's
// just naturally quiet, since that's real market behavior, not something to paper over.
// Values are a small bounded random walk seeded from the last real price, purely for
// display: never sent anywhere, never touches candle storage or any data API, and gets
// discarded the instant a real tick (or reconnect) arrives. Rendered with a distinct
// "estimated" style (see .estimated CSS) rather than looking identical to a real tick.
function tickGapFill() {
  const inResubscribeGrace = upstreamConnected && (Date.now() - reconnectedAt < RESUBSCRIBE_GRACE_MS);
  // Fallback for when the "connection" event just doesn't fire at all - confirmed for
  // real, see GLOBAL_STALE_MS comment above: don't trust upstreamConnected alone.
  const assumeDown = watched.size > 0 && (Date.now() - lastAnyTickAt > GLOBAL_STALE_MS);
  const anyFillTrigger = fakeDataEnabled && (!upstreamConnected || inResubscribeGrace || assumeDown);
  watched.forEach((row, asset) => {
    // During the post-reconnect grace window a row that already got its own fresh
    // real tick (lastRealTickAt updated at/after this reconnect) must NOT be filled
    // again - only rows still waiting on their first tick since reconnecting are. A
    // full outage or the assumeDown fallback has no such exception: nothing anywhere
    // is getting real ticks in either case, so every row fills.
    const alreadyResumed = upstreamConnected && !assumeDown && row.lastRealTickAt && row.lastRealTickAt >= reconnectedAt;
    const shouldFill = anyFillTrigger && !alreadyResumed && row.active !== false && row.value != null;
    if (!shouldFill) {
      if (row.filling) {
        row.filling = false;
        row.fillHistory = [];
        renderWatchRow(asset);
      }
      return;
    }
    if (!row.filling) {
      row.filling = true;
      row.fillBase = Number(row.value);
      row.fillHistory = [];
    }
    const last = row.fillHistory.length ? row.fillHistory[row.fillHistory.length - 1] : row.fillBase;
    const step = (Math.random() - 0.5) * row.fillBase * 0.0006; // ~0.06%/tick
    const maxDelta = row.fillBase * 0.0025; // keep it near the last real price
    const next = Math.min(row.fillBase + maxDelta, Math.max(row.fillBase - maxDelta, last + step));
    row.fillHistory.push(next);
    if (row.fillHistory.length > 50) row.fillHistory.shift();
    renderWatchRow(asset);
    // Keep the open chart moving too, if it's showing this asset - same estimated
    // value, same "not real data" rule (see updateChartWithPrice/onPrice: any real
    // tick always overwrites whatever this produced).
    if (asset === chartAsset && chartInstance) updateChartWithPrice(next, Date.now() / 1000);
  });
}

document.getElementById("search").addEventListener("input", renderAssetList);
document.getElementById("watch-all-btn").addEventListener("click", watchAllActive);
document.getElementById("unwatch-all-btn").addEventListener("click", unwatchAll);
// Full row re-render every 30s, for the "Schedule" countdown text.
setInterval(() => watched.forEach((_, asset) => renderWatchRow(asset)), 30000);
// Cheap text-only tick for "Updated" every 1s - avoids rebuilding every watched row's
// innerHTML (can be 100+ rows with "Watch all active") just to keep this fresh.
setInterval(() => {
  document.querySelectorAll("#watch-body td.ago").forEach((td) => {
    td.textContent = agoText(td.dataset.ts ? Number(td.dataset.ts) : null);
  });
}, 1000);
setInterval(tickGapFill, 1000);

// Restore whatever was being watched before the last reload so it doesn't have to be
// re-picked by hand every time; connect()'s onopen (re)subscribes to it once the
// socket is up, and the "assets" message on connect fills in real label/payout/
// schedule in place of these placeholders.
loadWatchlist();
watched.forEach((_, asset) => renderWatchRow(asset));
updateEmptyState();

// Also fetched via REST as a fallback/for non-JS-socket consumers; the WS "assets"
// message (see onAssetsList) is what actually keeps this current going forward.
fetch("/api/assets").then((r) => r.json()).then((items) => {
  items.forEach((item) => applyAssetMeta(item.asset, item));
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
  input[type="checkbox"] { width: auto; margin: 0; }
  .toggle-row { display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0; }
  .toggle-row label { margin-bottom: 0; }
  .hint { color: #6b7280; font-size: 0.78rem; margin: 0.4rem 0 0; }
  .table-scroll { max-height: 420px; overflow-y: auto; }
  .label-input { width: 100%; box-sizing: border-box; padding: 0.3rem; margin: 0; background: #0f1115; border: 1px solid #262a33; border-radius: 0.3rem; color: inherit; font-size: 0.8rem; }
  tr.asset-disabled { opacity: 0.4; }
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
    </section>

    <section>
      <h2>Gap-fill display</h2>
      <div class="toggle-row">
        <input type="checkbox" id="fake-data-toggle">
        <label for="fake-data-toggle" style="margin: 0">Estimate price movement during a real connection outage</label>
      </div>
      <p class="hint">
        Only ever activates while the backend's upstream connection is actually down (never for an
        asset that's just naturally quiet) and is always rendered in a visually distinct style
        (italic, "~" suffix, dashed chart line) - it's never sent as a real price or stored anywhere.
      </p>
    </section>

    <section>
      <h2>Assets</h2>
      <div class="row">
        <div>
          <label for="asset-search">Filter</label>
          <input type="text" id="asset-search" placeholder="Search symbol or label…">
        </div>
      </div>
      <div class="row" style="margin-bottom: 0.75rem">
        <button class="secondary" id="enable-all-btn" type="button">Enable all</button>
        <button class="secondary" id="disable-all-btn" type="button">Disable all</button>
      </div>
      <p class="hint" style="margin-bottom: 0.75rem">
        Only enabled assets appear in GET /api/assets and the WS asset list/subscribe - disabling one
        stops it immediately, even if something is already watching it.
      </p>
      <div class="table-scroll">
        <table>
          <thead><tr><th style="width: 40px">On</th><th>Asset</th><th>Label</th><th style="width: 70px">Type</th></tr></thead>
          <tbody id="assets-body"></tbody>
        </table>
      </div>
      <p class="muted" id="assets-empty">Loading…</p>
    </section>

    <section>
      <button class="secondary" id="logout-btn" type="button">Log out</button>
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

let allAssets = [];

async function refreshAssets() {
  const data = await api("/api/admin/assets");
  allAssets = data.assets;
  document.getElementById("fake-data-toggle").checked = data.fakeDataEnabled;
  renderAssets();
}

function renderAssets() {
  const filter = document.getElementById("asset-search").value.trim().toLowerCase();
  const body = document.getElementById("assets-body");
  body.innerHTML = "";
  const filtered = allAssets.filter(
    (a) => a.asset.toLowerCase().includes(filter) || a.label.toLowerCase().includes(filter),
  );
  filtered.forEach((a) => {
    const tr = document.createElement("tr");
    tr.className = a.enabled ? "" : "asset-disabled";
    tr.innerHTML = `
      <td><input type="checkbox" ${a.enabled ? "checked" : ""}></td>
      <td>${a.asset}</td>
      <td><input type="text" class="label-input" value="${a.label.replace(/"/g, "&quot;")}" placeholder="${a.originalLabel}"></td>
      <td>${a.assetType}</td>
    `;
    tr.querySelector('input[type="checkbox"]').onchange = (e) => setAssetEnabled(a.asset, e.target.checked, tr);
    const labelInput = tr.querySelector(".label-input");
    const saveLabel = () => {
      const value = labelInput.value.trim();
      if (value !== (a.label || "")) setAssetLabel(a.asset, value);
    };
    labelInput.addEventListener("blur", saveLabel);
    labelInput.addEventListener("keydown", (e) => { if (e.key === "Enter") labelInput.blur(); });
    body.appendChild(tr);
  });
  document.getElementById("assets-empty").style.display = filtered.length ? "none" : "block";
  if (!filtered.length) document.getElementById("assets-empty").textContent = "No matching assets.";
}

async function setAssetEnabled(asset, enabled, tr) {
  tr.className = enabled ? "" : "asset-disabled";
  const entry = allAssets.find((a) => a.asset === asset);
  if (entry) entry.enabled = enabled;
  try {
    await api("/api/admin/assets/enabled", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asset, enabled }),
    });
  } catch {
    refreshAssets(); // out of sync - resync from the server rather than leave it wrong
  }
}

async function setAssetLabel(asset, label) {
  const entry = allAssets.find((a) => a.asset === asset);
  if (entry) entry.label = label || entry.originalLabel;
  try {
    await api("/api/admin/assets/label", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asset, label }),
    });
  } catch {
    refreshAssets();
  }
}

document.getElementById("asset-search").addEventListener("input", renderAssets);

document.getElementById("enable-all-btn").onclick = async () => {
  await api("/api/admin/assets/enabled-bulk", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: true }),
  });
  refreshAssets();
};

document.getElementById("disable-all-btn").onclick = async () => {
  if (!confirm("Disable every asset? Nothing will be available via the API/WS until you re-enable some.")) return;
  await api("/api/admin/assets/enabled-bulk", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: false }),
  });
  refreshAssets();
};

document.getElementById("fake-data-toggle").onchange = async (e) => {
  await api("/api/admin/fake-data", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: e.target.checked }),
  });
};

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
  refreshAssets();
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

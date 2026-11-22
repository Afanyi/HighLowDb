import collections
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from flask import Flask, abort, render_template, request, redirect, session, url_for

import sync
import crypto_sync
import refresh
from refresh import next_run

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)


def _security_config(environ):
    origin = environ.get("APP_PUBLIC_ORIGIN", "")
    key = environ.get("APP_SECRET_KEY", "")
    hosts = ["localhost", "127.0.0.1", "[::1]"]
    if origin:
        parts = urlsplit(origin)
        if (parts.scheme != "https" or not parts.hostname or parts.netloc != parts.hostname
                or parts.path or parts.query or parts.fragment
                or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", parts.hostname)):
            raise ValueError("APP_PUBLIC_ORIGIN must be an HTTPS hostname without a port, path or credentials")
        hosts.append(parts.hostname)
        if len(key) < 32:
            raise ValueError("APP_SECRET_KEY must contain at least 32 random characters for public access")
    if key and len(key) < 32:
        raise ValueError("APP_SECRET_KEY must contain at least 32 random characters")
    return {
        "PUBLIC_ORIGIN": origin,
        "SECRET_KEY": key or secrets.token_hex(32),
        "TRUSTED_HOSTS": hosts,
        "SESSION_COOKIE_NAME": "__Host-allstocks_session" if origin else "allstocks_session",
        "SESSION_COOKIE_SECURE": bool(origin),
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Strict",
        "MAX_CONTENT_LENGTH": 16 * 1024,
    }


app.config.update(_security_config(os.environ))


@app.before_request
def protect_requests():
    # Flask validates Host against TRUSTED_HOSTS. Forwarded headers are not trusted.
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = app.config["PUBLIC_ORIGIN"] or request.host_url.rstrip("/")
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            abort(403)
        if "Origin" in request.headers and request.headers["Origin"] != origin:
            abort(403)
        if "Referer" in request.headers:
            try:
                ref = urlsplit(request.headers["Referer"])
                valid_ref = f"{ref.scheme}://{ref.netloc}" == origin
            except ValueError:
                valid_ref = False
            if not valid_ref:
                abort(403)
        supplied = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not expected or not secrets.compare_digest(supplied.encode(), expected.encode()):
            abort(403)


@app.after_request
def protect_responses(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


# The table renders ~10k rows; drop template indentation and newlines from the output.
app.jinja_env.trim_blocks = True
app.jinja_env.lstrip_blocks = True

SYNC_TIME_UTC = os.environ.get("SYNC_TIME_UTC", "14:45")
SYNC_HH, SYNC_MM = (int(x) for x in SYNC_TIME_UTC.split(":"))
if not (0 <= SYNC_HH <= 23 and 0 <= SYNC_MM <= 59):
    raise ValueError(f"SYNC_TIME_UTC must be HH:MM, got {SYNC_TIME_UTC!r}")

STATUS_PATTERN = re.compile(r"^(ACTIVE|DELISTED_OR_ZERO|NEW_\d{4})$")

ALLOWED_SORTS = {
    "ticker", "name", "exchange", "instrument_type", "opening_price", "year_max", "year_min",
    "all_time_max", "all_time_min", "status_tag", "last_updated",
}
# Distance columns: "% below highs" and "% above lows" of the current price.
DISTANCES = {
    "pct_year_max":     ("% below Year High",     "(year_max - opening_price) * 100.0 / NULLIF(year_max, 0)"),
    "pct_year_min":     ("% above Year Low",      "(opening_price - year_min) * 100.0 / NULLIF(year_min, 0)"),
    "pct_all_time_max": ("% below All-Time High", "(all_time_max - opening_price) * 100.0 / NULLIF(all_time_max, 0)"),
    "pct_all_time_min": ("% above All-Time Low",  "(opening_price - all_time_min) * 100.0 / NULLIF(all_time_min, 0)"),
}
ALLOWED_SORTS |= set(DISTANCES)

# Fixed grouping ranges: lower inclusive, upper exclusive; NULL is "n/a".
BUCKETS = [(None, 0), (0, 10), (10, 25), (25, 50), (50, 100), (100, None)]
MAX_SORTS = 3

DB_PATH = sync.DB_PATH
CRYPTO_DB_PATH = os.environ.get("CRYPTO_DB_PATH", "crypto.db")

CRYPTO_SYNC_TIME_UTC = os.environ.get("CRYPTO_SYNC_TIME_UTC", "02:00")
CRYPTO_HH, CRYPTO_MM = (int(x) for x in CRYPTO_SYNC_TIME_UTC.split(":"))
if not (0 <= CRYPTO_HH <= 23 and 0 <= CRYPTO_MM <= 59):
    raise ValueError(f"CRYPTO_SYNC_TIME_UTC must be HH:MM, got {CRYPTO_SYNC_TIME_UTC!r}")

STOCK_BASE_COLUMNS = [
    ("ticker", "Ticker"),
    ("name", "Name"),
    ("exchange", "Exchange"),
    ("instrument_type", "Type"),
    ("opening_price", "Open"),
    ("year_max", "Year High"),
    ("year_min", "Year Low"),
    ("all_time_max", "All-Time High"),
    ("all_time_min", "All-Time Low"),
    ("status_tag", "Status"),
    ("last_updated", "Updated"),
]

CRYPTO_BASE_COLUMNS = [
    ("market_cap_rank", "Rank"),
    ("symbol", "Symbol"),
    ("name", "Name"),
    ("opening_price", "Price"),
    ("year_max", "Year High"),
    ("year_min", "Year Low"),
    ("all_time_max", "All-Time High"),
    ("all_time_min", "All-Time Low"),
    ("status_tag", "Status"),
    ("last_updated", "Updated"),
]

MODES = {
    "stocks": {
        "table": "stocks",
        "columns": STOCK_BASE_COLUMNS + [(key, label) for key, (label, _expr) in DISTANCES.items()],
        "db_columns": [col for col, _ in STOCK_BASE_COLUMNS],
        "search": ["ticker", "name"],
        "default_sort": ("ticker", "asc"),
        "sorts": {
            "ticker", "name", "exchange", "instrument_type", "opening_price", "year_max", "year_min",
            "all_time_max", "all_time_min", "status_tag", "last_updated",
        },
    },
    "crypto": {
        "table": "crypto",
        "columns": CRYPTO_BASE_COLUMNS + [(key, label) for key, (label, _expr) in DISTANCES.items()],
        "db_columns": ["ticker"] + [col for col, _ in CRYPTO_BASE_COLUMNS],
        # ticker is the CoinGecko id (e.g. "bitcoin"); people search by symbol ("btc").
        "search": ["ticker", "symbol", "name"],
        # Biggest coins first; unranked coins go last (ORDER BY puts NULLs last).
        "default_sort": ("market_cap_rank", "asc"),
        "sorts": {
            "market_cap_rank", "symbol", "name", "opening_price", "year_max", "year_min",
            "all_time_max", "all_time_min", "status_tag", "last_updated",
        },
    },
}
DEFAULT_MODE = "stocks"


def mode_of(args):
    mode = (args.get("mode") or "").strip()
    return mode if mode in MODES else DEFAULT_MODE


_sync_lock = threading.Lock()
_crypto_lock = threading.Lock()
last_summary = None
last_crypto_summary = None


def _summary_counts(summary):
    """Counts and timestamps only, for the log: lists such as failed_tickers are left out."""
    return ", ".join(f"{k}={v}" for k, v in summary.items() if not isinstance(v, (list, dict)))


def _run_job(mode, source):
    global last_summary, last_crypto_summary
    lock = _sync_lock if mode == "stocks" else _crypto_lock
    label = "stock" if mode == "stocks" else "crypto"
    if not lock.acquire(blocking=False):
        log.info("%s sync skipped: already running", label)
        return False
    try:
        with refresh.job_guard(DB_PATH, mode) as admitted:
            if not admitted:
                log.info("%s sync deferred: deployment or refresh in progress", label)
                return False
            started = datetime.now(timezone.utc)
            if source == "scheduled" and refresh.ready_at(DB_PATH, mode, started) > started:
                return True  # A manual/other worker completed the due slot before admission.
            refresh.started(DB_PATH, mode, source, started)
            log.info("%s sync started (%s)", label, source)
            result, error = {}, None
            try:
                result = (sync.run_sync(db_path=DB_PATH) if mode == "stocks"
                          else crypto_sync.run_sync(db_path=CRYPTO_DB_PATH))
                if not isinstance(result, dict):
                    raise TypeError("Invalid sync summary")
            except Exception as exc:  # noqa: BLE001
                # Provider exception text can contain credentials. Persist/display
                # only the failure type; retain the previous success timestamp.
                result, error = {}, f"Refresh failed ({type(exc).__name__})"
                log.error("%s sync failed (%s)", label, type(exc).__name__)
            finished = datetime.now(timezone.utc)
            refresh.finished(DB_PATH, mode, result, error, finished)
            summary = {k: v for k, v in result.items() if k in refresh.COUNTS and type(v) is int and v >= 0}
            summary.update(started_at=started.isoformat(), finished_at=finished.isoformat())
            if error:
                summary["error"] = error
            if mode == "stocks":
                last_summary = summary
            else:
                last_crypto_summary = summary
            if not error:
                log.info("%s sync finished: %s", label, _summary_counts(summary))
            return True
    finally:
        lock.release()


def run_sync_job(source="manual"):
    return _run_job("stocks", source)


def run_crypto_sync_job(source="manual"):
    return _run_job("crypto", source)


def _scheduler_loop(mode, run_job, state_db_path):
    while True:
        try:
            now = datetime.now(timezone.utc)
            due = refresh.ready_at(state_db_path, mode, now)
            wait = (due - now).total_seconds()
            if wait > 0:
                time.sleep(wait)
                continue  # Re-read: a manual run may have satisfied this slot.
            if not run_job(source="scheduled"):
                time.sleep(1)  # Keep the durable due time while deployment holds the gate.
        except Exception as exc:  # noqa: BLE001
            log.error("%s scheduler unavailable (%s); retrying", mode, type(exc).__name__)
            time.sleep(30)


# Stock syncs are started manually; only crypto refreshes on a schedule.
threading.Thread(target=_scheduler_loop, args=("crypto", run_crypto_sync_job, DB_PATH), daemon=True).start()


def _query_rows(mode=DEFAULT_MODE, status=None, q=None, country=None, instrument_type=None,
                sorts=None, bounds=None):
    cfg = MODES[mode]
    if sorts is None:
        sorts = [cfg["default_sort"]]
    sorts_whitelist = cfg["sorts"] | set(DISTANCES)
    levels = []
    for col, direction in sorts:
        if col not in sorts_whitelist:
            continue
        if direction not in ("asc", "desc"):
            direction = "asc"
        levels.append((col, direction))
    if not levels:
        levels = [cfg["default_sort"]]
    order = ", ".join(f"{col} IS NULL, {col} {direction}" for col, direction in levels)
    order += ", ticker"

    dist_exprs = ", ".join(f"({expr}) AS {key}" for key, (_label, expr) in DISTANCES.items())
    select_cols = ", ".join(cfg["db_columns"])
    sql = f"SELECT * FROM (SELECT {select_cols}, {dist_exprs} FROM {cfg['table']})"
    params = []
    conditions = []
    if status and STATUS_PATTERN.match(status):
        conditions.append("status_tag = ?")
        params.append(status)
    if q:
        # Column names come from the fixed MODES config, never from the request.
        conditions.append("(" + " OR ".join(f"{col} LIKE ?" for col in cfg["search"]) + ")")
        params.extend([f"%{q}%"] * len(cfg["search"]))
    if country and mode == "stocks":
        countries = sync.EXCHANGE_COUNTRIES
        exchanges = [ex for ex, c in countries.items() if c == country]
        if exchanges:
            placeholders = ", ".join("?" for _ in exchanges)
            conditions.append(f"exchange IN ({placeholders})")
            params.extend(exchanges)
    if instrument_type and mode == "stocks" and instrument_type in ("STOCK", "ETF"):
        conditions.append("instrument_type = ?")
        params.append(instrument_type)
    if bounds:
        for key, op, value in bounds:
            if key in DISTANCES and op in (">=", "<="):
                conditions.append(f"{key} {op} ?")
                params.append(value)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += f" ORDER BY {order}"

    db_path = CRYPTO_DB_PATH if mode == "crypto" else DB_PATH
    if mode == "crypto":
        crypto_sync.init_db(db_path)
    else:
        sync.init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return rows


def _parse_pct(value):
    """Parse a percentage bound; None for empty, non-numeric or non-finite input."""
    if not value:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _parse_sorts(args, sorts_whitelist=ALLOWED_SORTS, default=("ticker", "asc")):
    """Parse repeated sort/dir params into up to MAX_SORTS whitelisted (col, dir) levels."""
    sorts = []
    cols = args.getlist("sort")
    dirs = args.getlist("dir")
    for i, col in enumerate(cols):
        if not col or col not in sorts_whitelist:
            continue
        if any(c == col for c, _ in sorts):
            continue
        direction = dirs[i] if i < len(dirs) else "asc"
        if direction not in ("asc", "desc"):
            direction = "asc"
        sorts.append((col, direction))
        if len(sorts) >= MAX_SORTS:
            break
    return sorts or [default]


def _header_sorts(sorts, col):
    """Return the new sort list when a header link for ``col`` is clicked."""
    levels = list(sorts)
    for i, (c, _) in enumerate(levels):
        if c == col:
            if i == 0:
                levels[0] = (col, "desc" if levels[0][1] == "asc" else "asc")
            else:
                direction = levels[i][1]
                levels.pop(i)
                levels.insert(0, (col, direction))
            break
    else:
        levels.insert(0, (col, "asc"))
    seen = set()
    result = []
    for c, d in levels:
        if c not in seen:
            seen.add(c)
            result.append((c, d))
    return result[:MAX_SORTS]


def _bucket_label(lower, upper):
    if lower is None:
        return "< 0%"
    if upper is None:
        return "≥ 100%"
    return f"{lower}–{upper}%"


def _group_rows(rows, key):
    """Split rows (already filtered/sorted) into the fixed percentage ranges."""
    if not key:
        return [(None, rows)]
    groups = []
    for lower, upper in BUCKETS:
        bucket = [
            row for row in rows
            if row[key] is not None
            and (lower is None or row[key] >= lower)
            and (upper is None or row[key] < upper)
        ]
        if bucket:
            groups.append((_bucket_label(lower, upper), bucket))
    na = [row for row in rows if row[key] is None]
    if na:
        groups.append(("n/a", na))
    return groups


@app.get("/")
def index():
    mode = mode_of(request.args)
    cfg = MODES[mode]
    columns = cfg["columns"]
    sorts_whitelist = cfg["sorts"] | set(DISTANCES)
    status = request.args.get("status", "").strip() or None
    q = request.args.get("q", "").strip() or None
    country = None if mode == "crypto" else (request.args.get("country", "").strip() or None)
    instrument_type = None if mode == "crypto" else (request.args.get("instrument_type", "").strip() or None)
    if instrument_type not in ("STOCK", "ETF"):
        instrument_type = None
    group = request.args.get("group", "").strip()
    if group not in DISTANCES:
        group = None
    sorts = _parse_sorts(request.args, sorts_whitelist, cfg["default_sort"])
    bounds = []
    filters = {}
    if status:
        filters["status"] = status
    if q:
        filters["q"] = q
    if country:
        filters["country"] = country
    if instrument_type:
        filters["instrument_type"] = instrument_type
    if group:
        filters["group"] = group
    for key in DISTANCES:
        for bound in ("min", "max"):
            value = _parse_pct(request.args.get(f"{bound}_{key}", ""))
            if value is not None:
                bounds.append((key, ">=" if bound == "min" else "<=", value))
                filters[f"{bound}_{key}"] = f"{value:g}"
    filters["mode"] = mode
    nav_filters = {k: v for k, v in filters.items() if k != "mode"}
    rows = _query_rows(mode=mode, status=status, q=q, country=country,
                       instrument_type=instrument_type, sorts=sorts, bounds=bounds)
    groups = _group_rows(rows, group)
    counts = collections.Counter(r["status_tag"] for r in rows)
    sort_markers = {}
    for i, (col, direction) in enumerate(sorts):
        level = i + 1
        arrow = "▼" if direction == "desc" else "▲"
        sort_markers[col] = arrow if len(sorts) == 1 else f"{arrow}{level}"
    header_urls = {}
    for col, _ in columns:
        hs = _header_sorts(sorts, col)
        header_urls[col] = url_for(
            "index", sort=[c for c, _ in hs], dir=[d for _, d in hs], **filters
        )
    new_tag = f"NEW_{datetime.now().year}"
    if mode == "crypto":
        running = _crypto_lock.locked()
        summary = last_crypto_summary
    else:
        running = _sync_lock.locked()
        summary = last_summary
    try:
        refresh_status = refresh.status(DB_PATH, mode)
        if summary is None and refresh_status["started_at"]:
            summary = dict(refresh_status["summary"], started_at=refresh_status["started_at"],
                           finished_at=refresh_status["finished_at"])
            if refresh_status["error"]:
                summary["error"] = refresh_status["error"]
        running = running or refresh_status["state"] == "running"
    except (OSError, ValueError, sqlite3.Error) as exc:
        log.error("Refresh status unavailable (%s)", type(exc).__name__)
        refresh_status = {"state": "unavailable", "stale": True}
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return render_template(
        "index.html",
        csrf_token=session["csrf_token"],
        refresh=refresh_status,
        groups=groups,
        group=group or "",
        status=status or "",
        q=q or "",
        country=country or "",
        filters=filters,
        nav_filters=nav_filters,
        columns=columns,
        distance_labels={key: label for key, (label, _expr) in DISTANCES.items()},
        numeric_cols={"opening_price", "year_max", "year_min", "all_time_max", "all_time_min", *DISTANCES},
        countries=sorted(sync.EXCHANGE_COUNTRIES.values()),
        summary=summary,
        running=running,
        new_tag=new_tag,
        mode=mode,
        instrument_type=instrument_type or "",
        sorts=sorts,
        sort_markers=sort_markers,
        header_urls=header_urls,
        shown=len(rows),
        active=counts.get("ACTIVE", 0),
        new_count=sum(v for k, v in counts.items() if k.startswith("NEW_")),
        delisted=counts.get("DELISTED_OR_ZERO", 0),
    )


@app.get("/health")
def health():
    return "ok", 200


@app.post("/sync")
def sync_now():
    # A full sync takes hours, so run it in the background; run_sync_job skips if one is running.
    threading.Thread(target=run_sync_job, daemon=True).start()
    return redirect(url_for("index"))


@app.post("/sync/crypto")
def sync_crypto_now():
    # A crypto sync takes minutes, so run it in the background; run_crypto_sync_job skips if one is running.
    threading.Thread(target=run_crypto_sync_job, daemon=True).start()
    return redirect(url_for("index", mode="crypto"))

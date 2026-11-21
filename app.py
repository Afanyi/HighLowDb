import collections
import logging
import math
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, render_template, request, redirect, url_for

import sync
import crypto_sync

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
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


def next_run(now=None, hh=SYNC_HH, mm=SYNC_MM, weekdays_only=True):
    """Return the next run at hh:mm UTC strictly after ``now``.

    When ``weekdays_only`` is False the run fires every day (crypto trades 24/7).
    """
    now = now or datetime.now(timezone.utc)
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    while candidate <= now or (weekdays_only and candidate.weekday() >= 5):
        candidate += timedelta(days=1)
        candidate = candidate.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return candidate


def _summary_counts(summary):
    """Counts and timestamps only, for the log: lists such as failed_tickers are left out."""
    return ", ".join(f"{k}={v}" for k, v in summary.items() if not isinstance(v, (list, dict)))


def run_sync_job():
    """Run the stock sync if the lock is free; otherwise skip (no concurrent syncs)."""
    global last_summary
    if not _sync_lock.acquire(blocking=False):
        log.info("stock sync skipped: already running")
        return
    try:
        log.info("stock sync started")
        last_summary = sync.run_sync(db_path=DB_PATH)
        log.info("stock sync finished: %s", _summary_counts(last_summary))
    except Exception as exc:  # noqa: BLE001
        log.exception("stock sync failed")
        last_summary = {"error": str(exc)}
    finally:
        _sync_lock.release()


def run_crypto_sync_job():
    """Run the crypto sync if the lock is free; otherwise skip (no concurrent syncs)."""
    global last_crypto_summary
    if not _crypto_lock.acquire(blocking=False):
        log.info("crypto sync skipped: already running")
        return
    try:
        log.info("crypto sync started")
        last_crypto_summary = crypto_sync.run_sync(db_path=CRYPTO_DB_PATH)
        log.info("crypto sync finished: %s", _summary_counts(last_crypto_summary))
    except Exception as exc:  # noqa: BLE001
        log.exception("crypto sync failed")
        last_crypto_summary = {"error": str(exc)}
    finally:
        _crypto_lock.release()


def _scheduler_loop(get_next_run, run_job):
    while True:
        nr = get_next_run()
        wait = (nr - datetime.now(timezone.utc)).total_seconds()
        if wait > 0:
            time.sleep(wait)
        run_job()


threading.Thread(target=_scheduler_loop, args=(
    lambda: next_run(hh=SYNC_HH, mm=SYNC_MM),
    run_sync_job,
), daemon=True).start()
threading.Thread(target=_scheduler_loop, args=(
    lambda: next_run(hh=CRYPTO_HH, mm=CRYPTO_MM, weekdays_only=False),
    run_crypto_sync_job,
), daemon=True).start()


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
    return render_template(
        "index.html",
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

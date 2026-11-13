import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, render_template, request, redirect, url_for

import sync

app = Flask(__name__)

SYNC_TIME_UTC = os.environ.get("SYNC_TIME_UTC", "14:45")
SYNC_HH, SYNC_MM = (int(x) for x in SYNC_TIME_UTC.split(":"))
if not (0 <= SYNC_HH <= 23 and 0 <= SYNC_MM <= 59):
    raise ValueError(f"SYNC_TIME_UTC must be HH:MM, got {SYNC_TIME_UTC!r}")

STATUS_PATTERN = re.compile(r"^(ACTIVE|DELISTED_OR_ZERO|NEW_\d{4})$")

ALLOWED_SORTS = {
    "ticker", "name", "exchange", "opening_price", "year_max", "year_min",
    "all_time_max", "all_time_min", "status_tag", "last_updated",
}
ROW_CAP = 500
DB_PATH = sync.DB_PATH

_sync_lock = threading.Lock()
last_summary = None


def next_run(now=None, hh=SYNC_HH, mm=SYNC_MM):
    """Return the next weekday at hh:mm UTC strictly after ``now``."""
    now = now or datetime.now(timezone.utc)
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    while candidate <= now or candidate.weekday() >= 5:
        candidate += timedelta(days=1)
        candidate = candidate.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return candidate


def run_sync_job():
    """Run sync if the lock is free; otherwise skip (no concurrent syncs)."""
    global last_summary
    if not _sync_lock.acquire(blocking=False):
        return
    try:
        last_summary = sync.run_sync(db_path=DB_PATH)
    except Exception as exc:  # noqa: BLE001
        last_summary = {"error": str(exc)}
    finally:
        _sync_lock.release()


def _scheduler_loop():
    while True:
        nr = next_run(hh=SYNC_HH, mm=SYNC_MM)
        wait = (nr - datetime.now(timezone.utc)).total_seconds()
        if wait > 0:
            time.sleep(wait)
        run_sync_job()


threading.Thread(target=_scheduler_loop, daemon=True).start()


def _query_rows(status=None, q=None, sort="ticker", direction="asc"):
    if sort not in ALLOWED_SORTS:
        sort = "ticker"
    if direction not in ("asc", "desc"):
        direction = "asc"

    sql = (
        "SELECT ticker, name, exchange, opening_price, year_max, year_min, "
        "all_time_max, all_time_min, status_tag, last_updated FROM stocks"
    )
    params = []
    conditions = []
    if status and STATUS_PATTERN.match(status):
        conditions.append("status_tag = ?")
        params.append(status)
    if q:
        conditions.append("(ticker LIKE ? OR name LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += f" ORDER BY {sort} {direction} LIMIT ?"
    params.append(ROW_CAP)

    sync.init_db(DB_PATH)  # fresh DB: create the table before the first sync
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return rows


@app.get("/")
def index():
    status = request.args.get("status", "").strip() or None
    q = request.args.get("q", "").strip() or None
    sort = request.args.get("sort", "ticker").strip()
    direction = request.args.get("dir", "asc").strip()
    rows = _query_rows(status=status, q=q, sort=sort, direction=direction)
    return render_template(
        "index.html",
        rows=rows,
        status=status or "",
        q=q or "",
        sort=sort,
        direction=direction,
        summary=last_summary,
        running=_sync_lock.locked(),
        new_tag=f"NEW_{datetime.now().year}",
        row_cap=ROW_CAP,
    )


@app.post("/sync")
def sync_now():
    # A full sync takes hours, so run it in the background; run_sync_job skips if one is running.
    threading.Thread(target=run_sync_job, daemon=True).start()
    return redirect(url_for("index"))

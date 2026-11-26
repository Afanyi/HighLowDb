"""Persistent refresh status and private, process-safe deployment coordination.

The CLI never imports the web app or starts scheduler/provider jobs.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time


COUNTS = {"ok", "coins", "failed", "no_data", "unmapped", "resolved_by_isin",
          "backfilled", "backfill_pending", "calls"}
STATES = {"never", "running", "completed", "partial", "failed", "interrupted"}


def schedule(mode):
    if mode not in {"stocks", "crypto"}:
        raise ValueError("Unknown refresh dataset")
    setting = "SYNC_TIME_UTC" if mode == "stocks" else "CRYPTO_SYNC_TIME_UTC"
    value = os.environ.get(setting, "14:45" if mode == "stocks" else "02:00")
    hh, mm = (int(part) for part in value.split(":"))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"{setting} must be HH:MM")
    return hh, mm, mode == "stocks"


def next_run(now=None, hh=None, mm=None, weekdays_only=True):
    default_hh, default_mm, _ = schedule("stocks")
    hh = default_hh if hh is None else hh
    mm = default_mm if mm is None else mm
    now = now or datetime.now(timezone.utc)
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    while candidate <= now or (weekdays_only and candidate.weekday() >= 5):
        candidate = (candidate + timedelta(days=1)).replace(hour=hh, minute=mm, second=0, microsecond=0)
    return candidate


def next_for(mode, now=None):
    hh, mm, weekdays = schedule(mode)
    return next_run(now, hh, mm, weekdays)


def _path(db_path, suffix):
    return Path(str(db_path) + ".refresh." + suffix)


def _private_file(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
        os.close(fd)
        raise PermissionError("Refresh state must be a private, owned regular file")
    return fd


@contextmanager
def _lock(db_path, suffix, kind):
    fd = _private_file(_path(db_path, suffix))
    try:
        try:
            fcntl.flock(fd, kind | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False
        yield acquired
    finally:
        os.close(fd)


@contextmanager
def job_guard(db_path, mode):
    schedule(mode)  # Validate before using the mode in a filename.
    with _lock(db_path, "gate.lock", fcntl.LOCK_SH) as admitted:
        if not admitted:
            yield False
            return
        with _lock(db_path, mode + ".lock", fcntl.LOCK_EX) as acquired:
            yield acquired


@contextmanager
def deployment_guard(db_path, timeout=60):
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("Drain timeout must be finite and nonnegative")
    deadline = time.monotonic() + timeout
    while True:
        with _lock(db_path, "gate.lock", fcntl.LOCK_EX) as acquired:
            if acquired:
                yield
                return
        if time.monotonic() >= deadline:
            raise TimeoutError("Active refresh or another deployment prevented draining")
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


@contextmanager
def _connection(db_path):
    path = _path(db_path, "db")
    os.close(_private_file(path))
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS refresh_state (
                dataset TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'never',
                source TEXT, started_at TEXT, finished_at TEXT, last_success_at TEXT,
                summary TEXT NOT NULL DEFAULT '{}', error TEXT, due_at TEXT)""")
            yield conn
    finally:
        conn.close()


def due_at(db_path, mode, now=None):
    with _connection(db_path) as conn:
        conn.execute("INSERT OR IGNORE INTO refresh_state (dataset, due_at) VALUES (?, ?)",
                     (mode, next_for(mode, now).isoformat()))
        value = conn.execute("SELECT due_at FROM refresh_state WHERE dataset=?", (mode,)).fetchone()[0]
    return datetime.fromisoformat(value)


def _eligible_at(mode, due, now):
    if mode == "stocks":
        # Anchor to the latest weekend, including after a Monday restart. A slot
        # missed before that weekend coalesces with its following Monday's run.
        saturday = (now - timedelta(days=(now.weekday() - 5) % 7)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        if due < saturday:
            return next_for(mode, saturday)
    return due


def ready_at(db_path, mode, now):
    return _eligible_at(mode, due_at(db_path, mode, now), now)


def started(db_path, mode, source, now):
    if source not in {"manual", "scheduled"}:
        raise ValueError("Unknown refresh source")
    due_at(db_path, mode, now)
    with _connection(db_path) as conn:
        conn.execute("""UPDATE refresh_state SET state='running', source=?, started_at=?,
            finished_at=NULL, summary='{}', error=NULL WHERE dataset=?""", (source, now.isoformat(), mode))


def finished(db_path, mode, summary, error, now):
    counts = {k: v for k, v in summary.items() if k in COUNTS and type(v) is int and v >= 0}
    complete = not error and not counts.get("failed", 0) and counts.get("ok" if mode == "stocks" else "coins", 0) > 0
    state = "failed" if error else "completed" if complete else "partial"
    with _connection(db_path) as conn:
        row = conn.execute("SELECT * FROM refresh_state WHERE dataset=?", (mode,)).fetchone()
        # A manual run begun after a due time also satisfies that slot. Advance
        # atomically with completion so the sleeping scheduler cannot run it twice.
        due = row["due_at"]
        if due <= row["started_at"]:
            due = next_for(mode, now).isoformat()
        conn.execute("""UPDATE refresh_state SET state=?, finished_at=?, summary=?, error=?, due_at=?,
            last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END WHERE dataset=?""",
                     (state, now.isoformat(), json.dumps(counts), error, due, complete, now.isoformat(), mode))


def status(db_path, mode, now=None):
    now = now or datetime.now(timezone.utc)
    due_at(db_path, mode, now)
    # A 'running' record with no kernel-held dataset lock belongs to a dead
    # process. Do this under the same lock jobs use, never from a stale PID.
    with _lock(db_path, mode + ".lock", fcntl.LOCK_EX) as idle:
        with _connection(db_path) as conn:
            if idle:
                conn.execute("""UPDATE refresh_state SET state='interrupted', finished_at=?, error='Process interrupted'
                    WHERE dataset=? AND state='running'""", (now.isoformat(), mode))
            row = dict(conn.execute("SELECT * FROM refresh_state WHERE dataset=?", (mode,)).fetchone())
    if row["state"] not in STATES:
        raise ValueError("Invalid persisted refresh state")
    due = datetime.fromisoformat(row["due_at"])
    row["summary"] = {k: v for k, v in json.loads(row["summary"]).items()
                      if k in COUNTS and type(v) is int and v >= 0}
    hh, mm, weekdays = schedule(mode)
    previous = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    while previous > now or (weekdays and previous.weekday() >= 5):
        previous -= timedelta(days=1)
    with _lock(db_path, "gate.lock", fcntl.LOCK_SH) as admitted:
        row["paused"] = not admitted
    eligible = _eligible_at(mode, due, now)
    if mode == "stocks":
        # Stock staleness is 7 days since the last successful manual sync,
        # not the weekday schedule.
        stale = not row["last_success_at"] or datetime.fromisoformat(row["last_success_at"]) < now - timedelta(days=7)
    else:
        stale = not row["last_success_at"] or datetime.fromisoformat(row["last_success_at"]) < previous
    row.update(next_run=eligible.isoformat(), overdue=due <= now,
               stale=stale,
               schedule=f"{'Weekdays' if weekdays else 'Daily'} at {hh:02}:{mm:02} UTC")
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "hold"))
    parser.add_argument("--db-path", default=os.environ.get("DB_PATH", "trading212_stocks.db"))
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    try:
        if args.command == "status":
            print(json.dumps({mode: status(args.db_path, mode) for mode in ("stocks", "crypto")}))
        else:
            with deployment_guard(args.db_path, args.timeout):
                print("drained", flush=True)
                # Phase 3 can hold this separate helper across app recreation.
                # EOF (or process death) releases the gate without a stale marker.
                sys.stdin.read()
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Refresh control failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Container-only data initialization, consistent backup and compatibility checks."""

import json
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, "/app")  # This helper is mounted at /tools; providers belong to the image.


NAMES = ("trading212_stocks.db", "crypto.db", "trading212_stocks.db.refresh.db")
# Reviewed additive stock migration (stock plan Phase 4b); any other change still fails check.
STOCK_COLUMNS = (", current_price REAL, price_date TEXT, isin TEXT, country TEXT,"
                 " verification TEXT, quality_flags TEXT")
UNIVERSE_MEMBERS = ("CREATE TABLE universe_members ( ticker TEXT PRIMARY KEY, isin TEXT NOT NULL,"
                    " country TEXT NOT NULL, rank INTEGER NOT NULL, volatility REAL NOT NULL,"
                    " selected_at TEXT NOT NULL )")


def describe(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("Unsafe or missing database")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Database integrity check failed")
        schema = conn.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name").fetchall()
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        counts = {table: conn.execute('SELECT count(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0]
                  for table in tables}
    return {"schema": schema, "counts": counts}


def initialize(directory):
    import sync
    import crypto_sync
    import refresh
    stock = str(directory / NAMES[0])
    sync.init_db(stock)
    crypto_sync.init_db(str(directory / NAMES[1]))
    for mode in ("stocks", "crypto"):
        refresh.status(stock, mode)


def backup(source, destination):
    report = {}
    for name in NAMES:
        path = source / name
        before = describe(path)
        target = destination / name
        with target.open("xb"):
            pass
        target.chmod(0o600)
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
        report[name] = describe(target)
        if before != report[name]:
            raise ValueError("Backup verification failed")
    return report


def check(directory):
    # Restore snapshots into disposable storage; never initialize the live data.
    with tempfile.TemporaryDirectory() as temporary:
        restored = Path(temporary)
        before = {}
        for name in NAMES:
            shutil.copyfile(directory / name, restored / name)
            (restored / name).chmod(0o600)
            before[name] = describe(restored / name)
        # root backup/check helpers must leave metadata owned by the current UID.
        initialize(restored)
        after = {name: describe(restored / name) for name in NAMES}
        if before != after and not reviewed(before, after):
            raise ValueError("Database schema/count change requires a reviewed migration")
        return after


def reviewed(before, after):
    """True only for the reviewed stock migration: listed columns and an empty new table."""
    if any(before[name] != after[name] for name in NAMES[1:]):
        return False
    old, new = before[NAMES[0]], after[NAMES[0]]
    old_schema, new_schema = dict(map(tuple, old["schema"])), dict(map(tuple, new["schema"]))
    new_counts = dict(new["counts"])
    stocks = old_schema.pop("stocks", None)
    if stocks is None or new_schema.pop("stocks", None) not in {stocks, stocks[:-1] + STOCK_COLUMNS + ")"}:
        return False
    if "universe_members" not in old_schema:
        table = new_schema.pop("universe_members", "")
        if " ".join(table.split()) != UNIVERSE_MEMBERS or new_counts.pop("universe_members", None) != 0:
            return False
    return old_schema == new_schema and old["counts"] == new_counts


if __name__ == "__main__":
    os.umask(0o077)
    command = sys.argv[1]
    if command == "init":
        initialize(Path("/data"))
        print("initialized")
    elif command == "backup":
        print(json.dumps(backup(Path("/data"), Path("/backup"))))
    elif command == "check":
        print(json.dumps(check(Path("/backup"))))
    else:
        raise SystemExit("Unknown data operation")

import os
import sqlite3
from datetime import datetime, timezone

import pandas as pd
import requests
import yfinance as yf

DB_PATH = os.environ.get("DB_PATH", "trading212_stocks.db")

# Fixed HTTPS hosts so the API key is never sent elsewhere; T212_ENVIRONMENT picks one.
T212_BASE_URLS = {
    "live": "https://live.trading212.com/api/v0",
    "demo": "https://demo.trading212.com/api/v0",
}
INSTRUMENTS_ENDPOINT = "/equity/metadata/instruments"
REQUEST_TIMEOUT = 30
BATCH_SIZE = 50

# T212 ticker endings -> (Yahoo suffix, exchange), derived from the real instruments list.
# "<SYMBOL>_<country>_EQ", e.g. "AAPL_US_EQ":
COUNTRY_CODES = {
    "US": ("", "US"),
    "CA": (".TO", "TSX"),
    "PT": (".LS", "LISBON"),
    "AT": (".VI", "VIENNA"),
    "BE": (".BR", "BRUSSELS"),
    "BB": (".BR", "BRUSSELS"),
}
# "<SYMBOL><letter>_EQ", e.g. "BPl_EQ". German "d" listings resolve on Yahoo as Frankfurt (.F).
# Anything else (rare endings, secondary "l1"/"d1" listings) is counted as unmapped.
EXCHANGE_LETTERS = {
    "l": (".L", "LSE"),
    "d": (".F", "FRANKFURT"),
    "p": (".PA", "PARIS"),
    "e": (".MC", "MADRID"),
    "s": (".SW", "SIX"),
    "a": (".AS", "AMSTERDAM"),
}

# Exchange label -> country, for the UI country filter.
EXCHANGE_COUNTRIES = {
    "US": "United States",
    "TSX": "Canada",
    "LISBON": "Portugal",
    "VIENNA": "Austria",
    "BRUSSELS": "Belgium",
    "LSE": "United Kingdom",
    "FRANKFURT": "Germany",
    "PARIS": "France",
    "MADRID": "Spain",
    "SIX": "Switzerland",
    "AMSTERDAM": "Netherlands",
}


def init_db(db_path: str = DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stocks (
                ticker TEXT PRIMARY KEY,
                name TEXT,
                exchange TEXT,
                opening_price REAL,
                year_max REAL,
                year_min REAL,
                all_time_max REAL,
                all_time_min REAL,
                status_tag TEXT,
                last_updated TEXT
            )
        """
        )
        conn.commit()
    finally:
        conn.close()


def fetch_instruments() -> list[dict]:
    """Fetch every instrument from Trading 212. Raises if T212_API_KEY is missing."""
    key = os.environ.get("T212_API_KEY")
    if not key:
        raise ValueError("T212_API_KEY environment variable is required; no request made.")
    environment = os.environ.get("T212_ENVIRONMENT", "live")
    if environment not in T212_BASE_URLS:
        raise ValueError("T212_ENVIRONMENT must be 'live' or 'demo'; no request made.")
    secret = os.environ.get("T212_API_SECRET")
    # Key + secret uses HTTP Basic; a legacy key alone goes in the Authorization header.
    auth = {"auth": (key, secret)} if secret else {"headers": {"Authorization": key}}
    resp = requests.get(T212_BASE_URLS[environment] + INSTRUMENTS_ENDPOINT, timeout=REQUEST_TIMEOUT, **auth)
    resp.raise_for_status()
    return resp.json()


def to_yahoo(ticker: str) -> tuple[str, str] | None:
    """Convert a T212 ticker to (yahoo_symbol, exchange), e.g. BPl_EQ -> ("BP.L", "LSE").

    Returns None for non-equity, malformed or unmapped tickers.
    """
    parts = ticker.split("_")
    if len(parts) == 3 and parts[0] and parts[1] in COUNTRY_CODES and parts[2] == "EQ":
        suffix, exchange = COUNTRY_CODES[parts[1]]
        return parts[0] + suffix, exchange
    if len(parts) == 2 and len(parts[0]) > 1 and parts[0][-1] in EXCHANGE_LETTERS and parts[1] == "EQ":
        suffix, exchange = EXCHANGE_LETTERS[parts[0][-1]]
        return parts[0][:-1] + suffix, exchange
    return None


def compute_metrics(hist: pd.DataFrame, year: int) -> dict:
    """Pure function: compute all stock metrics from a yfinance history frame."""
    if hist.empty:
        return {
            "opening_price": None,
            "year_max": None,
            "year_min": None,
            "all_time_max": None,
            "all_time_min": None,
            "status_tag": "DELISTED_OR_ZERO",
        }

    today_open = float(hist["Open"].iloc[-1])
    at_max = float(hist["High"].max())
    at_min = float(hist["Low"].min())

    hist_year = hist[hist.index.year == year]
    if not hist_year.empty:
        yr_max = float(hist_year["High"].max())
        yr_min = float(hist_year["Low"].min())
        if hist.index[0].year == year:
            status_tag = f"NEW_{year}"
        else:
            status_tag = "ACTIVE"
    else:
        yr_max, yr_min = None, None
        status_tag = "ACTIVE"

    current_price = float(hist["Close"].iloc[-1])
    if current_price <= 0.01 or len(hist) < 5:
        status_tag = "DELISTED_OR_ZERO"

    return {
        "opening_price": today_open,
        "year_max": yr_max,
        "year_min": yr_min,
        "all_time_max": at_max,
        "all_time_min": at_min,
        "status_tag": status_tag,
    }


def run_sync(db_path: str = DB_PATH) -> dict:
    """Fetch T212 instruments, map to Yahoo, download metrics, upsert into SQLite.

    Returns a summary dict with ok, failed, unmapped counts and timestamps.
    """
    init_db(db_path)
    instruments = fetch_instruments()

    stock_instruments = [i for i in instruments if i.get("type") == "STOCK"]

    yahoo_map: dict[str, tuple[dict, str]] = {}
    unmapped_count = 0
    for inst in stock_instruments:
        mapped = to_yahoo(inst["ticker"])
        if mapped:
            yahoo, exchange = mapped
            yahoo_map[yahoo] = (inst, exchange)
        else:
            unmapped_count += 1

    summary = {
        "ok": 0,
        "failed": 0,
        "unmapped": unmapped_count,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    yahoo_tickers = list(yahoo_map.keys())
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        current_year = datetime.now().year

        for i in range(0, len(yahoo_tickers), BATCH_SIZE):
            batch = yahoo_tickers[i : i + BATCH_SIZE]
            try:
                raw = yf.download(
                    batch, period="max", group_by="ticker", progress=False, threads=False
                )
            except Exception:
                raw = None

            for yahoo in batch:
                inst, exchange = yahoo_map[yahoo]
                try:
                    hist = None
                    if raw is not None:
                        try:
                            hist = raw[yahoo]
                        except (KeyError, TypeError):
                            hist = raw if len(batch) == 1 else None
                    # Batches pad invalid symbols and other exchanges' trading days with NaN rows.
                    if hist is not None:
                        hist = hist.dropna(how="all")
                    if hist is None or hist.empty:
                        summary["failed"] += 1
                        continue

                    metrics = compute_metrics(hist, current_year)
                    cursor.execute(
                        """
                        INSERT INTO stocks (ticker, name, exchange, opening_price, year_max, year_min,
                                            all_time_max, all_time_min, status_tag, last_updated)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                        ON CONFLICT(ticker) DO UPDATE SET
                            name=excluded.name,
                            exchange=excluded.exchange,
                            opening_price=excluded.opening_price,
                            year_max=excluded.year_max,
                            year_min=excluded.year_min,
                            all_time_max=excluded.all_time_max,
                            all_time_min=excluded.all_time_min,
                            status_tag=excluded.status_tag,
                            last_updated=datetime('now')
                        """,
                        (
                            inst["ticker"],
                            inst.get("name"),
                            exchange,
                            metrics["opening_price"],
                            metrics["year_max"],
                            metrics["year_min"],
                            metrics["all_time_max"],
                            metrics["all_time_min"],
                            metrics["status_tag"],
                        ),
                    )
                    summary["ok"] += 1
                except Exception:
                    summary["failed"] += 1
            conn.commit()
    finally:
        conn.close()

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    return summary

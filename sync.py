import logging
import os
import sqlite3
import time
import warnings
from datetime import datetime, timezone

import pandas as pd
import requests
import yfinance as yf
from yfinance.exceptions import YFRateLimitError, YFTickerMissingError

log = logging.getLogger(__name__)
# yfinance calls deprecated pandas inplace APIs on every download; keep that out of the logs.
warnings.filterwarnings("ignore", message="The inplace keyword", module="yfinance")

DB_PATH = os.environ.get("DB_PATH", "trading212_stocks.db")

# Fixed HTTPS hosts so the API key is never sent elsewhere; T212_ENVIRONMENT picks one.
T212_BASE_URLS = {
    "live": "https://live.trading212.com/api/v0",
    "demo": "https://demo.trading212.com/api/v0",
}
INSTRUMENTS_ENDPOINT = "/equity/metadata/instruments"
REQUEST_TIMEOUT = 30

# Yahoo pacing: one request per ticker, a pause between them, and an exponential back-off
# whenever Yahoo answers 429. Rate limits never count as a failure; the ticker is retried.
REQUEST_DELAY = float(os.environ.get("YAHOO_REQUEST_DELAY", "0.5"))
RATE_LIMIT_BACKOFF = 60
MAX_RATE_LIMIT_BACKOFF = 15 * 60
# Other errors (timeouts, connection resets): attempts per ticker per pass, and extra passes
# over the tickers that still failed, each after a pause.
MAX_ATTEMPTS = 3
RETRY_PASSES = 3
RETRY_PASS_PAUSE = 5 * 60

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
    # Yahoo writes share classes with a dash: T212 "BRK/A_US_EQ" is Yahoo "BRK-A".
    parts = ticker.replace("/", "-").split("_")
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


class _Yahoo:
    """Fetches price history one ticker at a time, respecting Yahoo's rate limit."""

    def __init__(self):
        # Let yfinance raise instead of logging and returning an empty frame, so a network
        # error is never mistaken for "this ticker has no data".
        yf.config.debug.hide_exceptions = False
        self.backoff = RATE_LIMIT_BACKOFF

    def history(self, yahoo: str) -> pd.DataFrame | None:
        """Return the full history, or None if Yahoo has no data for the symbol.

        Raises the last error if the symbol still fails after MAX_ATTEMPTS.
        """
        attempts = 0
        while True:
            time.sleep(REQUEST_DELAY)
            try:
                hist = yf.Ticker(yahoo).history(period="max", actions=False)
            except YFRateLimitError:
                log.warning("Yahoo rate limit hit; waiting %ds before retrying %s", self.backoff, yahoo)
                time.sleep(self.backoff)
                self.backoff = min(self.backoff * 2, MAX_RATE_LIMIT_BACKOFF)
                continue
            except YFTickerMissingError:
                return None
            except Exception:
                attempts += 1
                if attempts >= MAX_ATTEMPTS:
                    raise
                time.sleep(2 ** attempts)
                continue
            self.backoff = RATE_LIMIT_BACKOFF
            hist = hist.dropna(how="all")
            return None if hist.empty else hist


def _upsert(cursor, inst: dict, exchange: str, metrics: dict) -> None:
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


def run_sync(db_path: str = DB_PATH) -> dict:
    """Fetch T212 instruments, map to Yahoo, download metrics, upsert into SQLite.

    Returns a summary dict with counts and timestamps: ok, no_data (Yahoo has no prices for
    the symbol), failed (still erroring after every retry pass, listed in failed_tickers)
    and unmapped (no Yahoo symbol).
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
        "no_data": 0,
        "failed": 0,
        "unmapped": unmapped_count,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    yahoo = _Yahoo()
    pending = list(yahoo_map.keys())
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        current_year = datetime.now().year

        for sync_pass in range(RETRY_PASSES + 1):
            if sync_pass:
                log.info("Retry pass %d: %d tickers, after %ds", sync_pass, len(pending), RETRY_PASS_PAUSE)
                time.sleep(RETRY_PASS_PAUSE)
            errored = []
            for n, symbol in enumerate(pending, 1):
                inst, exchange = yahoo_map[symbol]
                try:
                    hist = yahoo.history(symbol)
                    if hist is None:
                        summary["no_data"] += 1
                        continue
                    _upsert(cursor, inst, exchange, compute_metrics(hist, current_year))
                    summary["ok"] += 1
                except Exception as exc:
                    log.warning("%s failed: %r", symbol, exc)
                    errored.append(symbol)
                if n % 100 == 0:
                    conn.commit()
                    log.info("Sync pass %d: %d/%d done", sync_pass, n, len(pending))
            conn.commit()
            pending = errored
            if not pending:
                break
    finally:
        conn.close()

    summary["failed"] = len(pending)
    summary["failed_tickers"] = [yahoo_map[s][0]["ticker"] for s in pending]
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    return summary

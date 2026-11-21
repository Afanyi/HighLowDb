import logging
import os
import re
import sqlite3
import time
import warnings
from datetime import datetime, timezone

import pandas as pd
import requests
import yfinance as yf
from yfinance.exceptions import YFRateLimitError, YFTickerMissingError
from yfinance.utils import get_ticker_by_isin

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
    "m": (".MI", "MILAN"),
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
    "MILAN": "Italy",
}

# A Yahoo symbol accepted from an ISIN lookup: uppercase, dots/dashes/caret/equals allowed.
ISIN_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9.\-^=]{1,20}$")


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
                last_updated TEXT,
                instrument_type TEXT NOT NULL DEFAULT 'STOCK'
            )
            """
        )
        # Migrate a DB that predates the instrument_type column; existing rows become STOCK.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(stocks)")}
        if "instrument_type" not in cols:
            conn.execute("ALTER TABLE stocks ADD COLUMN instrument_type TEXT NOT NULL DEFAULT 'STOCK'")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS yahoo_symbols (
                ticker TEXT PRIMARY KEY,
                yahoo_symbol TEXT NOT NULL,
                resolved_at TEXT NOT NULL
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

    def isin_to_symbol(self, isin: str) -> str | None:
        """Resolve an ISIN to a Yahoo symbol, with the same pacing and back-off as history().

        Returns None when Yahoo has no match or the ISIN is invalid; a failed lookup never
        crashes the sync.
        """
        attempts = 0
        while True:
            time.sleep(REQUEST_DELAY)
            try:
                return get_ticker_by_isin(isin) or None
            except YFRateLimitError:
                log.warning("Yahoo rate limit hit; waiting %ds before ISIN lookup %s", self.backoff, isin)
                time.sleep(self.backoff)
                self.backoff = min(self.backoff * 2, MAX_RATE_LIMIT_BACKOFF)
                continue
            except (YFTickerMissingError, ValueError):
                return None
            except Exception:
                attempts += 1
                if attempts >= MAX_ATTEMPTS:
                    return None
                time.sleep(2 ** attempts)
                continue


def _upsert(cursor, inst: dict, exchange: str, metrics: dict, instrument_type: str) -> None:
    cursor.execute(
        """
        INSERT INTO stocks (ticker, name, exchange, opening_price, year_max, year_min,
                            all_time_max, all_time_min, status_tag, last_updated, instrument_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
        ON CONFLICT(ticker) DO UPDATE SET
            name=excluded.name,
            exchange=excluded.exchange,
            opening_price=excluded.opening_price,
            year_max=excluded.year_max,
            year_min=excluded.year_min,
            all_time_max=excluded.all_time_max,
            all_time_min=excluded.all_time_min,
            status_tag=excluded.status_tag,
            last_updated=datetime('now'),
            instrument_type=excluded.instrument_type
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
            instrument_type,
        ),
    )


def _exchange_for_symbol(symbol: str) -> str | None:
    """Reverse-map a Yahoo symbol suffix to its exchange; None for an unknown suffix.

    A symbol without a suffix is a plain US ticker.
    """
    for suffix, exchange in list(COUNTRY_CODES.values()) + list(EXCHANGE_LETTERS.values()):
        if suffix and symbol.endswith(suffix):
            return exchange
    return "US" if "." not in symbol else None


def _isin_acceptable(sym: str | None, expected: str | None) -> str | None:
    """Return the exchange for an ISIN-resolved symbol if it may be used, else None.

    A mapped instrument only accepts a symbol on its expected exchange; an unmapped
    instrument accepts any known suffix.
    """
    if not sym or not ISIN_SYMBOL_PATTERN.match(sym):
        return None
    exch = _exchange_for_symbol(sym)
    if expected is not None and exch != expected:
        return None
    return exch


def _save_symbol(conn, ticker: str, symbol: str) -> None:
    conn.execute(
        """
        INSERT INTO yahoo_symbols (ticker, yahoo_symbol, resolved_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(ticker) DO UPDATE SET
            yahoo_symbol=excluded.yahoo_symbol,
            resolved_at=datetime('now')
        """,
        (ticker, symbol),
    )
    conn.commit()


def run_sync(db_path: str = DB_PATH) -> dict:
    """Fetch T212 instruments, map to Yahoo, download metrics, upsert into SQLite.

    STOCK and ETF instruments are kept; warrants are skipped. A saved ISIN match is used
    first; otherwise the symbol comes from the T212 ticker or, for unmappable tickers, from
    an ISIN lookup. A symbol with no data is retried once through the ISIN. An ISIN match is
    saved only once Yahoo returned prices for it, and a saved match without data is dropped.

    Returns a summary dict with counts and timestamps: ok, no_data (Yahoo has no prices for
    the symbol), failed (still erroring after every retry pass, listed in failed_tickers),
    unmapped (no Yahoo mapping and no ISIN match) and resolved_by_isin (instruments newly
    matched through their ISIN this run). ok + no_data + failed equals the number of
    instruments that had a Yahoo symbol to try.
    """
    init_db(db_path)
    instruments = fetch_instruments()

    keep = [i for i in instruments if i.get("type") in ("STOCK", "ETF")]

    yahoo = _Yahoo()
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        current_year = datetime.now().year

        saved = dict(conn.execute("SELECT ticker, yahoo_symbol FROM yahoo_symbols").fetchall())

        # T212 ticker -> [instrument, yahoo symbol, exchange, source, expected exchange].
        # Keyed by T212 ticker, so two instruments sharing one Yahoo symbol both get a row.
        # source: "mapped" (from the ticker), "saved" (yahoo_symbols) or "isin" (new match).
        work: dict[str, list] = {}
        isin_tried: set[str] = set()
        unmapped_count = 0
        for inst in keep:
            ticker = inst["ticker"]
            mapped = to_yahoo(ticker)
            expected = mapped[1] if mapped else None
            saved_sym = saved.get(ticker)
            saved_exchange = _isin_acceptable(saved_sym, expected)
            if saved_exchange:
                work[ticker] = [inst, saved_sym, saved_exchange, "saved", expected]
            elif mapped:
                work[ticker] = [inst, mapped[0], mapped[1], "mapped", expected]
            else:
                isin_tried.add(ticker)
                sym = _resolve_isin(yahoo, inst, None)
                if sym:
                    work[ticker] = [inst, sym[0], sym[1], "isin", None]
                else:
                    unmapped_count += 1

        summary = {
            "ok": 0,
            "no_data": 0,
            "failed": 0,
            "unmapped": unmapped_count,
            "resolved_by_isin": 0,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }

        pending = list(work)
        for sync_pass in range(RETRY_PASSES + 1):
            if sync_pass:
                log.info("Retry pass %d: %d tickers, after %ds", sync_pass, len(pending), RETRY_PASS_PAUSE)
                time.sleep(RETRY_PASS_PAUSE)
            errored = []
            for n, ticker in enumerate(pending, 1):
                try:
                    result = _sync_one(yahoo, conn, cursor, work[ticker], isin_tried, current_year)
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s failed: %r", work[ticker][1], exc)
                    errored.append(ticker)
                else:
                    summary[result] += 1
                    if result == "ok" and work[ticker][3] == "isin":
                        summary["resolved_by_isin"] += 1
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
    summary["failed_tickers"] = pending
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    return summary


def _resolve_isin(yahoo: _Yahoo, inst: dict, expected: str | None) -> tuple[str, str] | None:
    """Look up the instrument's ISIN; (symbol, exchange) if the match may be used, else None."""
    isin = inst.get("isin")
    if not isin:
        return None
    sym = yahoo.isin_to_symbol(isin)
    exchange = _isin_acceptable(sym, expected)
    if sym and exchange is None:
        log.info("ISIN match rejected for %s: %r", inst["ticker"], sym)
    return (sym, exchange) if exchange else None


def _sync_one(yahoo: _Yahoo, conn, cursor, entry: list, isin_tried: set, current_year: int) -> str:
    """Sync one instrument; returns "ok" or "no_data", raises on errors (retried later).

    entry is updated in place, so a retry pass reuses an ISIN match found earlier.
    """
    inst, symbol, exchange, source, expected = entry
    ticker = inst["ticker"]
    hist = yahoo.history(symbol)
    if hist is None and source == "saved":
        conn.execute("DELETE FROM yahoo_symbols WHERE ticker=?", (ticker,))
    if hist is None and ticker not in isin_tried:
        isin_tried.add(ticker)
        match = _resolve_isin(yahoo, inst, expected)
        if match and match[0] != symbol:
            entry[1:4] = [match[0], match[1], "isin"]
            symbol, exchange = match
            hist = yahoo.history(symbol)
    if hist is None:
        return "no_data"
    if entry[3] == "isin":
        _save_symbol(conn, ticker, symbol)
    _upsert(cursor, inst, exchange, compute_metrics(hist, current_year), inst.get("type", "STOCK"))
    return "ok"

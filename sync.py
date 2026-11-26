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
# Trading 212 keeps a US line's original ticker after a symbol change; shortName is current.
US_SHORT_NAME = re.compile(r"^[A-Z0-9]{1,6}(-[A-Z])?$")


# Confirmed Yahoo symbol corrections from the Phase 1 audit.
# T212 ticker -> (yahoo_symbol, exchange, exact T212 name, required ISIN or None).
# An override applies only when the instrument's name (and ISIN, if given) match exactly;
# otherwise the row is flagged OVERRIDE_MISMATCH and no prices are fetched for it.
SYMBOL_OVERRIDES = {
    "ACIC_US_EQ": ("ACHR", "US", "Archer Aviation", None),
    "GOLD_US_EQ": ("B", "US", "Barrick Mining", None),
    "CNX_US_EQ": ("CNR", "US", "Core Natural Resources", None),
    "CR_US_EQ": ("CXT", "US", "Crane NXT", None),
    "ALAIRp_EQ": ("ALMEX.PA", "PARIS", "Mexedia", None),
    "SEV_US_EQ": ("SSM", "US", "Sono Group", None),
    # Only the ISIN tells Under Armour Class A (UAA) from Class C (UA).
    "UA_US_EQ": ("UAA", "US", "Under Armour (Class A)", "US9043111072"),
}

# Ordering rule: 0 < all_time_min <= year_min <= current_price <= year_max <= all_time_max.
# A bar is impossible if any price is non-positive, high < low, or open/close fall outside [low, high].


def _valid_price(value) -> bool:
    """Return True if value is a finite, positive price."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return v > 0 and v == v and abs(v) != float("inf")


def _valid_bar(row) -> bool:
    """Return True if a single history row contains a valid, possible OHLC observation."""
    try:
        o = float(row["Open"])
        h = float(row["High"])
        l = float(row["Low"])
        c = float(row["Close"])
    except (TypeError, KeyError, ValueError):
        return False
    if not (_valid_price(o) and _valid_price(h) and _valid_price(l) and _valid_price(c)):
        return False
    if h < l:
        return False
    if c < l or c > h:
        return False
    if o < l or o > h:
        return False
    return True


def _ordering_ok(metrics: dict) -> bool:
    """Enforce 0 < all_time_min <= year_min <= current_price <= year_max <= all_time_max."""
    at_min = metrics.get("all_time_min")
    at_max = metrics.get("all_time_max")
    yr_min = metrics.get("year_min")
    yr_max = metrics.get("year_max")
    cur = metrics.get("current_price")
    if at_min is None or at_max is None or cur is None:
        return False
    if not (0 < at_min <= at_max and 0 < cur <= at_max):
        return False
    if yr_min is not None and not (at_min <= yr_min <= cur):
        return False
    if yr_max is not None and not (cur <= yr_max <= at_max):
        return False
    return True


def override_matches(inst: dict) -> bool:
    """True if the instrument has a symbol override and its exact name (and ISIN) match."""
    if inst.get("ticker") not in SYMBOL_OVERRIDES:
        return False
    _sym, _exch, name, isin = SYMBOL_OVERRIDES[inst["ticker"]]
    return (inst.get("name") or "").strip().casefold() == name.casefold() and (
        isin is None or inst.get("isin") == isin)


def line_symbol(inst: dict) -> tuple[str, str] | None:
    """(Yahoo symbol, exchange) from a Trading 212 line; US lines use the current shortName
    (BRK.B / TAP/A -> BRK-B / TAP-A) and fall back to the ticker when it is missing."""
    mapped = to_yahoo(inst["ticker"])
    if mapped and mapped[1] == "US":
        short = (inst.get("shortName") or "").replace(".", "-").replace("/", "-")
        if US_SHORT_NAME.match(short):
            return short, "US"
    return mapped


def yahoo_line(inst: dict) -> tuple[str, str] | None:
    """(Yahoo symbol, exchange) for a Trading 212 line, honouring confirmed overrides.

    None when the line cannot be priced: unmapped, or an override whose identity check fails.
    """
    if inst.get("ticker") in SYMBOL_OVERRIDES:
        if not override_matches(inst):
            return None
        sym, exch, _name, _isin = SYMBOL_OVERRIDES[inst["ticker"]]
        return sym, exch
    return line_symbol(inst)


def _approved_tickers(conn) -> set[str]:
    return {row[0] for row in conn.execute("SELECT ticker FROM universe_members")}


def completed_sessions(hist: pd.DataFrame) -> pd.DataFrame:
    """Drop event-only rows (no OHLC) and an unfinished current-day bar.

    The last bar is dropped when it is dated today in the exchange's own time zone,
    because the market may still be open.
    """
    df = hist.dropna(subset=["Open", "High", "Low", "Close"], how="all")
    if not df.empty:
        last = pd.Timestamp(df.index[-1])
        if last.date() == datetime.now(last.tz or timezone.utc).date():
            df = df.iloc[:-1]
    return df


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
                current_price REAL,
                price_date TEXT,
                year_max REAL,
                year_min REAL,
                all_time_max REAL,
                all_time_min REAL,
                isin TEXT,
                country TEXT,
                verification TEXT,
                quality_flags TEXT,
                status_tag TEXT,
                last_updated TEXT,
                instrument_type TEXT NOT NULL DEFAULT 'STOCK'
            )
            """
        )
        # Migrate a DB that predates any of the newer columns.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(stocks)")}
        migrations = {
            "instrument_type": "ALTER TABLE stocks ADD COLUMN instrument_type TEXT NOT NULL DEFAULT 'STOCK'",
            "current_price": "ALTER TABLE stocks ADD COLUMN current_price REAL",
            "price_date": "ALTER TABLE stocks ADD COLUMN price_date TEXT",
            "isin": "ALTER TABLE stocks ADD COLUMN isin TEXT",
            "country": "ALTER TABLE stocks ADD COLUMN country TEXT",
            "verification": "ALTER TABLE stocks ADD COLUMN verification TEXT",
            "quality_flags": "ALTER TABLE stocks ADD COLUMN quality_flags TEXT",
        }
        for col_name, alter_sql in migrations.items():
            if col_name not in cols:
                conn.execute(alter_sql)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS yahoo_symbols (
                ticker TEXT PRIMARY KEY,
                yahoo_symbol TEXT NOT NULL,
                resolved_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS universe_members (
                ticker TEXT PRIMARY KEY,
                isin TEXT NOT NULL,
                country TEXT NOT NULL,
                rank INTEGER NOT NULL,
                volatility REAL NOT NULL,
                selected_at TEXT NOT NULL
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
    """Pure function: compute all stock metrics from a yfinance history frame.

    Uses split-adjusted OHLC only (auto_adjust=False is applied by the caller).
    Discards event-only rows with no OHLC and excludes an unfinished current-day bar.
    Returns current_price (latest valid close) and price_date instead of opening_price.
    All values are Python floats (float64 preserved); None when unavailable.
    """
    if hist.empty:
        return {
            "current_price": None,
            "price_date": None,
            "year_max": None,
            "year_min": None,
            "all_time_max": None,
            "all_time_min": None,
            "status_tag": "DELISTED_OR_ZERO",
        }

    # Event-only rows and an unfinished current-day bar are not price observations;
    # impossible bars (non-positive, high<low, open/close outside [low,high]) are rejected.
    df = completed_sessions(hist)
    df = df[df.apply(_valid_bar, axis=1)] if not df.empty else df
    if df.empty:
        return {
            "current_price": None,
            "price_date": None,
            "year_max": None,
            "year_min": None,
            "all_time_max": None,
            "all_time_min": None,
            "status_tag": "DELISTED_OR_ZERO",
        }

    # All values as native Python floats (float64 preserved, no float32 artifacts).
    at_max = float(df["High"].max())
    at_min = float(df["Low"].min())

    hist_year = df[df.index.year == year]
    if not hist_year.empty:
        yr_max = float(hist_year["High"].max())
        yr_min = float(hist_year["Low"].min())
        if df.index[0].year == year:
            status_tag = f"NEW_{year}"
        else:
            status_tag = "ACTIVE"
    else:
        yr_max, yr_min = None, None
        status_tag = "ACTIVE"

    current_price = float(df["Close"].iloc[-1])
    price_date = str(df.index[-1].date())

    if current_price <= 0 or len(df) < 5:
        status_tag = "DELISTED_OR_ZERO"

    return {
        "current_price": current_price,
        "price_date": price_date,
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

    def history(self, yahoo: str, period: str = "max") -> pd.DataFrame | None:
        """Return the history for ``period``, or None if Yahoo has no data for the symbol.

        Raises the last error if the symbol still fails after MAX_ATTEMPTS.
        """
        attempts = 0
        while True:
            time.sleep(REQUEST_DELAY)
            try:
                hist = yf.Ticker(yahoo).history(period=period, actions=False, auto_adjust=False)
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

    def _call(self, label: str, fn):
        """Run one paced Yahoo request with the same back-off as history(); raise when it
        still fails after MAX_ATTEMPTS."""
        attempts = 0
        while True:
            time.sleep(REQUEST_DELAY)
            try:
                return fn()
            except YFRateLimitError:
                log.warning("Yahoo rate limit hit; waiting %ds before %s", self.backoff, label)
                time.sleep(self.backoff)
                self.backoff = min(self.backoff * 2, MAX_RATE_LIMIT_BACKOFF)
            except YFTickerMissingError:
                return None
            except Exception as exc:
                if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                    return None  # Yahoo does not know the symbol: not a provider failure
                attempts += 1
                if attempts >= MAX_ATTEMPTS:
                    raise
                time.sleep(2 ** attempts)

    def search(self, query: str) -> list[dict]:
        """Yahoo quote search results (symbol, exchange, names) for an ISIN or name."""
        return self._call("search", lambda: yf.Search(query, max_results=10, news_count=0).quotes) or []

    def info(self, yahoo: str) -> dict:
        """Yahoo company metadata (name, country, exchange, currency) for a symbol."""
        return self._call("info " + yahoo, lambda: yf.Ticker(yahoo).info) or {}

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


def _upsert(cursor, inst: dict, exchange: str, metrics: dict, instrument_type: str,
            isin: str | None = None, country: str | None = None,
            verification: str | None = None, quality_flags: str | None = None) -> None:
    cursor.execute(
        """
        INSERT INTO stocks (ticker, name, exchange, opening_price, current_price, price_date,
                            year_max, year_min, all_time_max, all_time_min,
                            isin, country, verification, quality_flags,
                            status_tag, last_updated, instrument_type)
        VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
        ON CONFLICT(ticker) DO UPDATE SET
            name=excluded.name,
            exchange=excluded.exchange,
            current_price=excluded.current_price,
            price_date=excluded.price_date,
            year_max=excluded.year_max,
            year_min=excluded.year_min,
            all_time_max=excluded.all_time_max,
            all_time_min=excluded.all_time_min,
            isin=excluded.isin,
            country=excluded.country,
            verification=excluded.verification,
            quality_flags=excluded.quality_flags,
            status_tag=excluded.status_tag,
            last_updated=datetime('now'),
            instrument_type=excluded.instrument_type
        """,
        (
            inst["ticker"],
            inst.get("name"),
            exchange,
            metrics.get("current_price"),
            metrics.get("price_date"),
            metrics.get("year_max"),
            metrics.get("year_min"),
            metrics.get("all_time_max"),
            metrics.get("all_time_min"),
            isin,
            country,
            verification,
            quality_flags,
            metrics.get("status_tag"),
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

    Only approved members (the ``universe_members`` table) are synced. With no approved
    members the sync returns at once, without calling Trading 212 or Yahoo.

    A confirmed symbol override is used
    first; otherwise a saved ISIN match, the symbol from the T212 ticker, or for unmappable
    tickers an ISIN lookup. A symbol with no data is retried once through the ISIN. An ISIN match
    is saved only once Yahoo returned prices for it, and a saved match without data is dropped.

    Returns a summary dict with counts and timestamps: ok, no_data (Yahoo has no prices for
    the symbol), invalid (prices broke the ordering rule), failed (still erroring after every
    retry pass, listed in failed_tickers), unmapped (no Yahoo mapping and no ISIN match) and
    resolved_by_isin (instruments newly matched through their ISIN this run).
    ok + no_data + invalid + failed equals the number of instruments that had a Yahoo symbol
    to try.
    """
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        members = _approved_tickers(conn)
    finally:
        conn.close()
    if not members:
        log.info("No approved universe members; not calling Yahoo")
        now = datetime.now(timezone.utc).isoformat()
        return {"ok": 0, "no_data": 0, "invalid": 0, "failed": 0, "unmapped": 0,
                "resolved_by_isin": 0, "failed_tickers": [], "started_at": now, "finished_at": now}

    instruments = fetch_instruments()

    keep = [i for i in instruments if i.get("type") == "STOCK" and i["ticker"] in members]
    missing_members = len(members) - len(keep)

    yahoo = _Yahoo()
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        current_year = datetime.now().year

        saved = dict(conn.execute("SELECT ticker, yahoo_symbol FROM yahoo_symbols").fetchall())

        # T212 ticker -> [instrument, yahoo symbol, exchange, source, expected exchange, quality flag].
        # Keyed by T212 ticker, so two instruments sharing one Yahoo symbol both get a row.
        # source: "override" (confirmed Phase 1 correction), "saved" (yahoo_symbols),
        # "mapped" (from the ticker) or "isin" (new match).
        work: dict[str, list] = {}
        isin_tried: set[str] = set()
        unmapped_count = 0
        for inst in keep:
            ticker = inst["ticker"]
            # Confirmed symbol override takes precedence; name must match the instrument.
            if ticker in SYMBOL_OVERRIDES:
                ov_sym, ov_exch, _name, _isin = SYMBOL_OVERRIDES[ticker]
                if override_matches(inst):
                    work[ticker] = [inst, ov_sym, ov_exch, "override", None, "OVERRIDE"]
                else:
                    # Never fall back to the known-wrong symbol.
                    log.warning("Symbol override for %s does not match the instrument; skipped", ticker)
                    work[ticker] = [inst, None, ov_exch, "override", None, "OVERRIDE_MISMATCH"]
                continue
            mapped = line_symbol(inst)
            expected = mapped[1] if mapped else None
            saved_sym = saved.get(ticker)
            saved_exchange = _isin_acceptable(saved_sym, expected)
            if saved_exchange:
                work[ticker] = [inst, saved_sym, saved_exchange, "saved", expected, ""]
            elif mapped:
                work[ticker] = [inst, mapped[0], mapped[1], "mapped", expected, ""]
            else:
                isin_tried.add(ticker)
                sym = _resolve_isin(yahoo, inst, None)
                if sym:
                    work[ticker] = [inst, sym[0], sym[1], "isin", None, ""]
                else:
                    unmapped_count += 1

        summary = {
            "ok": 0,
            "no_data": 0,
            "invalid": 0,
            "failed": 0,
            "unmapped": unmapped_count + missing_members,
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
        # Still failing after every retry pass: flag the stored rows like a no-data response.
        for ticker in pending:
            _keep_last_good(cursor, work[ticker], "STALE", "FAILED")
        conn.commit()
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
    """Sync one instrument; returns "ok", "no_data" or "invalid", raises on errors (retried later).

    entry is updated in place, so a retry pass reuses an ISIN match found earlier.
    Without valid new values, an existing row keeps its last good values, or loses legacy
    values that break the ordering rule, and is flagged. No new row is created then.
    """
    inst, symbol, exchange, source, expected, quality_flag = entry
    ticker = inst["ticker"]

    hist = yahoo.history(symbol) if symbol else None
    if hist is None and source == "saved":
        conn.execute("DELETE FROM yahoo_symbols WHERE ticker=?", (ticker,))
    if hist is None and source != "override" and ticker not in isin_tried:
        isin_tried.add(ticker)
        match = _resolve_isin(yahoo, inst, expected)
        if match and match[0] != symbol:
            entry[1:4] = [match[0], match[1], "isin"]
            symbol, exchange = match
            hist = yahoo.history(symbol)

    metrics = compute_metrics(hist, current_year) if hist is not None else None
    if metrics is not None and _ordering_ok(metrics):
        if entry[3] == "isin":
            _save_symbol(conn, ticker, symbol)
        _upsert(cursor, inst, exchange, metrics, inst.get("type", "STOCK"),
                quality_flags=quality_flag or None, **_identity(inst))
        return "ok"

    if metrics is None:
        _keep_last_good(cursor, entry, "STALE", "NO_DATA")
        return "no_data"
    _keep_last_good(cursor, entry, "INVALID_VALUES", "INVALID")
    return "invalid"


def _identity(inst: dict) -> dict:
    isin = inst.get("isin") or None
    return {"isin": isin, "country": isin[:2] if isin else None, "verification": "SINGLE_SOURCE"}


def _keep_last_good(cursor, entry: list, status: str, flag: str) -> None:
    """Flag an existing row without new valid values: keep its last good values, or clear
    values that break the ordering rule (legacy values become missing). Creates no row."""
    inst, _symbol, exchange, _source, _expected, quality_flag = entry
    ticker = inst["ticker"]
    row = cursor.execute(
        "SELECT current_price, price_date, year_max, year_min, all_time_max, all_time_min "
        "FROM stocks WHERE ticker=?", (ticker,)
    ).fetchone()
    if row:
        keys = ("current_price", "price_date", "year_max", "year_min", "all_time_max", "all_time_min")
        kept = dict(zip(keys, row))
        if not _ordering_ok(kept):
            # Legacy or invalid stored values are not good data: they become missing.
            kept = dict.fromkeys(keys)
        kept["status_tag"] = status
        flags = ":".join(f for f in (quality_flag, flag) if f)
        _upsert(cursor, inst, exchange, kept, inst.get("type", "STOCK"), quality_flags=flags,
                **_identity(inst))

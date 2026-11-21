import logging
import os
import re
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

DB_PATH = os.environ.get("CRYPTO_DB_PATH", "crypto.db")

# Fixed HTTPS host so the API key is never sent elsewhere.
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
MARKETS_ENDPOINT = "/coins/markets"

# CoinGecko Demo pacing: at most 24 calls a minute, so at least 2.5 s between calls.
REQUEST_TIMEOUT = 30
REQUEST_DELAY = 2.5
RATE_LIMIT_BACKOFF = 60
MAX_RATE_LIMIT_BACKOFF = 15 * 60
MAX_ATTEMPTS = 3
PER_PAGE = 250

VS_CURRENCY_PATTERN = re.compile(r"^[a-z]{3,5}$")
DEFAULT_VS_CURRENCY = "usd"
DEFAULT_HISTORY_CALLS_PER_RUN = 200
MAX_HISTORY_CALLS_PER_RUN = 1000

STATUS_ACTIVE = "ACTIVE"
STATUS_DELISTED_OR_ZERO = "DELISTED_OR_ZERO"


def init_db(db_path: str = DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS crypto (
                ticker TEXT PRIMARY KEY,
                symbol TEXT,
                name TEXT,
                market_cap_rank INTEGER,
                exchange TEXT,
                opening_price REAL,
                year_max REAL,
                year_min REAL,
                year_ref INTEGER,
                history_year INTEGER,
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


def compute_status(price, total_volume) -> str:
    if price is None or price <= 0:
        return STATUS_DELISTED_OR_ZERO
    if total_volume is not None and total_volume <= 0:
        return STATUS_DELISTED_OR_ZERO
    return STATUS_ACTIVE


class _CoinGecko:
    """CoinGecko Demo client: paced calls, 429 back-off, key only in a header."""

    def __init__(self):
        key = os.environ.get("COINGECKO_API_KEY")
        if not key:
            raise ValueError("COINGECKO_API_KEY environment variable is required; no request made.")
        self.key = key
        self.backoff = RATE_LIMIT_BACKOFF
        self.calls = 0
        self._last_call = 0.0

    def _get(self, path, params=None):
        now = time.monotonic()
        wait = REQUEST_DELAY - (now - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

        headers = {"x-cg-demo-api-key": self.key}
        attempts = 0
        while True:
            self.calls += 1
            try:
                resp = requests.get(COINGECKO_BASE + path, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                # Timeouts and connection resets get the same attempts as HTTP errors.
                attempts += 1
                if attempts >= MAX_ATTEMPTS:
                    log.warning("CoinGecko %s failed after %d attempts: %s", path, attempts, type(exc).__name__)
                    raise
                time.sleep(2 ** attempts)
                continue
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait_s = int(retry_after) if retry_after and retry_after.isdigit() else self.backoff
                wait_s = max(wait_s, REQUEST_DELAY)  # never retry a 429 without a pause
                self.backoff = min(self.backoff * 2, MAX_RATE_LIMIT_BACKOFF)
                log.warning("CoinGecko rate limit hit; waiting %ds before retrying %s", wait_s, path)
                time.sleep(wait_s)
                continue
            if resp.status_code >= 400:
                attempts += 1
                if attempts >= MAX_ATTEMPTS:
                    log.warning("CoinGecko %s failed after %d attempts: %s", path, attempts, resp.status_code)
                    resp.raise_for_status()
                time.sleep(2 ** attempts)
                continue
            self.backoff = RATE_LIMIT_BACKOFF
            return resp.json()

    def fetch_markets(self, vs_currency):
        """All market list pages, stopping at the first empty page.

        Pages are ordered by id, which does not change during the run (market-cap order does,
        and would skip or repeat coins that move between pages). Duplicates are dropped.
        """
        coins = {}
        page = 1
        while True:
            data = self._get(MARKETS_ENDPOINT, {
                "vs_currency": vs_currency,
                "order": "id_asc",
                "per_page": PER_PAGE,
                "page": page,
            })
            if not data:
                break
            for coin in data:
                if coin.get("id"):
                    coins.setdefault(coin["id"], coin)
            page += 1
        return list(coins.values())

    def fetch_year_range(self, coin_id, vs_currency, year):
        """Daily prices for one coin; returns (year_max, year_min) for ``year``."""
        path = "/coins/" + urllib.parse.quote(coin_id, safe="") + "/market_chart"
        data = self._get(path, {"vs_currency": vs_currency, "days": "365"})
        year_max = None
        year_min = None
        for ts_ms, price in data.get("prices", []) or []:
            if price is None:
                continue
            if datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).year != year:
                continue
            price = float(price)
            year_max = price if year_max is None else max(year_max, price)
            year_min = price if year_min is None else min(year_min, price)
        return year_max, year_min


def run_sync(db_path: str = DB_PATH, history_cap: int | None = None) -> dict:
    """Fill the crypto table from CoinGecko and return a summary dict."""
    init_db(db_path)
    vs_currency = os.environ.get("CRYPTO_VS_CURRENCY", DEFAULT_VS_CURRENCY)
    if not VS_CURRENCY_PATTERN.match(vs_currency):
        raise ValueError(f"CRYPTO_VS_CURRENCY must match '^[a-z]{{3,5}}$', got {vs_currency!r}")
    if history_cap is None:
        try:
            history_cap = int(os.environ.get("CRYPTO_HISTORY_CALLS_PER_RUN", str(DEFAULT_HISTORY_CALLS_PER_RUN)))
        except (TypeError, ValueError):
            raise ValueError(f"CRYPTO_HISTORY_CALLS_PER_RUN must be an integer 0..{MAX_HISTORY_CALLS_PER_RUN}")
    if not (0 <= history_cap <= MAX_HISTORY_CALLS_PER_RUN):
        raise ValueError(f"CRYPTO_HISTORY_CALLS_PER_RUN must be an integer 0..{MAX_HISTORY_CALLS_PER_RUN}")

    client = _CoinGecko()  # raises ValueError if the key is missing, before any request
    started_at = datetime.now(timezone.utc).isoformat()

    coins = client.fetch_markets(vs_currency)

    current_year = datetime.now().year
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        existing = {
            row[0]: (row[1], row[2], row[3], row[4])
            for row in conn.execute("SELECT ticker, year_ref, year_max, year_min, history_year FROM crypto")
        }

        backfill_candidates = []
        for n, coin in enumerate(coins, 1):
            cid = coin.get("id")
            price = coin.get("current_price")
            high_24h = coin.get("high_24h")
            low_24h = coin.get("low_24h")
            volume = coin.get("total_volume")
            rank = coin.get("market_cap_rank")

            yr, y_max, y_min, history_year = existing.get(cid, (None, None, None, None))
            if yr != current_year:
                y_max, y_min = None, None
            if high_24h is not None:
                y_max = high_24h if y_max is None else max(y_max, high_24h)
            if low_24h is not None:
                y_min = low_24h if y_min is None else min(y_min, low_24h)

            cursor.execute(
                """
                INSERT INTO crypto (ticker, symbol, name, market_cap_rank, exchange,
                                    opening_price, year_max, year_min, year_ref,
                                    all_time_max, all_time_min, status_tag, last_updated)
                VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(ticker) DO UPDATE SET
                    symbol=excluded.symbol,
                    name=excluded.name,
                    market_cap_rank=excluded.market_cap_rank,
                    opening_price=excluded.opening_price,
                    year_max=excluded.year_max,
                    year_min=excluded.year_min,
                    year_ref=excluded.year_ref,
                    all_time_max=excluded.all_time_max,
                    all_time_min=excluded.all_time_min,
                    status_tag=excluded.status_tag,
                    last_updated=datetime('now')
                """,
                (
                    cid,
                    coin.get("symbol"),
                    coin.get("name"),
                    rank,
                    price,
                    y_max,
                    y_min,
                    current_year,
                    coin.get("ath"),
                    coin.get("atl"),
                    compute_status(price, volume),
                ),
            )
            if n % 100 == 0:
                conn.commit()
            # One history call per coin per year; the 24h values keep widening it afterwards.
            if history_year != current_year:
                backfill_candidates.append((rank, cid))
        conn.commit()

        backfill_candidates.sort(key=lambda item: (item[0] is None, item[0] or 0))
        backfilled = 0
        failed = 0
        for rank, cid in backfill_candidates[:history_cap]:
            try:
                y_max, y_min = client.fetch_year_range(cid, vs_currency, current_year)
                # Merge with the stored values, which already include today's 24h high/low.
                cursor.execute(
                    """
                    UPDATE crypto SET
                        year_max = CASE WHEN ? IS NULL THEN year_max WHEN year_max IS NULL THEN ? ELSE max(year_max, ?) END,
                        year_min = CASE WHEN ? IS NULL THEN year_min WHEN year_min IS NULL THEN ? ELSE min(year_min, ?) END,
                        history_year = ?
                    WHERE ticker = ?
                    """,
                    (y_max, y_max, y_max, y_min, y_min, y_min, current_year, cid),
                )
                backfilled += 1
            except Exception as exc:
                log.warning("History for %s failed: %s", cid, type(exc).__name__)
                failed += 1
        conn.commit()
    finally:
        conn.close()

    return {
        "coins": len(coins),
        "backfilled": backfilled,
        "backfill_pending": max(0, len(backfill_candidates) - backfilled - failed),
        "failed": failed,
        "calls": client.calls,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }

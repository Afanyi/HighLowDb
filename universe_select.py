"""Manual stock-universe selection (CLI only; there is no web route).

    python universe_select.py select [--db PATH] [--out-dir DIR] [--resume]
    python universe_select.py quick [--db PATH] [--out-dir DIR]
    python universe_select.py approve REVIEW_CSV [--db PATH]

``select`` screens every eligible Trading 212 stock on its verified home listing and writes a
private review CSV plus a country summary. It never changes membership. ``approve`` loads the
SELECTED rows of a reviewed CSV into ``universe_members`` in one transaction.

Prices are held in memory per stock and discarded after scoring. ECB euro reference rates
(converted values, not republished) are used only for the traded-value and price gates.
"""

import argparse
import bisect
import csv
import io
import json
import logging
import math
import os
import re
import sqlite3
import sys
import unicodedata
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

import refresh
import sync

log = logging.getLogger("universe_select")

# Square-root MSCI ACWI IMI developed-market quotas (demo-derived, Phase 1). Ceilings, total 1,000.
COUNTRY_QUOTAS = {
    "US": 200, "JP": 104, "GB": 77, "CA": 76, "FR": 62, "CH": 60, "DE": 59, "AU": 54,
    "NL": 50, "ES": 39, "SE": 39, "IL": 26, "BE": 23, "DK": 23, "FI": 22, "NO": 19,
    "AT": 14, "IE": 12, "SG": 12, "IT": 10, "PT": 10, "NZ": 6, "HK": 3,
}
# ISIN country -> Yahoo company country, and the Yahoo suffix of that country's home exchange.
COUNTRY_NAMES = {
    "US": "United States", "JP": "Japan", "GB": "United Kingdom", "CA": "Canada",
    "FR": "France", "CH": "Switzerland", "DE": "Germany", "AU": "Australia",
    "NL": "Netherlands", "ES": "Spain", "SE": "Sweden", "IL": "Israel", "BE": "Belgium",
    "DK": "Denmark", "FI": "Finland", "NO": "Norway", "AT": "Austria", "IE": "Ireland",
    "SG": "Singapore", "IT": "Italy", "PT": "Portugal", "NZ": "New Zealand", "HK": "Hong Kong",
}
HOME_SUFFIXES = {
    "US": "", "JP": ".T", "GB": ".L", "CA": ".TO", "FR": ".PA", "CH": ".SW", "DE": ".DE",
    "AU": ".AX", "NL": ".AS", "ES": ".MC", "SE": ".ST", "IL": ".TA", "BE": ".BR", "DK": ".CO",
    "FI": ".HE", "NO": ".OL", "AT": ".VI", "IE": ".IR", "SG": ".SI", "IT": ".MI", "PT": ".LS",
    "NZ": ".NZ", "HK": ".HK",
}
# Yahoo exchange codes of the US national exchanges (no OTC markets).
US_EXCHANGES = {"NMS", "NGM", "NCM", "NYQ", "ASE", "PCX", "BTS"}

WINDOW = 63                 # scoring sessions
IPO_SKIP = 5                # a new listing skips its first 5 sessions (68 needed)
MIN_VALID_SESSIONS = 57
MAX_HIGH_LOW = 3.0          # bad tick: high/low of at least 3
MAX_GAP = 3.0               # split-like jump: overnight gap of 3x or more either way
MEMBER_BUFFER = 1.3
FX_MAX_AGE_DAYS = 7         # a session uses the latest ECB rate at most 7 days old, never a later one
HISTORY_PERIOD = "1y"
RESUME_MAX_AGE_DAYS = 7     # older screening progress is discarded
# Gates for new entrants and the softer ones for current members (starting values).
GATES = {
    False: {"traded_value_eur": 1_000_000, "trading_days": 0.90, "zero_volume": 6,
            "zero_return": 0.20, "price_usd": 2.0},
    True: {"traded_value_eur": 750_000, "trading_days": 0.85, "zero_volume": 9,
           "zero_return": 0.25, "price_usd": 1.5},
}
MINOR_UNITS = {"GBX": "GBP", "GBp": "GBP", "ILA": "ILS"}   # 100 minor units per major unit

ECB_HISTORY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
ISIN_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
TICKER_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")
NAME_STOPWORDS = {
    "the", "and", "inc", "incorporated", "corp", "corporation", "co", "company", "plc", "ltd",
    "limited", "ag", "sa", "se", "nv", "asa", "ab", "as", "oyj", "spa", "kgaa", "holding",
    "holdings", "group", "class", "publ", "a", "b", "c", "s", "n", "v", "p", "de", "adr",
}
CSV_COLUMNS = [
    "status", "country", "rank", "isin", "t212_ticker", "t212_name", "line_choice",
    "home_symbol", "currency", "volatility", "median_traded_value_eur", "price_usd",
    "last_session", "member", "reason",
]


# --- FX -------------------------------------------------------------------------------------

def parse_ecb_csv(text: str) -> dict[str, tuple[list[date], list[float]]]:
    """ECB history CSV -> currency -> (sorted dates, units per EUR)."""
    series: dict[str, list[tuple[date, float]]] = {}
    for row in csv.DictReader(io.StringIO(text)):
        day = date.fromisoformat(row.pop("Date").strip())
        for currency, value in row.items():
            currency = (currency or "").strip()
            try:
                rate = float(value)
            except (TypeError, ValueError):
                continue  # "N/A" or blank
            if currency and rate > 0:
                series.setdefault(currency, []).append((day, rate))
    return {c: ([d for d, _ in sorted(v)], [r for _, r in sorted(v)]) for c, v in series.items()}


def fetch_ecb_rates() -> dict[str, tuple[list[date], list[float]]]:
    resp = requests.get(ECB_HISTORY_URL, timeout=60)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".csv"))
        return parse_ecb_csv(archive.read(name).decode("utf-8"))


def _units_per_eur(fx, currency: str, day: date) -> float | None:
    """ECB rate on ``day`` or the latest earlier one at most FX_MAX_AGE_DAYS old."""
    if currency == "EUR":
        return 1.0
    if currency not in fx:
        return None
    dates, rates = fx[currency]
    i = bisect.bisect_right(dates, day) - 1
    if i < 0 or (day - dates[i]).days > FX_MAX_AGE_DAYS:
        return None
    return rates[i]


def to_eur(amount: float, currency: str | None, fx, day: date) -> float | None:
    """Convert an amount in a listing currency to EUR; minor units (GBX/GBp, ILA) first."""
    if not currency:
        return None
    if currency in MINOR_UNITS:
        amount, currency = amount / 100.0, MINOR_UNITS[currency]
    rate = _units_per_eur(fx, currency, day)
    return None if rate is None else amount / rate


def to_usd(amount: float, currency: str | None, fx, day: date) -> float | None:
    eur = to_eur(amount, currency, fx, day)
    usd = _units_per_eur(fx, "USD", day)
    return None if eur is None or usd is None else eur * usd


# --- Volatility and gates --------------------------------------------------------------------

def _clip(x: np.ndarray) -> np.ndarray:
    """Clip at median ± 6 scaled MAD."""
    med = np.median(x)
    mad = 1.4826 * np.median(np.abs(x - med))
    return np.clip(x, med - 6 * mad, med + 6 * mad)


def ryz63(df: pd.DataFrame) -> float | None:
    """Annualised Yang-Zhang volatility with each daily component clipped (RYZ63)."""
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("Open", "High", "Low", "Close"))
    overnight = _clip(np.log(o[1:] / c[:-1]))
    open_close = _clip(np.log(c[1:] / o[1:]))
    h, l, o, c = h[1:], l[1:], o[1:], c[1:]
    rogers_satchell = _clip(np.log(h / c) * np.log(h / o) + np.log(l / c) * np.log(l / o))
    n = len(overnight)
    if n < 2:
        return None
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    variance = (np.var(overnight, ddof=1) + k * np.var(open_close, ddof=1)
                + (1 - k) * np.mean(rogers_satchell))
    return float(math.sqrt(max(variance, 0.0) * 252))


def screen_history(hist: pd.DataFrame, currency: str | None, fx, member: bool,
                   window_start: date) -> tuple[dict | None, str | None]:
    """Apply the gates to the latest 63 completed home-listing sessions.

    Returns (metrics, None) for an eligible stock, otherwise (None, reason). ``window_start``
    is the first day of the requested history; a first session well after it marks a new
    listing, whose first IPO_SKIP sessions are skipped.
    """
    df = sync.completed_sessions(hist)
    if df.empty:
        return None, "no_data"
    if pd.Timestamp(df.index[0]).date() > window_start + timedelta(days=14):
        df = df.iloc[IPO_SKIP:]
    if len(df) < WINDOW:
        return None, "insufficient_sessions"
    w = df.iloc[-WINDOW:]
    complete = w[w[["Open", "High", "Low", "Close"]].notna().all(axis=1)].copy()
    prices = complete[["Open", "High", "Low", "Close"]]
    if (prices <= 0).any().any() or (complete["High"] < complete["Low"]).any():
        return None, "invalid_ohlc"
    # Feeds (notably London auctions) can print an open or close just outside the day's
    # high/low; widen the range to cover them instead of rejecting the stock.
    complete["High"] = prices.max(axis=1)
    complete["Low"] = prices.min(axis=1)
    if len(complete) < MIN_VALID_SESSIONS:
        return None, "too_few_valid_sessions"
    if (complete["High"] / complete["Low"] >= MAX_HIGH_LOW).any():
        return None, "bad_tick"
    gaps = complete["Open"].to_numpy()[1:] / complete["Close"].to_numpy()[:-1]
    if ((gaps >= MAX_GAP) | (gaps <= 1 / MAX_GAP)).any():
        return None, "split_like_jump"

    gate = GATES[member]
    volume = (w["Volume"] if "Volume" in w else pd.Series(0.0, index=w.index)).fillna(0)
    zero_volume = int((volume <= 0).sum())
    if zero_volume > gate["zero_volume"]:
        return None, "too_many_zero_volume_days"
    if (WINDOW - zero_volume) / WINDOW < gate["trading_days"]:
        return None, "too_few_trading_days"
    closes = complete["Close"].to_numpy()
    if np.mean(closes[1:] == closes[:-1]) > gate["zero_return"]:
        return None, "too_many_zero_return_days"

    traded = []
    for ts, close in complete["Close"].items():
        value = to_eur(float(close) * float(volume[ts]), currency, fx, pd.Timestamp(ts).date())
        if value is None:
            return None, "missing_fx"
        traded.append(value)
    median_traded = float(np.median(traded))
    if median_traded < gate["traded_value_eur"]:
        return None, "illiquid"
    last_day = pd.Timestamp(complete.index[-1]).date()
    price_usd = to_usd(float(closes[-1]), currency, fx, last_day)
    if price_usd is None:
        return None, "missing_fx"
    if price_usd < gate["price_usd"]:
        return None, "price_too_low"
    volatility = ryz63(complete)
    if volatility is None:
        return None, "no_volatility"
    return {"volatility": volatility, "median_traded_value_eur": median_traded,
            "price_usd": price_usd, "last_session": last_day.isoformat()}, None


# --- Home listing ----------------------------------------------------------------------------

def _name_tokens(name: str | None) -> list[str]:
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().casefold()
    return [t for t in re.findall(r"[a-z0-9]+", text) if t not in NAME_STOPWORDS]


def names_agree(a: str | None, b: str | None) -> bool:
    """Every significant word of the shorter name appears in the other."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return False
    short, long_ = sorted((ta, tb), key=len)
    return set(short) <= set(long_)


def is_home_symbol(symbol: str, country: str) -> bool:
    suffix = HOME_SUFFIXES[country]
    if not suffix:
        return bool(symbol) and "." not in symbol
    return symbol.endswith(suffix) and "." not in symbol[: -len(suffix)]


def _identity_ok(info: dict, country: str, names: list[str]) -> bool:
    if info.get("quoteType") != "EQUITY" or info.get("country") != COUNTRY_NAMES[country]:
        return False
    if country == "US" and info.get("exchange") not in US_EXCHANGES:
        return False
    yahoo_names = [info.get("longName"), info.get("shortName")]
    return any(names_agree(n, y) for n in names for y in yahoo_names)


def resolve_home(yahoo, isin: str, country: str, lines: list[dict]) -> tuple[str | None, str | None, str | None]:
    """Return (home symbol, listing currency, None) or (None, None, reason).

    Candidate sources, in order: Trading 212 lines already on the home exchange, Yahoo's ISIN
    search, then a search by the Trading 212 company name. A candidate is accepted only if
    Yahoo's company country, exchange and name agree. The first source with an accepted
    candidate decides, and it must have exactly one.
    """
    names = [line.get("name") for line in lines]

    def home_quotes(query):
        return [q["symbol"] for q in yahoo.search(query)
                if q.get("quoteType") == "EQUITY" and is_home_symbol(q.get("symbol") or "", country)]

    sources = [lambda: [m[0] for m in map(sync.yahoo_line, lines) if m and is_home_symbol(m[0], country)],
               lambda: home_quotes(isin)]
    if names[0]:
        sources.append(lambda: home_quotes(names[0]))
    tried, found = set(), False
    for source in sources:
        symbols = sorted(set(source()) - tried)[:5]
        tried |= set(symbols)
        found = found or bool(symbols)
        accepted = []
        for symbol in symbols:
            info = yahoo.info(symbol)
            if _identity_ok(info, country, names):
                accepted.append((symbol, info.get("currency")))
        if len(accepted) == 1:
            return accepted[0][0], accepted[0][1], None
        if accepted:
            return None, None, "ambiguous_home_listing"
    return None, None, "unverified_home_listing" if found else "no_home_listing"


def choose_line(yahoo, fx, lines: list[dict], home_symbol: str) -> tuple[dict, str]:
    """One Trading 212 line per ISIN: the home-exchange line if offered, otherwise the line
    with the highest median EUR traded value (ties: lowest ticker)."""
    lines = sorted(lines, key=lambda line: line["ticker"])
    for line in lines:
        if sync.yahoo_line(line)[0] == home_symbol:
            return line, "home"
    if len(lines) == 1:
        return lines[0], "only_line"
    best, best_value = lines[0], -1.0
    for line in lines:
        value = 0.0
        try:
            hist = yahoo.history(sync.yahoo_line(line)[0], period="6mo")
        except Exception as exc:  # noqa: BLE001
            log.warning("Line history failed for %s: %s", line["ticker"], type(exc).__name__)
            hist = None
        if hist is not None:
            df = sync.completed_sessions(hist).iloc[-WINDOW:]
            values = [to_eur(float(c) * float(v), line.get("currencyCode"), fx, pd.Timestamp(ts).date())
                      for ts, c, v in zip(df.index, df["Close"], df.get("Volume", pd.Series(0.0, index=df.index)))
                      if pd.notna(c) and pd.notna(v)]
            values = [x for x in values if x is not None]
            value = float(np.median(values)) if values else 0.0
        if value > best_value:
            best, best_value = line, value
    return best, "traded_value"


# --- Selection -------------------------------------------------------------------------------

def candidate_groups(instruments: list[dict]) -> tuple[dict[str, list[dict]], dict[str, int], dict[str, str]]:
    """Group Trading 212 stocks by ISIN in the 23 countries.

    Returns (ISIN -> priceable lines, country -> unique-ISIN supply, ISIN -> exclusion reason
    for ISINs without a priceable Trading 212 line). ETFs, warrants and funds are dropped.
    """
    groups: dict[str, list[dict]] = {}
    for inst in instruments:
        isin = inst.get("isin") or ""
        if inst.get("type") == "STOCK" and ISIN_PATTERN.match(isin) and isin[:2] in COUNTRY_QUOTAS:
            groups.setdefault(isin, []).append(inst)
    supply: dict[str, int] = {}
    for isin in groups:
        supply[isin[:2]] = supply.get(isin[:2], 0) + 1
    priceable, unpriceable = {}, {}
    for isin in sorted(groups):
        lines = [line for line in groups[isin] if sync.yahoo_line(line)]
        if lines:
            priceable[isin] = lines
        else:
            unpriceable[isin] = "no_priceable_t212_line"
    return priceable, supply, unpriceable


def screen_isin(yahoo, fx, isin: str, lines: list[dict], member: bool, window_start: date) -> dict:
    result = {"isin": isin, "country": isin[:2], "member": member, "eligible": False,
              "ticker": sorted(line["ticker"] for line in lines)[0], "name": lines[0].get("name")}
    symbol, currency, reason = resolve_home(yahoo, isin, isin[:2], lines)
    result.update(home_symbol=symbol, currency=currency)
    if reason:
        return {**result, "reason": reason}
    hist = yahoo.history(symbol, period=HISTORY_PERIOD)
    if hist is None:
        return {**result, "reason": "no_data"}
    metrics, reason = screen_history(hist, currency, fx, member, window_start)
    del hist  # history is never kept beyond scoring
    if reason:
        return {**result, "reason": reason}
    return {**result, **metrics, "eligible": True, "reason": ""}


def allocate(results: list[dict], supply: dict[str, int]) -> tuple[list[dict], list[dict]]:
    """Fill each country's quota by RYZ63 rank.

    Eligible current members ranked within 1.3 x quota are retained first (best first), then
    the remaining seats go to the best other eligible stocks. Seats never exceed the quota.
    Returns (selected rows with rank, per-country summary).
    """
    selected, summary = [], []
    for country, quota in COUNTRY_QUOTAS.items():
        ranked = sorted((r for r in results if r["eligible"] and r["country"] == country),
                        key=lambda r: (-r["volatility"], r["isin"]))
        buffer = math.floor(quota * MEMBER_BUFFER)
        retained = [r for i, r in enumerate(ranked, 1) if r["member"] and i <= buffer][:quota]
        kept = {r["isin"] for r in retained}
        seats = retained + [r for r in ranked if r["isin"] not in kept][:quota - len(retained)]
        seats.sort(key=lambda r: (-r["volatility"], r["isin"]))
        selected += [{**r, "rank": i} for i, r in enumerate(seats, 1)]
        unfilled = quota - len(seats)
        reason = "" if not unfilled else (
            "supply" if supply.get(country, 0) < quota else "too_few_pass_gates")
        summary.append({"country": country, "quota": quota, "supply": supply.get(country, 0),
                        "eligible": len(ranked), "selected": len(seats), "unfilled": unfilled,
                        "reason": reason})
    return selected, summary


# --- Private files ---------------------------------------------------------------------------

def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _open_private(path: Path, mode: str = "w"):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, mode, newline="", encoding="utf-8")


def csv_cell(value) -> str:
    """CSV text that a spreadsheet will not run as a formula."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.10g}"
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _uncell(text: str) -> str:
    return text[1:] if text[:2] in ("'=", "'+", "'-", "'@", "'\t", "'\r") else text


def write_review(out_dir: Path, stamp: str, selected: list[dict], results: list[dict],
                 summary: list[dict]) -> tuple[Path, Path]:
    review = out_dir / f"universe_review_{stamp}.csv"
    countries = out_dir / f"universe_countries_{stamp}.csv"
    chosen = {r["isin"] for r in selected}
    rows = [("SELECTED", r) for r in selected]
    rows += [("RESERVE", {**r, "reason": "out_of_quota"})
             for r in sorted(results, key=lambda r: (r["country"], -r.get("volatility", 0), r["isin"]))
             if r["eligible"] and r["isin"] not in chosen]
    rows += [("EXCLUDED", r) for r in sorted(results, key=lambda r: r["isin"]) if not r["eligible"]]
    with _open_private(review) as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_COLUMNS)
        for status, r in rows:
            values = {"status": status, "t212_ticker": r.get("ticker"), "t212_name": r.get("name"), **r}
            writer.writerow([csv_cell(values.get(col)) for col in CSV_COLUMNS])
    with _open_private(countries) as fh:
        writer = csv.writer(fh)
        columns = ["country", "quota", "supply", "eligible", "selected", "unfilled", "reason"]
        writer.writerow(columns)
        for row in summary:
            writer.writerow([csv_cell(row[c]) for c in columns])
    return review, countries


def _save_progress(path: Path, started: str, results: dict) -> None:
    tmp = path.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    with _open_private(tmp) as fh:
        json.dump({"started": started, "results": results}, fh)
    os.replace(tmp, path)


# --- Commands --------------------------------------------------------------------------------

def _member_isins(db_path: str) -> set[str]:
    sync.init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        return {row[0] for row in conn.execute("SELECT isin FROM universe_members")}
    finally:
        conn.close()


def run_select(db_path: str, out_dir: Path, resume: bool = False, today: date | None = None,
               yahoo=None, fx=None) -> dict:
    """Screen, rank and write the private review files. Never changes membership."""
    today = today or datetime.now(timezone.utc).date()
    out_dir = _private_dir(out_dir)
    progress_path = out_dir / "selection_progress.json"
    done = {}
    if resume and progress_path.exists():
        saved = json.loads(progress_path.read_text(encoding="utf-8"))
        if date.fromisoformat(saved["started"]) >= today - timedelta(days=RESUME_MAX_AGE_DAYS):
            started, done = saved["started"], saved["results"]
        else:
            log.warning("Progress from %s is too old to resume; starting again", saved["started"])
    if not done:
        started = today.isoformat()
    members = _member_isins(db_path)
    groups, supply, unpriceable = candidate_groups(sync.fetch_instruments())
    fx = fx if fx is not None else fetch_ecb_rates()
    yahoo = yahoo or sync._Yahoo()
    window_start = today - timedelta(days=365)

    try:
        for n, (isin, lines) in enumerate(groups.items(), 1):
            if isin in done:
                continue
            try:
                done[isin] = screen_isin(yahoo, fx, isin, lines, isin in members, window_start)
            except Exception as exc:  # noqa: BLE001 - provider failure: excluded now, retried on --resume
                log.warning("Screening %s failed: %s", isin, type(exc).__name__)
                continue
            if n % 25 == 0:
                _save_progress(progress_path, started, done)
                log.info("Screened %d/%d ISINs", n, len(groups))
    finally:
        _save_progress(progress_path, started, done)  # an interrupted run can continue with --resume

    # Only ISINs Trading 212 still offers; a stock dropped since an interruption is not selectable.
    results = [done[isin] for isin in groups if isin in done]
    for isin, lines in groups.items():
        if isin not in done:
            results.append({"isin": isin, "country": isin[:2], "member": isin in members,
                            "eligible": False, "ticker": sorted(l["ticker"] for l in lines)[0],
                            "name": lines[0].get("name"), "reason": "provider_error"})
    results += [{"isin": isin, "country": isin[:2], "member": isin in members, "eligible": False,
                 "reason": reason} for isin, reason in unpriceable.items()]
    selected, summary = allocate(results, supply)
    for row in selected:
        line, choice = choose_line(yahoo, fx, groups[row["isin"]], row["home_symbol"])
        row.update(ticker=line["ticker"], name=line.get("name"), line_choice=choice)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    review, countries = write_review(out_dir, stamp, selected, results, summary)
    progress_path.unlink()
    return {"screened": len(results), "eligible": sum(r["eligible"] for r in results),
            "selected": len(selected), "review": str(review), "countries": str(countries)}


def run_quick(out_dir: Path, instruments: list[dict] | None = None) -> dict:
    """Placeholder universe from Trading 212 metadata only (no prices, not volatility-ranked).

    Fills each country quota in ISIN order and writes the usual review files for ``approve``.
    """
    out_dir = _private_dir(out_dir)
    groups, supply, _ = candidate_groups(instruments if instruments is not None else sync.fetch_instruments())
    selected, summary = [], []
    for country, quota in COUNTRY_QUOTAS.items():
        isins = [isin for isin in groups if isin[:2] == country][:quota]
        for rank, isin in enumerate(isins, 1):
            line = min(groups[isin], key=lambda inst: inst["ticker"])
            selected.append({"isin": isin, "country": country, "rank": rank, "ticker": line["ticker"],
                             "name": line.get("name"), "volatility": 0.0, "eligible": True,
                             "reason": "quick_placeholder"})
        summary.append({"country": country, "quota": quota, "supply": supply.get(country, 0),
                        "eligible": len(isins), "selected": len(isins), "unfilled": quota - len(isins),
                        "reason": "too_few_priceable_lines" if len(isins) < quota else ""})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    review, countries = write_review(out_dir, stamp, selected, selected, summary)
    return {"review": str(review), "countries": str(countries), "selected": len(selected),
            "unfilled": {r["country"]: r["unfilled"] for r in summary if r["unfilled"]}}


def read_selected(review_csv: Path) -> list[dict]:
    """Validated SELECTED rows of a review CSV; raises ValueError on any inconsistency."""
    rows = []
    with open(review_csv, newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            if raw.get("status") != "SELECTED":
                continue
            ticker, isin = _uncell(raw["t212_ticker"]), raw["isin"]
            country, volatility, rank = raw["country"], float(raw["volatility"]), int(raw["rank"])
            if not (TICKER_PATTERN.match(ticker) and ISIN_PATTERN.match(isin) and country == isin[:2]
                    and country in COUNTRY_QUOTAS and rank >= 1 and math.isfinite(volatility)
                    and volatility >= 0):
                raise ValueError(f"Invalid SELECTED row for ISIN {isin[:12]!r}")
            rows.append({"ticker": ticker, "isin": isin, "country": country, "rank": rank,
                         "volatility": volatility})
    if len({r["ticker"] for r in rows}) != len(rows) or len({r["isin"] for r in rows}) != len(rows):
        raise ValueError("Duplicate ticker or ISIN in the review file")
    for country, quota in COUNTRY_QUOTAS.items():
        if sum(r["country"] == country for r in rows) > quota:
            raise ValueError(f"{country} exceeds its quota of {quota}")
    return rows


def run_approve(review_csv: Path, db_path: str) -> int:
    """Replace universe_members with the reviewed selection in one transaction."""
    rows = read_selected(review_csv)
    current = {i["ticker"]: i for i in sync.fetch_instruments()}
    for r in rows:
        inst = current.get(r["ticker"])
        if not inst or inst.get("type") != "STOCK" or inst.get("isin") != r["isin"] or not sync.yahoo_line(inst):
            raise ValueError(f"{r['ticker']} is no longer a priceable Trading 212 stock line for {r['isin']}")
    sync.init_db(db_path)
    with refresh.job_guard(db_path, "stocks") as acquired:
        if not acquired:
            raise RuntimeError("A stock refresh is running; approve again when it has finished")
        selected_at = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(db_path)
        try:
            with conn:
                conn.execute("DELETE FROM universe_members")
                conn.executemany(
                    "INSERT INTO universe_members (ticker, isin, country, rank, volatility, selected_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [(r["ticker"], r["isin"], r["country"], r["rank"], r["volatility"], selected_at)
                     for r in rows])
        finally:
            conn.close()
    return len(rows)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    select = sub.add_parser("select", help="screen and write a private review CSV")
    select.add_argument("--db", default=sync.DB_PATH)
    select.add_argument("--out-dir", type=Path, help="private output directory (default: <db dir>/universe)")
    select.add_argument("--resume", action="store_true", help="reuse progress from an interrupted run")
    quick = sub.add_parser("quick", help="write a placeholder review CSV from Trading 212 metadata only")
    quick.add_argument("--db", default=sync.DB_PATH)
    quick.add_argument("--out-dir", type=Path, help="private output directory (default: <db dir>/universe)")
    approve = sub.add_parser("approve", help="load a reviewed selection into universe_members")
    approve.add_argument("review_csv", type=Path)
    approve.add_argument("--db", default=sync.DB_PATH)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "select":
        out_dir = args.out_dir or Path(args.db).resolve().parent / "universe"
        print(json.dumps(run_select(args.db, out_dir, resume=args.resume), indent=2))
    elif args.command == "quick":
        print(json.dumps(run_quick(args.out_dir or Path(args.db).resolve().parent / "universe"), indent=2))
    else:
        print(f"Approved {run_approve(args.review_csv, args.db)} members")
    return 0


if __name__ == "__main__":
    sys.exit(main())

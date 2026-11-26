"""Tests for the manual universe selection, with fake Trading 212, Yahoo and ECB data."""

import csv
import os
import random
import sqlite3
import stat
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import sync
import universe_select as us

TODAY = date(2026, 11, 20)
WINDOW_START = TODAY - timedelta(days=365)
RATES = {"USD": 1.1, "GBP": 0.85, "JPY": 160.0, "ILS": 4.0, "CHF": 0.95}


def ecb_text(rates=RATES, start=date(2025, 1, 1), end=TODAY, skip=()):
    lines = ["Date," + ",".join(rates) + ",CYP"]
    day = start
    while day <= end:
        if day not in skip:
            lines.append(day.isoformat() + "," + ",".join(str(v) for v in rates.values()) + ",N/A")
        day += timedelta(days=1)
    return "\n".join(lines)


FX = us.parse_ecb_csv(ecb_text())


def bars(n=200, price=10.0, volume=500_000.0, end=TODAY - timedelta(days=1), seed=1, step=0.02):
    """Deterministic daily bars ending before TODAY (completed sessions)."""
    rng = np.random.RandomState(seed)
    idx = pd.bdate_range(end=end, periods=n)
    closes = price * np.exp(np.cumsum(rng.normal(0, step, n)))
    opens = np.concatenate([[price], closes[:-1]]) * np.exp(rng.normal(0, step / 4, n))
    highs = np.maximum(opens, closes) * 1.01
    lows = np.minimum(opens, closes) * 0.99
    return pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes,
                         "Volume": volume}, index=idx)


def isin(country, n):
    return f"{country}{n:09d}0"


class FakeYahoo:
    def __init__(self, histories=None, infos=None, searches=None, fail=None):
        self.histories, self.infos = histories or {}, infos or {}
        self.searches, self.fail = searches or {}, fail or {}
        self.calls = []

    def history(self, symbol, period="max"):
        self.calls.append(("history", symbol))
        if symbol in self.fail:
            raise self.fail[symbol]
        hist = self.histories.get(symbol)
        return None if hist is None else hist.copy()

    def search(self, query):
        self.calls.append(("search", query))
        return self.searches.get(query, [])

    def info(self, symbol):
        self.calls.append(("info", symbol))
        return self.infos.get(symbol, {})


def us_info(name, exchange="NMS", country="United States", currency="USD"):
    return {"quoteType": "EQUITY", "country": country, "exchange": exchange,
            "longName": name, "shortName": name, "currency": currency}


class TestFx(unittest.TestCase):
    def test_minor_units_convert_through_major_currency(self):
        day = TODAY - timedelta(days=2)
        self.assertAlmostEqual(us.to_eur(1000, "GBX", FX, day), 10 / 0.85)
        self.assertAlmostEqual(us.to_eur(1000, "GBp", FX, day), 10 / 0.85)
        self.assertAlmostEqual(us.to_eur(1000, "ILA", FX, day), 10 / 4.0)
        self.assertAlmostEqual(us.to_eur(10, "GBP", FX, day), 10 / 0.85)
        self.assertAlmostEqual(us.to_usd(1000, "GBp", FX, day), 10 / 0.85 * 1.1)
        self.assertEqual(us.to_eur(5, "EUR", FX, day), 5)

    def test_all_screening_currencies_supported_by_ecb_columns(self):
        for currency in ("USD", "GBP", "CAD", "CHF", "JPY", "AUD", "SEK", "DKK", "NOK", "ILS",
                         "SGD", "NZD", "HKD"):
            fx = us.parse_ecb_csv(f"Date,{currency}\n2026-11-02,2.0\n")
            self.assertAlmostEqual(us.to_eur(4.0, currency, fx, date(2026, 11, 2)), 2.0, msg=currency)

    def test_never_uses_future_or_stale_rate(self):
        fx = us.parse_ecb_csv("Date,USD\n2026-11-10,1.2\n2026-11-02,1.0\n")
        self.assertEqual(us.to_eur(1.0, "USD", fx, date(2026, 11, 9)), 1.0)  # earlier rate, 7 days
        self.assertIsNone(us.to_eur(1.0, "USD", fx, date(2026, 11, 1)))      # only a future rate
        fx = us.parse_ecb_csv("Date,USD\n2026-11-01,1.0\n")
        self.assertIsNone(us.to_eur(1.0, "USD", fx, date(2026, 11, 9)))      # 8 days old
        self.assertIsNone(us.to_eur(1.0, "XYZ", FX, TODAY))
        self.assertIsNone(us.to_eur(1.0, None, FX, TODAY))

    def test_parse_skips_missing_values(self):
        self.assertNotIn("CYP", FX)
        self.assertEqual(FX["USD"][0][0], date(2025, 1, 1))


class TestRyz63(unittest.TestCase):
    def test_matches_yang_zhang_formula(self):
        df = bars(64, seed=3)
        o, h, l, c = (df[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
        on, oc = np.log(o[1:] / c[:-1]), np.log(c[1:] / o[1:])
        rs = (np.log(h / c) * np.log(h / o) + np.log(l / c) * np.log(l / o))[1:]
        n = 63
        k = 0.34 / (1.34 + (n + 1) / (n - 1))
        expected = np.sqrt((np.var(on, ddof=1) + k * np.var(oc, ddof=1) + (1 - k) * rs.mean()) * 252)
        self.assertAlmostEqual(us.ryz63(df), expected, places=10)

    def test_outlier_is_clipped(self):
        df = bars(64, seed=3)
        spiked = df.copy()
        spiked.iloc[30, spiked.columns.get_loc("Open")] *= 1.8
        spiked.iloc[30, spiked.columns.get_loc("High")] = spiked.iloc[30][["Open", "Close"]].max() * 1.01
        o, c = spiked["Open"].to_numpy(), spiked["Close"].to_numpy()
        unclipped_overnight = np.var(np.log(o[1:] / c[:-1]), ddof=1)
        clipped_overnight = np.var(us._clip(np.log(o[1:] / c[:-1])), ddof=1)
        self.assertLess(clipped_overnight, unclipped_overnight / 5)
        self.assertGreater(us.ryz63(spiked), us.ryz63(df))


class TestScreenHistory(unittest.TestCase):
    def screen(self, hist, currency="USD", member=False, fx=FX):
        return us.screen_history(hist, currency, fx, member, WINDOW_START)

    def test_liquid_stock_passes_with_metrics(self):
        metrics, reason = self.screen(bars())
        self.assertIsNone(reason)
        self.assertEqual(metrics["last_session"], (TODAY - timedelta(days=1)).isoformat())
        self.assertGreater(metrics["volatility"], 0)

    def test_only_the_latest_63_sessions_count(self):
        hist = bars(250)
        hist.iloc[-63:, hist.columns.get_loc("Volume")] = 0.0
        self.assertEqual(self.screen(hist), (None, "too_many_zero_volume_days"))
        hist = bars(250)
        hist.iloc[:-63, hist.columns.get_loc("Volume")] = 0.0  # old illiquidity is ignored
        self.assertIsNone(self.screen(hist)[1])

    def test_illiquid_and_member_buffer_thresholds(self):
        hist = bars(volume=95_000.0, step=0.002)  # about EUR 0.86m a day: below 1m, above 0.75m
        self.assertEqual(self.screen(hist), (None, "illiquid"))
        metrics, reason = self.screen(hist, member=True)
        self.assertIsNone(reason)
        self.assertTrue(750_000 < metrics["median_traded_value_eur"] < 1_000_000)
        self.assertEqual(self.screen(bars(volume=70_000.0, step=0.002), member=True), (None, "illiquid"))

    def test_bad_tick_excluded(self):
        hist = bars()
        hist.iloc[-10, hist.columns.get_loc("High")] = hist.iloc[-10]["Low"] * 3.5
        self.assertEqual(self.screen(hist), (None, "bad_tick"))

    def test_invalid_ohlc_excluded(self):
        hist = bars()
        hist.iloc[-10, hist.columns.get_loc("High")] = hist.iloc[-10]["Low"] * 0.9
        self.assertEqual(self.screen(hist), (None, "invalid_ohlc"))
        hist = bars()
        hist.iloc[-10, hist.columns.get_loc("Open")] = 0.0
        self.assertEqual(self.screen(hist), (None, "invalid_ohlc"))

    def test_auction_close_outside_high_low_is_widened_not_rejected(self):
        hist = bars()
        hist.iloc[-10, hist.columns.get_loc("Close")] = hist.iloc[-10]["High"] * 1.01  # NET.L-style bar
        metrics, reason = self.screen(hist)
        self.assertIsNone(reason)
        self.assertGreater(metrics["volatility"], 0)

    def test_unadjusted_reverse_split_jump_excluded(self):
        hist = bars()
        hist.iloc[-20:, :4] *= 10  # 1-for-10 reverse split missing from the history
        self.assertEqual(self.screen(hist), (None, "split_like_jump"))

    def test_ipo_needs_68_sessions_and_skips_the_first_5(self):
        self.assertEqual(self.screen(bars(67)), (None, "insufficient_sessions"))
        hist = bars(68)
        hist.iloc[2, hist.columns.get_loc("High")] = hist.iloc[2]["Low"] * 4  # IPO-day tick
        self.assertIsNone(self.screen(hist)[1])
        self.assertEqual(self.screen(bars(62, end=date(2025, 2, 25))), (None, "insufficient_sessions"))

    def test_too_few_valid_sessions(self):
        hist = bars()
        hist.iloc[-10:-3, hist.columns.get_loc("Close")] = np.nan
        self.assertEqual(self.screen(hist), (None, "too_few_valid_sessions"))

    def test_zero_return_days(self):
        hist = bars()
        hist.iloc[-20:, hist.columns.get_loc("Close")] = hist.iloc[-21]["Close"]
        hist.iloc[-20:, hist.columns.get_loc("Open")] = hist.iloc[-21]["Close"]
        hist.iloc[-20:, hist.columns.get_loc("High")] = hist.iloc[-21]["Close"] * 1.001
        hist.iloc[-20:, hist.columns.get_loc("Low")] = hist.iloc[-21]["Close"] * 0.999
        self.assertEqual(self.screen(hist), (None, "too_many_zero_return_days"))

    def test_price_gate_in_usd(self):
        self.assertEqual(self.screen(bars(price=1.5, volume=2_000_000.0, step=0.001)), (None, "price_too_low"))

    def test_missing_fx_makes_stock_ineligible(self):
        self.assertEqual(self.screen(bars(), currency="XYZ"), (None, "missing_fx"))
        gap = {TODAY - timedelta(days=d) for d in range(20, 40)}
        fx = us.parse_ecb_csv(ecb_text(skip=gap))
        self.assertEqual(self.screen(bars(), fx=fx), (None, "missing_fx"))

    def test_pence_listing_is_not_100_times_too_liquid(self):
        metrics, reason = self.screen(bars(price=500.0, volume=300_000.0, step=0.001), currency="GBp")
        self.assertIsNone(reason)
        self.assertAlmostEqual(metrics["median_traded_value_eur"], 5 * 300_000 / 0.85, delta=150_000)
        self.assertAlmostEqual(metrics["price_usd"], 5 / 0.85 * 1.1, delta=0.5)
        metrics, reason = self.screen(bars(price=1500.0, volume=300_000.0, step=0.001), currency="ILA")
        self.assertIsNone(reason)
        self.assertAlmostEqual(metrics["median_traded_value_eur"], 15 * 300_000 / 4.0, delta=150_000)


class TestHomeListing(unittest.TestCase):
    def test_home_line_verified_without_search(self):
        yahoo = FakeYahoo(infos={"AAPL": us_info("Apple Inc.")})
        lines = [{"ticker": "AAPL_US_EQ", "name": "Apple", "isin": "US0378331005"},
                 {"ticker": "APCd_EQ", "name": "Apple", "isin": "US0378331005"}]
        self.assertEqual(us.resolve_home(yahoo, "US0378331005", "US", lines), ("AAPL", "USD", None))
        self.assertNotIn("search", [c[0] for c in yahoo.calls])

    def test_foreign_only_line_resolved_through_isin_search(self):
        jp = "JP3633400001"
        yahoo = FakeYahoo(
            searches={jp: [{"symbol": "TOM.F", "quoteType": "EQUITY"},
                           {"symbol": "7203.T", "quoteType": "EQUITY"}]},
            infos={"7203.T": us_info("Toyota Motor Corporation", "JPX", "Japan", "JPY")})
        lines = [{"ticker": "TOMd_EQ", "name": "Toyota Motor", "isin": jp}]
        self.assertEqual(us.resolve_home(yahoo, jp, "JP", lines), ("7203.T", "JPY", None))

    def test_unverified_listings_are_ineligible(self):
        lines = [{"ticker": "AAPL_US_EQ", "name": "Apple", "isin": "US0378331005"}]
        for info in (us_info("Pineapple Energy"), us_info("Apple Inc.", country="Ireland"),
                     us_info("Apple Inc.", exchange="PNK"), {**us_info("Apple Inc."), "quoteType": "ETF"}):
            yahoo = FakeYahoo(infos={"AAPL": info})
            self.assertEqual(us.resolve_home(yahoo, "US0378331005", "US", lines),
                             (None, None, "unverified_home_listing"))
        self.assertEqual(us.resolve_home(FakeYahoo(), "JP3633400001", "JP",
                                         [{"ticker": "TOMd_EQ", "name": "Toyota"}]),
                         (None, None, "no_home_listing"))

    def test_name_search_when_isin_search_misses_home_listing(self):
        il = "IL0002810146"
        yahoo = FakeYahoo(searches={il: [{"symbol": "ICL", "quoteType": "EQUITY"}],
                                    "ICL Group": [{"symbol": "ICL", "quoteType": "EQUITY"},
                                                  {"symbol": "ICL.TA", "quoteType": "EQUITY"}]},
                          infos={"ICL.TA": us_info("ICL Group Ltd", "TLV", "Israel", "ILA")})
        lines = [{"ticker": "ICL_US_EQ", "shortName": "ICL", "name": "ICL Group", "isin": il}]
        self.assertEqual(us.resolve_home(yahoo, il, "IL", lines), ("ICL.TA", "ILA", None))
        self.assertEqual([c for c in yahoo.calls if c[0] == "search"], [("search", il), ("search", "ICL Group")])

    def test_renamed_us_line_uses_current_short_name(self):
        yahoo = FakeYahoo(infos={"TKO": us_info("TKO Group Holdings, Inc.", "NYQ")})
        lines = [{"ticker": "WWE_US_EQ", "shortName": "TKO", "name": "TKO Group Holdings",
                  "isin": "US87256C1018"}]
        self.assertEqual(us.resolve_home(yahoo, "US87256C1018", "US", lines), ("TKO", "USD", None))
        self.assertEqual(us.choose_line(yahoo, FX, lines, "TKO"), (lines[0], "home"))

    def test_two_matching_home_listings_are_ambiguous(self):
        jp = "JP3633400001"
        info = us_info("Toyota Motor", "JPX", "Japan", "JPY")
        yahoo = FakeYahoo(searches={jp: [{"symbol": "7203.T", "quoteType": "EQUITY"},
                                         {"symbol": "7204.T", "quoteType": "EQUITY"}]},
                          infos={"7203.T": info, "7204.T": info})
        self.assertEqual(us.resolve_home(yahoo, jp, "JP", [{"ticker": "TOMd_EQ", "name": "Toyota Motor"}]),
                         (None, None, "ambiguous_home_listing"))

    def test_names_agree(self):
        self.assertTrue(us.names_agree("Novo Nordisk", "Novo Nordisk A/S"))
        self.assertTrue(us.names_agree("AIB Group", "AIB Group plc"))
        self.assertTrue(us.names_agree("Nestlé", "Nestle S.A."))
        self.assertFalse(us.names_agree("Archer Aviation", "American Coastal Insurance"))
        self.assertFalse(us.names_agree("Group", "Holdings plc"))


class TestChooseLine(unittest.TestCase):
    def test_home_line_preferred(self):
        lines = [{"ticker": "APCd_EQ"}, {"ticker": "AAPL_US_EQ"}]
        self.assertEqual(us.choose_line(FakeYahoo(), FX, lines, "AAPL"), (lines[1], "home"))

    def test_otherwise_highest_eur_traded_value_line(self):
        lines = [{"ticker": "TOMd_EQ", "currencyCode": "EUR"}, {"ticker": "TOMl_EQ", "currencyCode": "GBX"}]
        yahoo = FakeYahoo(histories={"TOM.F": bars(volume=1_000.0),
                                     "TOM.L": bars(price=1000.0, volume=1_000.0)})
        self.assertEqual(us.choose_line(yahoo, FX, lines, "7203.T"), (lines[1], "traded_value"))
        self.assertEqual(us.choose_line(FakeYahoo(), FX, lines[:1], "7203.T"), (lines[0], "only_line"))


def result(country, n, volatility, member=False, eligible=True):
    return {"isin": isin(country, n), "country": country, "volatility": volatility,
            "member": member, "eligible": eligible}


class TestAllocate(unittest.TestCase):
    def test_members_never_push_a_country_above_quota(self):
        results = [result("US", i, 1000 - i, member=True) for i in range(300)]
        selected, _ = us.allocate(results, {"US": 300})
        self.assertEqual(len(selected), 200)

    def test_buffer_retains_members_ranked_within_1_3x_quota(self):
        results = [result("US", i, 1000 - i, member=200 <= i < 250) for i in range(300)]
        results.append(result("US", 900, 1000 - 280, member=True))  # rank 281: outside 260
        chosen = {r["isin"] for r in us.allocate(results, {"US": 301})[0]}
        self.assertEqual(len(chosen), 200)
        self.assertTrue({isin("US", i) for i in range(200, 250)} <= chosen)   # retained
        self.assertTrue({isin("US", i) for i in range(150)} <= chosen)        # best others
        self.assertNotIn(isin("US", 150), chosen)
        self.assertNotIn(isin("US", 900), chosen)

    def test_caps_totals_and_is_deterministic(self):
        rng = random.Random(4)
        results = [result(c, i, round(rng.random(), 2), member=rng.random() < 0.3)
                   for c in us.COUNTRY_QUOTAS for i in range(400)]
        supply = {c: 400 for c in us.COUNTRY_QUOTAS}
        selected, summary = us.allocate(results, supply)
        self.assertEqual(len(selected), 1000)
        self.assertEqual(len({r["isin"] for r in selected}), 1000)
        for c, quota in us.COUNTRY_QUOTAS.items():
            self.assertEqual(sum(r["country"] == c for r in selected), quota)
        rng.shuffle(results)
        self.assertEqual(us.allocate(results, supply)[0], selected)

    def test_unfilled_seats_have_a_reason(self):
        results = [result("NZ", i, 1.0) for i in range(4)] + [result("HK", 1, 1.0)]
        summary = {s["country"]: s for s in us.allocate(results, {"NZ": 6, "HK": 2})[1]}
        self.assertEqual((summary["NZ"]["unfilled"], summary["NZ"]["reason"]), (2, "too_few_pass_gates"))
        self.assertEqual((summary["HK"]["unfilled"], summary["HK"]["reason"]), (2, "supply"))
        self.assertEqual(summary["US"]["reason"], "supply")


class TestCsvCell(unittest.TestCase):
    def test_formula_text_is_escaped(self):
        for text in ("=HYPERLINK(\"x\")", "+1", "-1", "@SUM(A1)", "\tx", "\rx"):
            self.assertEqual(us.csv_cell(text), "'" + text)
            self.assertEqual(us._uncell(us.csv_cell(text)), text)
        self.assertEqual(us.csv_cell("Apple"), "Apple")
        self.assertEqual(us.csv_cell(-1.5), "-1.5")


US_COUNT = 30


def universe():
    """30 US stocks, a Japanese stock offered only in Frankfurt, a duplicate listing, an ETF,
    a warrant, a Cayman issuer and a hostile company name."""
    instruments, histories, infos = [], {}, {}
    for i in range(US_COUNT):
        sym = f"S{i:02d}"
        instruments.append({"ticker": f"{sym}_US_EQ", "type": "STOCK", "name": f"Stock{i} Inc",
                            "isin": isin("US", i), "currencyCode": "USD"})
        histories[sym] = bars(seed=i + 10, step=0.01 + i * 0.001)
        infos[sym] = us_info(f"Stock{i} Incorporated")
    instruments[0]["name"] = "=HYPERLINK(\"http://x\") Inc"
    infos["S00"] = us_info("=HYPERLINK(\"http://x\") Inc")
    instruments.append({"ticker": "S01d_EQ", "type": "STOCK", "name": "Stock1 Inc",
                        "isin": isin("US", 1), "currencyCode": "EUR"})       # duplicate listing
    jp = "JP3633400001"
    instruments.append({"ticker": "TOMd_EQ", "type": "STOCK", "name": "Toyota Motor", "isin": jp,
                        "currencyCode": "EUR"})
    histories["7203.T"] = bars(price=3000.0, volume=5_000_000.0, seed=99, step=0.03)
    infos["7203.T"] = us_info("Toyota Motor Corporation", "JPX", "Japan", "JPY")
    instruments += [
        {"ticker": "SPY_US_EQ", "type": "ETF", "name": "SPDR", "isin": "US78462F1030"},
        {"ticker": "WRNT_US_EQ", "type": "WARRANT", "name": "Warrant", "isin": "US0000000AA9"},
        {"ticker": "CAYM_US_EQ", "type": "STOCK", "name": "Cayman Co", "isin": "KYG000000001"},
    ]
    searches = {jp: [{"symbol": "7203.T", "quoteType": "EQUITY"}]}
    return instruments, histories, infos, searches


@contextmanager
def no_sleep():
    with patch("sync.time.sleep"):
        yield


class TestSelectAndApprove(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = os.path.join(self.dir.name, "stocks.db")
        self.out = Path(self.dir.name) / "universe"
        sync.init_db(self.db)
        self.instruments, histories, infos, searches = universe()
        self.yahoo = FakeYahoo(histories, infos, searches)
        p = patch("sync.fetch_instruments", return_value=self.instruments)
        p.start()
        self.addCleanup(p.stop)

    def select(self, yahoo=None, resume=False):
        return us.run_select(self.db, self.out, resume=resume, today=TODAY,
                             yahoo=yahoo or self.yahoo, fx=FX)

    def members(self):
        conn = sqlite3.connect(self.db)
        try:
            return sorted(conn.execute("SELECT ticker, isin, country, rank FROM universe_members"))
        finally:
            conn.close()

    def rows(self, path):
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def test_select_writes_private_review_without_changing_membership(self):
        summary = self.select()
        self.assertEqual(self.members(), [])
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o700)
        for key in ("review", "countries"):
            self.assertEqual(stat.S_IMODE(os.stat(summary[key]).st_mode), 0o600)
        rows = self.rows(summary["review"])
        selected = [r for r in rows if r["status"] == "SELECTED"]
        self.assertEqual(len(selected), 31)
        self.assertEqual(len({r["isin"] for r in selected}), 31)
        self.assertNotIn("US78462F1030", {r["isin"] for r in rows})   # ETF never considered
        self.assertNotIn("KYG000000001", {r["isin"] for r in rows})   # outside the 23 countries
        by_isin = {r["isin"]: r for r in selected}
        self.assertEqual(by_isin["JP3633400001"]["home_symbol"], "7203.T")
        self.assertEqual(by_isin["JP3633400001"]["t212_ticker"], "TOMd_EQ")
        self.assertEqual(by_isin[isin("US", 1)]["t212_ticker"], "S01_US_EQ")  # home line wins
        self.assertTrue(by_isin[isin("US", 0)]["t212_name"].startswith("'="))
        countries = {r["country"]: r for r in self.rows(summary["countries"])}
        self.assertEqual(countries["JP"]["unfilled"], "103")
        self.assertEqual(countries["JP"]["reason"], "supply")
        self.assertFalse((self.out / "selection_progress.json").exists())

    def test_rerun_with_same_data_gives_same_list(self):
        first = self.rows(self.select()["review"])
        second = self.rows(self.select(FakeYahoo(*universe()[1:]))["review"])
        self.assertEqual(first, second)

    def test_interrupted_run_keeps_membership_and_resumes(self):
        conn_rows = [("S05_US_EQ", isin("US", 5), "US", 1, 0.5, "before")]
        sync.init_db(self.db)
        conn = sqlite3.connect(self.db)
        conn.executemany("INSERT INTO universe_members VALUES (?, ?, ?, ?, ?, ?)", conn_rows)
        conn.commit()
        conn.close()

        class Interrupting(FakeYahoo):
            def history(self, symbol, period="max"):
                if symbol == "S20":
                    raise KeyboardInterrupt
                return super().history(symbol, period)

        _, histories, infos, searches = universe()
        with self.assertRaises(KeyboardInterrupt):
            self.select(Interrupting(histories, infos, searches))
        self.assertEqual(self.members(), [("S05_US_EQ", isin("US", 5), "US", 1)])
        self.assertTrue((self.out / "selection_progress.json").exists())

        resumed = FakeYahoo(histories, infos, searches)
        rows = self.rows(self.select(resumed, resume=True)["review"])
        screened = {c[1] for c in resumed.calls if c[0] == "history"}
        self.assertNotIn("S00", screened)          # finished before the interruption
        self.assertIn("S20", screened)
        self.assertEqual(sum(r["status"] == "SELECTED" for r in rows), 31)
        self.assertEqual(self.members(), [("S05_US_EQ", isin("US", 5), "US", 1)])

    def test_resume_ignores_old_progress_and_dropped_stocks(self):
        class Interrupting(FakeYahoo):
            def history(self, symbol, period="max"):
                if symbol == "S20":
                    raise KeyboardInterrupt
                return super().history(symbol, period)

        _, histories, infos, searches = universe()
        with self.assertRaises(KeyboardInterrupt):
            self.select(Interrupting(histories, infos, searches))
        self.instruments[:] = [i for i in self.instruments if i.get("isin") != isin("US", 0)]
        rows = self.rows(self.select(FakeYahoo(histories, infos, searches), resume=True)["review"])
        self.assertNotIn(isin("US", 0), {r["isin"] for r in rows})

        with self.assertRaises(KeyboardInterrupt):
            self.select(Interrupting(histories, infos, searches))
        later = FakeYahoo(histories, infos, searches)
        us.run_select(self.db, self.out, resume=True, today=TODAY + timedelta(days=8), yahoo=later, fx=FX)
        self.assertIn(("history", "S01"), later.calls)  # stale progress was screened again

    def test_provider_error_is_reported_and_retried_on_resume(self):
        _, histories, infos, searches = universe()
        rows = self.rows(self.select(FakeYahoo(histories, infos, searches,
                                               fail={"S03": ConnectionError("down")}))["review"])
        self.assertEqual({r["reason"] for r in rows if r["isin"] == isin("US", 3)}, {"provider_error"})

    def test_approve_loads_reviewed_selection(self):
        review = self.select()["review"]
        self.assertEqual(us.run_approve(Path(review), self.db), 31)
        members = self.members()
        self.assertEqual(len(members), 31)
        self.assertIn(("TOMd_EQ", "JP3633400001", "JP", 1), members)

    def write_review(self, rows):
        path = Path(self.dir.name) / "edited.csv"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, us.CSV_COLUMNS)
            writer.writeheader()
            for r in rows:
                writer.writerow({c: r.get(c, "") for c in us.CSV_COLUMNS})
        return path

    def test_approve_rejects_inconsistent_files_and_keeps_membership(self):
        good = {"status": "SELECTED", "country": "US", "rank": "1", "isin": isin("US", 1),
                "t212_ticker": "S01_US_EQ", "volatility": "0.5"}
        hk = [{**good, "country": "HK", "isin": isin("HK", i), "t212_ticker": f"H{i}_US_EQ",
               "rank": str(i + 1)} for i in range(4)]
        bad_files = [
            [good, {**good, "t212_ticker": "S02_US_EQ"}],             # duplicate ISIN
            [{**good, "isin": "US123"}],                              # malformed ISIN
            [{**good, "country": "JP"}],                              # country != ISIN prefix
            hk,                                                       # HK quota is 3
            [{**good, "t212_ticker": "SPY_US_EQ", "isin": "US78462F1030"}],  # not a stock
            [{**good, "t212_ticker": "S01_US_EQ", "isin": isin("US", 2)}],   # ISIN changed
        ]
        for rows in bad_files:
            with self.assertRaises(ValueError):
                us.run_approve(self.write_review(rows), self.db)
        self.assertEqual(self.members(), [])

    def test_approve_waits_for_running_refresh(self):
        review = self.select()["review"]

        @contextmanager
        def busy(db_path, mode):
            yield False

        with patch("universe_select.refresh.job_guard", busy):
            with self.assertRaises(RuntimeError):
                us.run_approve(Path(review), self.db)
        self.assertEqual(self.members(), [])


class TestNoPublicRoute(unittest.TestCase):
    def test_app_does_not_import_selection(self):
        import app
        self.assertFalse(any("universe" in str(rule) for rule in app.app.url_map.iter_rules()))


if __name__ == "__main__":
    unittest.main()

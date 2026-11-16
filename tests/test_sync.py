import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
import requests

from yfinance.exceptions import YFPricesMissingError, YFRateLimitError

import sync


class TestToYahoo(unittest.TestCase):
    def test_us_ticker(self):
        self.assertEqual(sync.to_yahoo("AAPL_US_EQ"), ("AAPL", "US"))

    def test_london_ticker(self):
        self.assertEqual(sync.to_yahoo("BPl_EQ"), ("BP.L", "LSE"))

    def test_milan_ticker(self):
        self.assertEqual(sync.to_yahoo("XYZm_EQ"), ("XYZ.MI", "MILAN"))

    def test_german_ticker(self):
        self.assertEqual(sync.to_yahoo("TLSd_EQ"), ("TLS.F", "FRANKFURT"))

    def test_euronext_and_other_letter_tickers(self):
        self.assertEqual(sync.to_yahoo("SUp_EQ"), ("SU.PA", "PARIS"))
        self.assertEqual(sync.to_yahoo("MTa_EQ"), ("MT.AS", "AMSTERDAM"))
        self.assertEqual(sync.to_yahoo("FCCe_EQ"), ("FCC.MC", "MADRID"))
        self.assertEqual(sync.to_yahoo("NESNs_EQ"), ("NESN.SW", "SIX"))

    def test_share_class_slash_becomes_dash(self):
        self.assertEqual(sync.to_yahoo("BRK/A_US_EQ"), ("BRK-A", "US"))

    def test_country_code_tickers(self):
        self.assertEqual(sync.to_yahoo("WELL_CA_EQ"), ("WELL.TO", "TSX"))
        self.assertEqual(sync.to_yahoo("JMT_PT_EQ"), ("JMT.LS", "LISBON"))
        self.assertEqual(sync.to_yahoo("AMS2_AT_EQ"), ("AMS2.VI", "VIENNA"))
        self.assertEqual(sync.to_yahoo("VGP_BE_EQ"), ("VGP.BR", "BRUSSELS"))
        self.assertEqual(sync.to_yahoo("VAN_BB_EQ"), ("VAN.BR", "BRUSSELS"))

    def test_secondary_and_odd_listings_unmapped(self):
        for t in ("SWCl1_EQ", "CEZd1_EQ", "TPR_EQ_US", "ABBNsEQ", "YUMC", "PRNI_EQ"):
            self.assertIsNone(sync.to_yahoo(t), t)

    def test_unknown_exchange_letter_returns_none(self):
        self.assertIsNone(sync.to_yahoo("ABCz_EQ"))

    def test_non_equity_returns_none(self):
        self.assertIsNone(sync.to_yahoo("AAPL_US_CRYPTO"))

    def test_malformed_ticker_returns_none(self):
        self.assertIsNone(sync.to_yahoo("AAPL"))
        self.assertIsNone(sync.to_yahoo("AAPL_US"))
        self.assertIsNone(sync.to_yahoo("_US_EQ"))
        self.assertIsNone(sync.to_yahoo("l_EQ"))
        self.assertIsNone(sync.to_yahoo(""))


class TestComputeMetrics(unittest.TestCase):
    def _make_hist(self, opens, highs, lows, closes, dates):
        return pd.DataFrame(
            {"Open": opens, "High": highs, "Low": lows, "Close": closes},
            index=pd.DatetimeIndex(dates),
        )

    def test_normal_history_active(self):
        dates = pd.date_range("2024-12-30", periods=10, freq="D")
        hist = self._make_hist(
            opens=[100.0] * 10,
            highs=[110.0] * 10,
            lows=[90.0] * 10,
            closes=[105.0] * 10,
            dates=dates,
        )
        m = sync.compute_metrics(hist, 2025)
        self.assertEqual(m["status_tag"], "ACTIVE")
        self.assertEqual(m["year_max"], 110.0)
        self.assertEqual(m["year_min"], 90.0)
        self.assertEqual(m["all_time_max"], 110.0)
        self.assertEqual(m["all_time_min"], 90.0)
        self.assertEqual(m["opening_price"], 100.0)

    def test_first_bar_current_year_new_tag(self):
        dates = pd.date_range("2026-01-02", periods=5, freq="D")
        hist = self._make_hist(
            opens=[10.0] * 5,
            highs=[12.0] * 5,
            lows=[8.0] * 5,
            closes=[11.0] * 5,
            dates=dates,
        )
        m = sync.compute_metrics(hist, 2026)
        self.assertEqual(m["status_tag"], "NEW_2026")

    def test_last_close_zero_point_zero_zero_five_delisted(self):
        dates = pd.date_range("2025-01-02", periods=10, freq="D")
        closes = [105.0] * 9 + [0.005]
        hist = self._make_hist(
            opens=[100.0] * 10,
            highs=[110.0] * 10,
            lows=[90.0] * 10,
            closes=closes,
            dates=dates,
        )
        m = sync.compute_metrics(hist, 2025)
        self.assertEqual(m["status_tag"], "DELISTED_OR_ZERO")

    def test_three_bars_delisted(self):
        dates = pd.date_range("2025-01-02", periods=3, freq="D")
        hist = self._make_hist(
            opens=[100.0] * 3,
            highs=[110.0] * 3,
            lows=[90.0] * 3,
            closes=[105.0] * 3,
            dates=dates,
        )
        m = sync.compute_metrics(hist, 2025)
        self.assertEqual(m["status_tag"], "DELISTED_OR_ZERO")

    def test_no_current_year_bars_year_null(self):
        dates = pd.date_range("2024-01-02", periods=10, freq="D")
        hist = self._make_hist(
            opens=[100.0] * 10,
            highs=[110.0] * 10,
            lows=[90.0] * 10,
            closes=[105.0] * 10,
            dates=dates,
        )
        m = sync.compute_metrics(hist, 2025)
        self.assertEqual(m["status_tag"], "ACTIVE")
        self.assertIsNone(m["year_max"])
        self.assertIsNone(m["year_min"])

    def test_empty_history(self):
        hist = pd.DataFrame(
            {"Open": [], "High": [], "Low": [], "Close": []},
            index=pd.DatetimeIndex([]),
        )
        m = sync.compute_metrics(hist, 2025)
        self.assertEqual(m["status_tag"], "DELISTED_OR_ZERO")
        self.assertIsNone(m["year_max"])
        self.assertIsNone(m["all_time_max"])


def _hist(opens, dates):
    return pd.DataFrame(
        {"Open": opens, "High": [o + 10 for o in opens], "Low": [o - 10 for o in opens],
         "Close": [o + 5 for o in opens]},
        index=pd.DatetimeIndex(dates),
    )


class TestFetchInstruments(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    @patch("sync.requests.get")
    def test_missing_key_raises_without_request(self, get):
        with self.assertRaises(ValueError) as ctx:
            sync.fetch_instruments()
        self.assertIn("T212_API_KEY", str(ctx.exception))
        get.assert_not_called()

    @patch.dict(os.environ, {}, clear=True)
    @patch("sync.requests.get")
    def test_run_sync_missing_key_raises_without_request(self, get):
        with self.assertRaises(ValueError):
            sync.run_sync(db_path=":memory:")
        get.assert_not_called()

    @patch.dict(os.environ, {"T212_API_KEY": "k-123", "T212_API_SECRET": "s-456"}, clear=True)
    @patch("sync.requests.get")
    def test_key_and_secret_use_basic_auth_fixed_host_and_timeout(self, get):
        get.return_value.json.return_value = []
        sync.fetch_instruments()
        args, kwargs = get.call_args
        self.assertEqual(args[0], "https://live.trading212.com/api/v0/equity/metadata/instruments")
        self.assertEqual(kwargs["auth"], ("k-123", "s-456"))
        self.assertEqual(kwargs["timeout"], sync.REQUEST_TIMEOUT)

    @patch.dict(os.environ, {"T212_API_KEY": "k-123", "T212_ENVIRONMENT": "demo"}, clear=True)
    @patch("sync.requests.get")
    def test_demo_environment_uses_demo_host(self, get):
        get.return_value.json.return_value = []
        sync.fetch_instruments()
        self.assertEqual(get.call_args.args[0], "https://demo.trading212.com/api/v0/equity/metadata/instruments")

    @patch.dict(os.environ, {"T212_API_KEY": "k-123", "T212_ENVIRONMENT": "evil.example.com"}, clear=True)
    @patch("sync.requests.get")
    def test_unknown_environment_raises_without_request(self, get):
        with self.assertRaises(ValueError):
            sync.fetch_instruments()
        get.assert_not_called()

    @patch.dict(os.environ, {"T212_API_KEY": "k-123"}, clear=True)
    @patch("sync.requests.get")
    def test_legacy_key_uses_authorization_header(self, get):
        get.return_value.json.return_value = []
        sync.fetch_instruments()
        self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": "k-123"})

    @patch.dict(os.environ, {"T212_API_KEY": "secret-key-value"}, clear=True)
    @patch("sync.requests.get")
    def test_http_error_does_not_contain_key(self, get):
        resp = requests.Response()
        resp.status_code = 401
        resp.url = sync.T212_BASE_URLS["live"] + sync.INSTRUMENTS_ENDPOINT
        get.return_value = resp
        with self.assertRaises(requests.HTTPError) as ctx:
            sync.fetch_instruments()
        self.assertNotIn("secret-key-value", str(ctx.exception))


class TestRunSync(unittest.TestCase):
    INSTRUMENTS = [
        {"ticker": "AAPL_US_EQ", "type": "STOCK", "name": "Apple Inc."},
        {"ticker": "BPl_EQ", "type": "STOCK", "name": "BP plc"},
        {"ticker": "NOPE_US_EQ", "type": "STOCK", "name": "Invalid Corp"},
        {"ticker": "ABCz_EQ", "type": "STOCK", "name": "Unmapped"},
        {"ticker": "SPY_US_EQ", "type": "ETF", "name": "SPDR S&P 500"},
    ]
    DATES = pd.date_range("2024-01-02", periods=10, freq="D")

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self.sleep = patch("sync.time.sleep").start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        os.unlink(self.db_path)

    def _run(self, responses):
        """responses: Yahoo symbol -> list of results (frames or exceptions), one per call."""
        calls = {k: list(v) for k, v in responses.items()}

        def ticker(symbol):
            t = MagicMock()

            def history(**kwargs):
                result = calls[symbol].pop(0) if len(calls[symbol]) > 1 else calls[symbol][0]
                if isinstance(result, Exception):
                    raise result
                return result

            t.history.side_effect = history
            return t

        with patch("sync.fetch_instruments", return_value=self.INSTRUMENTS), \
                patch("sync.yf.Ticker", side_effect=ticker) as mock_ticker:
            summary = sync.run_sync(db_path=self.db_path)
        return summary, mock_ticker

    def _rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return {r[0]: r[1:] for r in conn.execute(
                "SELECT ticker, name, exchange, opening_price, status_tag FROM stocks")}
        finally:
            conn.close()

    def _ok(self, last_open=100.0):
        return {
            "AAPL": [_hist([100.0] * 9 + [last_open], self.DATES)],
            # A trailing all-NaN row is dropped, so the last real open is used.
            "BP.L": [_hist([5.0] * 9 + [float("nan")], self.DATES)],
            "SPY": [_hist([400.0] * 10, self.DATES)],
            "NOPE": [YFPricesMissingError("NOPE", "")],
        }

    def test_counts_and_rows(self):
        summary, _ = self._run(self._ok())
        self.assertEqual(
            (summary["ok"], summary["no_data"], summary["failed"], summary["unmapped"]), (3, 1, 0, 1))
        rows = self._rows()
        self.assertEqual(set(rows), {"AAPL_US_EQ", "BPl_EQ", "SPY_US_EQ"})
        self.assertEqual(rows["AAPL_US_EQ"], ("Apple Inc.", "US", 100.0, "ACTIVE"))
        self.assertEqual(rows["BPl_EQ"], ("BP plc", "LSE", 5.0, "ACTIVE"))

    def test_rerun_updates_instead_of_duplicating(self):
        self._run(self._ok(100.0))
        self._run(self._ok(150.0))
        rows = self._rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows["AAPL_US_EQ"][2], 150.0)

    def test_rate_limit_waits_and_retries_without_failing(self):
        responses = self._ok()
        responses["AAPL"] = [YFRateLimitError(), YFRateLimitError(), responses["AAPL"][0]]
        summary, _ = self._run(responses)
        self.assertEqual((summary["ok"], summary["failed"]), (3, 0))
        waits = [c.args[0] for c in self.sleep.call_args_list]
        self.assertIn(sync.RATE_LIMIT_BACKOFF, waits)
        self.assertIn(sync.RATE_LIMIT_BACKOFF * 2, waits)

    def test_transient_errors_are_retried_in_later_passes(self):
        responses = self._ok()
        flaky = [ConnectionError("reset")] * (sync.MAX_ATTEMPTS + 1)
        responses["BP.L"] = flaky + responses["BP.L"]
        summary, _ = self._run(responses)
        self.assertEqual((summary["ok"], summary["failed"]), (3, 0))
        self.assertIn("BPl_EQ", self._rows())

    def test_persistent_errors_reported_as_failed(self):
        responses = self._ok()
        responses["BP.L"] = [ConnectionError("down")]
        summary, _ = self._run(responses)
        self.assertEqual((summary["ok"], summary["failed"]), (2, 1))
        self.assertEqual(summary["failed_tickers"], ["BPl_EQ"])

    def test_empty_history_counts_as_no_data(self):
        responses = self._ok()
        responses["NOPE"] = [_hist([], [])]
        summary, _ = self._run(responses)
        self.assertEqual((summary["no_data"], summary["failed"]), (1, 0))

    def test_etf_kept_warrant_excluded_type_stored(self):
        instruments = [
            {"ticker": "AAPL_US_EQ", "type": "STOCK", "name": "Apple"},
            {"ticker": "SPY_US_EQ", "type": "ETF", "name": "SPY"},
            {"ticker": "WRNT_WS_EQ", "type": "WARRANT", "name": "Warrant"},
        ]
        responses = {
            "AAPL": self._ok()["AAPL"],
            "SPY": [_hist([400.0] * 10, self.DATES)],
        }
        with patch("sync.fetch_instruments", return_value=instruments), \
                patch("sync.yf.Ticker", side_effect=self._ticker_mock(responses)):
            summary = sync.run_sync(db_path=self.db_path)
        self.assertEqual(summary["ok"], 2)
        self.assertEqual(summary["unmapped"], 0)  # the warrant is excluded, not unmapped
        conn = sqlite3.connect(self.db_path)
        try:
            types = {row[0] for row in conn.execute("SELECT DISTINCT instrument_type FROM stocks")}
        finally:
            conn.close()
        self.assertEqual(types, {"STOCK", "ETF"})

    def _ticker_mock(self, responses):
        calls = {k: list(v) for k, v in responses.items()}

        def factory(symbol):
            t = MagicMock()

            def history(**kwargs):
                result = calls[symbol].pop(0) if len(calls[symbol]) > 1 else calls[symbol][0]
                if isinstance(result, Exception):
                    raise result
                return result

            t.history.side_effect = history
            return t

        return factory


class TestExchangeCountries(unittest.TestCase):
    def test_every_country_code_exchange_has_a_country(self):
        for code, (_suffix, exchange) in sync.COUNTRY_CODES.items():
            self.assertIn(exchange, sync.EXCHANGE_COUNTRIES)

    def test_every_letter_exchange_has_a_country(self):
        for letter, (_suffix, exchange) in sync.EXCHANGE_LETTERS.items():
            self.assertIn(exchange, sync.EXCHANGE_COUNTRIES)

    def test_countries_non_empty(self):
        for exchange, country in sync.EXCHANGE_COUNTRIES.items():
            self.assertTrue(exchange)
            self.assertTrue(country)


class TestIsinFallback(unittest.TestCase):
    DATES = pd.date_range("2024-01-02", periods=10, freq="D")

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self.sleep = patch("sync.time.sleep").start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        os.unlink(self.db_path)

    def _hist(self, opens):
        return pd.DataFrame(
            {"Open": opens, "High": [o + 10 for o in opens], "Low": [o - 10 for o in opens],
             "Close": [o + 5 for o in opens]},
            index=self.DATES,
        )

    def _empty_hist(self):
        return pd.DataFrame(
            {"Open": [], "High": [], "Low": [], "Close": []},
            index=pd.DatetimeIndex([]),
        )

    def _ticker(self, responses):
        calls = {k: list(v) for k, v in responses.items()}

        def factory(symbol):
            t = MagicMock()

            def history(**kwargs):
                result = calls[symbol].pop(0) if len(calls[symbol]) > 1 else calls[symbol][0]
                if isinstance(result, Exception):
                    raise result
                return result

            t.history.side_effect = history
            return t

        return factory

    def _run(self, instruments, responses, isin_lookup=None):
        with patch("sync.fetch_instruments", return_value=instruments), \
                patch("sync.yf.Ticker", side_effect=self._ticker(responses)), \
                patch("sync.get_ticker_by_isin", side_effect=isin_lookup or (lambda isin: None)):
            return sync.run_sync(db_path=self.db_path)

    def _rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return {r[0]: r[1:] for r in conn.execute(
                "SELECT ticker, name, exchange, opening_price, status_tag, instrument_type FROM stocks")}
        finally:
            conn.close()

    def test_init_db_adds_instrument_type_and_yahoo_symbols(self):
        sync.init_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            names = {row[1] for row in conn.execute("PRAGMA table_info(stocks)")}
            self.assertIn("instrument_type", names)
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("yahoo_symbols", tables)
        finally:
            conn.close()

    def test_init_db_migrates_existing_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE stocks (ticker TEXT PRIMARY KEY, name TEXT, exchange TEXT, "
            "opening_price REAL, year_max REAL, year_min REAL, all_time_max REAL, "
            "all_time_min REAL, status_tag TEXT, last_updated TEXT)")
        conn.execute(
            "INSERT INTO stocks (ticker, name, exchange, opening_price, year_max, year_min, "
            "all_time_max, all_time_min, status_tag, last_updated) "
            "VALUES ('AAPL_US_EQ', 'Apple', 'US', 100.0, 110.0, 90.0, 150.0, 80.0, 'ACTIVE', datetime('now'))")
        conn.commit()
        conn.close()
        sync.init_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        try:
            names = {row[1] for row in conn.execute("PRAGMA table_info(stocks)")}
            self.assertIn("instrument_type", names)
            row = conn.execute("SELECT instrument_type FROM stocks WHERE ticker='AAPL_US_EQ'").fetchone()
            self.assertEqual(row[0], "STOCK")
        finally:
            conn.close()

    def test_unmapped_ticker_with_isin_is_resolved(self):
        instruments = [{"ticker": "ABC_EQ", "type": "STOCK", "name": "Abc", "isin": "US0000000000"}]

        def isin_lookup(isin):
            self.assertEqual(isin, "US0000000000")
            return "GENI"

        summary = self._run(instruments, {"GENI": [self._hist([100.0] * 10)]}, isin_lookup)
        self.assertEqual(summary["resolved_by_isin"], 1)
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["unmapped"], 0)
        self.assertEqual(self._rows()["ABC_EQ"][4], "STOCK")

    def test_resolved_symbol_on_another_exchange_is_rejected(self):
        instruments = [{"ticker": "ABC_US_EQ", "type": "STOCK", "name": "Abc", "isin": "US0000000001"}]
        summary = self._run(
            instruments, {"ABC": [self._empty_hist()], "EMY.F": [self._hist([100.0] * 10)]},
            isin_lookup=lambda isin: "EMY.F",
        )
        self.assertEqual(summary["resolved_by_isin"], 0)
        self.assertEqual(summary["ok"], 0)
        self.assertEqual(summary["no_data"], 1)

    def test_saved_match_is_reused_without_isin_lookup(self):
        sync.init_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO yahoo_symbols (ticker, yahoo_symbol, resolved_at) "
            "VALUES ('ABC_EQ', 'GENI', datetime('now'))")
        conn.commit()
        conn.close()
        instruments = [{"ticker": "ABC_EQ", "type": "STOCK", "name": "Abc", "isin": "US0000000000"}]
        called = []
        summary = self._run(
            instruments, {"GENI": [self._hist([100.0] * 10)]},
            isin_lookup=lambda isin: called.append(isin) or None,
        )
        self.assertEqual(called, [])
        self.assertEqual(summary["resolved_by_isin"], 0)
        self.assertEqual(summary["ok"], 1)

    def test_saved_match_with_no_data_is_resolved_again(self):
        sync.init_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO yahoo_symbols (ticker, yahoo_symbol, resolved_at) "
            "VALUES ('ABC_EQ', 'OLDSYM', datetime('now'))")
        conn.commit()
        conn.close()
        instruments = [{"ticker": "ABC_EQ", "type": "STOCK", "name": "Abc", "isin": "US0000000000"}]
        summary = self._run(
            instruments, {"OLDSYM": [self._empty_hist()], "GENI": [self._hist([100.0] * 10)]},
            isin_lookup=lambda isin: "GENI",
        )
        self.assertEqual(summary["resolved_by_isin"], 1)
        self.assertEqual(summary["ok"], 1)

    def _saved(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return dict(conn.execute("SELECT ticker, yahoo_symbol FROM yahoo_symbols"))
        finally:
            conn.close()

    def test_mapped_ticker_without_data_is_recovered_by_isin(self):
        instruments = [{"ticker": "DMYD_US_EQ", "type": "STOCK", "name": "Genius", "isin": "GG00BMF1JR16"}]
        summary = self._run(
            instruments, {"DMYD": [self._empty_hist()], "GENI": [self._hist([100.0] * 10)]},
            isin_lookup=lambda isin: "GENI",
        )
        self.assertEqual((summary["ok"], summary["no_data"], summary["resolved_by_isin"]), (1, 0, 1))
        self.assertEqual(self._rows()["DMYD_US_EQ"][1], "US")
        self.assertEqual(self._saved(), {"DMYD_US_EQ": "GENI"})

    def test_saved_match_of_mapped_ticker_is_reused_on_second_run(self):
        instruments = [{"ticker": "DMYD_US_EQ", "type": "STOCK", "name": "Genius", "isin": "GG00BMF1JR16"}]
        responses = {"DMYD": [self._empty_hist()], "GENI": [self._hist([100.0] * 10)]}
        lookups = []
        self._run(instruments, responses, isin_lookup=lambda isin: lookups.append(isin) or "GENI")
        requested = []
        factory = self._ticker(responses)
        with patch("sync.fetch_instruments", return_value=instruments), \
                patch("sync.yf.Ticker", side_effect=lambda s: requested.append(s) or factory(s)), \
                patch("sync.get_ticker_by_isin", side_effect=lambda isin: lookups.append(isin) or "GENI"):
            summary = sync.run_sync(db_path=self.db_path)
        self.assertEqual(len(lookups), 1)
        self.assertEqual(requested, ["GENI"])
        self.assertEqual((summary["ok"], summary["resolved_by_isin"]), (1, 0))

    def test_instruments_sharing_a_yahoo_symbol_both_get_rows(self):
        instruments = [{"ticker": "3ASMl_EQ", "type": "ETF", "name": "Main", "isin": "XS0000000001"},
                       {"ticker": "3ASMl1_EQ", "type": "ETF", "name": "Second", "isin": "XS0000000001"}]
        summary = self._run(instruments, {"3ASM.L": [self._hist([100.0] * 10)]},
                            isin_lookup=lambda isin: "3ASM.L")
        self.assertEqual(sorted(self._rows()), ["3ASMl1_EQ", "3ASMl_EQ"])
        self.assertEqual((summary["ok"], summary["no_data"], summary["failed"]), (2, 0, 0))

    def test_upsert_error_after_isin_match_is_retried_not_crashing(self):
        instruments = [{"ticker": "DMYD_US_EQ", "type": "STOCK", "name": "Genius", "isin": "GG00BMF1JR16"}]
        real_upsert = sync._upsert
        calls = []

        def flaky_upsert(*args):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_upsert(*args)

        with patch("sync._upsert", side_effect=flaky_upsert):
            summary = self._run(
                instruments, {"DMYD": [self._empty_hist()], "GENI": [self._hist([100.0] * 10)]},
                isin_lookup=lambda isin: "GENI",
            )
        self.assertEqual((summary["ok"], summary["failed"], summary["resolved_by_isin"]), (1, 0, 1))
        self.assertEqual(summary["failed_tickers"], [])

    def test_isin_match_without_data_is_not_saved_or_counted(self):
        instruments = [{"ticker": "OLD_US_EQ", "type": "STOCK", "name": "Old", "isin": "US0000000002"}]
        summary = self._run(
            instruments, {"OLD": [self._empty_hist()], "NEW": [self._empty_hist()]},
            isin_lookup=lambda isin: "NEW",
        )
        self.assertEqual((summary["ok"], summary["no_data"], summary["resolved_by_isin"]), (0, 1, 0))
        self.assertEqual(self._saved(), {})

    def test_isin_without_match_counts_as_unmapped(self):
        instruments = [{"ticker": "ABC_EQ", "type": "STOCK", "name": "Abc", "isin": "US0000000000"}]
        summary = self._run(instruments, {}, isin_lookup=lambda isin: None)
        self.assertEqual(summary["resolved_by_isin"], 0)
        self.assertEqual(summary["unmapped"], 1)
        self.assertEqual(summary["ok"], 0)


if __name__ == "__main__":
    unittest.main()

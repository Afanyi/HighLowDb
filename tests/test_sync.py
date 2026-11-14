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
            "NOPE": [YFPricesMissingError("NOPE", "")],
        }

    def test_counts_and_rows(self):
        summary, _ = self._run(self._ok())
        self.assertEqual(
            (summary["ok"], summary["no_data"], summary["failed"], summary["unmapped"]), (2, 1, 0, 1))
        rows = self._rows()
        self.assertEqual(set(rows), {"AAPL_US_EQ", "BPl_EQ"})
        self.assertEqual(rows["AAPL_US_EQ"], ("Apple Inc.", "US", 100.0, "ACTIVE"))
        self.assertEqual(rows["BPl_EQ"], ("BP plc", "LSE", 5.0, "ACTIVE"))

    def test_rerun_updates_instead_of_duplicating(self):
        self._run(self._ok(100.0))
        self._run(self._ok(150.0))
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows["AAPL_US_EQ"][2], 150.0)

    def test_rate_limit_waits_and_retries_without_failing(self):
        responses = self._ok()
        responses["AAPL"] = [YFRateLimitError(), YFRateLimitError(), responses["AAPL"][0]]
        summary, _ = self._run(responses)
        self.assertEqual((summary["ok"], summary["failed"]), (2, 0))
        waits = [c.args[0] for c in self.sleep.call_args_list]
        self.assertIn(sync.RATE_LIMIT_BACKOFF, waits)
        self.assertIn(sync.RATE_LIMIT_BACKOFF * 2, waits)

    def test_transient_errors_are_retried_in_later_passes(self):
        responses = self._ok()
        flaky = [ConnectionError("reset")] * (sync.MAX_ATTEMPTS + 1)
        responses["BP.L"] = flaky + responses["BP.L"]
        summary, _ = self._run(responses)
        self.assertEqual((summary["ok"], summary["failed"]), (2, 0))
        self.assertIn("BPl_EQ", self._rows())

    def test_persistent_errors_reported_as_failed(self):
        responses = self._ok()
        responses["BP.L"] = [ConnectionError("down")]
        summary, _ = self._run(responses)
        self.assertEqual((summary["ok"], summary["failed"]), (1, 1))
        self.assertEqual(summary["failed_tickers"], ["BPl_EQ"])

    def test_empty_history_counts_as_no_data(self):
        responses = self._ok()
        responses["NOPE"] = [_hist([], [])]
        summary, _ = self._run(responses)
        self.assertEqual((summary["no_data"], summary["failed"]), (1, 0))


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


if __name__ == "__main__":
    unittest.main()

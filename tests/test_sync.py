import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
import requests

import sync


class TestToYahoo(unittest.TestCase):
    def test_us_ticker(self):
        self.assertEqual(sync.to_yahoo("AAPL_US_EQ"), ("AAPL", "US"))

    def test_london_ticker(self):
        self.assertEqual(sync.to_yahoo("BPl_EQ"), ("BP.L", "LSE"))

    def test_xetra_ticker(self):
        self.assertEqual(sync.to_yahoo("SXR8d_EQ"), ("SXR8.DE", "XETRA"))

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
        resp.url = sync.T212_BASE_URL + sync.INSTRUMENTS_ENDPOINT
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

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name

    def tearDown(self):
        os.unlink(self.db_path)

    def _raw(self, aapl_open):
        # Mimic yf.download(group_by="ticker"): union index, NaN padding, all-NaN invalid symbol.
        dates = pd.date_range("2024-01-02", periods=10, freq="D")
        aapl = _hist([100.0] * 9 + [aapl_open], dates)
        bp = _hist([5.0] * 9 + [float("nan")], dates)  # BP has no bar on the last union date
        nope = _hist([float("nan")] * 10, dates)
        return pd.concat({"AAPL": aapl, "BP.L": bp, "NOPE": nope}, axis=1)

    def _run(self, aapl_open):
        with patch("sync.fetch_instruments", return_value=self.INSTRUMENTS), \
                patch("sync.yf.download", return_value=self._raw(aapl_open)):
            return sync.run_sync(db_path=self.db_path)

    def test_counts_and_rows(self):
        summary = self._run(100.0)
        self.assertEqual((summary["ok"], summary["failed"], summary["unmapped"]), (2, 1, 1))
        conn = sqlite3.connect(self.db_path)
        try:
            rows = dict(
                (r[0], r[1:])
                for r in conn.execute("SELECT ticker, name, exchange, opening_price, status_tag FROM stocks")
            )
        finally:
            conn.close()
        self.assertEqual(set(rows), {"AAPL_US_EQ", "BPl_EQ"})
        self.assertEqual(rows["AAPL_US_EQ"], ("Apple Inc.", "US", 100.0, "ACTIVE"))
        # NaN padding row dropped, so the last real open is used.
        self.assertEqual(rows["BPl_EQ"], ("BP plc", "LSE", 5.0, "ACTIVE"))

    def test_rerun_updates_instead_of_duplicating(self):
        self._run(100.0)
        self._run(150.0)
        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute("SELECT count(*) FROM stocks WHERE ticker='AAPL_US_EQ'").fetchone()[0]
            opening = conn.execute("SELECT opening_price FROM stocks WHERE ticker='AAPL_US_EQ'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)
        self.assertEqual(opening, 150.0)


if __name__ == "__main__":
    unittest.main()

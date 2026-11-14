import logging
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import requests

import crypto_sync


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        patch.dict(os.environ, {}, clear=True).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        os.unlink(self.db_path)

    @patch("crypto_sync.requests.get")
    def test_missing_key_raises_without_request(self, get):
        with self.assertRaises(ValueError) as ctx:
            crypto_sync.run_sync(db_path=self.db_path)
        self.assertIn("COINGECKO_API_KEY", str(ctx.exception))
        get.assert_not_called()

    @patch.dict(os.environ, {"COINGECKO_API_KEY": "CG-secret-key"}, clear=True)
    @patch("crypto_sync.requests.get")
    def test_key_sent_in_header_not_in_url(self, get):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = []
        get.return_value = resp
        crypto_sync.run_sync(db_path=self.db_path)
        args, kwargs = get.call_args
        self.assertNotIn("CG-secret-key", args[0])
        self.assertEqual(kwargs["headers"], {"x-cg-demo-api-key": "CG-secret-key"})

    @patch.dict(os.environ, {"COINGECKO_API_KEY": "CG-key", "CRYPTO_VS_CURRENCY": "EUR"}, clear=True)
    def test_invalid_vs_currency_raises(self):
        with self.assertRaises(ValueError):
            crypto_sync.run_sync(db_path=self.db_path)

    @patch.dict(os.environ, {"COINGECKO_API_KEY": "CG-key", "CRYPTO_HISTORY_CALLS_PER_RUN": "notanumber"}, clear=True)
    def test_invalid_history_cap_raises(self):
        with self.assertRaises(ValueError):
            crypto_sync.run_sync(db_path=self.db_path)

    @patch.dict(os.environ, {"COINGECKO_API_KEY": "CG-key", "CRYPTO_HISTORY_CALLS_PER_RUN": "2000"}, clear=True)
    def test_history_cap_out_of_range_raises(self):
        with self.assertRaises(ValueError):
            crypto_sync.run_sync(db_path=self.db_path)

    @patch.dict(os.environ, {"COINGECKO_API_KEY": "CG-key"}, clear=True)
    @patch("crypto_sync.requests.get")
    def test_429_log_excludes_key(self, get):
        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.headers = {"Retry-After": "1"}
        rate_limited.json.return_value = []
        ok = MagicMock()
        ok.status_code = 200
        ok.headers = {}
        ok.json.return_value = []
        get.side_effect = [rate_limited, ok]
        logger = logging.getLogger("crypto_sync")
        with self.assertLogs(logger, level="WARNING") as captured:
            crypto_sync.run_sync(db_path=self.db_path)
        self.assertNotIn("CG-key", "\n".join(captured.output))

    @patch.dict(os.environ, {"COINGECKO_API_KEY": "CG-key"}, clear=True)
    @patch("crypto_sync.time.sleep")
    @patch("crypto_sync.requests.get")
    def test_http_error_log_and_exception_exclude_key(self, get, _sleep):
        resp = requests.Response()
        resp.status_code = 500
        resp.url = crypto_sync.COINGECKO_BASE + crypto_sync.MARKETS_ENDPOINT + "?vs_currency=usd&page=1"
        get.return_value = resp
        with self.assertLogs("crypto_sync", level="WARNING") as captured:
            with self.assertRaises(requests.HTTPError) as ctx:
                crypto_sync.run_sync(db_path=self.db_path, history_cap=0)
        self.assertNotIn("CG-key", "\n".join(captured.output))
        self.assertNotIn("CG-key", str(ctx.exception))

    @patch.dict(os.environ, {"COINGECKO_API_KEY": "CG-key"}, clear=True)
    @patch("crypto_sync.time.sleep")
    @patch("crypto_sync.requests.get")
    def test_retry_after_zero_still_pauses(self, get, sleep):
        limited = MagicMock(status_code=429, headers={"Retry-After": "0"})
        ok = MagicMock(status_code=200, headers={})
        ok.json.return_value = []
        get.side_effect = [limited, ok]
        crypto_sync.run_sync(db_path=self.db_path, history_cap=0)
        self.assertIn(crypto_sync.REQUEST_DELAY, [c.args[0] for c in sleep.call_args_list])
        self.assertNotIn(0, [c.args[0] for c in sleep.call_args_list])


class TestCoinGecko(unittest.TestCase):
    def setUp(self):
        patch.dict(os.environ, {"COINGECKO_API_KEY": "CG-key"}, clear=True).start()
        self.addCleanup(patch.stopall)

    def test_coin_id_url_encoded_in_path(self):
        client = crypto_sync._CoinGecko()
        with patch.object(client, "_get", return_value={"prices": []}) as mock_get:
            client.fetch_year_range("a/b coin", "usd", 2026)
        url = mock_get.call_args.args[0]
        self.assertIn("/coins/a%2Fb%20coin/market_chart", url)
        self.assertNotIn("/coins/a/b coin/market_chart", url)

    def test_fetch_year_range_filters_to_year(self):
        client = crypto_sync._CoinGecko()
        june = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
        dec25 = int(datetime(2025, 12, 15, tzinfo=timezone.utc).timestamp() * 1000)
        with patch.object(client, "_get", return_value={"prices": [[june, 100.0], [june, 200.0], [dec25, 50.0]]}) as mock_get:
            y_max, y_min = client.fetch_year_range("bitcoin", "usd", 2026)
        self.assertEqual((y_max, y_min), (200.0, 100.0))
        self.assertEqual(mock_get.call_args.args[1]["vs_currency"], "usd")


class TestRunSync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        patch.dict(os.environ, {"COINGECKO_API_KEY": "CG-key"}, clear=True).start()
        self.sleep = patch("crypto_sync.time.sleep").start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        os.unlink(self.db_path)

    def _markets_get(self, pages, charts=None):
        """pages: list of per-page coin lists. charts: dict coin_id -> prices list."""

        def get(url, params=None, headers=None, timeout=None):
            resp = MagicMock()
            resp.headers = {}
            if "/market_chart" in url:
                cid = url.split("/coins/")[1].split("/")[0]
                resp.status_code = 200
                resp.json.return_value = {"prices": (charts or {}).get(cid, [])}
                return resp
            page = (params or {}).get("page", 1)
            data = pages[page - 1] if 0 < page <= len(pages) else []
            resp.status_code = 200
            resp.json.return_value = data
            return resp

        return patch("crypto_sync.requests.get", side_effect=get)

    def _coin(self, cid, rank, price=10.0, high=11.0, low=9.0, volume=1000.0):
        return {
            "id": cid,
            "symbol": cid[:3],
            "name": cid.title(),
            "market_cap_rank": rank,
            "current_price": price,
            "high_24h": high,
            "low_24h": low,
            "total_volume": volume,
            "ath": 100.0,
            "atl": 1.0,
        }

    def test_pagination_stops_at_empty_page(self):
        pages = [
            [self._coin("bitcoin", 1), self._coin("ethereum", 2)],
            [],
        ]
        with self._markets_get(pages):
            summary = crypto_sync.run_sync(db_path=self.db_path, history_cap=0)
        self.assertEqual(summary["coins"], 2)
        self.assertEqual(summary["calls"], 2)

    def test_429_waits_and_retries_without_failing(self):
        coin = self._coin("bitcoin", 1)
        first = MagicMock()
        first.status_code = 429
        first.headers = {"Retry-After": "7"}
        first.json.return_value = []
        second = MagicMock()
        second.status_code = 200
        second.headers = {}
        second.json.return_value = [coin]
        empty = MagicMock()
        empty.status_code = 200
        empty.headers = {}
        empty.json.return_value = []
        with patch("crypto_sync.requests.get", side_effect=[first, second, empty]):
            summary = crypto_sync.run_sync(db_path=self.db_path, history_cap=0)
        waits = [c.args[0] for c in self.sleep.call_args_list]
        self.assertIn(7, waits)
        self.assertEqual(summary["coins"], 1)
        self.assertEqual(summary["failed"], 0)

    def test_persistent_error_raises(self):
        bad = MagicMock()
        bad.status_code = 500
        bad.headers = {}
        bad.json.return_value = []
        bad.raise_for_status.side_effect = requests.HTTPError("500")
        with patch("crypto_sync.requests.get", return_value=bad):
            with self.assertRaises(requests.HTTPError):
                crypto_sync.run_sync(db_path=self.db_path, history_cap=0)

    def test_year_values_widen_and_reset_when_year_changes(self):
        conn = sqlite3.connect(self.db_path)
        crypto_sync.init_db(self.db_path)
        conn.execute(
            "INSERT INTO crypto (ticker, name, year_max, year_min, year_ref, all_time_max, all_time_min, status_tag, last_updated) "
            "VALUES (?, ?, 100.0, 90.0, 2025, 200.0, 1.0, 'ACTIVE', datetime('now'))",
            ("bitcoin", "Bitcoin"),
        )
        conn.commit()
        conn.close()

        pages = [[self._coin("bitcoin", 1, price=10.0, high=50.0, low=5.0)]]
        with self._markets_get(pages):
            crypto_sync.run_sync(db_path=self.db_path, history_cap=0)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT year_max, year_min, year_ref FROM crypto WHERE ticker='bitcoin'").fetchone()
        conn.close()
        self.assertEqual(row, (50.0, 5.0, datetime.now().year))

    def test_year_values_accumulate_across_runs_without_backfill(self):
        with self._markets_get([[self._coin("smallcoin", 9000, high=50.0, low=5.0)]]):
            crypto_sync.run_sync(db_path=self.db_path, history_cap=0)
        with self._markets_get([[self._coin("smallcoin", 9000, high=20.0, low=8.0)]]):
            crypto_sync.run_sync(db_path=self.db_path, history_cap=0)
        self.assertEqual(self._one("SELECT year_max, year_min FROM crypto"), (50.0, 5.0))

    def test_backfill_merges_with_24h_values(self):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        charts = {"bitcoin": [[now_ms, 12.0], [now_ms, 9.5]]}
        with self._markets_get([[self._coin("bitcoin", 1, high=30.0, low=4.0)]], charts):
            crypto_sync.run_sync(db_path=self.db_path, history_cap=5)
        self.assertEqual(self._one("SELECT year_max, year_min FROM crypto"), (30.0, 4.0))
        charts = {"bitcoin": [[now_ms, 200.0], [now_ms, 1.0]]}
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE crypto SET history_year = NULL")
        conn.commit()
        conn.close()
        with self._markets_get([[self._coin("bitcoin", 1, high=30.0, low=4.0)]], charts):
            crypto_sync.run_sync(db_path=self.db_path, history_cap=5)
        self.assertEqual(self._one("SELECT year_max, year_min FROM crypto"), (200.0, 1.0))

    def test_backfilled_coin_not_fetched_again_same_year(self):
        calls = []
        with self._markets_get([[self._coin("bitcoin", 1)]]) as get:
            crypto_sync.run_sync(db_path=self.db_path, history_cap=5)
            calls.append(sum("/market_chart" in c.args[0] for c in get.call_args_list))
        with self._markets_get([[self._coin("bitcoin", 1)]]) as get:
            summary = crypto_sync.run_sync(db_path=self.db_path, history_cap=5)
            calls.append(sum("/market_chart" in c.args[0] for c in get.call_args_list))
        self.assertEqual(calls, [1, 0])
        self.assertEqual(summary["backfill_pending"], 0)

    def test_network_error_retried(self):
        empty = MagicMock(status_code=200, headers={})
        empty.json.return_value = []
        page = MagicMock(status_code=200, headers={})
        page.json.return_value = [self._coin("bitcoin", 1)]
        with patch("crypto_sync.requests.get",
                   side_effect=[requests.ConnectTimeout("t"), requests.ConnectionError("reset"), page, empty]):
            summary = crypto_sync.run_sync(db_path=self.db_path, history_cap=0)
        self.assertEqual(summary["coins"], 1)

    def test_persistent_network_error_raises(self):
        with patch("crypto_sync.requests.get", side_effect=requests.ConnectTimeout("t")) as get:
            with self.assertRaises(requests.ConnectTimeout):
                crypto_sync.run_sync(db_path=self.db_path, history_cap=0)
        self.assertEqual(get.call_count, crypto_sync.MAX_ATTEMPTS)

    def test_markets_paged_by_stable_id_order_and_deduplicated(self):
        pages = [[self._coin("aave", 50), self._coin("bitcoin", 1)], [self._coin("bitcoin", 1), self._coin("cardano", 9)], []]
        with self._markets_get(pages) as get:
            summary = crypto_sync.run_sync(db_path=self.db_path, history_cap=0)
        self.assertEqual({c.kwargs["params"]["order"] for c in get.call_args_list}, {"id_asc"})
        self.assertEqual(summary["coins"], 3)
        self.assertEqual(self._one("SELECT count(*) FROM crypto"), (3,))

    def _one(self, sql):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql).fetchone()
        finally:
            conn.close()

    def test_backfill_respects_cap_and_rank_order(self):
        called = []
        markets = [[self._coin("bitcoin", 1), self._coin("ethereum", 2), self._coin("solana", 3)], []]

        def get(url, params=None, headers=None, timeout=None):
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {}
            if "/market_chart" in url:
                cid = url.split("/coins/")[1].split("/")[0]
                called.append(cid)
                resp.json.return_value = {"prices": []}
                return resp
            page = (params or {}).get("page", 1)
            resp.json.return_value = markets[page - 1] if 0 < page <= len(markets) else []
            return resp

        with patch("crypto_sync.requests.get", side_effect=get):
            summary = crypto_sync.run_sync(db_path=self.db_path, history_cap=2)
        self.assertEqual(called, ["bitcoin", "ethereum"])
        self.assertEqual(summary["backfilled"], 2)
        self.assertEqual(summary["backfill_pending"], 1)
        self.assertEqual(summary["failed"], 0)
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT ticker, history_year FROM crypto ORDER BY market_cap_rank").fetchall()
        conn.close()
        self.assertEqual([r[0] for r in rows if r[1] == datetime.now().year], ["bitcoin", "ethereum"])

    def test_status_rules(self):
        self.assertEqual(crypto_sync.compute_status(None, 100.0), "DELISTED_OR_ZERO")
        self.assertEqual(crypto_sync.compute_status(0.0, 100.0), "DELISTED_OR_ZERO")
        self.assertEqual(crypto_sync.compute_status(5.0, 0.0), "DELISTED_OR_ZERO")
        self.assertEqual(crypto_sync.compute_status(5.0, 100.0), "ACTIVE")
        self.assertEqual(crypto_sync.compute_status(5.0, None), "ACTIVE")

    def test_rerun_updates_instead_of_duplicating(self):
        coin = self._coin("bitcoin", 1, price=10.0)
        pages = [[coin], []]
        with self._markets_get(pages):
            crypto_sync.run_sync(db_path=self.db_path, history_cap=0)
        coin2 = self._coin("bitcoin", 1, price=99.0)
        pages2 = [[coin2], []]
        with self._markets_get(pages2):
            crypto_sync.run_sync(db_path=self.db_path, history_cap=0)
        self.assertEqual(self._one("SELECT count(*), max(ticker), max(opening_price) FROM crypto"), (1, "bitcoin", 99.0))

    def test_stock_db_untouched(self):
        pages = [[self._coin("bitcoin", 1)], []]
        with self._markets_get(pages):
            crypto_sync.run_sync(db_path=self.db_path, history_cap=0)
        conn = sqlite3.connect(self.db_path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertIn("crypto", tables)
        self.assertNotIn("stocks", tables)


if __name__ == "__main__":
    unittest.main()

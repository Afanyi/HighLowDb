import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone

from unittest.mock import patch

import app as app_module


class TestNextRun(unittest.TestCase):
    def test_friday_afternoon_to_monday(self):
        friday = datetime(2026, 1, 9, 15, 0, tzinfo=timezone.utc)
        nr = app_module.next_run(now=friday, hh=14, mm=45)
        self.assertEqual(nr.weekday(), 0)
        self.assertEqual(nr.hour, 14)
        self.assertEqual(nr.minute, 45)

    def test_saturday_to_monday(self):
        saturday = datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc)
        nr = app_module.next_run(now=saturday, hh=14, mm=45)
        self.assertEqual(nr.weekday(), 0)
        self.assertEqual(nr.hour, 14)
        self.assertEqual(nr.minute, 45)

    def test_weekday_morning_same_day(self):
        tuesday = datetime(2026, 1, 6, 10, 0, tzinfo=timezone.utc)
        nr = app_module.next_run(now=tuesday, hh=14, mm=45)
        self.assertEqual(nr.weekday(), 1)
        self.assertEqual(nr.hour, 14)
        self.assertEqual(nr.minute, 45)

    def test_weekday_evening_next_day(self):
        tuesday = datetime(2026, 1, 6, 20, 0, tzinfo=timezone.utc)
        nr = app_module.next_run(now=tuesday, hh=14, mm=45)
        self.assertEqual(nr.weekday(), 2)
        self.assertEqual(nr.hour, 14)
        self.assertEqual(nr.minute, 45)

    def test_sunday_to_monday(self):
        sunday = datetime(2026, 1, 11, 12, 0, tzinfo=timezone.utc)
        nr = app_module.next_run(now=sunday, hh=14, mm=45)
        self.assertEqual(nr.weekday(), 0)
        self.assertEqual(nr.hour, 14)
        self.assertEqual(nr.minute, 45)

    def test_lock_prevents_concurrent_sync(self):
        with patch.object(app_module.sync, "run_sync") as mock_run:
            acquired = app_module._sync_lock.acquire(blocking=False)
            self.assertTrue(acquired)
            app_module.run_sync_job()
            mock_run.assert_not_called()
            app_module._sync_lock.release()


class TestAppRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        app_module.DB_PATH = self.db_path
        app_module.last_summary = None

        import sync as sync_mod
        sync_mod.init_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.executemany(
            "INSERT INTO stocks (ticker, name, exchange, opening_price, year_max, year_min, "
            "all_time_max, all_time_min, status_tag, last_updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            [
                ("AAPL_US_EQ", "Apple Inc.", "US", 100.0, 110.0, 90.0, 150.0, 80.0, "ACTIVE"),
                ("BPl_EQ", "BP plc", "LSE", 5.0, 6.0, 4.0, 7.0, 3.0, "ACTIVE"),
                ("XYZ_EQ", "Test Stock", "UK", 0.005, None, None, 10.0, 1.0, "DELISTED_OR_ZERO"),
                ("NEW_STOCK_EQ", "New Co", "US", 10.0, 12.0, 8.0, 12.0, 8.0, "NEW_2026"),
            ],
        )
        conn.commit()
        conn.close()

        self.client = app_module.app.test_client()

    def tearDown(self):
        os.unlink(self.db_path)
        app_module.DB_PATH = "trading212_stocks.db"

    def test_index_returns_200(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"AAPL_US_EQ", resp.data)

    def test_status_filter_new_2026(self):
        resp = self.client.get("/?status=NEW_2026")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"NEW_STOCK_EQ", resp.data)
        self.assertNotIn(b"AAPL_US_EQ", resp.data)

    def test_invalid_status_ignored_no_sql_error(self):
        resp = self.client.get("/?status=bogus")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"AAPL_US_EQ", resp.data)
        self.assertIn(b"BP plc", resp.data)

    def test_search_filters_by_name(self):
        resp = self.client.get("/?q=apple")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"AAPL_US_EQ", resp.data)
        self.assertNotIn(b"BP plc", resp.data)

    def test_invalid_sort_falls_back(self):
        resp = self.client.get("/?sort=notacolumn")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"AAPL_US_EQ", resp.data)

    def test_xss_escaped(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO stocks (ticker, name, exchange, status_tag, last_updated) "
            "VALUES (?, ?, ?, 'ACTIVE', datetime('now'))",
            ("SCRIPT_EQ", "<script>alert(1)</script>", "US"),
        )
        conn.commit()
        conn.close()

        resp = self.client.get("/?q=script")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"&lt;script&gt;alert(1)&lt;/script&gt;", resp.data)
        self.assertNotIn(b"<script>alert(1)</script>", resp.data)

    def test_post_sync_starts_background_sync_and_redirects(self):
        done = threading.Event()
        with patch.object(app_module.sync, "run_sync", side_effect=lambda **kw: done.set() or {}) as mock_run:
            resp = self.client.post("/sync")
            self.assertEqual(resp.status_code, 302)
            self.assertTrue(done.wait(5))
        mock_run.assert_called_once_with(db_path=self.db_path)

    def test_fresh_db_returns_200(self):
        fresh = self.db_path + ".fresh"
        app_module.DB_PATH = fresh
        try:
            resp = self.client.get("/")
            self.assertEqual(resp.status_code, 200)
        finally:
            os.unlink(fresh)

    def test_new_this_year_option_present(self):
        resp = self.client.get("/")
        year = datetime.now().year
        self.assertIn(f'value="NEW_{year}"'.encode(), resp.data)

    def test_sync_form_not_nested(self):
        html = self.client.get("/").get_data(as_text=True)
        filter_form_end = html.index("</form>")
        self.assertGreater(html.index('method="post"'), filter_form_end)

    def test_summary_shown(self):
        app_module.last_summary = {
            "ok": 7, "failed": 2, "unmapped": 3,
            "started_at": "2026-11-13T14:45:00", "finished_at": "2026-11-13T16:00:00",
        }
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("2026-11-13T16:00:00", html)
        self.assertIn("OK: 7, Failed: 2, Unmapped: 3", html)

    def test_get_sync_returns_405(self):
        resp = self.client.get("/sync")
        self.assertEqual(resp.status_code, 405)


if __name__ == "__main__":
    unittest.main()

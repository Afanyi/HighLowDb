import html as html_lib
import os
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

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


class TestSyncJobLogging(unittest.TestCase):
    def test_stock_sync_logs_start_and_summary_without_lists(self):
        summary = {"ok": 7, "no_data": 1, "failed": 1, "unmapped": 2, "failed_tickers": ["AAPL_US_EQ"],
                   "started_at": "2026-11-14T22:00:00", "finished_at": "2026-11-15T02:00:00"}
        with patch.object(app_module.sync, "run_sync", return_value=summary), \
                self.assertLogs("app", level="INFO") as logs:
            app_module.run_sync_job()
        out = "\n".join(logs.output)
        self.assertIn("stock sync started", out)
        self.assertIn("stock sync finished: ok=7, no_data=1, failed=1, unmapped=2", out)
        self.assertIn("finished_at=2026-11-15T02:00:00", out)
        self.assertNotIn("AAPL_US_EQ", out)

    def test_crypto_sync_logs_start_and_summary(self):
        summary = {"coins": 18230, "backfilled": 200, "backfill_pending": 18030, "failed": 0, "calls": 274,
                   "started_at": "a", "finished_at": "b"}
        with patch.object(app_module.crypto_sync, "run_sync", return_value=summary), \
                self.assertLogs("app", level="INFO") as logs:
            app_module.run_crypto_sync_job()
        out = "\n".join(logs.output)
        self.assertIn("crypto sync started", out)
        self.assertIn("crypto sync finished: coins=18230, backfilled=200", out)

    def test_failed_sync_logs_error_with_traceback(self):
        for job, module, name in ((app_module.run_sync_job, app_module.sync, "stock"),
                                  (app_module.run_crypto_sync_job, app_module.crypto_sync, "crypto")):
            with patch.object(module, "run_sync", side_effect=RuntimeError("boom")), \
                    self.assertLogs("app", level="ERROR") as logs:
                job()
            out = "\n".join(logs.output)
            self.assertIn(f"ERROR:app:{name} sync failed", out)
            self.assertIn("Traceback", out)
            self.assertIn("RuntimeError: boom", out)

    def test_skipped_sync_logs_line(self):
        for job, lock, name in ((app_module.run_sync_job, app_module._sync_lock, "stock"),
                                (app_module.run_crypto_sync_job, app_module._crypto_lock, "crypto")):
            self.assertTrue(lock.acquire(blocking=False))
            try:
                with self.assertLogs("app", level="INFO") as logs:
                    job()
            finally:
                lock.release()
            self.assertIn(f"{name} sync skipped: already running", "\n".join(logs.output))


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
                ("AD_AS_EQ", "Ahold Delhaize", "AMSTERDAM", 30.0, 35.0, 25.0, 40.0, 20.0, "ACTIVE"),
                ("XYZ_EQ", "Test Stock", "UK", 0.005, None, None, 10.0, 1.0, "DELISTED_OR_ZERO"),
                ("NEW_STOCK_EQ", "New Co", "US", 10.0, 12.0, 8.0, 12.0, 8.0, "NEW_2026"),
                ("SXR8d_EQ", "iShares Core", "FRANKFURT", 200.0, 220.0, 180.0, 250.0, 150.0, "ACTIVE"),
                ("PUA_PA_EQ", "Publicis", "PARIS", 60.0, 70.0, 50.0, 80.0, 40.0, "ACTIVE"),
                ("ITX_e_EQ", "Inditex", "MADRID", 35.0, 40.0, 30.0, 45.0, 25.0, "ACTIVE"),
                ("NESN_s_EQ", "Nestle", "SIX", 90.0, 100.0, 80.0, 110.0, 70.0, "ACTIVE"),
                ("VODl_EQ", "Vodafone", "LSE", 80.0, 90.0, 70.0, 100.0, 60.0, "ACTIVE"),
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
        self.client.get("/")
        with self.client.session_transaction() as session:
            token = session["csrf_token"]
        with patch.object(app_module.sync, "run_sync", side_effect=lambda **kw: done.set() or {}) as mock_run:
            resp = self.client.post("/sync", data={"csrf_token": token})
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
        filter_start = html.index('<form method="get"')
        filter_end = html.index("</form>", filter_start)
        self.assertNotIn('method="post"', html[filter_start:filter_end])

    def test_type_column_rendered(self):
        resp = self.client.get("/")
        html = resp.get_data(as_text=True)
        self.assertIn("Type", html)
        self.assertIn("STOCK", html)

    def test_filter_row_lines_up_with_header(self):
        for mode in ("stocks", "crypto"):
            html = self.client.get(f"/?mode={mode}").get_data(as_text=True)
            header, filters = html.split("<thead")[1].split("</thead>")[0].split("<tr")[1:3]
            cells = [sum(int(m.group(1) or 1) for m in re.finditer(r'<th(?:\s+colspan="(\d+)")?[\s>]', row))
                     for row in (header, filters)]
            self.assertEqual(cells[0], cells[1], mode)

    def test_type_filter_stock(self):
        resp = self.client.get("/?instrument_type=STOCK")
        html = resp.get_data(as_text=True)
        self.assertIn("AAPL_US_EQ", html)

    def test_type_filter_etf(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO stocks (ticker, name, exchange, instrument_type, status_tag, last_updated) "
            "VALUES ('SPY_US_EQ', 'SPY ETF', 'US', 'ETF', 'ACTIVE', datetime('now'))")
        conn.commit()
        conn.close()
        resp = self.client.get("/?instrument_type=ETF")
        html = resp.get_data(as_text=True)
        self.assertIn("SPY_US_EQ", html)
        self.assertNotIn("AAPL_US_EQ", html)
        resp = self.client.get("/?instrument_type=STOCK")
        html = resp.get_data(as_text=True)
        self.assertIn("AAPL_US_EQ", html)
        self.assertNotIn("SPY_US_EQ", html)

    def test_type_filter_bad_value_ignored(self):
        resp = self.client.get("/?instrument_type=bogus")
        html = resp.get_data(as_text=True)
        self.assertIn("AAPL_US_EQ", html)
        self.assertIn('value="" selected', html)

    def test_summary_shown(self):
        app_module.last_summary = {
            "ok": 7, "no_data": 4, "failed": 2, "unmapped": 3,
            "started_at": "2026-11-13T14:45:00", "finished_at": "2026-11-13T16:00:00",
        }
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("2026-11-13T16:00:00", html)
        self.assertEqual(html.count("OK: 7, No data: 4, Failed: 2, Unmapped: 3"), 1)

    def test_get_sync_returns_405(self):
        resp = self.client.get("/sync")
        self.assertEqual(resp.status_code, 405)


class TestDistanceFilters(unittest.TestCase):
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
                ("AD_AS_EQ", "Ahold Delhaize", "AMSTERDAM", 30.0, 35.0, 25.0, 40.0, 20.0, "ACTIVE"),
                ("XYZ_EQ", "Test Stock", "UK", 0.005, None, None, 10.0, 1.0, "DELISTED_OR_ZERO"),
                ("NEW_STOCK_EQ", "New Co", "US", 10.0, 12.0, 8.0, 12.0, 8.0, "NEW_2026"),
                ("SXR8d_EQ", "iShares Core", "FRANKFURT", 200.0, 220.0, 180.0, 250.0, 150.0, "ACTIVE"),
                ("PUA_PA_EQ", "Publicis", "PARIS", 60.0, 70.0, 50.0, 80.0, 40.0, "ACTIVE"),
                ("ITX_e_EQ", "Inditex", "MADRID", 35.0, 40.0, 30.0, 45.0, 25.0, "ACTIVE"),
                ("NESN_s_EQ", "Nestle", "SIX", 90.0, 100.0, 80.0, 110.0, 70.0, "ACTIVE"),
                ("VODl_EQ", "Vodafone", "LSE", 80.0, 90.0, 70.0, 100.0, 60.0, "ACTIVE"),
            ],
        )
        conn.commit()
        conn.close()
        self.client = app_module.app.test_client()

    def tearDown(self):
        os.unlink(self.db_path)
        app_module.DB_PATH = "trading212_stocks.db"

    def test_distance_values_for_aapl(self):
        rows = app_module._query_rows(sorts=[("ticker", "asc")])
        by_ticker = {r["ticker"]: r for r in rows}
        aapl = by_ticker["AAPL_US_EQ"]
        self.assertAlmostEqual(aapl["pct_year_max"], 9.1, places=1)
        self.assertAlmostEqual(aapl["pct_year_min"], 11.1, places=1)
        self.assertAlmostEqual(aapl["pct_all_time_max"], 33.3, places=1)
        self.assertAlmostEqual(aapl["pct_all_time_min"], 25.0, places=1)

    def test_min_and_max_filters(self):
        resp = self.client.get("/?min_pct_all_time_max=30")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"AAPL_US_EQ", resp.data)
        resp = self.client.get("/?min_pct_all_time_max=34")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"AAPL_US_EQ", resp.data)

    def test_max_year_min_filter(self):
        resp = self.client.get("/?max_pct_year_min=11.2")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"AAPL_US_EQ", resp.data)
        resp = self.client.get("/?max_pct_year_min=11")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"AAPL_US_EQ", resp.data)

    def test_min_and_max_on_same_distance_and_across_distances(self):
        resp = self.client.get("/?min_pct_all_time_max=30&max_pct_all_time_max=34")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"AAPL_US_EQ", resp.data)
        resp = self.client.get("/?min_pct_all_time_max=30&min_pct_year_min=10")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"AAPL_US_EQ", resp.data)

    def test_invalid_bounds_are_ignored(self):
        for value in ("abc", "nan", "inf", ""):
            resp = self.client.get(f"/?min_pct_year_max={value}")
            self.assertEqual(resp.status_code, 200)
            self.assertIn(b"AAPL_US_EQ", resp.data)

    def test_null_year_max_shows_dash_and_sorts_last(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"XYZ_EQ", resp.data)
        rows = app_module._query_rows(sorts=[("pct_year_max", "asc")])
        self.assertEqual(rows[-1]["ticker"], "XYZ_EQ")
        rows = app_module._query_rows(sorts=[("pct_year_max", "desc")])
        self.assertEqual(rows[-1]["ticker"], "XYZ_EQ")
        resp = self.client.get("/?min_pct_year_max=0")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"XYZ_EQ", resp.data)

    def test_group_shows_ranges_and_counts(self):
        resp = self.client.get("/?group=pct_all_time_max")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("10–25% · 5", html)
        self.assertIn("25–50% · 4", html)
        self.assertIn("50–100% · 1", html)
        self.assertIn('id="group-0"', html)
        resp = self.client.get("/?group=bogus")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertNotIn('id="group-', html)
        self.assertIn("AAPL_US_EQ", html)

    def test_sort_bogus_shows_marker_on_ticker(self):
        resp = self.client.get("/?sort=bogus")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("AAPL_US_EQ", html)
        self.assertIn("▲", html)

    def test_header_links_keep_filters(self):
        resp = self.client.get("/?group=pct_all_time_max&min_pct_year_max=5")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("group=pct_all_time_max", html)
        self.assertIn("min_pct_year_max=5", html)

    def test_bootstrap_link_and_no_script(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('bootstrap@5.3.3', html)
        self.assertIn(
            'sha384-QWTKZyjpPEjISv5WaRU9OFeRpok6YctnYmDr5pNlyT2bRjXh0JMhjY6hW+ALEwIH',
            html,
        )
        self.assertNotIn("<script", html)


class TestMultiColumnSort(unittest.TestCase):
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
                ("AAC_US_EQ", "AAC Corp", "US", 200.0, 90.0, 80.0, 250.0, 70.0, "ACTIVE"),
                ("AAA_US_EQ", "AAA Corp", "US", 100.0, 110.0, 90.0, 150.0, 80.0, "ACTIVE"),
                ("AAB_US_EQ", "AAB Corp", "US", 50.0, 110.0, 95.0, 140.0, 85.0, "ACTIVE"),
            ],
        )
        conn.commit()
        conn.close()
        self.client = app_module.app.test_client()

    def tearDown(self):
        os.unlink(self.db_path)
        app_module.DB_PATH = "trading212_stocks.db"

    def test_two_levels_tie_broken_by_second(self):
        # year_max asc: AAC(90) first; AAA/AAB tie at 110, name desc -> AAB before AAA
        resp = self.client.get("/?sort=year_max&dir=asc&sort=name&dir=desc")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertLess(html.index("AAC_US_EQ"), html.index("AAB_US_EQ"))
        self.assertLess(html.index("AAB_US_EQ"), html.index("AAA_US_EQ"))

    def test_three_levels_and_fourth_ignored(self):
        resp = self.client.get(
            "/?sort=year_max&dir=asc&sort=name&dir=desc&sort=exchange&dir=asc"
            "&sort=opening_price&dir=desc"
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Exchange ▲3", html)
        self.assertNotIn("Open ▼", html)

    def test_invalid_column_and_direction_dropped(self):
        resp = self.client.get("/?sort=bogus&dir=asc")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("AAC_US_EQ", resp.get_data(as_text=True))
        resp = self.client.get("/?sort=ticker&dir=notadir")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("AAC_US_EQ", resp.get_data(as_text=True))

    def test_duplicate_column_used_once(self):
        resp = self.client.get("/?sort=ticker&dir=asc&sort=ticker&dir=desc")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Ticker ▲<", html)
        self.assertNotIn("▼", html)

    def test_nulls_last_at_every_level(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO stocks (ticker, name, exchange, status_tag, last_updated) "
            "VALUES (?, ?, ?, 'ACTIVE', datetime('now'))",
            ("NULL_EQ", "Null Co", "US"),
        )
        conn.commit()
        conn.close()
        resp = self.client.get("/?sort=year_max&dir=asc")
        html = resp.get_data(as_text=True)
        self.assertLess(html.index("AAC_US_EQ"), html.index("NULL_EQ"))
        resp = self.client.get("/?sort=year_max&dir=desc")
        html = resp.get_data(as_text=True)
        self.assertLess(html.index("AAC_US_EQ"), html.index("NULL_EQ"))

    def test_old_single_sort_url_still_works(self):
        resp = self.client.get("/?sort=ticker&dir=desc")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertLess(html.index("AAC_US_EQ"), html.index("AAB_US_EQ"))
        self.assertLess(html.index("AAB_US_EQ"), html.index("AAA_US_EQ"))

    def test_level_markers(self):
        html = self.client.get("/?sort=year_max&dir=asc&sort=name&dir=desc").get_data(as_text=True)
        self.assertIn("Year High ▲1", html)
        self.assertIn("Name ▼2", html)

    def test_sort_controls_prefilled(self):
        html = self.client.get("/?sort=year_max&dir=asc&sort=name&dir=desc").get_data(as_text=True)
        sort_selects = re.findall(r'<select[^>]*name="sort".*?</select>', html, re.S)
        dir_selects = re.findall(r'<select[^>]*name="dir".*?</select>', html, re.S)
        self.assertEqual(len(sort_selects), 3)
        self.assertIn('value="year_max" selected', sort_selects[0])
        self.assertIn('value="name" selected', sort_selects[1])
        self.assertNotIn("selected", sort_selects[2])
        self.assertIn('value="asc" selected', dir_selects[0])
        self.assertIn('value="desc" selected', dir_selects[1])

    def test_header_links_keep_sort_levels_and_filters(self):
        html = self.client.get(
            "/?status=ACTIVE&q=Corp&country=United+States&min_pct_year_max=-50"
            "&sort=year_max&dir=asc&sort=name&dir=desc"
        ).get_data(as_text=True)
        hrefs = [html_lib.unescape(h) for h in re.findall(r'<th[^>]*>\s*<a href="([^"]+)"', html)]
        links = {parse_qs(urlsplit(h).query)["sort"][0]: parse_qs(urlsplit(h).query) for h in hrefs}
        # Clicking another column makes it level 1 and moves the others down.
        self.assertEqual(links["opening_price"]["sort"], ["opening_price", "year_max", "name"])
        self.assertEqual(links["opening_price"]["dir"], ["asc", "asc", "desc"])
        # Clicking the primary column flips only its direction.
        self.assertEqual(links["year_max"]["sort"], ["year_max", "name"])
        self.assertEqual(links["year_max"]["dir"], ["desc", "desc"])
        for query in links.values():
            self.assertEqual(query["status"], ["ACTIVE"])
            self.assertEqual(query["q"], ["Corp"])
            self.assertEqual(query["country"], ["United States"])
            self.assertEqual(query["min_pct_year_max"], ["-50"])


class _Args:
    def __init__(self, **kw):
        self._kw = {k: list(v) if isinstance(v, (list, tuple)) else [v] for k, v in kw.items()}

    def getlist(self, name):
        return self._kw.get(name, [])


class TestHelpers(unittest.TestCase):
    def test_parse_pct(self):
        self.assertEqual(app_module._parse_pct("30"), 30.0)
        self.assertEqual(app_module._parse_pct("12.5"), 12.5)
        self.assertIsNone(app_module._parse_pct(""))
        self.assertIsNone(app_module._parse_pct("abc"))
        self.assertIsNone(app_module._parse_pct("nan"))
        self.assertIsNone(app_module._parse_pct("inf"))

    def test_parse_sorts(self):
        args = _Args(sort=["year_max", "name"], dir=["asc", "desc"])
        self.assertEqual(app_module._parse_sorts(args), [("year_max", "asc"), ("name", "desc")])
        args = _Args(sort=["bogus", "ticker"], dir=["asc", "asc"])
        self.assertEqual(app_module._parse_sorts(args), [("ticker", "asc")])
        args = _Args(sort=["ticker", "ticker"], dir=["asc", "desc"])
        self.assertEqual(app_module._parse_sorts(args), [("ticker", "asc")])
        args = _Args(sort=["ticker", "name", "exchange", "opening_price"], dir=["asc"] * 4)
        self.assertEqual(
            app_module._parse_sorts(args),
            [("ticker", "asc"), ("name", "asc"), ("exchange", "asc")],
        )
        args = _Args(sort=[], dir=[])
        self.assertEqual(app_module._parse_sorts(args), [("ticker", "asc")])
        args = _Args(sort=["ticker"], dir=["bogus"])
        self.assertEqual(app_module._parse_sorts(args), [("ticker", "asc")])

    def test_header_sorts(self):
        sorts = [("ticker", "asc")]
        self.assertEqual(app_module._header_sorts(sorts, "ticker"), [("ticker", "desc")])
        self.assertEqual(app_module._header_sorts(sorts, "name"), [("name", "asc"), ("ticker", "asc")])
        sorts = [("ticker", "asc"), ("name", "desc")]
        self.assertEqual(
            app_module._header_sorts(sorts, "name"), [("name", "desc"), ("ticker", "asc")]
        )
        sorts = [("ticker", "asc"), ("name", "desc"), ("exchange", "asc")]
        self.assertEqual(
            app_module._header_sorts(sorts, "opening_price"),
            [("opening_price", "asc"), ("ticker", "asc"), ("name", "desc")],
        )

    def test_bucket_label(self):
        self.assertEqual(app_module._bucket_label(None, 0), "< 0%")
        self.assertEqual(app_module._bucket_label(0, 10), "0–10%")
        self.assertEqual(app_module._bucket_label(25, 50), "25–50%")
        self.assertEqual(app_module._bucket_label(100, None), "≥ 100%")

    def test_group_rows(self):
        rows = [
            {"pct_all_time_max": 5.0},
            {"pct_all_time_max": 15.0},
            {"pct_all_time_max": 30.0},
            {"pct_all_time_max": 120.0},
            {"pct_all_time_max": None},
        ]
        groups = app_module._group_rows(rows, "pct_all_time_max")
        self.assertEqual(
            [label for label, _ in groups],
            ["0–10%", "10–25%", "25–50%", "≥ 100%", "n/a"],
        )
        self.assertEqual(len(groups[0][1]), 1)
        self.assertEqual(len(groups[3][1]), 1)
        self.assertEqual(len(groups[4][1]), 1)
        self.assertEqual(app_module._group_rows(rows, None), [(None, rows)])


class TestCountryFilter(unittest.TestCase):
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
                ("SXR8d_EQ", "iShares Core", "FRANKFURT", 200.0, 220.0, 180.0, 250.0, 150.0, "ACTIVE"),
                ("AD_AS_EQ", "Ahold Delhaize", "AMSTERDAM", 30.0, 35.0, 25.0, 40.0, 20.0, "ACTIVE"),
                ("PUA_PA_EQ", "Publicis", "PARIS", 60.0, 70.0, 50.0, 80.0, 40.0, "ACTIVE"),
            ],
        )
        conn.commit()
        conn.close()
        self.client = app_module.app.test_client()

    def tearDown(self):
        os.unlink(self.db_path)
        app_module.DB_PATH = "trading212_stocks.db"

    def test_country_germany_shows_only_frankfurt(self):
        resp = self.client.get("/?country=Germany")
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn("SXR8d_EQ", html)
        self.assertNotIn("AAPL_US_EQ", html)
        self.assertNotIn("AD_AS_EQ", html)

    def test_unknown_country_ignored_shows_all_rows(self):
        resp = self.client.get("/?country=Atlantis")
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn("AAPL_US_EQ", html)
        self.assertIn("SXR8d_EQ", html)
        self.assertIn("AD_AS_EQ", html)

    def test_country_dropdown_lists_every_country(self):
        resp = self.client.get("/")
        html = resp.data.decode()
        for country in sorted(app_module.sync.EXCHANGE_COUNTRIES.values()):
            self.assertIn(f'value="{country}"', html)

    def test_country_selected_stays_selected(self):
        resp = self.client.get("/?country=Germany")
        html = resp.data.decode()
        self.assertIn('value="Germany" selected', html)


class TestSortLinks(unittest.TestCase):
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
            ],
        )
        conn.commit()
        conn.close()
        self.client = app_module.app.test_client()

    def tearDown(self):
        os.unlink(self.db_path)
        app_module.DB_PATH = "trading212_stocks.db"

    def test_every_column_header_is_a_sort_link(self):
        resp = self.client.get("/")
        html = resp.data.decode()
        for col in app_module.ALLOWED_SORTS:
            self.assertIn(f'sort={col}', html)

    def test_active_column_flips_direction(self):
        resp = self.client.get("/?sort=ticker&dir=asc")
        html = resp.data.decode()
        # Active column (ticker, asc) should link to desc. HTML escapes & as &amp;.
        self.assertIn('sort=ticker&amp;dir=desc', html)

    def test_links_keep_status_q_country(self):
        resp = self.client.get("/?status=ACTIVE&q=apple&country=Germany")
        html = resp.data.decode()
        self.assertIn("ACTIVE", html)
        self.assertIn("apple", html)
        self.assertIn("Germany", html)


class TestNullsLast(unittest.TestCase):
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
                ("A_WITH_YEAR_MAX", "A", "US", 100.0, 110.0, 90.0, 150.0, 80.0, "ACTIVE"),
                ("B_NO_YEAR_MAX", "B", "US", 50.0, None, None, 100.0, 40.0, "ACTIVE"),
                ("C_WITH_YEAR_MAX", "C", "US", 75.0, 90.0, 60.0, 120.0, 50.0, "ACTIVE"),
            ],
        )
        conn.commit()
        conn.close()
        self.client = app_module.app.test_client()

    def tearDown(self):
        os.unlink(self.db_path)
        app_module.DB_PATH = "trading212_stocks.db"

    def test_year_max_nulls_last_asc(self):
        rows = app_module._query_rows(sorts=[("year_max", "asc")])
        tickers = [r["ticker"] for r in rows]
        self.assertEqual(tickers.index("B_NO_YEAR_MAX"), len(tickers) - 1)

    def test_year_max_nulls_last_desc(self):
        rows = app_module._query_rows(sorts=[("year_max", "desc")])
        tickers = [r["ticker"] for r in rows]
        self.assertEqual(tickers.index("B_NO_YEAR_MAX"), len(tickers) - 1)


class TestNoRowCap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        app_module.DB_PATH = self.db_path
        app_module.last_summary = None

        import sync as sync_mod
        sync_mod.init_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        rows = [
            (f"TICK{i:04d}_US_EQ", f"Name {i}", "US", float(i), float(i + 10),
             float(i - 10), float(i + 50), float(i - 50), "ACTIVE")
            for i in range(600)
        ]
        conn.executemany(
            "INSERT INTO stocks (ticker, name, exchange, opening_price, year_max, year_min, "
            "all_time_max, all_time_min, status_tag, last_updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            rows,
        )
        conn.commit()
        conn.close()
        self.client = app_module.app.test_client()

    def tearDown(self):
        os.unlink(self.db_path)
        app_module.DB_PATH = "trading212_stocks.db"

    def test_all_600_rows_returned_no_cap(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn("TICK0000_US_EQ", html)
        self.assertIn("TICK0599_US_EQ", html)
        self.assertEqual(html.count("US_EQ"), 600)


class TestHealth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        app_module.DB_PATH = self.db_path
        self.client = app_module.app.test_client()

    def tearDown(self):
        os.unlink(self.db_path)
        app_module.DB_PATH = "trading212_stocks.db"

    def test_health_returns_ok(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"ok")


class TestNextRunCrypto(unittest.TestCase):
    def test_crypto_next_run_fires_on_saturday(self):
        # Saturday 01:00 -> next crypto run is Saturday 02:00 (weekends are NOT skipped).
        saturday = datetime(2026, 1, 10, 1, 0, tzinfo=timezone.utc)
        nr = app_module.next_run(now=saturday, hh=2, mm=0, weekdays_only=False)
        self.assertEqual(nr.weekday(), 5)
        self.assertEqual(nr.hour, 2)
        self.assertEqual(nr.minute, 0)

    def test_crypto_next_run_fires_on_sunday(self):
        # Sunday 01:00 -> next crypto run is Sunday 02:00.
        sunday = datetime(2026, 1, 11, 1, 0, tzinfo=timezone.utc)
        nr = app_module.next_run(now=sunday, hh=2, mm=0, weekdays_only=False)
        self.assertEqual(nr.weekday(), 6)
        self.assertEqual(nr.hour, 2)
        self.assertEqual(nr.minute, 0)

    def test_crypto_next_run_still_respects_time_after_midnight(self):
        # Monday 01:00 -> next crypto run is Monday 02:00
        monday = datetime(2026, 1, 12, 1, 0, tzinfo=timezone.utc)
        nr = app_module.next_run(now=monday, hh=2, mm=0, weekdays_only=False)
        self.assertEqual(nr.weekday(), 0)
        self.assertEqual(nr.hour, 2)


class TestCryptoMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self.crypto_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.crypto_tmp.close()
        self.crypto_db_path = self.crypto_tmp.name
        app_module.DB_PATH = self.db_path
        app_module.CRYPTO_DB_PATH = self.crypto_db_path
        app_module.last_summary = None
        app_module.last_crypto_summary = None

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
            ],
        )
        conn.commit()
        conn.close()

        import crypto_sync as crypto_sync_mod
        crypto_sync_mod.init_db(self.crypto_db_path)
        conn = sqlite3.connect(self.crypto_db_path)
        conn.executemany(
            "INSERT INTO crypto (ticker, symbol, name, market_cap_rank, opening_price, "
            "year_max, year_min, all_time_max, all_time_min, status_tag, last_updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            [
                ("bitcoin", "btc", "Bitcoin", 1, 100000.0, 110000.0, 90000.0, 120000.0, 80000.0, "ACTIVE"),
                ("pepe", "pepe", "Pepe", 1500, 0.0000035, 0.000004, 0.000003, 0.000005, 0.000002, "ACTIVE"),
            ],
        )
        conn.commit()
        conn.close()

        self.client = app_module.app.test_client()

    def tearDown(self):
        os.unlink(self.db_path)
        os.unlink(self.crypto_db_path)
        app_module.DB_PATH = "trading212_stocks.db"
        app_module.CRYPTO_DB_PATH = "crypto.db"

    def test_mode_stock_renders_stocks(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"AAPL_US_EQ", resp.data)

    def test_mode_stocks_explicit_renders_stocks(self):
        resp = self.client.get("/?mode=stocks")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"AAPL_US_EQ", resp.data)

    def test_mode_crypto_renders_coins(self):
        resp = self.client.get("/?mode=crypto")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"btc", resp.data)
        self.assertIn(b"Bitcoin", resp.data)
        self.assertIn(b"Pepe", resp.data)
        self.assertNotIn(b"AAPL_US_EQ", resp.data)

    def test_invalid_mode_falls_back_to_stocks(self):
        resp = self.client.get("/?mode=foo")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"AAPL_US_EQ", resp.data)
        self.assertNotIn(b"bitcoin", resp.data)

    def test_crypto_distance_filter_both_ends_included(self):
        # bitcoin is 16.67 % below its ATH (between 0 and 20), pepe is 30 % (excluded).
        resp = self.client.get("/?mode=crypto&min_pct_all_time_max=0&max_pct_all_time_max=20")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Bitcoin", html)
        self.assertNotIn("pepe", html)

    def test_header_links_keep_mode_crypto(self):
        resp = self.client.get("/?mode=crypto")
        html = resp.get_data(as_text=True)
        self.assertIn("mode=crypto", html)

    def test_apply_keeps_mode_crypto(self):
        resp = self.client.get("/?mode=crypto&q=bitcoin")
        html = resp.get_data(as_text=True)
        self.assertIn("mode=crypto", html)

    def test_post_sync_crypto_returns_302_and_starts_crypto_job(self):
        done = threading.Event()
        self.client.get("/?mode=crypto")
        with self.client.session_transaction() as session:
            token = session["csrf_token"]
        with patch.object(
            app_module.crypto_sync, "run_sync", side_effect=lambda **kw: done.set() or {}
        ) as mock_run:
            resp = self.client.post("/sync/crypto", data={"csrf_token": token})
            self.assertEqual(resp.status_code, 302)
            self.assertTrue(done.wait(5))
        mock_run.assert_called_once_with(db_path=self.crypto_db_path)

    def test_locks_are_independent(self):
        with patch.object(app_module.crypto_sync, "run_sync", return_value={}) as mock_crypto:
            app_module._sync_lock.acquire(blocking=False)
            app_module.run_crypto_sync_job()
            mock_crypto.assert_called_once_with(db_path=self.crypto_db_path)
            app_module._sync_lock.release()

        with patch.object(app_module.sync, "run_sync", return_value={}) as mock_stock:
            app_module._crypto_lock.acquire(blocking=False)
            app_module.run_sync_job()
            mock_stock.assert_called_once_with(db_path=self.db_path)
            app_module._crypto_lock.release()

    def test_crypto_shows_attribution_and_no_script(self):
        resp = self.client.get("/?mode=crypto")
        html = resp.get_data(as_text=True)
        self.assertIn("CoinGecko", html)
        self.assertIn("coingecko.com", html)
        self.assertIn('rel="noopener"', html)
        self.assertNotIn("<script", html)

    def test_tiny_crypto_price_not_zero(self):
        resp = self.client.get("/?mode=crypto")
        html = resp.get_data(as_text=True)
        self.assertIn("3.5e-06", html)

    def test_crypto_search_matches_symbol(self):
        # "btc" is in neither the CoinGecko id "bitcoin" nor the name "Bitcoin", only the symbol.
        html = self.client.get("/?mode=crypto&q=BTC").get_data(as_text=True)
        self.assertIn("Bitcoin", html)
        self.assertNotIn("Pepe", html)

    def test_stock_search_still_matches_ticker_and_name(self):
        self.assertIn(b"AAPL_US_EQ", self.client.get("/?q=Apple").data)
        self.assertIn(b"BPl_EQ", self.client.get("/?q=BPl").data)
        self.assertNotIn(b"BPl_EQ", self.client.get("/?q=Apple").data)

    def test_crypto_default_order_is_rank_with_unranked_last(self):
        conn = sqlite3.connect(self.crypto_db_path)
        conn.execute(
            "INSERT INTO crypto (ticker, symbol, name, market_cap_rank, opening_price, status_tag, last_updated) "
            "VALUES ('aaa-unranked', 'aaa', 'Aaa Unranked', NULL, 1.0, 'ACTIVE', datetime('now'))"
        )
        conn.commit()
        conn.close()
        html = self.client.get("/?mode=crypto").get_data(as_text=True)
        self.assertLess(html.index("Bitcoin"), html.index("Pepe"))
        self.assertLess(html.index("Pepe"), html.index("Aaa Unranked"))
        self.assertIn('value="market_cap_rank" selected', html)

    def test_search_label_follows_mode(self):
        self.assertIn('aria-label="Search symbol or name"', self.client.get("/?mode=crypto").get_data(as_text=True))
        self.assertIn('aria-label="Search ticker or name"', self.client.get("/").get_data(as_text=True))

    def test_crypto_mode_no_type_filter(self):
        self.assertNotIn('name="instrument_type"', self.client.get("/?mode=crypto").get_data(as_text=True))

    def test_new_this_year_status_only_in_stock_mode(self):
        self.assertNotIn("New this year</option>", self.client.get("/?mode=crypto").get_data(as_text=True))
        self.assertIn("New this year</option>", self.client.get("/").get_data(as_text=True))


class TestRequestSecurity(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(app_module, "DB_PATH", directory + "/stocks.db"))
        self.enterContext(patch.object(app_module, "CRYPTO_DB_PATH", directory + "/crypto.db"))
        self.enterContext(patch.dict(app_module.app.config, app_module._security_config({})))
        self.client = app_module.app.test_client()

    def token(self, client=None):
        client = client or self.client
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        with client.session_transaction() as session:
            token = session["csrf_token"]
        self.assertIn(f'name="csrf_token" value="{token}"'.encode(), response.data)
        return token

    def test_valid_sync_forms_for_both_datasets(self):
        token = self.token()
        for path, target in (("/sync", app_module.run_sync_job),
                             ("/sync/crypto", app_module.run_crypto_sync_job)):
            with self.subTest(path=path), patch.object(app_module.threading, "Thread") as thread:
                response = self.client.post(path, data={"csrf_token": token},
                                            headers={"Origin": "http://localhost"})
                self.assertEqual(response.status_code, 302)
                thread.assert_called_once_with(target=target, daemon=True)

    def test_missing_invalid_and_non_ascii_tokens_rejected(self):
        self.token()
        for path in ("/sync", "/sync/crypto"):
            for data in ({}, {"csrf_token": "incorrect"}, {"csrf_token": "é"}):
                with self.subTest(path=path, data=data), patch.object(app_module.threading, "Thread") as thread:
                    self.assertEqual(self.client.post(path, data=data).status_code, 403)
                    thread.assert_not_called()

    def test_token_from_another_browser_is_rejected(self):
        other = app_module.app.test_client()
        self.token()
        token = self.token(other)
        self.assertEqual(self.client.post("/sync", data={"csrf_token": token}).status_code, 403)

    def test_cross_site_requests_with_valid_tokens_are_rejected(self):
        token = self.token()
        for headers in ({"Origin": "https://attacker.example"}, {"Origin": "null"},
                        {"Referer": "https://attacker.example/form"},
                        {"Referer": "http://[malformed"}, {"Sec-Fetch-Site": "cross-site"}):
            for path in ("/sync", "/sync/crypto"):
                with self.subTest(path=path, headers=headers), patch.object(app_module.threading, "Thread") as thread:
                    response = self.client.post(path, data={"csrf_token": token}, headers=headers)
                    self.assertEqual(response.status_code, 403)
                    thread.assert_not_called()

    def test_untrusted_hosts_rejected_even_for_health(self):
        for path in ("/", "/health", "/sync"):
            response = self.client.get(path, headers={"Host": "attacker.example"})
            self.assertEqual(response.status_code, 400)

    def test_forwarded_headers_do_not_override_host_or_origin(self):
        self.assertEqual(self.client.get("/health", headers={
            "Host": "attacker.example", "X-Forwarded-Host": "localhost",
        }).status_code, 400)
        token = self.token()
        self.assertEqual(self.client.post("/sync", data={"csrf_token": token}, headers={
            "Origin": "https://localhost", "X-Forwarded-Proto": "https",
        }).status_code, 403)

    def test_health_needs_no_session_or_form_token(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"ok")
        self.assertNotIn("Set-Cookie", response.headers)

    def test_large_form_rejected_without_starting_a_job(self):
        token = self.token()
        with patch.object(app_module.threading, "Thread") as thread:
            response = self.client.post("/sync", data={"csrf_token": token, "extra": "x" * 17000})
            self.assertEqual(response.status_code, 413)
            thread.assert_not_called()

    def test_production_cookies_and_origin_behind_https_proxy(self):
        config = app_module._security_config({
            "APP_PUBLIC_ORIGIN": "https://stocks.example", "APP_SECRET_KEY": "test-only-" * 8,
        })
        with patch.dict(app_module.app.config, config):
            client = app_module.app.test_client()
            response = client.get("/", base_url="https://stocks.example")
            self.assertEqual(response.status_code, 200)
            cookie = response.headers["Set-Cookie"]
            for value in ("__Host-allstocks_session=", "Secure", "HttpOnly", "SameSite=Strict", "Path=/"):
                self.assertIn(value, cookie)
            token = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
            with patch.object(app_module.threading, "Thread") as thread:
                response = client.post("/sync", base_url="https://stocks.example",
                                       headers={"Origin": "https://stocks.example",
                                                "Referer": "https://stocks.example/?mode=stocks"},
                                       data={"csrf_token": token})
                self.assertEqual(response.status_code, 302)
                thread.assert_called_once()
            # A forged proxy header cannot authorize an HTTP origin.
            response = client.post("/sync", base_url="https://stocks.example",
                                   headers={"Origin": "http://stocks.example", "X-Forwarded-Proto": "http"},
                                   data={"csrf_token": token})
            self.assertEqual(response.status_code, 403)

    def test_public_configuration_fails_closed(self):
        for origin in ("http://stocks.example", "https://stocks.example/path",
                       "https://user:password@stocks.example", "https://stocks.example:443",
                       "https://stocks.example?query", "https://stocks.example#fragment"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                app_module._security_config({"APP_PUBLIC_ORIGIN": origin, "APP_SECRET_KEY": "test-only-" * 8})
        for key in ("", "short"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                app_module._security_config({"APP_PUBLIC_ORIGIN": "https://stocks.example", "APP_SECRET_KEY": key})


if __name__ == "__main__":
    unittest.main()

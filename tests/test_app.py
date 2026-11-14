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
        filter_start = html.index('<form method="get"')
        filter_end = html.index("</form>", filter_start)
        self.assertNotIn('method="post"', html[filter_start:filter_end])

    def test_summary_shown(self):
        app_module.last_summary = {
            "ok": 7, "failed": 2, "unmapped": 3,
            "started_at": "2026-11-13T14:45:00", "finished_at": "2026-11-13T16:00:00",
        }
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("2026-11-13T16:00:00", html)
        self.assertEqual(html.count("OK: 7, Failed: 2, Unmapped: 3"), 1)

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


if __name__ == "__main__":
    unittest.main()

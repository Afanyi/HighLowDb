from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import app
import refresh


class StopLoop(BaseException):
    pass


class TestRefresh(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.db = self.directory + "/stocks.db"
        self.enterContext(patch.object(app, "DB_PATH", self.db))
        self.enterContext(patch.object(app, "CRYPTO_DB_PATH", self.directory + "/crypto.db"))
        self.enterContext(patch.dict(os.environ, {"SYNC_TIME_UTC": "14:45", "CRYPTO_SYNC_TIME_UTC": "02:00"}))
        self.now = datetime(2026, 1, 6, 14, 46, tzinfo=timezone.utc)

    def run_stock(self, result=None, error=None):
        with patch.object(app, "datetime") as clock, patch.object(app.sync, "run_sync") as provider:
            clock.now.return_value = self.now
            provider.return_value = result if result is not None else {"ok": 8, "failed": 0}
            provider.side_effect = error
            self.assertTrue(app.run_sync_job())
        return refresh.status(self.db, "stocks", self.now)

    def make_due(self, mode="stocks"):
        return refresh.due_at(self.db, mode, self.now - timedelta(minutes=2))

    def test_completion_survives_a_fresh_process(self):
        self.make_due()
        status = self.run_stock()
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["source"], "manual")
        self.assertFalse(status["stale"])
        output = subprocess.check_output([sys.executable, "-c",
            "import refresh,sys,json; print(json.dumps(refresh.status(sys.argv[1], 'stocks')))", self.db], text=True)
        saved = json.loads(output)
        for key in ("started_at", "finished_at", "last_success_at", "summary", "due_at"):
            self.assertEqual(saved[key], status[key])
        self.assertEqual(status["next_run"], "2026-01-07T14:45:00+00:00")

    def test_failure_and_partial_coverage_keep_previous_success(self):
        success = self.run_stock()["last_success_at"]
        self.now += timedelta(days=1)
        partial = self.run_stock({"ok": 7, "failed": 1})
        self.assertEqual(partial["state"], "partial")
        self.assertEqual(partial["last_success_at"], success)
        # One day after success: not stale under the 7-day rule.
        self.assertFalse(partial["stale"])
        failed = self.run_stock(error=RuntimeError("secret-provider-value"))
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["last_success_at"], success)
        self.assertNotIn("secret-provider-value", json.dumps(failed))
        self.assertNotIn(b"secret-provider-value", Path(self.db + ".refresh.db").read_bytes())
        self.assertEqual(app.app.test_client().get("/health").data, b"ok")

    def test_only_allowlisted_counts_are_persisted(self):
        with self.assertLogs("app", level="INFO") as logs:
            status = self.run_stock({"ok": 8, "failed": 0, "token": "secret", "failed_tickers": ["private"],
                                     "started_at": "not-a-timestamp", "calls": True})
        self.assertEqual(status["summary"], {"ok": 8, "failed": 0})
        self.assertEqual(status["started_at"], self.now.isoformat())
        self.assertNotIn("secret", json.dumps(status))
        self.assertNotIn("secret", "\n".join(logs.output))
        self.assertNotIn("not-a-timestamp", "\n".join(logs.output))

    def test_never_run_and_empty_result_are_not_success(self):
        status = refresh.status(self.db, "stocks", self.now)
        self.assertEqual(status["state"], "never")
        self.assertTrue(status["stale"])
        self.assertIsNone(status["last_success_at"])
        status = self.run_stock({"ok": 0, "failed": 0})
        self.assertEqual(status["state"], "partial")
        self.assertIsNone(status["last_success_at"])

    def test_weekend_freshness_uses_last_weekday_slot(self):
        friday = datetime(2026, 1, 9, 16, tzinfo=timezone.utc)
        self.now = friday
        self.run_stock()
        sunday = friday + timedelta(days=2)
        self.assertFalse(refresh.status(self.db, "stocks", sunday)["stale"])
        self.assertEqual(refresh.status(self.db, "stocks", sunday)["next_run"], "2026-01-12T14:45:00+00:00")
        # Three days after success: still not stale under the 7-day rule.
        self.assertFalse(refresh.status(self.db, "stocks", sunday + timedelta(days=1))["stale"])

    def test_missed_weekday_slot_waits_until_monday_instead_of_running_on_weekend(self):
        friday = datetime(2026, 1, 9, 14, 44, tzinfo=timezone.utc)
        pending = refresh.due_at(self.db, "stocks", friday)
        saturday = friday + timedelta(days=1)
        with patch.object(app, "datetime") as clock, patch.object(app.sync, "run_sync") as provider:
            clock.now.return_value = saturday
            self.assertTrue(app.run_sync_job(source="scheduled"))
            provider.assert_not_called()
        status = refresh.status(self.db, "stocks", saturday)
        self.assertEqual(status["due_at"], pending.isoformat())
        self.assertEqual(status["next_run"], "2026-01-12T14:45:00+00:00")
        self.assertTrue(status["overdue"])

    def test_active_jobs_and_deployment_exclude_each_other(self):
        with refresh.job_guard(self.db, "stocks") as stock:
            self.assertTrue(stock)
            with refresh.job_guard(self.db, "stocks") as duplicate:
                self.assertFalse(duplicate)
            with refresh.job_guard(self.db, "crypto") as crypto:
                self.assertTrue(crypto)
            with self.assertRaises(TimeoutError), refresh.deployment_guard(self.db, timeout=0):
                self.fail("Deployment must not interrupt an active stock job")
        with refresh.deployment_guard(self.db, timeout=0):
            for mode in ("stocks", "crypto"):
                with refresh.job_guard(self.db, mode) as admitted:
                    self.assertFalse(admitted)
            self.assertTrue(refresh.status(self.db, "stocks", self.now)["paused"])
        self.assertFalse(refresh.status(self.db, "stocks", self.now)["paused"])

    def test_weekend_deferral_survives_early_monday_restart_and_runs_once_at_slot(self):
        friday = datetime(2026, 1, 9, 14, 44, tzinfo=timezone.utc)
        monday = datetime(2026, 1, 12, 0, 1, tzinfo=timezone.utc)
        slot = monday.replace(hour=14, minute=45)
        pending = refresh.due_at(self.db, "stocks", friday)
        self.assertEqual(refresh.status(self.db, "stocks", friday + timedelta(days=1))["next_run"], slot.isoformat())
        output = subprocess.check_output([sys.executable, "-c", """import refresh,sys,json
from datetime import datetime
now = datetime.fromisoformat(sys.argv[2])
print(json.dumps(refresh.status(sys.argv[1], 'stocks', now)))
""", self.db, monday.isoformat()], text=True)
        restarted = json.loads(output)
        self.assertEqual(restarted["next_run"], slot.isoformat())
        self.assertEqual(restarted["due_at"], pending.isoformat())
        with patch.object(app, "datetime") as clock, patch.object(app.time, "sleep", side_effect=StopLoop), \
                patch.object(app.sync, "run_sync", return_value={"ok": 5, "failed": 0}) as provider:
            clock.now.return_value = monday
            with self.assertRaises(StopLoop):
                app._scheduler_loop("stocks", app.run_sync_job, self.db)
            self.assertTrue(app.run_sync_job(source="scheduled"))
            provider.assert_not_called()
            clock.now.return_value = slot
            with self.assertRaises(StopLoop):
                app._scheduler_loop("stocks", app.run_sync_job, self.db)
            self.assertTrue(app.run_sync_job(source="scheduled"))
            provider.assert_called_once()
        self.assertEqual(refresh.status(self.db, "stocks", slot)["next_run"], "2026-01-13T14:45:00+00:00")

    def test_weekend_recovery_uses_configured_time_and_coalesces_multiple_weeks(self):
        friday = datetime(2025, 12, 26, 14, 44, tzinfo=timezone.utc)
        refresh.due_at(self.db, "stocks", friday)
        monday = datetime(2026, 1, 12, 0, 1, tzinfo=timezone.utc)
        with patch.dict(os.environ, {"SYNC_TIME_UTC": "09:15"}):
            expected = monday.replace(hour=9, minute=15)
            self.assertEqual(refresh.ready_at(self.db, "stocks", monday), expected)
            self.assertEqual(refresh.status(self.db, "stocks", monday)["next_run"], expected.isoformat())
            self.assertEqual(refresh.ready_at(self.db, "stocks", monday.replace(hour=10)), expected)

    def test_ordinary_weekday_deployment_delay_and_crypto_recovery_remain_immediate(self):
        due = self.make_due()
        self.assertEqual(refresh.ready_at(self.db, "stocks", self.now), due)
        self.assertLessEqual(due, self.now)
        friday = datetime(2026, 1, 9, 1, 59, tzinfo=timezone.utc)
        crypto_due = refresh.due_at(self.db, "crypto", friday)
        monday = datetime(2026, 1, 12, 0, 1, tzinfo=timezone.utc)
        self.assertEqual(refresh.ready_at(self.db, "crypto", monday), crypto_due)
        self.assertLessEqual(crypto_due, monday)

    def test_deployment_waits_for_an_active_job_to_finish(self):
        started, release, drained = threading.Event(), threading.Event(), threading.Event()
        def job():
            with refresh.job_guard(self.db, "stocks"):
                started.set()
                release.wait(5)
        def deploy():
            with refresh.deployment_guard(self.db, timeout=3):
                drained.set()
        worker = threading.Thread(target=job)
        deployer = threading.Thread(target=deploy)
        worker.start()
        self.assertTrue(started.wait(2))
        deployer.start()
        try:
            self.assertFalse(drained.wait(0.05))
        finally:
            release.set()
            worker.join(5)
            deployer.join(5)
        self.assertTrue(drained.is_set())

    def test_due_slot_survives_deployment_and_runs_once_after_release(self):
        due = self.make_due()
        with patch.object(app, "datetime") as clock, patch.object(app.time, "sleep", side_effect=StopLoop), \
                patch.object(app.sync, "run_sync", return_value={"ok": 5, "failed": 0}) as provider:
            clock.now.return_value = self.now
            with refresh.deployment_guard(self.db, timeout=0), self.assertRaises(StopLoop):
                app._scheduler_loop("stocks", app.run_sync_job, self.db)
            provider.assert_not_called()
            self.assertEqual(refresh.due_at(self.db, "stocks"), due)
            with self.assertRaises(StopLoop):
                app._scheduler_loop("stocks", app.run_sync_job, self.db)
            provider.assert_called_once()
            status = refresh.status(self.db, "stocks", self.now)
            self.assertEqual(status["source"], "scheduled")
            self.assertGreater(datetime.fromisoformat(status["next_run"]), self.now)

    def test_manual_completion_while_scheduler_sleeps_does_not_duplicate(self):
        before = self.now - timedelta(minutes=2)
        refresh.due_at(self.db, "stocks", before)
        with patch.object(app, "datetime") as clock, patch.object(app.sync, "run_sync", return_value={"ok": 5}) as provider:
            clock.now.return_value = before
            def sleep(seconds):
                if clock.now.return_value == before:
                    clock.now.return_value = self.now
                    app.run_sync_job()
                else:
                    raise StopLoop()
            with patch.object(app.time, "sleep", side_effect=sleep), self.assertRaises(StopLoop):
                app._scheduler_loop("stocks", app.run_sync_job, self.db)
            provider.assert_called_once()

    def test_scheduled_admission_rechecks_due_slot_after_manual_completion(self):
        self.make_due()
        with patch.object(app, "datetime") as clock, patch.object(app.sync, "run_sync", return_value={"ok": 5}) as provider:
            clock.now.return_value = self.now
            app.run_sync_job()
            self.assertTrue(app.run_sync_job(source="scheduled"))
            provider.assert_called_once()

    def test_crypto_daily_slot_runs_on_sunday_and_coalesces_missed_days(self):
        before = datetime(2026, 1, 11, 1, 59, tzinfo=timezone.utc)
        refresh.due_at(self.db, "crypto", before)
        after = before + timedelta(days=3, minutes=2)
        with patch.object(app, "datetime") as clock, patch.object(app.time, "sleep", side_effect=StopLoop), \
                patch.object(app.crypto_sync, "run_sync", return_value={"coins": 10, "failed": 0}) as provider:
            clock.now.return_value = after
            with self.assertRaises(StopLoop):
                app._scheduler_loop("crypto", app.run_crypto_sync_job, self.db)
            provider.assert_called_once()
        status = refresh.status(self.db, "crypto", after)
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["source"], "scheduled")
        self.assertEqual(status["next_run"], "2026-01-15T02:00:00+00:00")

    def test_dashboard_restores_completion_after_in_memory_summary_is_lost(self):
        status = self.run_stock()
        with patch.object(app, "last_summary", None):
            response = app.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(status["last_success_at"].encode(), response.data)
        self.assertIn(b"Last attempt: completed", response.data)
        self.assertIn(b"OK: 8", response.data)

    def test_unavailable_metadata_is_visible_without_changing_liveness(self):
        self.run_stock()
        with refresh._connection(self.db) as conn:
            conn.execute("UPDATE refresh_state SET state='invalid'")
        client = app.app.test_client()
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Refresh status unavailable", response.data)
        self.assertEqual(client.get("/health").data, b"ok")

    def test_provider_failure_releases_all_locks_for_a_later_attempt(self):
        self.assertEqual(self.run_stock(error=RuntimeError("failed"))["state"], "failed")
        self.assertEqual(self.run_stock()["state"], "completed")
        with refresh.deployment_guard(self.db, timeout=0):
            pass

    def test_no_browser_maintenance_route_is_added(self):
        client = app.app.test_client()
        for path in ("/maintenance", "/deploy", "/refresh/hold"):
            self.assertEqual(client.get(path).status_code, 404)

    def test_scheduler_retries_storage_failure_without_exposing_exception_text(self):
        future = self.now + timedelta(days=1)
        delays = []
        def sleep(seconds):
            delays.append(seconds)
            if len(delays) == 2:
                raise StopLoop()
        with patch.object(app, "datetime") as clock, patch.object(app.refresh, "due_at", side_effect=[
                sqlite3.OperationalError("private diagnostic"), future]), \
                patch.object(app.time, "sleep", side_effect=sleep), self.assertLogs("app", level="ERROR") as logs:
            clock.now.return_value = self.now
            with self.assertRaises(StopLoop):
                app._scheduler_loop("stocks", lambda **kw: self.fail("Unexpected provider job"), self.db)
        self.assertEqual(delays[0], 30)
        self.assertIn("OperationalError", "\n".join(logs.output))
        self.assertNotIn("private diagnostic", "\n".join(logs.output))

    def test_private_hold_cli_releases_after_process_death(self):
        proc = subprocess.Popen([sys.executable, "-u", "refresh.py", "hold", "--db-path", self.db, "--timeout", "0"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "drained")
            with refresh.job_guard(self.db, "stocks") as admitted:
                self.assertFalse(admitted)
        finally:
            proc.kill()
            proc.communicate(timeout=5)
        with refresh.job_guard(self.db, "stocks") as admitted:
            self.assertTrue(admitted)

    def test_interrupted_attempt_is_detected_without_stale_pid_files(self):
        code = """import refresh,sys
from datetime import datetime,timezone
with refresh.job_guard(sys.argv[1], 'stocks') as admitted:
    assert admitted
    refresh.started(sys.argv[1], 'stocks', 'scheduled', datetime.now(timezone.utc))
    print('running', flush=True)
    sys.stdin.read()
"""
        proc = subprocess.Popen([sys.executable, "-u", "-c", code, self.db], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "running")
            self.assertEqual(refresh.status(self.db, "stocks")["state"], "running")
        finally:
            proc.kill()
            proc.communicate(timeout=5)
        status = refresh.status(self.db, "stocks")
        self.assertEqual(status["state"], "interrupted")
        self.assertTrue(status["stale"])
        self.assertIsNotNone(status["finished_at"])

    def test_failed_cutover_body_releases_gate_and_allows_refresh(self):
        with self.assertRaises(RuntimeError):
            with refresh.deployment_guard(self.db, timeout=0):
                raise RuntimeError("simulated pre-cutover failure")
        self.assertEqual(self.run_stock()["state"], "completed")

    def test_symlinked_or_shared_state_files_are_rejected(self):
        target = Path(self.directory + "/untouched")
        target.write_text("original")
        gate = Path(self.db + ".refresh.gate.lock")
        gate.symlink_to(target)
        with self.assertRaises(OSError), refresh.deployment_guard(self.db, timeout=0):
            self.fail("Symlinked lock must be rejected")
        self.assertEqual(target.read_text(), "original")
        state = Path(self.db + ".refresh.db")
        state.write_text("original")
        state.chmod(0o644)
        with self.assertRaises(PermissionError):
            refresh.status(self.db, "stocks")
        self.assertEqual(state.read_text(), "original")

    def test_bad_mode_or_timeout_does_not_create_control_files(self):
        for mode in ("../outside", "other"):
            with self.assertRaises(ValueError), refresh.job_guard(self.db, mode):
                pass
        for timeout in (-1, float("inf"), float("nan")):
            with self.assertRaises(ValueError), refresh.deployment_guard(self.db, timeout):
                pass
        self.assertEqual(list(Path(self.directory).iterdir()), [])

    def test_stock_staleness_is_seven_days_not_weekday_schedule(self):
        """Stock staleness uses 7 days since last success, not the weekday schedule."""
        self.run_stock()  # success at Tuesday 2026-01-06T14:46
        # Four days later (Saturday): not stale under the 7-day rule,
        # even though the weekday schedule would consider it stale.
        saturday = self.now + timedelta(days=4)
        status = refresh.status(self.db, "stocks", saturday)
        self.assertFalse(status["stale"])
        # Eight days later: stale.
        next_wednesday = self.now + timedelta(days=8)
        status = refresh.status(self.db, "stocks", next_wednesday)
        self.assertTrue(status["stale"])

    def test_crypto_staleness_unchanged_by_stock_rule(self):
        """Crypto staleness still uses the daily schedule, not the 7-day stock rule."""
        self.run_stock()  # this only touches stocks state
        # Crypto state is independent; check it uses its own schedule.
        status = refresh.status(self.db, "crypto", self.now)
        self.assertEqual(status["schedule"], "Daily at 02:00 UTC")


if __name__ == "__main__":
    unittest.main()

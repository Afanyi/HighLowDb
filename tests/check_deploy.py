"""Host-only deployment tests: python3 -m unittest tests.check_deploy -q."""

from contextlib import closing, redirect_stdout
import io
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import manage
import release
import data

ROOT = Path(__file__).resolve().parents[1]


class TestDeployment(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def source_copy(self):
        source = self.tmp / "source"
        source.mkdir()
        for name in release.FILES:
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        return source

    def test_allowlist_excludes_runtime_secrets_and_binds_manifest_to_content(self):
        source = self.source_copy()
        secret = "private-fixture-" + uuid.uuid4().hex
        (source / ".env").write_text(secret)
        (source / "deploy/server.env").write_text("OTHER_SECRET_VALUE")
        manifest = release.prepare(source, self.tmp / "review")
        self.assertEqual(set(manifest["files"]), set(release.FILES))
        self.assertEqual(manifest["release"], release.identifier(manifest["files"]))
        with tarfile.open(self.tmp / "review/release.tar.gz") as archive:
            self.assertEqual(set(archive.getnames()), set(release.FILES))
            self.assertTrue(all(secret.encode() not in archive.extractfile(e).read() for e in archive))

    def test_corrupt_archive_fails_before_extraction(self):
        release.prepare(ROOT, self.tmp / "original")
        stage = self.tmp / "corrupt"
        stage.mkdir()
        for name in ("manifest.json", "release.tar.gz"):
            shutil.copyfile(self.tmp / "original" / name, stage / name)
            (stage / name).chmod(0o600)
        with (stage / "release.tar.gz").open("ab") as stream:
            stream.write(b"corruption")
        with self.assertRaises(ValueError):
            release.unpack(stage)
        self.assertFalse((stage / "code").exists())

    def test_links_in_release_sources_are_rejected(self):
        source = self.source_copy()
        (source / "app.py").unlink()
        (source / "app.py").symlink_to(ROOT / "app.py")
        with self.assertRaises(ValueError):
            release.prepare(source, self.tmp / "linked")

    def test_manifest_rejects_unknown_paths(self):
        files = dict.fromkeys(release.FILES, "a" * 64)
        files["../escape"] = files.pop("app.py")
        with self.assertRaises(ValueError):
            release.validate_manifest({"release": release.identifier(files), "files": files, "archive_sha256": "b" * 64})

    def test_private_outer_release_has_image_readable_code_directories(self):
        previous = os.umask(0o077)
        try:
            release.prepare(ROOT, self.tmp / "review")
        finally:
            os.umask(previous)
        self.assertEqual((self.tmp / "review").stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.tmp / "review/code/templates").stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.tmp / "review/code/app.py").stat().st_mode & 0o777, 0o644)

    def test_recovery_record_rejects_path_escape_and_mutable_image_tags(self):
        for value in ({"release": "../tradingSupport", "image": "sha256:" + "a" * 64},
                      {"release": "a" * 16, "image": "allstocks:latest"}):
            with self.assertRaises(ValueError):
                manage.validate_record(value)

    def test_preflight_rejects_symlinked_target_before_docker_calls(self):
        target = self.tmp / "unrelated"
        target.mkdir()
        root = self.tmp / "allstocksDatabase"
        root.symlink_to(target)
        with patch.object(manage.os, "geteuid", return_value=0), \
                patch.object(manage.socket, "gethostname", return_value="tradingsupport-vnic"), \
                patch.object(manage, "docker", side_effect=AssertionError("Unexpected Docker call")), self.assertRaises(ValueError):
            manage.preflight(root, setup=True)

    def test_preflight_refuses_foreign_upstream_alias(self):
        caddy = {"State": {"Running": True}, "NetworkSettings": {"Networks": {manage.NETWORK: {}}}}
        network = {"Driver": "bridge", "Internal": False, "Containers": {"foreign": {}}}
        foreign = {"NetworkSettings": {"Networks": {manage.NETWORK: {"Aliases": ["allstocks-database-app"]}}},
                   "Config": {"Labels": {"com.docker.compose.project": "other-project"}}}
        def docker(*args):
            if args[:2] == ("compose", "version"):
                return "2.40.3"
            if args[:2] == ("network", "inspect"):
                return json.dumps([network])
            if args[0] == "inspect":
                return json.dumps([caddy if args[1] == "trading-assistant-caddy-1" else foreign])
            return ""
        with patch.object(manage.os, "geteuid", return_value=0), \
                patch.object(manage.socket, "gethostname", return_value="tradingsupport-vnic"), \
                patch.object(manage, "docker", side_effect=docker), patch.object(manage, "run", return_value="enabled"), \
                self.assertRaisesRegex(ValueError, "alias"):
            manage.preflight(self.tmp / "new", setup=True)

    def test_readiness_gate_cannot_be_optimized_away(self):
        for table in ("stocks", "crypto"):
            with closing(sqlite3.connect(self.tmp / (table + ".db"))) as conn, conn:
                conn.execute("CREATE TABLE " + table + " (id INTEGER)")
        fake_refresh = SimpleNamespace(STATES={"completed"}, status=lambda *args: {"state": "completed", "paused": False})
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.status = 200
        with patch.dict(os.environ, {"DB_PATH": str(self.tmp / "stocks.db"), "CRYPTO_DB_PATH": str(self.tmp / "crypto.db")}), \
                patch.dict(sys.modules, {"refresh": fake_refresh}), patch("urllib.request.urlopen", return_value=response), \
                self.assertRaisesRegex(RuntimeError, "readiness"):
            exec(compile(manage.HEALTH, "health-probe", "exec", optimize=2), {})

    def test_docker_failure_does_not_echo_environment_or_inspect_output(self):
        secret = "fixture-private-" + uuid.uuid4().hex
        result = subprocess.CompletedProcess(["docker"], 1, stdout=secret, stderr=secret)
        with patch.object(manage.subprocess, "run", return_value=result), self.assertRaises(RuntimeError) as error:
            manage.run(["docker", "inspect", "fixture"])
        self.assertNotIn(secret, str(error.exception))

    def test_environment_is_literal_and_never_executes_shell(self):
        path = self.tmp / "settings"
        path.write_text("AS_HOST=$(touch injected)\n")
        path.chmod(0o600)
        with self.assertRaises(ValueError):
            manage.settings(path, {"AS_HOST"})
        self.assertFalse((self.tmp / "injected").exists())

    def test_environment_rejects_shared_files_and_unknown_keys(self):
        path = self.tmp / "settings"
        path.write_text("DOCKER_HOST=ssh://other\n")
        with self.assertRaises(ValueError):
            manage.settings(path, {"AS_HOST"})
        path.chmod(0o600)
        with self.assertRaises(ValueError):
            manage.settings(path, {"AS_HOST"})

    def test_lock_timeout_and_failure_release_and_child_inheritance(self):
        path = self.tmp / "gate"
        with manage.locked(path, create=True):
            fd = manage.LOCK_FD
            with self.assertRaises(TimeoutError), manage.locked(path, create=True):
                pass
            result = manage.run([sys.executable, "-c", "import os,sys; print(os.fstat(int(sys.argv[1])).st_ino)", str(fd)])
            self.assertEqual(int(result), os.fstat(fd).st_ino)
        self.assertIsNone(manage.LOCK_FD)
        with self.assertRaises(RuntimeError):
            with manage.locked(path):
                raise RuntimeError("failure before cutover")
        with manage.locked(path):
            pass

    def test_symlinked_gate_and_unbounded_timeout_rejected(self):
        path = self.tmp / "gate"
        target = self.tmp / "target"
        target.write_text("untouched")
        path.symlink_to(target)
        with self.assertRaises(OSError), manage.locked(path, create=True):
            pass
        self.assertEqual(target.read_text(), "untouched")
        with self.assertRaises(ValueError), manage.locked(self.tmp / "other", float("inf"), create=True):
            pass

    def test_real_sqlite_backup_and_disposable_restore_checks(self):
        source, backup = self.tmp / "data", self.tmp / "backup"
        source.mkdir(); backup.mkdir()
        for name in data.NAMES:
            with closing(sqlite3.connect(source / name)) as conn, conn:
                conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
                conn.execute("INSERT INTO sample VALUES (1,'kept')")
        report = data.backup(source, backup)
        self.assertTrue(all(item["counts"] == {"sample": 1} for item in report.values()))
        with patch.object(data, "initialize"):
            self.assertEqual(data.check(backup), report)
        def incompatible(directory):
            with closing(sqlite3.connect(directory / data.NAMES[0])) as conn, conn:
                conn.execute("ALTER TABLE sample ADD COLUMN extra INTEGER")
        with patch.object(data, "initialize", side_effect=incompatible), self.assertRaises(ValueError):
            data.check(backup)
        self.assertEqual(data.describe(source / data.NAMES[0])["counts"], {"sample": 1})

    def test_failed_candidate_checks_do_not_touch_current_installation(self):
        root = self.tmp / "allstocksDatabase"
        release.prepare(ROOT, self.tmp / "stage")
        with patch.object(manage, "preflight"), patch.object(manage, "validate_environment", return_value={}), \
                patch.object(manage, "docker", side_effect=RuntimeError("candidate build failed")):
            with self.assertRaises(RuntimeError):
                manage.apply(root, self.tmp / "stage", setup=True)
        self.assertFalse(root.exists())

    def fake_cli(self, args, docker_exit=0, host=None):
        source = self.source_copy()
        if host:
            (source / "deploy/server.env").write_text("AS_HOST=" + host + "\n")
            (source / "deploy/server.env").chmod(0o600)
        fake = self.tmp / "bin"
        fake.mkdir()
        log = self.tmp / "commands"
        for name in ("docker", "ssh", "scp"):
            script = fake / name
            script.write_text("#!/usr/bin/env python3\nimport os,sys\nwith open(os.environ['FAKE_COMMAND_LOG'],'a') as f: f.write(sys.argv[0]+'\\n')\nsys.exit(int(os.environ['FAKE_DOCKER_EXIT']) if sys.argv[0].endswith('docker') else 97)\n")
            script.chmod(0o700)
        environment = os.environ | {"PATH": str(fake) + ":" + os.environ["PATH"], "FAKE_COMMAND_LOG": str(log),
                                    "FAKE_DOCKER_EXIT": str(docker_exit)}
        result = subprocess.run(["bash", str(source / "deploy/update.sh"), *args],
                                env=environment, text=True, capture_output=True)
        return result, log.read_text().splitlines()

    def test_cli_dry_run_never_uses_ssh_or_reads_environment(self):
        result, log = self.fake_cli(["--dry-run", "--env", "/missing/private.env"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Dry run complete", result.stdout)
        self.assertTrue(all(line.endswith("docker") for line in log))

    def test_failed_local_tests_stop_before_ssh(self):
        result, log = self.fake_cli([], docker_exit=7, host="fixture")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exit 7", result.stderr)
        self.assertTrue(all(line.endswith("docker") for line in log))

    def test_option_shaped_ssh_target_is_rejected_before_connecting(self):
        result, log = self.fake_cli([], host="-evil@host")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AS_HOST", result.stderr)
        self.assertTrue(all(line.endswith("docker") for line in log))

    def test_unmatched_environment_quotes_are_rejected(self):
        path = self.tmp / "settings"
        path.write_text('AS_HOST="unclosed\n')
        path.chmod(0o600)
        with self.assertRaises(ValueError):
            manage.settings(path, {"AS_HOST"})

    def test_target_identity_is_checked_before_staging_or_uploading_credentials(self):
        source = self.source_copy()
        config = source / "deploy/server.env"
        config.write_text("AS_HOST=fixture\n")
        config.chmod(0o600)
        environment = self.tmp / "dummy.env"
        environment.write_text("APP_PUBLIC_ORIGIN=https://stocks.skyboyz1.duckdns.org\n"
                               "APP_SECRET_KEY=private-review-fixture-01234567890123456789\n"
                               "T212_API_KEY=dummy-stock\nCOINGECKO_API_KEY=dummy-crypto\n")
        environment.chmod(0o600)
        identities = [("tradingsupport-vnic", "ubuntu", "/home/ubuntu"),
                      ("wrong-server", "ubuntu", "/home/ubuntu"),
                      ("tradingsupport-vnic", "other", "/home/ubuntu"),
                      ("tradingsupport-vnic", "ubuntu", "/other/home")]
        for mode in ("setup", "update"):
            for hostname, username, home in identities:
                with self.subTest(mode=mode, hostname=hostname, username=username, home=home):
                    calls = []
                    stage_writes = []
                    def remote(command, **kwargs):
                        calls.append(command)
                        if len(calls) == 1:
                            parsed = manage.shlex.split(command[-1])
                            output = io.StringIO()
                            with patch.object(socket, "gethostname", return_value=hostname), \
                                    patch.object(pwd, "getpwuid", return_value=SimpleNamespace(pw_name=username)), \
                                    patch.object(Path, "home", return_value=Path(home)), \
                                    patch.object(sys, "argv", ["-c", *parsed[3:]]), \
                                    patch.object(Path, "mkdir") as mkdir, redirect_stdout(output):
                                try:
                                    exec(compile(parsed[2], "ssh-bootstrap", "exec", optimize=2), {})
                                except SystemExit:
                                    raise RuntimeError("ssh failed (exit 1)") from None
                                finally:
                                    stage_writes.append(mkdir.call_count)
                            return output.getvalue().strip()
                        return ""
                    def temporary(**kwargs):
                        path = self.tmp / ("client-review-" + uuid.uuid4().hex)
                        path.mkdir(mode=0o700)
                        return str(path)
                    args = SimpleNamespace(command=mode, env=str(environment), dry_run=False,
                                           test_image="fixture:cached", drain_timeout=0)
                    with patch.object(manage, "__file__", str(source / "deploy/manage.py")), \
                            patch.object(manage, "image_tests"), patch.object(manage, "run", side_effect=remote), \
                            patch.object(manage.tempfile, "mkdtemp", side_effect=temporary), \
                            redirect_stdout(io.StringIO()):
                        if (hostname, username, home) == identities[0]:
                            manage.local(args)
                            self.assertEqual([call[0] for call in calls], ["ssh", "scp", "scp", "ssh", "ssh"])
                            self.assertTrue(calls[2][-1].endswith("/environment"))
                            self.assertIn("_apply", manage.shlex.split(calls[-1][-1]))
                            self.assertEqual(stage_writes, [1])
                        else:
                            with self.assertRaisesRegex(RuntimeError, "ssh failed"):
                                manage.local(args)
                            self.assertEqual([call[0] for call in calls], ["ssh"])
                            self.assertEqual(stage_writes, [0])
                    self.assertTrue(all("StrictHostKeyChecking=yes" in call for call in calls))

    def test_verified_ssh_transport_keeps_stage_identity_and_helper_checksum_gate(self):
        source = self.source_copy()
        (source / "deploy/server.env").write_text("AS_HOST=fixture\n")
        (source / "deploy/server.env").chmod(0o600)
        calls = []
        def remote(args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                parsed = manage.shlex.split(args[-1])
                return "/home/ubuntu/" + parsed[-1]
            return ""
        def temporary(**kwargs):
            path = self.tmp / "client-review"
            path.mkdir(mode=0o700)
            return str(path)
        args = SimpleNamespace(command="update", env=None, dry_run=False, test_image="fixture:cached", drain_timeout=0)
        with patch.object(manage, "__file__", str(source / "deploy/manage.py")), \
                patch.object(manage, "image_tests"), patch.object(manage, "run", side_effect=remote), \
                patch.object(manage.tempfile, "mkdtemp", side_effect=temporary), redirect_stdout(io.StringIO()):
            manage.local(args)
        self.assertEqual([call[0] for call in calls], ["ssh", "scp", "ssh", "ssh"])
        self.assertTrue(all("StrictHostKeyChecking=yes" in call for call in calls))
        command = manage.shlex.split(calls[2][-1])
        verifier = command[2]
        stage = self.tmp / "uploaded"
        stage.mkdir()
        (stage / "release.py").write_bytes((source / "deploy/release.py").read_bytes())
        expected = release.digest((stage / "release.py").read_bytes())
        with patch.object(sys, "argv", ["-c", str(stage), expected]), patch.object(os, "execv") as execute:
            exec(compile(verifier, "transport-verifier", "exec", optimize=2), {})
            execute.assert_called_once()
        (stage / "release.py").write_text("corrupted helper")
        with patch.object(sys, "argv", ["-c", str(stage), expected]), patch.object(os, "execv") as execute, \
                self.assertRaises(SystemExit):
            exec(compile(verifier, "transport-verifier", "exec", optimize=2), {})
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()

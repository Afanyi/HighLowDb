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
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import manage
import release
import data
import ci

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
        result, log = self.fake_cli(["--dry-run", "--env", "/missing/private.env",
                                     "--server-config", "/missing/private-ssh.env"])
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

    def test_explicit_ci_settings_keep_local_config_and_pin_ssh_trust(self):
        source = self.source_copy()
        default = source / "deploy/server.env"
        default.write_text("AS_HOST=-must-not-be-read\n")
        default.chmod(0o600)
        key, known, config = (self.tmp / name for name in ("key", "known_hosts", "ci.env"))
        key.write_text("dummy-private-key")
        known.write_text("server.example ssh-ed25519 dummy-public-key\n")
        config.write_text("AS_HOST=ubuntu@server.example\nAS_KEY=" + str(key)
                          + "\nAS_KNOWN_HOSTS=" + str(known) + "\n")
        for path in (key, known, config):
            path.chmod(0o600)
        calls = []
        def remote(command, **kwargs):
            calls.append(command)
            if command[0] == "ssh" and len(calls) == 1:
                return "/home/ubuntu/" + manage.shlex.split(command[-1])[-1]
            return ""
        def temporary(**kwargs):
            path = self.tmp / ("client-" + uuid.uuid4().hex)
            path.mkdir(mode=0o700)
            return str(path)
        args = SimpleNamespace(command="update", env=None, dry_run=False, test_image="fixture:cached",
                               drain_timeout=300, server_config=str(config))
        with patch.object(manage, "__file__", str(source / "deploy/manage.py")), \
                patch.object(manage, "image_tests") as tests, \
                patch.object(manage, "run", side_effect=remote), \
                patch.object(manage.tempfile, "mkdtemp", side_effect=temporary), redirect_stdout(io.StringIO()):
            manage.local(args)
            tests.assert_called_once()
            self.assertEqual([call[0] for call in calls], ["ssh", "scp", "ssh", "ssh"])
            for call in calls:
                for option in ("StrictHostKeyChecking=yes", "UserKnownHostsFile=" + str(known),
                               "GlobalKnownHostsFile=/dev/null", "IdentitiesOnly=yes", "IdentityAgent=none"):
                    self.assertIn(option, call)
                self.assertEqual(call[call.index("-F") + 1], "/dev/null")
                self.assertEqual(call[call.index("-i") + 1], str(key))
            self.assertEqual(default.read_text(), "AS_HOST=-must-not-be-read\n")
            calls.clear()
            known.chmod(0o644)
            with self.assertRaises(ValueError):
                manage.local(args)
            self.assertEqual(calls, [])


class TestCiDeployment(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="allstocks-ci-deploy-test-")))
        self.root = self.tmp / "repo"
        self.root.mkdir()
        self.execute_real = subprocess.run
        key = self.tmp / "fixture-key"
        self.execute_real(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                          capture_output=True, check=True)
        self.sha, self.release = "a" * 40, "b" * 16
        self.env = {
            "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": ci.REPOSITORY, "GITHUB_SHA": self.sha,
            "ENABLE_AUTO_DEPLOY": "true", "CI_VERIFY_RESULT": "success",
            "GH_TOKEN": "dummy-read-token", "DEPLOY_HOST": "server.example",
            "DEPLOY_SSH_KEY": key.read_text(),
            "DEPLOY_KNOWN_HOSTS": "server.example " + key.with_suffix(".pub").read_text(),
            "GITHUB_STEP_SUMMARY": str(self.tmp / "summary"),
        }
        self.head, self.dirty, self.api_heads = self.sha, "", [self.sha, self.sha]
        self.commands, self.private_dirs = [], set()
        self.updater_exit, self.build_exit = 0, 0
        self.updater_output = "Reviewed release " + self.release + ": /private/fixture\n"

    def execute(self, command, **kwargs):
        self.commands.append(command)
        for name in ("DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS", "GH_TOKEN", "GITHUB_TOKEN"):
            self.assertNotIn(name, kwargs.get("env", {}))
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            output, code = self.head, 0
        elif command[:2] == ["git", "status"]:
            output, code = self.dirty, 0
        elif command[0] == "ssh-keygen":
            path = Path(command[-1])
            self.private_dirs.add(path.parent)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            return self.execute_real(command, **kwargs)
        elif command[0] == "docker":
            self.assertEqual(command[1:4], ["--host", "unix:///var/run/docker.sock", "build"])
            output, code = "", self.build_exit
        elif command[0] == "bash":
            self.assertEqual(command[:3], ["bash", str(self.root / "deploy/update.sh"), "--server-config"])
            self.assertEqual(len(command), 4)
            cfg = manage.settings(Path(command[3]), {"AS_HOST", "AS_KEY", "AS_KNOWN_HOSTS"})
            self.assertEqual(cfg["AS_HOST"], "ubuntu@server.example")
            self.assertTrue(Path(cfg["AS_KEY"]).is_file())
            self.assertTrue(Path(cfg["AS_KNOWN_HOSTS"]).is_file())
            output, code = self.updater_output, self.updater_exit
        else:
            raise AssertionError("Unexpected tool: " + command[0])
        return subprocess.CompletedProcess(command, code, stdout=output, stderr="dummy-private-tool-error")

    def api(self, request, **kwargs):
        self.assertEqual(request.full_url, "https://api.github.com/repos/Afanyi/HighLowDb/git/ref/heads/main")
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"object": {"sha": self.api_heads.pop(0)}}).encode()
        return response

    def run_ci(self):
        self.commands.clear()
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), patch.object(ci, "ROOT", self.root), \
                patch.object(ci.subprocess, "run", side_effect=self.execute), \
                patch.object(ci.urllib.request, "urlopen", side_effect=self.api) as api, \
                redirect_stdout(out), patch("sys.stderr", err):
            code = ci.main()
        self.assertTrue(all(not path.exists() for path in self.private_dirs))
        combined = out.getvalue() + err.getvalue()
        if self.env.get("DEPLOY_SSH_KEY"):
            self.assertNotIn(self.env["DEPLOY_SSH_KEY"], combined)
        self.assertNotIn("dummy-private-tool-error", combined)
        self.assertNotIn("dummy-read-token", combined)
        return code, combined, api.call_count

    def test_success_reuses_updater_reports_identity_and_cleans_private_files(self):
        code, output, api_calls = self.run_ci()
        self.assertEqual(code, 0, output)
        self.assertEqual(api_calls, 2)
        self.assertIn(self.sha, output)
        self.assertIn(self.release, output)
        self.assertIn("**succeeded**", (self.tmp / "summary").read_text())
        self.assertEqual(len([c for c in self.commands if c[0] == "bash"]), 1)

    def test_ineligible_runs_never_prepare_credentials_build_or_update(self):
        for name, value in (("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_EVENT_NAME", "workflow_dispatch"),
                            ("GITHUB_REF", "refs/heads/dev"), ("GITHUB_REF", "refs/heads/feature/example"),
                            ("GITHUB_REF", "refs/tags/main"), ("GITHUB_REPOSITORY", "other/fork"),
                            ("ENABLE_AUTO_DEPLOY", "false"), ("ENABLE_AUTO_DEPLOY", ""),
                            ("CI_VERIFY_RESULT", "failure"), ("CI_VERIFY_RESULT", "skipped"),
                            ("CI_VERIFY_RESULT", "cancelled"), ("CI_VERIFY_RESULT", "")):
            with self.subTest(name=name, value=value):
                original = self.env[name]
                self.env[name] = value
                try:
                    code, _, api_calls = self.run_ci()
                    self.assertEqual(code, 1)
                    self.assertEqual(api_calls, 0)
                    self.assertEqual(self.commands, [])
                finally:
                    self.env[name] = original

    def test_old_commit_is_rejected_before_credential_materialization(self):
        self.api_heads = ["f" * 40]
        code, _, _ = self.run_ci()
        self.assertEqual(code, 1)
        self.assertTrue(all(c[0] == "git" for c in self.commands))
        self.assertEqual(self.private_dirs, set())

    def test_main_advancing_during_build_prevents_updater_and_cleans_key(self):
        self.api_heads = [self.sha, "f" * 40]
        code, _, api_calls = self.run_ci()
        self.assertEqual(code, 1)
        self.assertEqual(api_calls, 2)
        self.assertTrue(any(c[0] == "docker" for c in self.commands))
        self.assertFalse(any(c[0] == "bash" for c in self.commands))

    def test_wrong_checkout_and_dirty_worktree_stop_before_api_or_build(self):
        for head, dirty in (("f" * 40, ""), (self.sha, " M app.py")):
            with self.subTest(head=head, dirty=dirty):
                self.head, self.dirty = head, dirty
                code, _, api_calls = self.run_ci()
                self.assertEqual(code, 1)
                self.assertEqual(api_calls, 0)
                self.assertTrue(all(c[0] == "git" for c in self.commands))

    def test_missing_settings_fail_without_build_or_update(self):
        for name in ("DEPLOY_HOST", "DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS", "GH_TOKEN"):
            with self.subTest(name=name):
                original = self.env[name]
                self.env[name] = ""
                self.api_heads = [self.sha]
                try:
                    code, _, _ = self.run_ci()
                    self.assertEqual(code, 1)
                    self.assertTrue(all(c[0] == "git" for c in self.commands))
                finally:
                    self.env[name] = original

    def test_known_host_mismatch_and_fingerprint_are_rejected_before_build(self):
        for known in ("SHA256:fingerprint-is-not-a-host-entry", self.env["DEPLOY_KNOWN_HOSTS"].replace("server.example", "wrong.example")):
            with self.subTest(known=known.split()[0]):
                self.env["DEPLOY_KNOWN_HOSTS"] = known
                self.api_heads = [self.sha]
                code, _, _ = self.run_ci()
                self.assertEqual(code, 1)
                self.assertFalse(any(c[0] in {"docker", "bash"} for c in self.commands))

    def test_hashed_known_hosts_are_supported(self):
        known = self.tmp / "hashed-hosts"
        known.write_text(self.env["DEPLOY_KNOWN_HOSTS"])
        self.execute_real(["ssh-keygen", "-H", "-f", str(known)], capture_output=True, check=True)
        self.env["DEPLOY_KNOWN_HOSTS"] = known.read_text()
        code, output, _ = self.run_ci()
        self.assertEqual(code, 0, output)

    def test_bad_private_key_is_rejected_before_build_and_not_logged(self):
        self.env["DEPLOY_SSH_KEY"] = "invalid-test-private-key"
        code, _, _ = self.run_ci()
        self.assertEqual(code, 1)
        self.assertFalse(any(c[0] in {"docker", "bash"} for c in self.commands))

    def test_unavailable_github_api_cannot_allow_deployment(self):
        with patch.object(self, "api", side_effect=OSError("dummy-read-token")):
            code, _, api_calls = self.run_ci()
        self.assertEqual(code, 1)
        self.assertEqual(api_calls, 1)
        self.assertTrue(all(c[0] == "git" for c in self.commands))

    def test_failed_build_cleans_credentials_without_starting_update(self):
        self.build_exit = 17
        code, _, _ = self.run_ci()
        self.assertEqual(code, 1)
        self.assertFalse(any(c[0] == "bash" for c in self.commands))

    def test_failed_update_is_failure_and_preserves_safe_release_identity(self):
        self.updater_exit = 19
        self.updater_output += "Deployment stopped: TimeoutError: dummy-private-tool-error\n"
        code, output, _ = self.run_ci()
        self.assertEqual(code, 1)
        self.assertIn(self.release, output)
        self.assertIn("**failed**", (self.tmp / "summary").read_text())

    def test_update_without_release_confirmation_cannot_be_success(self):
        self.updater_output = ""
        code, _, _ = self.run_ci()
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()

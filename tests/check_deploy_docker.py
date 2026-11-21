"""Host-only, real Docker/Compose deployment drill; never SSHs or uses production names.

Run: python3 tests/check_deploy_docker.py
The root controller is test-only; the application never receives a Docker socket.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
CONTROLLER = "allstocks-deployment-test-controller:local"


def command(*args):
    result = subprocess.run([*DOCKER, *map(str, args)], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def inside(directory, project):
    sys.path.insert(0, str(ROOT / "deploy"))
    import manage
    import release
    # The controller mounts the new fixture volume before the app does. Match
    # the ownership normally inherited from the production image's /data.
    os.chown("/fixture-volume", 10001, 10001)
    root = directory / "allstocksDatabase"
    environment = """APP_PUBLIC_ORIGIN=https://stocks.skyboyz1.duckdns.org
APP_SECRET_KEY=deployment-test-only-session-value-01234567890123456789
T212_API_KEY=deployment-dummy-stock
T212_ENVIRONMENT=demo
COINGECKO_API_KEY=deployment-dummy-crypto
SYNC_TIME_UTC=14:45
CRYPTO_SYNC_TIME_UTC=02:00
"""

    def stage(name, unhealthy=False, with_env=False):
        source = directory / (name + "-source")
        source.mkdir()
        for filename in release.FILES:
            target = source / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / filename, target)
        with (source / "app.py").open("a") as stream:
            stream.write("\n# Isolated deployment drill: " + name + "\n")
        if unhealthy:
            with (source / "Dockerfile").open("a") as stream:
                stream.write('\nCMD ["python", "-c", "import time; time.sleep(120)"]\n')
        destination = directory / name
        manifest = release.prepare(source, destination)
        if with_env:
            (destination / "environment").write_text(environment)
            (destination / "environment").chmod(0o600)
        return destination, manifest

    def snapshot():
        value = manage.inspect_app()
        return value["Id"], value["Image"], (root / ".env").read_bytes()

    original_run = subprocess.run
    def diagnostics(*args, **kwargs):
        result = original_run(*args, **kwargs)
        if result.returncode and "ConnectionRefusedError" not in (result.stderr or ""):
            # Only isolated dummy fixtures exist in this controller.
            print("Fixture Docker diagnostic:", result.stderr, flush=True)
        return result

    with patch.object(manage, "PROJECT", project), patch.object(manage, "VOLUME", project + "_data"), \
            patch.object(manage, "NETWORK", project + "_ingress"), \
            patch.object(manage, "preflight"), patch.object(manage, "volume_path", return_value=Path("/fixture-volume")), \
            patch.object(manage.subprocess, "run", side_effect=diagnostics):
        first, manifest = stage("first", with_env=True)
        manage.apply(root, first, setup=True, health_timeout=15)
        first_id, first_image, first_environment = snapshot()
        seed = """import sqlite3,refresh,os
from contextlib import closing
for name,table in [('DB_PATH','stocks'),('CRYPTO_DB_PATH','crypto')]:
    with closing(sqlite3.connect(os.environ[name])) as conn, conn:
        conn.execute('INSERT INTO '+table+" (ticker,name,status_tag,last_updated) VALUES ('FIXTURE','Deployment fixture','ACTIVE',datetime('now'))")
"""
        manage.docker("exec", first_id, "python", "-c", seed)
        second, _ = stage("second")
        manage.apply(root, second, health_timeout=15)
        second_id, second_image, second_environment = snapshot()
        assert first_id != second_id and first_image != second_image
        assert first_environment == second_environment
        print("PASS: real initial installation and update; stable volume and unchanged environment.", flush=True)

        # A corrupted staged source cannot reach building/cutover.
        corrupt, _ = stage("corrupt")
        with (corrupt / "code/app.py").open("a") as stream:
            stream.write("\n# corrupted after manifest\n")
        try:
            manage.apply(root, corrupt)
            raise AssertionError("Corrupt stage was accepted")
        except ValueError:
            pass
        assert snapshot() == (second_id, second_image, second_environment)

        active = project + "-active"
        guard_code = """import refresh,time
with refresh.job_guard('/data/trading212_stocks.db','stocks') as acquired:
    assert acquired
    print('active',flush=True)
    time.sleep(180)
"""
        manage.docker("run", "-d", "--name", active, "--network", "none", "--mount",
                      f"type=volume,src={project}_data,dst=/data", "--entrypoint", "python", second_image, "-c", guard_code)
        try:
            for _ in range(30):
                if "active" in manage.docker("logs", active):
                    break
                time.sleep(0.2)
            assert "active" in manage.docker("logs", active)
            try:
                manage.apply(root, second, drain_timeout=0)
                raise AssertionError("Active refresh was interrupted")
            except TimeoutError:
                pass
            assert snapshot() == (second_id, second_image, second_environment)
        finally:
            manage.docker("rm", "-f", active)
        print("PASS: corrupted staging and active refresh prevent cutover; live fixture unchanged.", flush=True)

        bad, _ = stage("unhealthy", unhealthy=True, with_env=True)
        (bad / "environment").write_text(environment.replace("deployment-dummy-stock", "rotated-dummy-stock"))
        try:
            manage.apply(root, bad, health_timeout=10)
            raise AssertionError("Unhealthy candidate was accepted")
        except RuntimeError:
            pass
        recovered_id, recovered_image, recovered_environment = snapshot()
        assert recovered_id != second_id and recovered_image == second_image and recovered_environment == second_environment
        manage.docker("exec", recovered_id, "python", "-c", """import os,sqlite3,refresh
from contextlib import closing
for name,table in [('DB_PATH','stocks'),('CRYPTO_DB_PATH','crypto')]:
    with closing(sqlite3.connect('file:'+os.environ[name]+'?mode=ro',uri=True)) as conn:
        assert conn.execute('SELECT count(*) FROM '+table).fetchone()[0] == 1
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
for mode in ('stocks','crypto'):
    assert not refresh.status(os.environ['DB_PATH'],mode)['paused']
""")
        assert len(list((root / "backups").glob("*/recovery.json"))) == 2
        print("PASS: unhealthy cutover restores previous image/environment; both databases and schedules survive.", flush=True)
        first_backup = sorted((root / "backups").iterdir())[0]
        manage.recover(root, first_backup.name)
        assert snapshot()[1] == first_image
        assert snapshot()[2] == first_environment
        print("PASS: explicit recovery validates current data copies and restores the selected known image.", flush=True)


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--inside":
        inside(Path(sys.argv[2]), sys.argv[3])
        return
    directory = Path(tempfile.mkdtemp(prefix="allstocks-deploy-drill-"))
    project = "allstocks-drill-" + uuid.uuid4().hex[:12]
    sentinel = project + "-unrelated"
    volume, network = project + "_data", project + "_ingress"
    dockerfile = directory / "Dockerfile.controller"
    dockerfile.write_text("""FROM docker:29-cli AS cli
FROM python:3.13.15-slim-bookworm
COPY --from=cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=cli /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/
""")
    try:
        command("build", "-f", dockerfile, "-t", CONTROLLER, directory)
        command("volume", "create", "--label", "com.docker.compose.project=" + project,
                "--label", "com.docker.compose.volume=data", volume)
        command("network", "create", "--internal", network)
        sentinel_id = command("run", "-d", "--name", sentinel, "--network", "none", "alpine:3.22", "sleep", "600")
        result = subprocess.run([*DOCKER, "run", "--rm", "--name", project + "-controller", "--network", "bridge",
            "--mount", "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
            "--mount", f"type=bind,src={ROOT},dst=/src,readonly", "--mount", f"type=bind,src={directory},dst={directory}",
            "--mount", f"type=volume,src={volume},dst=/fixture-volume", CONTROLLER,
            "python", "/src/tests/check_deploy_docker.py", "--inside", str(directory), project], text=True)
        if result.returncode:
            raise RuntimeError("Deployment drill failed")
        assert command("inspect", "--format", "{{.Id}}", sentinel) == sentinel_id
        assert command("inspect", "--format", "{{.State.Running}}", sentinel) == "true"
        print("PASS: unrelated sentinel container stayed running and was not recreated.")
    finally:
        for name in (project + "-controller", project + "-active", project + "-app-1", sentinel):
            subprocess.run([*DOCKER, "rm", "-f", name], capture_output=True)
        subprocess.run([*DOCKER, "volume", "rm", volume], capture_output=True)
        subprocess.run([*DOCKER, "network", "rm", network], capture_output=True)
        # The controller wrote root-owned fixture files; remove only this freshly
        # generated directory's contents using a disposable root cleanup container.
        subprocess.run([*DOCKER, "run", "--rm", "--network", "none", "--mount", f"type=bind,src={directory},dst=/fixture",
            CONTROLLER, "python", "-c", "import pathlib,shutil; [(shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()) for p in pathlib.Path('/fixture').iterdir()]"], capture_output=True)
        directory.rmdir()


if __name__ == "__main__":
    main()

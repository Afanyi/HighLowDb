"""Manual deployment only. Local entry points are setup.sh and update.sh."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release


PROJECT = "allstocks-database"
VOLUME = "allstocks-database_data"
NETWORK = "trading-assistant_default"
SERVER_HOSTNAME = "tradingsupport-vnic"
DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
LOCK_FD = None
APP_KEYS = {"APP_PUBLIC_ORIGIN", "APP_SECRET_KEY", "T212_API_KEY", "T212_API_SECRET", "T212_ENVIRONMENT",
            "SYNC_TIME_UTC", "YAHOO_REQUEST_DELAY", "COINGECKO_API_KEY", "CRYPTO_VS_CURRENCY",
            "CRYPTO_SYNC_TIME_UTC", "CRYPTO_HISTORY_CALLS_PER_RUN"}
HEALTH = """import os,sqlite3,urllib.request,refresh
from contextlib import closing
for path in ('/health','/','/?mode=crypto'):
    with urllib.request.urlopen('http://127.0.0.1:8000'+path, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError('HTTP readiness failed')
for name,table in [('DB_PATH','stocks'),('CRYPTO_DB_PATH','crypto')]:
    with closing(sqlite3.connect('file:'+os.environ[name]+'?mode=ro',uri=True)) as conn:
        conn.execute('SELECT count(*) FROM '+table).fetchone()
for mode in ('stocks','crypto'):
    state=refresh.status(os.environ['DB_PATH'],mode)
    if state['state'] not in refresh.STATES or state['state'] == 'running' or not state['paused']:
        raise RuntimeError('Data/scheduler readiness failed')
print('ready')
"""


def run(args, **kwargs):
    # Preserve the drain descriptor in mutating Docker children too: if the
    # deployer dies, an in-flight cutover still holds the lock until it finishes.
    if LOCK_FD is not None:
        kwargs["pass_fds"] = (LOCK_FD,)
    result = subprocess.run(args, text=True, capture_output=True, **kwargs)
    if result.returncode:
        # Docker errors/inspect output can contain environment values. Never echo
        # raw output, command environments or exception reprs to deployment logs.
        raise RuntimeError(f"{Path(args[0]).name} failed (exit {result.returncode})")
    return result.stdout.strip()


def docker(*args, **kwargs):
    return run([*DOCKER, *map(str, args)], **kwargs)


def write_private(path, data):
    if path.is_symlink():
        raise ValueError("Refusing a symlinked destination")
    temporary = path.with_name(path.name + ".next-" + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def settings(path, allowed):
    data = release.private_file(path).decode()
    if len(data) > 65536:
        raise ValueError("Environment file too large")
    result = {}
    for line in data.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in allowed or key in result:
            raise ValueError("Unexpected or duplicate environment key")
        if value.startswith(("'", '"')) and value.endswith(value[0]) and len(value) >= 2:
            value = value[1:-1]
        if not value or any(c.isspace() for c in value) or any(c in value for c in "$`\\\x00#'\""):
            # Empty optional provider secrets are legitimate.
            if value or key != "T212_API_SECRET":
                raise ValueError("Environment values must be nonempty literals without interpolation")
        result[key] = value
    return result


def validate_environment(path, code):
    cfg = settings(path, APP_KEYS)
    host = re.search(r"^([a-z0-9.-]+) \{", (code / "deploy/Caddyfile.stocks").read_text(), re.M).group(1)
    if cfg.get("APP_PUBLIC_ORIGIN") != "https://" + host or len(cfg.get("APP_SECRET_KEY", "")) < 32:
        raise ValueError("Set the reviewed HTTPS origin and a private random APP_SECRET_KEY of 32+ characters")
    if not cfg.get("T212_API_KEY") or not cfg.get("COINGECKO_API_KEY"):
        raise ValueError("Both Trading 212 and CoinGecko keys are required")
    if cfg.get("T212_ENVIRONMENT", "live") not in {"live", "demo"}:
        raise ValueError("Invalid Trading 212 environment")
    for key, default in (("SYNC_TIME_UTC", "14:45"), ("CRYPTO_SYNC_TIME_UTC", "02:00")):
        value = cfg.get(key, default)
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
            raise ValueError("Invalid UTC schedule: " + key)
    if not re.fullmatch(r"[a-z]{3,5}", cfg.get("CRYPTO_VS_CURRENCY", "usd")):
        raise ValueError("Invalid crypto currency")
    try:
        if not 0 <= int(cfg.get("CRYPTO_HISTORY_CALLS_PER_RUN", "200")) <= 1000:
            raise ValueError()
        delay = float(cfg.get("YAHOO_REQUEST_DELAY", "0.5"))
        if not 0.5 <= delay <= 60:
            raise ValueError()
    except ValueError:
        raise ValueError("Invalid history budget or Yahoo request pacing; values suppressed") from None
    return cfg


@contextmanager
def locked(path, timeout=0, owner=None, create=False):
    global LOCK_FD
    if type(timeout) is not int or not 0 <= timeout <= 86400:
        raise ValueError("Lock timeout must be a bounded nonnegative integer")
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_CREAT if create else 0)
    fd = os.open(path, flags, 0o600)
    previous = LOCK_FD
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077
                or info.st_uid != (os.geteuid() if owner is None else owner)):
            raise ValueError("Unsafe deployment lock")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Deployment/refresh busy; current release left running")
                time.sleep(0.1)
        LOCK_FD = fd
        yield
    finally:
        LOCK_FD = previous
        os.close(fd)


def image_tests(image, code=None):
    args = ["run", "--rm", "--pull=never", "--network", "none", "--memory", "512m", "--memory-swap", "512m",
            "--cpus", "0.5", "--pids-limit", "128", "--entrypoint", "python",
            "-e", "T212_API_KEY=deployment-dummy", "-e", "COINGECKO_API_KEY=deployment-dummy",
            "-e", "DB_PATH=/tmp/stocks.db", "-e", "CRYPTO_DB_PATH=/tmp/crypto.db"]
    if code:
        args += ["--user", f"{os.getuid()}:{os.getgid()}", "--mount", f"type=bind,src={code},dst=/src,readonly", "-w", "/src"]
    output = docker(*args, image, "-m", "unittest", "-q")
    print("Offline application tests passed", flush=True)
    return output


def inspect_app(allow_stopped=False):
    ids = docker("ps", "-aq", "--filter", "label=com.docker.compose.project=" + PROJECT).split()
    if len(ids) != 1:
        raise ValueError("Expected exactly one existing app container")
    value = json.loads(docker("inspect", ids[0]))[0]
    if value["Config"]["Labels"].get("com.docker.compose.service") != "app":
        raise ValueError("Unexpected service in app project")
    mounts = [m for m in value["Mounts"] if m["Destination"] == "/data"]
    if len(mounts) != 1 or mounts[0].get("Name") != VOLUME or (not value["State"]["Running"] and not allow_stopped):
        raise ValueError("Existing app data/runtime identity mismatch")
    return value


def volume_path():
    value = json.loads(docker("volume", "inspect", VOLUME))[0]
    if value["Driver"] != "local" or value["Labels"].get("com.docker.compose.project") != PROJECT:
        raise ValueError("Unexpected data volume owner/driver")
    return Path(value["Mountpoint"])


def preflight(root, setup):
    if os.geteuid() != 0 or socket.gethostname() != SERVER_HOSTNAME:
        raise ValueError("Run server operations with sudo on the inspected TradingSupport host only")
    if root.is_symlink() or (root.exists() and (not root.is_dir() or root.stat().st_uid != 0 or root.stat().st_mode & 0o077)):
        raise ValueError("Unsafe app installation directory")
    if setup:
        if root.exists() or docker("ps", "-aq", "--filter", "label=com.docker.compose.project=" + PROJECT):
            raise ValueError("Setup requires an absent application installation")
        if VOLUME in docker("volume", "ls", "--format", "{{.Name}}").splitlines():
            raise ValueError("Setup refuses an existing data volume; inspect/migrate it explicitly")
    version = docker("compose", "version", "--short")
    numbers = tuple(map(int, re.search(r"(\d+)\.(\d+)\.(\d+)", version).groups()))
    if numbers < (2, 24, 0) or run(["systemctl", "is-enabled", "docker"]) != "enabled":
        raise ValueError("Enabled Docker and Compose >= 2.24 are required")
    caddy = json.loads(docker("inspect", "trading-assistant-caddy-1"))[0]
    if not caddy["State"]["Running"] or NETWORK not in caddy["NetworkSettings"]["Networks"]:
        raise ValueError("Existing TradingSupport ingress is unavailable")
    network = json.loads(docker("network", "inspect", NETWORK))[0]
    if network["Driver"] != "bridge" or network["Internal"]:
        raise ValueError("Existing ingress must support provider egress on its bridge network")
    for identifier in network["Containers"]:
        peer = json.loads(docker("inspect", identifier))[0]
        aliases = peer["NetworkSettings"]["Networks"][NETWORK].get("Aliases") or []
        if "allstocks-database-app" in aliases and peer["Config"]["Labels"].get("com.docker.compose.project") != PROJECT:
            raise ValueError("The private upstream alias is already owned by another project")
    available = int(re.search(r"^MemAvailable:\s+(\d+)", Path("/proc/meminfo").read_text(), re.M).group(1)) * 1024
    if available < 1024 ** 3 or shutil.disk_usage(root.parent).free < 3 * 1024 ** 3:
        raise ValueError("Need at least 1 GiB available RAM and 3 GiB free disk before building")
    if not setup:
        data_size = sum(path.stat().st_size for path in volume_path().glob("*.db"))
        if shutil.disk_usage(root.parent).free < 3 * data_size + 3 * 1024 ** 3:
            raise ValueError("Insufficient disk for consistent backups and disposable restore checks")


def record_at(root):
    value = json.loads(release.private_file(root / "current.json", os.geteuid()))
    return validate_record(value)


def validate_record(value):
    if set(value) != {"release", "image"} or not re.fullmatch(r"[a-f0-9]{16}", value["release"]):
        raise ValueError("Invalid installed release record")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", value["image"]):
        raise ValueError("Invalid installed image identity")
    return value


def compose(root, record, *args):
    code = root / "releases" / record["release"]
    if code.is_symlink() or not code.is_dir():
        raise ValueError("Missing/unsafe installed release")
    environment = validate_environment(root / ".env", code)
    environment = {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "APP_"))} | environment
    environment.update(APP_RELEASE=record["release"], APP_ENV_FILE=str(root / ".env"))
    override = {"name": PROJECT, "services": {"app": {"image": record["image"]}},
                "volumes": {"data": {"name": VOLUME}}, "networks": {"ingress": {"name": NETWORK}}}
    write_private(root / "runtime.json", json.dumps(override).encode())
    return docker("compose", "--project-name", PROJECT, "--env-file", root / ".env",
                  "-f", code / "compose.yaml", "-f", code / "compose.production.yaml", "-f", root / "runtime.json",
                  *args, env=environment)


def data_operation(code, image, operation, backup=None, cfg=None):
    args = ["run", "--rm", "--pull=never", "--network", "none", "--memory", "512m", "--entrypoint", "python",
            "--mount", f"type=bind,src={code / 'deploy/data.py'},dst=/tools/data.py,readonly",
            "--mount", f"type=volume,src={VOLUME},dst=/data" + (",readonly" if operation != "init" else "")]
    if operation != "init":
        args += ["--user", "0:0", "--cap-drop", "ALL", "--cap-add", "DAC_OVERRIDE"]
    if backup:
        args += ["--mount", f"type=bind,src={backup},dst=/backup" + (",readonly" if operation == "check" else "")]
    for key in ("SYNC_TIME_UTC", "CRYPTO_SYNC_TIME_UTC"):
        if cfg and key in cfg:
            args += ["-e", key + "=" + cfg[key]]
    return docker(*args, image, "/tools/data.py", operation)


def healthy(root, record, timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        try:
            value = inspect_app()
            if value["Image"] != record["image"]:
                raise ValueError("Unexpected running image")
            if docker("exec", value["Id"], "python", "-c", HEALTH) == "ready":
                return
        except (RuntimeError, ValueError):
            if time.monotonic() >= deadline:
                raise RuntimeError("App did not pass HTTP/data/scheduler readiness checks") from None
            time.sleep(1)


def restore(root, recovery, timeout=60):
    previous = validate_record(recovery["previous"])
    write_private(root / ".env", release.private_file(Path(recovery["backup"]) / "environment"))
    compose(root, previous, "up", "-d", "--no-build", "--pull", "never", "app")
    healthy(root, previous, timeout)
    write_private(root / "current.json", json.dumps(previous).encode())


def apply(root, stage, setup=False, drain_timeout=300, health_timeout=60):
    # Server CLI supplies fixed trusted root/stage paths, not user shell expressions.
    with locked(root.parent / ".allstocksDatabase.deploy.lock", create=True):
        preflight(root, setup)
        if root.exists() and len(list((root / "backups").glob("*"))) >= 10:
            raise ValueError("Ten retained backups reached; inspect and archive old app-only backups first")
        manifest = json.loads(release.private_file(stage / "manifest.json"))
        release.validate_manifest(manifest)
        code = stage / "code"
        for name, expected in manifest["files"].items():
            path = code / name
            if path.is_symlink() or path.resolve() != code.resolve() / name or release.digest(path.read_bytes()) != expected:
                raise ValueError("Staged code checksum mismatch")
        previous = None if setup else record_at(root)
        if previous and inspect_app()["Image"] != previous["image"]:
            raise ValueError("Running image differs from installed release; recover before updating")
        environment_file = stage / "environment" if (stage / "environment").exists() else root / ".env"
        cfg = validate_environment(environment_file, code)
        if previous:
            old_cfg = validate_environment(root / ".env", root / "releases" / previous["release"])
            if old_cfg.get("CRYPTO_VS_CURRENCY", "usd") != cfg.get("CRYPTO_VS_CURRENCY", "usd"):
                raise ValueError("Crypto currency change needs an explicit database migration")
            for key, default in (("SYNC_TIME_UTC", "14:45"), ("CRYPTO_SYNC_TIME_UTC", "02:00")):
                if old_cfg.get(key, default) != cfg.get(key, default):
                    raise ValueError("Changing a persisted schedule requires a reviewed rescheduling procedure")
        image_tag = PROJECT + ":" + manifest["release"]
        print("Building and testing isolated candidate", flush=True)
        docker("build", "-t", image_tag, code)
        image_tests(image_tag)
        candidate = {"release": manifest["release"], "image": docker("image", "inspect", image_tag, "--format", "{{.Id}}")}
        if setup:
            root.mkdir(mode=0o700)
            (root / "releases").mkdir(mode=0o700)
            (root / "backups").mkdir(mode=0o700)
            docker("volume", "create", "--label", "com.docker.compose.project=" + PROJECT,
                   "--label", "com.docker.compose.volume=data", VOLUME)
        installed = root / "releases" / candidate["release"]
        if installed.is_symlink():
            raise ValueError("Unsafe installed release directory")
        if not installed.exists():
            shutil.copytree(code, installed)
        else:
            for name, expected in manifest["files"].items():
                if release.digest((installed / name).read_bytes()) != expected:
                    raise ValueError("Existing release was modified")
        if setup:
            write_private(root / ".env", release.private_file(environment_file))
            data_operation(installed, candidate["image"], "init", cfg=cfg)
        gate = volume_path() / "trading212_stocks.db.refresh.gate.lock"
        cutover = False
        recovery = None
        try:
            # Same inode/protocol as refresh.deployment_guard, held by this process.
            with locked(gate, drain_timeout, owner=10001):
                if previous:
                    backup = root / "backups" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
                    backup.mkdir(mode=0o700)
                    write_private(backup / "environment", release.private_file(root / ".env"))
                    recovery = {"previous": previous, "candidate": candidate, "backup": str(backup)}
                    write_private(backup / "recovery.json", json.dumps(recovery).encode())
                    report = data_operation(installed, previous["image"], "backup", backup)
                    write_private(backup / "data.json", report.encode())
                    data_operation(installed, candidate["image"], "check", backup)
                    # Test the actual rollback image against copies of the same data.
                    data_operation(root / "releases" / previous["release"], previous["image"], "check", backup)
                    print("Verified private snapshots and rollback image: " + backup.name, flush=True)
                try:
                    if previous and environment_file != root / ".env":
                        write_private(root / ".env", release.private_file(environment_file))
                    cutover = True
                    compose(root, candidate, "up", "-d", "--no-build", "--pull", "never", "app")
                    healthy(root, candidate, health_timeout)
                    write_private(root / "current.json", json.dumps(candidate).encode())
                except BaseException:
                    if previous:
                        try:
                            restore(root, recovery, health_timeout)
                        except Exception:
                            raise RuntimeError("Cutover and rollback failed; inspect app and retained recovery record") from None
                    else:
                        compose(root, candidate, "rm", "-s", "-f", "app")
                    raise
        except BaseException:
            if setup:
                # Only this attempt's fresh private installation is removable.
                # Failed cutover removed its container before releasing the gate.
                if not cutover or not docker("ps", "-aq", "--filter", "label=com.docker.compose.project=" + PROJECT):
                    docker("volume", "rm", VOLUME)
                    shutil.rmtree(root)
            raise
        print("Installed " + candidate["release"] + "; stock/crypto scheduling resumed. Code updates remain manual.", flush=True)
        return candidate


def recover(root, name):
    if not re.fullmatch(r"\d{8}T\d{6}-[a-f0-9]{8}", name):
        raise ValueError("Invalid backup identity")
    with locked(root.parent / ".allstocksDatabase.deploy.lock", create=True):
        preflight(root, False)
        backup = root / "backups" / name
        if backup.is_symlink():
            raise ValueError("Unsafe recovery directory")
        recovery = json.loads(release.private_file(backup / "recovery.json"))
        if recovery["backup"] != str(backup):
            raise ValueError("Recovery path mismatch")
        previous = validate_record(recovery["previous"])
        candidate = validate_record(recovery["candidate"])
        current = record_at(root)
        if inspect_app(allow_stopped=True)["Image"] not in {previous["image"], candidate["image"], current["image"]}:
            raise ValueError("Unknown running image; inspect before recovering")
        with locked(volume_path() / "trading212_stocks.db.refresh.gate.lock", 300, owner=10001):
            with tempfile.TemporaryDirectory(prefix="recover-check-", dir=root / "backups") as temporary:
                snapshot = Path(temporary)
                data_operation(root / "releases" / current["release"], current["image"], "backup", snapshot)
                data_operation(root / "releases" / previous["release"], previous["image"], "check", snapshot)
                restore(root, recovery)


def local(args):
    source = Path(__file__).resolve().parents[1]
    temporary = Path(tempfile.mkdtemp(prefix="allstocks-release-"))
    manifest = release.prepare(source, temporary / "review")
    stage = temporary / "review"
    image_tests(args.test_image, stage / "code")
    print("Reviewed release " + manifest["release"] + ": " + str(stage), flush=True)
    if args.dry_run:
        print("Dry run complete: no SSH, environment file read/upload, or deployment.")
        return
    cfg = settings(source / "deploy/server.env", {"AS_HOST", "AS_KEY"})
    host = cfg.get("AS_HOST", "")
    if not re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError("AS_HOST must be a safe SSH alias or user@hostname")
    options = ["-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes", "-o", "ControlMaster=no", "-o", "ControlPath=none"]
    if cfg.get("AS_KEY"):
        key = Path(cfg["AS_KEY"]).expanduser()
        release.private_file(key, os.getuid())
        options += ["-i", str(key)]
    if args.command == "setup" and not args.env:
        raise ValueError("First setup requires --env /absolute/path/to/private-app.env")
    if args.env:
        validate_environment(Path(args.env), stage / "code")
    remote_name = ".allstocks-stage-" + uuid.uuid4().hex
    # Reject a misconfigured SSH destination before creating a stage or sending secrets.
    # Keep the privileged server-side preflight as an independent check.
    bootstrap = """import os,pathlib,pwd,socket,sys
if (socket.gethostname()!=sys.argv[1] or pwd.getpwuid(os.geteuid()).pw_name!='ubuntu'
        or pathlib.Path.home()!=pathlib.Path('/home/ubuntu')):
    raise SystemExit('Unexpected TradingSupport SSH target')
p=pathlib.Path.home()/sys.argv[2]
p.mkdir(mode=0o700)
print(p)
"""
    remote = run(["ssh", *options, host, shlex.join(["python3", "-c", bootstrap, SERVER_HOSTNAME, remote_name])])
    if remote != "/home/ubuntu/" + remote_name:
        raise ValueError("Unexpected SSH staging destination")
    run(["scp", "-p", *options, str(stage / "release.tar.gz"), str(stage / "manifest.json"),
         str(source / "deploy/release.py"), host + ":" + remote + "/"])
    if args.env:
        run(["scp", "-p", *options, str(Path(args.env).resolve()), host + ":" + remote + "/environment"])
    helper_hash = release.digest((source / "deploy/release.py").read_bytes())
    verify = """import hashlib,pathlib,os,sys
p=pathlib.Path(sys.argv[1])
if hashlib.sha256((p/'release.py').read_bytes()).hexdigest()!=sys.argv[2]:
    raise SystemExit('Release helper checksum mismatch')
os.execv(sys.executable,[sys.executable,str(p/'release.py'),str(p)])
"""
    run(["ssh", *options, host, shlex.join(["python3", "-c", verify, remote, helper_hash])])
    # No shared host setup, proxy edits, Git polling or global Docker commands.
    run(["ssh", *options, host, shlex.join(["sudo", "-n", "python3", remote + "/code/deploy/manage.py",
         "_apply", remote, args.command, str(args.drain_timeout)])])
    print("Server update completed; inspect the app through its protected hostname.")


def main():
    os.umask(0o077)
    if len(sys.argv) > 1 and sys.argv[1] in {"_apply", "recover"}:
        if os.geteuid() != 0 or os.environ.get("SUDO_USER") != "ubuntu":
            raise ValueError("Server operation requires sudo from the existing ubuntu account")
        root = Path("/home/ubuntu/allstocksDatabase")
        if sys.argv[1] == "recover":
            recover(root, sys.argv[2])
        else:
            stage = Path(sys.argv[2])
            if not re.fullmatch(r"/home/ubuntu/\.allstocks-stage-[a-f0-9]{32}", str(stage)) or stage.is_symlink():
                raise ValueError("Unsafe server stage")
            if sys.argv[3] not in {"setup", "update"}:
                raise ValueError("Unknown installation operation")
            apply(root, stage, sys.argv[3] == "setup", int(sys.argv[4]))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("setup", "update"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--env", help="Explicitly install/replace only this app's private environment file")
    parser.add_argument("--test-image", default="allstocksdatabase-app:latest", help="Cached local dependency image")
    parser.add_argument("--drain-timeout", type=int, default=300)
    args = parser.parse_args()
    if not 0 <= args.drain_timeout <= 86400 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*", args.test_image):
        raise ValueError("Invalid timeout or test image")
    local(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Deployment stopped: {type(exc).__name__}: {exc}" if isinstance(exc, (ValueError, TimeoutError, RuntimeError))
              else f"Deployment stopped: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1)

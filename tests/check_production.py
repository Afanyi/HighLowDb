"""Host-only production smoke test: cached images, dummy keys, isolated Docker network.

Run: python3 tests/check_production.py
No production environment file, volume, network, hostname routing or server is used.
"""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
CADDY = "caddy:2.10.2-alpine"
APP = "allstocksdatabase-app:latest"
CURL = "curlimages/curl:8.12.1"


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def main():
    # --no-env-resolution prevents reading the service's real env_file.
    environ = os.environ | {
        "APP_RELEASE": "phase1-test", "APP_PUBLIC_ORIGIN": "https://stocks.skyboyz1.duckdns.org",
        "APP_SECRET_KEY": "phase1-test-only-not-a-production-secret",
    }
    config = json.loads(run("docker", "compose", "--env-file", "/dev/null", "-f", str(ROOT / "compose.yaml"),
                            "-f", str(ROOT / "compose.production.yaml"), "config", "--no-env-resolution",
                            "--format", "json", env=environ))
    app = config["services"]["app"]
    assert not app.get("ports")
    assert config["name"] == "allstocks-database"
    assert config["volumes"]["data"]["name"] == "allstocks-database_data"
    assert config["networks"]["ingress"]["name"] == "trading-assistant_default"
    assert config["networks"]["ingress"]["external"]
    assert int(app["mem_limit"]) == 512 * 1024 * 1024
    assert app["restart"] == "unless-stopped"
    print("PASS: production Compose has a stable private upstream and volume, with bounded resources.")

    name = "allstocks-phase1-" + uuid.uuid4().hex[:12]
    containers = []
    created_network = False
    with tempfile.TemporaryDirectory(prefix="allstocks-phase1-") as directory:
        fixture = Path(directory)
        # Test-only password; no production hash is read or copied.
        password_hash = run("docker", "run", "--rm", "--pull=never", "--network", "none", CADDY,
                            "caddy", "hash-password", "--plaintext", "phase1-test-only")
        route = (ROOT / "deploy/Caddyfile.stocks").read_text()
        host = re.search(r"^([a-z0-9.-]+) \{", route, re.MULTILINE).group(1)
        # Private test CA rather than ACME; trust its root explicitly in curl.
        (fixture / "Caddyfile").write_text(route.replace(host + " {", host + " {\n\ttls internal", 1))
        try:
            run("docker", "network", "create", "--internal", name)
            created_network = True
            containers.append(name + "-app")
            mounts = []
            for source in ("app.py", "sync.py", "crypto_sync.py", "templates"):
                mounts.extend(("--mount", f"type=bind,src={ROOT / source},dst=/app/{source},readonly"))
            run("docker", "run", "-d", "--pull=never", "--name", containers[-1], "--network", name,
                "--network-alias", "allstocks-database-app", "--memory", "512m", "--memory-swap", "512m",
                "--cpus", "0.5", "--pids-limit", "128", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true", *mounts,
                "-e", "DB_PATH=/tmp/stocks.db", "-e", "CRYPTO_DB_PATH=/tmp/crypto.db",
                "-e", "T212_API_KEY=phase1-dummy", "-e", "COINGECKO_API_KEY=phase1-dummy",
                "-e", f"APP_PUBLIC_ORIGIN=https://{host}", "-e", "APP_SECRET_KEY=" + environ["APP_SECRET_KEY"], APP)
            containers.append(name + "-proxy")
            run("docker", "run", "-d", "--pull=never", "--name", containers[-1], "--network", name,
                "--network-alias", host, "--mount", f"type=bind,src={fixture}/Caddyfile,dst=/etc/caddy/Caddyfile,readonly",
                "-e", "DASHBOARD_USER=phase1", "-e", "DASHBOARD_PASSWORD_HASH=" + password_hash, CADDY)
            for attempt in range(40):
                try:
                    run("docker", "cp", containers[-1] + ":/data/caddy/pki/authorities/local/root.crt",
                        str(fixture / "root.crt"))
                    break
                except subprocess.CalledProcessError:
                    if attempt == 39:
                        raise
                    time.sleep(0.25)

            def request(path, status, *options):
                result = run("docker", "run", "--rm", "--pull=never", "--user", "0:0", "--network", name,
                             "--mount", f"type=bind,src={fixture},dst=/fixture", CURL,
                             "--silent", "--show-error", "--max-time", "10", "--noproxy", "*",
                             "--cacert", "/fixture/root.crt", "-b", "/fixture/cookies", "-c", "/fixture/cookies",
                             "-w", "\n%{http_code}", *options, "https://" + host + path)
                body, _, code = result.rpartition("\n")
                assert code == str(status), (path, code, status, body[:200])
                return body

            auth = ("--user", "phase1:phase1-test-only")
            for path in ("/", "/?mode=crypto", "/health"):
                request(path, 401)
                request(path, 401, "--user", "phase1:wrong")
            for path in ("/sync", "/sync/crypto"):
                request(path, 401, "-X", "POST")
            assert request("/health", 200, *auth) == "ok"
            page = request("/", 200, *auth)
            request("/?mode=crypto", 200, *auth)
            token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
            for path in ("/sync", "/sync/crypto"):
                request(path, 403, *auth, "-X", "POST")
                request(path, 403, *auth, "-d", "csrf_token=" + token, "-H", "Origin: https://attacker.example")
                request(path, 302, *auth, "-d", "csrf_token=" + token, "-H", "Origin: https://" + host)
            print("PASS: TLS certificate verified against isolated CA; both datasets and health require valid login.")
            print("PASS: both sync routes reject missing tokens/cross-site requests and accept authenticated forms.")
        finally:
            for container in reversed(containers):
                subprocess.run(["docker", "rm", "-f", "-v", container], capture_output=True)
            if created_network:
                subprocess.run(["docker", "network", "rm", name], capture_output=True)


if __name__ == "__main__":
    main()

"""Guarded GitHub Actions client; all server changes still use deploy/update.sh."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request

import manage


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "Afanyi/HighLowDb"
SHA = re.compile(r"[a-f0-9]{40}")


def eligible(environ):
    # The switch gates automatic push deploys only; a manual "Run workflow" on main always may deploy.
    event = environ.get("GITHUB_EVENT_NAME")
    return ((event == "workflow_dispatch" or (event == "push" and environ.get("ENABLE_AUTO_DEPLOY") == "true"))
            and environ.get("GITHUB_REF") == "refs/heads/main"
            and environ.get("GITHUB_REPOSITORY") == REPOSITORY
            and environ.get("CI_VERIFY_RESULT") == "success")


def check_revision(environ, child_environment):
    source = environ.get("GITHUB_SHA", "")
    if not SHA.fullmatch(source):
        raise ValueError("Invalid event commit")
    head = manage.run(["git", "rev-parse", "HEAD"], cwd=ROOT, env=child_environment)
    dirty = manage.run(["git", "status", "--porcelain", "--untracked-files=normal"],
                       cwd=ROOT, env=child_environment)
    if head != source or dirty:
        raise ValueError("Checkout is not the clean verified event commit")
    token = environ.get("GH_TOKEN", "")
    if not token:
        raise ValueError("Missing GitHub read token")
    request = urllib.request.Request(
        "https://api.github.com/repos/" + REPOSITORY + "/git/ref/heads/main",
        headers={"Accept": "application/vnd.github+json", "Authorization": "Bearer " + token,
                 "User-Agent": "HighLowDb-CI"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        remote = json.loads(response.read(65536))["object"]["sha"]
    if remote != source:
        raise ValueError("Superseded main commit; not deploying")


def main():
    environ = os.environ.copy()
    source = environ.get("GITHUB_SHA", "")
    source = source if SHA.fullmatch(source) else "unknown"
    candidate, outcome, stage = "not prepared", "failed", "eligibility check"
    child_environment = environ.copy()
    for name in ("DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS", "GH_TOKEN", "GITHUB_TOKEN"):
        child_environment.pop(name, None)
    result_code = 1
    try:
        if not eligible(environ):
            raise ValueError("Deployment is not eligible")
        stage = "source and main revision check"
        check_revision(environ, child_environment)
        stage = "SSH credential validation"
        host = environ.get("DEPLOY_HOST", "")
        key = environ.get("DEPLOY_SSH_KEY", "").strip()
        known = environ.get("DEPLOY_KNOWN_HOSTS", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host) or not key or not known:
            raise ValueError("Missing or invalid deployment settings")
        with tempfile.TemporaryDirectory(prefix="allstocks-ci-") as directory:
            private = Path(directory)
            key_path, known_path, config = private / "key", private / "known_hosts", private / "server.env"
            manage.write_private(key_path, (key + "\n").encode())
            manage.write_private(known_path, (known + "\n").encode())
            manage.write_private(config, ("AS_HOST=ubuntu@" + host + "\nAS_KEY=" + str(key_path)
                                         + "\nAS_KNOWN_HOSTS=" + str(known_path) + "\n").encode())
            # These native checks support hashed known_hosts entries and reject fingerprints.
            manage.run(["ssh-keygen", "-y", "-P", "", "-f", str(key_path)],
                       env=child_environment, stdin=subprocess.DEVNULL)
            manage.run(["ssh-keygen", "-F", host, "-f", str(known_path)],
                       env=child_environment, stdin=subprocess.DEVNULL)
            stage = "local image preparation"
            manage.docker("build", "--tag", "allstocksdatabase-app:latest", str(ROOT),
                          env=child_environment, stdin=subprocess.DEVNULL)
            # A queued run may become stale while building. Recheck before starting the updater.
            stage = "final source and main revision check"
            check_revision(environ, child_environment)
            stage = "updater (including candidate tests, lock, drain and rollback)"
            result = subprocess.run(
                ["bash", str(ROOT / "deploy/update.sh"), "--server-config", str(config)],
                cwd=ROOT, env=child_environment, stdin=subprocess.DEVNULL,
                capture_output=True, text=True,
            )
            releases = re.findall(r"^Reviewed release ([a-f0-9]{16}):", result.stdout, re.MULTILINE)
            if len(releases) == 1:
                candidate = releases[0]
            if result.returncode or len(releases) != 1:
                raise RuntimeError("Updater did not confirm a successful release")
        outcome, result_code = "succeeded", 0
    except Exception as exc:
        # Neither raw tool output nor exception text is safe for shared Actions logs.
        print("CI deployment failed during " + stage + " (" + type(exc).__name__ + ").", file=sys.stderr)
    finally:
        report = ("Source commit: `" + source + "`\n\nCandidate release: `" + candidate
                  + "`\n\nDeployment: **" + outcome + "**\n\nStage: " + stage + "\n")
        print(report)
        if environ.get("GITHUB_STEP_SUMMARY"):
            try:
                with Path(environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
                    summary.write(report)
            except OSError:
                print("Could not write deployment summary.", file=sys.stderr)
                result_code = 1
    return result_code


if __name__ == "__main__":
    raise SystemExit(main())

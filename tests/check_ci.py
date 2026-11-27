"""Host-only regressions for the CI runner; use fake tools and disposable source.

Run: python3 -m unittest tests.check_ci -q
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TestCiRunner(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="allstocks-ci-check-")))
        for name in ("tests", "deploy", "bin"):
            (self.root / name).mkdir()
        shutil.copyfile(ROOT / "tests/check_ci.sh", self.root / "tests/check_ci.sh")
        for name in ("setup.sh", "update.sh"):
            (self.root / "deploy" / name).write_text("#!/bin/bash\npython3 fixture-dry-run\n")
        self.log = self.root / "commands"
        self.environment = os.environ | {
            "PATH": str(self.root / "bin") + ":" + os.environ["PATH"],
            "CI_CHECK_LOG": str(self.log),
            "CI_CHECK_FAIL": "",
        }
        tool = "#!" + sys.executable + "\n" + '''import os,pathlib,sys
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
if name == "docker":
    label="docker:"+args[0]
elif args[:3] == ["-m","unittest","tests.check_ci"]:
    label="ci-tests"
elif args[:3] == ["-m","unittest","tests.check_deploy"]:
    label="host-tests"
elif args and args[0].endswith("check_deploy_docker.py"):
    label="docker-drill"
elif args and args[0].endswith("check_production.py"):
    label="https-smoke"
else:
    label="release-dry-run"
with open(os.environ["CI_CHECK_LOG"],"a") as stream:
    stream.write(label+"\\n")
sys.exit(23 if os.environ["CI_CHECK_FAIL"] == label else 0)
'''
        for name in ("docker", "python3"):
            path = self.root / "bin" / name
            path.write_text(tool)
            path.chmod(0o700)

    def run_checks(self, failure=""):
        self.log.write_text("")
        result = subprocess.run(
            ["bash", str(self.root / "tests/check_ci.sh")],
            env=self.environment | {"CI_CHECK_FAIL": failure},
            capture_output=True, text=True, timeout=20,
        )
        return result, self.log.read_text().splitlines()

    def test_each_invalid_shell_script_stops_before_tools(self):
        for name in ("tests/check_ci.sh", "deploy/setup.sh", "deploy/update.sh"):
            with self.subTest(script=name):
                path = self.root / name
                original = path.read_text()
                try:
                    path.write_text(original + "\nif\n")
                    result, calls = self.run_checks()
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(calls, [], "No test, Docker or deployment tool may start")
                finally:
                    path.write_text(original)

    def test_each_required_failure_stops_later_checks(self):
        for failure in ("ci-tests", "docker:version", "docker:compose", "docker:pull",
                        "docker:build", "docker:run", "host-tests", "docker-drill",
                        "https-smoke", "release-dry-run"):
            with self.subTest(failure=failure):
                result, calls = self.run_checks(failure)
                self.assertEqual(result.returncode, 23, result.stderr)
                self.assertEqual(calls[-1], failure)

    def test_success_reaches_every_required_suite(self):
        result, calls = self.run_checks()
        self.assertEqual(result.returncode, 0, result.stderr)
        for label in ("ci-tests", "docker:run", "host-tests", "docker-drill", "https-smoke"):
            self.assertIn(label, calls)
        self.assertEqual(calls[-1], "release-dry-run")


if __name__ == "__main__":
    unittest.main()

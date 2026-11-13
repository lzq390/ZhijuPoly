"""Offline systemd transactions for the guest's first execution boot.

No service is started: systemd-analyze builds its transaction in a temporary
filesystem. The cloud-final -> multi-user ordering matches Ubuntu 24.04.
"""
from __future__ import annotations

import configparser
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
GUEST_UNIT = ROOT / "ops/self_hosted_runner/guest/nexpoly-ci-bootstrap.service"
ANALYZE = shutil.which("systemd-analyze")


def parse_unit(text: str) -> configparser.ConfigParser:
    unit = configparser.ConfigParser(interpolation=None)
    unit.read_string(text)
    return unit


@unittest.skipUnless(ANALYZE, "systemd-analyze is required for offline ordering validation")
class GuestBootOrderingTests(unittest.TestCase):
    def transaction(self, bootstrap: str) -> tuple[int, str]:
        with tempfile.TemporaryDirectory(prefix="nexpoly-guest-ordering-") as temporary:
            root = Path(temporary)
            units = root / "etc/systemd/system"
            units.mkdir(parents=True)
            # verify checks executable paths, but never executes these stubs.
            interpreter = root / "usr/bin/python3"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text("#!/bin/sh\nexit 0\n")
            interpreter.chmod(0o755)
            fixtures = {
                "default.target": "[Unit]\nWants=multi-user.target cloud-init.target\n",
                "multi-user.target": "[Unit]\nDescription=Normal guest services\n",
                "cloud-init.target": (
                    "[Unit]\nAfter=multi-user.target\nWants=cloud-final.service\n"
                ),
                "cloud-final.service": (
                    "[Unit]\nAfter=multi-user.target\n[Service]\nType=oneshot\n"
                    "ExecStart=/usr/bin/python3\nRemainAfterExit=yes\n"
                ),
                "docker.service": "[Service]\nType=exec\nExecStart=/usr/bin/python3\n",
                "network-online.target": "[Unit]\nDescription=Network ready\n",
                "sysinit.target": "[Unit]\nDefaultDependencies=no\n",
                "basic.target": "[Unit]\nDefaultDependencies=no\n",
                "shutdown.target": "[Unit]\nDefaultDependencies=no\n",
                "nexpoly-ci-bootstrap.service": bootstrap,
            }
            for name, content in fixtures.items():
                (units / name).write_text(content)
            for target in parse_unit(bootstrap)["Install"]["WantedBy"].split():
                wants = units / (target + ".wants")
                wants.mkdir()
                (wants / "nexpoly-ci-bootstrap.service").symlink_to(
                    "../nexpoly-ci-bootstrap.service")
            environment = {**os.environ, "SYSTEMD_LOG_LEVEL": "debug",
                           "SYSTEMD_COLORS": "0", "LC_ALL": "C",
                           "SYSTEMD_ENVIRONMENT_GENERATORS": "/dev/null"}
            environment.pop("SYSTEMD_UNIT_PATH", None)
            result = subprocess.run(
                [ANALYZE, "verify", "--generators=no", "--root=" + temporary,
                 "default.target", "nexpoly-ci-bootstrap.service"],
                capture_output=True, text=True, timeout=15, env=environment,
            )
            diagnostics = result.stdout + result.stderr
            return result.returncode, diagnostics

    def test_old_multi_user_install_reproduces_the_cloud_final_cycle(self):
        old = GUEST_UNIT.read_text().replace(
            "Requires=docker.service cloud-final.service", "Requires=docker.service"
        ).replace("Type=exec", "Type=oneshot").replace(
            "WantedBy=cloud-init.target", "WantedBy=multi-user.target")
        _, diagnostics = self.transaction(old)
        # systemd may still return success after deleting a job to break a cycle.
        self.assertIn("ordering cycle", diagnostics)
        self.assertIn("deleted to break ordering cycle", diagnostics)

    def test_execution_boot_keeps_cloud_final_and_bootstrap_in_the_transaction(self):
        status, diagnostics = self.transaction(GUEST_UNIT.read_text())
        self.assertEqual(status, 0, diagnostics)
        self.assertNotIn("ordering cycle", diagnostics)
        self.assertIn("cloud-final.service: Installed new job cloud-final.service/start", diagnostics)
        self.assertIn(
            "nexpoly-ci-bootstrap.service: Installed new job nexpoly-ci-bootstrap.service/start",
            diagnostics,
        )
        self.assertIn("Type: exec", diagnostics)

    def test_long_running_runner_does_not_hold_cloud_init_target_until_job_exit(self):
        unit = parse_unit(GUEST_UNIT.read_text())
        # The bootstrap interpreter waits for registration and the entire job.
        # Type=exec completes systemd's start job when that interpreter execs;
        # Type=oneshot would keep cloud-init.target waiting for its final exit.
        self.assertEqual(unit["Service"]["Type"], "exec")
        self.assertIn("cloud-final.service", unit["Unit"]["After"].split())
        self.assertIn("cloud-final.service", unit["Unit"]["Requires"].split())
        # A template build still skips registration while its marker is present.
        self.assertEqual(unit["Unit"]["ConditionPathExists"],
                         "!/etc/nexpoly-ci-runner/build-mode")

    def test_template_build_marker_skips_bootstrap_and_sealed_boot_allows_it(self):
        condition = parse_unit(GUEST_UNIT.read_text())["Unit"]["ConditionPathExists"]
        with tempfile.TemporaryDirectory(prefix="nexpoly-build-condition-") as temporary:
            marker = Path(temporary) / Path(condition.removeprefix("!")).relative_to("/")
            marker.parent.mkdir(parents=True)
            # Exercise systemd's condition evaluator against disposable paths.
            # Requires dependencies do not turn a failed condition into failure
            # of this service; systemd simply skips its ExecStart during builds.
            for building in (False, True):
                with self.subTest(building=building):
                    if building:
                        marker.touch()
                    result = subprocess.run(
                        [ANALYZE, "condition", "ConditionPathExists=!" + str(marker)],
                        capture_output=True, text=True, timeout=15,
                        env={**os.environ, "LC_ALL": "C", "SYSTEMD_COLORS": "0"},
                    )
                    self.assertEqual(result.returncode, 1 if building else 0,
                                     result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

"""Exercise template maintenance offline; no host command or service is run."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "ops/self_hosted_runner/maintain-template.sh"
IDLE = {"paused": True, "state": {"instance": None}}
BUSY = {"paused": True, "state": {"instance": {"uuid": "active-vm"}}}


class TemplateMaintenanceTests(unittest.TestCase):
    def run_maintenance(self, statuses, fail="", clock_step=0):
        with tempfile.TemporaryDirectory(prefix="nexpoly-maintenance-test-") as temporary:
            temporary = Path(temporary)
            state = temporary / "state.json"
            events = temporary / "events.jsonl"
            state.write_text(json.dumps({"statuses": statuses, "index": 0, "clock": 0}))
            fake = temporary / "fake.py"
            fake.write_text('''import json, os, pathlib, sys
state_path = pathlib.Path(os.environ["NEXPOLY_TEST_STATE"])
state = json.loads(state_path.read_text())
command = sys.argv[1:]
if command[0] == "clock":
    print(state["clock"])
    state["clock"] += int(os.environ["NEXPOLY_TEST_CLOCK_STEP"])
elif command[0] == "sleep":
    assert command[1] == "5"
    state["clock"] += 5
else:
    with open(os.environ["NEXPOLY_TEST_EVENTS"], "a") as log:
        log.write(json.dumps(command) + "\\n")
    if ":".join(command) == os.environ["NEXPOLY_TEST_FAIL"]:
        sys.exit(23)
    if command == ["runnerctl", "status"]:
        index = min(state["index"], len(state["statuses"]) - 1)
        status = state["statuses"][index]
        state["index"] += 1
        if isinstance(status, str):
            print(status)
        else:
            print(json.dumps(status))
state_path.write_text(json.dumps(state))
''')
            package = temporary / "package"
            package.mkdir()
            (package / "install.sh").write_text(
                '#!/usr/bin/env bash\nset -euo pipefail\n'
                '/usr/bin/python3 "$NEXPOLY_TEST_FAKE" install\n')
            environment = {
                **os.environ,
                "NEXPOLY_TEST_HELPER": str(HELPER),
                "NEXPOLY_TEST_PACKAGE": str(package),
                "NEXPOLY_TEST_STATE": str(state),
                "NEXPOLY_TEST_EVENTS": str(events),
                "NEXPOLY_TEST_FAKE": str(fake),
                "NEXPOLY_TEST_FAIL": fail,
                "NEXPOLY_TEST_CLOCK_STEP": str(clock_step),
            }
            result = subprocess.run(["bash", "-c", '''
set -euo pipefail
source "$NEXPOLY_TEST_HELPER"
package_dir="$NEXPOLY_TEST_PACKAGE"
runnerctl() { /usr/bin/python3 "$NEXPOLY_TEST_FAKE" runnerctl "$@"; }
systemctl() { /usr/bin/python3 "$NEXPOLY_TEST_FAKE" systemctl "$@"; }
maintenance_now() { /usr/bin/python3 "$NEXPOLY_TEST_FAKE" clock; }
sleep() { /usr/bin/python3 "$NEXPOLY_TEST_FAKE" sleep "$@"; }
maintain_template
'''], env=environment, capture_output=True, text=True, timeout=15)
            recorded = [json.loads(line) for line in events.read_text().splitlines()]
            return result, recorded

    def test_busy_vm_is_retained_until_paused_and_empty(self):
        result, events = self.run_maintenance([
            BUSY, {"paused": False, "state": {"instance": None}}, IDLE,
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events, [
            ["runnerctl", "drain"],
            ["runnerctl", "status"], ["runnerctl", "status"], ["runnerctl", "status"],
            ["systemctl", "stop", "nexpoly-runner-controller.service"],
            ["install"], ["runnerctl", "update-template"], ["runnerctl", "resume"],
            ["systemctl", "start", "nexpoly-runner-controller.service"],
            ["runnerctl", "status"],
        ])
        self.assertIn("active VM/job is retained", result.stdout)

    def test_build_or_install_failure_never_resumes_or_starts(self):
        for fail in ("install", "runnerctl:update-template"):
            with self.subTest(fail=fail):
                result, events = self.run_maintenance([IDLE], fail=fail)
                self.assertEqual(result.returncode, 23, result.stderr)
                self.assertIn(["systemctl", "stop", "nexpoly-runner-controller.service"], events)
                self.assertNotIn(["runnerctl", "resume"], events)
                self.assertNotIn(["systemctl", "start", "nexpoly-runner-controller.service"], events)

    def test_deadline_aborts_without_stopping_active_vm(self):
        result, events = self.run_maintenance([BUSY], clock_step=6600)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Timed out waiting for VM cleanup", result.stderr)
        self.assertEqual(events, [["runnerctl", "drain"], ["runnerctl", "status"]])

    def test_invalid_status_aborts_without_stopping_controller(self):
        for status in (
            "invalid JSON", {"paused": True, "state": "unknown"},
            {"paused": True}, {"state": None}, {"paused": True, "state": {}},
            {"paused": True, "state": {"instance": False}},
            {"paused": True, "state": {"instance": "unknown"}},
        ):
            with self.subTest(status=status):
                result, events = self.run_maintenance([status])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Cannot validate controller status", result.stderr)
                self.assertEqual(events, [["runnerctl", "drain"], ["runnerctl", "status"]])

    def test_missing_initial_journal_null_is_valid_idle_cli_status(self):
        result, events = self.run_maintenance([{"paused": True, "state": None}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["runnerctl", "update-template"], events)
        self.assertIn(["runnerctl", "resume"], events)
        self.assertIn("Template maintenance: build and validate a new template.", result.stdout)

    def test_status_command_failure_aborts_without_stopping_controller(self):
        result, events = self.run_maintenance([IDLE], fail="runnerctl:status")
        self.assertEqual(result.returncode, 23)
        self.assertEqual(events, [["runnerctl", "drain"], ["runnerctl", "status"]])

    @unittest.skipIf(os.geteuid() == 0, "requires an unprivileged process")
    def test_direct_execution_rejects_unprivileged_user_before_commands(self):
        result = subprocess.run(["bash", str(HELPER)], capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("administrator execution is required", result.stderr)


if __name__ == "__main__":
    unittest.main()

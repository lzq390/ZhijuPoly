"""Exercise the complete maintenance entry point using function doubles only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "ops/self_hosted_runner/maintain-runner.sh"
CONTROLLER = "nexpoly-runner-controller.service"
RETIRE = ["python", "retire-idle.py"]
SWITCH = ["python", "switch-upstream.py"]
PREFIX = [["runnerctl", "drain"], ["systemctl", "stop", CONTROLLER], RETIRE]
ACCEPT = [SWITCH, ["runnerctl", "update-template"], ["runnerctl", "resume"],
          ["systemctl", "start", CONTROLLER], ["runnerctl", "status"]]


class RunnerMaintenanceTests(unittest.TestCase):
    def exercise(self, retire_codes=(0,), *, fail="", source_only=False):
        with tempfile.TemporaryDirectory(prefix="nexpoly-runner-maintenance-test-") as directory:
            root = Path(directory)
            state, events = root / "state.json", root / "events.jsonl"
            state.write_text(json.dumps({"retire_codes": list(retire_codes), "retire_index": 0}))
            fake = root / "fake.py"
            fake.write_text('''import json, os, pathlib, sys
state_path = pathlib.Path(os.environ["NEXPOLY_TEST_STATE"])
state = json.loads(state_path.read_text())
command = sys.argv[1:]
if command[0] == "python":
    command[1] = pathlib.Path(command[1]).name
    if command[1] not in {"retire-idle.py", "switch-upstream.py"}:
        raise SystemExit(99)
with open(os.environ["NEXPOLY_TEST_EVENTS"], "a") as log:
    log.write(json.dumps(command) + "\\n")
status = 0
if ":".join(command) == os.environ["NEXPOLY_TEST_FAIL"]:
    status = 23
elif command == ["python", "retire-idle.py"]:
    index = state["retire_index"]
    state["retire_index"] += 1
    status = state["retire_codes"][min(index, len(state["retire_codes"]) - 1)]
state_path.write_text(json.dumps(state))
raise SystemExit(status)
''')
            environment = {**os.environ, "NEXPOLY_TEST_HELPER": str(HELPER),
                           "NEXPOLY_TEST_FAKE": str(fake), "NEXPOLY_TEST_STATE": str(state),
                           "NEXPOLY_TEST_EVENTS": str(events), "NEXPOLY_TEST_FAIL": fail,
                           "NEXPOLY_TEST_SOURCE_ONLY": "1" if source_only else "0"}
            # Cover the actual sourceable function. No sudo, runnerctl,
            # systemctl, installer, retirement or proxy-switch executable runs.
            harness = '''set -euo pipefail
fake() { /usr/bin/python3 "$NEXPOLY_TEST_FAKE" "$@"; }
runnerctl() { fake runnerctl "$@"; }
systemctl() { fake systemctl "$@"; }
wait_for_idle() { fake wait_for_idle; }
maintenance_python() { fake python "$@"; }
source "$NEXPOLY_TEST_HELPER"
# Imported definitions replace these names; override them after sourcing too.
runnerctl() { fake runnerctl "$@"; }
systemctl() { fake systemctl "$@"; }
wait_for_idle() { fake wait_for_idle; }
maintenance_python() { fake python "$@"; }
if [[ "$NEXPOLY_TEST_SOURCE_ONLY" == 0 ]]; then
    maintain_runner
fi
'''
            result = subprocess.run(["bash", "-c", harness], env=environment,
                                    capture_output=True, text=True, timeout=15)
            recorded = [json.loads(line) for line in events.read_text().splitlines()] if events.exists() else []
            return result, recorded

    def test_source_only_defines_functions_without_running_maintenance(self):
        result, events = self.exercise(source_only=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events, [])
        self.assertEqual(result.stdout, "")

    def test_success_retires_before_switch_build_acceptance_and_resume(self):
        result, events = self.exercise()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events, PREFIX + ACCEPT)
        self.assertIn("retain its VM", result.stdout)

    def test_assigned_job_restores_old_controller_waits_and_retires_again(self):
        result, events = self.exercise((75, 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events, PREFIX + [["systemctl", "start", CONTROLLER],
                                          ["wait_for_idle"], ["systemctl", "stop", CONTROLLER], RETIRE] + ACCEPT)
        self.assertIn("real assigned/busy job remains", result.stdout)

    def test_uncertainty_retains_vm_under_drained_controller_and_stops_migration(self):
        result, events = self.exercise((76,))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(events, PREFIX + [["systemctl", "start", CONTROLLER]])
        self.assertIn("VM retained under the drained installed controller", result.stderr)

    def test_any_retirement_failure_prevents_switch_template_and_resume(self):
        for statuses in ((1,), (23,), (75, 1), (75, 76)):
            with self.subTest(statuses=statuses):
                result, events = self.exercise(statuses)
                self.assertEqual(result.returncode, statuses[-1])
                self.assertEqual(events[:3], PREFIX)
                self.assertNotIn(SWITCH, events)
                self.assertNotIn(["runnerctl", "update-template"], events)
                self.assertNotIn(["runnerctl", "resume"], events)
                self.assertFalse(any("install" in str(event) for event in events))
                expected_starts = 1 if statuses[0] == 75 else 0
                self.assertEqual(events.count(["systemctl", "start", CONTROLLER]), expected_starts)

    def test_switch_failure_never_builds_resumes_or_starts_new_controller(self):
        result, events = self.exercise(fail="python:switch-upstream.py")
        self.assertEqual(result.returncode, 23)
        self.assertEqual(events, PREFIX + [SWITCH])
        self.assertNotIn(["runnerctl", "resume"], events)

    def test_template_failure_never_resumes_or_starts_new_controller(self):
        result, events = self.exercise(fail="runnerctl:update-template")
        self.assertEqual(result.returncode, 23)
        self.assertEqual(events, PREFIX + [SWITCH, ["runnerctl", "update-template"]])
        self.assertNotIn(["runnerctl", "resume"], events)

    def test_wait_failure_preserves_running_drained_controller_without_switch(self):
        result, events = self.exercise((75,), fail="wait_for_idle")
        self.assertEqual(result.returncode, 23)
        self.assertEqual(events, PREFIX + [["systemctl", "start", CONTROLLER], ["wait_for_idle"]])
        self.assertEqual(events.count(["systemctl", "stop", CONTROLLER]), 1)
        self.assertNotIn(SWITCH, events)

    def test_failed_initial_drain_or_stop_cannot_retire_or_migrate(self):
        for fail, expected in (("runnerctl:drain", PREFIX[:1]),
                               ("systemctl:stop:" + CONTROLLER, PREFIX[:2])):
            with self.subTest(fail=fail):
                result, events = self.exercise(fail=fail)
                self.assertEqual(result.returncode, 23)
                self.assertEqual(events, expected)

    @unittest.skipIf(os.geteuid() == 0, "requires an unprivileged process")
    def test_direct_execution_requires_admin_before_running_any_phase(self):
        result = subprocess.run(["bash", str(HELPER)], capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("administrator execution is required", result.stderr)


if __name__ == "__main__":
    unittest.main()

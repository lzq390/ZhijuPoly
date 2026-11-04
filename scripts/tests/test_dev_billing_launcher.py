from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts.dev_gpu_operator import DevGpuOperator, OperatorError, billing_configuration, prepare_runtime
from scripts.dev_gpu_session import SessionController


ROOT = Path(__file__).resolve().parents[2]
MEMBERSHIP = "NEXPOLY_DEV_MEMBERSHIP_ENABLED"
SELECTOR = "NEXPOLY_DEV_BILLING_SIMULATION_ENABLED"
PRICE = "NEXPOLY_DEV_BILLING_MONTHLY_PRICE_FEN"


class DevBillingLauncherTests(unittest.TestCase):
    def _load(self, overrides):
        source = (ROOT / "scripts/dev_server_gpu.sh").read_text()
        start = source.index("load_billing_environment() {")
        function = source[start:source.index("\n}\n", start) + 3]
        environment = {"PATH": os.environ["PATH"], "NEXPOLY_DEV_USER_ISOLATION_ENABLED": "true",
                       "AUTH_COOKIE_NAME": "nexpoly_dev_test", "NEXPOLY_DEV_SCHEMA_TARGET": "literature-0024",
                       **overrides}
        return subprocess.run(["bash", "-c", "set -euo pipefail\n" + function + '''
load_billing_environment
python3 -c 'import os,json;print(json.dumps({k:os.getenv(k) for k in ["NEXPOLY_DEV_MEMBERSHIP_ENABLED","NEXPOLY_DEV_BILLING_SIMULATION_ENABLED","NEXPOLY_DEV_BILLING_MONTHLY_PRICE_FEN","NEXPOLY_DEV_SCHEMA_TARGET"]}))'
printf '%s\\n' "${BILLING_BUILD_CONFIG_FILES[*]}" "${BILLING_COMPOSE_FILES[*]}"
'''], env=environment, capture_output=True, text=True)

    def test_default_retains_research_boundary_and_has_no_billing_overlay(self):
        result = self._load({})
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(json.loads(lines[0])[MEMBERSHIP], "false")
        self.assertEqual(json.loads(lines[0])[SELECTOR], "false")
        self.assertEqual(json.loads(lines[0])["NEXPOLY_DEV_SCHEMA_TARGET"], "literature-0024")
        self.assertEqual(lines[1:], ["", ""])

    def test_selected_simulation_uses_0025_and_the_same_build_and_runtime_overlay(self):
        result = self._load({SELECTOR: "true", PRICE: "9900"})
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(json.loads(lines[0]), {MEMBERSHIP: "true", SELECTOR: "true", PRICE: "9900",
                                              "NEXPOLY_DEV_SCHEMA_TARGET": "membership-0025"})
        self.assertEqual(lines[1:], ["docker-compose.billing-simulation-dev.yml",
                                    "-f docker-compose.billing-simulation-dev.yml"])

    def test_disabling_simulation_retains_0025_build_and_runtime_overlay(self):
        for price in ("", "9900"):
            with self.subTest(price=price):
                result = self._load({MEMBERSHIP: "true", SELECTOR: "false", PRICE: price})
                self.assertEqual(result.returncode, 0, result.stderr)
                lines = result.stdout.splitlines()
                self.assertEqual(json.loads(lines[0]), {
                    MEMBERSHIP: "true", SELECTOR: "false", PRICE: price,
                    "NEXPOLY_DEV_SCHEMA_TARGET": "membership-0025"})
                self.assertEqual(lines[1:], ["docker-compose.billing-simulation-dev.yml",
                                            "-f docker-compose.billing-simulation-dev.yml"])

    def test_membership_without_simulation_preserves_isolation_and_quota_guards(self):
        for overrides in [{MEMBERSHIP: "yes"}, {MEMBERSHIP: ""}, {PRICE: "0"},
                          {PRICE: "100000001"}, {"AUTH_COOKIE_NAME": "production"},
                          {"NEXPOLY_DEV_USER_ISOLATION_ENABLED": "false"},
                          {"NATIVE_COHORT_CONFIG_INPUT": "/private/cohort.json"},
                          {"USAGE_ENTITLEMENTS_ENABLED": "true"}]:
            with self.subTest(overrides=overrides):
                result = self._load({MEMBERSHIP: "true", SELECTOR: "false", **overrides})
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_malformed_or_nonisolated_simulation_is_rejected_before_launch(self):
        for overrides in [{SELECTOR: "yes"}, {MEMBERSHIP: "false"}, {MEMBERSHIP: ""},
                          {PRICE: ""}, {PRICE: "0"}, {PRICE: "100000001"},
                          {PRICE: "9900;true"}, {PRICE: "09900"}, {"AUTH_COOKIE_NAME": "production"},
                          {"NEXPOLY_DEV_USER_ISOLATION_ENABLED": "false"},
                          {"NATIVE_COHORT_CONFIG_INPUT": "/private/cohort.json"},
                          {"USAGE_ENTITLEMENTS_ENABLED": "true"}]:
            with self.subTest(overrides=overrides):
                result = self._load({SELECTOR: "true", PRICE: "9900", **overrides})
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_operator_selection_requires_a_bounded_explicit_price(self):
        self.assertEqual(billing_configuration({}), {})
        self.assertEqual(billing_configuration({MEMBERSHIP: "true"}),
                         {MEMBERSHIP: "true", SELECTOR: "false", PRICE: ""})
        for environment in [{SELECTOR: "true"}, {SELECTOR: "yes", PRICE: "9900"},
                            {SELECTOR: "true", PRICE: "100000001"},
                            {MEMBERSHIP: "false", SELECTOR: "true", PRICE: "9900"},
                            {MEMBERSHIP: ""}, {MEMBERSHIP: "true", PRICE: "0"}]:
            with self.assertRaises(OperatorError):
                billing_configuration(environment)

    def test_recovery_inputs_override_later_dotenv_defaults(self):
        source = (ROOT / "scripts/dev_server_gpu.sh").read_text()
        capture = source[source.index("declare -A RESEARCH_LAUNCH_INPUTS"):source.index("unset research_input")]
        functions = []
        for name in ("load_research_environment", "load_billing_environment"):
            start = source.index(name + "() {")
            functions.append(source[start:source.index("\n}\n", start) + 3])
        for initial in [{SELECTOR: "true", PRICE: "9900"},
                        {MEMBERSHIP: "true", SELECTOR: "false", PRICE: ""}]:
            # The ordinary .env.dev source runs after the operator input capture.
            # In particular, an explicit schema opt-in must survive a later false.
            script = "set -euo pipefail\n" + capture + '''
NEXPOLY_DEV_MEMBERSHIP_ENABLED=false
NEXPOLY_DEV_BILLING_SIMULATION_ENABLED=true
NEXPOLY_DEV_BILLING_MONTHLY_PRICE_FEN=12345
''' + "\n".join(functions) + '''
load_research_environment
load_billing_environment
printf '%s|%s|%s|%s\\n' "$NEXPOLY_DEV_MEMBERSHIP_ENABLED" "$NEXPOLY_DEV_BILLING_SIMULATION_ENABLED" "$NEXPOLY_DEV_BILLING_MONTHLY_PRICE_FEN" "$NEXPOLY_DEV_SCHEMA_TARGET"
'''
            # Legacy callers acquire a canonical schema selector in the launcher
            # before a controller/operator is started.
            frozen = billing_configuration(initial)
            with self.subTest(initial=initial):
                result = subprocess.run(["bash", "-c", script], env={
                    "PATH": os.environ["PATH"], **frozen,
                    "NEXPOLY_DEV_USER_ISOLATION_ENABLED": "true", "AUTH_COOKIE_NAME": "nexpoly_dev_test"},
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(),
                                 f"true|{frozen[SELECTOR]}|{frozen[PRICE]}|membership-0025")

    def test_operator_recovery_freezes_enabled_and_disabled_selection(self):
        for initial in [{}, {SELECTOR: "true", PRICE: "9900"},
                        {MEMBERSHIP: "true", SELECTOR: "false"},
                        {MEMBERSHIP: "true", SELECTOR: "false", PRICE: "9900"}]:
            with self.subTest(initial=initial), tempfile.TemporaryDirectory() as raw:
                repository = Path(raw)
                prepare_runtime(repository)
                with patch.dict(os.environ, initial, clear=True):
                    operator = DevGpuOperator(repository=repository, source_sha="a" * 40, source_tree="b" * 40)
                operation = operator._new_operation("starting", "starting")
                operator._write_operation(operation)
                statuses = iter([{"schema_version": 1, "status": "stopped", "gpu_index": 1},
                                 {"schema_version": 1, "status": "ready", "gpu_index": 1}])
                operator._controller_status = lambda: next(statuses)
                with patch.dict(os.environ, {MEMBERSHIP: "false", SELECTOR: "true", PRICE: "12345"}), \
                        patch("scripts.dev_gpu_operator.subprocess.Popen") as popen:
                    popen.return_value.wait.return_value = 0
                    operator._run_recovery(operation["operation_id"], time.monotonic() + 5)
                child = popen.call_args.kwargs["env"]
                expected = billing_configuration(initial)
                self.assertEqual(child[MEMBERSHIP], expected.get(MEMBERSHIP, "false"))
                self.assertEqual(child[SELECTOR], initial.get(SELECTOR, "false"))
                self.assertEqual(child.get(PRICE), expected.get(PRICE))

    def test_controller_cpu_restore_keeps_billing_selection(self):
        for simulation in ("true", "false"):
            with self.subTest(simulation=simulation), tempfile.TemporaryDirectory() as raw:
                controller = SessionController(Path(raw) / ("run-" + "d" * 32), "a" * 40, "b" * 40, direct_start=True)
                with patch.dict(os.environ, {MEMBERSHIP: "true", SELECTOR: simulation, PRICE: "9900"}), \
                    patch("scripts.dev_gpu_session.subprocess.run") as run:
                    run.return_value.returncode = 0
                    self.assertTrue(controller._recovery_command("gpu-session-restore-cpu-internal"))
                child = run.call_args.kwargs["env"]
                self.assertEqual(child[MEMBERSHIP], "true")
                self.assertEqual(child[SELECTOR], simulation)
                self.assertEqual(child[PRICE], "9900")
                self.assertEqual(child["NEXPOLY_DEV_GPU_SESSION_INTERNAL_RECOVERY"], "1")

    def test_compose_overlay_retains_0025_when_simulation_is_disabled(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw) / "base.json"
            base.write_text(json.dumps({"services": {"backend": {"image": "python:3.11",
                "environment": {"NEXPOLY_BILLING_ENABLED": "true", "NEXPOLY_BILLING_PROVIDERS": "wechat",
                                "NEXPOLY_POSTGRES_PREFLIGHT_ARGS": "--strict --schema-target literature-0024"}}}}))
            for inputs in ({SELECTOR: "true", PRICE: "9900"}, {SELECTOR: "false"}, {}):
                with self.subTest(inputs=inputs):
                    result = subprocess.run(["docker", "compose", "-p", "billing-launcher-test", "-f", str(base),
                        "-f", str(ROOT / "docker-compose.billing-simulation-dev.yml"), "config", "--format", "json"],
                        env={"PATH": os.environ["PATH"], **inputs}, capture_output=True, text=True, check=True)
                    environment = json.loads(result.stdout)["services"]["backend"]["environment"]
                    self.assertEqual(environment["NEXPOLY_BILLING_ENABLED"], "false")
                    self.assertEqual(environment["NEXPOLY_BILLING_PROVIDERS"], "")
                    self.assertEqual(environment["NEXPOLY_BILLING_SIMULATION_ENABLED"], inputs.get(SELECTOR, "false"))
                    self.assertEqual(environment["NEXPOLY_BILLING_REGISTRATION_ENABLED"], "true")
                    self.assertEqual(environment["NEXPOLY_BILLING_MONTHLY_PRICE_FEN"], inputs.get(PRICE, ""))
                    self.assertEqual(environment["NEXPOLY_POSTGRES_PREFLIGHT_ARGS"],
                                     "--strict --schema-target membership-0025 --service-context")
                    self.assertEqual(environment["USAGE_ENTITLEMENTS_ENABLED"], "false")


if __name__ == "__main__":
    unittest.main()

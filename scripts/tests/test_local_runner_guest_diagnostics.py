from __future__ import annotations

from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
GUEST = ROOT / "ops/self_hosted_runner/guest"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, GUEST / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = load("diagnostic_guest_bootstrap", "bootstrap.py")
diagnostics = sys.modules["exit_diagnostics"]
smoke = load("diagnostic_guest_smoke", "smoke.py")


class GuestDiagnosticsTests(unittest.TestCase):
    def test_counter_summary_uses_fixed_interfaces_and_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vmstat, events = root / "vmstat", root / "events"
            vmstat.write_text("irrelevant SECRET\noom_kill 3\n")
            events.write_text("oom 8\noom_kill 2\n")
            with mock.patch.object(diagnostics, "VMSTAT", vmstat), mock.patch.object(diagnostics, "UNIT_EVENTS", events):
                self.assertEqual(diagnostics.oom_counters(), {"oom_kill_count": 3, "unit_oom_kill_count": 2})
                events.write_text("oom_kill -2\n")
                self.assertEqual(diagnostics.oom_counters(), {"oom_kill_count": 3})

    def test_state_query_is_bounded_fixed_and_never_returns_raw_output(self):
        units = "\n\n".join("Id=" + unit + "\nActiveState=" + ("deactivating" if unit == "docker.service" else "active")
                                for unit in diagnostics.UNITS)
        units += "\n\nId=SECRET\nActiveState=TOKEN\nEnvironment=PRIVATE\n"
        results = [subprocess.CompletedProcess([], 0, units), subprocess.CompletedProcess([], 0, "stopping\n")]
        with mock.patch.object(diagnostics.subprocess, "run", side_effect=results) as run:
            fields = diagnostics.unit_states()
        self.assertEqual(fields["docker_state"], "deactivating")
        self.assertEqual(fields["system_state"], "stopping")
        self.assertTrue({"apt_daily_state", "apt_upgrade_state", "unattended_upgrades_state"} <= set(fields))
        self.assertNotIn("SECRET", json.dumps(fields))
        self.assertNotIn("PRIVATE", json.dumps(fields))
        self.assertEqual(run.call_args_list[0].args[0][-6:], list(diagnostics.UNITS))
        for call in run.call_args_list:
            self.assertLessEqual(call.kwargs["timeout"], 1)
            self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)
            self.assertNotIn("Environment", " ".join(call.args[0]))
        with mock.patch.object(diagnostics.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 0.5)) as run:
            self.assertEqual(set(diagnostics.unit_states().values()), {"unknown"})
            self.assertEqual(run.call_count, 1)

    def test_record_sends_only_fixed_enums_integers_and_oom_deltas(self):
        emit = mock.Mock()
        session = diagnostics.GuestDiagnostics(emit)
        session.enabled = True
        session.baseline = {"oom_kill_count": 3, "unit_oom_kill_count": 2}
        with mock.patch.object(diagnostics, "unit_states", return_value={"docker_state": "active"}), \
                mock.patch.object(diagnostics, "oom_counters", return_value={"oom_kill_count": 5, "unit_oom_kill_count": 2}):
            session.record("runner_exit", **diagnostics.exit_fields(-15),
                           exception="SECRET", token="PRIVATE", service_result="untrusted SECRET")
            session.record("untrusted SECRET")
        emit.assert_called_once()
        self.assertEqual(emit.call_args.args, ("guest_diagnostic",))
        fields = emit.call_args.kwargs
        self.assertEqual(fields["exit_code"], -15)
        self.assertEqual(fields["signal"], 15)
        self.assertEqual(fields["oom_kill_delta"], 2)
        self.assertEqual(fields["unit_oom_kill_delta"], 0)
        self.assertGreaterEqual(fields["uptime_ms"], 0)
        self.assertNotIn("SECRET", json.dumps(fields))
        self.assertNotIn("PRIVATE", json.dumps(fields))
        self.assertEqual(diagnostics.exit_fields(True), {})
        self.assertEqual(diagnostics.exit_fields(-999), {})

    def test_marker_enables_only_root_and_validates_private_fixed_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            session = diagnostics.GuestDiagnostics(mock.Mock(), root)
            with mock.patch.object(diagnostics.os, "geteuid", return_value=1001), \
                    mock.patch.object(diagnostics, "oom_counters") as counters:
                self.assertFalse(session.enable())
                counters.assert_not_called()
            metadata = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0)
            with mock.patch.object(diagnostics.os, "geteuid", return_value=0), \
                    mock.patch.object(Path, "lstat", return_value=metadata), \
                    mock.patch.object(diagnostics, "oom_counters", return_value={"oom_kill_count": 2}), \
                    mock.patch.object(diagnostics.signal, "getsignal", return_value=signal.SIG_DFL), \
                    mock.patch.object(diagnostics.signal, "signal"):
                self.assertTrue(session.enable())
            marker = root / diagnostics.MARKER
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
            private_file = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0)
            with mock.patch.object(diagnostics.os, "geteuid", return_value=0), \
                    mock.patch.object(diagnostics.os, "fstat", return_value=private_file):
                loaded = diagnostics.load_enabled(mock.Mock(), root)
                self.assertEqual(loaded.baseline, {"oom_kill_count": 2})
                for data in ({"schema_version": 1, "baseline": {"token": "SECRET"}},
                             {"schema_version": 1, "baseline": {"oom_kill_count": True}},
                             {"schema_version": 1, "baseline": {"oom_kill_count": -1}}):
                    marker.write_text(json.dumps(data))
                    self.assertIsNone(diagnostics.load_enabled(mock.Mock(), root))

    def test_stop_post_is_gated_and_reads_only_three_fixed_manager_environment_keys(self):
        environment = mock.Mock()
        values = {"SERVICE_RESULT": "oom-kill", "EXIT_CODE": "killed", "EXIT_STATUS": "KILL"}
        environment.get.side_effect = lambda key, default=None: values.get(key, default)
        with mock.patch.object(diagnostics.os, "environ", environment):
            self.assertEqual(diagnostics.systemd_exit_fields(), {
                "service_result": "oom-kill", "service_exit_kind": "killed", "signal": 9})
        self.assertEqual([call.args[0] for call in environment.get.call_args_list],
                         ["SERVICE_RESULT", "EXIT_CODE", "EXIT_STATUS"])
        with mock.patch.dict(os.environ, {"SERVICE_RESULT": "SECRET", "EXIT_CODE": "SECRET", "EXIT_STATUS": "SECRET"}):
            self.assertEqual(diagnostics.systemd_exit_fields(), {"service_result": "unknown", "service_exit_kind": "unknown"})
        with mock.patch.dict(sys.modules, {"bootstrap": bootstrap}), \
                mock.patch.object(diagnostics, "load_enabled", return_value=None), \
                mock.patch.object(diagnostics, "systemd_exit_fields") as fields:
            self.assertEqual(diagnostics.main(), 0)
            fields.assert_not_called()

    def test_signal_records_without_queries_and_preserves_default_termination(self):
        # Real ordinary child, no privilege changes, Docker, systemd or network.
        code = """import json, os, signal, sys
sys.path.insert(0, sys.argv[1])
from exit_diagnostics import GuestDiagnostics
def emit(event, **fields):
    print(json.dumps(dict(event=event, **fields)), flush=True)
d = GuestDiagnostics(emit)
d.enabled = True
d.handlers[signal.SIGTERM] = signal.SIG_DFL
signal.signal(signal.SIGTERM, d.handle_signal)
os.kill(os.getpid(), signal.SIGTERM)
raise SystemExit(99)
"""
        result = subprocess.run([sys.executable, "-c", code, str(GUEST)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, -signal.SIGTERM)
        event = json.loads(result.stdout)
        self.assertGreaterEqual(event.pop("uptime_ms"), 0)
        self.assertEqual(event, {"event": "guest_diagnostic", "schema_version": 1,
                                 "reason": "bootstrap_signal", "signal": 15})
        self.assertEqual(result.stderr, "")
        session = diagnostics.GuestDiagnostics(mock.Mock(side_effect=OSError))
        session.handlers[signal.SIGTERM] = signal.SIG_DFL
        with mock.patch.object(diagnostics.signal, "signal") as restore, \
                mock.patch.object(diagnostics.os, "kill") as kill, \
                mock.patch.object(diagnostics, "unit_states") as query:
            session.handle_signal(signal.SIGTERM, None)
        restore.assert_called_once_with(signal.SIGTERM, signal.SIG_DFL)
        kill.assert_called_once_with(os.getpid(), signal.SIGTERM)
        query.assert_not_called()

    def test_bootstrap_waits_for_registration_and_cleared_diag_before_enabling(self):
        for registration_fails, diag_remains in ((False, False), (True, False), (False, True)):
            with self.subTest(registration_fails=registration_fails, diag_remains=diag_remains), \
                    tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root = Path(directory)
                runner = root / "runner"
                runner.mkdir()
                if diag_remains:
                    (runner / "_diag").mkdir()
                events = []
                session = mock.Mock()
                stack.enter_context(mock.patch.object(bootstrap, "GuestDiagnostics", return_value=session))
                stack.enter_context(mock.patch.object(bootstrap, "RUNNER", runner))
                stack.enter_context(mock.patch.object(bootstrap, "RUNTIME", root / "runtime"))
                stack.enter_context(mock.patch.object(bootstrap.Path, "read_text", return_value=bootstrap.PROFILE))
                stack.enter_context(mock.patch.object(bootstrap, "ensure_user_session"))
                stack.enter_context(mock.patch.object(bootstrap.os, "fork", return_value=123))
                stack.enter_context(mock.patch.object(bootstrap.os, "waitpid", side_effect=lambda *args: events.append("registration_child_exited") or (123, 256 if registration_fails else 0)))
                session.enable.side_effect = lambda: events.append("diagnostics_enabled")
                stack.enter_context(mock.patch.object(bootstrap, "emit"))
                run = stack.enter_context(mock.patch.object(bootstrap.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)))
                result = bootstrap.main()
                self.assertEqual(result, 1 if registration_fails or diag_remains else 0)
                self.assertEqual(events[0], "registration_child_exited")
                if registration_fails or diag_remains:
                    session.enable.assert_not_called()
                    session.record.assert_not_called()
                    self.assertEqual(run.call_count, 1)  # Existing final poweroff.
                else:
                    self.assertEqual(events, ["registration_child_exited", "diagnostics_enabled"])
                    self.assertEqual(session.record.call_args_list,
                                     [mock.call("runner_started"), mock.call("runner_exit", exit_code=0)])
                session.restore_signals.assert_called_once()

    def test_both_upstream_ports_are_checked_and_successful_direct_access_fails_smoke(self):
        with mock.patch.object(smoke, "denied_direct") as denied:
            smoke.check_direct_egress()
        self.assertEqual(denied.call_args_list, [mock.call("10.0.2.2", 17891),
                                               mock.call("10.0.2.2", 27891), mock.call("1.1.1.1", 443)])
        for port in (17891, 27891):
            with self.subTest(port=port), mock.patch.object(smoke.socket, "create_connection") as connect:
                with self.assertRaisesRegex(RuntimeError, "direct network access"):
                    smoke.denied_direct("10.0.2.2", port)
                connect.return_value.close.assert_called_once()

    def test_stop_post_unit_does_not_override_main_exit_or_add_dependency_cycles(self):
        text = (GUEST / "nexpoly-ci-bootstrap.service").read_text()
        self.assertIn("ExecStopPost=-/usr/bin/python3 /usr/local/lib/nexpoly-ci-runner/exit_diagnostics.py", text)
        self.assertIn("ConditionPathExists=!/etc/nexpoly-ci-runner/build-mode", text)
        self.assertIn("After=network-online.target docker.service cloud-final.service user@1001.service", text)
        self.assertNotIn("Restart=always", text)


if __name__ == "__main__":
    unittest.main()

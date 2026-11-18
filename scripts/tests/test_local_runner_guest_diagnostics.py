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
import time
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
                self.assertEqual(loaded.registered_ms, session.registered_ms)
                marker.write_text(json.dumps({"schema_version": 1, "baseline": {}}))
                self.assertIsNone(diagnostics.load_enabled(mock.Mock(), root).registered_ms)
                for data in ({"schema_version": 1, "baseline": {"token": "SECRET"}},
                             {"schema_version": 1, "baseline": {"oom_kill_count": True}},
                             {"schema_version": 1, "baseline": {"oom_kill_count": -1}},
                             {"schema_version": 1, "baseline": {}, "registered_uptime_ms": True},
                             {"schema_version": 1, "baseline": {}, "registered_uptime_ms": None},
                             {"schema_version": 1, "baseline": {}, "registered_uptime_ms": -1}):
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
                mock.patch.object(diagnostics, "journal_bytes") as journal, \
                mock.patch.object(diagnostics, "systemd_exit_fields") as fields:
            self.assertEqual(diagnostics.main(), 0)
            fields.assert_not_called()
            journal.assert_not_called()

    @staticmethod
    def journal_line(**fields):
        return (json.dumps({"_PID": "1", "UNIT": "nexpoly-ci-bootstrap.service",
                            "MESSAGE_ID": "de5b426a63be47a7b6ac3eaac82e2f6f",
                            "__MONOTONIC_TIMESTAMP": "150000", **fields}) + "\n").encode()

    def test_history_classifies_metadata_and_never_copies_messages_or_unknown_fields(self):
        data = (self.journal_line(JOB_TYPE="restart", JOB_ID="42", JOB_RESULT="done",
                                  MESSAGE="TOKEN restart requested by SECRET", Environment="PRIVATE")
                + self.journal_line(UNIT="shutdown.target", MESSAGE_ID="7d4958e842da4a758f6c1cdc7b36dcc5")
                + self.journal_line(_PID="900", _UID="0", _COMM="needrestart",
                                    _SYSTEMD_UNIT="apt-daily-upgrade.service", MESSAGE="SECRET"))
        events = diagnostics.classify_history(data, 100, 200)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0], {"schema_version": 1, "reason": "unit_history", "uptime_ms": 150,
                                     "history_source": "systemd", "source_unit": "bootstrap",
                                     "unit_transition": "stopping", "systemd_job_type": "restart",
                                     "systemd_job_result": "done", "systemd_job_id": 42})
        self.assertEqual(events[1]["source_unit"], "shutdown")
        self.assertEqual(events[1]["systemd_job_type"], "unknown")
        self.assertEqual(events[2], {"schema_version": 1, "reason": "unit_history", "uptime_ms": 150,
                                     "history_source": "needrestart", "source_unit": "apt_upgrade",
                                     "unit_transition": "needrestart_observed"})
        encoded = json.dumps(events)
        for forbidden in ("TOKEN", "SECRET", "PRIVATE", "MESSAGE", "Environment"):
            self.assertNotIn(forbidden, encoded)

    def test_history_rejects_pre_registration_future_unknown_and_spoofed_metadata(self):
        invalid = [{"__MONOTONIC_TIMESTAMP": "99000"}, {"__MONOTONIC_TIMESTAMP": "201000"},
                   {"__MONOTONIC_TIMESTAMP": ["150000"]}, {"__MONOTONIC_TIMESTAMP": "9" * 100},
                   {"UNIT": "SECRET.service"}, {"UNIT": ["nexpoly-ci-bootstrap.service"]},
                   {"MESSAGE_ID": "unknown"}, {"MESSAGE_ID": ["de5b426a63be47a7b6ac3eaac82e2f6f"]},
                   {"_PID": "123"}, {"_PID": "123", "_UID": "1001", "_COMM": "needrestart"}]
        for fields in invalid:
            with self.subTest(fields=fields):
                self.assertEqual(diagnostics.classify_history(self.journal_line(**fields), 100, 200), [])
        self.assertEqual(diagnostics.classify_history(b"not-json\n[]\n" + self.journal_line()[:-1], 100, 200), [])
        event = diagnostics.classify_history(self.journal_line(JOB_TYPE="SECRET", JOB_RESULT=["done"],
                                                               JOB_ID="TOKEN"), 100, 200)[0]
        self.assertEqual(event["systemd_job_type"], "unknown")
        self.assertEqual(event["systemd_job_result"], "unknown")
        self.assertNotIn("systemd_job_id", event)

    def test_history_record_count_is_bounded_and_keeps_recent_monotonic_events(self):
        data = b"".join(self.journal_line(__MONOTONIC_TIMESTAMP=str(index * 1000)) for index in range(1, 65))
        events = diagnostics.classify_history(data, 1, 100)
        self.assertEqual(len(events), diagnostics.HISTORY_EVENTS)
        self.assertEqual([event["uptime_ms"] for event in events], list(range(49, 65)))

    def run_journal_child(self, code, budget=1):
        original = subprocess.Popen
        children, commands = [], []
        def launch(command, **kwargs):
            commands.append(command)
            child = original([sys.executable, "-I", "-c", code], **kwargs)
            children.append(child)
            return child
        started = time.monotonic()
        with mock.patch.object(diagnostics.subprocess, "Popen", side_effect=launch):
            result = diagnostics.journal_bytes(started + budget)
        self.assertLess(time.monotonic() - started, budget + 0.4)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)
        return result, commands[0]

    def test_journal_child_reads_only_fixed_fields_and_is_byte_bounded(self):
        (data, status), command = self.run_journal_child("import os; os.write(1, b'x' * 1000000)")
        self.assertEqual(status, "truncated")
        self.assertEqual(len(data), diagnostics.HISTORY_BYTES)
        fields = next(argument for argument in command if argument.startswith("--output-fields=")).split("=", 1)[1].split(",")
        self.assertEqual(set(fields), set(diagnostics.HISTORY_FIELDS))
        self.assertNotIn("MESSAGE", fields)
        self.assertNotIn("_CMDLINE", fields)
        self.assertIn("--boot=0", command)
        self.assertIn("--lines=64", command)
        self.assertEqual(command[-3:], ["+", "_UID=0", "_COMM=needrestart"])
        self.assertEqual(command[0], "/usr/bin/journalctl")

    def test_journal_child_timeout_reaps_process_and_keeps_only_complete_records(self):
        line = self.journal_line()
        (data, status), _ = self.run_journal_child(
            "import os,time; os.write(1," + repr(line + b'partial SECRET') + "); time.sleep(10)", budget=0.25)
        self.assertEqual(status, "timeout")
        self.assertEqual(len(diagnostics.classify_history(data, 100, 200)), 1)
        self.assertNotIn("SECRET", json.dumps(diagnostics.classify_history(data, 100, 200)))
        with mock.patch.object(diagnostics.subprocess, "Popen", side_effect=OSError("TOKEN")):
            self.assertEqual(diagnostics.journal_bytes(time.monotonic() + 0.2), (b"", "unavailable"))

    def test_journal_child_exit_between_poll_and_kill_does_not_lose_evidence(self):
        original = subprocess.Popen
        def launch(command, **kwargs):
            child = original([sys.executable, "-I", "-c", "pass"], **kwargs)
            wrapped = mock.Mock(wraps=child)
            wrapped.stdout = child.stdout
            wrapped.poll.return_value = None
            wrapped.kill.side_effect = ProcessLookupError("PRIVATE")
            return wrapped
        with mock.patch.object(diagnostics.subprocess, "Popen", side_effect=launch):
            self.assertEqual(diagnostics.journal_bytes(time.monotonic() + 1), (b"", "ok"))

    def test_stop_history_uses_shared_deadline_and_old_marker_skips_query(self):
        emit = mock.Mock()
        session = diagnostics.GuestDiagnostics(emit)
        session.enabled = True
        with mock.patch.object(diagnostics, "unit_states", return_value={}), \
                mock.patch.object(diagnostics, "oom_counters", return_value={}), \
                mock.patch.object(diagnostics.time, "monotonic", return_value=20), \
                mock.patch.object(diagnostics, "uptime_ms", return_value=200), \
                mock.patch.object(diagnostics, "journal_bytes", return_value=(self.journal_line(), "ok")) as journal:
            session.record("unit_stop")
            journal.assert_not_called()
            self.assertEqual(emit.call_args.kwargs["history_status"], "baseline_unavailable")
            session.registered_ms = 100
            session.record("unit_stop")
            journal.assert_called_once_with(22)
            self.assertEqual(emit.call_args_list[-2].kwargs["history_event_count"], 1)
            self.assertEqual(emit.call_args_list[-1].kwargs["reason"], "unit_history")
            session.record("runner_started")
            journal.assert_called_once()
        session.enabled = False
        with mock.patch.object(diagnostics, "journal_bytes") as journal:
            session.record("unit_stop")
            journal.assert_not_called()

    def test_optional_history_failure_cannot_suppress_original_exit_evidence(self):
        for failing in ("journal_bytes", "classify_history"):
            with self.subTest(failing=failing), ExitStack() as stack:
                emit = mock.Mock()
                session = diagnostics.GuestDiagnostics(emit)
                session.enabled, session.registered_ms = True, 100
                stack.enter_context(mock.patch.object(diagnostics, "unit_states", return_value={"docker_state": "active"}))
                stack.enter_context(mock.patch.object(diagnostics, "oom_counters", return_value={"oom_kill_count": 0}))
                if failing == "classify_history":
                    stack.enter_context(mock.patch.object(diagnostics, "journal_bytes", return_value=(b"", "ok")))
                stack.enter_context(mock.patch.object(diagnostics, failing, side_effect=RuntimeError("TOKEN")))
                session.record("unit_stop", signal=15, service_result="success")
                emit.assert_called_once()
                fields = emit.call_args.kwargs
                self.assertEqual(fields["signal"], 15)
                self.assertEqual(fields["docker_state"], "active")
                self.assertEqual(fields["oom_kill_count"], 0)
                self.assertEqual(fields["history_status"], "unavailable")
                self.assertEqual(fields["history_event_count"], 0)
                self.assertNotIn("TOKEN", json.dumps(fields))

    def test_stop_history_and_state_collection_share_real_two_second_budget(self):
        emit = mock.Mock()
        session = diagnostics.GuestDiagnostics(emit)
        session.enabled, session.registered_ms = True, 0
        original = subprocess.Popen
        children = []
        def launch(command, **kwargs):
            child = original([sys.executable, "-I", "-c", "import time; time.sleep(10)"], **kwargs)
            children.append(child)
            return child
        def states():
            time.sleep(0.2)
            return {}
        started = time.monotonic()
        with mock.patch.object(diagnostics, "unit_states", side_effect=states), \
                mock.patch.object(diagnostics, "oom_counters", return_value={}), \
                mock.patch.object(diagnostics.subprocess, "Popen", side_effect=launch):
            session.record("unit_stop", signal=15)
        self.assertLess(time.monotonic() - started, 2.4)
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(emit.call_args.kwargs["history_status"], "timeout")

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

"""Exit evidence must survive recycling without acquiring lifecycle authority."""
from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ops.self_hosted_runner import controller, template
from scripts.tests.test_local_runner_controller import FakeAPI, FakeClock, FakeHost


def diagnostic(**fields):
    return {"event": "guest_diagnostic", "schema_version": 1,
            "reason": "runner_exit", "exit_code": 0, **fields}


class RunnerDiagnosticTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.config = controller.Config(root=root, state=root / "state")
        self.clock, self.api = FakeClock(), FakeAPI()
        self.host = FakeHost(self.config)
        self.control = controller.Controller(self.config, self.api, self.host, self.clock)
        self.printed = mock.patch("builtins.print").start()
        self.addCleanup(mock.patch.stopall)

    def register(self):
        self.control.tick()
        instance = self.control.state["instance"]
        self.api.record = {"id": 42, "name": instance["name"], "busy": True,
                           "labels": [{"name": value} for value in
                                      ("self-hosted", "linux", "x64", self.config.label)]}
        self.control.tick()
        instance["job_id"] = 99
        self.api.job_record = {"id": 99, "runner_id": 42, "runner_name": instance["name"],
                               "status": "in_progress"}
        return instance

    def test_only_bounded_classifications_can_be_retained(self):
        valid = diagnostic(reason="unit_stop", service_result="oom-kill", signal=9,
                           oom_kill_delta=1, uptime_ms=12345)
        self.assertEqual(controller.guest_diagnostic(valid), valid)
        invalid = [None, [], {**valid, "Environment": "registration-secret"},
                   {**valid, "reason": "arbitrary command output"},
                   {**valid, "docker_state": ["active"]},
                   {**valid, "service_result": "remote exception"},
                   {**valid, "schema_version": True}, {**valid, "signal": True},
                   {**valid, "signal": 65}, {**valid, "exit_code": -256},
                   {**valid, "oom_kill_delta": -1}, {**valid, "uptime_ms": 2 ** 63}]
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(controller.guest_diagnostic(value))

    def test_legacy_exit_is_narrowly_accepted(self):
        self.assertEqual(controller.guest_diagnostic({"event": "runner_exit", "exit_code": -15}),
                         diagnostic(exit_code=-15))
        self.assertIsNone(controller.guest_diagnostic({"event": "runner_exit", "exit_code": 0,
                                                      "message": "registration-secret"}))

    def test_unit_history_accepts_only_fixed_metadata_and_rejects_contradictory_sources(self):
        valid = {"event": "guest_diagnostic", "schema_version": 1, "reason": "unit_history",
                 "history_source": "systemd", "source_unit": "bootstrap", "unit_transition": "stopping",
                 "systemd_job_type": "restart", "systemd_job_result": "unknown", "systemd_job_id": 123,
                 "uptime_ms": 150}
        self.assertEqual(controller.guest_diagnostic(valid), valid)
        invalid = [{**valid, "MESSAGE": "registration-secret"}, {**valid, "source_unit": "SECRET.service"},
                   {**valid, "unit_transition": "TOKEN"}, {**valid, "systemd_job_type": "kill PRIVATE"},
                   {**valid, "systemd_job_result": ["done"]}, {**valid, "systemd_job_id": True},
                   {**valid, "systemd_job_id": -1}, {**valid, "systemd_job_id": 2 ** 63},
                   {**valid, "source_unit": "unknown"}, {**valid, "unit_transition": "needrestart_observed"},
                   {key: value for key, value in valid.items() if key != "uptime_ms"}]
        for event in invalid:
            with self.subTest(event=event):
                self.assertIsNone(controller.guest_diagnostic(event))
        needrestart = {key: value for key, value in valid.items() if not key.startswith("systemd_job_")}
        needrestart.update(history_source="needrestart", source_unit="apt_upgrade",
                           unit_transition="needrestart_observed")
        self.assertEqual(controller.guest_diagnostic(needrestart), needrestart)
        self.assertIsNone(controller.guest_diagnostic({**needrestart, "systemd_job_type": "restart"}))
        self.assertIsNone(controller.guest_diagnostic({**needrestart, "unit_transition": "stopping"}))

    def test_history_query_status_and_count_are_bounded_with_old_events_compatible(self):
        valid = diagnostic(reason="unit_stop", history_status="truncated", history_event_count=16)
        self.assertEqual(controller.guest_diagnostic(valid), valid)
        self.assertEqual(controller.guest_diagnostic(diagnostic(reason="unit_stop")), diagnostic(reason="unit_stop"))
        for fields in ({"history_status": "secret exception"}, {"history_event_count": True},
                       {"history_event_count": -1}, {"history_event_count": 17}):
            self.assertIsNone(controller.guest_diagnostic({**valid, **fields}))

    def test_stopping_transaction_evidence_does_not_stop_an_authenticated_running_job(self):
        instance = self.register()
        events = [{"event": "guest_diagnostic", "schema_version": 1, "reason": "unit_history",
                   "history_source": "systemd", "source_unit": unit, "unit_transition": "stopping",
                   "systemd_job_type": "stop", "uptime_ms": uptime}
                  for unit, uptime in (("docker", 100), ("bootstrap", 150), ("shutdown", 160))]
        self.host.hints = events
        self.control.tick()
        self.assertEqual(instance["phase"], "running")
        self.assertFalse(self.host.stops or self.host.poweroffs or self.api.deleted)
        # A host batch has one observed_at; the original guest times stay distinct.
        self.assertEqual([entry["uptime_ms"] for entry in instance["diagnostics"]], [100, 150, 160])
        self.assertEqual(len({entry["observed_at"] for entry in instance["diagnostics"]}), 1)

    def test_success_exit_evidence_cannot_complete_an_authenticated_running_job(self):
        instance = self.register()
        self.host.hints = [diagnostic(reason="unit_stop", service_result="success")]
        self.clock.advance()
        self.control.tick()
        self.assertEqual(instance["phase"], "running")
        self.assertEqual(self.host.live, {instance["id"]})
        self.assertFalse(self.host.stops or self.host.poweroffs or self.api.deleted)
        self.assertEqual(instance["diagnostics"][0]["service_result"], "success")

    def test_final_events_survive_stopping_and_stopped_checks_before_cleanup(self):
        instance = self.register()
        self.control.pause("maintenance")
        self.api.job_record["status"] = "completed"
        self.clock.advance()
        self.control.tick()
        self.assertEqual(instance["phase"], "stopping")
        self.host.hints = [diagnostic(reason="bootstrap_signal", signal=15, uptime_ms=100)]
        self.control.tick()
        self.host.live.clear()
        self.host.hints = [diagnostic(reason="unit_stop", service_result="signal", signal=15,
                                      uptime_ms=120)]
        self.control.tick()
        self.assertEqual(instance["phase"], "cleanup")
        saved = []
        self.host.record_metrics = lambda value: saved.append(copy.deepcopy(value))
        self.control.tick()
        self.assertIsNone(self.control.state["instance"])
        self.assertEqual([event["uptime_ms"] for event in saved[0]["diagnostics"]], [100, 120])
        self.assertEqual(self.host.removed, [instance["id"]])

    def test_diagnostics_are_bounded_and_persist_across_controller_restart(self):
        instance = self.register()
        for index in range(80):
            self.host.hints = [diagnostic(uptime_ms=index)]
            self.control.tick()
        recovered = controller.Controller(self.config, self.api, self.host, self.clock)
        evidence = recovered.state["instance"]
        self.assertEqual(len(evidence["diagnostics"]), controller.MAX_GUEST_DIAGNOSTICS)
        self.assertEqual(evidence["diagnostics_dropped"], 16)
        self.assertEqual(evidence["diagnostics"][0]["uptime_ms"], 16)
        self.assertEqual(evidence["diagnostics"][-1]["uptime_ms"], 79)
        self.assertEqual(evidence["id"], instance["id"])

    def test_untrusted_extra_fields_never_reach_journal_or_log(self):
        instance = self.register()
        self.host.hints = [diagnostic(Environment="registration-secret")]
        self.control.tick()
        self.assertNotIn("diagnostics", instance)
        self.assertNotIn("registration-secret", self.config.journal.read_text())
        self.assertNotIn("registration-secret", str(self.printed.call_args_list))

    def test_unavailable_diagnostics_do_not_weaken_host_deadline(self):
        instance = self.register()
        self.host.events = mock.Mock(side_effect=OSError("channel unavailable"))
        self.clock.advance(self.config.job_timeout_seconds)
        self.control.tick()
        self.assertEqual(instance["reason"], "host_job_deadline")
        self.assertEqual(instance["phase"], "stopping")

    def test_resource_summary_retains_evidence_as_private_json(self):
        instance = self.register()
        instance["diagnostics"] = [{"observed_at": self.clock(), **diagnostic(oom_kill_delta=1)}]
        instance["diagnostics_dropped"] = 2
        controller.Host(self.config).record_metrics(instance)
        path = self.config.state / "logs" / f"instance-{instance['id']}.json"
        saved = json.loads(path.read_text())
        self.assertEqual(saved["diagnostics"], instance["diagnostics"])
        self.assertEqual(saved["diagnostics_dropped"], 2)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_cloud_seed_carries_diagnostic_helper_and_stop_hook(self):
        with tempfile.TemporaryDirectory() as directory:
            user, _ = template.render_seed(template.read_lock(template.DEFAULT_LOCK), Path(directory))
            files = {item["path"]: item for item in json.loads(user.read_text().split("\n", 1)[1])["write_files"]}
            helper = files["/usr/local/lib/nexpoly-ci-runner/exit_diagnostics.py"]
            self.assertEqual(base64.b64decode(helper["content"]),
                             (template.PACKAGE / "guest/exit_diagnostics.py").read_bytes())
            self.assertIn("ExecStopPost=-/usr/bin/python3 /usr/local/lib/nexpoly-ci-runner/exit_diagnostics.py",
                          files["/etc/systemd/system/nexpoly-ci-bootstrap.service"]["content"])


if __name__ == "__main__":
    unittest.main()

"""Exercise idle retirement with the real lifecycle and fake API/VM only."""
from __future__ import annotations

import copy
import importlib.util
import json
import stat
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ops.self_hosted_runner import controller
from scripts.tests.test_local_runner_controller import FakeAPI, FakeClock, FakeHost

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("nexpoly_retire_idle", ROOT / "ops/self_hosted_runner/retire-idle.py")
retirement = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retirement)


class RouteAPI(FakeAPI):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.requests = []
        self.runs = []
        self.jobs = {}
        self.scan_error = False
        self.restore_error = False
        self.after_unroute = None
        self.check_durable = None

    def request(self, method, path, payload=None):
        self.requests.append((method, path, payload, self.clock()))
        if method == "DELETE" and "/labels/" in path:
            if self.check_durable:
                self.check_durable()
            self.record["labels"] = [label for label in self.record["labels"] if label["name"] != "nexpoly-ci-ephemeral"]
            if self.after_unroute:
                self.after_unroute()
            return {"labels": self.record["labels"]}
        if method == "POST" and path.endswith("/labels"):
            if self.restore_error:
                raise controller.APIError(503)
            self.record["labels"].append({"name": "nexpoly-ci-ephemeral"})
            return {"labels": self.record["labels"]}
        if method == "GET" and path.startswith("/actions/runs?"):
            if self.scan_error:
                raise controller.APIError(503)
            return {"workflow_runs": copy.deepcopy(self.runs)}
        if method == "GET" and path.startswith("/actions/runs/"):
            run_id = int(path.split("/")[3])
            return {"jobs": copy.deepcopy(self.jobs.get(run_id, []))}
        raise AssertionError((method, path, payload))


class RetirementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = controller.Config(root=self.root, state=self.root / "state")
        self.clock = FakeClock()
        self.api = RouteAPI(self.clock)
        self.host = FakeHost(self.config)
        self.metrics = []
        self.host.record_metrics = lambda instance: self.metrics.append(copy.deepcopy(instance))
        self.ctl = controller.Controller(self.config, self.api, self.host, self.clock)
        quiet = patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)
        self.ctl.begin()
        self.instance = self.ctl.state["instance"]
        self.identity = self.instance["id"]
        self.instance.update(registered=True, runner_id=8, phase="idle", bootstrap_attempted=True)
        self.host.hints.clear()
        self.api.record = {"id": 8, "name": self.instance["name"], "busy": False,
                           "labels": [{"name": label} for label in ("self-hosted", "linux", "x64", self.config.label)]}
        self.ctl.pause("administrator_drain")
        self.ctl.save()

    def helper(self):
        return retirement.IdleRetirement(self.ctl, now=self.clock, monotonic=self.clock,
                                          sleep=lambda seconds: self.clock.advance(seconds))

    def actual_job(self, status="in_progress"):
        return {"id": 77, "status": status, "runner_id": 8, "runner_name": self.instance["name"]}

    def test_unused_standby_is_unrouted_before_two_spaced_observations_and_normal_cleanup(self):
        def check():
            disk_state = json.loads(self.config.journal.read_text())
            self.assertEqual(disk_state["instance"]["maintenance_quarantine"]["runner_id"], 8)
        self.api.check_durable = check
        self.assertEqual(self.helper().run(), 0)
        self.assertEqual(self.api.deleted, [8])
        scans = [row for row in self.api.requests if row[0] == "GET" and row[1].startswith("/actions/runs?")]
        self.assertEqual(len(scans), 3)
        self.assertGreaterEqual(scans[1][3] - scans[0][3], 30)
        self.assertEqual(self.api.requests[0][:2], ("DELETE", "/actions/runners/8/labels/nexpoly-ci-ephemeral"))
        self.assertEqual(self.host.poweroffs, [self.identity])
        self.assertEqual(self.host.stops, [self.identity])
        self.assertEqual(self.host.removed, [self.identity])
        self.assertIsNone(self.ctl.state["instance"])
        self.assertEqual(self.ctl.state["last_instance"]["reason"], "maintenance_idle_retired")
        self.assertEqual(len(self.metrics), 1)
        self.assertTrue(self.ctl.paused())
        self.assertEqual(len(self.host.created), 1)

    def test_job_dispatched_during_label_removal_is_retained_and_route_restored(self):
        self.api.after_unroute = lambda: self.api.record.update(busy=True)
        self.assertEqual(self.helper().run(), retirement.RETAIN_JOB)
        self.assertIn(self.config.label, {item["name"] for item in self.api.record["labels"]})
        self.assertEqual(self.api.deleted, [])
        self.assertEqual(self.host.poweroffs, [])
        self.assertEqual(self.host.removed, [])
        self.assertIn(self.identity, self.host.live)

    def test_queued_workflow_actual_job_cannot_be_missed_when_runner_busy_is_false(self):
        self.api.runs = [{"id": 7, "status": "queued"}]
        self.api.jobs[7] = [self.actual_job()]
        self.assertEqual(self.helper().run(), retirement.RETAIN_JOB)
        self.assertEqual(self.ctl.state["instance"]["job_id"], 77)
        self.assertEqual(self.ctl.state["instance"]["phase"], "running")
        self.assertEqual(self.api.deleted, [])
        self.assertEqual(self.host.poweroffs, [])

    def test_completed_run_updated_during_vm_lifetime_is_scanned(self):
        self.api.runs = [{"id": 9, "status": "completed", "updated_at": "1970-01-01T00:16:41Z"}]
        self.api.jobs[9] = [self.actual_job("completed")]
        self.assertEqual(self.helper().run(), retirement.RETAIN_JOB)
        self.assertEqual(self.ctl.state["instance"]["reason"], "github_job_completed")
        self.assertEqual(self.api.deleted, [])

    def test_second_precision_updated_at_equal_to_creation_is_not_skipped(self):
        # API timestamps have second precision, while local creation uses floats.
        self.instance["created_at"] = 1000.875
        self.api.runs = [{"id": 9, "status": "completed", "updated_at": "1970-01-01T00:16:40Z"}]
        self.api.jobs[9] = [self.actual_job("completed")]
        self.assertEqual(self.helper().run(), retirement.RETAIN_JOB)
        self.assertEqual(self.ctl.state["instance"]["job_id"], 77)
        self.assertEqual(self.api.deleted, [])

    def test_only_completed_runs_strictly_before_creation_second_are_skipped(self):
        self.instance["created_at"] = 1000.875
        self.api.runs = [{"id": 9, "status": "completed", "updated_at": "1970-01-01T00:16:39Z"}]
        self.api.jobs[9] = [self.actual_job("completed")]
        self.assertEqual(self.helper().run(), 0)
        self.assertFalse(any("/actions/runs/9/jobs" in request[1] for request in self.api.requests))

    def test_post_delete_404_retry_keeps_prior_complete_proof_during_api_outage(self):
        previous = [self.clock() - 30, self.clock()]
        self.instance["maintenance_quarantine"] = {
            "runner_id": 8, "attempted_at": self.clock(), "delete_attempted_at": self.clock(),
            "deleted_at": self.clock(), "clear_observations": previous.copy()}
        self.api.record = None
        self.api.scan_error = True
        with self.assertRaises(retirement.RetirementError):
            self.helper().run()
        self.assertEqual(self.instance["maintenance_quarantine"]["clear_observations"], previous)
        self.assertEqual(self.host.poweroffs, [])
        self.api.scan_error = False
        before = len(self.api.requests)
        self.assertEqual(self.helper().run(), 0)
        self.assertGreater(len(self.api.requests), before)
        self.assertEqual(self.host.removed, [self.identity])

    def test_post_delete_404_retry_still_retains_vm_with_actual_assigned_job(self):
        self.instance["maintenance_quarantine"] = {
            "runner_id": 8, "attempted_at": self.clock(), "delete_attempted_at": self.clock(),
            "clear_observations": [self.clock() - 30, self.clock()]}
        self.api.record = None
        self.api.runs = [{"id": 7, "status": "queued"}]
        self.api.jobs[7] = [self.actual_job()]
        with self.assertRaises(retirement.RetirementError):
            self.helper().run()
        self.assertEqual(self.host.stops, [])
        self.assertEqual(self.host.poweroffs, [])
        self.assertEqual(self.api.deleted, [])

    def test_installed_import_directories_must_be_root_owned_unwritable_real_directories(self):
        directories = {Path("/usr"), Path("/usr/local"), Path("/usr/local/libexec"), retirement.INSTALLED}
        for unsafe_mode, unsafe_uid in ((stat.S_IFDIR | 0o775, 0), (stat.S_IFDIR | 0o755, 1000),
                                        (stat.S_IFLNK | 0o777, 0)):
            for unsafe_path in directories:
                with self.subTest(path=unsafe_path, mode=unsafe_mode, uid=unsafe_uid):
                    def metadata(path):
                        if path == unsafe_path:
                            return SimpleNamespace(st_mode=unsafe_mode, st_uid=unsafe_uid)
                        return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755 if path in directories else stat.S_IFREG | 0o644,
                                               st_uid=0)
                    with patch.object(Path, "lstat", autospec=True, side_effect=metadata), \
                            patch.object(retirement.importlib, "import_module") as importer:
                        with self.assertRaisesRegex(retirement.RetirementError, "import directories"):
                            retirement.installed_modules()
                        importer.assert_not_called()

    def test_api_uncertainty_restores_route_and_never_stops_or_deletes_vm(self):
        self.api.scan_error = True
        self.assertEqual(self.helper().run(), retirement.RETAIN_UNCERTAIN)
        self.assertIn(self.config.label, {item["name"] for item in self.api.record["labels"]})
        self.assertEqual(self.api.deleted, [])
        self.assertEqual(self.host.poweroffs, [])
        self.assertEqual(self.host.removed, [])

    def test_unconfirmed_restore_requires_stopped_controller_and_retains_quarantine_state(self):
        self.api.scan_error = True
        self.api.restore_error = True
        with self.assertRaisesRegex(retirement.RetirementError, "controller must remain stopped"):
            self.helper().run()
        self.assertTrue(self.ctl.state["instance"]["maintenance_quarantine"]["attempted_at"])
        self.assertEqual(self.api.deleted, [])
        self.assertEqual(self.host.stops, [])

    def test_runner_404_is_never_idle_proof(self):
        self.api.record = None
        with self.assertRaises(retirement.RetirementError):
            self.helper().run()
        self.assertEqual(self.api.deleted, [])
        self.assertEqual(self.host.poweroffs, [])

    def test_identity_mismatch_foreign_vm_or_pending_resume_blocks_api_mutation(self):
        mutations = (
            lambda: self.instance.update(name="foreign"),
            lambda: self.host.live.add("b" * 32),
            lambda: (self.config.controller_dir / "resume.json").write_text("{}"),
            lambda: self.api.record.update(name="foreign"),
        )
        for change in mutations:
            with self.subTest(change=change):
                state = copy.deepcopy(self.instance)
                record = copy.deepcopy(self.api.record)
                change()
                with self.assertRaises(retirement.RetirementError):
                    self.helper().run()
                self.instance.clear()
                self.instance.update(state)
                self.api.record = record
                self.host.live = {self.identity}
                (self.config.controller_dir / "resume.json").unlink(missing_ok=True)
                self.assertEqual(self.api.requests, [])

    def test_existing_real_job_evidence_is_retained_without_quarantine(self):
        self.instance["job_started_at"] = self.clock()
        self.assertEqual(self.helper().run(), retirement.RETAIN_JOB)
        self.assertEqual(self.api.requests, [])
        self.assertEqual(self.host.stops, [])

    def test_stopped_vm_uses_existing_cleanup_without_idle_api_quarantine(self):
        self.host.live.clear()
        self.assertEqual(self.helper().run(), 0)
        self.assertIsNone(self.ctl.state["instance"])
        self.assertEqual(self.host.removed, [self.identity])
        self.assertEqual(self.host.poweroffs, [])
        self.assertEqual(self.api.requests, [])

    def test_pagination_limit_is_uncertain_and_not_idle_proof(self):
        self.api.runs = [{"id": i, "status": "in_progress"} for i in range(100)]
        self.assertEqual(self.helper().run(), retirement.RETAIN_UNCERTAIN)
        self.assertEqual(self.api.deleted, [])
        self.assertEqual(self.host.poweroffs, [])


if __name__ == "__main__":
    unittest.main()

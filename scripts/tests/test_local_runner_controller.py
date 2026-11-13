"""Offline lifecycle tests using the real controller and durable JSON journal."""
from __future__ import annotations

import json
import io
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from ops.self_hosted_runner import controller


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds=30):
        self.now += seconds


class FakeHost:
    """No commands, sockets, VM processes, host mounts or privileged paths."""
    def __init__(self, config):
        self.config = config
        self.live = set()
        self.created = []
        self.removed = []
        self.stops = []
        self.poweroffs = []
        self.bootstraps = []
        self.deadlines = []
        self.channel_ready = True
        self.bootstrap_error = None
        self.remove_error = None
        self.hints = []

    def preflight(self):
        return {"sha256": "a" * 64}

    def active_instances(self):
        return set(self.live)

    def arm_deadline(self, instance_id, deadline):
        self.deadlines.append((instance_id, deadline))

    def create(self, instance_id, manifest):
        if self.live:
            raise AssertionError("A second VM must never be created")
        path = self.config.instance(instance_id)
        path.mkdir(parents=True, exist_ok=False)
        (path / "root.qcow2").write_bytes(b"fresh-overlay")
        self.created.append(instance_id)

    def start(self, instance_id):
        self.live.add(instance_id)
        self.hints.append({"event": "ready"})

    def stopped(self, instance_id):
        return instance_id not in self.live

    def stop(self, instance_id):
        self.stops.append(instance_id)
        self.live.discard(instance_id)

    def poweroff(self, instance_id):
        self.poweroffs.append(instance_id)

    def connect(self, instance_id, channel):
        return object() if self.channel_ready else None

    def bootstrap(self, instance_id, payload):
        self.bootstraps.append((instance_id, payload))
        if self.bootstrap_error:
            raise self.bootstrap_error
        return True

    def events(self, instance_id):
        hints, self.hints = self.hints, []
        return hints

    def remove(self, instance_id):
        if not self.stopped(instance_id):
            raise AssertionError("Cannot delete a running VM overlay")
        if self.remove_error:
            raise self.remove_error
        path = self.config.instance(instance_id)
        if path.exists():
            shutil.rmtree(path)
        self.removed.append(instance_id)


class FakeAPI:
    def __init__(self):
        self.record = None
        self.job_record = None
        self.runner_error = None
        self.registration_error = None
        self.delete_error = None
        self.token_calls = 0
        self.registration_calls = 0
        self.runner_calls = 0
        self.deleted = []

    def token(self):
        self.token_calls += 1
        return "installation-secret"

    def registration_token(self):
        self.registration_calls += 1
        if self.registration_error:
            raise self.registration_error
        return "registration-secret"

    def find_runner(self, name):
        self.runner_calls += 1
        if self.runner_error:
            raise self.runner_error
        return self.record

    def runner(self, runner_id):
        return self.find_runner("")

    def delete_runner(self, runner_id):
        if self.delete_error:
            raise self.delete_error
        self.deleted.append(runner_id)
        self.record = None

    def job(self, job_id):
        return self.job_record

    def find_job(self, run_id, runner_id):
        return self.job_record

    def discover_job(self, runner_id):
        return self.job_record


class LocalRunnerControllerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.config = controller.Config(root=root, state=root / "state")
        self.clock = FakeClock()
        self.api = FakeAPI()
        self.host = FakeHost(self.config)
        self.controller = self.new_controller()
        # Lifecycle logs contain random UUIDs; assertions inspect durable state.
        output = mock.patch("builtins.print")
        output.start()
        self.addCleanup(output.stop)

    def new_controller(self):
        return controller.Controller(self.config, self.api, self.host, self.clock)

    def begin(self):
        self.controller.tick()
        return self.controller.state["instance"]

    def register(self, *, busy=False):
        instance = self.begin()
        self.api.record = {
            "id": 42, "name": instance["name"], "busy": busy,
            "labels": [{"name": label} for label in
                       ("self-hosted", "linux", "x64", self.config.label)],
        }
        self.controller.tick()
        self.assertTrue(instance["registered"])
        return instance

    def complete_job(self, instance):
        instance["job_id"] = 99
        self.api.job_record = {
            "id": 99, "runner_id": 42, "runner_name": instance["name"],
            "status": "completed",
        }
        self.clock.advance()
        self.controller.tick()

    def assert_vm_retained(self, instance):
        self.assertNotIn(instance["phase"], ("stopping", "cleanup"))
        self.assertEqual(self.host.live, {instance["id"]})
        self.assertEqual(self.host.stops, [])
        self.assertEqual(self.host.poweroffs, [])
        self.assertEqual(self.host.removed, [])
        self.assertEqual(self.controller.state["instance"]["id"], instance["id"])

    def test_api_outage_retains_running_vm_and_backs_off(self):
        instance = self.register(busy=True)
        for status in (401, 429, 503):
            with self.subTest(status=status):
                self.api.runner_error = controller.APIError(status, retry_after=90)
                self.clock.advance(90)
                self.controller.tick()
                calls = self.api.runner_calls
                self.controller.tick()
                self.assertEqual(self.api.runner_calls, calls)
                self.assertEqual(self.controller.state["api_error"]["status"], status)
                self.assert_vm_retained(instance)

    def test_runner_404_absence_does_not_imply_completion(self):
        instance = self.register(busy=True)
        self.api.record = None  # GitHub.runner translates authenticated 404 to None.
        self.clock.advance()
        self.controller.tick()
        self.assert_vm_retained(instance)

    def test_busy_false_does_not_imply_completion(self):
        instance = self.register(busy=True)
        self.api.record["busy"] = False
        self.clock.advance()
        self.controller.tick()
        self.assertEqual(instance["phase"], "running")
        self.assert_vm_retained(instance)

    def test_untrusted_completed_hint_requires_authenticated_job_evidence(self):
        instance = self.register(busy=True)
        self.host.hints = [{"event": "completed", "run_id": 1234}]
        self.clock.advance()
        self.controller.tick()
        self.assertEqual(instance["run_hint"], 1234)
        self.assert_vm_retained(instance)

    def test_completed_job_for_other_runner_is_rejected(self):
        instance = self.register(busy=True)
        instance["job_id"] = 99
        self.api.job_record = {"id": 99, "runner_id": 43,
                               "runner_name": instance["name"], "status": "completed"}
        self.clock.advance()
        with self.assertRaisesRegex(controller.RunnerError, "does not belong"):
            self.controller.tick()
        self.assert_vm_retained(instance)

    def test_verified_completion_grace_then_stop_before_remove(self):
        instance = self.register(busy=True)
        self.complete_job(instance)
        self.assertEqual(instance["phase"], "stopping")
        self.assertEqual(self.host.poweroffs, [instance["id"]])
        self.controller.tick()
        self.assertEqual(self.host.stops, [])
        self.clock.advance(self.config.shutdown_timeout_seconds)
        self.controller.tick()
        self.assertEqual(self.host.stops, [instance["id"]])
        self.assertEqual(self.host.removed, [])
        self.controller.tick()
        self.assertEqual(self.host.removed, [instance["id"]])
        self.assertIsNone(self.controller.state["instance"])

    def test_vm_exit_cleanup_api_retry_blocks_replenishment(self):
        instance = self.register(busy=True)
        self.host.live.clear()
        self.api.delete_error = controller.APIError(503, 90)
        self.controller.tick()
        self.assertEqual(instance["phase"], "cleanup")
        self.controller.tick()
        self.controller.tick()
        self.assertEqual(self.host.created, [instance["id"]])
        self.assertTrue(self.config.instance(instance["id"]).exists())
        self.assertEqual(self.host.removed, [])
        self.api.delete_error = None
        self.clock.advance(90)
        self.controller.tick()
        self.assertEqual(self.api.deleted, [42])
        self.assertEqual(self.host.removed, [instance["id"]])
        self.assertIsNone(self.controller.state["instance"])
        self.clock.advance(10)
        self.controller.tick()
        self.assertEqual(len(self.host.created), 2)
        self.assertNotEqual(self.host.created[0], self.host.created[1])
        self.assertFalse(self.config.instance(instance["id"]).exists())

    def test_cleanup_remove_failure_retains_journal_and_blocks_replenishment(self):
        instance = self.register()
        self.host.live.clear()
        self.controller.tick()
        self.host.remove_error = controller.RunnerError("temporary remove failure")
        for _ in range(2):
            with self.assertRaisesRegex(controller.RunnerError, "temporary remove"):
                self.controller.tick()
        self.assertEqual(self.host.created, [instance["id"]])
        self.assertEqual(self.controller.state["instance"]["phase"], "cleanup")
        self.host.remove_error = None
        self.controller.tick()
        self.assertIsNone(self.controller.state["instance"])

    def test_cleanup_refuses_live_cgroup(self):
        instance = self.register()
        instance["phase"] = "cleanup"
        with self.assertRaisesRegex(controller.RunnerError, "stopped VM cgroup"):
            self.controller.tick()
        self.assertEqual(self.host.live, {instance["id"]})
        self.assertEqual(self.host.removed, [])

    def test_three_consecutive_boot_failures_pause_without_fourth_vm(self):
        for count in range(1, 4):
            instance = self.begin()
            self.host.live.clear()
            self.controller.tick()
            self.controller.tick()
            self.assertEqual(self.controller.state["failures"], count)
            self.assertIsNone(self.controller.state["instance"])
            self.assertFalse(self.config.instance(instance["id"]).exists())
            self.clock.advance(300)
        self.controller.tick()
        self.assertTrue(self.controller.paused())
        self.assertEqual(len(self.host.created), 3)

    def test_restart_between_failure_journal_and_pause_cannot_launch_fourth_vm(self):
        controller.atomic_json(self.config.journal, {
            "schema_version": 1, "failures": 3, "instance": None,
        })
        recovered = self.new_controller()
        recovered.recovery()
        recovered.tick()
        self.assertTrue(recovered.paused())
        self.assertEqual(self.host.created, [])

    def test_recovery_preserves_running_vm_without_second_bootstrap(self):
        instance = self.register(busy=True)
        self.assertEqual(len(self.host.bootstraps), 1)
        recovered = self.new_controller()
        recovered.recovery()
        recovered.tick()
        self.assertEqual(len(self.host.bootstraps), 1)
        self.assertEqual(self.api.registration_calls, 1)
        self.assertEqual(recovered.state["instance"]["id"], instance["id"])
        self.assert_vm_retained(instance)

    def test_interrupted_bootstrap_is_durable_and_never_retransmitted(self):
        instance = self.begin()
        self.host.bootstrap_error = controller.RunnerError("interrupted send")
        with self.assertRaisesRegex(controller.RunnerError, "interrupted send"):
            self.controller.tick()
        self.assertTrue(controller.read_json(self.config.journal)["instance"]["bootstrap_attempted"])
        recovered = self.new_controller()
        recovered.recovery()
        recovered.tick()
        self.assertEqual(len(self.host.bootstraps), 1)
        self.assertEqual(self.api.registration_calls, 1)
        self.clock.advance(self.config.boot_timeout_seconds)
        recovered.tick()
        self.assertEqual(recovered.state["instance"]["reason"], "startup_timeout")
        self.assertEqual(self.host.created, [instance["id"]])

    def test_recovery_rejects_unjournaled_overlay(self):
        orphan = self.config.instance("b" * 32)
        orphan.mkdir(parents=True)
        with self.assertRaisesRegex(controller.RunnerError, "Unjournaled"):
            self.controller.recovery()
        self.assertEqual(self.host.created, [])

    def test_recovery_rejects_unjournaled_live_service(self):
        self.host.live.add("b" * 32)
        with self.assertRaisesRegex(controller.RunnerError, "Unjournaled VM service"):
            self.controller.recovery()
        self.assertEqual(self.host.created, [])

    def test_repeated_busy_and_restart_preserve_original_deadline(self):
        instance = self.register(busy=True)
        expected = (instance["id"], self.clock() + self.config.job_timeout_seconds)
        self.assertEqual(self.host.deadlines, [expected])
        self.clock.advance(120)
        self.controller.tick()
        recovered = self.new_controller()
        recovered.recovery()
        self.assertGreaterEqual(len(self.host.deadlines), 3)
        self.assertTrue(all(deadline == expected for deadline in self.host.deadlines))

    def test_guest_start_hint_arms_deadline_without_claiming_completion(self):
        instance = self.register()
        self.assertEqual(self.host.deadlines, [])
        self.host.hints = [{"event": "started", "run_id": 1234}]
        self.clock.advance()
        self.controller.tick()
        expected = (instance["id"], self.clock() + self.config.job_timeout_seconds)
        self.assertTrue(self.host.deadlines)
        self.assertTrue(all(deadline == expected for deadline in self.host.deadlines))
        self.assert_vm_retained(instance)

    def test_timer_failure_never_erases_durable_job_start_time(self):
        self.register()
        self.api.record["busy"] = True
        self.clock.advance()
        with mock.patch.object(self.host, "arm_deadline", side_effect=controller.RunnerError("timer unavailable")):
            with self.assertRaisesRegex(controller.RunnerError, "timer unavailable"):
                self.controller.tick()
        recorded = controller.read_json(self.config.journal)["instance"]["job_started_at"]
        self.assertEqual(recorded, self.clock())
        self.clock.advance(30)
        recovered = self.new_controller()
        recovered.recovery()
        self.assertEqual(self.host.deadlines[-1][1], recorded + self.config.job_timeout_seconds)

    def test_host_create_never_reuses_existing_overlay(self):
        instance_id = "b" * 32
        path = self.config.instance(instance_id)
        path.mkdir(parents=True)
        overlay = path / "root.qcow2"
        overlay.write_bytes(b"previous-job-private-data")
        real_host = controller.Host(self.config)
        with self.assertRaises(controller.RunnerError):
            real_host.create(instance_id, {"sha256": "a" * 64})
        self.assertEqual(overlay.read_bytes(), b"previous-job-private-data")

    def test_registration_token_requested_only_when_channel_ready(self):
        self.host.channel_ready = False
        self.begin()
        self.controller.tick()
        self.clock.advance()
        self.controller.tick()
        self.assertEqual(self.api.registration_calls, 0)
        self.host.channel_ready = True
        self.clock.advance()
        self.controller.tick()
        self.assertEqual(self.api.registration_calls, 1)
        self.assertEqual(len(self.host.bootstraps), 1)

    def test_registration_api_outage_cannot_postpone_boot_deadline(self):
        instance = self.begin()
        self.api.registration_error = controller.APIError(503, 900)
        self.controller.tick()
        self.controller.tick()
        self.assertEqual(self.api.registration_calls, 1)
        self.clock.advance(self.config.boot_timeout_seconds)
        self.controller.tick()
        self.assertEqual(instance["reason"], "startup_timeout")
        self.assertEqual(self.api.registration_calls, 1)

    def test_api_backoff_cannot_postpone_running_job_deadline(self):
        instance = self.register(busy=True)
        self.api.runner_error = controller.APIError(503, 900)
        self.clock.advance(self.config.job_timeout_seconds)
        calls = self.api.runner_calls
        self.controller.tick()
        self.assertEqual(instance["reason"], "host_job_deadline")
        self.assertEqual(self.api.runner_calls, calls)

    def test_drain_then_explicit_resume_rechecks_health_and_resets_failures(self):
        self.controller.state["failures"] = 3
        self.controller.save()
        self.controller.pause("administrator_drain")
        self.controller.tick()
        self.assertEqual(self.host.created, [])
        controller.atomic_json(self.config.controller_dir / "resume.json", {"at": self.clock()})
        self.controller.tick()
        self.assertFalse(self.controller.paused())
        self.assertEqual(self.controller.state["failures"], 0)
        self.assertEqual(self.api.token_calls, 1)
        self.assertEqual(len(self.host.created), 1)

    def test_drain_before_bootstrap_does_not_register_runner(self):
        instance = self.begin()
        self.controller.pause("administrator_drain")
        self.controller.tick()
        self.assertEqual(instance["reason"], "drained_before_registration")
        self.assertEqual(self.api.registration_calls, 0)

    def test_drain_registered_idle_runner_preserves_registration_and_vm(self):
        instance = self.register()
        self.controller.pause("administrator_drain")
        self.clock.advance()
        self.controller.tick()
        self.assertEqual(self.api.deleted, [])
        self.assertEqual(instance["phase"], "idle")
        self.assert_vm_retained(instance)

    def test_drain_preserves_vm_when_assignment_follows_idle_poll(self):
        instance = self.register()
        self.controller.pause("administrator_drain")
        self.clock.advance()
        self.controller.tick()
        self.api.record["busy"] = True
        self.clock.advance()
        self.controller.tick()
        self.assert_vm_retained(instance)
        self.assertEqual(self.api.deleted, [])
        self.assertEqual(instance["phase"], "running")

    def test_drain_preserves_active_job_even_when_busy_later_false(self):
        instance = self.register(busy=True)
        self.controller.pause("administrator_drain")
        self.api.record["busy"] = False
        self.clock.advance()
        self.controller.tick()
        self.assert_vm_retained(instance)
        self.assertEqual(self.api.deleted, [])
        self.complete_job(instance)
        self.assertEqual(instance["reason"], "github_job_completed")

    def test_journal_contains_no_app_or_registration_credentials(self):
        self.register()
        encoded = self.config.journal.read_text()
        self.assertNotIn("installation-secret", encoded)
        self.assertNotIn("registration-secret", encoded)
        self.assertTrue(json.loads(encoded)["instance"]["bootstrap_attempted"])


class GitHubTransportTests(unittest.TestCase):
    def setUp(self):
        self.api = controller.GitHub(controller.Config())

    def test_runner_404_is_absence_but_other_errors_propagate(self):
        with mock.patch.object(self.api, "request", side_effect=controller.APIError(404)):
            self.assertIsNone(self.api.runner(42))
        with mock.patch.object(self.api, "request", side_effect=controller.APIError(503)):
            with self.assertRaises(controller.APIError):
                self.api.runner(42)

    def test_delete_404_is_idempotent_but_other_errors_propagate(self):
        with mock.patch.object(self.api, "request", side_effect=controller.APIError(404)):
            self.api.delete_runner(42)
        with mock.patch.object(self.api, "request", side_effect=controller.APIError(503)):
            with self.assertRaises(controller.APIError):
                self.api.delete_runner(42)

    def test_redirect_never_forwards_bearer_to_other_endpoint(self):
        import github_api
        with self.assertRaises(controller.APIError):
            github_api.NoRedirect().redirect_request(None, None, 302, "", {}, "https://attacker.invalid")

    def test_non_object_api_response_is_retryable_error(self):
        for value in (None, [], "unexpected", 42):
            with self.subTest(value=value):
                response = mock.MagicMock()
                response.__enter__.return_value.read.return_value = json.dumps(value).encode()
                with mock.patch.object(self.api.opener, "open", return_value=response):
                    with self.assertRaises(controller.APIError):
                        self.api._request("GET", self.api.prefix + "/actions/runners", "secret")

    def test_runner_list_missing_or_invalid_is_error_not_absence(self):
        for result in ({}, {"runners": None}, {"runners": [{}]}, {"runners": ["invalid"]}):
            with self.subTest(result=result), mock.patch.object(self.api, "request", return_value=result):
                with self.assertRaises(controller.APIError):
                    self.api.find_runner("expected")

    def test_runner_record_with_malformed_nested_labels_is_retryable_error(self):
        for labels in ([None], [{"name": 4}], ["linux"]):
            record = {"id": 42, "name": "expected", "busy": False, "labels": labels}
            with self.subTest(labels=labels), mock.patch.object(self.api, "request", return_value=record):
                with self.assertRaises(controller.APIError):
                    self.api.runner(42)

    def test_runner_list_match_must_have_valid_labels_and_busy(self):
        for record in ({"id": 42, "name": "expected"},
                       {"id": 42, "name": "expected", "labels": [None], "busy": False},
                       {"id": 42, "name": "expected", "labels": [], "busy": "false"}):
            with self.subTest(record=record), mock.patch.object(self.api, "request", return_value={"runners": [record]}):
                with self.assertRaises(controller.APIError):
                    self.api.find_runner("expected")

    def test_job_list_missing_or_invalid_is_error_not_empty(self):
        for result in ({}, {"jobs": None}, {"jobs": [{}]}, {"jobs": ["invalid"]}):
            with self.subTest(result=result), mock.patch.object(self.api, "request", return_value=result):
                with self.assertRaises(controller.APIError):
                    self.api.find_job(1234, 42)

    def test_invalid_token_expiry_does_not_leak_token_or_raw_error(self):
        with mock.patch.object(self.api, "jwt", return_value="jwt-secret"), \
                mock.patch.object(self.api, "_request", return_value={"token": "secret", "expires_at": 4}):
            with self.assertRaises(controller.APIError) as failure:
                self.api.token()
        self.assertEqual(self.api._token, "")
        self.assertNotIn("secret", str(failure.exception))

    def test_401_invalidates_cached_installation_token(self):
        self.api._expires = 10 ** 12
        self.api._token = "secret"
        with mock.patch.object(self.api, "_request", side_effect=controller.APIError(401)):
            with self.assertRaises(controller.APIError):
                self.api.request("GET", "/actions/runners")
        self.assertEqual(self.api._expires, 0)

    def test_unexpected_endpoint_is_rejected_before_network(self):
        with mock.patch.object(self.api.opener, "open") as network:
            with self.assertRaises(controller.RunnerError):
                self.api._request("GET", "/repos/other/repository/actions/runners", "secret")
        network.assert_not_called()

    def test_poll_budget_exhaustion_prevents_another_network_request(self):
        import github_api
        with mock.patch.object(github_api.time, "monotonic", side_effect=[100, 121]), \
                mock.patch.object(self.api.opener, "open") as network:
            with self.api.budget():
                with self.assertRaises(controller.APIError):
                    self.api._request("GET", self.api.prefix + "/actions/runners", "secret")
        network.assert_not_called()
        self.assertIsNone(self.api._deadline)

    def test_request_timeout_uses_remaining_poll_budget(self):
        import github_api
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"{}"
        with mock.patch.object(github_api.time, "monotonic", side_effect=[100, 113]), \
                mock.patch.object(self.api.opener, "open", return_value=response) as network:
            with self.api.budget():
                self.api._request("GET", self.api.prefix + "/actions/runners", "secret")
        self.assertEqual(network.call_args.kwargs["timeout"], 7)


class CommandDiagnosticsTests(unittest.TestCase):
    def test_template_failure_keeps_the_safe_download_diagnosis(self):
        with mock.patch.object(controller.Config, "load", side_effect=controller.TemplateError(
                "Download ubuntu-image stalled; retry after checking the upstream proxy")), \
                mock.patch.object(controller.sys, "stderr", new_callable=io.StringIO) as output:
            code = controller.main(["status"])
        self.assertEqual(code, 1)
        self.assertIn("ubuntu-image stalled", output.getvalue())
        self.assertNotIn("Operation failed: TemplateError", output.getvalue())

    def test_unclassified_system_errors_still_hide_arbitrary_remote_text(self):
        with mock.patch.object(controller.Config, "load", side_effect=OSError(
                "https://sensitive.invalid/?registration-secret")), \
                mock.patch.object(controller.sys, "stderr", new_callable=io.StringIO) as output:
            code = controller.main(["status"])
        self.assertEqual(code, 1)
        self.assertIn("Operation failed: OSError", output.getvalue())
        self.assertNotIn("registration-secret", output.getvalue())


class HostProxyTests(unittest.TestCase):
    def setUp(self):
        self.host = controller.Host(controller.Config())
        self.opener = mock.MagicMock()

    def assert_web_health_request(self):
        self.opener.open.assert_called_once()
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://github.com/robots.txt")
        self.assertNotIn("api.github.com", request.full_url)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("User-agent"), "nexpoly-runner-health/1.0")
        self.assertFalse(request.has_header("Authorization"))
        self.assertIsNone(request.data)
        self.assertEqual(self.opener.open.call_args.kwargs, {"timeout": 15})

    def assert_safe_failure(self, exception, expected):
        self.opener.open.side_effect = exception
        with mock.patch.object(controller.urllib.request, "build_opener", return_value=self.opener) as build:
            with self.assertRaises(controller.RunnerError) as failure:
                self.host.proxy_check()
        message = str(failure.exception)
        self.assertIn(expected, message)
        self.assertIn("127.0.0.1:27892 -> upstream 127.0.0.1:27891", message)
        self.assertIn("A listening port alone does not verify HTTPS connectivity", message)
        self.assertNotIn("registration-secret", message)
        self.assertNotIn("sensitive.invalid", message)
        build.assert_called_once()
        self.assertEqual(build.call_args.args[0].proxies, {"https": self.host.config.host_proxy})
        self.assert_web_health_request()

    def test_success_uses_web_health_without_spending_unauthenticated_api_quota(self):
        response = self.opener.open.return_value.__enter__.return_value
        response.status = 200
        with mock.patch.object(controller.urllib.request, "build_opener", return_value=self.opener) as build:
            self.host.proxy_check()
        self.assertEqual(build.call_args.args[0].proxies, {"https": self.host.config.host_proxy})
        self.assert_web_health_request()
        response.read.assert_called_once_with(1024)

    def test_direct_timeout_explains_the_upstream_dependency_without_retry_or_fallback(self):
        self.assert_safe_failure(TimeoutError("registration-secret"), "HTTPS request timed out")

    def test_wrapped_timeout_explains_the_upstream_dependency_without_retry_or_fallback(self):
        self.assert_safe_failure(controller.urllib.error.URLError(TimeoutError("registration-secret")),
                                 "HTTPS request timed out")

    def test_connection_refused_has_safe_diagnosis(self):
        self.assert_safe_failure(controller.urllib.error.URLError(ConnectionRefusedError("registration-secret")),
                                 "proxy connection refused")

    def test_tls_validation_failure_hides_certificate_or_endpoint_details(self):
        self.assert_safe_failure(controller.urllib.error.URLError(
            controller.ssl.SSLCertVerificationError(1, "registration-secret sensitive.invalid")),
            "TLS certificate verification failed")

    def test_http_error_reports_only_the_numeric_status(self):
        error = controller.urllib.error.HTTPError("https://sensitive.invalid/registration-secret", 403,
                                                 "registration-secret", {}, mock.MagicMock())
        self.assert_safe_failure(error, "HTTP 403")

    def test_connect_failure_reports_only_the_numeric_status(self):
        self.assert_safe_failure(controller.urllib.error.URLError(
            OSError("Tunnel connection failed: 503 registration-secret sensitive.invalid")),
            "proxy CONNECT returned HTTP 503")

    def test_unrecognized_reason_is_not_copied_to_diagnostics(self):
        self.assert_safe_failure(controller.urllib.error.URLError("registration-secret sensitive.invalid"),
                                 "HTTPS connection failed")

    def test_invalid_configuration_does_not_expose_the_proxy_url(self):
        self.assert_safe_failure(ValueError("registration-secret sensitive.invalid"),
                                 "invalid HTTPS proxy configuration")

    def test_non_200_response_is_rejected_without_reading_its_body(self):
        response = self.opener.open.return_value.__enter__.return_value
        response.status = 503
        with mock.patch.object(controller.urllib.request, "build_opener", return_value=self.opener):
            with self.assertRaisesRegex(controller.RunnerError, "HTTP 503"):
                self.host.proxy_check()
        response.read.assert_not_called()
        self.assert_web_health_request()


class HostLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.host = controller.Host(controller.Config())
        self.instance_id = "b" * 32

    def test_main_pid_or_active_service_blocks_cleanup_even_with_empty_cgroup(self):
        for state, pid in (("active", "0"), ("inactive", "123"), ("deactivating", "0")):
            with self.subTest(state=state, pid=pid), mock.patch.object(self.host, "unit_state", return_value={
                    "ActiveState": state, "MainPID": pid, "ControlGroup": ""}):
                self.assertFalse(self.host.stopped(self.instance_id))

    def test_remaining_cgroup_process_blocks_cleanup_after_main_pid_exits(self):
        with mock.patch.object(self.host, "unit_state", return_value={
                "ActiveState": "inactive", "MainPID": "0", "ControlGroup": "/system.slice/vm.service"}), \
                mock.patch.object(controller.Path, "read_text", return_value="populated 1\nfrozen 0\n"):
            self.assertFalse(self.host.stopped(self.instance_id))

    def test_unknown_cgroup_state_fails_closed(self):
        with mock.patch.object(self.host, "unit_state", return_value={
                "ActiveState": "inactive", "MainPID": "0", "ControlGroup": "/system.slice/vm.service"}), \
                mock.patch.object(controller.Path, "read_text", side_effect=PermissionError("unreadable")):
            with self.assertRaises(PermissionError):
                self.host.stopped(self.instance_id)

    def test_stop_verifies_full_cgroup_exit(self):
        with mock.patch.object(controller, "command") as command, \
                mock.patch.object(self.host, "stopped", return_value=False):
            with self.assertRaisesRegex(controller.RunnerError, "still contains processes"):
                self.host.stop(self.instance_id)
        command.assert_called_once_with(
            ["systemctl", "stop", self.host.config.unit(self.instance_id)], timeout=150)

    def test_active_timer_is_not_recreated_or_extended(self):
        with mock.patch.object(controller, "command", return_value="active") as command:
            self.host.arm_deadline(self.instance_id, 7300)
            self.host.arm_deadline(self.instance_id, 9000)
        self.assertEqual(command.call_count, 2)
        self.assertTrue(all(call.args[0][0] == "systemctl" for call in command.call_args_list))

    def test_recover_missing_timer_uses_remaining_time_and_fixed_vm_unit(self):
        with mock.patch.object(controller, "command", side_effect=["inactive", ""]) as command, \
                mock.patch.object(controller.time, "time", return_value=7000):
            self.host.arm_deadline(self.instance_id, 7300)
        argv = command.call_args.args[0]
        self.assertEqual(argv[0], "systemd-run")
        self.assertIn("--on-active=300s", argv)
        self.assertIn("--timer-property=AccuracySec=1s", argv)
        self.assertIn("--unit=nexpoly-runner-deadline-" + self.instance_id, argv)
        self.assertEqual(argv[-3:], ["/usr/bin/systemctl", "stop", self.host.config.unit(self.instance_id)])

    def test_deadline_cannot_select_arbitrary_host_unit(self):
        with mock.patch.object(controller, "command") as command:
            with self.assertRaises(controller.RunnerError):
                self.host.arm_deadline("../../production", 7300)
        command.assert_not_called()

    def test_preflight_detects_other_vm_before_storage_or_network_access(self):
        with mock.patch.object(self.host, "raw_config", return_value={}), \
                mock.patch.object(self.host, "active_instances", return_value={"b" * 32}):
            with self.assertRaisesRegex(controller.RunnerError, "Another VM service"):
                self.host.preflight()


if __name__ == "__main__":
    unittest.main()

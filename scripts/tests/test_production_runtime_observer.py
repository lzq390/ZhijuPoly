from __future__ import annotations

import contextlib
import copy
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import struct
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("production_runtime_observer", ROOT / "scripts/production_runtime_observer.py")
assert SPEC is not None and SPEC.loader is not None
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)
NOW = dt.datetime(2026, 11, 10, 8, 0, 0, tzinfo=dt.timezone.utc)


def fixture() -> tuple[dict, dict]:
    inspected = {"Id": "production-id", "Name": "/prod-backend", "Image": "sha256:production",
        "State": {"Status": "running", "Running": True, "Pid": 100, "Health": {"Status": "healthy"}},
        "Config": {"Labels": {"org.opencontainers.image.revision": "b" * 40,
                                "com.docker.compose.project": "production", "com.docker.compose.service": "backend"},
                   "Env": ["PRIVATE=do-not-print"]},
        "HostConfig": {"DeviceRequests": [{"DeviceIDs": ["2"]}]},
        "Mounts": [{"Destination": "/b"}, {"Destination": "/a"}]}
    contract = {"name": "prod-backend", "image_id": inspected["Image"], "revision": "b" * 40,
                "compose_project": "production", "compose_service": "backend",
                "config_sha256": observer.canonical_hash(inspected["Config"]),
                "host_config_sha256": observer.canonical_hash(inspected["HostConfig"]),
                "mounts_sha256": observer.canonical_hash(sorted(inspected["Mounts"], key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))))}
    config = {"version": 1, "source_sha": "a" * 40, "source_tree": "c" * 40,
        "backend_source_sha": "b" * 40, "backend_container": "prod-backend", "containers": [contract],
        "units": [{"name": "production-dft.service", "working_directory": "/production/runtime",
                   "fragment_sha256": "d" * 64, "dropins": {}}],
        "workers": [{"name": "dft", "socket": "/production/dft.sock",
                     "expected": {"runtime_ready": True, "release_sha": "a" * 40, "runtime.fatal": False}}],
        "gpu_index": "2", "gpu_uuid": "GPU-production", "allowed_gpu_units": ["production-dft.service"],
        "state_path": "/production/guard.json", "report_path": "/production/report.json",
        "required_models": sorted(observer.REQUIRED_MODELS), "public_probes": []}
    return config, inspected


def healthy_gpu() -> dict:
    return {"status": "ready", "build_revision": "b" * 40, "accepting_inferences": True,
        "active_inferences": 1, "waiting_inferences": 1, "max_concurrent_inferences": 1, "max_waiting_inferences": 8,
        "models": {name: {"enabled": True, "loaded": True, "ready": True, "loading": False,
            "fatal": False, "error": None, "active_tasks": 1, "waiting_tasks": 1,
            "last_inference_error": "historical failure"} for name in observer.REQUIRED_MODELS}}


class ProductionRuntimeObserverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config, self.inspected = fixture()

    def test_private_config_validates_permissions_owner_regular_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(self.config))
            path.chmod(0o600)
            loaded, digest = observer.private_json(path)
            self.assertEqual(observer.validate_config(loaded), self.config)
            self.assertEqual(len(digest), 64)
            path.chmod(0o644)
            with self.assertRaisesRegex(observer.ObservationError, "identity_invalid"):
                observer.private_json(path)
            path.chmod(0o600)
            link = Path(directory) / "link.json"
            link.symlink_to(path)
            with self.assertRaisesRegex(observer.ObservationError, "unreadable"):
                observer.private_json(link)
            with mock.patch.object(observer.os, "geteuid", return_value=os.geteuid() + 1):
                with self.assertRaisesRegex(observer.ObservationError, "identity_invalid"):
                    observer.private_json(path)
            with self.assertRaisesRegex(observer.ObservationError, "not_absolute"):
                observer.private_json(Path("relative.json"))

    def test_duplicate_json_keys_and_invalid_schema_rejected(self) -> None:
        with self.assertRaisesRegex(observer.ObservationError, "duplicate_json"):
            observer.parse_json('{"version":1,"version":1}')
        for key, value in (("source_tree", "/untrusted/path"), ("gpu_index", "1"),
                           ("allowed_gpu_units", ["unverified.service"]), ("required_models", [])):
            config = dict(self.config, **{key: value})
            with self.subTest(key=key), self.assertRaises(observer.ObservationError):
                observer.validate_config(config)

    def test_public_probes_require_local_http_without_credentials(self) -> None:
        for url in ("https://127.0.0.1/", "http://example.com/", "http://user@127.0.0.1/", "file:///tmp/a"):
            config = dict(self.config, public_probes=[{"name": "bad", "url": url}])
            with self.subTest(url=url), self.assertRaises(observer.ObservationError):
                observer.validate_config(config)

    def test_only_named_production_containers_are_inspected(self) -> None:
        # Development may change or disappear; discovery/listing would fail this test.
        def command(*args: str) -> str:
            self.assertEqual(args, ("docker", "inspect", "prod-backend"))
            return json.dumps([self.inspected])
        with mock.patch.object(observer, "run", side_effect=command) as commands:
            first = observer.container_identity(self.config["containers"][0])
            self.inspected["State"]["Pid"] = 999  # restart with the same production contract
            second = observer.container_identity(self.config["containers"][0])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(commands.call_count, 2)

    def test_container_contract_drift_is_detected(self) -> None:
        for section in ("Config", "HostConfig", "Mounts"):
            changed = copy.deepcopy(self.inspected)
            if section == "Mounts":
                changed[section].append({"Destination": "/unexpected"})
            else:
                changed[section]["Unexpected"] = True
            with self.subTest(section=section), mock.patch.object(observer, "run", return_value=json.dumps([changed])):
                with self.assertRaisesRegex(observer.ObservationError, "contract_drift:" + section):
                    observer.container_identity(self.config["containers"][0])

    def test_mount_order_does_not_change_container_contract(self) -> None:
        self.inspected["Mounts"].reverse()
        with mock.patch.object(observer, "run", return_value=json.dumps([self.inspected])):
            self.assertEqual(observer.container_identity(self.config["containers"][0])["id"], "production-id")

    def test_container_health_and_source_label_are_required(self) -> None:
        self.inspected["State"]["Health"]["Status"] = "unhealthy"
        with mock.patch.object(observer, "run", return_value=json.dumps([self.inspected])):
            with self.assertRaisesRegex(observer.ObservationError, "unhealthy"):
                observer.container_identity(self.config["containers"][0])
        self.inspected["State"]["Health"]["Status"] = "healthy"
        self.inspected["Config"]["Labels"]["org.opencontainers.image.revision"] = "wrong"
        with mock.patch.object(observer, "run", return_value=json.dumps([self.inspected])):
            with self.assertRaisesRegex(observer.ObservationError, "label_drift:revision"):
                observer.container_identity(self.config["containers"][0])

    def test_unit_contract_checks_process_cgroup_and_resource_properties(self) -> None:
        contract = dict(self.config["units"][0], expected_properties={"MemoryMax": "34359738368"})
        raw = ("ActiveState=active\nSubState=running\nMainPID=200\nControlGroup=/user.slice/production-dft.service\n"
               "WorkingDirectory=/production/runtime\nFragmentPath=/production/worker.service\nDropInPaths=\nMemoryMax=34359738368\n")
        with mock.patch.object(observer, "run", return_value=raw), mock.patch.object(observer, "file_hash", return_value="d" * 64), mock.patch.object(observer, "cgroup_member", return_value=True):
            self.assertEqual(observer.unit_identity(contract)["pid"], 200)
        with mock.patch.object(observer, "run", return_value=raw), mock.patch.object(observer, "file_hash", return_value="d" * 64), mock.patch.object(observer, "cgroup_member", return_value=False):
            with self.assertRaisesRegex(observer.ObservationError, "cgroup_mismatch"):
                observer.unit_identity(contract)
        with mock.patch.object(observer, "run", return_value=raw.replace("MemoryMax=34359738368", "MemoryMax=1")), mock.patch.object(observer, "file_hash", return_value="d" * 64):
            with self.assertRaisesRegex(observer.ObservationError, "property_drift:MemoryMax"):
                observer.unit_identity(contract)

    def test_worker_identity_mismatch_is_detected(self) -> None:
        payload = {"runtime_ready": True, "release_sha": "wrong", "runtime": {"fatal": False}}
        with mock.patch.object(observer, "uds_health", return_value=payload):
            with self.assertRaisesRegex(observer.ObservationError, "expected_mismatch:release_sha"):
                observer.worker_health(self.config["workers"][0])

    def test_workers_allow_busy_capacity_without_accepting_new_jobs(self) -> None:
        variants = [
            {"active_jobs": 1, "max_active_jobs": 1},
            {"active_jobs": 1, "max_concurrent_jobs": 1, "queued_jobs": 8, "max_queued_jobs": 8},
            {"running_jobs": 1, "max_running_jobs": 1, "queued_jobs": 8, "max_queued_jobs": 8},
            {"ready": True, "active": 1, "queued": 1, "max_running_analyses": 1, "max_pending_analyses": 1},
        ]
        for variant in variants:
            payload = {"runtime_ready": True, "release_sha": "a" * 40, "runtime": {"fatal": False},
                       "draining": False, "accepting_jobs": False, **variant}
            if "ready" in variant:
                del payload["runtime_ready"]
            contract = {"name": "worker", "socket": "/production/worker.sock", "expected": {"release_sha": "a" * 40}}
            with self.subTest(variant=variant), mock.patch.object(observer, "uds_health", return_value=payload):
                observer.worker_health(contract)

    def test_worker_overcapacity_and_draining_fail(self) -> None:
        for extra in ({"active_jobs": 2, "max_active_jobs": 1}, {"draining": True}, {"runtime": {"fatal": True}}):
            payload = {"runtime_ready": True, "release_sha": "a" * 40, "runtime": {"fatal": False}, **extra}
            with mock.patch.object(observer, "uds_health", return_value=payload), self.assertRaises(observer.ObservationError):
                observer.worker_health(self.config["workers"][0])

    def test_uds_health_uses_only_get_health_and_timeout(self) -> None:
        response = mock.Mock(status=200)
        response.read.return_value = b'{"runtime_ready":true}'
        with mock.patch.object(observer, "socket_identity", return_value=(1, 2)), mock.patch.object(observer.socket, "socket") as sock, mock.patch.object(observer.http.client.HTTPConnection, "request") as request, mock.patch.object(observer.http.client.HTTPConnection, "getresponse", return_value=response):
            sock.return_value.getsockopt.return_value = struct.pack("3i", 123, os.geteuid(), os.getegid())
            self.assertEqual(observer.uds_health("/production/worker.sock"), {"runtime_ready": True})
            request.assert_called_once_with("GET", "/health")
            sock.return_value.settimeout.assert_called_once_with(observer.TIMEOUT)
            sock.return_value.connect.assert_called_once_with("/production/worker.sock")
        response.status = 503
        with mock.patch.object(observer, "socket_identity", return_value=(1, 2)), mock.patch.object(observer.socket, "socket") as sock, mock.patch.object(observer.http.client.HTTPConnection, "request"), mock.patch.object(observer.http.client.HTTPConnection, "getresponse", return_value=response):
            sock.return_value.getsockopt.return_value = struct.pack("3i", 123, os.geteuid(), os.getegid())
            with self.assertRaisesRegex(observer.ObservationError, "worker_health_http:503"):
                observer.uds_health("/production/worker.sock")

    def test_uds_peer_must_belong_to_verified_unit_or_container(self) -> None:
        with mock.patch.object(observer, "socket_identity", return_value=(1, 2)), mock.patch.object(observer.socket, "socket") as sock, mock.patch.object(observer, "cgroup_member", return_value=False):
            sock.return_value.getsockopt.return_value = struct.pack("3i", 123, os.geteuid(), os.getegid())
            with self.assertRaisesRegex(observer.ObservationError, "peer_cgroup_mismatch"):
                observer.uds_health("/production/worker.sock", expected_cgroup="/production/worker")
            with self.assertRaisesRegex(observer.ObservationError, "peer_container_mismatch"):
                observer.uds_health("/production/worker.sock", expected_pids={999})

    def test_uds_rejects_socket_replacement_and_unsafe_permissions(self) -> None:
        response = mock.Mock(status=200)
        response.read.return_value = b'{}'
        with mock.patch.object(observer, "socket_identity", side_effect=[(1, 2), (1, 3)]), mock.patch.object(observer.socket, "socket") as sock, mock.patch.object(observer.http.client.HTTPConnection, "request"), mock.patch.object(observer.http.client.HTTPConnection, "getresponse", return_value=response):
            sock.return_value.getsockopt.return_value = struct.pack("3i", 123, os.geteuid(), os.getegid())
            with self.assertRaisesRegex(observer.ObservationError, "socket_changed"):
                observer.uds_health("/production/worker.sock")
        fake = types.SimpleNamespace(st_mode=0o140666, st_uid=os.geteuid(), st_nlink=1, st_dev=1, st_ino=2)
        with mock.patch.object(observer.Path, "lstat", return_value=fake), mock.patch.object(observer.Path, "resolve", return_value=Path("/production/worker.sock")):
            with self.assertRaisesRegex(observer.ObservationError, "socket_identity_invalid"):
                observer.socket_identity("/production/worker.sock")

    def test_historical_world_mode_socket_requires_private_cohort_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "dft"
            parent.mkdir(mode=0o700)
            path = parent / "worker.sock"
            with observer.socket.socket(observer.socket.AF_UNIX, observer.socket.SOCK_STREAM) as connection:
                connection.bind(str(path))
                path.chmod(0o666)
                with self.assertRaisesRegex(observer.ObservationError, "socket_identity_invalid"):
                    observer.socket_identity(str(path))
                self.assertEqual(observer.socket_identity(str(path), private_socket_root=str(root)),
                                 (path.stat().st_dev, path.stat().st_ino))
                parent.chmod(0o755)
                with self.assertRaisesRegex(observer.ObservationError, "private_parent_invalid"):
                    observer.socket_identity(str(path), private_socket_root=str(root))
                parent.chmod(0o700)
                root.chmod(0o755)
                with self.assertRaisesRegex(observer.ObservationError, "private_parent_invalid"):
                    observer.socket_identity(str(path), private_socket_root=str(root))

    def test_private_socket_root_cannot_authorize_outside_or_symlinked_socket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cohort = root / "cohort"
            cohort.mkdir(mode=0o700)
            path = root / "other.sock"
            with observer.socket.socket(observer.socket.AF_UNIX, observer.socket.SOCK_STREAM) as connection:
                connection.bind(str(path))
                path.chmod(0o666)
                with self.assertRaisesRegex(observer.ObservationError, "outside_private_root"):
                    observer.socket_identity(str(path), private_socket_root=str(cohort))
                link = cohort / "worker.sock"
                link.symlink_to(path)
                with self.assertRaisesRegex(observer.ObservationError, "socket_identity_invalid"):
                    observer.socket_identity(str(link), private_socket_root=str(cohort))

    def test_worker_unit_peer_binding_rechecks_the_unit(self) -> None:
        contract = dict(self.config["workers"][0], unit="production-dft.service")
        health = {"runtime_ready": True, "release_sha": "a" * 40, "runtime": {"fatal": False}}
        identity = {"pid": 200, "cgroup": "/production/dft"}
        with mock.patch.object(observer, "unit_identity", return_value=identity) as unit, mock.patch.object(observer, "uds_health", return_value=health) as uds:
            observer.worker_health(contract, self.config)
        uds.assert_called_once_with(contract["socket"], expected_cgroup="/production/dft")
        self.assertEqual(unit.call_count, 2)

    def test_isolation_probe_uses_pinned_private_config_and_stable_unit(self) -> None:
        contract = {"name": "pmd", "unit": "production-dft.service", "helper": "/trusted/helper.py",
                    "helper_sha256": "1" * 64, "worker_config": "/private/worker.json", "worker_config_sha256": "2" * 64}
        helper = mock.Mock()
        helper.inspect_runtime.return_value = {"no_new_privileges": True, "gpu_device_count": 0}
        identity = {"pid": 200, "cgroup": "/production/dft"}
        with mock.patch.object(observer, "private_json", return_value=({"source_sha": "a" * 40}, "2" * 64)), mock.patch.object(observer, "pinned_helper", return_value=helper) as load, mock.patch.object(observer, "unit_identity", return_value=identity) as unit:
            self.assertEqual(observer.isolation_probe(contract, self.config)["gpu_device_count"], 0)
        load.assert_called_once_with("/trusted/helper.py", "1" * 64)
        helper.inspect_runtime.assert_called_once_with({"source_sha": "a" * 40}, 200, "/production/dft")
        self.assertEqual(unit.call_count, 2)
        with mock.patch.object(observer, "private_json", return_value=({}, "wrong")), self.assertRaisesRegex(observer.ObservationError, "worker_config_hash_mismatch"):
            observer.isolation_probe(contract, self.config)

    def test_pinned_helper_rejects_hash_drift_and_runs_only_verified_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "helper.py"
            helper.write_text("def inspect_runtime(config, pid, cgroup):\n return {'verified': True}\n")
            helper.chmod(0o600)
            with self.assertRaisesRegex(observer.ObservationError, "helper_hash_mismatch"):
                observer.pinned_helper(str(helper), "0" * 64)
            loaded = observer.pinned_helper(str(helper), observer.file_hash(str(helper)))
            self.assertEqual(loaded.inspect_runtime({}, 1, "/production"), {"verified": True})

    def test_gpu_health_accepts_activity_and_historical_error(self) -> None:
        with mock.patch.object(observer, "backend_status", return_value=healthy_gpu()):
            details = observer.gpu_health(self.config)
        self.assertEqual(details["active_inferences"], 1)
        self.assertEqual(set(details["historical_inference_errors"]), observer.REQUIRED_MODELS)

    def test_gpu_health_rejects_source_loading_disabled_and_overcapacity(self) -> None:
        for change in ("source", "missing", "disabled", "loading", "fatal", "error", "capacity", "concurrency"):
            payload = healthy_gpu()
            if change == "source":
                payload["build_revision"] = "a" * 40
            elif change == "missing":
                del payload["models"]["ocsr"]
            elif change == "disabled":
                payload["models"]["ocsr"]["enabled"] = False
            elif change in ("loading", "fatal"):
                payload["models"]["ocsr"][change] = True
            elif change == "error":
                payload["models"]["ocsr"][change] = "load failed"
            elif change == "concurrency":
                payload["max_concurrent_inferences"] = 2
            else:
                payload["active_inferences"] = 3
            with self.subTest(change=change), mock.patch.object(observer, "backend_status", return_value=payload):
                with self.assertRaises(observer.ObservationError):
                    observer.gpu_health(self.config)

    def test_deployment_allows_active_work_but_not_drain(self) -> None:
        with mock.patch.object(observer, "backend_status", return_value={"drain": {"enabled": False}, "active_total": 3}):
            self.assertEqual(observer.deployment_health(self.config)["active_total"], 3)
        with mock.patch.object(observer, "backend_status", return_value={"drain": {"enabled": True}, "active_total": 3}):
            with self.assertRaisesRegex(observer.ObservationError, "deployment_draining"):
                observer.deployment_health(self.config)

    def guard_patches(self, *, processes: list[dict] | None = None, groups: dict[int, list[str]] | None = None):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(observer, "container_identity", return_value={"id": "production-id", "pid": 100}))
        stack.enter_context(mock.patch.object(observer, "unit_identity", return_value={"pid": 200, "cgroup": "/production/dft"}))
        stack.enter_context(mock.patch.object(observer, "gpu_processes", return_value=processes or []))
        stack.enter_context(mock.patch.object(observer, "backend_pids", return_value={100, 101}))
        stack.enter_context(mock.patch.object(observer, "process_cgroups", side_effect=lambda pid: (groups or {}).get(pid, [])))
        stack.enter_context(mock.patch.object(observer, "now", return_value=NOW))
        return stack

    def test_guard_classifies_only_backend_pids_and_verified_unit_cgroups(self) -> None:
        processes = [{"pid": pid, "gpu_uuid": "GPU-production"} for pid in (101, 201, 999)]
        groups = {101: ["/docker/production"], 201: ["/production/dft/child"], 999: ["/production/dft-lookalike"]}
        with self.guard_patches(processes=processes, groups=groups), mock.patch.object(observer, "atomic_write") as write:
            payload, code = observer.guard(self.config, "f" * 64)
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "quarantined")
        self.assertEqual([item["pid"] for item in payload["allowed_processes"]], [101, 201])
        self.assertEqual([item["pid"] for item in payload["unknown_processes"]], [999])
        write.assert_called_once_with(Path(self.config["state_path"]), payload)

    def test_guard_success_does_not_depend_on_unrelated_services_or_workers(self) -> None:
        self.config["containers"].append({"name": "prod-nginx"})
        self.config["units"].append({"name": "unrelated.service"})
        with self.guard_patches(), mock.patch.object(observer, "atomic_write"), mock.patch.object(observer, "worker_health", side_effect=AssertionError("must not probe workers")), mock.patch.object(observer, "verify", side_effect=AssertionError("must not precheck monitor")):
            payload, code = observer.guard(self.config, "f" * 64)
        self.assertEqual((payload["status"], code), ("ready", 0))
        self.assertEqual(payload["unknown_processes"], [])

    def test_guard_sampling_failure_writes_fresh_unresolved_state(self) -> None:
        with self.guard_patches(), mock.patch.object(observer, "gpu_processes", side_effect=observer.ObservationError("command_timeout:nvidia-smi")), mock.patch.object(observer, "atomic_write") as write:
            payload, code = observer.guard(self.config, "f" * 64)
        self.assertEqual(code, 2)
        self.assertEqual(payload["observed_at"], "2026-11-10T08:00:00Z")
        self.assertEqual(payload["unknown_processes"][0]["kind"], "unresolved_observation")
        self.assertEqual(payload["errors"], ["command_timeout:nvidia-smi"])
        self.assertEqual(payload["guard_mode"], "observe")
        write.assert_called_once()

    def test_guard_identity_failure_also_writes_fresh_unresolved_state(self) -> None:
        with self.guard_patches(), mock.patch.object(observer, "container_identity", side_effect=observer.ObservationError("container_contract_drift:Config")), mock.patch.object(observer, "atomic_write") as write:
            payload, code = observer.guard(self.config, "f" * 64)
        self.assertEqual(code, 2)
        self.assertTrue(payload["unknown_processes"])
        self.assertIn("contract_drift", payload["errors"][0])
        write.assert_called_once()

    def test_guard_identity_change_during_sampling_quarantines(self) -> None:
        with self.guard_patches(), mock.patch.object(observer, "container_identity", side_effect=[{"id": "old", "pid": 100}, {"id": "new", "pid": 100}]), mock.patch.object(observer, "atomic_write"):
            payload, code = observer.guard(self.config, "f" * 64)
        self.assertEqual(code, 2)
        self.assertEqual(payload["errors"], ["backend_changed_during_sample"])

    def test_guard_ignores_a_process_that_exited_naturally(self) -> None:
        with self.guard_patches(processes=[{"pid": 999, "gpu_uuid": "GPU-production"}]), mock.patch.object(observer.Path, "exists", return_value=False), mock.patch.object(observer, "atomic_write"):
            payload, code = observer.guard(self.config, "f" * 64)
        self.assertEqual((payload["unknown_processes"], code), ([], 0))

    def test_gpu_queries_never_initialize_cuda_and_reject_malformed_samples(self) -> None:
        with mock.patch.object(observer, "run", side_effect=["GPU-production\n", "101, GPU-production, private-command\n"]) as commands:
            self.assertEqual(observer.gpu_processes(self.config), [{"pid": 101, "gpu_uuid": "GPU-production"}])
        self.assertTrue(all(call.args[0] == "nvidia-smi" and "-i" in call.args and "2" in call.args for call in commands.call_args_list))
        with mock.patch.object(observer, "run", side_effect=["GPU-production\n", "malformed\n"]):
            with self.assertRaisesRegex(observer.ObservationError, "sample_invalid"):
                observer.gpu_processes(self.config)

    def test_monitor_guard_freshness_and_identity(self) -> None:
        with self.guard_patches(), mock.patch.object(observer, "atomic_write"):
            payload, _ = observer.guard(self.config, "f" * 64)
        with mock.patch.object(observer, "private_json", return_value=(payload, "ignored")), mock.patch.object(observer, "now", return_value=NOW):
            observer.guard_health(self.config, "f" * 64)
            payload["observed_at"] = "2026-11-10T07:57:59Z"
            with self.assertRaisesRegex(observer.ObservationError, "snapshot_stale"):
                observer.guard_health(self.config, "f" * 64)
            payload["observed_at"] = "2026-11-10T8:00:00Z"
            with self.assertRaisesRegex(observer.ObservationError, "timestamp_invalid"):
                observer.guard_health(self.config, "f" * 64)
            payload["observed_at"] = "2026-11-10T08:00:00Z"
            with self.assertRaisesRegex(observer.ObservationError, "config_identity_invalid"):
                observer.guard_health(self.config, "e" * 64)

    def test_checks_continue_after_production_drift(self) -> None:
        with mock.patch.object(observer, "container_identity", side_effect=observer.ObservationError("container_identity_drift")), mock.patch.object(observer, "unit_identity", return_value={}), mock.patch.object(observer, "worker_health", return_value={}) as worker, mock.patch.object(observer, "gpu_health", return_value={}) as gpu, mock.patch.object(observer, "deployment_health", return_value={}):
            checks = observer.verify(self.config)
        self.assertEqual(checks["container:prod-backend"]["status"], "failed")
        self.assertEqual(checks["gpu_runtime"]["status"], "ok")
        worker.assert_called_once()
        gpu.assert_called_once()

    def test_verify_has_no_gpu_sampling_and_writes_nothing(self) -> None:
        with mock.patch.object(observer, "private_json", return_value=(self.config, "f" * 64)), mock.patch.object(observer, "container_identity", return_value={}), mock.patch.object(observer, "unit_identity", return_value={}), mock.patch.object(observer, "worker_health", return_value={}), mock.patch.object(observer, "gpu_health") as gpu, mock.patch.object(observer, "gpu_processes") as sample, mock.patch.object(observer, "atomic_write") as write, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(observer.main(["--config", "/private/config.json", "--mode", "verify"]), 0)
        gpu.assert_not_called()
        sample.assert_not_called()
        write.assert_not_called()

    def test_monitor_writes_report_only_and_never_samples_or_updates_guard(self) -> None:
        checks = {"container:prod-backend": {"status": "failed", "error": "contract_drift"}, "worker:dft": {"status": "ok"}}
        with mock.patch.object(observer, "private_json", return_value=(self.config, "f" * 64)), mock.patch.object(observer, "verify", return_value=checks), mock.patch.object(observer, "guard_health", return_value={"status": "ready"}), mock.patch.object(observer, "gpu_processes") as sample, mock.patch.object(observer, "guard") as guard, mock.patch.object(observer, "atomic_write") as write, mock.patch.object(observer, "now", return_value=NOW), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(observer.main(["--config", "/private/config.json", "--mode", "monitor"]), 2)
        sample.assert_not_called()
        guard.assert_not_called()
        self.assertEqual(write.call_args.args[0], Path(self.config["report_path"]))
        self.assertEqual(write.call_args.args[1]["failed_checks"], ["container:prod-backend"])
        self.assertEqual(write.call_args.args[1]["checked_at"], "2026-11-10T08:00:00Z")

    def test_invalid_config_cannot_write_a_guard_or_report(self) -> None:
        self.config["source_sha"] = "invalid"
        with mock.patch.object(observer, "private_json", return_value=(self.config, "f" * 64)), mock.patch.object(observer, "atomic_write") as write, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(observer.main(["--config", "/private/config.json", "--mode", "guard"]), 2)
        write.assert_not_called()

    def test_subprocess_has_timeout_and_safe_diagnostics(self) -> None:
        with mock.patch.object(observer.subprocess, "run", side_effect=subprocess.CalledProcessError(7, ["docker"], output="SECRET", stderr="SECRET")) as run:
            with self.assertRaisesRegex(observer.ObservationError, "^command_failed:docker:7$"):
                observer.run("docker", "inspect", "prod-backend")
        self.assertEqual(run.call_args.kwargs["timeout"], observer.TIMEOUT)
        with mock.patch.object(observer.subprocess, "run", side_effect=subprocess.TimeoutExpired("docker", 15, output="SECRET")):
            with self.assertRaisesRegex(observer.ObservationError, "^command_timeout:docker$"):
                observer.run("docker", "inspect", "prod-backend")

    def test_atomic_write_private_and_fsync_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(observer.os, "fsync") as sync:
            path = Path(directory) / "state.json"
            observer.atomic_write(path, {"status": "ready"})
            self.assertEqual(json.loads(path.read_text()), {"status": "ready"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(sync.call_count, 2)


if __name__ == "__main__":
    unittest.main()

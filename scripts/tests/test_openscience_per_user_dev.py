"""Recovery and fail-closed cases complementary to the two-user Docker acceptance."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, mock_open, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openscience_per_user_dev as m1
import openscience_per_user_service as service


class ManagerTest(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.registry = m1.Registry(Path(directory.name))
        self.registry.data = {"format": 1, "image": "image-a", "template_version": m1.VERSION,
                              "deployment": "deployment-test", "workspaces": {}, "max_active": 2,
                              "idle_seconds": 1800}
        self.a = self.registry.workspace("dev-alpha", "member", create=True)

    def test_binding_survives_process_reopen_and_uid_collision(self):
        with patch.object(m1.secrets, "randbelow", side_effect=[self.a.row["uid"] - 210000,
                                                           (self.a.row["uid"] - 209999) % 100000000]):
            b = self.registry.workspace("dev-beta", "member", create=True)
        fresh = m1.Registry(self.registry.root)
        first = fresh.workspace("dev-alpha", "member")
        second = fresh.workspace("dev-beta", "member")
        self.assertEqual((first.volume, first.uid), (self.a.volume, self.a.uid))
        self.assertEqual((second.volume, second.uid), (b.volume, b.uid))
        self.assertNotEqual(first.volume, second.volume)
        self.assertNotEqual(first.uid, second.uid)
        with self.assertRaises(m1.Problem):
            fresh.workspace("production", "member", create=True)

    def test_lost_registered_volume_is_never_replaced_by_empty_storage(self):
        self.a.row["initialized"] = True
        with patch.object(self.a, "resource", return_value=None), patch.object(m1, "docker") as docker:
            with self.assertRaisesRegex(m1.Problem, "Persistent volume is missing"):
                self.a.prepare_volume()
            docker.assert_not_called()

    def test_existing_foreign_labels_are_rejected_before_removal(self):
        foreign = self.a.labels("backend")
        foreign[m1.PREFIX + "workspace"] = "someone-else"
        output = json.dumps(foreign) + "\ncontainer\nrunning\nimage-a"
        with patch.object(m1, "docker", return_value=output) as docker:
            with self.assertRaisesRegex(m1.Problem, "Conflicting"):
                self.a.remove_pair()
            self.assertEqual(docker.call_count, 1)
            self.assertEqual(docker.call_args.args[:2], ("container", "inspect"))

    def test_unavailable_probe_and_unsettled_work_prevent_drain(self):
        for value in ("{}", "null", '{"idle":true,"last_activity_ms":"invalid"}'):
            with self.subTest(value=value), patch.object(m1, "docker", return_value=value):
                self.assertEqual(self.a.activity({"id": "a"}, False), "activity_unknown")
        self.a.row["leases"] = ["unfinished"]
        with patch.object(m1, "docker") as docker:
            self.assertEqual(self.a.activity({"id": "a"}, False), "busy")
            docker.assert_not_called()

    def test_idle_probe_must_still_pass_after_admission_closes(self):
        self.a.control.mkdir(parents=True)
        backend = {"state": "running", "id": "A"}
        seen = []
        def activity(*args):
            seen.append((self.a.control / "draining").is_file())
            return "active_request" if len(seen) == 2 else None
        with patch.object(self.a, "resource", return_value=backend), patch.object(self.a, "verify_pair"), \
                patch.object(self.a, "activity", side_effect=activity), patch.object(self.a, "remove_pair") as remove:
            result = self.a.drain("reap")
        self.assertEqual(seen, [False, True])
        self.assertEqual(result["reason"], "active_request")
        self.assertFalse((self.a.control / "draining").exists())
        remove.assert_not_called()

    def test_resident_container_blocks_template_update_until_recycle(self):
        with patch.object(m1, "software_image"), patch.object(m1, "docker", return_value="running-container"):
            with self.assertRaisesRegex(m1.Problem, "template_in_use"):
                self.registry.set_image("image-b")
        self.assertEqual(self.registry.data["image"], "image-a")
        with patch.object(m1, "software_image"), patch.object(m1, "docker", return_value=""):
            self.registry.set_image("image-b")
        self.assertEqual(m1.Registry(self.registry.root).data["image"], "image-b")
        self.assertEqual(self.registry.workspace("dev-alpha", "member").uid, self.a.uid)

    def test_idle_reap_rechecks_the_water_level_before_removing_the_warm_instance(self):
        self.registry.data["format"] = 5
        self.a.control.mkdir(parents=True)
        refused = m1.CapacityRefusal("global_disk", "workspace_disk_low_watermark")
        with patch.object(self.registry, "check_disk_admission", side_effect=[None, refused]), \
                patch.object(self.a, "resource", return_value={"id": "A", "state": "running"}), \
                patch.object(self.a, "verify_pair"), patch.object(self.a, "activity", return_value=None), \
                patch.object(self.a, "state", side_effect=lambda reason: reason), patch.object(self.a, "remove_pair") as remove:
            self.assertEqual(self.a.drain("reap"), "disk_recovery")
        remove.assert_not_called()
        self.assertFalse((self.a.control / "draining").exists())

    def test_capacity_defaults_apply_to_legacy_registry_and_settings_persist(self):
        fresh = m1.Registry(self.registry.root)
        self.assertNotIn("capacity", fresh.data)
        self.assertEqual(fresh.capacity(), m1.DEFAULT_CAPACITY)
        with patch.object(fresh, "resident_ids", return_value=set()), \
                patch.object(m1, "host_available_mib", return_value=16384), \
                self.assertRaisesRegex(m1.CapacityRefusal, "host_memory"):
            fresh.admit(self.a)
        configured = fresh.set_capacity(3000, 4000, 5000)
        self.assertEqual(configured["capacity"], m1.Registry(fresh.root).capacity())
        with self.assertRaises(m1.Problem):
            fresh.set_capacity(0, 1, 1)
        self.assertEqual(configured["capacity"], m1.Registry(fresh.root).capacity())
        self.assertEqual(fresh.workspace("dev-alpha", "member").uid, self.a.uid)
        fresh.data["capacity"] = {"workspace_memory_mib": True}
        with self.assertRaisesRegex(m1.CapacityRefusal, "probe_unavailable"):
            fresh.admit(self.a)

    def test_new_registry_saves_defaults_and_recovery_charges_a_partial_pair_once(self):
        with TemporaryDirectory() as directory:
            fresh = m1.Registry(Path(directory))
            settings = SimpleNamespace(max_active=1, idle_seconds=1800, image="image-a")
            with patch.object(m1, "software_image"), patch.object(m1, "docker"):
                fresh.initialize(settings)
            self.assertEqual(m1.Registry(fresh.root).data["capacity"], m1.DEFAULT_CAPACITY)
            candidate = fresh.workspace("dev-test", "member", create=True)
            fresh.set_capacity(m1.PAIR_MEMORY_MIB, 1, 1)
            with patch.object(fresh, "resident_ids", return_value={candidate.row["id"]}), \
                    patch.object(m1, "host_available_mib", return_value=m1.PAIR_MEMORY_MIB + 1), \
                    patch.object(candidate, "volume_free_mib", return_value=m1.VOLUME_RESERVE_MIB + 1):
                fresh.admit(candidate)

    def test_pair_memory_host_headroom_and_disk_reserve_include_unknown_residents(self):
        other = "f" * 32  # Labeled container whose binding is absent from this copy of the registry.
        output = f"backend|{other}\nfixture|{other}\nvolume-check|{self.a.row['id']}"
        limits = self.registry.capacity()
        host = limits["host_memory_floor_mib"] + 2 * m1.PAIR_MEMORY_MIB
        disk = limits["volume_free_floor_mib"] + 2 * m1.VOLUME_RESERVE_MIB
        for count, budget, available, space, refusal in (
            (1, 4288, host, disk, "instance_count"), (2, 4287, host, disk, "workspace_memory"),
            (2, 4288, host - 1, disk, "host_memory"), (2, 4288, host, disk - 1, "volume_free"),
            (2, 4288, host, disk, None),
        ):
            with self.subTest(refusal=refusal), patch.object(m1, "docker", return_value=output) as docker, \
                    patch.object(m1, "host_available_mib", return_value=available), \
                    patch.object(self.a, "volume_free_mib", return_value=space):
                self.registry.data["max_active"] = count
                self.registry.set_capacity(budget, limits["host_memory_floor_mib"], limits["volume_free_floor_mib"])
                if refusal:
                    with self.assertRaisesRegex(m1.CapacityRefusal, "^workspace_capacity: " + refusal):
                        self.registry.admit(self.a)
                else:
                    self.registry.admit(self.a)
                self.assertIn("label=" + m1.PREFIX + "deployment=deployment-test", docker.call_args.args)

    def test_capacity_probes_fail_closed_without_start_or_allocation(self):
        for output, meminfo, space in (
            ("backend|", "", ""), ("backend|" + self.a.row["id"], "MemFree:   100000000 kB", ""),
            ("", "MemAvailable:   100000000 kB", "-2"), ("", "MemAvailable:   100000000 kB", "nan"),
        ):
            with self.subTest(output=output, space=space), patch.object(m1, "docker", return_value=output), \
                    patch.object(self.a, "verify_template_files"), patch.object(self.a, "resource", return_value=None), \
                    patch.object(m1.Path, "read_text", return_value=meminfo), \
                    patch.object(self.a, "helper", return_value=space), \
                    patch.object(self.a, "create_pair") as create, patch.object(self.a, "prepare_volume") as prepare:
                with self.assertRaisesRegex(m1.CapacityRefusal, "^workspace_capacity: probe_unavailable"):
                    self.a.ensure()
                prepare.assert_not_called()
                create.assert_not_called()
        with patch.object(self.registry, "resident_ids", side_effect=m1.Problem("Docker unavailable")), \
                self.assertRaisesRegex(m1.CapacityRefusal, "probe_unavailable"):
            self.registry.admit(self.a)

    def test_space_probe_is_read_only_on_the_named_or_anonymous_docker_volume(self):
        for existing in (self.a.volume, None):
            with self.subTest(existing=existing), patch.object(self.a, "resource", return_value=existing), \
                    patch.object(m1, "docker", return_value="178123") as docker:
                self.assertEqual(self.a.volume_free_mib(), 178123)
                args = docker.call_args.args
                mount = args[args.index("--mount") + 1]
                self.assertIn("type=volume", mount)
                self.assertIn("readonly", mount)
                self.assertEqual("src=" + self.a.volume in mount, bool(existing))
                self.assertIn("--rm", args)
                self.assertIn("--cap-drop=ALL", args)
                self.assertEqual(args[args.index("--user") + 1], self.a.identity)
        self.a.row["initialized"] = True
        with patch.object(self.a, "resource", return_value=None), patch.object(self.a, "helper") as helper, \
                self.assertRaisesRegex(m1.Problem, "Persistent volume is missing"):
            self.a.volume_free_mib()
        helper.assert_not_called()

    def test_reconciliation_preserves_uncertain_resident_and_recovers_stopped_pair(self):
        b = self.registry.workspace("dev-alpha", "other", create=True)
        self.registry.data["max_active"] = 1
        residents = {b.row["id"]}
        for backend, fixture in (("paused", "running"), (None, "restarting"), ("exited", "running")):
            with self.subTest(backend=backend, fixture=fixture), \
                    patch.object(self.registry, "resident_ids", side_effect=[residents, set()]), \
                    patch.object(self.registry, "bindings", return_value=[self.a, b]), \
                    patch.object(b, "resource", side_effect=[{"state": backend} if backend else None, {"state": fixture}]), \
                    patch.object(b, "remove_pair") as remove, patch.object(m1, "host_available_mib", return_value=20000), \
                    patch.object(self.a, "volume_free_mib", return_value=40000):
                if backend == "exited":
                    self.registry.admit(self.a)
                    remove.assert_called_once_with()
                else:
                    with self.assertRaisesRegex(m1.CapacityRefusal, "instance_count"):
                        self.registry.admit(self.a)
                    remove.assert_not_called()

    def test_healthy_workspace_remains_available_when_limits_cannot_admit_one(self):
        self.registry.set_capacity(1, 1, 1)
        self.a.row["initialized"] = True
        with patch.object(self.a, "verify_template_files"), patch.object(self.a, "verify_pair"), \
                patch.object(self.a, "resource", return_value={"state": "running", "id": "container"}), \
                patch.object(self.a, "ready"), patch.object(self.registry, "admit") as admit:
            self.assertEqual(self.a.ensure()["state"], "running")
            self.assertEqual(self.a.state()["state"], "running")
            admit.assert_not_called()

    def test_start_backoff_still_surfaces_cold_cleanup_during_low_water(self):
        self.a.row["retry_after"] = m1.time.time() + 60
        denied = m1.CapacityRefusal("global_disk", "workspace_disk_low_watermark")
        with patch.object(self.a, "verify_template_files"), patch.object(self.a, "resource", return_value=None), \
                patch.object(self.registry, "check_disk_admission", side_effect=[denied, None]), \
                patch.object(self.a, "create_pair") as create:
            with self.assertRaisesRegex(m1.CapacityRefusal, "workspace_disk_low_watermark"):
                self.a.ensure()
            with self.assertRaisesRegex(m1.Problem, "backoff"):
                self.a.ensure()
            create.assert_not_called()


class QuotaManagerTest(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.registry = m1.Registry(root)
        self.registry.data = {"format": 1, "image": "image-a", "template_version": m1.VERSION,
                              "deployment": "deployment-test", "workspaces": {}, "max_active": 2,
                              "idle_seconds": 1800}
        self.bindings = []
        for index, suffix in enumerate("ab"):
            mount = root / suffix
            (mount / "workspace").mkdir(parents=True)
            self.bindings.append({"mount": str(mount), "filesystem_uuid": str(uuid4()),
                                  "environment": "dev-m1", "user_id": "test-user-" + suffix,
                                  "workspace": suffix * 32, "uid": 220000 + index, "generation": 12})
        self.web = Mock()

    def register(self):
        with patch.object(m1, "inspect_quota_volume", return_value={}), patch.object(m1, "pilot_module", return_value=self.web):
            self.registry.register_quota_storage(self.bindings)
        return self.registry.workspace("dev-m1", "test-user-a")

    def test_registration_is_atomic_and_cannot_convert_an_existing_registry(self):
        with patch.object(m1, "inspect_quota_volume", side_effect=[{}, m1.Problem("wrong B quota")]), \
                self.assertRaisesRegex(m1.Problem, "wrong B"):
            self.registry.register_quota_storage(self.bindings)
        self.assertFalse(self.registry.file.exists())
        self.assertEqual(self.registry.data["workspaces"], {})
        self.registry.workspace("dev-m1", "test-user-a", create=True)
        with patch.object(m1, "inspect_quota_volume") as probe, self.assertRaisesRegex(m1.Problem, "fresh"):
            self.registry.register_quota_storage(self.bindings)
        probe.assert_not_called()

    def test_reopen_reuses_advance_and_rejects_missing_or_unregistered_quota_identity(self):
        a = self.register()
        a.row["generation"] += 1
        self.registry.save()
        self.register()  # Replaying the operator input keeps the live generation.
        fresh = m1.Registry(self.registry.root)
        self.assertEqual(fresh.data["format"], 2)
        a = fresh.workspace("dev-m1", "test-user-a")
        self.assertEqual((a.row["generation"], a.uid, a.row["id"]), (13, "220000", "a" * 32))
        self.assertEqual(a.volume, "ext4:" + self.bindings[0]["filesystem_uuid"])
        self.assertEqual(a.data_mount(m1.MOUNT),
                         f"type=bind,src={self.bindings[0]['mount']}/workspace,dst=/openscience,bind-propagation=rprivate")
        with self.assertRaises(m1.Problem):
            fresh.workspace("dev-m1", "other", create=True)
        fresh.data["quota_storage"].pop("dev-m1/test-user-a")
        with self.assertRaisesRegex(m1.Problem, "identity"):
            fresh.workspace("dev-m1", "test-user-a", create=True)
        del fresh.data["quota_storage"]
        with self.assertRaisesRegex(m1.Problem, "identity"):
            fresh.workspace("dev-m1", "test-user-a", create=True)

    def test_register_rejects_duplicate_owner_device_or_directory_alias(self):
        a, b = self.bindings
        for key in ("user_id", "workspace", "uid", "filesystem_uuid", "mount"):
            with self.subTest(key=key), self.assertRaisesRegex(m1.Problem, "distinct"):
                self.registry.register_quota_storage([a, {**b, key: a[key]}])
        alias = Path(a["mount"]).with_name("alias")
        alias.symlink_to(a["mount"])
        with self.assertRaisesRegex(m1.Problem, "canonical"):
            self.registry.register_quota_storage([{**a, "mount": str(alias)}, b])
        self.assertFalse(self.registry.file.exists())

    def test_wrong_uuid_uid_or_unenforced_hard_limit_cannot_reach_payload(self):
        binding = self.bindings[0]
        quota = Mock(PILOT_HARD_BYTES=2000000000)
        observed = {"filesystem_uuid": binding["filesystem_uuid"], "uid": binding["uid"], "hard_bytes": 2000000000,
                    "accounting": True, "enforced": True}
        for key, bad in [("filesystem_uuid", str(uuid4())), ("uid", 220001), ("hard_bytes", 0),
                         ("accounting", False), ("enforced", False)]:
            quota.read.return_value = {**observed, key: bad}
            with self.subTest(key=key), patch.object(m1, "pilot_module", return_value=quota), \
                    self.assertRaisesRegex(m1.Problem, "hard quota differs"):
                m1.inspect_quota_volume(binding)
        quota.check_source_mount.assert_not_called()
        quota.read.return_value = observed
        with patch.object(m1, "pilot_module", return_value=quota), self.assertRaisesRegex(m1.Problem, "directory differs"):
            m1.inspect_quota_volume(binding)

    def test_full_quota_uses_shared_admission_space_and_missing_quota_never_creates_storage(self):
        a = self.register()
        full = {"used_bytes": 2000000000, "hard_bytes": 2000000000}
        with patch.object(m1, "inspect_quota_volume", return_value=full), patch.object(m1, "docker", return_value="5000") as docker:
            self.assertEqual(a.volume_free_mib(), 5000)
            self.assertIn("type=volume,dst=/data,volume-nocopy,readonly", docker.call_args.args)
        with patch.object(m1, "inspect_quota_volume", side_effect=m1.Problem("missing quota mount")), \
                patch.object(m1, "docker") as docker, self.assertRaisesRegex(m1.Problem, "missing quota mount"):
            a.prepare_volume()
        docker.assert_not_called()
        with patch.object(a, "resource", return_value=None), patch.object(m1, "inspect_quota_volume", return_value=full):
            status = a.state()
            self.assertEqual((status["quota"], status["storage_backend"], status["state"]),
                             (full, m1.QUOTA_BACKEND, "recycled"))

    def test_live_directory_inode_must_match_the_attested_mount_even_if_source_path_is_same(self):
        a = self.register()
        with patch.object(Path, "samefile", return_value=False) as same, self.assertRaisesRegex(m1.Problem, "Resident"):
            a.verify_live_quota_mount({"state": "running", "pid": 1234})
        same.assert_called_once_with(Path(self.bindings[0]["mount"]) / "workspace")

    def test_configured_quota_readiness_is_persistent_bounded_and_replay_safe(self):
        a = self.register()
        with patch.object(m1, "inspect_quota_volume", return_value={}), patch.object(m1, "pilot_module", return_value=self.web):
            self.registry.register_quota_storage(self.bindings, 150)
            for invalid in (29, 181, True, "150"):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(m1.Problem, "30 and 180"):
                    self.registry.register_quota_storage(self.bindings, invalid)
            self.registry.register_quota_storage(self.bindings)
        fresh = m1.Registry(self.registry.root)
        self.assertEqual(fresh.readiness_seconds(), 150)
        with patch.object(m1, "docker") as docker:
            a.ready({"id": "fixture"}, {"id": "backend"})
            self.assertEqual((docker.call_args.args[-1], docker.call_args.kwargs["timeout"]), ("150", 165))
        with patch.object(service.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="{}")) as run:
            service.manager(fresh.root, "ensure")
            self.assertEqual(run.call_args.kwargs["timeout"], 270)
            service.manager(fresh.root, "status")
            self.assertEqual(run.call_args.kwargs["timeout"], 150)
        with TemporaryDirectory() as directory:
            normal = m1.Registry(Path(directory))
            normal.data = {"format": 1, "deployment": "old", "workspaces": {}}
            normal_ws = normal.workspace("dev-legacy", "member", create=True)
            with patch.object(m1, "docker") as docker:
                normal_ws.ready({"id": "fixture"}, {"id": "backend"})
                self.assertEqual((docker.call_args.args[-1], docker.call_args.kwargs["timeout"]), ("30", 35))

    def test_failed_probe_records_allowlisted_diagnostics_before_backend_removal(self):
        a = self.register()
        error = m1.Problem("Docker exec failed: workspace readiness timed out: /global/health URLError")
        state = json.dumps({"Status": "exited", "OOMKilled": False, "ExitCode": 132, "Error": "private log data"})
        logs = SimpleNamespace(stdout=b"private document", stderr=b"Illegal instruction; private token")
        with patch.object(m1, "docker", side_effect=(error, state)), patch.object(m1.subprocess, "run", return_value=logs), \
                self.assertRaisesRegex(m1.Problem, "Illegal instruction") as caught:
            a.ready({"id": "fixture"}, {"id": "backend"})
        self.assertNotIn("private", str(caught.exception))
        note = m1.Registry(self.registry.root).workspace("dev-m1", "test-user-a").row["last_readiness_failure"]
        self.assertEqual((note["exit_code"], note["oom_killed"], note["backend_log_signals"]), (132, False, ["Illegal instruction"]))
        self.assertEqual((note["probe_path"], note["probe_result"]), ("/global/health", "URLError"))

    def test_only_quota_backend_overrides_healthcheck_and_uses_bounded_stderr_instead_of_file_logger(self):
        quota = self.register()
        self.registry.data["readiness_seconds"] = 150
        with TemporaryDirectory() as directory:
            registry = m1.Registry(Path(directory))
            registry.data = {"format": 1, "deployment": "legacy", "workspaces": {}, "template_version": m1.VERSION,
                             "image": "local-image"}
            ordinary = registry.workspace("dev-legacy", "member", create=True)
            for ws in (ordinary, quota):
                with self.subTest(user=ws.user), patch.object(ws, "m3_overlay", return_value=({}, {})), \
                        patch.object(m1, "docker", return_value="container-id") as docker:
                    ws.create_pair()
                    fixture, backend = (call.args for call in docker.call_args_list)
                    self.assertNotIn("--health-cmd", fixture)
                    self.assertNotIn("--print-logs", fixture)
                    if ws is ordinary:
                        self.assertNotIn("--health-cmd", backend)
                        self.assertNotIn("--print-logs", backend)
                        self.assertNotIn("--log-level", backend)
                    else:
                        cli = backend[backend.index("/app/backend/cli/src/index.ts") + 1:]
                        self.assertEqual(cli[:4], ("--print-logs", "--log-level", "ERROR", "serve"))
                        self.assertIn("max-size=2m", backend)
                        self.assertIn("max-file=1", backend)
                        command = backend[backend.index("--health-cmd") + 1]
                        self.assertIn("exec python3 -ISB", command)
                        self.assertIn(m1.TOKEN_DIR + "/token", command)
                        self.assertNotIn((ws.secret_dir / "token").read_text().strip(), repr(backend))
                        self.assertTrue(command.endswith(" >/dev/null 2>&1"))
                        for flag, value in [("--health-interval", "30s"), ("--health-timeout", "10s"),
                                            ("--health-start-period", "150s"), ("--health-retries", "3")]:
                            self.assertEqual(backend[backend.index(flag) + 1], value)

    def test_container_health_reads_the_current_token_and_requires_a_healthy_authenticated_response(self):
        for token, status, payload, expected in [("first", 200, b'{"healthy":true}', 0),
                                                  ("rotated", 200, b'{"healthy":false}', 1),
                                                  ("revoked", 401, b'', 1)]:
            with self.subTest(status=status, token=token), patch("builtins.open", mock_open(read_data=token + "\n")) as reader, \
                    patch("http.client.HTTPConnection") as client, self.assertRaises(SystemExit) as result:
                reply = client.return_value.getresponse.return_value
                reply.status = status
                reply.read.return_value = payload
                exec(m1.QUOTA_HEALTH, {})
            self.assertEqual(result.exception.code, expected)
            reader.assert_called_once_with("/run/nexpoly-m1/token", encoding="ascii")
            client.assert_called_once_with("127.0.0.1", 4096, timeout=5)
            client.return_value.request.assert_called_once_with("GET", "/global/health", headers={"Authorization": "Bearer " + token})

    def test_reuse_checks_pinned_runtime_logging_and_health_options_but_old_pairs_can_be_recycled(self):
        a = self.register()
        backend = {"id": "b", "state": "exited"}
        fixture = {"id": "f", "state": "exited"}
        def mount(target, path, rw=False):
            return {"Type": "bind", "Source": str(path), "Destination": target, "RW": rw, "Propagation": "rprivate"}
        bm = [mount(m1.MOUNT, Path(a.quota_binding["mount"]) / "workspace", True), mount(m1.TOKEN_DIR, a.secret_dir),
              mount(m1.CONTROL, a.control), mount("/m1-config.json", a.registry.root / "config.json")]
        fm = [mount(m1.TOKEN_DIR, a.secret_dir), mount("/fixture.py", a.registry.root / "fixture.py")]
        config = {"cmd": a.backend_command(), "env": ["HOME=/openscience/home"], "health": {"Test": ["CMD-SHELL", m1.QUOTA_HEALTH_CMD]}}
        def inspected(*args):
            fmt, cid = args[-2:]
            if cid == "f":
                return "\n".join((json.dumps(fm), "none", "65534:65534", "{}"))
            data = [json.dumps(bm), "container:f", a.identity, "{}"]
            if ".Config.Env" in fmt:
                data += [json.dumps(config[k]) for k in ("cmd", "env", "health")]
            return "\n".join(data)
        with patch.object(a, "m3_overlay", return_value=({}, {})), patch.object(m1, "docker", side_effect=inspected):
            a.verify_pair(backend, fixture)
            for key, stale in [("cmd", ["bun", "serve"]), ("env", ["BUN_JSC_custom=1"]), ("health", None)]:
                with self.subTest(key=key, stale=stale), patch.dict(config, {key: stale}):
                    with self.assertRaisesRegex(m1.Problem, "recycle"):
                        a.verify_pair(backend, fixture)
                    a.verify_pair(backend, fixture, require_current=False)


if __name__ == "__main__":
    main()

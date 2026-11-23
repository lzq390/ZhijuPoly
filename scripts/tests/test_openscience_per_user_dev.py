"""Recovery and fail-closed cases complementary to the two-user Docker acceptance."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openscience_per_user_dev as m1


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


if __name__ == "__main__":
    main()

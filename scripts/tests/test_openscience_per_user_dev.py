"""Recovery and fail-closed cases complementary to the two-user Docker acceptance."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
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


if __name__ == "__main__":
    main()

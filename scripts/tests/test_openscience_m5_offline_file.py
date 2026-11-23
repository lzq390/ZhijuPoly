"""Cold recovery must retain the exact owner and reviewed version of a file."""

import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import openscience_m5_offline_file as recovery

client = recovery.manager.pilot_module("offline_file")


class OfflineFileTest(unittest.TestCase):
    def setUp(self):
        area = TemporaryDirectory()
        self.addCleanup(area.cleanup)
        self.root = Path(area.name)
        self.path = "same-project/release.txt"
        self.folder = self.root / "files/projects/same-project"
        self.folder.mkdir(parents=True)
        self.file = self.folder / "release.txt"
        self.file.write_bytes(b"user content\x00\n")
        self.owner = {"uid": os.getuid(), "user_id": "test-user-a", "filesystem_uuid": "disk-a"}

    def test_export_verify_copy_and_delete_only_the_version_read_by_this_owner(self):
        output = io.BytesIO()
        read = client.project_file(self.root, self.path, self.owner, None, output, 1024)
        self.assertEqual(output.getvalue(), self.file.read_bytes())
        self.assertEqual(read["sha256"], hashlib.sha256(output.getvalue()).hexdigest())
        for changed in ({"user_id": "test-user-b"}, {"filesystem_uuid": "replacement"}):
            with self.assertRaisesRegex(ValueError, "confirmation"):
                client.project_file(self.root, self.path, {**self.owner, **changed}, read["confirmation"], None)
        self.file.write_bytes(b"new version")
        with self.assertRaisesRegex(ValueError, "confirmation"):
            client.project_file(self.root, self.path, self.owner, read["confirmation"], None)
        new = client.project_file(self.root, self.path, self.owner, None, io.BytesIO(), 1024)
        deleted = client.project_file(self.root, self.path, self.owner, new["confirmation"], None)
        self.assertTrue(deleted["deleted"])
        self.assertFalse(self.file.exists())

    def test_checks_destination_budget_and_confines_operations_to_regular_project_files(self):
        output = io.BytesIO()
        with self.assertRaisesRegex(ValueError, "reserve"):
            client.project_file(self.root, self.path, self.owner, None, output, 1)
        self.assertEqual(output.getvalue(), b"")
        (self.folder / "link").symlink_to(self.file)
        (self.folder.parent / "other").symlink_to(self.folder)
        for path in ("same-project/link", "other/release.txt", "same-project/../release.txt",
                     "/same-project/release.txt", "same-project/.private"):
            with self.subTest(path=path), self.assertRaises((OSError, ValueError)):
                client.project_file(self.root, path, self.owner, None, output, 1024)
        os.link(self.file, self.folder / "hardlink")
        with self.assertRaisesRegex(ValueError, "single-link"):
            client.project_file(self.root, self.path, self.owner, None, output, 1024)
        self.assertTrue(self.file.exists())

    def test_host_rejects_running_workspace_attached_alias_and_non_low_disk(self):
        ws = Mock(env="dev-m1", user="test-user-a", volume="private-a")
        ws.registry.data = {"format": 5}
        ws.quota_status.return_value = {"filesystem_uuid": "disk-a"}
        with patch.object(recovery.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "Recycle"):
                recovery.recover(ws, self.path, "confirmation", None)
            ws.resource.return_value = None
            with patch.object(recovery.manager, "docker", side_effect=["private-a\nalias-a", "", "container"]):
                with self.assertRaisesRegex(ValueError, "attached"):
                    recovery.recover(ws, self.path, "confirmation", None)
            ws.registry.disk_watermark.return_value = {"state": "normal"}
            with patch.object(recovery.manager, "docker", side_effect=["private-a", ""]):
                with self.assertRaisesRegex(ValueError, "verified low"):
                    recovery.recover(ws, self.path, "confirmation", None)
            run.assert_not_called()

    def test_read_and_delete_use_the_owner_helper_and_correct_mount_modes(self):
        nfs = SimpleNamespace(
            __file__=recovery.manager.pilot_module("quota_nfs").__file__,
            CLIENT_CHECK="root = sys.argv[1]\n",
            client_args=lambda *_: (str(self.root), "src-a", "10000", "workspace-a", str(os.getuid())),
        )
        ws = Mock(env="dev-m1", user="test-user-a", volume="private-a", row={"id": "workspace-a", "uid": os.getuid()},
                  identity=f"{os.getuid()}:{os.getgid()}", deployment="dev-test")
        ws.registry.data = {"format": 5}
        ws.quota_status.return_value = {"filesystem_uuid": "disk-a"}
        ws.resource.return_value = None
        ws.data_mount.return_value = "type=volume,src=private-a,dst=/data"
        ws.label_flags.return_value = []
        ws.registry.disk_watermark.return_value = {"state": "low"}
        mounts = []
        run = subprocess.run

        def helper(command, **kwargs):
            mounts.append(command[command.index("--mount") + 1])
            self.assertIn("--network=none", command)
            self.assertIn("--cap-drop=ALL", command)
            self.assertEqual(command[command.index("--user") + 1], ws.identity)
            return run([sys.executable, *command[command.index("python") + 1:]], **kwargs)

        with patch.object(recovery.manager, "pilot_module", return_value=nfs), \
                patch.object(recovery.manager, "docker", side_effect=["private-a", ""] * 2), \
                patch.object(recovery.subprocess, "run", side_effect=helper):
            copy = self.root / "result"
            with copy.open("xb") as output:
                read = recovery.recover(ws, self.path, None, output, 1024)
            self.assertEqual(copy.read_bytes(), self.file.read_bytes())
            removed = recovery.recover(ws, self.path, read["confirmation"], subprocess.DEVNULL)
            self.assertTrue(removed["deleted"])
            self.assertFalse(self.file.exists())
            self.assertEqual(mounts, [ws.data_mount.return_value + ",readonly", ws.data_mount.return_value])

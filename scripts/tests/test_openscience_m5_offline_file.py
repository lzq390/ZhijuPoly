"""Cold recovery must retain the exact owner and reviewed version of a file."""

import base64
import errno
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import time
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

    def browse(self, action, **data):
        return client.browse_file(self.root, self.owner, action, data, time.time() * 1000 + 20_000)

    def test_paged_directory_hides_links_and_internal_files(self):
        self.assertEqual(self.browse("list", path="", after="")["entries"],
                         [{"name": "same-project", "kind": "directory", "size": None}])
        (self.folder / ".state").touch()
        (self.folder / "shortcut").symlink_to(self.file)
        (self.folder / "subfolder").mkdir()
        for i in range(40):
            (self.folder / f"item{i:02d}").touch()
        first = self.browse("list", path="same-project", after="")
        second = self.browse("list", path="same-project", after=first["next"])
        names = [row["name"] for row in first["entries"] + second["entries"]]
        self.assertEqual(len(first["entries"]), client.PAGE_SIZE)
        self.assertCountEqual(names, [f"item{i:02d}" for i in range(40)] + ["release.txt", "subfolder"])
        self.assertIsNone(second["next"])

    def test_large_directory_resumes_with_bounded_work_even_across_hidden_entries(self):
        for i in range(300):
            (self.folder / f".hidden-{i}").touch()
        expected = [f"visible-{i:03d}" for i in range(65)]
        for name in expected:
            (self.folder / name).touch()
        visited = []
        real_entries = client.directory_entries
        def tracked(parent, cookie):
            page = []
            visited.append((cookie, page))
            for name, offset in real_entries(parent, cookie):
                page.append(offset)
                yield name, offset
        names, cursor = [], ""
        with patch.object(client, "directory_entries", side_effect=tracked), patch.object(client, "SCAN_LIMIT", 7):
            while True:
                result = self.browse("list", path="same-project", after=cursor)
                names.extend(row["name"] for row in result["entries"])
                cursor = result["next"]
                if not cursor:
                    break
                self.assertLess(len(visited), 100)
        self.assertCountEqual(names, ["release.txt", *expected])
        self.assertTrue(all(len(records) <= 7 for _, records in visited))
        for (_, prior), (start, _) in zip(visited, visited[1:]):
            self.assertEqual(start, prior[-1])

    def test_page_can_checkpoint_before_the_grant_deadline(self):
        for i in range(20):
            (self.folder / f"item{i:02d}").touch()
        # Less than a second remains. Preserve progress including hidden or dot entries.
        fresh = {"path":"same-project", "after":""}
        result = client.browse_file(self.root, self.owner, "list", fresh, time.time()*1000 + 800)
        self.assertLessEqual(len(result["entries"]), 1)
        self.assertTrue(result["next"])
        second = self.browse("list", path="same-project", after=result["next"])
        self.assertCountEqual([row["name"] for row in result["entries"] + second["entries"]],
                              [f"item{i:02d}" for i in range(20)] + ["release.txt"])

    def test_directory_cursor_binds_owner_directory_and_snapshot_and_resumes_legacy_pages(self):
        for i in range(70):
            (self.folder / f"item{i:02d}").touch()
        names, cursor = [], "item29"
        while True:
            result = self.browse("list", path="same-project", after=cursor)
            names.extend(row["name"] for row in result["entries"])
            cursor = result["next"]
            if not cursor:
                break
        self.assertCountEqual(names, [f"item{i:02d}" for i in range(30, 70)] + ["release.txt"])
        cursor = self.browse("list", path="same-project", after="")["next"]
        other = {**self.owner, "user_id":"test-user-b"}
        for owner, path in ((other, "same-project"), (self.owner, "")):
            with self.subTest(owner=owner, path=path), self.assertRaises(FileExistsError):
                client.browse_file(self.root, owner, "list", {"path":path, "after":cursor}, time.time()*1000+2000)
        with patch.object(client.os, "lseek", side_effect=OSError(errno.ESTALE, "stale NFS cookie")):
            with self.assertRaises(FileExistsError):
                self.browse("list", path="same-project", after=cursor)
        (self.folder / "new").touch()
        with self.assertRaises(FileExistsError):
            self.browse("list", path="same-project", after=cursor)

    def test_paged_read_and_delete_are_bound_to_file_version_owner_and_deadline(self):
        content = b"a" * (client.READ_BYTES - 1) + "中文".encode()
        self.file.write_bytes(content)
        first = self.browse("read", path=self.path, offset=0)
        second = self.browse("read", path=self.path, offset=first["next"], version=first["version"])
        self.assertEqual(base64.b64decode(second["content"]).decode(), "中文")
        self.assertEqual(base64.b64decode(first["content"]) + base64.b64decode(second["content"]), content)
        self.assertIsNone(second["next"])
        data = {"path": self.path, "version": first["version"]}
        with self.assertRaises(FileExistsError):
            client.browse_file(self.root, {**self.owner, "user_id": "test-user-b"}, "delete", data, time.time()*1000+2000)
        with self.assertRaises(TimeoutError):
            client.browse_file(self.root, self.owner, "delete", data, 0)
        self.file.write_bytes(b"replacement")
        with self.assertRaises(FileExistsError):
            self.browse("delete", **data)
        self.assertEqual(self.file.read_bytes(), b"replacement")
        self.assertEqual(self.browse("delete", path=self.path, version=self.browse("read", path=self.path, offset=0)["version"]),
                         {"deleted": True})
        with self.assertRaises(FileNotFoundError):
            self.browse("delete", **data)

    def test_public_contract_has_no_owner_selector(self):
        for action, data in [("list", {"path": "", "after": "", "user_id": "test-user-b"}),
                             ("delete", {"path": self.path}), ("read", {"path": self.path, "offset": -1})]:
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "cleanup_invalid"):
                recovery.web_cleanup(self.root, "test-user-a", action, data, int(time.time()*1000)+2000)

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
        ws.registry.disk_watermark.return_value = {"state": "low"}
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

    def test_cold_web_reclaims_only_terminal_backend_and_its_registered_companion(self):
        ws = Mock(env="dev-m1", user="test-user-a", volume="private-a", deployment="dev-test",
                  row={"id":"workspace-a", "uid":os.getuid()})
        ws.registry.data = {"format": 5}
        ws.quota_status.return_value = {"filesystem_uuid": "disk-a"}
        ws.runtime = self.root / "runtime"
        ws.remove_pair = recovery.manager.Workspace.remove_pair.__get__(ws)
        ws.registry.disk_watermark.return_value = {"state": "normal"}
        with self.assertRaises(recovery.CleanupUnavailable):
            recovery.cold_owner(ws, True, reclaim_stopped=True)
        ws.retire_generation.assert_not_called()
        ws.registry.disk_watermark.return_value = {"state": "low"}
        for backend, fixture in (("running", "exited"), ("paused", "running"), ("exited", "restarting")):
            with self.subTest(backend=backend, fixture=fixture), patch.object(recovery.manager, "docker") as docker:
                ws.resource.side_effect = {"backend":{"state":backend}, "fixture":{"state":fixture}}.get
                with self.assertRaises(recovery.CleanupUnavailable):
                    recovery.cold_owner(ws, True, reclaim_stopped=True)
                ws.retire_generation.assert_not_called()
                docker.assert_not_called()
        ws.resource.side_effect = {"backend":{"state":"exited", "id":"backend-a"},
                                   "fixture":{"state":"running", "id":"fixture-a"}}.get
        with patch.object(recovery.manager, "docker", side_effect=["", "", "", "private-a", ""]) as docker:
            owner = recovery.cold_owner(ws, True, reclaim_stopped=True)
        self.assertEqual(owner["user_id"], "test-user-a")
        ws.retire_generation.assert_called_once_with()
        self.assertEqual([call.args for call in docker.call_args_list[:3]], [
            ("rm", "backend-a"), ("stop", "--time", "10", "fixture-a"), ("rm", "fixture-a")])
        ws.registry.disk_watermark.side_effect = [{"state":"low"}, {"state":"normal"}]
        with patch.object(recovery.manager, "docker", side_effect=["", "", "", "private-a", ""]):
            with self.assertRaisesRegex(recovery.CleanupUnavailable, "verified low"):
                recovery.cold_owner(ws, True, reclaim_stopped=True)

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

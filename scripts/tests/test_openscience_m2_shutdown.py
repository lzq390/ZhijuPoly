"""The development stack must leave a still-busy workspace eligible for takeover."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openscience_m2_dev as stack


class WorkspaceShutdownTest(TestCase):
    def test_temporary_busy_user_does_not_block_other_users(self):
        attempts = []
        def recycle(user):
            attempts.append(user)
            busy = len(attempts) == 1
            return {"volume": user, "state": "running" if busy else "recycled", "reason": "busy"}
        with patch.object(stack.time, "sleep"):
            stack.recycle_workspaces({"A": "A", "B": "B"}, recycle)
        self.assertEqual(attempts, ["A", "B", "A"])

    def test_expired_shutdown_grace_reports_remaining_workspace(self):
        attempts = []
        def recycle(user):
            attempts.append(user)
            return {"volume": user, "state": "running" if user == "A" else "recycled", "reason": "pending_input"}
        with self.assertRaisesRegex(RuntimeError, "same configuration \\(A: pending_input"):
            stack.recycle_workspaces({"A": "A", "B": "B"}, recycle, grace_seconds=0)
        self.assertEqual(attempts, ["A", "B"])

    def test_same_template_reopens_exact_mounts_after_old_transport_disappears(self):
        args = SimpleNamespace(m3_gateway_image="image", m3_adapters=False, m4=False, registry=Path("registry"))
        def prepare(code, registry, adapters, m4):
            code.mkdir()
            (code / "config.json").write_text(json.dumps([adapters, m4]))
        with TemporaryDirectory() as root, patch.object(stack.runpy, "run_path", return_value={"prepare": prepare}):
            state = Path(root)
            with TemporaryDirectory() as first:
                overlay = stack.workspace_overlay(args, state, Path(first))
                self.assertEqual((Path(first) / "m3").resolve(), overlay)
            self.assertTrue((overlay / "code/config.json").is_file())
            with TemporaryDirectory() as second:
                self.assertEqual(stack.workspace_overlay(args, state, Path(second)), overlay)
            row = {"state": "running", "workspace": "test", "container_id": "original"}
            mounts = json.dumps([{"Destination": "/m3-code", "Source": str(overlay / "code")},
                                 {"Destination": "/m3-peer", "Source": str(overlay / "test")}])
            with patch.object(stack, "run", return_value=mounts), TemporaryDirectory() as third:
                self.assertTrue(stack.attachment_matches(row, overlay))
                args.m4 = True
                changed = stack.workspace_overlay(args, state, Path(third))
                self.assertFalse(stack.attachment_matches(row, changed))
                self.assertFalse(stack.attachment_matches(row, None))


class QuotaFilesTest(TestCase):
    def bindings(self):
        return [
            {"environment": "dev-m1", "user_id": "test-user-" + who, "uid": uid,
             "storage_backend": "nfs-ext4-user-quota",
             "quota": {"uid": uid, "accounting": True, "enforced": True, "hard_bytes": 2_000_000_000}}
            for who, uid in (("a", 101), ("b", 102))
        ]

    def test_native_router_requires_both_owners_and_enforced_decimal_limit(self):
        stack.require_quota_workspaces(self.bindings())
        for group, field, value in (
            ("binding", "environment", "other"),
            ("binding", "user_id", "test-user-a"),
            ("binding", "storage_backend", "ext4-user-quota"),
            ("quota", "uid", 101),
            ("quota", "enforced", False),
            ("quota", "accounting", False),
            ("quota", "hard_bytes", 2 * 1024 ** 3),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                rows = self.bindings()
                (rows[1] if group == "binding" else rows[1]["quota"])[field] = value
                stack.require_quota_workspaces(rows)
        with self.assertRaises(RuntimeError):
            stack.require_quota_workspaces(self.bindings()[:1])


if __name__ == "__main__":
    main()

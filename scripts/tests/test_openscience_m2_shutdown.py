"""The development stack must leave a still-busy workspace eligible for takeover."""
import json
import hashlib
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openscience_m2_dev as stack
import openscience_per_user_dev as manager


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
        args = SimpleNamespace(m3_gateway_image="image", m3_adapters=False, m4=False, registry=Path("registry"), verify_retries=False)
        def prepare(code, registry, adapters, m4, verify_retries):
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

    def test_quota_image_switch_preserves_legacy_mounts_and_checks_each_candidate(self):
        files = manager.pilot_module("quota_web_files")
        original, candidate, incompatible = ("sha256:" + ch * 64 for ch in "abc")
        raw = b"fixture source"
        digest = hashlib.sha256(raw).hexdigest()
        patched = hashlib.sha256(b"fixture source patched").hexdigest()
        def extract(args):
            return b"changed" if args[-2] == incompatible else raw

        with TemporaryDirectory() as tmp, patch.object(files.subprocess, "check_output", side_effect=extract) as docker, \
                patch.object(files, "patch", side_effect=lambda name, text: text + " patched"), \
                patch.object(files, "ORIGINAL", {"index.ts": digest, "file.ts": digest}), \
                patch.object(files, "PATCHED", {"index.ts": patched, "file.ts": patched}):
            root = Path(tmp)
            files.prepare(root, original)
            legacy = root / "quota-web-code"
            files.overlay_path(root, original).rename(legacy)
            old = files.mounts(root, original)
            new = files.prepare(root, candidate)
            self.assertNotEqual(old, new)
            self.assertEqual(files.prepare(root, original), old)
            self.assertEqual(files.mounts(root, candidate), new)
            self.assertEqual(docker.call_count, 4)
            with self.assertRaisesRegex(ValueError, "Unexpected November source"):
                files.prepare(root, incompatible)
            self.assertFalse(files.overlay_path(root, incompatible).exists())
            self.assertEqual(files.mounts(root, original), old)
            marker = files.overlay_path(root, candidate) / "image-id"
            marker.unlink()
            marker.symlink_to(legacy / "image-id")
            with self.assertRaisesRegex(ValueError, "image marker"):
                files.mounts(root, candidate)

    def test_scoped_upgrade_requires_the_reviewed_runtime(self):
        scoped = stack.runpy.run_path(str(stack.ROOT / "ops/openscience-m3-dev/workspace.py"))
        image = "sha256:" + "a" * 64
        original = {"Architecture": "amd64", "Os": "linux", "RootFS": {"Layers": ["original"]},
                    "Config": {"Entrypoint": None, "Labels": {}}}
        candidate = {**original, "Config": {**original["Config"], "Labels": {"build": "candidate"}}}
        def check(inspected):
            with patch.object(stack.subprocess, "check_output", return_value=json.dumps([original, inspected])):
                scoped["reviewed_image"](image)

        check(candidate)
        for bad in ({**candidate, "RootFS": {"Layers": ["modified"]}},
                    {**candidate, "Config": {**candidate["Config"], "Entrypoint": ["changed"]}}):
            with self.assertRaisesRegex(ValueError, "explicitly reviewed"):
                check(bad)

    def test_combined_retry_drill_uses_local_titles_and_keeps_the_real_primary(self):
        scoped = stack.runpy.run_path(str(stack.ROOT / "ops/openscience-m3-dev/workspace.py"))
        normal = scoped["model_settings"]()
        self.assertEqual(normal["small_model"], normal["model"])
        self.assertNotIn("m0fixture", normal["provider"])
        fixture = {"options": {"baseURL": "http://127.0.0.1:18080/v1"}}
        drill = scoped["model_settings"](fixture)
        self.assertEqual(drill["small_model"], "m0fixture/m0")
        self.assertEqual(drill["model"], normal["model"])
        self.assertEqual(drill["provider"], {**normal["provider"], "m0fixture": fixture})


if __name__ == "__main__":
    main()

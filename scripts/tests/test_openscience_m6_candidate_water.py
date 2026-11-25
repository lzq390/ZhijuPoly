"""Candidate backing-image checks, group allocation leases and threshold update recovery."""

import fcntl
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SCRIPTS), str(SCRIPTS.parent / "ops/openscience-per-user-dev")]
import candidate_gate as guest  # noqa: E402
import candidate_water as host  # noqa: E402
import quota_nfs  # noqa: E402


class CandidateGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.policy = dict(
            filesystem_uuid=str(uuid4()), low_bytes=128, recover_bytes=192
        )
        self.claim = dict(
            deployment=uuid4().hex,
            phase="src",
            registry_sha256="d" * 64,
            filesystem_uuids={role: str(uuid4()) for role in "ab"},
        )
        self.registry = Mock(root=self.root, data=dict(native_claim=self.claim))
        self.registry.quota_keys.return_value = ["a", "b"]

    def test_backing_image_replacement_refused_even_on_same_host_partition(self):
        image = self.root / "src-a.raw"
        image.write_bytes(b"test")
        info = image.stat()
        pin = dict(
            file=str(image),
            inode=info.st_ino,
            device=f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}",
        )
        free, total = quota_nfs.host_space(pin)
        self.assertGreater(total, free)
        with patch.object(quota_nfs, "host_space", return_value=(free, total)):
            self.assertEqual(quota_nfs.host_free_mib(pin), free // 1024**2)
        image.rename(self.root / "previous")
        image.write_bytes(b"other")
        with self.assertRaisesRegex(ValueError, "differs"):
            quota_nfs.host_space(pin)

    def test_absent_supervisor_and_stale_lock_file_do_not_authorize_registration(self):
        self.assertFalse(host.polling(self.root))
        with self.assertRaisesRegex(ValueError, "updated candidate supervisor"):
            host.configure(self.registry, self.policy, None)
        with (self.root / "host-watermark-poller.lock").open("a") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertTrue(host.polling(self.root))
        self.assertFalse(host.polling(self.root))

    def test_failed_install_is_retried_with_pinned_configuration(self):
        failed = {"errors": {"b": "quota"}, "workspaces": {}}
        ok = {"errors": {}, "workspaces": {"a": {}, "b": {}}}
        with (
            patch.object(host, "polling", return_value=True),
            patch.object(host.CandidatePool, "space"),
            patch.object(
                host.CandidatePool, "guest", side_effect=[failed, ok]
            ) as install,
            patch.object(host, "observe", return_value={"state": "normal"}),
        ):
            with self.assertRaisesRegex(ValueError, "Both candidate watchdog"):
                host.configure(self.registry, self.policy, None)
            self.assertEqual(self.registry.data["format"], 5)
            self.assertFalse((self.root / "host-watermark/installed.json").exists())
            host.configure(self.registry, self.policy, None)
            changed = {**self.policy, "low_bytes": 150}
            host.configure(self.registry, changed, None)
            self.assertEqual(install.call_count, 2)
            self.assertEqual(self.registry.data["disk_watermark"], changed)
            with self.assertRaisesRegex(ValueError, "Different host filesystem"):
                host.configure(
                    self.registry, {**changed, "filesystem_uuid": str(uuid4())}, None
                )
            self.assertEqual(self.registry.data["disk_watermark"], changed)

    def test_guest_lease_expiry_and_partial_open(self):
        ok = {"errors": {}, "workspaces": {}}
        with (
            patch.object(guest, "RUN", self.root),
            patch.object(guest, "apply", return_value=ok) as apply,
        ):
            with patch.object(guest.time, "monotonic", return_value=100):
                guest.guarded(self.claim, "open")
            with patch.object(
                guest.time, "monotonic", return_value=100 + guest.LEASE_SECONDS - 1
            ):
                guest.guarded(self.claim, "expiry")
            self.assertEqual(apply.call_count, 1)
            with patch.object(
                guest.time, "monotonic", return_value=100 + guest.LEASE_SECONDS
            ):
                guest.guarded(self.claim, "expiry")
            apply.assert_called_with(self.claim, "block", enable=True)
            self.assertFalse((self.root / "deadline").exists())
        with (
            patch.object(guest, "owners", return_value=dict(a="A", b="B")),
            patch.object(
                guest, "volume", side_effect=lambda role, *args: (role, {"role": role})
            ),
            patch.object(guest.kernel, "set_group_gate") as gate,
        ):

            def update(node, row, mode):
                if node == "b" and mode == "open":
                    raise ValueError("injected open failure")

            gate.side_effect = update
            result = guest.apply(self.claim, "open")
            self.assertIn("b", result["errors"])
            self.assertEqual(
                [(c.args[0], c.args[2]) for c in gate.call_args_list],
                [("a", "open"), ("b", "open"), ("a", "block"), ("b", "block")],
            )

    def test_bad_host_stat_still_blocks_guest_allocations(self):
        pool = host.CandidatePool(self.registry, self.policy)
        with (
            patch.object(
                pool, "guest", return_value=dict(workspaces={}, errors={})
            ) as rpc,
            patch.object(pool, "space", side_effect=OSError("missing host medium")),
        ):
            with self.assertRaises(OSError):
                pool.gate("open")
            rpc.assert_not_called()
            with self.assertRaises(OSError):
                pool.gate("block")
            rpc.assert_called_once_with("block")

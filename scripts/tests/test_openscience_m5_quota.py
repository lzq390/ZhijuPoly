"""Regression coverage for the identity-bound ext4 quota policy in the dev-m1 pilot."""

import ctypes
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[2] / "ops/openscience-per-user-dev/quota.py"
SPEC = importlib.util.spec_from_file_location("dev_m1_quota", MODULE)
quota = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(quota)

A = {"environment": "dev-m1", "user_id": "test-user-a", "workspace": "a" * 32, "uid": 230001}
B = {"environment": "dev-m1", "user_id": "test-user-b", "workspace": "b" * 32, "uid": 230002}
SOURCE = "ad45f49a-e314-422b-826b-eaa5e2891a94"
TARGET = "2b1297ff-7b2b-44d4-994e-6dcc63407e47"


class QuotaTests(unittest.TestCase):
    def test_candidate_label_binds_account_workspace_uid_and_phase(self):
        account = dict(A, environment="deploy-m6", user_id="e89ff385-a3db-4b4d-91ad-564f9b286e9e")
        label = quota.volume_label(account, "src")
        self.assertEqual(len(label), 16)
        self.assertEqual(quota.volume_label(dict(account, gid=A["uid"]), "src"), label)
        for other in (dict(account, workspace=B["workspace"]), dict(account, uid=B["uid"]),
                      dict(account, user_id="b60ff385-a3db-4b4d-91ad-564f9b286e9e")):
            self.assertNotEqual(quota.volume_label(other, "src"), label)
        self.assertNotEqual(quota.volume_label(account, "dst"), label)
        self.assertEqual(quota.volume_label(B, "src"), "m5quota-src-b")
        with self.assertRaises(quota.QuotaError):
            quota.volume_label(dict(account, user_id="test-user-a"), "src")

    def test_linux_uapi_layout(self):
        self.assertEqual(ctypes.sizeof(quota._DiskQuota), 72)
        self.assertEqual(ctypes.sizeof(quota._QuotaStatusV1), 160)
        self.assertEqual(quota._QuotaStatusV1.flags.offset, 2)
        self.assertEqual(quota._hard_kib(2_000_000_000), 1_953_125)

    def test_restore_uses_kernel_limit_and_preserves_usage(self):
        state = {"source": 0, "target": 0}
        writes = []

        def fake_filesystem(mount, owner, role=None):
            return mount, {"filesystem_uuid": SOURCE if mount == "source" else TARGET}

        def fake_kernel(device, operation, uid, value):
            self.assertEqual(uid, A["uid"])
            if operation == quota.Q_GETQUOTA:
                value.block_hard = state[device]
                value.used_bytes = 4096
            else:
                self.assertEqual(operation, quota.Q_SETQUOTA)
                self.assertEqual(value.valid, quota.QIF_BLIMITS)
                writes.append((device, value.block_hard))
                state[device] = value.block_hard

        with patch.object(quota, "_filesystem", fake_filesystem), patch.object(quota, "_call", fake_kernel):
            quota.install("source", A)
            state["source"] = 32768  # Emulate an up-to-date kernel limit different from the install default.
            saved = quota.snapshot("source", A)
            pin = quota.snapshot_digest(saved)
            restored = quota.restore("target", A, saved, pin)
            self.assertEqual(restored["hard_bytes"], 32 * 1024**2)
            self.assertEqual(restored["used_bytes"], 4096)
            self.assertEqual(quota.restore("target", A, saved, pin), restored)
            self.assertEqual(writes, [("source", 1_953_125), ("target", 32768)])
            state["target"] = 7000
            with self.assertRaisesRegex(quota.QuotaError, "conflicts"):
                quota.restore("target", A, saved, pin)
            self.assertEqual(len(writes), 2)

    def test_wrong_identity_pin_or_unsupported_limits_do_not_allocate(self):
        saved = {"format": quota.FORMAT, "identity": A, "hard_bytes": 8192, "source_filesystem_uuid": SOURCE}
        with patch.object(quota, "_filesystem") as device:
            for binding in [B, dict(A, uid=234999), dict(A, environment="dev-other"), dict(A, workspace="c" * 32)]:
                with self.subTest(binding=binding), self.assertRaises(quota.QuotaError):
                    quota.restore("/unused", binding, saved, quota.snapshot_digest(saved))
            with self.assertRaises(quota.QuotaError):
                quota.restore("/unused", A, saved, "0" * 64)
            for value in [False, 0, 21, quota.PILOT_HARD_BYTES + 1024]:
                altered = dict(saved, hard_bytes=value)
                with self.subTest(value=value), self.assertRaises(quota.QuotaError):
                    quota.restore("/unused", A, altered, quota.snapshot_digest(altered))
            device.assert_not_called()

    def test_source_uuid_cannot_be_restored_in_place(self):
        saved = {"format": quota.FORMAT, "identity": A, "hard_bytes": 8192, "source_filesystem_uuid": SOURCE}
        with patch.object(quota, "_filesystem", return_value=("unused", {"filesystem_uuid": SOURCE})), \
                patch.object(quota, "_call") as kernel, self.assertRaisesRegex(quota.QuotaError, "independently"):
            quota.restore("/unused", A, saved, quota.snapshot_digest(saved))
        kernel.assert_not_called()

    def test_unscoped_mount_cannot_reach_kernel_quota_api(self):
        with patch.object(quota, "_call") as kernel:
            for path in ["/", Path(__file__).resolve().parent]:
                with self.subTest(path=path), self.assertRaises(quota.QuotaError):
                    quota.install(path, A)
            kernel.assert_not_called()


if __name__ == "__main__":
    unittest.main()

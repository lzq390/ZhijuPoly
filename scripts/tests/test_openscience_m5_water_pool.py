"""Guard the existing user quotas and reject foreign group policies during global allocation gating."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "ops/openscience-per-user-dev")
)
import water_pool as pool


class WaterPoolTest(unittest.TestCase):
    def test_reinstall_preserves_changed_user_data_and_checks_initial_receipt_binding(
        self,
    ):
        cfg = {
            "filesystem_uuid": "pool",
            "sources": {
                role: {
                    "uid": uid,
                    "filesystem_uuid": "disk-" + role,
                    "workspace": role,
                    "inventory": {"files": 1},
                }
                for role, uid in (("a", 222000), ("b", 222001))
            },
        }
        owners = {
            "users": {
                role: {"uid": values["uid"]} for role, values in cfg["sources"].items()
            }
        }
        inspect = Mock(return_value={"files": 1})

        def publish(path, data, mode):
            path.write_bytes(data)
            path.chmod(mode)

        with (
            TemporaryDirectory() as tmp,
            patch.object(pool, "INITIAL", Path(tmp) / "initial.json"),
            patch.object(pool, "POOL", Path("/proc")),
            patch.object(Path, "read_bytes", return_value=json.dumps(owners).encode()),
        ):
            pool.verify_initial(cfg, inspect, publish)
            self.assertEqual(inspect.call_count, 2)
            inspect.side_effect = AssertionError(
                "A verified workspace can have new files"
            )
            pool.verify_initial(cfg, inspect, publish)
            self.assertEqual(inspect.call_count, 2)
            cfg["sources"]["a"]["uid"] += 1
            with self.assertRaisesRegex(ValueError, "receipt"):
                pool.verify_initial(cfg, inspect, publish)

    def test_foreign_group_policy_and_disabled_enforcement_are_rejected(self):
        def observe(flags, hard):
            def call(device, command, gid, value):
                if command == pool.quota.Q_XGETQSTATV:
                    value.flags = flags
                else:
                    self.assertEqual(command, pool.quota.Q_GETQUOTA)
                    value.block_hard = hard

            with patch.object(pool, "group_call", side_effect=call):
                return pool.group_state(Path("/dev/loop0"), 42)

        self.assertEqual(observe(pool.GROUP_FLAGS, 0)["group_gate"], "open")
        with self.assertRaisesRegex(ValueError, "inactive"):
            observe(1 << 2, 0)
        with self.assertRaisesRegex(ValueError, "pre-existing"):
            observe(pool.GROUP_FLAGS, 99)

    def test_partial_block_is_repaired_and_open_clears_group_limits_only(self):
        devices = {
            "a": (Path("/dev/loop0"), {"uid": 450000, "group_gate": "block"}),
            "b": (Path("/dev/loop1"), {"uid": 450001, "group_gate": "partial"}),
        }
        limits = {450000: (1, 1), 450001: (1, 0)}
        writes = []

        def set_limit(device, command, gid, data):
            self.assertEqual(command, pool.quota.Q_SETQUOTA)
            self.assertEqual(data.valid, pool.LIMIT_MASK)
            writes.append((gid, device))
            limits[gid] = (data.block_hard, data.inode_hard)

        def read_limit(_device, uid):
            return {"group_gate": {(1, 1): "block", (0, 0): "open"}[limits[uid]]}

        with (
            patch.object(pool, "topology", return_value=({}, devices)),
            patch.object(pool, "observation", return_value={}),
            patch.object(pool, "group_call", side_effect=set_limit),
            patch.object(pool, "group_state", side_effect=read_limit),
        ):
            pool.gate("block")
            self.assertEqual(writes, [(450001, devices["b"][0])])
            self.assertEqual(set(limits.values()), {(1, 1)})
            pool.gate("open")
            self.assertEqual(len(writes), 3)
            self.assertEqual(set(limits.values()), {(0, 0)})


if __name__ == "__main__":
    unittest.main()

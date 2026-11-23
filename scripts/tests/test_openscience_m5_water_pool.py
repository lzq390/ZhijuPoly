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

    def test_a_foreign_workspace_uuid_never_reaches_the_kernel_gate(self):
        users = {
            role: {
                "environment": "dev-m1",
                "user_id": "test-user-" + role,
                "uid": uid,
                "workspace": role * 32,
            }
            for role, uid in (("a", 450000), ("b", 450001))
        }
        expected = {role: "pinned-" + role for role in users}
        for foreign in users:
            with self.subTest(foreign=foreign):
                cfg = {
                    "sources": {
                        role: {
                            "filesystem_uuid": "foreign"
                            if role == foreign
                            else expected[role]
                        }
                        for role in users
                    }
                }
                policy = {"claim": {"filesystem_uuids": expected}}
                verify = pool.workspace_volume

                def volume(cfg, role, owner, policy):
                    if role == foreign:
                        return verify(cfg, role, owner, policy)
                    return role, {"group_gate": "open", "uid": owner["uid"]}

                with (
                    patch.object(pool, "source_layout", return_value=(cfg, users)),
                    patch.object(pool, "workspace_volume", side_effect=volume),
                    patch.object(pool, "group_call") as write,
                    patch.object(
                        pool, "group_state", return_value={"group_gate": "block"}
                    ),
                    patch.object(pool, "observation", return_value={}),
                    self.assertRaises(pool.IncompleteBlock) as incomplete,
                ):
                    pool.gate("block", policy)
                self.assertEqual(incomplete.exception.errors, {foreign: "ValueError"})
                self.assertEqual(
                    [c.args[0] for c in write.call_args_list],
                    [r for r in users if r != foreign],
                )

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
            patch.object(pool, "source_layout", return_value=({}, devices)),
            patch.object(
                pool,
                "workspace_volume",
                side_effect=lambda cfg, role, device, policy: device,
            ),
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

    def test_failed_open_returns_previously_opened_peer_to_block(self):
        users = {"a": 450000, "b": 450001}
        for failed in users:
            with self.subTest(failed=failed):
                modes = dict.fromkeys(users, "block")

                def device(cfg, role, uid, policy):
                    return role, {"uid": uid, "group_gate": modes[role]}

                def update(role, command, uid, limits):
                    if role == failed and not limits.block_hard:
                        raise OSError("open failed")
                    modes[role] = "block" if limits.block_hard else "open"

                with (
                    patch.object(pool, "source_layout", return_value=({}, users)),
                    patch.object(pool, "workspace_volume", side_effect=device),
                    patch.object(pool, "group_call", side_effect=update),
                    patch.object(
                        pool,
                        "group_state",
                        side_effect=lambda role, uid: {"group_gate": modes[role]},
                    ),
                    patch.object(pool, "observation", return_value={}),
                    self.assertRaisesRegex(OSError, "open failed"),
                ):
                    pool.gate("open")
                self.assertEqual(modes, dict.fromkeys(users, "block"))


if __name__ == "__main__":
    unittest.main()

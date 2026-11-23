"""Shared-pool admission, durable transitions, and recovery under probes and quota-update failures."""

from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SCRIPTS), str(SCRIPTS.parent / "ops/openscience-per-user-dev")]
import openscience_per_user_dev as m1  # noqa: E402
import openscience_per_user_service as service  # noqa: E402

import water_monitor as monitor  # noqa: E402


class Pool:
    def __init__(self, policy):
        self.policy = policy
        self.free = 400
        self.modes = {"a": "open", "b": "open"}

    def sample(self):
        return {
            "filesystem_uuid": self.policy["filesystem_uuid"],
            "filesystem_type": "ext4",
            "total_bytes": 500,
            "available_bytes": self.free,
            "workspaces": {
                r: {
                    "group_gate": mode,
                    "filesystem_uuid": self.policy["claim"]["filesystem_uuids"][r],
                }
                for r, mode in self.modes.items()
            },
        }

    def gate(self, mode):
        self.modes = dict.fromkeys(self.modes, mode)
        return self.sample()


class WatermarkTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        stderr = redirect_stderr(StringIO())
        stderr.__enter__()
        self.addCleanup(stderr.__exit__, None, None, None)
        self.claim = {
            "phase": "src",
            "filesystem_uuids": {r: str(uuid4()) for r in "ab"},
        }
        self.policy = monitor.policy_for(self.claim, str(uuid4()), 100, 160)
        self.pool = Pool(self.policy)

    def observed(self, **kw):
        with monitor.locked(self.root):
            return monitor.observe(self.root, self.policy, self.pool, **kw)

    def events(self):
        return [
            json.loads(p.read_bytes())
            for p in sorted((self.root / "alerts").glob("*.json"))
        ]

    def test_hysteretic_boundary_idle_repeats_and_resume_keep_delivered_ids(self):
        self.assertEqual(self.observed()["state"], "normal")
        self.assertFalse(self.events())
        self.pool.free = 100
        low = self.observed()
        self.assertFalse(low["write_allowed"])
        self.assertEqual(self.pool.modes, dict.fromkeys("ab", "block"))
        self.assertEqual(self.events(), [low["last_transition"]])
        self.assertEqual(low["last_transition"]["notification"], "firing")
        # Reconstruct physical probe and monitor state from disk between polls.
        self.pool = Pool(self.policy)
        self.pool.free = 159
        self.assertEqual(
            self.observed()["last_transition"]["id"], low["last_transition"]["id"]
        )
        self.assertEqual(self.pool.modes, dict.fromkeys("ab", "block"))
        self.pool.free = 160
        cleared = self.observed()
        self.assertTrue(cleared["write_allowed"])
        self.assertEqual(self.pool.modes, dict.fromkeys("ab", "open"))
        self.assertEqual(cleared["last_transition"]["notification"], "resolved")
        for _ in range(3):
            self.observed()
        self.assertEqual([e["sequence"] for e in self.events()], [1, 2])

    def test_unavailable_and_partial_group_update_do_not_allow_start_or_write(self):
        self.observed()
        self.pool.free = 50

        def partial(mode):
            self.pool.modes["a"] = mode
            raise OSError("interrupted kernel update")

        with patch.object(self.pool, "gate", side_effect=partial):
            self.assertEqual(self.observed()["state"], "unavailable")
        self.pool.free = 140
        self.assertEqual(self.observed()["state"], "low")
        self.assertEqual(self.pool.modes, dict.fromkeys("ab", "block"))
        self.pool.free = 300
        self.assertEqual(self.observed(stopped=True)["state"], "unavailable")
        self.assertEqual(self.pool.modes, dict.fromkeys("ab", "block"))
        with patch.object(self.pool, "sample", side_effect=OSError("missing mount")):
            result = self.observed()
            self.assertFalse(result["write_allowed"])
            self.assertIsNone(result["available_bytes"])
        self.assertEqual(self.observed()["state"], "normal")
        self.assertEqual(self.pool.modes, dict.fromkeys("ab", "open"))

    def test_interrupted_alert_publish_replays_exactly_one_event(self):
        self.observed()
        self.pool.free = 50

        def crash_before_spool(root, event):
            if event is not None:
                raise OSError("power lost after state commit")

        with (
            patch.object(monitor, "deliver", side_effect=crash_before_spool),
            self.assertRaises(OSError),
        ):
            self.observed()
        self.assertEqual(self.events(), [])
        self.pool.free = 160
        self.observed()
        self.observed()
        self.assertEqual(
            [x["notification"] for x in self.events()], ["firing", "resolved"]
        )

    def test_probe_rejects_unrelated_filesystem_without_touching_its_limits(self):
        foreign = self.pool.sample()
        foreign["filesystem_uuid"] = str(uuid4())
        with (
            patch.object(self.pool, "sample", return_value=foreign),
            patch.object(self.pool, "gate") as mutate,
        ):
            result = self.observed()
            self.assertEqual(result["state"], "unavailable")
            self.assertFalse(result["write_allowed"])
            mutate.assert_not_called()

    def test_guarded_registry_admission_and_rpc_do_not_fall_back(self):
        r = m1.Registry(self.root)
        r.data = {
            "format": 5,
            "disk_watermark": {"filesystem_uuid": self.policy["filesystem_uuid"]},
        }
        with patch.object(
            r, "disk_watermark", return_value={"state": "low", "write_allowed": False}
        ):
            with self.assertRaisesRegex(
                m1.CapacityRefusal, "workspace_capacity: global_disk"
            ):
                r.admit(Mock())
            with self.assertRaisesRegex(m1.Problem, "^workspace_disk_low_watermark$"):
                r.check_disk_admission()
        with (
            patch.object(r, "check_format"),
            patch.object(
                r, "pool_command", side_effect=subprocess.TimeoutExpired("ssh", 25)
            ),
        ):
            with self.assertRaisesRegex(
                m1.Problem, "^workspace_disk_monitor_unavailable$"
            ):
                r.check_disk_admission()
        with patch.object(
            service, "manager", return_value={"write_allowed": True}
        ) as runner:
            self.assertTrue(
                service.dispatch(
                    r.root,
                    {
                        "operation": "check-write",
                        "environment": "dev-m1",
                        "user_id": "test-user-a",
                    },
                )["write_allowed"]
            )
            runner.assert_called_once_with(
                r.root,
                "check-write",
                "--environment",
                "dev-m1",
                "--user-id",
                "test-user-a",
            )
            with self.assertRaisesRegex(ValueError, "arguments"):
                service.dispatch(
                    r.root,
                    {
                        "operation": "check-write",
                        "environment": "dev-m1",
                        "user_id": "test-user-a",
                        "filesystem_uuid": str(uuid4()),
                    },
                )

    def test_policy_pinned_before_activation_and_retry_cannot_switch_disks(self):
        reg = m1.Registry(self.root)
        reg.data = {
            "format": 4,
            "native_claim": self.claim,
            "nfs_endpoints": {r: {"volume": r} for r in "ab"},
        }
        disk = self.policy["filesystem_uuid"]
        with (
            patch.object(reg, "check_format"),
            patch.object(reg, "resident_ids", return_value=set()),
            patch.object(m1, "docker", return_value=""),
            patch.object(
                reg,
                "pool_command",
                side_effect=[{}, OSError("interrupted guest setup")],
            ),
        ):
            with self.assertRaisesRegex(OSError, "interrupted"):
                reg.configure_watermark(disk, 96, 160)
        self.assertEqual(json.loads(reg.file.read_bytes())["format"], 5)
        with (
            patch.object(reg, "check_format"),
            patch.object(reg, "pool_command", return_value={"state": "normal"}) as send,
        ):
            with self.assertRaisesRegex(m1.Problem, "different global"):
                reg.configure_watermark(str(uuid4()), 96, 160)
            send.assert_not_called()
            self.assertEqual(
                reg.configure_watermark(disk, 96, 160), {"state": "normal"}
            )
            self.assertEqual(send.call_args.args[0], "configure")

    def test_stop_guards_kernel_before_reading_broken_state_and_avoids_false_shutdown_alarm(
        self,
    ):
        self.observed()
        self.pool.free = 80
        self.observed()
        self.pool.free = 250
        self.observed()
        with monitor.locked(self.root):
            monitor.stop_guard(self.root, self.policy, self.pool, shutting_down=True)
        self.assertEqual(
            [e["notification"] for e in self.events()], ["firing", "resolved"]
        )
        self.assertEqual(self.pool.modes, dict.fromkeys("ab", "block"))
        self.assertTrue(self.observed()["write_allowed"])
        self.assertEqual(self.pool.modes, dict.fromkeys("ab", "open"))
        (self.root / "state.json").write_bytes(b"bad read")
        with monitor.locked(self.root), self.assertRaises(ValueError):
            monitor.stop_guard(self.root, self.policy, self.pool, shutting_down=False)
        self.assertEqual(self.pool.modes, dict.fromkeys("ab", "block"))

    def test_recovery_rechecks_free_space_after_opening_both_groups(self):
        self.pool.free = 80
        self.assertEqual(self.observed()["state"], "low")
        self.pool.free = 180
        apply = self.pool.gate

        def compete(mode):
            if mode == "open":
                self.pool.free = 120
            return apply(mode)

        with patch.object(self.pool, "gate", side_effect=compete):
            state = self.observed()
        self.assertEqual(state["state"], "low")
        self.assertFalse(state["write_allowed"])
        self.assertEqual(self.pool.modes, dict.fromkeys("ab", "block"))
        self.assertEqual([e["notification"] for e in self.events()], ["firing"])

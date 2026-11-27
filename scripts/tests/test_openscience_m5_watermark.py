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

import water_alerts  # noqa: E402
import water_monitor as monitor  # noqa: E402
import water_pool  # noqa: E402


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

    def gate(self, mode, policy):
        assert self.policy == policy
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

        def partial(mode, policy):
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

    def test_receiver_outage_preserves_backlog_across_retention_and_detects_missing_event(
        self,
    ):
        self.observed()
        fs = self.policy["filesystem_uuid"]
        socket = str(self.root / "receiver.sock")
        with patch.object(monitor, "HISTORY", 3):
            water_alerts.register(self.root, socket, fs)
            for i in range(8):
                self.pool.free = 50 if i % 2 == 0 else 300
                self.observed()
            self.assertEqual([e["sequence"] for e in self.events()], list(range(1, 9)))
            gap = self.root / "alerts" / "000000000001.json"
            gap.rename(self.root / "displaced")
            with (
                self.assertRaises(FileNotFoundError),
                patch.object(water_alerts, "post") as post,
            ):
                water_alerts.send_next(self.root, 3)
            post.assert_not_called()
            self.assertIsNone(
                water_alerts.read(self.root / "receiver.json")["last_receipt"]
            )
            (self.root / "displaced").rename(gap)
            with patch.object(water_alerts, "post") as post:
                for i in range(8):
                    self.assertTrue(water_alerts.send_next(self.root, 3))
                    # Reapplying configuration must preserve the cursor.
                    water_alerts.register(self.root, socket, fs)
                self.assertFalse(water_alerts.send_next(self.root, 3))
                self.assertEqual(
                    [call.args[1]["sequence"] for call in post.call_args_list],
                    list(range(1, 9)),
                )
            self.assertEqual([e["sequence"] for e in self.events()], [6, 7, 8])
            status = monitor.alerts_status(self.root)
            self.assertTrue(status["subscribed"])
            self.assertEqual(status["pending"], 0)
            self.assertEqual(
                [r["id"] for r in status["receiver"]["receipts"]],
                [e["id"] for e in self.events()],
            )
            with self.assertRaises(ValueError):
                water_alerts.register(self.root, str(self.root / "other.sock"), fs)

    def test_shared_host_filesystem_namespaces_receipts_and_refreshes_kernel_gates(
        self,
    ):
        deployment = str(uuid4())
        self.policy.update(environment="deploy-m6")
        self.claim["deployment"] = deployment
        fs = self.policy["filesystem_uuid"]
        sock = str(self.root / "receiver.sock")
        with patch.object(self.pool, "gate", wraps=self.pool.gate) as gate:
            self.assertTrue(self.observed(refresh=True)["write_allowed"])
            gate.assert_called_once_with("open", self.policy)
        self.pool.free = 50
        self.observed()
        unsubscribed = monitor.alerts_status(self.root)
        self.assertFalse(unsubscribed["subscribed"])
        self.assertIsNone(unsubscribed["receiver"])
        self.assertEqual(unsubscribed["alerts"][0]["environment"], "deploy-m6")
        with self.assertRaisesRegex(ValueError, "deployment"):
            water_alerts.register(self.root, sock, fs, deployment=str(uuid4()))
        water_alerts.register(self.root, sock, fs, deployment=deployment)
        self.pool.free = 300
        self.observed()
        pending = monitor.alerts_status(self.root)
        self.assertEqual(pending["pending"], 2)
        self.assertEqual(pending["receiver"]["receipts"], [])
        with patch.object(water_alerts, "post") as post:
            self.assertTrue(water_alerts.send_next(self.root, 64))
            self.assertTrue(water_alerts.send_next(self.root, 64))
            self.assertFalse(water_alerts.send_next(self.root, 64))
        acknowledged = monitor.alerts_status(self.root)
        self.assertEqual(acknowledged["pending"], 0)
        ids = [f"{deployment}:{fs}:{s}" for s in (1, 2)]
        self.assertEqual([call.args[1]["id"] for call in post.call_args_list], ids)
        self.assertEqual([e["id"] for e in acknowledged["alerts"]], ids)
        receipts = acknowledged["receiver"]["receipts"]
        self.assertEqual([r["id"] for r in receipts], ids)
        self.assertEqual([r["notification"] for r in receipts], ["firing", "resolved"])
        self.assertEqual(receipts[-1], acknowledged["receiver"]["last_receipt"])
        self.claim["deployment"] = str(uuid4())
        with self.assertRaisesRegex(ValueError, "deployment"):
            self.observed()

    def test_upgraded_receiver_preserves_only_its_recorded_acknowledgements(self):
        fs = self.policy["filesystem_uuid"]
        water_alerts.register(self.root, str(self.root / "receiver.sock"), fs)
        self.pool.free = 50
        self.observed()
        self.pool.free = 300
        self.observed()
        self.pool.free = 50
        self.observed()
        record = water_alerts.read(self.root / "receiver.json")
        del record["receipts"]
        previous = {"id": f"{fs}:2", "sequence": 2, "at": "2026-11-25T00:00:00+00:00"}
        record["last_receipt"] = previous
        water_alerts.save(self.root, record)
        self.assertEqual(
            monitor.alerts_status(self.root)["receiver"]["receipts"], [previous]
        )
        with patch.object(water_alerts, "post"):
            self.assertTrue(water_alerts.send_next(self.root, 64))
        receipts = monitor.alerts_status(self.root)["receiver"]["receipts"]
        self.assertEqual(len(receipts), 2)
        self.assertEqual(receipts[0], previous)
        self.assertEqual(receipts[1]["id"], f"{fs}:3")

    def test_relay_idles_without_a_subscription_and_stops_on_request(self):
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        with patch.object(water_alerts, "send_next") as send:
            water_alerts.serve(self.root, 64, 2, stop=stop)
        send.assert_not_called()
        stop.wait.assert_called_once_with(2)
        self.assertFalse((self.root / "receiver.json").exists())

    def test_damaged_receipt_keeps_monitor_running_and_retains_pending_files(self):
        water_alerts.register(
            self.root, str(self.root / "receiver.sock"), self.policy["filesystem_uuid"]
        )
        with patch.object(monitor, "HISTORY", 1):
            self.pool.free = 50
            self.observed()
            dest = self.root / "receiver.json"
            record = water_alerts.read(dest)
            record["last_receipt"] = {"id": "other", "sequence": 512}
            water_alerts.save(self.root, record)
            self.pool.free = 300
            self.assertTrue(self.observed()["write_allowed"])
            with self.assertRaisesRegex(
                ValueError, "Invalid watermark receiver receipt"
            ):
                water_alerts.send_next(self.root, 1)
            self.assertEqual([e["sequence"] for e in self.events()], [1, 2])

    def test_older_or_missing_feed_cannot_appear_caught_up(self):
        fs = self.policy["filesystem_uuid"]
        water_alerts.register(self.root, str(self.root / "receiver.sock"), fs)
        self.pool.free = 50
        self.observed()
        with patch.object(water_alerts, "post"):
            self.assertTrue(water_alerts.send_next(self.root, 64))
        stale = self.root / "state.json"
        snapshot = water_alerts.read(stale)
        snapshot["sequence"] = 0
        monitor.publish(stale, snapshot)
        with self.assertRaisesRegex(ValueError, "predates"):
            water_alerts.send_next(self.root, 64)
        stale.unlink()
        with self.assertRaises(FileNotFoundError):
            water_alerts.send_next(self.root, 64)
        self.assertEqual(
            water_alerts.read(self.root / "receiver.json")["last_receipt"]["id"],
            fs + ":1",
        )

    def test_unrelated_success_response_does_not_advance_cursor(self):
        from http.server import BaseHTTPRequestHandler
        import socketserver
        from threading import Thread

        class WrongReceipt(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"id":"another-event"}')

            def log_message(self, *args):
                pass

        sock = str(self.root / "receiver.sock")
        water_alerts.register(self.root, sock, self.policy["filesystem_uuid"])
        self.pool.free = 50
        self.observed()
        with socketserver.UnixStreamServer(sock, WrongReceipt) as receiver:
            worker = Thread(target=receiver.handle_request, daemon=True)
            worker.start()
            with self.assertRaisesRegex(ValueError, "acknowledge"):
                water_alerts.send_next(self.root, 64)
            worker.join(timeout=3)
        self.assertIsNone(
            water_alerts.read(self.root / "receiver.json")["last_receipt"]
        )

    def test_registration_includes_active_incident_and_retries_after_unsaved_ack(self):
        self.observed()
        self.pool.free = 50
        active = self.observed()["last_transition"]
        water_alerts.register(
            self.root, str(self.root / "receiver.sock"), self.policy["filesystem_uuid"]
        )
        self.assertEqual(
            water_alerts.read(self.root / "receiver.json")["first_sequence"],
            active["sequence"],
        )
        with patch.object(water_alerts, "post") as post:
            with (
                patch.object(water_alerts, "save", side_effect=OSError("interrupted")),
                self.assertRaises(OSError),
            ):
                water_alerts.send_next(self.root, 64)
            self.assertTrue(water_alerts.send_next(self.root, 64))
            self.assertFalse(water_alerts.send_next(self.root, 64))
            self.assertEqual(
                [call.args[1]["id"] for call in post.call_args_list], [active["id"]] * 2
            )

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
        self.pool = water_pool
        with (
            patch.object(water_pool, "sample", return_value=foreign),
            patch.object(water_pool, "pins", return_value=foreign),
            patch.object(water_pool, "group_call") as mutate,
        ):
            result = self.observed()
            self.assertEqual(result["state"], "unavailable")
            self.assertFalse(result["write_allowed"])
            mutate.assert_not_called()

    def test_one_workspace_fault_guards_peer_and_recovers_at_the_high_watermark(self):
        self.pool = water_pool
        uuids = self.claim["filesystem_uuids"]
        for failed, peer in (("a", "b"), ("b", "a")):
            for fault in ("inspect", "set", "readback"):
                with self.subTest(failed=failed, fault=fault):
                    free = 300
                    modes = dict.fromkeys("ab", "open")
                    broken = False
                    writes = []
                    broken_readback = False

                    def volume(cfg, role, owner, policy):
                        if broken and role == failed and fault == "inspect":
                            raise ValueError("unverified source")
                        return role, {
                            "uid": owner,
                            "filesystem_uuid": uuids[role],
                            "group_gate": modes[role],
                        }

                    def write(role, cmd, uid, value):
                        nonlocal broken_readback
                        self.assertEqual(cmd, water_pool.quota.Q_SETQUOTA)
                        self.assertEqual(value.block_hard, value.inode_hard)
                        writes.append(role)
                        if broken and role == failed:
                            if fault == "set":
                                raise OSError("kernel quota update failed")
                            if fault == "readback":
                                broken_readback = True
                        modes[role] = "block" if value.block_hard else "open"

                    def read(role, uid):
                        if broken and role == failed and broken_readback:
                            raise OSError("quota readback unavailable")
                        return {"group_gate": modes[role]}

                    def measure(cfg, roles):
                        return {
                            "filesystem_uuid": self.policy["filesystem_uuid"],
                            "filesystem_type": "ext4",
                            "available_bytes": free,
                            "total_bytes": 500,
                            "workspaces": {k: row for k, (_, row) in roles.items()},
                        }

                    with (
                        patch.object(
                            water_pool,
                            "source_layout",
                            return_value=({}, {"a": 410000, "b": 410001}),
                        ),
                        patch.object(
                            water_pool, "workspace_volume", side_effect=volume
                        ),
                        patch.object(water_pool, "group_call", side_effect=write),
                        patch.object(water_pool, "group_state", side_effect=read),
                        patch.object(water_pool, "observation", side_effect=measure),
                    ):
                        self.assertTrue(self.observed()["write_allowed"])
                        seq = len(self.events())
                        free, broken = 70, True
                        result = self.observed()
                        self.assertFalse(result["write_allowed"])
                        self.assertEqual(result["state"], "unavailable")
                        self.assertEqual(modes[peer], "block")
                        self.assertEqual(set(result["workspaces"]), {peer})
                        self.assertEqual(set(result["gate_errors"]), {failed})
                        self.assertEqual(
                            result["last_transition"]["gate_errors"],
                            result["gate_errors"],
                        )
                        if fault == "inspect":
                            self.assertNotIn(failed, writes)
                        free = 200
                        self.assertEqual(self.observed()["state"], "unavailable")
                        self.assertEqual(modes[peer], "block")
                        # A repaired workspace must rejoin the block until the
                        # recovery margin is met, even after a service retry.
                        broken, free = False, 120
                        self.assertEqual(self.observed()["state"], "low")
                        self.assertEqual(modes, dict.fromkeys("ab", "block"))
                        free = 160
                        self.assertTrue(self.observed()["write_allowed"])
                        self.assertEqual(modes, dict.fromkeys("ab", "open"))
                        self.assertEqual(
                            [e["notification"] for e in self.events()[seq:]],
                            ["degraded", "firing", "resolved"],
                        )

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
            self.assertEqual(
                send.call_args.args, ("configure", reg.data["disk_watermark"])
            )
            reg.configure_watermark(disk, 96, 160, "/run/receiver.sock")
            send.assert_called_with(
                "configure",
                reg.data["disk_watermark"],
                "--alert-socket",
                "/run/receiver.sock",
            )

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

        def compete(mode, policy):
            if mode == "open":
                self.pool.free = 120
            return apply(mode, policy)

        with patch.object(self.pool, "gate", side_effect=compete):
            state = self.observed()
        self.assertEqual(state["state"], "low")
        self.assertFalse(state["write_allowed"])
        self.assertEqual(self.pool.modes, dict.fromkeys("ab", "block"))
        self.assertEqual([e["notification"] for e in self.events()], ["firing"])

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import UUID

from pydantic import ValidationError

from server_monitoring.collector import (
    Collector, HistoryRing, atomic_json, cpu_counters, cpu_percent, iso,
    memory_values, parse_gpus, query_gpus,
)
from server_monitoring.models import validate_history, validate_snapshot


EPOCH = 1_800_000_000.0  # Exact thirty-second boundary.
GPU_CSV = "0, GPU-0000, GPU Zero, 25, 24000, 1000, 45, 62.5\n1, GPU-1111, GPU One, 80, 24000, 2000, 55, 100\n"


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        (self.proc / "stat").write_text("cpu 10 0 20 70 0 0 0 0 999 999\n")
        (self.proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 250 kB\n")
        (self.proc / "uptime").write_text("100.5 90\n")
        self.now = EPOCH

    def tearDown(self):
        self.directory.cleanup()

    def collector(self, gpu_query=lambda: parse_gpus(GPU_CSV)):
        return Collector(self.root / "output", proc_root=self.proc,
                         disks=((str(self.root), "Test disk"),), gpu_query=gpu_query,
                         clock=lambda: self.now)

    def test_cpu_uses_deltas_without_double_counting_guest_and_resets_baseline(self):
        before = cpu_counters("cpu 10 0 20 70 0 0 0 0 999 999")
        after = cpu_counters("cpu 20 0 20 80 0 0 0 0 99999 99999")
        self.assertEqual(cpu_percent(before, after), 50.0)
        self.assertIsNone(cpu_percent(None, after))
        self.assertIsNone(cpu_percent(after, after))
        self.assertIsNone(cpu_percent(after, before))
        self.assertIsNone(cpu_percent((1, 2, 3, 4), after))

    def test_cpu_reset_never_reports_zero_and_recovers(self):
        collector = self.collector()
        first, _ = collector.sample()
        self.assertEqual(first["components"]["cpu"]["status"], "warming")
        self.assertIsNone(first["cpu"]["utilization_percent"])
        (self.proc / "stat").write_text("cpu 20 0 20 80 0 0 0 0\n")
        self.now += 5
        second, _ = collector.sample()
        self.assertEqual(second["cpu"]["utilization_percent"], 50.0)
        last_success = second["components"]["cpu"]["last_success_at"]
        (self.proc / "stat").write_text("cpu 1 0 2 7 0 0 0 0\n")
        self.now += 5
        reset, _ = collector.sample()
        self.assertIsNone(reset["cpu"]["utilization_percent"])
        self.assertEqual(reset["components"]["cpu"]["status"], "warming")
        self.assertEqual(reset["components"]["cpu"]["last_success_at"], last_success)

    def test_long_sampling_gap_resets_cpu_delta_before_resuming(self):
        collector = self.collector()
        elapsed = [0.0]
        collector.monotonic = lambda: elapsed[0]
        collector.sample()
        elapsed[0] = 60.0
        (self.proc / "stat").write_text("cpu 20 0 20 80 0 0 0 0\n")
        snapshot, _ = collector.sample()
        self.assertEqual(snapshot["components"]["cpu"]["status"], "warming")
        self.assertIsNone(snapshot["cpu"]["utilization_percent"])
        elapsed[0] = 65.0
        (self.proc / "stat").write_text("cpu 30 0 20 90 0 0 0 0\n")
        snapshot, _ = collector.sample()
        self.assertEqual(snapshot["cpu"]["utilization_percent"], 50.0)

    def test_missing_memory_and_disks_are_explicit_while_other_metrics_survive(self):
        collector = self.collector()
        collector.disks = ((str(self.root), "Available"), (str(self.root / "absent"), "Absent"))
        initial, _ = collector.sample()
        self.assertEqual(initial["memory"]["used_bytes"], 750 * 1024)
        self.assertEqual(initial["memory"]["used_percent"], 75.0)
        self.assertEqual(initial["disks"][0]["status"], "ok")
        self.assertEqual(initial["disks"][1]["status"], "unavailable")
        self.assertIsNone(initial["disks"][1]["used_bytes"])
        self.assertEqual(initial["components"]["disks"]["status"], "unavailable")
        (self.proc / "meminfo").write_text("MemTotal: 1000 kB\n")
        self.now += 5
        broken, _ = collector.sample()
        self.assertIsNone(broken["memory"])
        self.assertEqual(broken["components"]["memory"]["status"], "unavailable")
        self.assertEqual(broken["components"]["memory"]["last_success_at"], initial["captured_at"])
        self.assertEqual(len(broken["gpus"]), 2)
        with self.assertRaises(ValueError):
            memory_values("MemTotal: 100 kB\nMemAvailable: 200 kB\n")

    def test_gpu_zero_and_partial_metrics_are_preserved(self):
        values, partial = parse_gpus(GPU_CSV + "2, GPU-2222, Partial, N/A, 100, N/A, nan, 50\nbad,row\n")
        self.assertTrue(partial)
        self.assertEqual([value["index"] for value in values], [0, 1, 2])
        self.assertEqual(values[0]["memory_used_bytes"], 1000 * 1024**2)
        self.assertIsNone(values[2]["utilization_percent"])
        self.assertIsNone(values[2]["temperature_celsius"])
        self.assertEqual(values[2]["power_watts"], 50.0)
        duplicates, partial = parse_gpus(GPU_CSV + GPU_CSV)
        self.assertTrue(partial)
        self.assertEqual(len(duplicates), 2)

    def test_gpu_query_is_bounded_and_does_not_inherit_mps_device_view(self):
        with patch.dict("os.environ", {"NVML_MPS_DEVICE_ENUMERATION": "1", "CUDA_VISIBLE_DEVICES": "1", "LD_PRELOAD": "/untrusted.so"}), patch(
            "server_monitoring.collector.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=GPU_CSV)
        ) as run:
            values, partial = query_gpus()
        self.assertFalse(partial)
        self.assertEqual(values[0]["index"], 0)
        arguments, options = run.call_args
        self.assertEqual(arguments[0][0], "/usr/bin/nvidia-smi")
        self.assertNotIn("-i", arguments[0])
        self.assertEqual(options["timeout"], 3)
        self.assertFalse(any(name in options["env"] for name in ("NVML_MPS_DEVICE_ENUMERATION", "CUDA_VISIBLE_DEVICES", "LD_PRELOAD")))
        self.assertNotIn("shell", options)

    def test_gpu_timeout_does_not_remove_cpu_or_ram(self):
        def timeout():
            raise subprocess.TimeoutExpired("nvidia-smi", 3)
        snapshot, _ = self.collector(timeout).sample()
        self.assertEqual(snapshot["gpus"], [])
        self.assertEqual(snapshot["components"]["gpus"]["status"], "unavailable")
        self.assertIsNotNone(snapshot["memory"])
        self.assertIsNotNone(snapshot["cpu"])

    def test_history_averages_thirty_second_windows_and_keeps_device_identity(self):
        ring = HistoryRing()
        first, _ = self.collector().sample()
        first["cpu"]["utilization_percent"] = 20.0
        first["gpus"] = [first["gpus"][0]]
        ring.observe(first, EPOCH + 5)
        second = deepcopy(first)
        second["cpu"]["utilization_percent"] = 40.0
        second["gpus"][0]["utilization_percent"] = 75.0
        ring.observe(second, EPOCH + 10)
        replacement = deepcopy(first)
        replacement["gpus"][0]["uuid"] = "GPU-NEW"
        replacement["gpus"][0]["utilization_percent"] = 90.0
        ring.observe(replacement, EPOCH + 30)
        point = ring.points[0]
        self.assertEqual(point["timestamp"], iso(EPOCH + 30))
        self.assertEqual(point["cpu_percent"], 30.0)
        self.assertEqual(point["gpus"][0]["utilization_percent"], 50.0)
        ring.observe(replacement, EPOCH + 60)
        self.assertEqual(ring.points[-1]["gpus"][0]["uuid"], "GPU-NEW")
        self.assertEqual(ring.points[-1]["gpus"][0]["utilization_percent"], 90.0)

    def test_history_gap_is_null_and_ring_is_bounded(self):
        ring = HistoryRing()
        snapshot, _ = self.collector().sample()
        ring.observe(snapshot, EPOCH)
        ring.observe(snapshot, EPOCH + 90)
        self.assertEqual(len(ring.points), 3)
        self.assertIsNone(ring.points[1]["cpu_percent"])
        self.assertIsNone(ring.points[1]["memory_percent"])
        self.assertEqual(ring.points[1]["gpus"], [])
        ring.observe(snapshot, EPOCH + 10000)
        self.assertEqual(len(ring.points), 120)
        self.assertTrue(all(datetime.fromisoformat(point["timestamp"].replace("Z", "+00:00")).timestamp() >= EPOCH + 10000 - 3600 for point in ring.points))
        self.assertTrue(all(point["cpu_percent"] is None for point in ring.points))
        ring.observe(snapshot, EPOCH + 5)
        self.assertEqual(len(ring.points), 0)

    def test_wall_clock_rollback_resets_observations_without_future_timestamps(self):
        collector = self.collector()
        collector.sample()
        self.now += 60
        (self.proc / "stat").write_text("cpu 20 0 20 80 0 0 0 0\n")
        before, before_history = collector.sample()
        self.assertTrue(before_history["points"])
        self.now -= 30
        (self.proc / "meminfo").unlink()
        rolled_back, history = collector.sample()
        self.assertEqual(rolled_back["collector_id"], before["collector_id"])
        self.assertGreater(rolled_back["sequence"], before["sequence"])
        self.assertEqual(rolled_back["captured_at"], iso(self.now))
        self.assertEqual(rolled_back["components"]["cpu"]["status"], "warming")
        self.assertIsNone(rolled_back["components"]["memory"]["last_success_at"])
        self.assertEqual(history["points"], [])
        self.now += 5
        (self.proc / "stat").write_text("cpu 30 0 20 90 0 0 0 0\n")
        resumed, _ = collector.sample()
        self.assertEqual(resumed["cpu"]["utilization_percent"], 50.0)
        self.now = EPOCH - 10
        earlier, history = collector.sample()
        self.assertEqual(earlier["started_at"], earlier["captured_at"])
        self.assertEqual(history["points"], [])

    def test_wall_clock_rollback_during_collection_invalidates_cpu_interval(self):
        collector = self.collector()
        collector.sample()
        self.now += 60
        collector.sample()
        # The GPU stage happens after reading CPU and memory counters.
        def rollback_during_query():
            self.now -= 30
            return parse_gpus(GPU_CSV)
        collector.gpu_query = rollback_during_query
        self.now += 5
        snapshot, history = collector.sample()
        self.assertIsNone(snapshot["cpu"]["utilization_percent"])
        self.assertEqual(snapshot["components"]["cpu"]["status"], "warming")
        self.assertEqual(snapshot["captured_at"], iso(self.now))
        self.assertEqual(history["points"], [])

    def test_restart_creates_new_identity_and_clears_previous_history(self):
        first = self.collector()
        first.run(threading.Event(), once=True)
        self.now += 30
        first.run(threading.Event(), once=True)
        self.assertEqual(len(json.loads((first.output_dir / "history.json").read_text())["points"]), 1)
        restarted = self.collector()
        restarted.run(threading.Event(), once=True)
        history = json.loads((restarted.output_dir / "history.json").read_text())
        snapshot = json.loads((restarted.output_dir / "latest.json").read_text())
        self.assertNotEqual(first.collector_id, history["collector_id"])
        UUID(history["collector_id"])
        self.assertEqual(history["points"], [])
        self.assertEqual(history["collector_id"], snapshot["collector_id"])
        self.assertEqual((restarted.output_dir / "latest.json").stat().st_mode & 0o777, 0o600)

    def test_atomic_write_never_exposes_partial_json_and_preserves_prior_on_failure(self):
        path = self.root / "atomic.json"
        atomic_json(path, {"sequence": 0})
        errors, stopped = [], threading.Event()
        def reader():
            while not stopped.is_set():
                try:
                    json.loads(path.read_text())
                except Exception as error:
                    errors.append(error)
        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for number in range(20):
                atomic_json(path, {"sequence": number, "payload": "x" * 10000})
        finally:
            stopped.set()
            thread.join()
        self.assertEqual(errors, [])
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            atomic_json(path, {"bad": float("nan")})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".monitor-*")), [])

    def test_models_reject_nonfinite_out_of_range_naive_time_and_extra_data(self):
        snapshot, history = self.collector().sample()
        validate_snapshot(snapshot)
        validate_history(history)
        for value in (float("nan"), float("inf"), -1.0, 101.0, "50", True):
            bad = deepcopy(snapshot)
            bad["cpu"]["utilization_percent"] = value
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_snapshot(bad)
        for name, value in (("captured_at", "2026-01-01T00:00:00"), ("command_line", "secret"), ("sequence", True), ("schema_version", True)):
            bad = deepcopy(snapshot)
            bad[name] = value
            with self.subTest(name=name), self.assertRaises(ValidationError):
                validate_snapshot(bad)


if __name__ == "__main__":
    unittest.main()

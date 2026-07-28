from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "gpu2_guard",
    ROOT / "scripts/gpu2_guard.py",
)
assert SPEC is not None and SPEC.loader is not None
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class Gpu2GuardTests(unittest.TestCase):
    def test_collect_allows_only_production_service_descendants(self) -> None:
        parents = {301: 300, 401: 400}
        with (
            mock.patch.object(
                guard,
                "gpu_processes",
                return_value=(
                    guard.GPU_UUID,
                    [
                        {
                            "pid": 301,
                            "gpu_uuid": guard.GPU_UUID,
                            "process_name": "backend",
                        },
                        {
                            "pid": 401,
                            "gpu_uuid": guard.GPU_UUID,
                            "process_name": "dft",
                        },
                    ],
                ),
            ),
            mock.patch.object(
                guard,
                "production_backend_containers",
                return_value={"abc": 300},
            ),
            mock.patch.object(
                guard,
                "unit_main_pid",
                side_effect=lambda unit: 400 if "dft" in unit else None,
            ),
            mock.patch.object(
                guard,
                "parent_pid",
                side_effect=lambda pid: parents.get(pid),
            ),
        ):
            payload = guard.collect()

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["unknown_processes"], [])
        self.assertEqual(
            {item["pid"] for item in payload["allowed_processes"]},
            {301, 401},
        )

    def test_collect_quarantines_unknown_without_killing_it(self) -> None:
        with (
            mock.patch.object(
                guard,
                "gpu_processes",
                return_value=(
                    guard.GPU_UUID,
                    [
                        {
                            "pid": 999,
                            "gpu_uuid": guard.GPU_UUID,
                            "process_name": "unknown",
                        }
                    ],
                ),
            ),
            mock.patch.object(
                guard,
                "production_backend_containers",
                return_value={},
            ),
            mock.patch.object(guard, "unit_main_pid", return_value=None),
            mock.patch.object(guard, "parent_pid", return_value=None),
            tempfile.TemporaryDirectory(prefix="gpu2-guard-") as raw,
        ):
            payload = guard.collect()
            state = Path(raw) / "guard.json"
            guard.atomic_write(state, payload)

            self.assertEqual(payload["status"], "quarantined")
            self.assertEqual(payload["unknown_processes"][0]["pid"], 999)
            self.assertEqual(json.loads(state.read_text())["status"], "quarantined")
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)

    def test_gpu_processes_rejects_uuid_change(self) -> None:
        with (
            mock.patch.object(guard, "run", return_value="GPU-wrong\n"),
            self.assertRaisesRegex(RuntimeError, "UUID mismatch"),
        ):
            guard.gpu_processes()


if __name__ == "__main__":
    unittest.main()

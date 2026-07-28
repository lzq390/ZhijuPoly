from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "preflight_monomer_dft_prod",
    ROOT / "scripts/preflight_monomer_dft_prod.py",
)
assert SPEC is not None and SPEC.loader is not None
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


class ProductionDftPreflightTests(unittest.TestCase):
    def build_fixture(self, root: Path) -> tuple[Path, Path, str]:
        release = "a" * 40
        repo = root / "repo"
        runtime = root / "runtime"
        repo.mkdir(mode=0o700)
        runtime.mkdir(mode=0o700)
        slot = runtime / "worker-venvs/dft-a"
        model_root = slot / "aimnet-cache"
        python = slot / "venv/bin/python"
        model_root.mkdir(parents=True, mode=0o700)
        model_root.chmod(0o700)
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\n")
        python.chmod(0o700)
        (slot / "slot.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "release": release,
                    "python": "3.12",
                    "uv": "0.11.21",
                }
            )
        )
        (slot / "slot.json").chmod(0o600)
        models = []
        for index in range(6):
            filename = f"model-{index}.pt"
            payload = f"model-{index}".encode()
            path = model_root / filename
            path.write_bytes(payload)
            path.chmod(0o600)
            models.append(
                {
                    "alias": f"model-{index}",
                    "file": filename,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        lock = repo / "workers/monomer_dft_worker/aimnet-source.lock.json"
        lock.parent.mkdir(parents=True)
        lock.write_text(json.dumps({"models": models}))
        for relative in (
            "state/monomer-dft-worker-socket",
            "state/monomer-dft-worker-runs",
            "state/monomer-dft-download-spool",
        ):
            path = runtime / relative
            path.mkdir(parents=True, mode=0o700)
            path.chmod(0o700)
        return repo, runtime, release

    def test_validates_release_python_models_and_gpu_uuid(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dft-prod-preflight-") as raw:
            repo, runtime, release = self.build_fixture(Path(raw))

            def command(*args: str) -> str:
                if args[-2:] == ("rev-parse", "HEAD"):
                    return release
                if "status" in args:
                    return ""
                if args[0].endswith("/venv/bin/python"):
                    return "3.12"
                if args[0] == "nvidia-smi":
                    return preflight.GPU_UUID
                raise AssertionError(args)

            with mock.patch.object(preflight, "run", side_effect=command):
                result = preflight.validate(repo_root=repo, runtime_root=runtime)

            self.assertEqual(result["status"], "ready")
            self.assertEqual(len(result["models"]), 6)

    def test_rejects_model_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dft-prod-preflight-") as raw:
            repo, runtime, release = self.build_fixture(Path(raw))
            model = next((runtime / preflight.SLOT_RELATIVE / "aimnet-cache").iterdir())
            model.write_bytes(b"changed")
            model.chmod(0o600)

            def command(*args: str) -> str:
                if args[-2:] == ("rev-parse", "HEAD"):
                    return release
                if "status" in args:
                    return ""
                if args[0].endswith("/venv/bin/python"):
                    return "3.12"
                return preflight.GPU_UUID

            with (
                mock.patch.object(preflight, "run", side_effect=command),
                self.assertRaisesRegex(preflight.PreflightError, "SHA mismatch"),
            ):
                preflight.validate(repo_root=repo, runtime_root=runtime)


if __name__ == "__main__":
    unittest.main()

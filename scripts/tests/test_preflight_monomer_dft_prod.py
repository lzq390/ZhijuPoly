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
    def test_systemd_unit_uses_only_the_versioned_runtime_environment(self) -> None:
        unit = (
            ROOT / "ops/systemd/nexpoly-monomer-dft-worker.service"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "EnvironmentFile=/data/lzq/gith/nexpoly-runtime/config/"
            "monomer-dft-runtime.env",
            unit,
        )
        self.assertNotIn("runtime-launcher", unit)
        self.assertNotIn("worker-venvs/dft-a", unit)
        self.assertNotIn("NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY=0", unit)

    def test_runtime_builder_creates_a_release_directory_not_a_slot(self) -> None:
        setup = (
            ROOT / "scripts/setup_monomer_dft_prod_runtime.sh"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'worker-venvs/dft/$release_sha',
            setup,
        )
        self.assertIn(
            'config/monomer-dft-runtime.env',
            setup,
        )
        self.assertIn("MONOMER_DFT_RUNTIME_CONTRACT_SHA256=", setup)
        self.assertNotIn("worker-venvs/dft-a", setup)
        self.assertNotIn("runtime-launcher", setup)

    def build_fixture(
        self, root: Path
    ) -> tuple[Path, Path, str, str, dict[str, str]]:
        release = "a" * 40
        source_tree = "b" * 40
        repo = root / "repo"
        runtime = root / "runtime"
        repo.mkdir(mode=0o700)
        runtime.mkdir(mode=0o700)
        release_runtime = runtime / "worker-venvs/dft" / release
        model_root = release_runtime / "aimnet-cache"
        python = release_runtime / "venv/bin/python"
        model_root.mkdir(parents=True, mode=0o700)
        model_root.chmod(0o700)
        release_runtime.chmod(0o700)
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\n")
        python.chmod(0o700)
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
        requirements_lock = repo / "workers/monomer_dft_worker/requirements.lock"
        requirements_lock.write_text("locked\n")
        runtime_manifest = release_runtime / "runtime.json"
        runtime_manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "release": release,
                    "source_tree": source_tree,
                    "python": "3.12",
                    "uv": "0.11.21",
                    "requirements_lock_sha256": (
                        "sha256:" + hashlib.sha256(b"locked\n").hexdigest()
                    ),
                    "aimnet_source_lock_sha256": (
                        "sha256:" + hashlib.sha256(lock.read_bytes()).hexdigest()
                    ),
                }
            )
        )
        runtime_manifest.chmod(0o600)
        runtime_contract = "sha256:" + hashlib.sha256(
            runtime_manifest.read_bytes()
        ).hexdigest()
        environment = {
            "MONOMER_DFT_RELEASE_SHA": release,
            "MONOMER_DFT_RUNTIME_CONTRACT_SHA256": runtime_contract,
            "MONOMER_DFT_PYTHON": str(python),
            "AIMNET_CACHE_DIR": str(model_root),
            "WARP_CACHE_PATH": str(release_runtime / "warp-cache"),
        }
        (release_runtime / "warp-cache").mkdir(mode=0o700)
        config = runtime / "config"
        config.mkdir(mode=0o700)
        runtime_env = config / "monomer-dft-runtime.env"
        runtime_env.write_text(
            "".join(f"{key}={value}\n" for key, value in environment.items())
        )
        runtime_env.chmod(0o600)
        for relative in (
            "state/monomer-dft-worker-socket",
            "state/monomer-dft-worker-runs",
            "state/monomer-dft-download-spool",
        ):
            path = runtime / relative
            path.mkdir(parents=True, mode=0o700)
            path.chmod(0o700)
        return repo, runtime, release, source_tree, environment

    def test_validates_release_python_models_and_gpu_uuid(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dft-prod-preflight-") as raw:
            repo, runtime, release, source_tree, environment = self.build_fixture(
                Path(raw)
            )

            def command(*args: str) -> str:
                if args[-2:] == ("rev-parse", "HEAD"):
                    return release
                if args[-2:] == ("rev-parse", "HEAD^{tree}"):
                    return source_tree
                if "status" in args:
                    return ""
                if args[0].endswith("/venv/bin/python"):
                    return "3.12"
                if args[0] == "nvidia-smi":
                    return preflight.GPU_UUID
                raise AssertionError(args)

            with mock.patch.object(preflight, "run", side_effect=command):
                result = preflight.validate(
                    repo_root=repo,
                    runtime_root=runtime,
                    environment=environment,
                )

            self.assertEqual(result["status"], "ready")
            self.assertEqual(len(result["models"]), 6)
            self.assertEqual(
                result["runtime_contract_sha256"],
                environment["MONOMER_DFT_RUNTIME_CONTRACT_SHA256"],
            )

    def test_rejects_model_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dft-prod-preflight-") as raw:
            repo, runtime, release, source_tree, environment = self.build_fixture(
                Path(raw)
            )
            model = next(
                (runtime / "worker-venvs/dft" / release / "aimnet-cache").iterdir()
            )
            model.write_bytes(b"changed")
            model.chmod(0o600)

            def command(*args: str) -> str:
                if args[-2:] == ("rev-parse", "HEAD"):
                    return release
                if args[-2:] == ("rev-parse", "HEAD^{tree}"):
                    return source_tree
                if "status" in args:
                    return ""
                if args[0].endswith("/venv/bin/python"):
                    return "3.12"
                return preflight.GPU_UUID

            with (
                mock.patch.object(preflight, "run", side_effect=command),
                self.assertRaisesRegex(preflight.PreflightError, "SHA mismatch"),
            ):
                preflight.validate(
                    repo_root=repo,
                    runtime_root=runtime,
                    environment=environment,
                )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import argparse
from contextlib import ExitStack
import fcntl
import importlib.metadata
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTROLLER_PATH = REPOSITORY_ROOT / "scripts" / "release_controller.py"
SPEC = importlib.util.spec_from_file_location("release_controller", CONTROLLER_PATH)
assert SPEC and SPEC.loader
release_controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_controller)

SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
BACKEND_IMAGE = "ghcr.io/lzq390/nexpoly-backend@" + DIGEST
WEB_IMAGE = "ghcr.io/lzq390/nexpoly-web@sha256:" + "c" * 64


def worker_base_identity() -> dict[str, object]:
    material: dict[str, object] = {
        "schema_version": 1,
        "configured_path": "/opt/frozen-byteff2/bin/python",
        "resolved_path": "/opt/frozen-byteff2/bin/python3.11",
        "executable_sha256": "sha256:" + "1" * 64,
        "executable_size": 12345,
        "implementation": "cpython",
        "python_version": "3.11.14 (fixture)",
        "python_abi": "cpython-311",
        "prefix": "/opt/frozen-byteff2",
        "base_prefix": "/opt/frozen-byteff2",
        "distribution_count": 7,
        "distribution_metadata_sha256": "sha256:" + "2" * 64,
        "conda_package_count": 5,
        "conda_metadata_sha256": "sha256:" + "3" * 64,
    }
    return {
        **material,
        "identity_sha256": release_controller.canonical_json_digest(material),
    }


def worker_toolchain_identity() -> dict[str, object]:
    material: dict[str, object] = {
        "schema_version": 1,
        "conda_executable": "/opt/conda/bin/conda",
        "conda_executable_sha256": "sha256:" + "4" * 64,
        "conda_explicit_sha256": "sha256:" + "5" * 64,
        "gmx_executable": "/opt/frozen-byteff2/bin/gmx",
        "gmx_executable_sha256": "sha256:" + "6" * 64,
        "gmx_version_sha256": "sha256:" + "7" * 64,
    }
    return {
        **material,
        "identity_sha256": release_controller.canonical_json_digest(material),
    }


def write_worker_base_identity(release: Path) -> dict[str, object]:
    identity = worker_base_identity()
    (release / "worker-base-python-identity.json").write_text(
        json.dumps(identity),
        encoding="utf-8",
    )
    return identity


class ReleaseControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        bundle_root = self.root / "default-bundle-root"
        (bundle_root / "scripts").mkdir(parents=True)
        (bundle_root / "wheelhouse").mkdir()
        worker_root = bundle_root / "workers" / "monomer_md_worker"
        worker_root.mkdir(parents=True)
        (bundle_root / "docker-compose.yml").write_text("name: fixture\n", encoding="utf-8")
        (bundle_root / "docker-compose.prod.yml").write_text("services: {}\n", encoding="utf-8")
        (bundle_root / "scripts" / "release_controller.py").write_text(
            "# controller fixture\n",
            encoding="utf-8",
        )
        (worker_root / "requirements.lock").write_text(
            "fixture==1.0 --hash=sha256:" + "1" * 64 + "\n",
            encoding="utf-8",
        )
        self.release_bundle = self.root / f"nexpoly-release-{SHA}.tar.gz"
        with tarfile.open(self.release_bundle, "w:gz") as archive:
            archive.add(bundle_root, arcname=".")
        self.release_input = self.root / "release-input.json"
        self.release_input.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "asset_manifest_digest": DIGEST,
                    "datasets_on_asset_change": [
                        "governance",
                        "core",
                        "knowledge",
                        "online",
                        "pi",
                        "dft",
                        "experimental",
                        "lab",
                        "property_filter",
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        for current, directories, files in os.walk(self.root):
            os.chmod(current, 0o700)
            for name in directories:
                os.chmod(Path(current) / name, 0o700)
            for name in files:
                path = Path(current) / name
                if not path.is_symlink():
                    os.chmod(path, 0o600)
        self.temporary.cleanup()

    def make_asset_release(self) -> Path:
        release = self.root / "asset-release"
        for tree in ("model", "database", "backend-data", "byteff2"):
            (release / tree).mkdir(parents=True)
        (release / "model" / "checkpoint.bin").write_bytes(b"model")
        (release / "database" / "source.csv").write_text("id,value\n1,2\n", encoding="utf-8")
        (release / "backend-data" / "runtime.json").write_text("{}\n", encoding="utf-8")
        (release / "byteff2" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        (release / "byteff2" / "BYTEFF2-COMMIT").write_text(SHA + "\n", encoding="ascii")
        assets: dict[str, list[dict[str, object]]] = {}
        for tree in ("model", "database", "backend-data", "byteff2"):
            records: list[dict[str, object]] = []
            for path in sorted((release / tree).rglob("*")):
                if path.is_file():
                    records.append(
                        {
                            "path": path.relative_to(release / tree).as_posix(),
                            "size": path.stat().st_size,
                            "sha256": release_controller.sha256_file(path).removeprefix("sha256:"),
                        }
                    )
            assets[tree] = records
        manifest = {
            "schema_version": 1,
            "byteff2_commit": SHA,
            "byteff2_submodules": {},
            "assets": assets,
        }
        (release / "ASSET-MANIFEST.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        for current, directories, files in os.walk(release):
            for name in files:
                os.chmod(Path(current) / name, 0o444)
            for name in directories:
                os.chmod(Path(current) / name, 0o555)
        os.chmod(release, 0o555)
        return release

    def build(self, **overrides: object) -> Path:
        output = self.root / "release-manifest.json"
        values: dict[str, object] = {
            "sha": SHA,
            "ci_run_id": "42",
            "backend_image": BACKEND_IMAGE,
            "web_image": WEB_IMAGE,
            "release_bundle": str(self.release_bundle),
            "release_input": str(self.release_input),
            "migration": ["0001_expand:expand"],
            "output": str(output),
        }
        values.update(overrides)
        release_controller.build_manifest(argparse.Namespace(**values))
        return output

    def build_single_bundle(self, **overrides: object) -> Path:
        return self.build(**overrides)

    def build_v2(self, **overrides: object) -> Path:
        return self.build(
            migration=[],
            migration_manifest=str(
                REPOSITORY_ROOT / "backend" / "migrations" / "postgres" / "manifest.json"
            ),
            **overrides,
        )

    def existing_release_controller(self) -> release_controller.ReleaseController:
        manifest = self.build()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            True,
        )
        previous = {
            "status": "success",
            "source_sha": "1" * 40,
            "asset_manifest_digest": DIGEST,
            "byteff2_commit": SHA,
            "migrations": [],
            "approved_contract_migrations": [],
        }
        controller.state_path.parent.mkdir(parents=True)
        controller.state_path.write_text(json.dumps(previous), encoding="utf-8")
        previous_release = controller.ops / "releases" / previous["source_sha"]
        previous_release.mkdir(parents=True)
        (controller.ops / "current").symlink_to(
            Path("releases") / previous["source_sha"]
        )
        asset_root = self.root / "assets"
        controller.document.update(
            {
                "current_asset_manifest_digest": DIGEST,
                "current_asset_root": str(asset_root),
                "current_byteff2_commit": SHA,
                "resolved_asset_manifest_digest": DIGEST,
                "resolved_asset_root": str(asset_root),
                "resolved_byteff2_commit": SHA,
            }
        )
        return controller

    def make_bootstrap_hook(self, name: str) -> Path:
        hook = self.root / name
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(hook, 0o700)
        return hook

    def test_build_manifest_records_immutable_artifacts(self) -> None:
        output = self.build()
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(document["source_sha"], SHA)
        self.assertEqual(document["images"]["backend"], BACKEND_IMAGE)
        self.assertEqual(
            document["release_bundle"]["sha256"],
            release_controller.sha256_file(self.release_bundle),
        )
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_single_bundle_manifest_records_release_input_without_oci_inventory(self) -> None:
        output = self.build_single_bundle()
        document = json.loads(output.read_text(encoding="utf-8"))

        self.assertRegex(document["release_bundle"]["sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(
            set(document),
            {
                "schema_version",
                "release_type",
                "source_sha",
                "ci_run_id",
                "created_at",
                "images",
                "release_bundle",
                "asset_manifest_digest",
                "datasets_on_asset_change",
                "migrations",
            },
        )
        self.assertEqual(document["asset_manifest_digest"], DIGEST)
        self.assertNotIn("all", document["datasets_on_asset_change"])
        release_controller.validate_manifest(document, deployment_mode="auto")

        bundle = output.parent / document["release_bundle"]["name"]
        bundle.write_bytes(b"tampered")
        with self.assertRaisesRegex(release_controller.ReleaseError, "release bundle .*differs"):
            release_controller.verify_manifest_command(
                argparse.Namespace(
                    manifest=str(output),
                    sha=SHA,
                )
            )

    def test_asset_release_verifies_every_manifested_file(self) -> None:
        release = self.make_asset_release()

        resolved, digest, commit = release_controller.inspect_asset_release(release)
        self.assertEqual(resolved, release.resolve())
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(commit, SHA)

        checkpoint = release / "model" / "checkpoint.bin"
        os.chmod(checkpoint, 0o644)
        checkpoint.write_bytes(b"tampered")
        os.chmod(checkpoint, 0o444)
        with self.assertRaisesRegex(release_controller.ReleaseError, "size differs|digest differs"):
            release_controller.inspect_asset_release(release)

    def test_asset_release_rejects_unmanifested_or_writable_content(self) -> None:
        release = self.make_asset_release()
        model = release / "model"
        os.chmod(model, 0o755)
        (model / "unlisted.bin").write_bytes(b"unlisted")
        os.chmod(model / "unlisted.bin", 0o444)
        os.chmod(model, 0o555)

        with self.assertRaisesRegex(release_controller.ReleaseError, "inventory differs"):
            release_controller.inspect_asset_release(release)

    def test_managed_asset_pointer_requires_digest_named_store_release(self) -> None:
        release = self.make_asset_release()
        _, digest, commit = release_controller.inspect_asset_release(release)
        store = self.root / "asset-store" / "releases"
        store.mkdir(parents=True)
        target = store / digest.removeprefix("sha256:")
        os.chmod(release, 0o755)
        release.rename(target)
        os.chmod(target, 0o555)
        pointer = self.root / "production" / "ops" / "current-assets"
        pointer.parent.mkdir(parents=True)
        pointer.symlink_to(target)

        with mock.patch.object(release_controller, "ASSET_RELEASES_ROOT", store):
            resolved, actual_digest, actual_commit = release_controller.inspect_managed_asset_pointer(
                pointer,
                digest,
            )

        self.assertEqual(resolved, target)
        self.assertEqual(actual_digest, digest)
        self.assertEqual(actual_commit, commit)

        wrong_digest = "sha256:" + "f" * 64
        with (
            mock.patch.object(release_controller, "ASSET_RELEASES_ROOT", store),
            self.assertRaisesRegex(release_controller.ReleaseError, "configured digest"),
        ):
            release_controller.inspect_managed_asset_pointer(pointer, wrong_digest)

    def test_production_environment_rejects_unmanaged_assets_and_custom_hooks(self) -> None:
        manifest = self.build(asset_manifest_digest=None)
        production = self.root / "production"
        controller = release_controller.ReleaseController(production, manifest, "auto", False)
        for directory in (
            controller.config_dir,
            controller.ops / "state",
            controller.ops / "releases",
            production / "backups",
        ):
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        release = self.make_asset_release()
        _, digest, _ = release_controller.inspect_asset_release(release)
        controller.document["asset_manifest_digest"] = digest
        store = self.root / "managed-assets" / "releases"
        store.mkdir(parents=True)
        target = store / digest.removeprefix("sha256:")
        os.chmod(release, 0o755)
        release.rename(target)
        os.chmod(target, 0o555)
        pointer = controller.ops / "current-assets"
        pointer.symlink_to(target)
        (controller.config_dir / "app.env").write_text("ONLINE_KNOWLEDGE_API_KEY=\n", encoding="utf-8")
        (controller.config_dir / "worker.env").write_text(
            "APP_POSTGRES_DSN=postgresql://polyprop:random-production-value@127.0.0.1:55432/nexpoly\n"
            "MONOMER_MD_DEFAULT_STEPS=300\n"
            "MONOMER_MD_MAX_STEPS=300\n"
            "MONOMER_MD_MAX_ACTIVE_JOBS=1\n"
            "MONOMER_MD_MAX_CONCURRENT_JOBS=1\n"
            f"BYTEFF2_ROOT={controller.ops / 'current-assets' / 'byteff2'}\n"
            "BYTEFF2_PYTHON=/home/devuser/miniconda3/envs/byteff2-repro/bin/python\n"
            f"PYTHONPATH={controller.ops / 'current'}:"
            f"{controller.ops / 'current-assets' / 'byteff2'}:"
            f"{controller.ops / 'current-assets' / 'byteff2' / 'submodules' / 'bytemol'}\n"
            f"MONOMER_MD_PYTHON={controller.ops / 'current' / 'worker-venv' / 'bin' / 'python'}\n"
            f"MONOMER_MD_JOB_ROOT={controller.ops / 'state' / 'monomer-md-worker-runs'}\n"
            f"MONOMER_MD_WORKER_UDS={controller.ops / 'state' / 'monomer-md-worker-socket' / 'worker.sock'}\n"
            "MONOMER_MD_WORKER_MODE=real\n"
            "MONOMER_MD_GPU_BROKER_ENABLED=false\n"
            "MONOMER_MD_GPU_BROKER_ENVIRONMENT=prod\n"
            f"MONOMER_MD_GPU_BROKER_SOCKET_PATH={controller.ops / 'state' / 'gpu-resource' / 'broker.sock'}\n"
            f"MONOMER_MD_GPU_MPS_PIPE_ROOT={controller.ops / 'state' / 'gpu-resource'}\n"
            "MONOMER_MD_GPU_BROKER_WAIT_TIMEOUT_SECONDS=600\n"
            "MONOMER_MD_GPU_BROKER_HEARTBEAT_INTERVAL_SECONDS=5\n",
            encoding="utf-8",
        )
        deploy_values = (
            "NEXPOLY_POSTGRES_USER=polyprop\n"
            "NEXPOLY_POSTGRES_PASSWORD=random-production-value\n"
            "NEXPOLY_POSTGRES_DB=nexpoly\n"
            "APP_POSTGRES_DSN=postgresql://polyprop:random-production-value@lab-postgres:5432/nexpoly\n"
            "PI_POSTGRES_DSN=postgresql://polyprop:random-production-value@lab-postgres:5432/nexpoly\n"
            "LAB_DATA_POSTGRES_DSN=postgresql://polyprop:random-production-value@lab-postgres:5432/nexpoly\n"
            f"NEXPOLY_ASSET_ROOT={pointer}\n"
            f"NEXPOLY_WORKER_BASE_PYTHON={sys.executable}\n"
            f"NEXPOLY_WORKER_BASE_PYTHON_IDENTITY_SHA256={DIGEST}\n"
            "NEXPOLY_WORKER_CONDA_EXE=/opt/conda/bin/conda\n"
            f"NEXPOLY_WORKER_GMX={Path(sys.executable).parent / 'gmx'}\n"
            "POLYTAO_ENABLED=true\n"
            "NEXPOLY_MIN_FREE_BYTES=1073741824\n"
        )
        controller.env_file.write_text(deploy_values, encoding="utf-8")
        for path in (
            controller.env_file,
            controller.config_dir / "app.env",
            controller.config_dir / "worker.env",
        ):
            os.chmod(path, 0o600)

        with mock.patch.object(release_controller, "ASSET_RELEASES_ROOT", store):
            environment = controller.environment()
        self.assertEqual(environment["NEXPOLY_ASSET_ROOT"], str(pointer))

        controller.env_file.write_text(
            deploy_values.replace("NEXPOLY_POSTGRES_USER=polyprop\n", ""),
            encoding="utf-8",
        )
        os.chmod(controller.env_file, 0o600)
        with (
            mock.patch.object(release_controller, "ASSET_RELEASES_ROOT", store),
            self.assertRaisesRegex(release_controller.ReleaseError, "NEXPOLY_POSTGRES_USER"),
        ):
            controller.environment()
        controller.env_file.write_text(deploy_values, encoding="utf-8")
        os.chmod(controller.env_file, 0o600)

        controller.env_file.write_text(
            deploy_values.replace("NEXPOLY_POSTGRES_DB=nexpoly", "NEXPOLY_POSTGRES_DB=other"),
            encoding="utf-8",
        )
        os.chmod(controller.env_file, 0o600)
        with (
            mock.patch.object(release_controller, "ASSET_RELEASES_ROOT", store),
            self.assertRaisesRegex(release_controller.ReleaseError, "hard-locked.*nexpoly"),
        ):
            controller.environment()
        controller.env_file.write_text(deploy_values, encoding="utf-8")
        os.chmod(controller.env_file, 0o600)

        controller.env_file.write_text(
            deploy_values.replace(
                "PI_POSTGRES_DSN=postgresql://polyprop:random-production-value@lab-postgres:5432/nexpoly",
                "PI_POSTGRES_DSN=postgresql://polyprop:random-production-value@lab-postgres:5432/nexpoly?sslmode=disable",
            ),
            encoding="utf-8",
        )
        os.chmod(controller.env_file, 0o600)
        with (
            mock.patch.object(release_controller, "ASSET_RELEASES_ROOT", store),
            self.assertRaisesRegex(release_controller.ReleaseError, "PI_POSTGRES_DSN"),
        ):
            controller.environment()
        controller.env_file.write_text(deploy_values, encoding="utf-8")
        os.chmod(controller.env_file, 0o600)

        controller.env_file.write_text(deploy_values + "PATH=/tmp/untrusted\n", encoding="utf-8")
        os.chmod(controller.env_file, 0o600)
        with (
            mock.patch.object(release_controller, "ASSET_RELEASES_ROOT", store),
            self.assertRaisesRegex(release_controller.ReleaseError, "forbidden.*PATH"),
        ):
            controller.environment()
        controller.env_file.write_text(deploy_values, encoding="utf-8")
        os.chmod(controller.env_file, 0o600)

        worker_env_path = controller.config_dir / "worker.env"
        worker_env = worker_env_path.read_text(encoding="utf-8")
        worker_env_path.write_text(
            worker_env.replace("127.0.0.1:55432", "lab-postgres:5432"),
            encoding="utf-8",
        )
        os.chmod(worker_env_path, 0o600)
        with (
            mock.patch.object(release_controller, "ASSET_RELEASES_ROOT", store),
            self.assertRaisesRegex(release_controller.ReleaseError, "worker.env APP_POSTGRES_DSN"),
        ):
            controller.environment()
        worker_env_path.write_text(worker_env, encoding="utf-8")
        os.chmod(worker_env_path, 0o600)

        worker_env_path.write_text(
            worker_env.replace("MONOMER_MD_MAX_ACTIVE_JOBS=1", "MONOMER_MD_MAX_ACTIVE_JOBS=2"),
            encoding="utf-8",
        )
        os.chmod(worker_env_path, 0o600)
        with (
            mock.patch.object(release_controller, "ASSET_RELEASES_ROOT", store),
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "MONOMER_MD_MAX_ACTIVE_JOBS must equal",
            ),
        ):
            controller.environment()
        worker_env_path.write_text(worker_env, encoding="utf-8")
        os.chmod(worker_env_path, 0o600)

        worker_env_path.write_text(worker_env + "PATH=/tmp/untrusted\n", encoding="utf-8")
        os.chmod(worker_env_path, 0o600)
        with (
            mock.patch.object(release_controller, "ASSET_RELEASES_ROOT", store),
            self.assertRaisesRegex(release_controller.ReleaseError, "forbidden.*PATH"),
        ):
            controller.environment()
        worker_env_path.write_text(worker_env, encoding="utf-8")
        os.chmod(worker_env_path, 0o600)

        worker_env_path.write_text(
            worker_env.replace(
                f"BYTEFF2_ROOT={controller.ops / 'current-assets' / 'byteff2'}",
                "BYTEFF2_ROOT=/tmp/stale-byteff2",
            ),
            encoding="utf-8",
        )
        os.chmod(worker_env_path, 0o600)
        with (
            mock.patch.object(release_controller, "ASSET_RELEASES_ROOT", store),
            self.assertRaisesRegex(release_controller.ReleaseError, "BYTEFF2_ROOT must equal"),
        ):
            controller.environment()
        worker_env_path.write_text(worker_env, encoding="utf-8")
        os.chmod(worker_env_path, 0o600)

        controller.env_file.write_text(
            deploy_values.replace("POLYTAO_ENABLED=true", "POLYTAO_ENABLED=false"),
            encoding="utf-8",
        )
        os.chmod(controller.env_file, 0o600)
        with (
            mock.patch.object(release_controller, "ASSET_RELEASES_ROOT", store),
            self.assertRaisesRegex(release_controller.ReleaseError, "POLYTAO_ENABLED must be true"),
        ):
            controller.environment()

        controller.env_file.write_text(
            deploy_values + "NEXPOLY_ACTIVE_JOBS_COMMAND=/bin/untrusted\n",
            encoding="utf-8",
        )
        os.chmod(controller.env_file, 0o600)
        with (
            mock.patch.object(release_controller, "ASSET_RELEASES_ROOT", store),
            self.assertRaisesRegex(release_controller.ReleaseError, "custom production drain/job hooks"),
        ):
            controller.environment()

        controller.env_file.write_text(
            deploy_values.replace(f"NEXPOLY_ASSET_ROOT={pointer}", f"NEXPOLY_ASSET_ROOT={target}"),
            encoding="utf-8",
        )
        os.chmod(controller.env_file, 0o600)
        with (
            mock.patch.object(release_controller, "ASSET_RELEASES_ROOT", store),
            self.assertRaisesRegex(release_controller.ReleaseError, "managed production pointer"),
        ):
            controller.environment()

    def test_mutable_image_and_short_sha_are_rejected(self) -> None:
        with self.assertRaisesRegex(release_controller.ReleaseError, "immutable"):
            self.build(backend_image="ghcr.io/lzq390/nexpoly-backend:latest")
        with self.assertRaisesRegex(release_controller.ReleaseError, "40-character"):
            self.build(sha="abc123")

    def test_postgres_dsn_validation_is_exact_and_never_echoes_credentials(self) -> None:
        release_controller.validate_postgres_dsn(
            "postgresql://polyprop:p%40ss%3Aword@lab-postgres:5432/nexpoly",
            "APP_POSTGRES_DSN",
            expected_user="polyprop",
            expected_password="p@ss:word",
            expected_host="lab-postgres",
            expected_port=5432,
            expected_database="nexpoly",
        )

        secret = "never-print-this-password"
        invalid_values = (
            f"postgres://polyprop:{secret}@lab-postgres:5432/nexpoly",
            f"postgresql://polyprop:{secret}@127.0.0.1:5432/nexpoly",
            f"postgresql://polyprop:{secret}@lab-postgres:55432/nexpoly",
            f"postgresql://polyprop:{secret}@lab-postgres:5432/nexpoly/extra",
            f"postgresql://polyprop:{secret}@lab-postgres:5432/nexpoly?sslmode=disable",
            f"postgresql://polyprop:{secret}@lab-postgres:5432/nexpoly#fragment",
            "postgresql://polyprop:bad%ZZ@lab-postgres:5432/nexpoly",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(release_controller.ReleaseError) as caught:
                    release_controller.validate_postgres_dsn(
                        value,
                        "APP_POSTGRES_DSN",
                        expected_user="polyprop",
                        expected_password=secret,
                        expected_host="lab-postgres",
                        expected_port=5432,
                        expected_database="nexpoly",
                    )
                self.assertNotIn(secret, str(caught.exception))

    def test_auto_manifest_retains_contract_for_pending_database_policy(self) -> None:
        manifest = self.build(migration=["0002_contract:contract"])
        document = release_controller.load_manifest(manifest)
        release_controller.validate_manifest(document, deployment_mode="auto")

    def test_v2_release_manifest_binds_epoch_checksum_and_contract_dependencies(self) -> None:
        document = release_controller.load_manifest(self.build_v2())

        release_controller.validate_manifest(document, deployment_mode="auto")

        self.assertEqual(document["schema_version"], 2)
        contract = next(
            record
            for record in document["migrations"]
            if record["version"] == "0012_drop_polytao_jobs"
        )
        self.assertEqual(contract["kind"], "contract")
        self.assertEqual(contract["epoch"], 1)
        self.assertEqual(contract["checksum"], release_controller.POLYTAO_CONTRACT_CHECKSUM)
        self.assertEqual(contract["requires_contracts"], [])

    def test_epoch_two_expand_requires_exact_checksum_approval(self) -> None:
        contract = {
            "version": "0012_drop_polytao_jobs",
            "kind": "contract",
            "epoch": 1,
            "checksum": release_controller.POLYTAO_CONTRACT_CHECKSUM,
            "requires_contracts": [],
        }
        expand = {
            "version": "0013_next_epoch",
            "kind": "expand",
            "epoch": 2,
            "checksum": "d" * 64,
            "requires_contracts": [
                {
                    "version": contract["version"],
                    "checksum": contract["checksum"],
                }
            ],
        }
        candidate = {"schema_version": 2, "migrations": [contract, expand]}
        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "checksum-approved contracts",
        ):
            release_controller.code_deploy_migration_mode(
                {"approved_contracts": []},
                candidate,
                deployment_mode="auto",
                target_sha="e" * 40,
            )

        state = {
            "approved_contracts": [
                {
                    "version": contract["version"],
                    "checksum": contract["checksum"],
                    "operation_id": "contract-0012-fixture",
                    "approved_at": "2026-07-15T00:00:00+00:00",
                }
            ],
            "schema_compatibility_floor": {
                "version": contract["version"],
                "checksum": contract["checksum"],
            },
            "migration_epoch_barrier": {
                "epoch": 1,
                "contract": {
                    "version": contract["version"],
                    "checksum": contract["checksum"],
                },
                "operation_id": "contract-0012-fixture",
                "approved_at": "2026-07-15T00:00:00+00:00",
            },
            "last_contract_operation": "contract-0012-fixture",
        }
        incomplete_state = {"approved_contracts": state["approved_contracts"]}
        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "missing its migration epoch barrier",
        ):
            release_controller.code_deploy_migration_mode(
                incomplete_state,
                candidate,
                deployment_mode="auto",
                target_sha="e" * 40,
            )
        self.assertEqual(
            release_controller.code_deploy_migration_mode(
                state,
                candidate,
                deployment_mode="auto",
                target_sha="e" * 40,
            ),
            "expand",
        )

    def test_contract_approval_is_never_inferred_from_history_or_manifest(self) -> None:
        state = {
            "migrations": ["0012_drop_polytao_jobs"],
            "migration_manifest": [
                {
                    "version": "0012_drop_polytao_jobs",
                    "kind": "contract",
                    "epoch": 1,
                    "checksum": release_controller.POLYTAO_CONTRACT_CHECKSUM,
                    "requires_contracts": [],
                }
            ],
        }

        self.assertEqual(release_controller.approved_contract_migrations(state), {})

        state["schema_compatibility_floor"] = {
            "version": "0012_drop_polytao_jobs",
            "checksum": release_controller.POLYTAO_CONTRACT_CHECKSUM,
        }
        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "missing its migration epoch barrier",
        ):
            release_controller.approved_contract_migrations(state)

    def test_contract_approval_requires_matching_floor_barrier_and_operation(self) -> None:
        approval = {
            "version": "0012_drop_polytao_jobs",
            "checksum": release_controller.POLYTAO_CONTRACT_CHECKSUM,
            "operation_id": "contract-0012-fixture",
            "approved_at": "2026-07-15T00:00:00+00:00",
        }
        state = {
            "approved_contracts": [approval],
            "schema_compatibility_floor": {
                "version": approval["version"],
                "checksum": approval["checksum"],
            },
            "migration_epoch_barrier": {
                "epoch": 1,
                "contract": {
                    "version": approval["version"],
                    "checksum": approval["checksum"],
                },
                "operation_id": approval["operation_id"],
                "approved_at": approval["approved_at"],
            },
            "last_contract_operation": approval["operation_id"],
        }
        self.assertEqual(
            release_controller.approved_contract_migrations(state),
            {approval["version"]: approval["checksum"]},
        )

        for mutation in ("floor", "barrier-operation", "approval-operation"):
            broken = json.loads(json.dumps(state))
            if mutation == "floor":
                broken["schema_compatibility_floor"]["checksum"] = "0" * 64
            elif mutation == "barrier-operation":
                broken["migration_epoch_barrier"]["operation_id"] = "other-operation"
            else:
                broken["approved_contracts"][0]["operation_id"] = "other-operation"
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(release_controller.ReleaseError),
            ):
                release_controller.approved_contract_migrations(broken)

        extra = json.loads(json.dumps(state))
        extra["approved_contracts"].append(
            {
                "version": "0013_unreviewed_contract",
                "checksum": "f" * 64,
                "operation_id": "contract-0013-fixture",
                "approved_at": "2026-07-15T01:00:00+00:00",
            }
        )
        with self.assertRaisesRegex(release_controller.ReleaseError, "invalid approved"):
            release_controller.approved_contract_migrations(extra)

        with self.assertRaisesRegex(release_controller.ReleaseError, "name-only"):
            release_controller.approved_contract_migrations(
                {"approved_contract_migrations": [approval["version"]]}
            )

        for mutation in (
            "barrier-epoch",
            "approval-timestamp",
            "barrier-timestamp",
            "noncanonical-zulu",
        ):
            broken = json.loads(json.dumps(state))
            if mutation == "barrier-epoch":
                broken["migration_epoch_barrier"]["epoch"] = 999
            elif mutation == "approval-timestamp":
                broken["approved_contracts"][0]["approved_at"] = "not-a-timestamp"
            elif mutation == "barrier-timestamp":
                broken["migration_epoch_barrier"]["approved_at"] = "not-a-timestamp"
            else:
                broken["approved_contracts"][0]["approved_at"] = "2026-07-15T00:00:00Z"
                broken["migration_epoch_barrier"]["approved_at"] = "2026-07-15T00:00:00Z"
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(release_controller.ReleaseError),
            ):
                release_controller.approved_contract_migrations(broken)

    def test_migration_runner_output_is_exact_checksum_bound_and_mode_scoped(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build_v2(),
            "auto",
            False,
        )
        policy_source = REPOSITORY_ROOT / "backend" / "migrations" / "postgres"
        policy_target = (
            controller.candidate_dir / "backend" / "migrations" / "postgres"
        )
        shutil.copytree(policy_source, policy_target)
        records = release_controller.release_migrations_from_policy_manifest(
            policy_target / "manifest.json",
            include_baseline=True,
        )

        def output(
            *,
            statuses: dict[str, str] | None = None,
            checksum_overrides: dict[str, str] | None = None,
            duplicate: str | None = None,
            extra: str | None = None,
        ) -> str:
            statuses = statuses or {}
            checksum_overrides = checksum_overrides or {}
            lines = [
                "\t".join(
                    (
                        record["version"],
                        statuses.get(record["version"], "skipped"),
                        checksum_overrides.get(record["version"], record["checksum"]),
                    )
                )
                for record in records
            ]
            if duplicate is not None:
                record = next(item for item in records if item["version"] == duplicate)
                lines.append(f"{duplicate}\tskipped\t{record['checksum']}")
            if extra is not None:
                lines.append(f"{extra}\tskipped\t{'f' * 64}")
            return "\n".join(lines) + "\n"

        valid = subprocess.CompletedProcess(
            [],
            0,
            stdout=output(statuses={"0011_monomer_md_demo_steps": "applied"}),
        )
        with mock.patch.object(release_controller.subprocess, "run", return_value=valid):
            self.assertEqual(
                controller.run_migrations({}, mode="expand"),
                ["0011_monomer_md_demo_steps"],
            )

        invalid_cases = (
            (
                "checksum",
                output(checksum_overrides={"0011_monomer_md_demo_steps": "0" * 64}),
            ),
            ("duplicate", output(duplicate="0011_monomer_md_demo_steps")),
            ("canonical migration set", output(extra="9999_unreviewed")),
            (
                "outside maintenance",
                output(statuses={"0012_drop_polytao_jobs": "applied"}),
            ),
            (
                "baseline may only",
                output(statuses={"0001_app_data_governance": "applied"}),
            ),
        )
        for message, stdout in invalid_cases:
            with (
                self.subTest(message=message),
                mock.patch.object(
                    release_controller.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, stdout=stdout),
                ),
                self.assertRaisesRegex(release_controller.ReleaseError, message),
            ):
                controller.run_migrations({}, mode="expand")

        contract_output = output(
            statuses={"0012_drop_polytao_jobs": "applied"}
        )
        with mock.patch.object(
            release_controller.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, stdout=contract_output),
        ):
            self.assertEqual(
                controller.run_migrations({}, mode="contract-0012"),
                ["0012_drop_polytao_jobs"],
            )

        controller.previous_state = {"migrations": ["0011_monomer_md_demo_steps"]}
        with (
            mock.patch.object(release_controller.subprocess, "run", return_value=valid),
            self.assertRaisesRegex(release_controller.ReleaseError, "re-applied"),
        ):
            controller.run_migrations({}, mode="expand")
        controller.previous_state = {}

        controller.document["migrations"][0]["checksum"] = "0" * 64
        with (
            mock.patch.object(release_controller.subprocess, "run") as run,
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "candidate canonical policy",
            ),
        ):
            controller.run_migrations({}, mode="expand")
        run.assert_not_called()

    def test_0012_maintenance_plan_is_checksum_pinned_and_non_mutating(self) -> None:
        manifest = self.build_v2()
        production = self.root / "absent-production"

        plan = release_controller.PolytaoContractMaintenance(
            production,
            manifest,
            "contract-0012-fixture",
            False,
        ).run()

        self.assertFalse(production.exists())
        self.assertEqual(plan["action"], "maintain-contract-0012")
        self.assertEqual(
            plan["contract"],
            {
                "version": "0012_drop_polytao_jobs",
                "checksum": release_controller.POLYTAO_CONTRACT_CHECKSUM,
                "epoch": 1,
            },
        )

    def test_0012_maintenance_rejects_name_only_release_manifest(self) -> None:
        with self.assertRaisesRegex(release_controller.ReleaseError, "V2 release manifest"):
            release_controller.PolytaoContractMaintenance(
                self.root / "production",
                self.build(),
                "contract-0012-fixture",
                False,
            )

    def test_0012_inventory_requires_exact_database_set_ledger_and_owned_verify_db(
        self,
    ) -> None:
        maintenance = release_controller.PolytaoContractMaintenance(
            self.root / "production",
            self.build_v2(),
            "contract-0012-fixture",
            False,
        )
        policy_source = REPOSITORY_ROOT / "backend" / "migrations" / "postgres"
        policy_target = (
            maintenance.controller.candidate_dir
            / "backend"
            / "migrations"
            / "postgres"
        )
        shutil.copytree(policy_source, policy_target)
        environment = {"NEXPOLY_POSTGRES_USER": "polyprop"}
        payload = {
            "schema_version": 1,
            "target_database": "nexpoly",
            "current_user": "polyprop",
            "databases": [
                {
                    "name": "nexpoly",
                    "owner": "polyprop",
                    "is_template": False,
                    "allow_connections": True,
                },
                {
                    "name": "postgres",
                    "owner": "polyprop",
                    "is_template": False,
                    "allow_connections": True,
                },
                {
                    "name": "template0",
                    "owner": "polyprop",
                    "is_template": True,
                    "allow_connections": False,
                },
                {
                    "name": "template1",
                    "owner": "polyprop",
                    "is_template": True,
                    "allow_connections": True,
                },
            ],
            "ledger": maintenance._canonical_contract_ledger_prefix(
                include_contract=False
            ),
            "legacy_relation_present": True,
        }
        validated = maintenance._validate_database_inventory(
            payload,
            environment,
            allow_contract=False,
            allow_owned_verification=False,
        )
        self.assertEqual(
            validated["database_purposes"]["nexpoly"],
            "production-target",
        )

        unknown = json.loads(json.dumps(payload))
        unknown["databases"].append(
            {
                "name": "nexpoly_shadow",
                "owner": "polyprop",
                "is_template": False,
                "allow_connections": True,
            }
        )
        with self.assertRaisesRegex(release_controller.ReleaseError, "unknown"):
            maintenance._validate_database_inventory(
                unknown,
                environment,
                allow_contract=False,
                allow_owned_verification=False,
            )

        wrong_ledger = json.loads(json.dumps(payload))
        wrong_ledger["ledger"].append(
            {"version": "9999_unreviewed", "checksum": "f" * 64}
        )
        with self.assertRaisesRegex(release_controller.ReleaseError, "exact canonical"):
            maintenance._validate_database_inventory(
                wrong_ledger,
                environment,
                allow_contract=False,
                allow_owned_verification=False,
            )

        verification_database = maintenance._verification_database_name()
        verification = json.loads(json.dumps(payload))
        verification["databases"].append(
            {
                "name": verification_database,
                "owner": "polyprop",
                "is_template": False,
                "allow_connections": True,
            }
        )
        with self.assertRaisesRegex(release_controller.ReleaseError, "unknown"):
            maintenance._validate_database_inventory(
                verification,
                environment,
                allow_contract=False,
                allow_owned_verification=False,
            )
        intent_owner = maintenance._write_verification_owner(
            verification_database,
            "create-intent",
            database_absent_before_create=True,
        )
        owned_intent = maintenance._validate_database_inventory(
            verification,
            environment,
            allow_contract=False,
            allow_owned_verification=True,
        )
        self.assertEqual(
            owned_intent["database_purposes"][verification_database],
            "operation-owned-isolated-restore-create-intent",
        )
        maintenance._write_verification_owner(
            verification_database,
            "created",
            previous=intent_owner,
        )
        owned = maintenance._validate_database_inventory(
            verification,
            environment,
            allow_contract=False,
            allow_owned_verification=True,
        )
        self.assertEqual(
            owned["database_purposes"][verification_database],
            "operation-owned-isolated-restore",
        )

        after_contract = maintenance._canonical_contract_ledger_prefix(
            include_contract=True
        )
        dev_audit = {
            "schema_version": 1,
            "database": "nexpoly_dev",
            "current_user": "polyprop",
            "transaction_read_only": True,
            "ledger": after_contract,
            "legacy_relation_present": False,
        }
        self.assertEqual(
            maintenance._validate_registered_database_audit(
                dev_audit,
                environment,
                "nexpoly_dev",
            ),
            dev_audit,
        )
        writable_dev_audit = json.loads(json.dumps(dev_audit))
        writable_dev_audit["transaction_read_only"] = False
        with self.assertRaisesRegex(release_controller.ReleaseError, "invalid identity"):
            maintenance._validate_registered_database_audit(
                writable_dev_audit,
                environment,
                "nexpoly_dev",
            )
        dirty_dev = json.loads(json.dumps(dev_audit))
        dirty_dev["ledger"][-1]["checksum"] = "0" * 64
        with self.assertRaisesRegex(release_controller.ReleaseError, "exact canonical"):
            maintenance._validate_registered_database_audit(
                dirty_dev,
                environment,
                "nexpoly_dev",
            )
        health_prefixes = maintenance._canonical_contract_ledger_prefixes()
        for prefix in health_prefixes:
            health_audit = {
                "schema_version": 1,
                "database": "nexpoly_md_health_opt",
                "current_user": "polyprop",
                "transaction_read_only": True,
                "ledger": prefix,
                "legacy_relation_present": maintenance._legacy_relation_expected(
                    prefix
                ),
            }
            self.assertEqual(
                maintenance._validate_registered_database_audit(
                    health_audit,
                    environment,
                    "nexpoly_md_health_opt",
                ),
                health_audit,
            )

        through_0008 = health_prefixes[7]
        self.assertEqual(through_0008[-1]["version"], "0008_polytao_backend_runtime")
        gap = json.loads(json.dumps(health_audit))
        gap["ledger"] = [*through_0008[:3], *through_0008[4:]]
        gap["legacy_relation_present"] = True
        with self.assertRaisesRegex(release_controller.ReleaseError, "canonical ledger prefix"):
            maintenance._validate_registered_database_audit(
                gap,
                environment,
                "nexpoly_md_health_opt",
            )

        relation_mismatch = {
            "schema_version": 1,
            "database": "nexpoly_md_health_opt",
            "current_user": "polyprop",
            "transaction_read_only": True,
            "ledger": through_0008,
            "legacy_relation_present": False,
        }
        with self.assertRaisesRegex(release_controller.ReleaseError, "relation state"):
            maintenance._validate_registered_database_audit(
                relation_mismatch,
                environment,
                "nexpoly_md_health_opt",
            )

        external_environment = {
            **environment,
            "NEXPOLY_CONTRACT_0012_DEV_AUDIT_USER": "nexpoly_dev_auditor",
            "NEXPOLY_CONTRACT_0012_MD_HEALTH_AUDIT_USER": "nexpoly_health_auditor",
        }
        external_inventory = {
            "schema_version": 1,
            "inventory_complete": True,
            "writable_target": {
                "stack": "production",
                "database": "nexpoly",
            },
            "databases": [
                {
                    "stack": "nexpoly_dev",
                    "database": "nexpoly_dev",
                    "current_user": "nexpoly_dev_auditor",
                    "transaction_read_only": True,
                    "role_superuser": False,
                    "role_create_db": False,
                    "role_create_role": False,
                    "ledger": after_contract,
                    "legacy_relation_present": False,
                },
                {
                    "stack": "nexpoly_md_health_opt",
                    "database": "nexpoly_md_health_opt",
                    "current_user": "nexpoly_health_auditor",
                    "transaction_read_only": True,
                    "role_superuser": False,
                    "role_create_db": False,
                    "role_create_role": False,
                    "ledger": through_0008,
                    "legacy_relation_present": True,
                },
            ],
        }
        self.assertEqual(
            maintenance._validate_external_database_inventory(
                external_inventory,
                external_environment,
            ),
            external_inventory,
        )
        missing_stack = json.loads(json.dumps(external_inventory))
        missing_stack["databases"].pop()
        with self.assertRaisesRegex(release_controller.ReleaseError, "missing required stacks"):
            maintenance._validate_external_database_inventory(
                missing_stack,
                external_environment,
            )
        writable_stack = json.loads(json.dumps(external_inventory))
        writable_stack["databases"][0]["transaction_read_only"] = False
        with self.assertRaisesRegex(release_controller.ReleaseError, "not provably read-only"):
            maintenance._validate_external_database_inventory(
                writable_stack,
                external_environment,
            )
        wrong_writable_target = json.loads(json.dumps(external_inventory))
        wrong_writable_target["writable_target"] = {
            "stack": "nexpoly_dev",
            "database": "nexpoly_dev",
        }
        with self.assertRaisesRegex(release_controller.ReleaseError, "only writable target"):
            maintenance._validate_external_database_inventory(
                wrong_writable_target,
                external_environment,
            )
        unknown_stack = json.loads(json.dumps(external_inventory))
        unknown_stack["databases"].append(
            {
                **unknown_stack["databases"][0],
                "stack": "nexpoly_shadow",
                "database": "nexpoly_shadow",
            }
        )
        with self.assertRaisesRegex(release_controller.ReleaseError, "unknown"):
            maintenance._validate_external_database_inventory(
                unknown_stack,
                external_environment,
            )

    def test_0012_database_gate_requires_external_stack_evidence(self) -> None:
        maintenance = release_controller.PolytaoContractMaintenance(
            self.root / "production",
            self.build_v2(),
            "contract-0012-fixture",
            False,
        )
        with (
            mock.patch.object(maintenance, "_capture_json", return_value={}),
            mock.patch.object(
                maintenance,
                "_validate_database_inventory",
                return_value={"databases": []},
            ),
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "requires the external database audit command",
            ),
        ):
            maintenance._pre_destructive_database_gate(
                {"NEXPOLY_POSTGRES_USER": "polyprop"}
            )

    def test_0012_registered_stack_audits_are_read_only_and_recovery_reuses_evidence(
        self,
    ) -> None:
        self.assertIn(
            'connection.execute("SET TRANSACTION READ ONLY")',
            release_controller.CONTRACT_0012_DATABASE_AUDIT_PROGRAM,
        )
        self.assertIn(
            '"transaction_read_only": identity["transaction_read_only"] == "on"',
            release_controller.CONTRACT_0012_DATABASE_AUDIT_PROGRAM,
        )
        maintenance = release_controller.PolytaoContractMaintenance(
            self.root / "production",
            self.build_v2(),
            "contract-0012-fixture",
            False,
        )
        recorded = {"recorded": True}
        validated_external = {
            "schema_version": 1,
            "inventory_complete": True,
            "writable_target": {"stack": "production", "database": "nexpoly"},
            "databases": [],
        }
        with (
            mock.patch.object(maintenance, "_capture_json", return_value={}),
            mock.patch.object(
                maintenance,
                "_validate_database_inventory",
                return_value={"databases": []},
            ),
            mock.patch.object(
                maintenance,
                "_validate_external_database_inventory",
                return_value=validated_external,
            ) as validate_external,
            mock.patch.object(
                maintenance,
                "_capture_external_database_inventory",
            ) as capture_external,
        ):
            inventory = maintenance._pre_destructive_database_gate(
                {"NEXPOLY_POSTGRES_USER": "polyprop"},
                recorded_external_inventory=recorded,
            )

        validate_external.assert_called_once_with(
            recorded,
            {"NEXPOLY_POSTGRES_USER": "polyprop"},
        )
        capture_external.assert_not_called()
        self.assertEqual(
            inventory["external_registered_database_inventory"],
            validated_external,
        )

    def test_atomic_state_replace_and_unlink_fsync_parent_directory(self) -> None:
        path = self.root / "state" / "record.json"
        with mock.patch.object(release_controller, "fsync_directory") as fsync:
            release_controller.atomic_json(path, {"ok": True})
            self.assertEqual(fsync.call_args_list[-1], mock.call(path.parent))
            self.assertIn(mock.call(path.parent.parent), fsync.call_args_list)
            fsync.reset_mock()
            release_controller.durable_unlink(path)
            fsync.assert_called_once_with(path.parent)

    def test_0012_archives_are_fsynced_before_they_become_evidence(self) -> None:
        maintenance = release_controller.PolytaoContractMaintenance(
            self.root / "production",
            self.build_v2(),
            "contract-0012-fixture",
            False,
        )
        events: list[str] = []
        real_fsync_file = release_controller.fsync_regular_file

        def tracked_fsync(path: Path) -> None:
            events.append(f"fsync:{path.name}")
            real_fsync_file(path)

        def fake_run(command, *, env, stdin=None, stdout=None) -> None:
            del env, stdin
            if "pg_dump" in command:
                events.append("pg_dump")
                assert stdout is not None
                stdout.write(b"verified fixture dump")
            elif "pg_restore" in command:
                events.append("pg_restore-list")

        evidence = {
            "schema_version": 2,
            "row_count": 9,
            "status_counts": {"completed": 7, "failed": 2},
            "rows_sha256": "a" * 64,
            "schema_sha256": "b" * 64,
            "structure_counts": {
                "columns": 1,
                "indexes": 1,
                "constraints": 1,
                "triggers": 0,
            },
        }
        with (
            mock.patch.object(maintenance.controller, "run", side_effect=fake_run),
            mock.patch.object(maintenance, "_capture_json", return_value=evidence),
            mock.patch.object(
                release_controller,
                "fsync_regular_file",
                side_effect=tracked_fsync,
            ),
        ):
            result = maintenance._archive_legacy_table(
                {"NEXPOLY_POSTGRES_USER": "polyprop", "NEXPOLY_POSTGRES_DB": "nexpoly"},
                {"source_sha": SHA},
                {"external_registered_database_inventory": {"verified": True}},
            )

        self.assertEqual(result, evidence)
        first_dump = events.index("pg_dump")
        first_dump_fsync = next(
            index
            for index, event in enumerate(events)
            if event.startswith("fsync:pre-") and event.endswith(".dump")
        )
        restore_list = events.index("pg_restore-list")
        self.assertLess(first_dump, first_dump_fsync)
        self.assertLess(first_dump_fsync, restore_list)
        self.assertIn("fsync:generation.polytao_jobs.dump", events)
        self.assertIn("fsync:generation.schema.sql", events)
        copied_names = {
            path.name
            for path in maintenance.audit_dir.iterdir()
            if path.name.startswith("pre-")
        }
        for name in copied_names:
            self.assertIn(f"fsync:{name}", events)

    def test_verification_database_create_failure_never_runs_unowned_drop(self) -> None:
        maintenance = release_controller.PolytaoContractMaintenance(
            self.root / "production",
            self.build_v2(),
            "contract-0012-fixture",
            False,
        )
        backup = self.root / "database.dump"
        backup.write_bytes(b"fixture")
        maintenance.controller.backup_path = backup
        with (
            mock.patch.object(
                maintenance,
                "_pre_destructive_database_gate",
                return_value={"databases": []},
            ),
            mock.patch.object(
                maintenance.controller,
                "run",
                side_effect=RuntimeError("createdb failed"),
            ) as run,
            self.assertRaisesRegex(RuntimeError, "createdb failed"),
        ):
            maintenance._verify_full_restore(
                {"NEXPOLY_POSTGRES_USER": "polyprop"},
                {},
            )
        self.assertEqual(run.call_count, 1)
        self.assertEqual(
            release_controller.load_manifest(maintenance.verification_owner_path)["status"],
            "create-intent",
        )

    def test_unknown_createdb_result_exactly_cleans_owned_database(self) -> None:
        maintenance = release_controller.PolytaoContractMaintenance(
            self.root / "production",
            self.build_v2(),
            "contract-0012-fixture",
            False,
        )
        verification_database = maintenance._verification_database_name()
        maintenance._write_verification_owner(
            verification_database,
            "create-intent",
            database_absent_before_create=True,
        )
        before = {"databases": [{"name": verification_database}]}
        after = {"databases": []}
        recorded = {"external_registered_database_inventory": {"verified": True}}

        with (
            mock.patch.object(
                maintenance,
                "_pre_destructive_database_gate",
                side_effect=[before, after],
            ) as gate,
            mock.patch.object(
                maintenance.controller,
                "compose",
                return_value=["drop-exact-owned-verification"],
            ) as compose,
            mock.patch.object(maintenance.controller, "run") as run,
        ):
            result = maintenance._reconcile_owned_verification_database(
                {"NEXPOLY_POSTGRES_USER": "polyprop"},
                recorded_database_inventory=recorded,
            )

        self.assertEqual(
            result,
            {
                "database": verification_database,
                "status": "dropped",
                "present_before_cleanup": True,
                "verified_absent": True,
            },
        )
        self.assertEqual(gate.call_count, 2)
        compose.assert_called_once_with(
            maintenance.controller.candidate_dir,
            "exec",
            "-T",
            "lab-postgres",
            "dropdb",
            "--if-exists",
            "--force",
            "-U",
            "polyprop",
            verification_database,
        )
        run.assert_called_once_with(
            ["drop-exact-owned-verification"],
            env={"NEXPOLY_POSTGRES_USER": "polyprop"},
        )
        self.assertEqual(
            release_controller.load_manifest(maintenance.verification_owner_path)["status"],
            "dropped",
        )

    def test_unknown_createdb_result_absence_closes_intent_without_drop(self) -> None:
        maintenance = release_controller.PolytaoContractMaintenance(
            self.root / "production",
            self.build_v2(),
            "contract-0012-fixture",
            False,
        )
        verification_database = maintenance._verification_database_name()
        maintenance._write_verification_owner(
            verification_database,
            "create-intent",
            database_absent_before_create=True,
        )
        absent = {"databases": []}

        with (
            mock.patch.object(
                maintenance,
                "_pre_destructive_database_gate",
                side_effect=[absent, absent],
            ),
            mock.patch.object(
                maintenance,
                "_drop_owned_verification_database",
            ) as drop,
        ):
            result = maintenance._reconcile_owned_verification_database({})

        drop.assert_not_called()
        self.assertFalse(result["present_before_cleanup"])
        self.assertTrue(result["verified_absent"])
        self.assertEqual(
            release_controller.load_manifest(maintenance.verification_owner_path)["status"],
            "dropped",
        )

    def test_unknown_createdb_inventory_failure_keeps_marker_and_drain(self) -> None:
        operation_id = "contract-0012-fixture"
        production = self.root / "production"
        maintenance = release_controller.PolytaoContractMaintenance(
            production,
            self.build_v2(),
            operation_id,
            False,
        )
        maintenance.apply = True
        previous_state = {
            "status": "success",
            "source_sha": SHA,
            "migrations": [release_controller.POLYTAO_CONTRACT_PREVIOUS_VERSION],
            "approved_contracts": [],
        }
        database_inventory = {
            "external_registered_database_inventory": {"verified": True}
        }
        backup = production / "backups" / "pre-contract.dump"
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"verified database backup")
        maintenance.controller.backup_path = backup
        current_release = production / "ops" / "releases" / SHA
        verification_database = maintenance._verification_database_name()

        def ambiguous_createdb(_environment, _evidence) -> None:
            maintenance._write_verification_owner(
                verification_database,
                "create-intent",
                database_absent_before_create=True,
            )
            raise RuntimeError("createdb result is unknown")

        with (
            mock.patch.object(maintenance.controller, "ensure_root"),
            mock.patch.object(
                maintenance.controller,
                "deployment_lock",
                return_value=ExitStack(),
            ),
            mock.patch.object(
                maintenance,
                "_load_current_state",
                return_value=previous_state,
            ),
            mock.patch.object(maintenance, "_approved_record", return_value=None),
            mock.patch.object(
                maintenance,
                "_bind_current_release",
                return_value=current_release,
            ),
            mock.patch.object(maintenance.controller, "environment", return_value={}),
            mock.patch.object(maintenance.controller, "validate_current_runtime"),
            mock.patch.object(
                maintenance,
                "_pre_destructive_database_gate",
                side_effect=[
                    database_inventory,
                    release_controller.ReleaseError("inventory unavailable"),
                ],
            ),
            mock.patch.object(release_controller, "release_uses_worker", return_value=False),
            mock.patch.object(maintenance.controller, "drain"),
            mock.patch.object(maintenance.controller, "wait_for_jobs"),
            mock.patch.object(
                maintenance,
                "_archive_legacy_table",
                return_value={"schema_version": 2},
            ),
            mock.patch.object(
                maintenance,
                "_verify_full_restore",
                side_effect=ambiguous_createdb,
            ),
            mock.patch.object(maintenance, "_resume_admission") as resume,
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "rollback is incomplete",
            ),
        ):
            maintenance.run()

        resume.assert_not_called()
        self.assertTrue(maintenance.marker_path.is_file())
        self.assertEqual(
            release_controller.load_manifest(maintenance.verification_owner_path)["status"],
            "create-intent",
        )
        other = release_controller.PolytaoContractMaintenance(
            production,
            self.build_v2(),
            "contract-0012-other",
            False,
        )
        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "different 0012 maintenance operation",
        ):
            other._recover(release_controller.load_manifest(maintenance.marker_path))

    def test_0012_recovery_cleans_create_intent_database_after_createdb_crash(self) -> None:
        operation_id = "contract-0012-fixture"
        production = self.root / "production"
        maintenance = release_controller.PolytaoContractMaintenance(
            production,
            self.build_v2(),
            operation_id,
            False,
        )
        previous_state = {
            "status": "success",
            "source_sha": SHA,
            "migrations": [release_controller.POLYTAO_CONTRACT_PREVIOUS_VERSION],
            "approved_contracts": [],
        }
        maintenance.state_path.parent.mkdir(parents=True)
        maintenance.state_path.write_text(json.dumps(previous_state), encoding="utf-8")
        verification_database = maintenance._verification_database_name()
        maintenance._write_verification_owner(
            verification_database,
            "create-intent",
            database_absent_before_create=True,
        )
        marker = {
            "operation_id": operation_id,
            "source_sha": SHA,
            "previous_state": previous_state,
            "database_change_started": False,
            "worker_drain_attempted": False,
        }
        release_controller.atomic_json(maintenance.marker_path, marker)
        current_release = production / "ops" / "releases" / SHA
        inventory_with_verify = {
            "databases": [{"name": verification_database}]
        }
        inventory_without_verify = {"databases": []}

        with (
            mock.patch.object(maintenance.controller, "environment", return_value={}),
            mock.patch.object(
                maintenance,
                "_bind_current_release",
                return_value=current_release,
            ),
            mock.patch.object(
                maintenance,
                "_pre_destructive_database_gate",
                side_effect=[inventory_with_verify, inventory_without_verify],
            ) as gate,
            mock.patch.object(
                maintenance.controller,
                "compose",
                return_value=["drop-owned-verification"],
            ),
            mock.patch.object(maintenance.controller, "run") as run,
            mock.patch.object(maintenance, "_resume_admission") as resume,
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "recovered an interrupted 0012 operation",
            ),
        ):
            maintenance._recover(marker)

        run.assert_called_once_with(["drop-owned-verification"], env={})
        self.assertEqual(gate.call_count, 2)
        self.assertEqual(
            release_controller.load_manifest(maintenance.verification_owner_path)["status"],
            "dropped",
        )
        resume.assert_called_once_with({}, worker_was_drained=False)
        self.assertFalse(maintenance.marker_path.exists())

    def test_0012_recovery_rebuilds_success_journal_after_state_commit(self) -> None:
        operation_id = "contract-0012-fixture"
        production = self.root / "production"
        maintenance = release_controller.PolytaoContractMaintenance(
            production,
            self.build_v2(),
            operation_id,
            False,
        )
        approval = {
            "version": release_controller.POLYTAO_SCHEMA_COMPATIBILITY_FLOOR,
            "checksum": release_controller.POLYTAO_CONTRACT_CHECKSUM,
            "operation_id": operation_id,
            "approved_at": "2026-07-15T00:00:00+00:00",
        }
        previous_state = {
            "status": "success",
            "source_sha": SHA,
            "migrations": [release_controller.POLYTAO_CONTRACT_PREVIOUS_VERSION],
            "approved_contracts": [],
        }
        committed_state = {
            **previous_state,
            "migrations": [
                release_controller.POLYTAO_CONTRACT_PREVIOUS_VERSION,
                release_controller.POLYTAO_SCHEMA_COMPATIBILITY_FLOOR,
            ],
            "approved_contracts": [approval],
            "schema_compatibility_floor": {
                "version": release_controller.POLYTAO_SCHEMA_COMPATIBILITY_FLOOR,
                "checksum": release_controller.POLYTAO_CONTRACT_CHECKSUM,
            },
            "migration_epoch_barrier": {
                "epoch": 1,
                "contract": {
                    "version": release_controller.POLYTAO_SCHEMA_COMPATIBILITY_FLOOR,
                    "checksum": release_controller.POLYTAO_CONTRACT_CHECKSUM,
                },
                "operation_id": operation_id,
                "approved_at": approval["approved_at"],
            },
            "last_contract_operation": operation_id,
        }
        maintenance.state_path.parent.mkdir(parents=True)
        maintenance.state_path.write_text(json.dumps(committed_state), encoding="utf-8")

        backup = production / "backups" / "pre-contract.dump"
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"verified database backup")
        maintenance.audit_dir.mkdir(parents=True)
        os.chmod(maintenance.audit_dir, 0o700)
        (maintenance.audit_dir / "legacy-table-evidence.json").write_text(
            '{"schema_version":1}\n',
            encoding="utf-8",
        )
        os.chmod(maintenance.audit_dir / "legacy-table-evidence.json", 0o600)
        audit_manifest = maintenance._audit_manifest()
        audit_path = maintenance.audit_dir / "AUDIT-MANIFEST.json"
        marker = {
            "operation_id": operation_id,
            "source_sha": SHA,
            "previous_state": previous_state,
            "database_backup": str(backup),
            "database_backup_sha256": release_controller.sha256_file(backup),
            "audit_manifest_sha256": release_controller.sha256_file(audit_path),
            "database_change_started": True,
            "worker_drain_attempted": True,
        }
        release_controller.atomic_json(maintenance.marker_path, marker)
        current_release = production / "ops" / "releases" / SHA

        with (
            mock.patch.object(maintenance.controller, "environment", return_value={}),
            mock.patch.object(
                maintenance,
                "_bind_current_release",
                return_value=current_release,
            ),
            mock.patch.object(
                maintenance,
                "_capture_json",
                return_value={"schema_version": 1, "verified": True},
            ),
            mock.patch.object(
                maintenance,
                "_pre_destructive_database_gate",
                return_value={},
            ),
            mock.patch.object(
                maintenance.controller,
                "compose",
                return_value=["compose"],
            ),
            mock.patch.object(maintenance.controller, "run") as run,
            mock.patch.object(maintenance, "_resume_admission") as resume,
        ):
            recovered = maintenance._recover(marker)

        self.assertEqual(recovered, committed_state)
        self.assertFalse(maintenance.marker_path.exists())
        journal = release_controller.load_manifest(maintenance.journal_path)
        self.assertEqual(journal["status"], "success")
        self.assertEqual(journal["approval"], approval)
        self.assertEqual(journal["audit_manifest"], audit_manifest)
        self.assertEqual(
            maintenance._validate_success_journal(journal, approval),
            journal,
        )
        run.assert_called_once_with(["compose"], env={})
        resume.assert_called_once_with({}, worker_was_drained=True)

    def test_0012_rollback_recovery_journals_before_requiring_new_operation_id(self) -> None:
        operation_id = "contract-0012-fixture"
        production = self.root / "production"
        maintenance = release_controller.PolytaoContractMaintenance(
            production,
            self.build_v2(),
            operation_id,
            False,
        )
        previous_state = {
            "status": "success",
            "source_sha": SHA,
            "migrations": [release_controller.POLYTAO_CONTRACT_PREVIOUS_VERSION],
            "approved_contracts": [],
        }
        maintenance.state_path.parent.mkdir(parents=True)
        maintenance.state_path.write_text(json.dumps(previous_state), encoding="utf-8")
        marker = {
            "operation_id": operation_id,
            "source_sha": SHA,
            "previous_state": previous_state,
            "database_change_started": False,
            "worker_drain_attempted": False,
        }
        release_controller.atomic_json(maintenance.marker_path, marker)
        current_release = production / "ops" / "releases" / SHA

        with (
            mock.patch.object(maintenance.controller, "environment", return_value={}),
            mock.patch.object(
                maintenance,
                "_bind_current_release",
                return_value=current_release,
            ),
            mock.patch.object(
                maintenance,
                "_pre_destructive_database_gate",
                return_value={},
            ),
            mock.patch.object(maintenance, "_resume_admission") as resume,
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "new operation ID",
            ),
        ):
            maintenance._recover(marker)

        self.assertFalse(maintenance.marker_path.exists())
        journal = release_controller.load_manifest(maintenance.journal_path)
        self.assertEqual(journal["status"], "recovered")
        self.assertTrue(journal["retry_requires_new_operation_id"])
        resume.assert_called_once_with({}, worker_was_drained=False)





    def test_resume_failure_keeps_durable_verified_state_and_marker(self) -> None:
        manifest = self.build()
        production = self.root / "production"
        controller = release_controller.ReleaseController(production, manifest, "auto", True)
        previous = {
            "status": "success",
            "source_sha": "1" * 40,
            "asset_manifest_digest": DIGEST,
            "byteff2_commit": SHA,
            "migrations": [],
            "approved_contract_migrations": [],
        }
        controller.state_path.parent.mkdir(parents=True)
        controller.state_path.write_text(json.dumps(previous), encoding="utf-8")
        asset_root = self.root / "assets"
        controller.document.update(
            {
                "current_asset_manifest_digest": DIGEST,
                "current_asset_root": str(asset_root),
                "current_byteff2_commit": SHA,
                "resolved_asset_manifest_digest": DIGEST,
                "resolved_asset_root": str(asset_root),
                "resolved_byteff2_commit": SHA,
            }
        )
        backup = production / "backups" / "fixture.dump"
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"verified backup")
        resume_attempts = 0

        def prepare(_environment: dict[str, str]) -> None:
            controller.staging.mkdir(parents=True)

        def drain(_environment: dict[str, str], enabled: bool) -> None:
            nonlocal resume_attempts
            if not enabled:
                resume_attempts += 1
                if resume_attempts == 1:
                    raise release_controller.ReleaseError("resume failed")

        def create_backup(_environment: dict[str, str], _from_sha: str) -> None:
            controller.backup_path = backup

        with ExitStack() as stack:
            patched = stack.enter_context(
                mock.patch.multiple(
                    controller,
                    ensure_root=mock.DEFAULT,
                    validate_current_runtime=mock.DEFAULT,
                    verify_image_labels=mock.DEFAULT,
                    assert_still_current_main=mock.DEFAULT,
                    run=mock.DEFAULT,
                    wait_for_jobs=mock.DEFAULT,
                    switch_current=mock.DEFAULT,
                    restart_or_defer_worker=mock.DEFAULT,
                    backend_healthcheck=mock.DEFAULT,
                    run_ingress_isolated_contract_smoke=mock.DEFAULT,
                    run_ingress_isolated_monomer_smoke=mock.DEFAULT,
                    run_isolated_web_smoke=mock.DEFAULT,
                    healthcheck=mock.DEFAULT,
                    rollback_runtime=mock.DEFAULT,
                )
            )
            stack.enter_context(mock.patch.object(controller, "environment", return_value={}))
            stack.enter_context(
                mock.patch.object(controller, "prepare_staging", side_effect=prepare)
            )
            stack.enter_context(mock.patch.object(controller, "drain", side_effect=drain))
            stack.enter_context(
                mock.patch.object(controller, "backup_database", side_effect=create_backup)
            )
            stack.enter_context(mock.patch.object(controller, "run_migrations", return_value=[]))
            stack.enter_context(
                mock.patch.object(controller, "candidate_asset_environment", return_value={})
            )
            stack.enter_context(
                mock.patch.object(
                    controller,
                    "drain_worker",
                    return_value={"supported": True},
                )
            )
            stack.enter_context(
                self.assertRaisesRegex(release_controller.ReleaseError, "resume failed")
            )
            controller.deploy()

        patched["rollback_runtime"].assert_not_called()
        committed = json.loads(controller.state_path.read_text(encoding="utf-8"))
        self.assertEqual(committed["source_sha"], SHA)
        self.assertEqual(committed["status"], "success")
        marker = json.loads(controller.in_progress_path.read_text(encoding="utf-8"))
        self.assertEqual(marker["status"], "verified-resume-pending")
        self.assertEqual(resume_attempts, 1)

    def test_asset_change_failure_restores_dump_pointer_and_previous_runtime(self) -> None:
        manifest = self.build_single_bundle()
        production = self.root / "production"
        controller = release_controller.ReleaseController(production, manifest, "auto", True)
        previous_sha = "1" * 40
        previous = {
            "status": "success",
            "source_sha": previous_sha,
            "asset_manifest_digest": "sha256:" + "f" * 64,
            "byteff2_commit": "2" * 40,
            "migrations": [],
            "approved_contract_migrations": [],
        }
        controller.state_path.parent.mkdir(parents=True)
        controller.state_path.write_text(json.dumps(previous), encoding="utf-8")
        old_assets = self.root / "old-assets"
        target_assets = self.root / "target-assets"
        controller.document.update(
            {
                "current_asset_manifest_digest": previous["asset_manifest_digest"],
                "current_asset_root": str(old_assets),
                "current_byteff2_commit": previous["byteff2_commit"],
                "resolved_asset_manifest_digest": DIGEST,
                "resolved_asset_root": str(target_assets),
                "resolved_byteff2_commit": SHA,
            }
        )

        def prepare(_environment: dict[str, str]) -> None:
            controller.staging.mkdir(parents=True)

        backup = production / "backups" / "fixture.dump"
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"verified backup")

        def create_backup(_environment: dict[str, str], _from_sha: str) -> None:
            controller.backup_path = backup

        patched_names = {
            name: mock.DEFAULT
            for name in (
                "ensure_root",
                "validate_current_runtime",
                "prepare_staging",
                "verify_image_labels",
                "assert_still_current_main",
                "run",
                "drain",
                "wait_for_jobs",
                "rebuild_datasets",
                "refresh_analytics_snapshot",
                "switch_asset_pointer",
                "switch_current",
                "restart_or_defer_worker",
                "backend_healthcheck",
                "run_ingress_isolated_contract_smoke",
                "run_ingress_isolated_monomer_smoke",
                "run_isolated_web_smoke",
                "restore_database",
                "rollback_runtime",
            )
        }
        with (
            mock.patch.multiple(controller, **patched_names) as patched,
            mock.patch.object(controller, "environment", return_value={}),
            mock.patch.object(controller, "backup_database", side_effect=create_backup),
            mock.patch.object(controller, "drain_worker", return_value={"supported": True}),
            mock.patch.object(controller, "run_migrations", return_value=[]),
            mock.patch.object(
                controller,
                "healthcheck",
                side_effect=release_controller.ReleaseError("post-switch failure"),
            ),
            mock.patch.object(controller, "recover_drained_worker", return_value="resumed"),
        ):
            patched["prepare_staging"].side_effect = prepare
            with self.assertRaisesRegex(release_controller.ReleaseError, "post-switch failure"):
                controller.deploy()

        patched["rebuild_datasets"].assert_called_once()
        self.assertEqual(
            [call.args[0] for call in patched["switch_asset_pointer"].call_args_list],
            [target_assets, old_assets],
        )
        patched["restore_database"].assert_called_once_with(
            mock.ANY,
            release=production / "ops" / "releases" / previous_sha,
        )
        patched["rollback_runtime"].assert_called_once()
        self.assertFalse(controller.in_progress_path.exists())
        self.assertFalse(controller.release_dir.exists())
        self.assertFalse(controller.staging.exists())
        controller.staging.mkdir()
        self.assertTrue(controller.staging.is_dir())



    def test_safe_extract_rejects_parent_path_and_links(self) -> None:
        malicious = self.root / "malicious.tar"
        with tarfile.open(malicious, "w") as archive:
            content = b"bad"
            info = tarfile.TarInfo("../outside")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        with self.assertRaisesRegex(release_controller.ReleaseError, "unsafe archive"):
            release_controller.safe_extract_tar(malicious, self.root / "extract-parent")

        symlink = self.root / "symlink.tar"
        with tarfile.open(symlink, "w") as archive:
            info = tarfile.TarInfo("link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
        with self.assertRaisesRegex(release_controller.ReleaseError, "unsupported"):
            release_controller.safe_extract_tar(symlink, self.root / "extract-link")

    def test_deploy_without_apply_is_a_non_mutating_plan(self) -> None:
        manifest = self.build()
        production = self.root / "absent-production"
        plan = release_controller.ReleaseController(production, manifest, "auto", False).deploy()
        self.assertFalse(plan["apply"])
        self.assertFalse(production.exists())

    def test_production_compose_preflight_accepts_only_supported_versions(self) -> None:
        accepted = (
            ("2.24.4\n", (2, 24, 4)),
            ("v2.24.4-desktop.1\n", (2, 24, 4)),
            ("2.40.0+vendor.1\n", (2, 40, 0)),
        )
        for output, expected in accepted:
            with self.subTest(output=output):
                completed = subprocess.CompletedProcess([], 0, stdout=output)
                with mock.patch.object(
                    release_controller.subprocess,
                    "run",
                    return_value=completed,
                ) as run:
                    actual = release_controller.require_docker_compose_version({"PATH": "/bin"})
                self.assertEqual(actual, expected)
                run.assert_called_once_with(
                    ["docker", "compose", "version", "--short"],
                    env={"PATH": "/bin"},
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                )

        rejected = (
            subprocess.CompletedProcess([], 0, stdout="2.24.3\n"),
            subprocess.CompletedProcess([], 0, stdout="Docker Compose version v2.24.4\n"),
            subprocess.CompletedProcess([], 1, stdout="2.40.0\n"),
        )
        for completed in rejected:
            with (
                self.subTest(returncode=completed.returncode, output=completed.stdout),
                mock.patch.object(
                    release_controller.subprocess,
                    "run",
                    return_value=completed,
                ),
                self.assertRaisesRegex(
                    release_controller.ReleaseError,
                    r"Docker Compose >= 2\.24\.4",
                ),
            ):
                release_controller.require_docker_compose_version()

    def test_apply_checks_compose_version_before_entering_deployment(self) -> None:
        manifest = self.build()
        production = self.root / "production"
        production.mkdir()
        controller = release_controller.ReleaseController(
            production,
            manifest,
            "auto",
            True,
        )
        with (
            mock.patch.dict(os.environ, {"NEXPOLY_ALLOW_TEST_ROOT": "1"}),
            mock.patch.object(
                release_controller,
                "require_docker_compose_version",
            ) as require_compose,
        ):
            controller.ensure_root()
        require_compose.assert_called_once()

    def test_same_sha_redeploy_preserves_distinct_rollback_target(self) -> None:
        current = "b" * 40
        previous = "c" * 40
        state = {"source_sha": current, "previous_release": previous}

        self.assertEqual(
            release_controller.previous_release_for_deploy(state, current),
            previous,
        )
        self.assertEqual(
            release_controller.previous_release_for_deploy(state, SHA),
            current,
        )
        self.assertEqual(
            release_controller.previous_release_for_deploy({}, SHA),
            "bootstrap",
        )
        with self.assertRaisesRegex(release_controller.ReleaseError, "missing previous_release"):
            release_controller.previous_release_for_deploy({"source_sha": current}, current)
        with self.assertRaisesRegex(release_controller.ReleaseError, "no distinct rollback"):
            release_controller.previous_release_for_deploy(
                {"source_sha": current, "previous_release": current},
                current,
            )

    def test_apply_refuses_an_unconfirmed_nonproduction_root(self) -> None:
        manifest = self.build()
        production = self.root / "production"
        production.mkdir()
        controller = release_controller.ReleaseController(production, manifest, "auto", True)
        with self.assertRaisesRegex(release_controller.ReleaseError, "allowed only"):
            controller.deploy()

    def test_first_release_is_server_gated_to_explicit_bootstrap_hooks(self) -> None:
        manifest = self.build()
        production = self.root / "production"
        production.mkdir()

        auto = release_controller.ReleaseController(production, manifest, "auto", True)
        with (
            mock.patch.object(auto, "ensure_root"),
            mock.patch.object(auto, "environment", return_value={}),
            self.assertRaisesRegex(release_controller.ReleaseError, "first release requires --mode bootstrap"),
        ):
            auto.deploy()

        bootstrap = release_controller.ReleaseController(production, manifest, "bootstrap", True)
        with (
            mock.patch.object(bootstrap, "ensure_root"),
            mock.patch.object(
                bootstrap,
                "environment",
                return_value={"NEXPOLY_BOOTSTRAP_RELEASE_SHA": SHA},
            ),
            self.assertRaisesRegex(release_controller.ReleaseError, "NEXPOLY_BOOTSTRAP_QUIESCE_COMMAND"),
        ):
            bootstrap.deploy()

        initialized = release_controller.ReleaseController(production, manifest, "bootstrap", True)
        initialized.state_path.parent.mkdir(parents=True, exist_ok=True)
        initialized.state_path.write_text("{}", encoding="utf-8")
        with (
            mock.patch.object(initialized, "ensure_root"),
            mock.patch.object(initialized, "environment", return_value={}),
            self.assertRaisesRegex(release_controller.ReleaseError, "bootstrap is forbidden"),
        ):
            initialized.deploy()

    def test_bootstrap_rollback_removes_only_its_failed_release(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            True,
        )
        controller.release_dir.mkdir(parents=True)
        controller.ops.mkdir(exist_ok=True)
        current = controller.ops / "current"
        current.symlink_to(controller.release_dir.relative_to(controller.ops))

        controller.clear_failed_bootstrap_release()
        self.assertFalse(current.exists())
        self.assertFalse(current.is_symlink())
        self.assertFalse(controller.release_dir.exists())

        other = controller.ops / "releases" / ("b" * 40)
        other.mkdir()
        controller.release_dir.mkdir()
        current.symlink_to(other.relative_to(controller.ops))
        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "does not reference the target release",
        ):
            controller.clear_failed_bootstrap_release()
        self.assertTrue(current.is_symlink())
        self.assertTrue(controller.release_dir.is_dir())

    def test_second_controller_fails_before_state_change_when_lock_is_held(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            True,
        )
        lock_path = controller.ops / "state" / "deploy.lock"
        lock_path.parent.mkdir(parents=True)
        with lock_path.open("a+", encoding="utf-8") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(
                release_controller.ReleaseError,
                "another production deployment holds deploy.lock",
            ):
                with controller.deployment_lock():
                    self.fail("the second deployment unexpectedly acquired deploy.lock")

        self.assertFalse(controller.state_path.exists())

    def test_unrecorded_staging_cleanup_removes_directory_and_rejects_symlink(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            True,
        )
        controller.staging.mkdir(parents=True)
        (controller.staging / "partial").write_text("interrupted\n", encoding="utf-8")

        controller.cleanup_unrecorded_staging()
        self.assertFalse(controller.staging.exists())

        outside = self.root / "outside-staging"
        outside.mkdir()
        controller.staging.symlink_to(outside)
        with self.assertRaisesRegex(release_controller.ReleaseError, "not a safe directory"):
            controller.cleanup_unrecorded_staging()
        self.assertTrue(controller.staging.is_symlink())
        self.assertTrue(outside.is_dir())

    def test_interrupted_deployment_marker_fails_before_environment_or_mutation(self) -> None:
        manifest = self.build_single_bundle()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            True,
        )
        controller.in_progress_path.parent.mkdir(parents=True)
        controller.in_progress_path.write_text(
            json.dumps({"schema_version": 1, "phase": "db-changed"}),
            encoding="utf-8",
        )
        with (
            mock.patch.object(controller, "ensure_root"),
            mock.patch.object(controller, "environment") as environment,
            self.assertRaisesRegex(release_controller.ReleaseError, "interrupted release SHA"),
        ):
            controller.deploy()
        environment.assert_not_called()

    def test_verified_interrupted_deploy_only_resumes_admission(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            True,
        )
        committed = {"status": "success", "source_sha": SHA}
        controller.state_path.parent.mkdir(parents=True)
        controller.state_path.write_text(json.dumps(committed), encoding="utf-8")
        marker = {
            "source_sha": SHA,
            "phase": "verified",
            "previous_state": {"status": "success", "source_sha": "1" * 40},
            "bootstrap": False,
        }
        controller.in_progress_path.write_text(json.dumps(marker), encoding="utf-8")

        with (
            mock.patch.object(controller, "environment", return_value={}),
            mock.patch.object(controller, "validate_current_runtime") as validate_runtime,
            mock.patch.object(controller, "drain") as drain,
            mock.patch.object(controller, "restore_database") as restore_database,
            mock.patch.object(controller, "rollback_runtime") as rollback_runtime,
        ):
            controller.recover_interrupted_deployment(marker)

        validate_runtime.assert_called_once_with({})
        drain.assert_called_once_with({}, False)
        restore_database.assert_not_called()
        rollback_runtime.assert_not_called()
        self.assertFalse(controller.in_progress_path.exists())

    def test_bootstrap_interrupted_recovery_proves_legacy_runtime_then_conditionally_resumes(
        self,
    ) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "bootstrap",
            True,
        )
        controller.staging.mkdir(parents=True)
        marker = {
            "source_sha": SHA,
            "phase": "prepared",
            "previous_state": {},
            "bootstrap": True,
            "drain_attempted": True,
        }
        controller.in_progress_path.parent.mkdir(parents=True, exist_ok=True)
        controller.in_progress_path.write_text(json.dumps(marker), encoding="utf-8")
        events: list[str] = []

        with (
            mock.patch.object(controller, "environment", return_value={}),
            mock.patch.object(
                controller,
                "run_bootstrap_rollback",
                side_effect=lambda _environment: events.append("rollback-evidence"),
            ),
            mock.patch.object(
                controller,
                "drain",
                side_effect=lambda _environment, enabled: events.append(
                    "drain" if enabled else "conditional-resume"
                ),
            ),
            mock.patch.object(
                controller,
                "clear_failed_bootstrap_release",
                side_effect=lambda: events.append("cleanup"),
            ),
        ):
            controller.recover_interrupted_deployment(marker)

        self.assertEqual(events, ["rollback-evidence", "conditional-resume", "cleanup"])
        self.assertFalse(controller.staging.exists())
        self.assertFalse(controller.in_progress_path.exists())

    def test_interrupted_data_change_restores_old_identity_database_and_runtime(self) -> None:
        manifest = self.build()
        production = self.root / "production"
        controller = release_controller.ReleaseController(production, manifest, "auto", True)
        previous_sha = "1" * 40
        previous_release = controller.ops / "releases" / previous_sha
        previous_release.mkdir(parents=True)
        controller.staging.mkdir()
        old_assets = self.root / "old-assets"
        old_digest = "sha256:" + "f" * 64
        old_commit = "2" * 40
        backup = production / "backups" / "interrupted.dump"
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"verified interrupted backup")
        marker = {
            "source_sha": SHA,
            "phase": "db-changed",
            "previous_state": {"status": "success", "source_sha": previous_sha},
            "bootstrap": False,
            "database_change_started": True,
            "data_change_started": True,
            "runtime_switch_started": False,
            "previous_asset_root": str(old_assets),
            "previous_asset_digest": old_digest,
            "database_backup": str(backup),
            "database_backup_sha256": release_controller.sha256_file(backup),
        }
        controller.in_progress_path.parent.mkdir(parents=True, exist_ok=True)
        controller.in_progress_path.write_text(json.dumps(marker), encoding="utf-8")
        environment = {"NEXPOLY_ASSET_MANIFEST_DIGEST": DIGEST}
        rollback_evidence: dict[str, object] = {}

        def record_rollback(actual_environment: dict[str, str]) -> None:
            rollback_evidence.update(
                {
                    "environment_digest": actual_environment[
                        "NEXPOLY_ASSET_MANIFEST_DIGEST"
                    ],
                    "asset_root": controller.document["current_asset_root"],
                    "asset_digest": controller.document["current_asset_manifest_digest"],
                    "byteff2_commit": controller.document["current_byteff2_commit"],
                }
            )

        with (
            mock.patch.object(controller, "environment", return_value=environment),
            mock.patch.object(
                release_controller,
                "inspect_asset_release",
                return_value=(old_assets, old_digest, old_commit),
            ),
            mock.patch.object(controller, "switch_asset_pointer") as switch_assets,
            mock.patch.object(controller, "run"),
            mock.patch.object(controller, "restore_database") as restore_database,
            mock.patch.object(controller, "rollback_runtime", side_effect=record_rollback),
            mock.patch.object(controller, "drain") as drain,
        ):
            controller.recover_interrupted_deployment(marker)

        switch_assets.assert_called_once_with(old_assets)
        restore_database.assert_called_once_with(environment, release=previous_release)
        self.assertEqual(
            rollback_evidence,
            {
                "environment_digest": old_digest,
                "asset_root": str(old_assets),
                "asset_digest": old_digest,
                "byteff2_commit": old_commit,
            },
        )
        drain.assert_called_once_with(environment, False)
        self.assertFalse(controller.staging.exists())
        self.assertFalse(controller.in_progress_path.exists())

    def test_prepared_interrupted_deploy_cleans_staging_and_allows_same_sha_retry(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            True,
        )
        previous_sha = "1" * 40
        previous_release = controller.ops / "releases" / previous_sha
        previous_release.mkdir(parents=True)
        current = controller.ops / "current"
        current.symlink_to(Path("releases") / previous_sha)
        controller.staging.mkdir()
        marker = {
            "source_sha": SHA,
            "phase": "prepared",
            "previous_state": {"status": "success", "source_sha": previous_sha},
            "bootstrap": False,
            "worker_drain_attempted": True,
            "database_change_started": False,
            "data_change_started": False,
            "runtime_switch_started": False,
        }
        controller.in_progress_path.parent.mkdir(parents=True, exist_ok=True)
        controller.in_progress_path.write_text(json.dumps(marker), encoding="utf-8")

        with (
            mock.patch.object(controller, "environment", return_value={}),
            mock.patch.object(controller, "resume_worker") as resume_worker,
            mock.patch.object(controller, "drain") as drain,
        ):
            controller.recover_interrupted_deployment(marker)

        resume_worker.assert_called_once_with({})
        drain.assert_called_once_with({}, False)
        self.assertFalse(controller.staging.exists())
        self.assertFalse(controller.in_progress_path.exists())
        controller.staging.mkdir()
        self.assertTrue(controller.staging.is_dir())






    def test_contract_gpu_smoke_uses_real_async_apis_and_validates_svg(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            False,
        )
        with mock.patch.object(controller, "run") as run:
            controller.run_contract_gpu_api_smoke(
                {"NEXPOLY_CONTRACT_GPU_SMOKE_TIMEOUT_SECONDS": "600"}
            )

        command = run.call_args.args[0]
        self.assertIn("exec", command)
        self.assertIn("backend", command)
        self.assertIn("python", command)
        self.assertIn("-c", command)
        self.assertEqual(command[-1], "600")
        program = command[-2]
        compile(program, "contract GPU API smoke", "exec")
        self.assertIn('/api/v1/conditional-generation/tg/jobs', program)
        self.assertIn('/api/v1/conditional-generation/polytao/jobs', program)
        self.assertIn('status != 202', program)
        self.assertIn('phase == "completed"', program)
        self.assertIn('generated_smiles', program)
        self.assertIn('structure_svg', program)
        self.assertIn('endswith("</svg>")', program)

    def test_contract_gpu_smoke_can_target_previous_release_compose_tree(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            False,
        )
        previous = self.root / "previous-release"
        previous.mkdir()
        with mock.patch.object(controller, "run") as run:
            controller.run_contract_gpu_api_smoke({}, release=previous)

        command = run.call_args.args[0]
        self.assertIn(str(previous / "docker-compose.yml"), command)
        self.assertNotIn(str(controller.release_dir / "docker-compose.yml"), command)

    def test_contract_smoke_temporarily_opens_only_the_isolated_backend(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            False,
        )
        calls: list[tuple[str, object]] = []

        with (
            mock.patch.object(
                controller,
                "drain",
                side_effect=lambda _environment, enabled: calls.append(("drain", enabled)),
            ),
            mock.patch.object(
                controller,
                "run_contract_gpu_api_smoke",
                side_effect=lambda _environment, **_kwargs: calls.append(("smoke", None)),
            ),
        ):
            controller.run_ingress_isolated_contract_smoke({})

        self.assertEqual(calls, [("drain", False), ("smoke", None), ("drain", True)])

    def test_contract_smoke_failure_reenables_drain_before_propagating(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            False,
        )
        drain_states: list[bool] = []
        with (
            mock.patch.object(
                controller,
                "drain",
                side_effect=lambda _environment, enabled: drain_states.append(enabled),
            ),
            mock.patch.object(
                controller,
                "run_contract_gpu_api_smoke",
                side_effect=release_controller.ReleaseError("smoke failed"),
            ),
        ):
            with self.assertRaisesRegex(release_controller.ReleaseError, "smoke failed"):
                controller.run_ingress_isolated_contract_smoke({})

        self.assertEqual(drain_states, [False, True])

    def test_contract_smoke_resume_response_loss_still_redrains_and_propagates(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            False,
        )
        drain_states: list[bool] = []

        def drain(_environment: dict[str, str], enabled: bool) -> None:
            drain_states.append(enabled)
            if not enabled:
                raise release_controller.ReleaseError("conditional resume response lost")

        with (
            mock.patch.object(controller, "drain", side_effect=drain),
            mock.patch.object(controller, "run_contract_gpu_api_smoke") as smoke,
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "conditional resume response lost",
            ),
        ):
            controller.run_ingress_isolated_contract_smoke({})

        self.assertEqual(drain_states, [False, True])
        smoke.assert_not_called()

    def test_web_smoke_runs_exact_digest_without_host_network_and_always_cleans_up(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            False,
        )
        html = b'<html><div id="root"></div><script src="/assets/app-123.js"></script></html>'
        completed = [
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0, stdout=html),
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0),
        ]
        with (
            mock.patch.object(controller, "run") as run,
            mock.patch.object(
                release_controller.subprocess,
                "run",
                side_effect=completed,
            ) as subprocess_run,
        ):
            controller.run_isolated_web_smoke({})

        launch = run.call_args.args[0]
        self.assertEqual(launch[:3], ["docker", "run", "-d"])
        self.assertIn("--network", launch)
        self.assertEqual(launch[launch.index("--network") + 1], "none")
        self.assertNotIn("-p", launch)
        self.assertEqual(launch[-1], WEB_IMAGE)
        container = f"nexpoly-web-smoke-{SHA[:12]}"
        html_command = subprocess_run.call_args_list[1].args[0]
        asset_command = subprocess_run.call_args_list[2].args[0]
        self.assertEqual(html_command[:3], ["docker", "exec", container])
        self.assertEqual(html_command[-1], "http://127.0.0.1/")
        self.assertEqual(asset_command[:3], ["docker", "exec", container])
        self.assertEqual(asset_command[-1], "http://127.0.0.1/assets/app-123.js")
        cleanup_commands = [
            call.args[0]
            for call in subprocess_run.call_args_list
            if call.args[0][:3] == ["docker", "rm", "-f"]
        ]
        self.assertEqual(len(cleanup_commands), 2)
        self.assertEqual(cleanup_commands[0], cleanup_commands[1])

    def test_public_web_static_smoke_fetches_root_and_referenced_asset(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            False,
        )

        class Response:
            def __init__(self, payload: bytes, content_type: str) -> None:
                self.payload = payload
                self.status = 200
                self.headers = mock.Mock()
                self.headers.get_content_type.return_value = content_type

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _limit: int) -> bytes:
                return self.payload

        html = b'<html><div id="root"></div><script src="/assets/app-123.js"></script></html>'
        with mock.patch.object(
            release_controller.urllib.request,
            "urlopen",
            side_effect=[
                Response(html, "text/html"),
                Response(b"console.log('ok')", "application/javascript"),
            ],
        ) as urlopen:
            controller.public_web_static_smoke(
                {"NEXPOLY_WEB_BASE_URL": "http://127.0.0.1:9000/"}
            )

        self.assertEqual(
            [call.args[0] for call in urlopen.call_args_list],
            [
                "http://127.0.0.1:9000/",
                "http://127.0.0.1:9000/assets/app-123.js",
            ],
        )

    def test_monomer_smoke_runs_inside_backend_while_nginx_is_stopped(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            False,
        )
        script = controller.release_dir / "scripts" / "monomer_md_smoke.py"
        script.parent.mkdir(parents=True)
        script.write_text("print('fixture')\n", encoding="utf-8")
        asset_root = self.root / "pinned-assets"
        commit_marker = asset_root / "byteff2" / "BYTEFF2-COMMIT"
        commit_marker.parent.mkdir(parents=True)
        commit_marker.write_text(SHA + "\n", encoding="ascii")
        calls: list[tuple[str, object]] = []

        with (
            mock.patch.object(
                controller,
                "run",
                side_effect=lambda command, **_kwargs: calls.append(("run", command)),
            ),
            mock.patch.object(
                controller,
                "drain",
                side_effect=lambda _environment, enabled: calls.append(("drain", enabled)),
            ),
        ):
            controller.run_ingress_isolated_monomer_smoke(
                {"NEXPOLY_ASSET_ROOT": str(asset_root)}
            )

        self.assertEqual(calls[0][0], "run")
        self.assertEqual(calls[0][1][-2:], ["stop", "nginx"])
        self.assertEqual(calls[1], ("drain", False))
        smoke_command = calls[2][1]
        self.assertIn("exec", smoke_command)
        self.assertIn("backend", smoke_command)
        self.assertIn("http://127.0.0.1:8000", smoke_command)
        self.assertIn("--expected-byteff2-commit", smoke_command)
        self.assertEqual(smoke_command[-1], SHA)
        self.assertEqual(calls[3], ("drain", True))

    def test_monomer_smoke_failure_reenables_drain_before_propagating(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            False,
        )
        drain_states: list[bool] = []
        with (
            mock.patch.object(controller, "run"),
            mock.patch.object(
                controller,
                "drain",
                side_effect=lambda _environment, enabled: drain_states.append(enabled),
            ),
            mock.patch.object(
                controller,
                "run_monomer_md_smoke",
                side_effect=release_controller.ReleaseError("worker smoke failed"),
            ),
        ):
            with self.assertRaisesRegex(release_controller.ReleaseError, "worker smoke failed"):
                controller.run_ingress_isolated_monomer_smoke({})

        self.assertEqual(drain_states, [False, True])

    def test_monomer_smoke_resume_response_loss_still_redrains_and_propagates(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            False,
        )
        drain_states: list[bool] = []

        def drain(_environment: dict[str, str], enabled: bool) -> None:
            drain_states.append(enabled)
            if not enabled:
                raise release_controller.ReleaseError("worker smoke resume response lost")

        with (
            mock.patch.object(controller, "run"),
            mock.patch.object(controller, "drain", side_effect=drain),
            mock.patch.object(controller, "run_monomer_md_smoke") as smoke,
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "worker smoke resume response lost",
            ),
        ):
            controller.run_ingress_isolated_monomer_smoke({})

        self.assertEqual(drain_states, [False, True])
        smoke.assert_not_called()

    def test_schema_compatibility_floor_is_recorded_and_preserved(self) -> None:
        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "without canonical records",
        ):
            release_controller.schema_compatibility_floor_after(
                None,
                ["0012_drop_polytao_jobs"],
            )
        self.assertEqual(
            release_controller.schema_compatibility_floor_after(
                None,
                ["0012_drop_polytao_jobs"],
                [
                    {
                        "version": "0012_drop_polytao_jobs",
                        "kind": "contract",
                        "epoch": 1,
                        "checksum": release_controller.POLYTAO_CONTRACT_CHECKSUM,
                        "requires_contracts": [],
                    }
                ],
            ),
            {
                "version": "0012_drop_polytao_jobs",
                "checksum": release_controller.POLYTAO_CONTRACT_CHECKSUM,
            },
        )

    def test_migration_history_is_a_fail_closed_ordered_union(self) -> None:
        previous = [
            "0009_monomer_md_job_leases",
            "0010_deployment_control",
            "0011_monomer_md_demo_steps",
        ]
        self.assertEqual(
            release_controller.merge_applied_migrations(
                previous,
                ["0012_drop_polytao_jobs"],
            ),
            [*previous, "0012_drop_polytao_jobs"],
        )
        self.assertEqual(
            release_controller.merge_applied_migrations(previous, []),
            previous,
        )
        with self.assertRaisesRegex(release_controller.ReleaseError, "invalid migration history"):
            release_controller.merge_applied_migrations("0011_monomer_md_demo_steps", [])
        with self.assertRaisesRegex(release_controller.ReleaseError, "duplicate migrations"):
            release_controller.merge_applied_migrations([previous[0], previous[0]], [])
        self.assertEqual(
            release_controller.schema_compatibility_floor_after(
                "0012_drop_polytao_jobs",
                [],
            ),
            "0012_drop_polytao_jobs",
        )

    def test_pre_contract_release_cannot_cross_active_schema_floor(self) -> None:
        manifest = release_controller.load_manifest(self.build())
        with self.assertRaisesRegex(release_controller.ReleaseError, "compatibility floor"):
            release_controller.assert_release_supports_schema_floor(
                manifest,
                "0012_drop_polytao_jobs",
            )

        manifest["migrations"].append(
            {"name": "0012_drop_polytao_jobs", "type": "contract"}
        )
        release_controller.assert_release_supports_schema_floor(
            manifest,
            "0012_drop_polytao_jobs",
        )

    def test_release_controller_rejects_multiple_backend_replicas(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            False,
        )
        multiple = subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout="backend-one\nbackend-two\n",
        )
        with mock.patch.object(release_controller.subprocess, "run", return_value=multiple):
            with self.assertRaisesRegex(release_controller.ReleaseError, "exactly one running backend"):
                controller.resolve_single_running_container(
                    controller.release_dir,
                    "backend",
                    {},
                )

    def test_runtime_postgres_binding_must_be_exactly_one_loopback_port(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            False,
        )
        environment = {"NEXPOLY_POSTGRES_PORT": "55432"}
        valid_ports = {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55432"}]}
        results = [
            subprocess.CompletedProcess([], 0, stdout="postgres-container\n"),
            subprocess.CompletedProcess([], 0, stdout=json.dumps(valid_ports)),
        ]
        with mock.patch.object(
            release_controller.subprocess,
            "run",
            side_effect=results,
        ) as run:
            controller.verify_postgres_loopback(controller.release_dir, environment)

        inspect_command = run.call_args_list[1].args[0]
        self.assertEqual(inspect_command[:3], ["docker", "inspect", "--format"])
        self.assertIn(".NetworkSettings.Ports", inspect_command[3])

        invalid_bindings = (
            {"5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": "55432"}]},
            {
                "5432/tcp": [
                    {"HostIp": "127.0.0.1", "HostPort": "55432"},
                    {"HostIp": "::1", "HostPort": "55432"},
                ]
            },
            {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5432"}]},
        )
        for ports in invalid_bindings:
            with (
                self.subTest(ports=ports),
                mock.patch.object(
                    release_controller.subprocess,
                    "run",
                    side_effect=[
                        subprocess.CompletedProcess([], 0, stdout="postgres-container\n"),
                        subprocess.CompletedProcess([], 0, stdout=json.dumps(ports)),
                    ],
                ),
                self.assertRaisesRegex(
                    release_controller.ReleaseError,
                    "127.0.0.1:55432",
                ),
            ):
                controller.verify_postgres_loopback(controller.release_dir, environment)

    def test_pending_contract_migrations_compares_current_release_manifest(self) -> None:
        current = {
            "migrations": ["0012_drop_polytao_jobs"],
            "migration_manifest": [
                {"name": "0012_drop_polytao_jobs", "type": "contract"},
            ]
        }
        candidate = {
            "migrations": [
                {"name": "0012_drop_polytao_jobs", "type": "contract"},
                {"name": "0013_future_contract", "type": "contract"},
                {"name": "0014_expand", "type": "expand"},
            ]
        }
        self.assertEqual(
            release_controller.pending_contract_migrations(current, candidate),
            ["0012_drop_polytao_jobs", "0013_future_contract"],
        )

    def test_bootstrap_state_keeps_deferred_contract_pending_until_approval(self) -> None:
        bootstrap_state = {
            "migrations": [
                "0009_monomer_md_job_leases",
                "0010_deployment_control",
                "0011_monomer_md_demo_steps",
            ],
            "migration_manifest": [
                {"name": "0009_monomer_md_job_leases", "type": "expand"},
                {"name": "0010_deployment_control", "type": "expand"},
                {"name": "0011_monomer_md_demo_steps", "type": "expand"},
                {"name": "0012_drop_polytao_jobs", "type": "contract"},
            ],
            "approved_contract_migrations": [],
        }
        candidate = {"migrations": bootstrap_state["migration_manifest"]}

        self.assertEqual(
            release_controller.pending_contract_migrations(bootstrap_state, candidate),
            ["0012_drop_polytao_jobs"],
        )

        bootstrap_state["approved_contract_migrations"] = ["0012_drop_polytao_jobs"]
        with self.assertRaisesRegex(release_controller.ReleaseError, "name-only"):
            release_controller.pending_contract_migrations(bootstrap_state, candidate)

    def test_pending_trailing_contract_is_deferred_by_code_deploy(self) -> None:
        current = {
            "source_sha": SHA,
            "approved_contract_migrations": [],
        }
        candidate = {
            "migrations": [
                {"name": "0012_drop_polytao_jobs", "type": "contract"},
            ]
        }

        self.assertEqual(
            release_controller.code_deploy_migration_mode(
                current,
                candidate,
                deployment_mode="auto",
                target_sha="b" * 40,
            ),
            "expand",
        )
        candidate["migrations"].append(
            {"name": "0013_expand_after_contract", "type": "expand"}
        )
        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "trailing migration suffix",
        ):
            release_controller.code_deploy_migration_mode(
                current,
                candidate,
                deployment_mode="auto",
                target_sha="b" * 40,
            )

    def test_single_bundle_worker_preparation_records_frozen_runtime_identities(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            False,
        )
        (controller.staging / "wheelhouse").mkdir(parents=True)
        lock = controller.staging / "workers" / "monomer_md_worker" / "requirements.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(
            "fixture-package==1.0 \\\n"
            "    --hash=sha256:" + "a" * 64 + "\n",
            encoding="utf-8",
        )
        base_identity = worker_base_identity()
        toolchain_identity = worker_toolchain_identity()
        environment = {
            "NEXPOLY_WORKER_BASE_PYTHON": str(base_identity["configured_path"]),
            "NEXPOLY_WORKER_BASE_PYTHON_IDENTITY_SHA256": str(
                base_identity["identity_sha256"]
            ),
            "NEXPOLY_WORKER_CONDA_EXE": str(toolchain_identity["conda_executable"]),
            "NEXPOLY_WORKER_GMX": str(toolchain_identity["gmx_executable"]),
        }
        with (
            mock.patch.object(controller, "run") as run,
            mock.patch.object(
                release_controller,
                "inspect_worker_base_python",
                return_value=base_identity,
            ) as inspect_base,
            mock.patch.object(
                release_controller,
                "inspect_worker_toolchain",
                return_value=toolchain_identity,
            ) as inspect_toolchain,
        ):
            controller.prepare_worker(environment)
        self.assertEqual(run.call_count, 3)
        install_command = run.call_args_list[1].args[0]
        self.assertEqual(install_command.count("-r"), 1)
        self.assertIn("--ignore-installed", install_command)
        self.assertIn("--only-binary=:all:", install_command)
        verify_command = run.call_args_list[2].args[0]
        self.assertIn(release_controller.WORKER_VENV_VERIFY_PROGRAM, verify_command)
        self.assertEqual(inspect_base.call_count, 2)
        self.assertEqual(inspect_toolchain.call_count, 2)
        self.assertEqual(controller.worker_base_python_identity, base_identity)
        self.assertEqual(controller.worker_toolchain_identity, toolchain_identity)
        recorded_base = json.loads(
            (controller.staging / "worker-base-python-identity.json").read_text(encoding="utf-8")
        )
        recorded_toolchain = json.loads(
            (controller.staging / "worker-toolchain-identity.json").read_text(encoding="utf-8")
        )
        self.assertEqual(recorded_base, base_identity)
        self.assertEqual(recorded_toolchain, toolchain_identity)

    def test_worker_lock_expectations_resolve_only_safe_exact_includes(self) -> None:
        bundle = self.root / "worker-locks"
        nested = bundle / "workers" / "monomer_md_worker" / "requirements.lock"
        nested.parent.mkdir(parents=True)
        nested.write_text(
            "--only-binary :all:\n"
            "Example_Pkg==1.2.3 \\\n"
            "    --hash=sha256:" + "a" * 64 + "\n",
            encoding="utf-8",
        )
        root = bundle / "requirements.lock"
        root.write_text("-r workers/monomer_md_worker/requirements.lock\n", encoding="utf-8")

        self.assertEqual(
            release_controller.worker_lock_requirements(root, bundle),
            [{"name": "example-pkg", "version": "1.2.3"}],
        )
        root.write_text("-r ../outside.lock\n", encoding="utf-8")
        with self.assertRaisesRegex(release_controller.ReleaseError, "escapes"):
            release_controller.worker_lock_requirements(root, bundle)

        root.write_text("--index-url https://packages.invalid/simple\n", encoding="utf-8")
        with self.assertRaisesRegex(release_controller.ReleaseError, "unsupported"):
            release_controller.worker_lock_requirements(root, bundle)

    def test_worker_base_python_accepts_resolved_symlink_and_enforces_pin(self) -> None:
        configured = self.root / "frozen" / "bin" / "python"
        configured.parent.mkdir(parents=True)
        target = configured.with_name("python3.11")
        shutil.copy2(Path(sys.executable).resolve(), target)
        os.chmod(target, 0o755)
        configured.symlink_to(target.name)

        identity = release_controller.inspect_worker_base_python(
            str(configured),
            None,
            os.environ.copy(),
        )
        self.assertEqual(identity["configured_path"], str(configured))
        self.assertEqual(identity["resolved_path"], str(target))
        self.assertEqual(
            release_controller.inspect_worker_base_python(
                str(configured),
                identity["identity_sha256"],
                os.environ.copy(),
            ),
            identity,
        )
        with self.assertRaisesRegex(release_controller.ReleaseError, "differs from deploy.env"):
            release_controller.inspect_worker_base_python(
                str(configured),
                "sha256:" + "f" * 64,
                os.environ.copy(),
            )

    def test_worker_base_python_rejects_relative_and_non_file_paths(self) -> None:
        with self.assertRaisesRegex(release_controller.ReleaseError, "absolute safe path"):
            release_controller.inspect_worker_base_python("bin/python", None)
        directory = self.root / "not-python"
        directory.mkdir()
        with self.assertRaisesRegex(release_controller.ReleaseError, "file or a file symlink"):
            release_controller.inspect_worker_base_python(str(directory), None)

    def test_worker_base_identity_record_is_tamper_evident(self) -> None:
        identity = worker_base_identity()
        release_controller.validate_worker_base_identity(identity)
        identity["python_version"] = "3.11.99 (tampered)"
        with self.assertRaisesRegex(release_controller.ReleaseError, "fingerprint"):
            release_controller.validate_worker_base_identity(identity)

    def test_worker_toolchain_identity_record_is_tamper_evident(self) -> None:
        identity = worker_toolchain_identity()
        release_controller.validate_worker_toolchain_identity(identity)
        identity["gmx_version_sha256"] = "sha256:" + "9" * 64
        with self.assertRaisesRegex(release_controller.ReleaseError, "fingerprint"):
            release_controller.validate_worker_toolchain_identity(identity)

    def test_worker_venv_verifier_does_not_accept_inherited_distribution(self) -> None:
        venv = self.root / "verification-venv"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "venv",
                "--without-pip",
                "--system-site-packages",
                str(venv),
            ],
            check=True,
        )
        purelib = Path(
            subprocess.run(
                [
                    str(venv / "bin" / "python"),
                    "-I",
                    "-c",
                    "import sysconfig; print(sysconfig.get_path('purelib'))",
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
        )
        metadata = purelib / "fixture_pkg-1.0.dist-info"
        metadata.mkdir(parents=True)
        (metadata / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: fixture-pkg\nVersion: 1.0\n",
            encoding="utf-8",
        )
        (metadata / "RECORD").write_text(
            "fixture_pkg-1.0.dist-info/METADATA,,\n"
            "fixture_pkg-1.0.dist-info/RECORD,,\n",
            encoding="utf-8",
        )
        expectation = self.root / "worker-expectation.json"
        expectation.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "requirements": [{"name": "fixture-pkg", "version": "1.0"}],
                }
            ),
            encoding="utf-8",
        )
        command = [
            str(venv / "bin" / "python"),
            "-I",
            "-c",
            release_controller.WORKER_VENV_VERIFY_PROGRAM,
            str(venv),
            str(expectation),
        ]
        subprocess.run(command, check=True)

        expectation.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "requirements": [
                        {"name": "pip", "version": importlib.metadata.version("pip")}
                    ],
                }
            ),
            encoding="utf-8",
        )
        inherited = subprocess.run(
            command,
            text=True,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(inherited.returncode, 0)
        self.assertIn("local versions: []", inherited.stderr)

    def test_bootstrap_job_check_uses_persistent_status_cli(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(self.root / "production", manifest, "auto", False)
        controller.bootstrap = True
        payload = {
            "drain": {"enabled": True},
            "active_jobs": {"monomer_md": 0, "online_knowledge": 0},
            "active_total": 0,
        }
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload))
        with mock.patch.object(release_controller.subprocess, "run", return_value=completed) as run:
            controller.wait_for_jobs({"APP_POSTGRES_DSN": "postgresql://fixture", "NEXPOLY_DRAIN_TIMEOUT_SECONDS": "1"})
        command = run.call_args.args[0]
        self.assertIn("app.deployment_control_cli", command)
        self.assertNotIn("/internal/deployment/status", command)
        self.assertNotIn("--dsn", command)
        self.assertNotIn("postgresql://fixture", command)

    def test_drain_never_exposes_postgres_dsn_in_argv_or_logs(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            False,
        )
        secret = "postgresql://nexpoly:do-not-log-me@lab-postgres:5432/nexpoly"
        environment = {"APP_POSTGRES_DSN": secret}
        output = io.StringIO()

        with (
            mock.patch.object(release_controller.subprocess, "run") as run,
            mock.patch("sys.stdout", output),
        ):
            controller.drain(environment, True)
            controller.drain(environment, False)

        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            command = call.args[0]
            self.assertNotIn("--dsn", command)
            self.assertNotIn(secret, command)
            self.assertIn("--actor", command)
            self.assertEqual(command[command.index("--actor") + 1], "release-controller")
            self.assertIn("--release-sha", command)
            self.assertEqual(command[command.index("--release-sha") + 1], SHA)
        self.assertIn("resume", run.call_args_list[1].args[0])
        self.assertNotIn(secret, output.getvalue())

    def test_lost_persistent_drain_response_attempts_owned_resume_and_keeps_marker_on_failure(
        self,
    ) -> None:
        controller = self.existing_release_controller()
        drain_calls: list[bool] = []

        def prepare(_environment: dict[str, str]) -> None:
            controller.staging.mkdir(parents=True)

        def drain(_environment: dict[str, str], enabled: bool) -> None:
            drain_calls.append(enabled)
            if enabled:
                raise release_controller.ReleaseError("drain response lost")
            raise release_controller.ReleaseError("conditional resume failed")

        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.multiple(
                    controller,
                    ensure_root=mock.DEFAULT,
                    validate_current_runtime=mock.DEFAULT,
                    verify_image_labels=mock.DEFAULT,
                    assert_still_current_main=mock.DEFAULT,
                    run=mock.DEFAULT,
                )
            )
            stack.enter_context(mock.patch.object(controller, "environment", return_value={}))
            stack.enter_context(
                mock.patch.object(controller, "prepare_staging", side_effect=prepare)
            )
            stack.enter_context(mock.patch.object(controller, "drain", side_effect=drain))
            stack.enter_context(
                self.assertRaisesRegex(
                    release_controller.ReleaseError,
                    "drain resume failed; ingress remains isolated",
                )
            )
            controller.deploy()

        self.assertEqual(drain_calls, [True, False])
        marker = json.loads(controller.in_progress_path.read_text(encoding="utf-8"))
        self.assertTrue(marker["drain_attempted"])
        self.assertFalse(marker["drain_enabled"])
        self.assertEqual(marker["drain_resume"], "failed")
        self.assertIn("conditional resume failed", marker["drain_resume_error"])
        self.assertTrue(controller.staging.is_dir())

    def test_lost_worker_drain_response_forces_resume_before_global_admission(
        self,
    ) -> None:
        controller = self.existing_release_controller()
        drain_calls: list[bool] = []

        def prepare(_environment: dict[str, str]) -> None:
            controller.staging.mkdir(parents=True)

        def drain(_environment: dict[str, str], enabled: bool) -> None:
            drain_calls.append(enabled)

        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.multiple(
                    controller,
                    ensure_root=mock.DEFAULT,
                    validate_current_runtime=mock.DEFAULT,
                    verify_image_labels=mock.DEFAULT,
                    assert_still_current_main=mock.DEFAULT,
                    run=mock.DEFAULT,
                )
            )
            stack.enter_context(mock.patch.object(controller, "environment", return_value={}))
            stack.enter_context(
                mock.patch.object(controller, "prepare_staging", side_effect=prepare)
            )
            stack.enter_context(mock.patch.object(controller, "drain", side_effect=drain))
            stack.enter_context(
                mock.patch.object(
                    controller,
                    "drain_worker",
                    side_effect=release_controller.ReleaseError("worker drain response lost"),
                )
            )
            resume_worker = stack.enter_context(
                mock.patch.object(
                    controller,
                    "resume_worker",
                    side_effect=release_controller.ReleaseError("worker resume failed"),
                )
            )
            stack.enter_context(
                self.assertRaisesRegex(
                    release_controller.ReleaseError,
                    "worker drain response lost",
                )
            )
            controller.deploy()

        resume_worker.assert_called_once_with({})
        self.assertEqual(drain_calls, [True])
        marker = json.loads(controller.in_progress_path.read_text(encoding="utf-8"))
        self.assertTrue(marker["drain_attempted"])
        self.assertTrue(marker["drain_enabled"])
        self.assertTrue(marker["worker_drain_attempted"])
        self.assertEqual(marker["worker_restart"], "manual-intervention-required")
        self.assertIn("worker resume failed", marker["worker_restart_error"])
        self.assertTrue(controller.staging.is_dir())

    def test_active_job_timeout_recovers_worker_then_global_drain_and_records_deferred(
        self,
    ) -> None:
        controller = self.existing_release_controller()
        drain_commands: list[list[str]] = []
        worker_calls: list[tuple[str, str]] = []
        state_snapshots: list[dict[str, object]] = []
        original_write_attempt = controller.write_attempt
        previous_release = controller.ops / "releases" / ("1" * 40)

        def prepare(_environment: dict[str, str]) -> None:
            controller.staging.mkdir(parents=True)

        def run(command: list[str], **_kwargs: object) -> None:
            if "app.deployment_control_cli" not in command:
                return
            drain_commands.append(command)
            operation = command[command.index("app.deployment_control_cli") + 1]
            if (
                operation == "resume"
                and str(controller.staging / "docker-compose.yml") in command
            ):
                raise release_controller.ReleaseError(
                    "failed target release cannot execute drain resume"
                )

        def worker_request(
            _environment: dict[str, str],
            method: str,
            path: str,
        ) -> dict[str, object]:
            worker_calls.append((method, path))
            responses = {
                ("GET", "/health"): {
                    "status": "ok",
                    "active_jobs": 1,
                    "worker_instance_id": "instance-a",
                },
                ("POST", "/drain"): {
                    "status": "draining",
                    "active_jobs": 1,
                    "worker_instance_id": "instance-a",
                },
                ("POST", "/resume"): {
                    "status": "ready",
                    "accepting_jobs": False,
                    "active_jobs": 1,
                    "worker_instance_id": "instance-a",
                },
            }
            return responses[(method, path)]

        def capture_attempt(state: dict[str, object]) -> None:
            state_snapshots.append(json.loads(json.dumps(state)))
            original_write_attempt(state)

        jobs = {category: 0 for category in release_controller.ACTIVE_JOB_CATEGORIES}
        jobs["monomer_md"] = 1
        active_status = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps({"active_jobs": jobs, "active_total": 1}),
        )

        with ExitStack() as stack:
            patched = stack.enter_context(
                mock.patch.multiple(
                    controller,
                    ensure_root=mock.DEFAULT,
                    validate_current_runtime=mock.DEFAULT,
                    verify_image_labels=mock.DEFAULT,
                    assert_still_current_main=mock.DEFAULT,
                    run=mock.DEFAULT,
                )
            )
            patched["run"].side_effect = run
            stack.enter_context(
                mock.patch.object(
                    controller,
                    "environment",
                    return_value={"NEXPOLY_DRAIN_TIMEOUT_SECONDS": "1"},
                )
            )
            stack.enter_context(
                mock.patch.object(controller, "prepare_staging", side_effect=prepare)
            )
            stack.enter_context(
                mock.patch.object(controller, "worker_request", side_effect=worker_request)
            )
            backup = stack.enter_context(mock.patch.object(controller, "backup_database"))
            stack.enter_context(
                mock.patch.object(controller, "write_attempt", side_effect=capture_attempt)
            )
            stack.enter_context(
                mock.patch.object(release_controller.subprocess, "run", return_value=active_status)
            )
            stack.enter_context(
                mock.patch.object(
                    release_controller.time,
                    "monotonic",
                    side_effect=[0.0, 2.0],
                )
            )
            with self.assertRaises(release_controller.DeploymentDeferred) as caught:
                controller.deploy()

        self.assertEqual(release_controller.failure_status(caught.exception), "deferred")
        self.assertEqual(
            worker_calls,
            [("GET", "/health"), ("POST", "/drain"), ("POST", "/resume")],
        )
        self.assertEqual(len(drain_commands), 2)
        self.assertIn("drain", drain_commands[0])
        self.assertIn(str(controller.staging / "docker-compose.yml"), drain_commands[0])
        self.assertIn("resume", drain_commands[1])
        self.assertIn(str(previous_release / "docker-compose.yml"), drain_commands[1])
        self.assertNotIn(str(controller.staging / "docker-compose.yml"), drain_commands[1])
        self.assertEqual(state_snapshots[-1]["status"], "deferred")
        self.assertEqual(state_snapshots[-1]["worker_drain"], "resumed-after-failure")
        self.assertEqual(state_snapshots[-1]["drain_resume"], "success")
        self.assertEqual(
            state_snapshots[-1]["drain_resume_release"],
            str(previous_release),
        )
        self.assertFalse(controller.in_progress_path.exists())
        backup.assert_not_called()

    def test_automatic_freshness_is_rechecked_inside_controller(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            False,
        )
        current = subprocess.CompletedProcess(
            [],
            0,
            stdout=f"{SHA}\trefs/heads/main\n",
        )
        with mock.patch.object(release_controller.subprocess, "run", return_value=current) as run:
            controller.assert_still_current_main({})
        self.assertIn("ls-remote", run.call_args.args[0])

        newer_sha = "f" * 40
        superseded = subprocess.CompletedProcess(
            [],
            0,
            stdout=f"{newer_sha}\trefs/heads/main\n",
        )
        with (
            mock.patch.object(release_controller.subprocess, "run", return_value=superseded),
            self.assertRaises(release_controller.DeploymentSuperseded) as caught,
        ):
            controller.assert_still_current_main({})
        self.assertEqual(release_controller.failure_status(caught.exception), "superseded")

    def test_successful_deploy_rechecks_freshness_after_smokes_and_final_health(self) -> None:
        manifest = self.build()
        production = self.root / "production"
        controller = release_controller.ReleaseController(production, manifest, "auto", True)
        previous = {
            "status": "success",
            "source_sha": "1" * 40,
            "asset_manifest_digest": DIGEST,
            "byteff2_commit": SHA,
            "migrations": [],
            "approved_contract_migrations": [],
        }
        controller.state_path.parent.mkdir(parents=True)
        controller.state_path.write_text(json.dumps(previous), encoding="utf-8")
        asset_root = self.root / "assets"
        controller.document.update(
            {
                "current_asset_manifest_digest": DIGEST,
                "current_asset_root": str(asset_root),
                "current_byteff2_commit": SHA,
                "resolved_asset_manifest_digest": DIGEST,
                "resolved_asset_root": str(asset_root),
                "resolved_byteff2_commit": SHA,
            }
        )
        backup = production / "backups" / "freshness.dump"
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"freshness backup")
        events: list[str] = []
        run_commands: list[list[str]] = []

        def prepare(_environment: dict[str, str]) -> None:
            controller.staging.mkdir(parents=True)

        def create_backup(_environment: dict[str, str], _from_sha: str) -> None:
            controller.backup_path = backup

        def record_run(command: list[str], **_kwargs: object) -> None:
            run_commands.append(command)
            if "up" in command and command[-1] == "nginx":
                events.append("nginx")

        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.multiple(
                    controller,
                    ensure_root=mock.DEFAULT,
                    validate_current_runtime=mock.DEFAULT,
                    verify_image_labels=mock.DEFAULT,
                    wait_for_jobs=mock.DEFAULT,
                    switch_current=mock.DEFAULT,
                    restart_or_defer_worker=mock.DEFAULT,
                    backend_healthcheck=mock.DEFAULT,
                )
            )
            stack.enter_context(mock.patch.object(controller, "environment", return_value={}))
            stack.enter_context(
                mock.patch.object(controller, "prepare_staging", side_effect=prepare)
            )
            freshness = stack.enter_context(
                mock.patch.object(
                    controller,
                    "assert_still_current_main",
                    side_effect=lambda _environment: events.append("freshness"),
                )
            )
            stack.enter_context(mock.patch.object(controller, "run", side_effect=record_run))
            stack.enter_context(mock.patch.object(controller, "drain"))
            stack.enter_context(
                mock.patch.object(
                    controller,
                    "drain_worker",
                    return_value={"supported": True, "active_jobs": 0},
                )
            )
            stack.enter_context(
                mock.patch.object(controller, "backup_database", side_effect=create_backup)
            )
            stack.enter_context(mock.patch.object(controller, "run_migrations", return_value=[]))
            stack.enter_context(
                mock.patch.object(controller, "candidate_asset_environment", return_value={})
            )
            stack.enter_context(
                mock.patch.object(controller, "refresh_analytics_snapshot")
            )
            stack.enter_context(
                mock.patch.object(
                    controller,
                    "run_ingress_isolated_contract_smoke",
                    side_effect=lambda _environment: events.append("contract"),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    controller,
                    "run_ingress_isolated_monomer_smoke",
                    side_effect=lambda _environment: events.append("monomer"),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    controller,
                    "run_isolated_web_smoke",
                    side_effect=lambda _environment: events.append("web"),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    controller,
                    "healthcheck",
                    side_effect=lambda _environment: events.append("health"),
                )
            )
            result = controller.deploy()

        self.assertEqual(result["status"], "success")
        self.assertEqual(freshness.call_count, 4)
        pull_command = next(command for command in run_commands if "pull" in command)
        backend_up = next(
            command
            for command in run_commands
            if "up" in command and command[-1] == "backend"
        )
        self.assertIn("lab-postgres", pull_command)
        self.assertIn("lab-postgres", backend_up)
        self.assertEqual(
            events[-7:],
            ["contract", "monomer", "web", "freshness", "nginx", "health", "freshness"],
        )

    def test_analytics_snapshot_refresh_uses_candidate_image_environment(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            False,
        )
        with mock.patch.object(controller, "run") as run:
            controller.refresh_analytics_snapshot({"APP_POSTGRES_DSN": "postgresql://secret"})
        command = run.call_args.args[0]
        self.assertIn("app.generate_database_analytics_snapshot", command)
        self.assertIn(SHA, command)
        self.assertNotIn("postgresql://secret", command)

    def test_bootstrap_quiesce_requires_complete_structured_zero_work_evidence(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            False,
        )
        jobs = {category: 0 for category in release_controller.ACTIVE_JOB_CATEGORIES}
        valid = {
            "active_jobs_schema_version": 1,
            "ingress_isolated": True,
            "active_jobs": jobs,
            "active_total": 0,
        }
        quiesce_hook = self.make_bootstrap_hook("bootstrap-quiesce")
        environment = {"NEXPOLY_BOOTSTRAP_QUIESCE_COMMAND": str(quiesce_hook)}

        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(valid))
        with mock.patch.object(release_controller.subprocess, "run", return_value=completed):
            self.assertEqual(controller.run_bootstrap_quiesce(environment), valid)

        unversioned = {
            key: value
            for key, value in valid.items()
            if key != "active_jobs_schema_version"
        }
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(unversioned))
        with mock.patch.object(release_controller.subprocess, "run", return_value=completed):
            self.assertEqual(controller.run_bootstrap_quiesce(environment), valid)

        invalid_payloads: list[tuple[str, object]] = []
        missing = json.loads(json.dumps(valid))
        missing["active_jobs"].pop("gpu_waiting")
        invalid_payloads.append(("cover", missing))
        boolean_count = json.loads(json.dumps(valid))
        boolean_count["active_jobs"]["polytao"] = False
        invalid_payloads.append(("count", boolean_count))
        active = json.loads(json.dumps(valid))
        active["active_jobs"]["polytao"] = 1
        active["active_total"] = 1
        invalid_payloads.append(("active work", active))
        not_isolated = json.loads(json.dumps(valid))
        not_isolated["ingress_isolated"] = False
        invalid_payloads.append(("isolated ingress", not_isolated))
        mismatched_total = json.loads(json.dumps(valid))
        mismatched_total["active_total"] = 1
        invalid_payloads.append(("does not match", mismatched_total))
        old_field = json.loads(json.dumps(valid))
        old_field["schema_version"] = old_field.pop("active_jobs_schema_version")
        invalid_payloads.append(("invalid shape", old_field))
        dual_fields = json.loads(json.dumps(valid))
        dual_fields["schema_version"] = 1
        invalid_payloads.append(("invalid shape", dual_fields))

        for message, payload in invalid_payloads:
            with (
                self.subTest(message=message),
                mock.patch.object(
                    release_controller.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(payload)),
                ),
                self.assertRaisesRegex(release_controller.ReleaseError, message),
            ):
                controller.run_bootstrap_quiesce(environment)
        with (
            mock.patch.object(
                release_controller.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout="not-json\n"),
            ),
            self.assertRaisesRegex(release_controller.ReleaseError, "exactly one JSON object"),
        ):
            controller.run_bootstrap_quiesce(environment)

    def test_bootstrap_hooks_require_one_owned_mode_0700_regular_executable(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "bootstrap",
            False,
        )
        hook = self.make_bootstrap_hook("audited-hook")
        key = "NEXPOLY_BOOTSTRAP_QUIESCE_COMMAND"
        self.assertEqual(controller.bootstrap_hook_command({key: str(hook)}, key), [str(hook)])

        os.chmod(hook, 0o755)
        with self.assertRaisesRegex(release_controller.ReleaseError, "mode-0700"):
            controller.bootstrap_hook_command({key: str(hook)}, key)
        os.chmod(hook, 0o700)
        with self.assertRaisesRegex(release_controller.ReleaseError, "exactly one"):
            controller.bootstrap_hook_command({key: f"{hook} --unsafe-argument"}, key)

        link = self.root / "hook-link"
        link.symlink_to(hook)
        with self.assertRaisesRegex(release_controller.ReleaseError, "regular file"):
            controller.bootstrap_hook_command({key: str(link)}, key)

    def test_bootstrap_rollback_requires_complete_matching_legacy_runtime_evidence(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "bootstrap",
            False,
        )
        rollback_hook = self.make_bootstrap_hook("bootstrap-rollback")
        identity_material = {
            "backend_image_id": "sha256:" + "4" * 64,
            "web_image_id": "sha256:" + "5" * 64,
            "worker_unit_sha256": "sha256:" + "6" * 64,
        }
        legacy_digest = release_controller.canonical_json_digest(identity_material)
        environment = {
            "NEXPOLY_BOOTSTRAP_ROLLBACK_COMMAND": str(rollback_hook),
            "NEXPOLY_BOOTSTRAP_LEGACY_RUNTIME_SHA256": legacy_digest,
        }
        valid = {
            "schema_version": 1,
            "legacy_runtime_restored": True,
            **identity_material,
            "backend_healthy": True,
            "web_healthy": True,
            "worker_healthy": True,
            "ingress_restored": True,
        }
        with mock.patch.object(
            release_controller.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(valid)),
        ):
            self.assertEqual(controller.run_bootstrap_rollback(environment), valid)

        invalid = dict(valid)
        invalid["worker_healthy"] = False
        with (
            mock.patch.object(
                release_controller.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(invalid)),
            ),
            self.assertRaisesRegex(release_controller.ReleaseError, "worker_healthy"),
        ):
            controller.run_bootstrap_rollback(environment)

        wrong_identity = {**valid, "worker_unit_sha256": "sha256:" + "9" * 64}
        with (
            mock.patch.object(
                release_controller.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(wrong_identity)),
            ),
            self.assertRaisesRegex(release_controller.ReleaseError, "different legacy runtime"),
        ):
            controller.run_bootstrap_rollback(environment)

    def test_invalid_bootstrap_quiesce_runs_rollback_before_any_backup(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "bootstrap",
            True,
        )
        environment = {
            "NEXPOLY_BOOTSTRAP_RELEASE_SHA": SHA,
            "NEXPOLY_BOOTSTRAP_QUIESCE_COMMAND": "/fixture/quiesce",
            "NEXPOLY_BOOTSTRAP_ROLLBACK_COMMAND": "/fixture/rollback",
            "NEXPOLY_BOOTSTRAP_LEGACY_RUNTIME_SHA256": "sha256:" + "8" * 64,
        }
        asset_root = self.root / "baseline-assets"
        controller.document.update(
            {
                "current_asset_manifest_digest": DIGEST,
                "current_asset_root": str(asset_root),
                "current_byteff2_commit": SHA,
                "resolved_asset_manifest_digest": DIGEST,
                "resolved_asset_root": str(asset_root),
                "resolved_byteff2_commit": SHA,
            }
        )
        bootstrap_events: list[str] = []

        def fail_quiesce(_environment: dict[str, str]) -> None:
            bootstrap_events.append("quiesce")
            raise release_controller.ReleaseError("invalid evidence")

        with (
            mock.patch.object(controller, "ensure_root"),
            mock.patch.object(controller, "environment", return_value=environment),
            mock.patch.object(controller, "prepare_staging"),
            mock.patch.object(controller, "verify_image_labels"),
            mock.patch.object(
                controller,
                "verify_postgres_loopback",
                side_effect=lambda _release, _environment: bootstrap_events.append(
                    "postgres-boundary"
                ),
            ) as postgres_boundary,
            mock.patch.object(controller, "assert_still_current_main"),
            mock.patch.object(controller, "run"),
            mock.patch.object(
                controller,
                "run_bootstrap_quiesce",
                side_effect=fail_quiesce,
            ),
            mock.patch.object(controller, "backup_database") as backup,
            mock.patch.object(controller, "run_migrations") as migrations,
            mock.patch.object(
                controller,
                "run_bootstrap_rollback",
                return_value={"schema_version": 1},
            ) as rollback,
            mock.patch.object(controller, "clear_failed_bootstrap_release"),
            self.assertRaisesRegex(release_controller.ReleaseError, "invalid evidence"),
        ):
            controller.deploy()

        backup.assert_not_called()
        migrations.assert_not_called()
        postgres_boundary.assert_called_once_with(controller.candidate_dir, environment)
        self.assertEqual(bootstrap_events, ["postgres-boundary", "quiesce"])
        rollback.assert_called_once_with(environment)

    def test_runtime_job_check_requires_write_and_gpu_waiting_categories(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(self.root / "production", manifest, "auto", False)
        complete_jobs = {
            "monomer_md": 0,
            "polytao": 0,
            "online_knowledge": 0,
            "conditional_generation": 0,
            "reverse_design": 0,
            "gpu_inference": 0,
            "gpu_waiting": 0,
            "inflight_api_writes": 0,
        }
        complete = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps({"active_jobs": complete_jobs, "active_total": 0}),
        )
        with mock.patch.object(release_controller.subprocess, "run", return_value=complete):
            controller.wait_for_jobs({"NEXPOLY_DRAIN_TIMEOUT_SECONDS": "1"})

        for required in ("gpu_waiting", "inflight_api_writes"):
            incomplete_jobs = complete_jobs.copy()
            incomplete_jobs.pop(required)
            incomplete = subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps({"active_jobs": incomplete_jobs, "active_total": 0}),
            )
            with (
                self.subTest(required=required),
                mock.patch.object(release_controller.subprocess, "run", return_value=incomplete),
                self.assertRaisesRegex(release_controller.ReleaseError, "status is incomplete"),
            ):
                controller.wait_for_jobs({"NEXPOLY_DRAIN_TIMEOUT_SECONDS": "1"})

    def test_active_job_timeout_is_classified_as_deferred(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(self.root / "production", manifest, "auto", False)
        controller.root.mkdir()
        jobs = {category: 0 for category in release_controller.ACTIVE_JOB_CATEGORIES}
        jobs["polytao"] = 1
        completed = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps({"active_jobs": jobs, "active_total": 1}),
        )
        environment = {
            "NEXPOLY_DRAIN_TIMEOUT_SECONDS": "1",
        }
        with (
            mock.patch.object(release_controller.subprocess, "run", return_value=completed),
            mock.patch.object(release_controller.time, "monotonic", side_effect=[0.0, 2.0]),
        ):
            with self.assertRaises(release_controller.DeploymentDeferred) as caught:
                controller.wait_for_jobs(environment)
        self.assertEqual(release_controller.failure_status(caught.exception), "deferred")

    def test_drain_resume_requires_successful_runtime_rollback(self) -> None:
        self.assertTrue(release_controller.rollback_allows_resume(False, None))
        self.assertTrue(release_controller.rollback_allows_resume(True, "success"))
        self.assertFalse(release_controller.rollback_allows_resume(True, "failed"))
        self.assertFalse(release_controller.rollback_allows_resume(True, None))

    def test_current_runtime_preflight_blocks_on_missing_web_static_asset(self) -> None:
        target_manifest = self.build()
        target_payload = target_manifest.read_bytes()
        previous_sha = "b" * 40
        previous_manifest_path = self.build(sha=previous_sha)
        previous_document = release_controller.load_manifest(previous_manifest_path)
        previous_payload = previous_manifest_path.read_bytes()
        target_manifest.write_bytes(target_payload)

        production = self.root / "production"
        previous_release = production / "ops" / "releases" / previous_sha
        previous_release.mkdir(parents=True)
        (previous_release / "release-manifest.json").write_bytes(previous_payload)
        current = production / "ops" / "current"
        current.symlink_to(Path("releases") / previous_sha)
        controller = release_controller.ReleaseController(
            production,
            target_manifest,
            "auto",
            True,
        )
        controller.previous_state = {
            "source_sha": previous_sha,
            "backend_image": previous_document["images"]["backend"],
            "web_image": previous_document["images"]["web"],
            "asset_manifest_digest": DIGEST,
            "byteff2_commit": SHA,
        }
        controller.document.update(
            {
                "current_asset_manifest_digest": DIGEST,
                "current_byteff2_commit": SHA,
            }
        )

        with (
            mock.patch.object(
                controller,
                "public_web_static_smoke",
                side_effect=release_controller.ReleaseError(
                    "versioned static asset smoke failed"
                ),
            ) as web_static,
            mock.patch.object(controller, "run") as run,
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "versioned static asset smoke failed",
            ),
        ):
            controller.validate_current_runtime({})

        web_static.assert_called_once_with({})
        run.assert_not_called()

    def test_runtime_rollback_waits_for_old_backend_worker_web_and_smoke(self) -> None:
        target_manifest = self.build()
        target_payload = target_manifest.read_bytes()
        previous_sha = "b" * 40
        previous_manifest = self.build(sha=previous_sha)
        production = self.root / "production"
        previous_release = production / "ops" / "releases" / previous_sha
        previous_release.mkdir(parents=True)
        shutil.copy2(previous_manifest, previous_release / "release-manifest.json")
        target_manifest.write_bytes(target_payload)

        controller = release_controller.ReleaseController(
            production,
            target_manifest,
            "auto",
            True,
        )
        controller.previous_state = {"source_sha": previous_sha}
        smoke_events: list[str] = []
        with (
            mock.patch.object(controller, "switch_current") as switch_current,
            mock.patch.object(controller, "run") as run,
            mock.patch.object(
                controller,
                "worker_request",
                return_value={"worker_instance_id": "failed-target-worker"},
            ),
            mock.patch.object(controller, "wait_for_worker_health") as worker_health,
            mock.patch.object(
                controller,
                "backend_healthcheck",
                side_effect=lambda *_args, **_kwargs: smoke_events.append("backend"),
            ) as backend_health,
            mock.patch.object(
                controller,
                "run_ingress_isolated_contract_smoke",
                side_effect=lambda *_args, **_kwargs: smoke_events.append("contract"),
            ) as contract_smoke,
            mock.patch.object(
                controller,
                "validate_current_runtime",
                side_effect=lambda *_args, **_kwargs: smoke_events.append("web"),
            ) as runtime_health,
            mock.patch.object(
                controller,
                "run_ingress_isolated_monomer_smoke",
                side_effect=lambda *_args, **_kwargs: smoke_events.append("worker"),
            ) as worker_smoke,
        ):
            controller.rollback_runtime({})

        switch_current.assert_called_once_with(previous_release)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][-2:], ["nginx", "backend"])
        self.assertIn("--wait-timeout", commands[1])
        self.assertIn("lab-postgres", commands[1])
        self.assertEqual(commands[1][-1], "backend")
        self.assertIn("app.generate_database_analytics_snapshot", commands[2])
        self.assertIn(previous_sha, commands[2])
        self.assertEqual(
            commands[3],
            ["systemctl", "--user", "restart", "nexpoly-monomer-md-worker.service"],
        )
        self.assertIn("--wait-timeout", commands[4])
        self.assertEqual(commands[4][-1], "nginx")
        worker_health.assert_called_once_with(
            mock.ANY,
            expected_release=previous_release,
            previous_instance_id="failed-target-worker",
        )
        backend_health.assert_called_once_with(mock.ANY, release=previous_release)
        contract_smoke.assert_called_once_with(mock.ANY, release=previous_release)
        runtime_health.assert_called_once()
        worker_smoke.assert_called_once_with(mock.ANY, release=previous_release)
        self.assertEqual(smoke_events, ["backend", "contract", "worker", "web"])
        self.assertEqual(controller.candidate_dir, previous_release)

    def test_runtime_rollback_fails_when_previous_web_static_asset_is_missing(self) -> None:
        target_manifest = self.build()
        target_payload = target_manifest.read_bytes()
        previous_sha = "b" * 40
        previous_manifest_path = self.build(sha=previous_sha)
        previous_document = release_controller.load_manifest(previous_manifest_path)
        previous_payload = previous_manifest_path.read_bytes()
        target_manifest.write_bytes(target_payload)

        production = self.root / "production"
        previous_release = production / "ops" / "releases" / previous_sha
        previous_release.mkdir(parents=True)
        (previous_release / "release-manifest.json").write_bytes(previous_payload)
        controller = release_controller.ReleaseController(
            production,
            target_manifest,
            "auto",
            True,
        )
        controller.previous_state = {
            "source_sha": previous_sha,
            "backend_image": previous_document["images"]["backend"],
            "web_image": previous_document["images"]["web"],
            "asset_manifest_digest": DIGEST,
            "byteff2_commit": SHA,
        }
        controller.document.update(
            {
                "current_asset_manifest_digest": DIGEST,
                "current_byteff2_commit": SHA,
            }
        )

        with (
            mock.patch.object(controller, "run") as run,
            mock.patch.object(controller, "refresh_analytics_snapshot"),
            mock.patch.object(controller, "backend_healthcheck"),
            mock.patch.object(controller, "run_ingress_isolated_contract_smoke"),
            mock.patch.object(
                controller,
                "worker_request",
                return_value={"worker_instance_id": "failed-target-worker"},
            ),
            mock.patch.object(controller, "wait_for_worker_health"),
            mock.patch.object(controller, "run_ingress_isolated_monomer_smoke"),
            mock.patch.object(
                controller,
                "public_web_static_smoke",
                side_effect=release_controller.ReleaseError(
                    "previous Web versioned static asset is missing"
                ),
            ) as web_static,
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "previous Web versioned static asset is missing",
            ),
        ):
            controller.rollback_runtime({})

        nginx_up = [
            call.args[0]
            for call in run.call_args_list
            if "up" in call.args[0] and call.args[0][-1] == "nginx"
        ]
        self.assertEqual(len(nginx_up), 1)
        self.assertIn(str(previous_release / "docker-compose.yml"), nginx_up[0])
        web_static.assert_called_once()

    def test_failed_previous_web_rollback_keeps_global_drain_enabled(self) -> None:
        controller = self.existing_release_controller()
        backup = controller.root / "backups" / "rollback-web.dump"
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"verified backup")
        drain_calls: list[bool] = []

        def prepare(_environment: dict[str, str]) -> None:
            controller.staging.mkdir(parents=True)

        def create_backup(_environment: dict[str, str], _from_sha: str) -> None:
            controller.backup_path = backup

        with ExitStack() as stack:
            patched = stack.enter_context(
                mock.patch.multiple(
                    controller,
                    ensure_root=mock.DEFAULT,
                    validate_current_runtime=mock.DEFAULT,
                    verify_image_labels=mock.DEFAULT,
                    assert_still_current_main=mock.DEFAULT,
                    run=mock.DEFAULT,
                    wait_for_jobs=mock.DEFAULT,
                    switch_current=mock.DEFAULT,
                    restart_or_defer_worker=mock.DEFAULT,
                    backend_healthcheck=mock.DEFAULT,
                    run_ingress_isolated_contract_smoke=mock.DEFAULT,
                    run_ingress_isolated_monomer_smoke=mock.DEFAULT,
                    run_isolated_web_smoke=mock.DEFAULT,
                    refresh_analytics_snapshot=mock.DEFAULT,
                )
            )
            stack.enter_context(mock.patch.object(controller, "environment", return_value={}))
            stack.enter_context(
                mock.patch.object(controller, "prepare_staging", side_effect=prepare)
            )
            stack.enter_context(
                mock.patch.object(
                    controller,
                    "drain",
                    side_effect=lambda _environment, enabled: drain_calls.append(enabled),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    controller,
                    "drain_worker",
                    return_value={"supported": True, "active_jobs": 0},
                )
            )
            stack.enter_context(
                mock.patch.object(controller, "backup_database", side_effect=create_backup)
            )
            stack.enter_context(mock.patch.object(controller, "run_migrations", return_value=[]))
            stack.enter_context(
                mock.patch.object(controller, "candidate_asset_environment", return_value={})
            )
            stack.enter_context(
                mock.patch.object(
                    controller,
                    "healthcheck",
                    side_effect=release_controller.ReleaseError("candidate health failed"),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    controller,
                    "rollback_runtime",
                    side_effect=release_controller.ReleaseError(
                        "previous Web versioned static asset is missing"
                    ),
                )
            )
            recover_worker = stack.enter_context(
                mock.patch.object(controller, "recover_drained_worker")
            )
            with self.assertRaisesRegex(
                release_controller.ReleaseError,
                "candidate health failed",
            ):
                controller.deploy()

        self.assertEqual(drain_calls, [True])
        recover_worker.assert_not_called()
        marker = json.loads(controller.in_progress_path.read_text(encoding="utf-8"))
        self.assertEqual(marker["rollback"], "failed")
        self.assertIn(
            "previous Web versioned static asset is missing",
            marker["rollback_error"],
        )


    def test_asset_change_rebuild_uses_one_explicit_full_dataset_command(self) -> None:
        manifest = self.build_single_bundle()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            False,
        )
        with mock.patch.object(controller, "run") as run:
            controller.rebuild_datasets({})

        command = run.call_args.args[0]
        self.assertIn("--rebuild", command)
        self.assertIn("--skip-migrations", command)
        self.assertNotIn("all", command)
        declared = controller.document["datasets_on_asset_change"]
        selected = [command[index + 1] for index, value in enumerate(command) if value == "--dataset"]
        self.assertEqual(selected, declared)

    def test_previous_release_gpu_preflight_uses_previous_compose_tree(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            False,
        )
        previous = self.root / "previous-release"
        previous.mkdir()
        shutil.copy2(manifest, previous / "release-manifest.json")
        with mock.patch.object(controller, "run") as run:
            controller.backend_healthcheck({}, release=previous)

        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertIn(str(previous / "docker-compose.yml"), call.args[0])
            self.assertNotIn(str(controller.release_dir / "docker-compose.yml"), call.args[0])

    def test_cli_matches_workflow_contract(self) -> None:
        output = self.root / "cli.json"
        result = subprocess.run(
            [
                sys.executable, str(CONTROLLER_PATH), "build-manifest",
                "--sha", SHA, "--ci-run-id", "101",
                "--backend-image", BACKEND_IMAGE, "--web-image", WEB_IMAGE,
                "--release-bundle", str(self.release_bundle),
                "--release-input", str(self.release_input),
                "--output", str(output),
            ],
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        verify = subprocess.run(
            [sys.executable, str(CONTROLLER_PATH), "verify-manifest", "--manifest", str(output), "--sha", SHA],
            text=True, capture_output=True,
        )
        self.assertEqual(verify.returncode, 0, verify.stderr)

    def test_worker_drain_reports_active_job_without_stopping_it(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(self.root, manifest, "auto", False)
        with mock.patch.object(
            controller,
            "worker_request",
            side_effect=[
                {"status": "ok", "active_jobs": 1, "worker_instance_id": "instance-a"},
                {"status": "draining", "active_jobs": 1, "worker_instance_id": "instance-a"},
            ],
        ):
            result = controller.drain_worker({})

        self.assertEqual(result["active_jobs"], 1)
        self.assertTrue(result["supported"])
        self.assertEqual(result["worker_instance_id"], "instance-a")

    def test_worker_drain_response_loss_is_never_downgraded_to_unsupported(self) -> None:
        controller = release_controller.ReleaseController(
            self.root,
            self.build(),
            "auto",
            False,
        )
        with (
            mock.patch.object(
                controller,
                "worker_request",
                side_effect=[
                    {"status": "ok", "active_jobs": 0, "worker_instance_id": "instance-a"},
                    release_controller.ReleaseError("POST /drain response lost"),
                ],
            ),
            self.assertRaisesRegex(release_controller.ReleaseError, "response lost"),
        ):
            controller.drain_worker({})

        controller.worker_drain_info = {"supported": False, "active_jobs": 0}
        with mock.patch.object(controller, "resume_worker") as resume_worker:
            self.assertEqual(
                controller.recover_drained_worker({}),
                "resumed-after-failure",
            )
        resume_worker.assert_called_once_with({})

    def test_worker_resume_rejects_ready_response_that_is_not_accepting_jobs(self) -> None:
        controller = release_controller.ReleaseController(
            self.root,
            self.build(),
            "auto",
            False,
        )
        with (
            mock.patch.object(
                controller,
                "worker_request",
                return_value={
                    "status": "ready",
                    "accepting_jobs": False,
                    "active_jobs": 0,
                },
            ),
            self.assertRaisesRegex(release_controller.ReleaseError, "invalid resume response"),
        ):
            controller.resume_worker({})

    def test_worker_resume_acceptance_matches_active_capacity(self) -> None:
        controller = release_controller.ReleaseController(
            self.root,
            self.build(),
            "auto",
            False,
        )
        valid = (
            {"status": "ready", "accepting_jobs": True, "active_jobs": 0},
            {"status": "ready", "accepting_jobs": False, "active_jobs": 1},
        )
        for response in valid:
            with (
                self.subTest(response=response),
                mock.patch.object(controller, "worker_request", return_value=response),
            ):
                controller.resume_worker({})

        inconsistent = (
            {"status": "ready", "accepting_jobs": True, "active_jobs": 1},
            {"status": "ready", "accepting_jobs": False, "active_jobs": 0},
            {"status": "ready", "accepting_jobs": False, "active_jobs": 2},
        )
        for response in inconsistent:
            with (
                self.subTest(response=response),
                mock.patch.object(controller, "worker_request", return_value=response),
                self.assertRaisesRegex(
                    release_controller.ReleaseError,
                    "invalid resume response",
                ),
            ):
                controller.resume_worker({})

    def test_wait_for_jobs_can_exclude_drained_monomer_worker(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(self.root, manifest, "auto", False)
        payload = {
            "active_jobs": {
                "monomer_md": 1,
                "polytao": 0,
                "online_knowledge": 0,
                "conditional_generation": 0,
                "reverse_design": 0,
                "gpu_inference": 0,
                "gpu_waiting": 0,
                "inflight_api_writes": 0,
            },
            "active_total": 1,
        }
        completed = subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )
        with mock.patch.object(release_controller.subprocess, "run", return_value=completed):
            controller.wait_for_jobs({"NEXPOLY_DRAIN_TIMEOUT_SECONDS": "1"}, ignore_monomer_md=True)

    def test_custom_drain_hook_cannot_replace_persistent_control(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(self.root, manifest, "auto", False)
        with mock.patch.object(controller, "run") as run:
            controller.drain(
                {
                    "APP_POSTGRES_DSN": "postgresql://fixture",
                    "NEXPOLY_DRAIN_ENABLE_COMMAND": "/bin/untrusted-hook",
                },
                True,
            )
        command = run.call_args.args[0]
        self.assertNotIn("/bin/untrusted-hook", command)
        self.assertIn("app.deployment_control_cli", command)

    def test_deployment_status_requires_exact_counts_and_matching_total(self) -> None:
        jobs = {category: 0 for category in release_controller.ACTIVE_JOB_CATEGORIES}
        self.assertEqual(
            release_controller.validated_active_total(
                {"active_jobs": jobs, "active_total": 0},
                set(release_controller.ACTIVE_JOB_CATEGORIES),
            ),
            0,
        )
        extra = {**jobs, "unreviewed": 0}
        with self.assertRaisesRegex(release_controller.ReleaseError, "exact required"):
            release_controller.validated_active_total(
                {"active_jobs": extra, "active_total": 0},
                set(release_controller.ACTIVE_JOB_CATEGORIES),
            )
        mismatched = jobs.copy()
        mismatched["gpu_waiting"] = 1
        with self.assertRaisesRegex(release_controller.ReleaseError, "does not match"):
            release_controller.validated_active_total(
                {"active_jobs": mismatched, "active_total": 0},
                set(release_controller.ACTIVE_JOB_CATEGORIES),
            )

        v2_jobs = {**jobs, "monomer_dft": 1}
        self.assertEqual(
            release_controller.validated_active_total(
                {
                    "active_jobs_schema_version": 2,
                    "active_jobs": v2_jobs,
                    "active_total": 1,
                },
                set(release_controller.ACTIVE_JOB_CATEGORIES_V1),
            ),
            1,
        )
        for unsupported in (None, 0, 3, True, 1.0, 2.0):
            with (
                self.subTest(active_jobs_schema_version=unsupported),
                self.assertRaisesRegex(release_controller.ReleaseError, "unsupported"),
            ):
                release_controller.validated_active_total(
                    {
                        "active_jobs_schema_version": unsupported,
                        "active_jobs": jobs,
                        "active_total": 0,
                    },
                    set(release_controller.ACTIVE_JOB_CATEGORIES_V1),
                )

        for payload in (
            {"schema_version": 1, "active_jobs": jobs, "active_total": 0},
            {
                "schema_version": 1,
                "active_jobs_schema_version": 1,
                "active_jobs": jobs,
                "active_total": 0,
            },
        ):
            with self.assertRaisesRegex(release_controller.ReleaseError, "legacy"):
                release_controller.validated_active_total(
                    payload,
                    set(release_controller.ACTIVE_JOB_CATEGORIES_V1),
                )

        with self.assertRaisesRegex(release_controller.ReleaseError, "exact required"):
            release_controller.validated_active_total(
                {
                    "active_jobs_schema_version": 1,
                    "active_jobs": {"monomer_md": 0, "online_knowledge": 0},
                    "active_total": 0,
                },
                {"monomer_md", "online_knowledge"},
            )

    def test_busy_worker_cannot_be_restarted_after_global_drain_gate(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(self.root, manifest, "auto", False)
        with (
            mock.patch.object(
                controller,
                "worker_request",
                return_value={"active_jobs": 1, "worker_instance_id": "instance-a"},
            ),
            mock.patch.object(controller, "run") as run,
        ):
            with self.assertRaisesRegex(release_controller.ReleaseError, "global drain gate"):
                controller.restart_or_defer_worker({})

        run.assert_not_called()
        self.assertEqual(controller.worker_previous_instance, "instance-a")

    def test_idle_worker_restarts_immediately(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(self.root, manifest, "auto", False)
        with (
            mock.patch.object(
                controller,
                "worker_request",
                return_value={"active_jobs": 0, "worker_instance_id": "instance-a"},
            ),
            mock.patch.object(controller, "run") as run,
            mock.patch.object(controller, "wait_for_worker_health") as worker_health,
        ):
            controller.restart_or_defer_worker({})

        self.assertIn("restart", run.call_args.args[0])
        worker_health.assert_called_once_with(
            {},
            expected_release=controller.release_dir,
            previous_instance_id="instance-a",
        )

    def test_worker_health_requires_new_instance_and_300_step_contract(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(self.root, manifest, "auto", False)
        release = controller.ops / "releases" / SHA
        release.mkdir(parents=True)
        shutil.copy2(manifest, release / "release-manifest.json")
        (release / "worker-venv").mkdir()
        base_identity = write_worker_base_identity(release)
        healthy = {
            "status": "ok",
            "runtime_ready": True,
            "accepting_jobs": True,
            "default_steps": 300,
            "max_steps": 300,
            "worker_instance_id": "instance-b",
            "source_sha": SHA,
            "source_root": str(release.resolve()),
            "venv_prefix": str((release / "worker-venv").resolve()),
            "python_executable": base_identity["resolved_path"],
        }
        with mock.patch.object(controller, "worker_request", return_value=healthy):
            result = controller.wait_for_worker_health(
                {"NEXPOLY_WORKER_HEALTH_TIMEOUT_SECONDS": "1"},
                expected_release=release,
                previous_instance_id="instance-a",
            )

        self.assertEqual(result["worker_instance_id"], "instance-b")

    def test_worker_health_rejects_new_instance_with_old_source_or_venv(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(self.root, manifest, "auto", False)
        release = controller.ops / "releases" / SHA
        release.mkdir(parents=True)
        shutil.copy2(manifest, release / "release-manifest.json")
        (release / "worker-venv").mkdir()
        base_identity = write_worker_base_identity(release)
        healthy = {
            "status": "ok",
            "runtime_ready": True,
            "accepting_jobs": True,
            "default_steps": 300,
            "max_steps": 300,
            "worker_instance_id": "brand-new-instance",
            "source_sha": SHA,
            "source_root": str(release.resolve()),
            "venv_prefix": str((release / "worker-venv").resolve()),
            "python_executable": base_identity["resolved_path"],
        }

        old_source = {**healthy, "source_sha": "1" * 40}
        with self.assertRaisesRegex(release_controller.ReleaseError, "source SHA"):
            controller.assert_worker_runtime_identity(old_source, release)

        old_venv = {**healthy, "venv_prefix": "/ops/releases/old/worker-venv"}
        with self.assertRaisesRegex(release_controller.ReleaseError, "venv"):
            controller.assert_worker_runtime_identity(old_venv, release)

        wrong_executable = {
            **healthy,
            "python_executable": "/opt/other-byteff2/bin/python3.11",
        }
        with self.assertRaisesRegex(release_controller.ReleaseError, "base identity"):
            controller.assert_worker_runtime_identity(wrong_executable, release)

    def test_deploy_waits_all_jobs_and_uses_authoritative_smoke(self) -> None:
        source = CONTROLLER_PATH.read_text(encoding="utf-8")
        self.assertNotIn("self.wait_for_jobs(environment, ignore_monomer_md=True)", source)
        self.assertIn("self.wait_for_jobs(environment)", source)
        self.assertIn("self.restart_or_defer_worker(environment)", source)
        self.assertIn("self.resume_worker(environment)", source)
        self.assertIn("self.run_ingress_isolated_contract_smoke(environment)", source)
        self.assertIn("self.run_ingress_isolated_monomer_smoke(environment)", source)


if __name__ == "__main__":
    unittest.main()

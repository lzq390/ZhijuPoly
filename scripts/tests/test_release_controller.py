from __future__ import annotations

import argparse
import ast
from contextlib import ExitStack, contextmanager
import fcntl
import inspect
import importlib.metadata
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

from scripts.tests.test_postgres_media_evidence import (
    external_inventory_fixture,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTROLLER_PATH = REPOSITORY_ROOT / "scripts" / "release_controller.py"
SPEC = importlib.util.spec_from_file_location("release_controller", CONTROLLER_PATH)
assert SPEC and SPEC.loader
release_controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_controller)
BOOTSTRAP_ASSET_PATH = REPOSITORY_ROOT / "scripts" / "bootstrap_asset_release.py"
BOOTSTRAP_ASSET_SPEC = importlib.util.spec_from_file_location(
    "bootstrap_asset_release_contract",
    BOOTSTRAP_ASSET_PATH,
)
assert BOOTSTRAP_ASSET_SPEC and BOOTSTRAP_ASSET_SPEC.loader
bootstrap_asset_release = importlib.util.module_from_spec(BOOTSTRAP_ASSET_SPEC)
BOOTSTRAP_ASSET_SPEC.loader.exec_module(bootstrap_asset_release)

SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
BACKEND_IMAGE = "ghcr.io/lzq390/nexpoly-backend@" + DIGEST
WEB_IMAGE = "ghcr.io/lzq390/nexpoly-web@sha256:" + "c" * 64
BYTEFF2_RUNTIME_ASSET_FIXTURE = (
    REPOSITORY_ROOT / "scripts" / "tests" / "fixtures" / "bond_length_ref.csv"
)
PRODUCTION_BYTEFF2_FORMAL_RUNTIME_ASSETS = (
    release_controller.BYTEFF2_FORMAL_RUNTIME_ASSETS
)
PRODUCTION_BYTEFF2_GIT_REVISION = release_controller.BYTEFF2_GIT_REVISION
TEST_BYTEFF2_RUNTIME_CONTENTS = {
    PRODUCTION_BYTEFF2_FORMAL_RUNTIME_ASSETS[0][0]: (
        BYTEFF2_RUNTIME_ASSET_FIXTURE.read_bytes()
    ),
    PRODUCTION_BYTEFF2_FORMAL_RUNTIME_ASSETS[1][0]: b"fixture: true\n",
    PRODUCTION_BYTEFF2_FORMAL_RUNTIME_ASSETS[2][0]: b"fixture-model\n",
}
TEST_BYTEFF2_FORMAL_RUNTIME_ASSETS = tuple(
    (
        relative,
        len(TEST_BYTEFF2_RUNTIME_CONTENTS[relative]),
        release_controller.sha256_bytes(
            TEST_BYTEFF2_RUNTIME_CONTENTS[relative]
        ).removeprefix("sha256:"),
    )
    for relative, _size, _digest in PRODUCTION_BYTEFF2_FORMAL_RUNTIME_ASSETS
)
TEST_BYTEFF2_AUDITED_OVERLAY_ASSETS = TEST_BYTEFF2_FORMAL_RUNTIME_ASSETS[1:]


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
    def test_production_compose_up_never_controls_postgres_dependencies(
        self,
    ) -> None:
        calls: list[tuple[Path, int, list[str]]] = []
        for source_path in (
            REPOSITORY_ROOT / "scripts/release_controller.py",
            REPOSITORY_ROOT / "scripts/pull_deploy_controller.py",
        ):
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                if (
                    not isinstance(function, ast.Attribute)
                    or function.attr not in {"compose", "_compose"}
                ):
                    continue
                literals = [
                    argument.value
                    for argument in node.args
                    if isinstance(argument, ast.Constant)
                    and isinstance(argument.value, str)
                ]
                if "up" in literals:
                    calls.append((source_path, node.lineno, literals))

        self.assertEqual(len(calls), 10)
        for source_path, line, literals in calls:
            with self.subTest(source=source_path.name, line=line):
                self.assertIn("--no-deps", literals)
                self.assertNotIn("lab-postgres", literals)
                self.assertNotIn("postgres-init", literals)
                self.assertIn(literals[-1], {"backend", "nginx"})

    def setUp(self) -> None:
        for patcher in (
            mock.patch.object(
                release_controller,
                "BYTEFF2_FORMAL_RUNTIME_ASSETS",
                TEST_BYTEFF2_FORMAL_RUNTIME_ASSETS,
            ),
            mock.patch.object(
                release_controller,
                "BYTEFF2_AUDITED_OVERLAY_ASSETS",
                TEST_BYTEFF2_AUDITED_OVERLAY_ASSETS,
            ),
            mock.patch.object(
                release_controller,
                "BYTEFF2_GIT_REVISION",
                SHA,
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        candidate_controller = self.root / "candidate-controller"
        candidate_controller.mkdir()
        candidate_worker_env_helper = candidate_controller / "monomer_worker_env.py"
        shutil.copyfile(
            REPOSITORY_ROOT / "scripts" / "monomer_worker_env.py",
            candidate_worker_env_helper,
        )
        os.chmod(candidate_worker_env_helper, 0o700)
        controller_directory_patcher = mock.patch.object(
            release_controller,
            "CONTROLLER_DIRECTORY",
            candidate_controller,
        )
        controller_directory_patcher.start()
        self.addCleanup(controller_directory_patcher.stop)
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
                    "schema_version": 2,
                    "asset_manifest_digest": DIGEST,
                    "predecessor_asset_manifest_digest": (
                        "sha256:" + "d" * 64
                    ),
                    "changed_asset_trees": ["byteff2"],
                    "datasets_on_asset_change": [],
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

    def make_asset_release(self, name: str = "asset-release") -> Path:
        release = self.root / name
        for tree in ("model", "database", "backend-data", "byteff2"):
            (release / tree).mkdir(parents=True)
        (release / "model" / "checkpoint.bin").write_bytes(b"model")
        (release / "database" / "source.csv").write_text("id,value\n1,2\n", encoding="utf-8")
        (release / "backend-data" / "runtime.json").write_text("{}\n", encoding="utf-8")
        (release / "byteff2" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        (release / "byteff2" / "BYTEFF2-COMMIT").write_text(SHA + "\n", encoding="ascii")
        for relative, content in TEST_BYTEFF2_RUNTIME_CONTENTS.items():
            runtime_asset = release / "byteff2" / relative
            runtime_asset.parent.mkdir(parents=True, exist_ok=True)
            runtime_asset.write_bytes(content)
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
            "schema_version": 2,
            "byteff2_commit": SHA,
            "byteff2_submodules": {},
            "byteff2_source": {
                "source": release_controller.BYTEFF2_GIT_SOURCE,
                "revision": SHA,
            },
            "byteff2_audited_overlays": {
                "source": release_controller.BYTEFF2_AUDITED_OVERLAY_SOURCE,
                "revision": release_controller.BYTEFF2_AUDITED_OVERLAY_REVISION,
                "files": [
                    {
                        "source_path": (
                            "trained_models/"
                            + PurePosixPath(relative).name
                        ),
                        "path": relative,
                        "size": size,
                        "sha256": checksum,
                    }
                    for relative, size, checksum in TEST_BYTEFF2_AUDITED_OVERLAY_ASSETS
                ],
            },
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

    def prepare_mock_ready_release(
        self,
        controller: release_controller.ReleaseController,
    ) -> None:
        """Create the minimal final READY tree used by mocked deploy tests."""

        controller.release_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(controller.release_dir, 0o700)
        ready = controller.release_dir / release_controller.PROVISIONING_READY_NAME
        if not ready.exists():
            ready.write_text('{"status":"ready"}\n', encoding="utf-8")
            os.chmod(ready, 0o600)
        controller.candidate_dir = controller.release_dir

    def seal_mock_interrupted_release(
        self,
        controller: release_controller.ReleaseController,
        marker: dict[str, object],
    ) -> None:
        self.prepare_mock_ready_release(controller)
        marker["provisioning_ready_sha256"] = release_controller.sha256_file(
            controller.release_dir / release_controller.PROVISIONING_READY_NAME
        )

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

    def test_schema_v2_release_input_pins_predecessor_and_skips_database_rebuilds(
        self,
    ) -> None:
        self.release_input.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "asset_manifest_digest": (
                        release_controller.SCHEMA_V2_ASSET_MANIFEST_DIGEST
                    ),
                    "predecessor_asset_manifest_digest": (
                        release_controller.SCHEMA_V2_PREDECESSOR_ASSET_MANIFEST_DIGEST
                    ),
                    "changed_asset_trees": ["byteff2"],
                    "datasets_on_asset_change": [],
                }
            ),
            encoding="utf-8",
        )

        loaded = release_controller.load_release_input(self.release_input)
        output = self.build_single_bundle()
        document = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(loaded["changed_asset_trees"], ["byteff2"])
        self.assertEqual(loaded["datasets_on_asset_change"], [])
        self.assertEqual(
            document["asset_manifest_digest"],
            release_controller.SCHEMA_V2_ASSET_MANIFEST_DIGEST,
        )
        self.assertEqual(document["datasets_on_asset_change"], [])

    def test_schema_v2_release_input_rejects_database_rebuild_declarations(self) -> None:
        self.release_input.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "asset_manifest_digest": (
                        release_controller.SCHEMA_V2_ASSET_MANIFEST_DIGEST
                    ),
                    "predecessor_asset_manifest_digest": (
                        release_controller.SCHEMA_V2_PREDECESSOR_ASSET_MANIFEST_DIGEST
                    ),
                    "changed_asset_trees": ["byteff2"],
                    "datasets_on_asset_change": ["online"],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "must not rebuild PostgreSQL datasets",
        ):
            release_controller.load_release_input(self.release_input)

    def test_schema_v1_release_input_is_retired(self) -> None:
        self.release_input.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "asset_manifest_digest": DIGEST,
                    "datasets_on_asset_change": ["online"],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "must use the non-rebuilding schema-v2 contract",
        ):
            release_controller.load_release_input(self.release_input)

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

    def test_predecessor_schema_v2_asset_verifies_unchanged_tree_evidence(
        self,
    ) -> None:
        release = self.make_asset_release()
        manifest_path = release / "ASSET-MANIFEST.json"
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        unchanged_digests = {
            tree: release_controller.sha256_bytes(
                (
                    json.dumps(
                        {"files": document["assets"][tree]},
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
            )
            for tree in ("model", "database", "backend-data")
        }
        document.update(
            {
                "predecessor_asset_digest": (
                    release_controller.SCHEMA_V2_PREDECESSOR_ASSET_MANIFEST_DIGEST
                ),
                "changed_asset_trees": ["byteff2"],
                "unchanged_asset_tree_digests": unchanged_digests,
            }
        )
        os.chmod(release, 0o755)
        os.chmod(manifest_path, 0o644)
        manifest_path.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(manifest_path, 0o444)
        os.chmod(release, 0o555)
        digest = release_controller.sha256_file(manifest_path)

        with (
            mock.patch.object(
                release_controller,
                "SCHEMA_V2_UNCHANGED_ASSET_TREE_DIGESTS",
                unchanged_digests,
            ),
            mock.patch.object(
                release_controller,
                "SCHEMA_V2_ASSET_MANIFEST_DIGEST",
                digest,
            ),
        ):
            _resolved, actual_digest, _commit = (
                release_controller.inspect_asset_release(release)
            )

        self.assertEqual(actual_digest, digest)

    def test_provenance_schema_v2_asset_verifies_all_tree_and_build_evidence(
        self,
    ) -> None:
        release = self.make_asset_release()
        manifest_path = release / "ASSET-MANIFEST.json"
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        tree_digests = {
            tree: release_controller.sha256_bytes(
                (
                    json.dumps(
                        {"files": document["assets"][tree]},
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
            )
            for tree in ("model", "database", "backend-data", "byteff2")
        }
        unchanged_digests = {
            tree: tree_digests[tree]
            for tree in ("model", "database", "backend-data")
        }
        predecessor_digest = (
            release_controller.SCHEMA_V2_PREDECESSOR_ASSET_MANIFEST_DIGEST
        )
        document.update(
            {
                "predecessor_asset_digest": predecessor_digest,
                "changed_asset_trees": ["byteff2"],
                "unchanged_asset_tree_digests": unchanged_digests,
                "asset_tree_digests": tree_digests,
                "byteff2_tree": release_controller.BYTEFF2_GIT_TREE,
                "byteff2_submodule_trees": {},
                "build_provenance": {
                    "schema_version": 1,
                    "builder_source": {
                        "repository": (
                            release_controller.ASSET_BUILD_SOURCE_REPOSITORY
                        ),
                        "commit": "1" * 40,
                        "tree": "2" * 40,
                        "script_path": release_controller.ASSET_BUILD_SOURCE_SCRIPT,
                        "script_blob": "3" * 40,
                    },
                    "evidence": {
                        "predecessor_manifest_digest": predecessor_digest,
                        "predecessor_all_trees_rehashed": [
                            "model",
                            "database",
                            "backend-data",
                            "byteff2",
                        ],
                        "unchanged_trees_byte_identical": [
                            "model",
                            "database",
                            "backend-data",
                        ],
                        "asset_tree_digest_algorithm": (
                            "canonical-manifest-inventory-v1"
                        ),
                        "byteff2_source_verification": (
                            "clean-recursive-commit-and-tree"
                        ),
                        "staging_directory_mode": "0700",
                        "file_and_directory_fsync": True,
                        "publication": "atomic-rename",
                        "existing_target": "full-content-revalidation",
                    },
                },
            }
        )
        os.chmod(release, 0o755)
        os.chmod(manifest_path, 0o644)
        manifest_path.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(manifest_path, 0o444)
        os.chmod(release, 0o555)
        digest = release_controller.sha256_file(manifest_path)

        with (
            mock.patch.object(
                release_controller,
                "SCHEMA_V2_UNCHANGED_ASSET_TREE_DIGESTS",
                unchanged_digests,
            ),
            mock.patch.object(
                release_controller,
                "SCHEMA_V2_ASSET_MANIFEST_DIGEST",
                digest,
            ),
        ):
            _resolved, actual_digest, _commit = (
                release_controller.inspect_asset_release(release)
            )
        self.assertEqual(actual_digest, digest)

        os.chmod(release, 0o755)
        os.chmod(manifest_path, 0o644)
        document["build_provenance"]["builder_source"]["script_blob"] = "short"
        manifest_path.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(manifest_path, 0o444)
        os.chmod(release, 0o555)
        with (
            mock.patch.object(
                release_controller,
                "SCHEMA_V2_UNCHANGED_ASSET_TREE_DIGESTS",
                unchanged_digests,
            ),
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "invalid builder identity",
            ),
        ):
            release_controller.inspect_asset_release(release)

    def test_asset_release_rejects_unmanifested_or_writable_content(self) -> None:
        release = self.make_asset_release()
        model = release / "model"
        os.chmod(model, 0o755)
        (model / "unlisted.bin").write_bytes(b"unlisted")
        os.chmod(model / "unlisted.bin", 0o444)
        os.chmod(model, 0o555)

        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "inventory differs",
        ):
            release_controller.inspect_asset_release(release)

    def test_candidate_byteff2_runtime_assets_accept_exact_fixed_contract(self) -> None:
        release = self.make_asset_release()
        self.assertEqual(
            PRODUCTION_BYTEFF2_FORMAL_RUNTIME_ASSETS,
            (
                (
                    "submodules/bytemol/bytemol/toolkit/infer_molecule/bond_length_ref.csv",
                    802,
                    "caa78ff02c7e65fb0c8bcf240382fa8d90b0dfea85a4d9888c96eab04cc4a40e",
                ),
                (
                    "byteff2/trained_models/fftrainer_config_in_use.yaml",
                    986,
                    "8245a5c6ad9b4aa9d180c8bb24d6f05c210f1724ffae93aec0ef4f88e5fd7ea3",
                ),
                (
                    "byteff2/trained_models/optimal.pt",
                    111_892_932,
                    "ae47a6e6860b563908a2e0a83d4a3f6adc1c36b48f544e2241d24066d43d539c",
                ),
            ),
        )
        release_controller.validate_candidate_byteff2_runtime_assets(release)

    def test_bootstrap_and_release_controller_share_audited_overlay_contract(self) -> None:
        self.assertEqual(
            release_controller.BYTEFF2_GIT_SOURCE,
            bootstrap_asset_release.BYTEFF2_GIT_SOURCE,
        )
        self.assertEqual(
            "8f2813407ba5fbecfb5ec5c69e10b124c5b5bdc2",
            bootstrap_asset_release.BYTEFF2_GIT_REVISION,
        )
        self.assertEqual(
            PRODUCTION_BYTEFF2_GIT_REVISION,
            bootstrap_asset_release.BYTEFF2_GIT_REVISION,
        )
        self.assertEqual(
            release_controller.BYTEFF2_AUDITED_OVERLAY_SOURCE,
            bootstrap_asset_release.BYTEFF2_AUDITED_OVERLAY_SOURCE,
        )
        self.assertEqual(
            release_controller.BYTEFF2_AUDITED_OVERLAY_REVISION,
            bootstrap_asset_release.BYTEFF2_AUDITED_OVERLAY_REVISION,
        )
        self.assertEqual(
            PRODUCTION_BYTEFF2_FORMAL_RUNTIME_ASSETS[1:],
            bootstrap_asset_release.BYTEFF2_AUDITED_OVERLAY_FILES,
        )
        with mock.patch.object(
            release_controller,
            "BYTEFF2_AUDITED_OVERLAY_ASSETS",
            PRODUCTION_BYTEFF2_FORMAL_RUNTIME_ASSETS[1:],
        ):
            release_controller.validate_byteff2_audited_overlay(
                bootstrap_asset_release.byteff2_audited_overlays_manifest(),
                require_exact_identity=True,
            )

    def test_candidate_byteff2_runtime_assets_reject_each_missing_disk_file(self) -> None:
        for index, (relative, _size, _digest) in enumerate(
            TEST_BYTEFF2_FORMAL_RUNTIME_ASSETS
        ):
            with self.subTest(relative=relative):
                release = self.make_asset_release(f"missing-runtime-{index}")
                runtime_asset = release / "byteff2" / relative
                os.chmod(runtime_asset.parent, 0o755)
                runtime_asset.unlink()
                os.chmod(runtime_asset.parent, 0o555)
                with self.assertRaisesRegex(
                    release_controller.ReleaseError,
                    "missing or unsafe",
                ):
                    release_controller.validate_candidate_byteff2_runtime_assets(
                        release
                    )

    def test_candidate_byteff2_runtime_assets_reject_each_inventory_omission(self) -> None:
        for index, (relative, _size, _digest) in enumerate(
            TEST_BYTEFF2_FORMAL_RUNTIME_ASSETS
        ):
            with self.subTest(relative=relative):
                release = self.make_asset_release(f"omitted-runtime-{index}")
                manifest_path = release / "ASSET-MANIFEST.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["assets"]["byteff2"] = [
                    record
                    for record in manifest["assets"]["byteff2"]
                    if record["path"] != relative
                ]
                os.chmod(manifest_path, 0o644)
                manifest_path.write_text(
                    json.dumps(manifest, sort_keys=True, separators=(",", ":"))
                    + "\n",
                    encoding="utf-8",
                )
                os.chmod(manifest_path, 0o444)
                with self.assertRaisesRegex(
                    release_controller.ReleaseError,
                    "omits",
                ):
                    release_controller.validate_candidate_byteff2_runtime_assets(
                        release
                    )

    def test_candidate_byteff2_runtime_assets_reject_self_consistent_wrong_files(self) -> None:
        for index, (relative, _size, _digest) in enumerate(
            TEST_BYTEFF2_FORMAL_RUNTIME_ASSETS
        ):
            with self.subTest(relative=relative):
                release = self.make_asset_release(f"wrong-runtime-{index}")
                runtime_asset = release / "byteff2" / relative
                os.chmod(runtime_asset, 0o644)
                runtime_asset.write_bytes(b"self-consistent-but-not-fixed\n")
                os.chmod(runtime_asset, 0o444)
                manifest_path = release / "ASSET-MANIFEST.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                inventory_record = next(
                    item
                    for item in manifest["assets"]["byteff2"]
                    if item["path"] == relative
                )
                inventory_record["size"] = runtime_asset.stat().st_size
                inventory_record["sha256"] = release_controller.sha256_file(
                    runtime_asset
                ).removeprefix("sha256:")
                for overlay_record in manifest["byteff2_audited_overlays"]["files"]:
                    if overlay_record["path"] == relative:
                        overlay_record.update(inventory_record)
                os.chmod(manifest_path, 0o644)
                manifest_path.write_text(
                    json.dumps(manifest, sort_keys=True, separators=(",", ":"))
                    + "\n",
                    encoding="utf-8",
                )
                os.chmod(manifest_path, 0o444)
                release_controller.inspect_asset_release(release)
                with self.assertRaisesRegex(
                    release_controller.ReleaseError,
                    "wrong .*runtime asset contract|wrong .*overlay contract",
                ):
                    release_controller.validate_candidate_byteff2_runtime_assets(
                        release
                    )

    def test_candidate_requires_schema_v2_exact_audited_overlay(self) -> None:
        legacy = self.make_asset_release("legacy-v1-candidate")
        legacy_manifest = legacy / "ASSET-MANIFEST.json"
        legacy_document = json.loads(legacy_manifest.read_text(encoding="utf-8"))
        legacy_document["schema_version"] = 1
        legacy_document.pop("byteff2_source")
        legacy_document.pop("byteff2_audited_overlays")
        os.chmod(legacy_manifest, 0o644)
        legacy_manifest.write_text(
            json.dumps(legacy_document, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        os.chmod(legacy_manifest, 0o444)
        release_controller.inspect_asset_release(legacy)
        with self.assertRaisesRegex(release_controller.ReleaseError, "schema v2"):
            release_controller.validate_candidate_byteff2_runtime_assets(legacy)

        wrong_git_source = self.make_asset_release("wrong-byteff2-git-source")
        source_manifest = wrong_git_source / "ASSET-MANIFEST.json"
        source_document = json.loads(source_manifest.read_text(encoding="utf-8"))
        source_document["byteff2_source"]["source"] = "https://example.invalid/byteff2.git"
        os.chmod(source_manifest, 0o644)
        source_manifest.write_text(
            json.dumps(source_document, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        os.chmod(source_manifest, 0o444)
        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "invalid ByteFF2 source metadata",
        ):
            release_controller.inspect_asset_release(wrong_git_source)

        wrong_git_revision = self.make_asset_release("wrong-byteff2-git-revision")
        source_manifest = wrong_git_revision / "ASSET-MANIFEST.json"
        source_document = json.loads(source_manifest.read_text(encoding="utf-8"))
        source_document["byteff2_source"]["revision"] = "0" * 40
        os.chmod(source_manifest, 0o644)
        source_manifest.write_text(
            json.dumps(source_document, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        os.chmod(source_manifest, 0o444)
        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "invalid ByteFF2 source metadata",
        ):
            release_controller.inspect_asset_release(wrong_git_revision)

        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "wrong ByteFF2 Git source contract",
        ):
            release_controller.validate_byteff2_source(
                {
                    "source": release_controller.BYTEFF2_GIT_SOURCE,
                    "revision": "0" * 40,
                },
                manifest_commit="0" * 40,
                require_exact_identity=True,
            )

        wrong_source = self.make_asset_release("wrong-overlay-source")
        source_manifest = wrong_source / "ASSET-MANIFEST.json"
        source_document = json.loads(source_manifest.read_text(encoding="utf-8"))
        source_document["byteff2_audited_overlays"]["source"] = "unreviewed.example"
        os.chmod(source_manifest, 0o644)
        source_manifest.write_text(
            json.dumps(source_document, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        os.chmod(source_manifest, 0o444)
        release_controller.inspect_asset_release(wrong_source)
        with self.assertRaisesRegex(release_controller.ReleaseError, "overlay contract"):
            release_controller.validate_candidate_byteff2_runtime_assets(wrong_source)

        omitted_overlay = self.make_asset_release("omitted-overlay-file")
        omitted_manifest = omitted_overlay / "ASSET-MANIFEST.json"
        omitted_document = json.loads(omitted_manifest.read_text(encoding="utf-8"))
        omitted_document["byteff2_audited_overlays"]["files"].pop()
        os.chmod(omitted_manifest, 0o644)
        omitted_manifest.write_text(
            json.dumps(omitted_document, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        os.chmod(omitted_manifest, 0o444)
        release_controller.inspect_asset_release(omitted_overlay)
        with self.assertRaisesRegex(release_controller.ReleaseError, "overlay contract"):
            release_controller.validate_candidate_byteff2_runtime_assets(
                omitted_overlay
            )

    def test_broken_legacy_asset_does_not_block_a_valid_candidate_asset(self) -> None:
        broken = self.make_asset_release()
        for relative, _size, _digest in TEST_BYTEFF2_FORMAL_RUNTIME_ASSETS:
            runtime_asset = broken / "byteff2" / relative
            os.chmod(runtime_asset.parent, 0o755)
            runtime_asset.unlink()
            os.chmod(runtime_asset.parent, 0o555)
        broken_manifest = broken / "ASSET-MANIFEST.json"
        document = json.loads(broken_manifest.read_text(encoding="utf-8"))
        document["schema_version"] = 1
        document.pop("byteff2_source")
        document.pop("byteff2_audited_overlays")
        formal_paths = {
            relative for relative, _size, _digest in TEST_BYTEFF2_FORMAL_RUNTIME_ASSETS
        }
        document["assets"]["byteff2"] = [
            record
            for record in document["assets"]["byteff2"]
            if record["path"] not in formal_paths
        ]
        os.chmod(broken_manifest, 0o644)
        broken_manifest.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(broken_manifest, 0o444)
        _, broken_digest, _ = release_controller.inspect_asset_release(broken)

        store = self.root / "candidate-assets" / "releases"
        store.mkdir(parents=True)
        broken_target = store / broken_digest.removeprefix("sha256:")
        os.chmod(broken, 0o755)
        broken.rename(broken_target)
        os.chmod(broken_target, 0o555)
        pointer = self.root / "production" / "ops" / "current-assets"
        pointer.parent.mkdir(parents=True)
        pointer.symlink_to(broken_target)

        valid = self.make_asset_release()
        _, valid_digest, _ = release_controller.inspect_asset_release(valid)
        valid_target = store / valid_digest.removeprefix("sha256:")
        os.chmod(valid, 0o755)
        valid.rename(valid_target)
        os.chmod(valid_target, 0o555)
        with mock.patch.object(release_controller, "ASSET_RELEASES_ROOT", store):
            current, _, _ = release_controller.inspect_managed_asset_pointer(
                pointer,
                broken_digest,
            )
            candidate, _, _ = release_controller.inspect_managed_asset_release(
                valid_digest,
                require_byteff2_runtime_assets=True,
            )
        self.assertEqual(current, broken_target)
        self.assertEqual(candidate, valid_target)

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

    def test_production_environment_enforces_worker_assets_before_mutation(self) -> None:
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
        stable_worker_env_helper = controller.config_dir / "monomer_worker_env.py"
        shutil.copy2(
            REPOSITORY_ROOT / "scripts" / "monomer_worker_env.py",
            stable_worker_env_helper,
        )
        os.chmod(stable_worker_env_helper, 0o700)
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

        with (
            mock.patch.object(release_controller, "ASSET_RELEASES_ROOT", store),
            mock.patch.dict(
                os.environ,
                {
                    "MONOMER_MD_REQUIRE_TRANSPORT_READY": "true",
                    "MONOMER_MD_WORKER_UDS": "/tmp/forged-worker.sock",
                    "NEXPOLY_HEALTH_URLS": "http://127.0.0.1:65535/health",
                    "NEXPOLY_WEB_BASE_URL": "http://127.0.0.1:65535",
                    "NEXPOLY_MONOMER_MD_STATUS_URL": "http://127.0.0.1:65535/status",
                    "NEXPOLY_MONOMER_MD_PROTOCOLS_URL": "http://127.0.0.1:65535/protocols",
                    "PYTHONPATH": "/tmp/forged-python",
                    "LD_PRELOAD": "/tmp/forged-loader.so",
                },
            ),
        ):
            environment = controller.environment()
        self.assertEqual(environment["NEXPOLY_ASSET_ROOT"], str(pointer))
        self.assertFalse(controller.deploy_transport_required)
        self.assertNotIn("MONOMER_MD_REQUIRE_TRANSPORT_READY", environment)
        for forbidden_inherited in (
            "MONOMER_MD_WORKER_UDS",
            "NEXPOLY_WEB_BASE_URL",
            "NEXPOLY_MONOMER_MD_STATUS_URL",
            "NEXPOLY_MONOMER_MD_PROTOCOLS_URL",
            "PYTHONPATH",
            "LD_PRELOAD",
        ):
            self.assertNotIn(forbidden_inherited, environment)
        self.assertEqual(environment["PATH"], release_controller.SAFE_SYSTEM_PATH)
        self.assertEqual(
            environment["NEXPOLY_HEALTH_URLS"],
            release_controller.PRODUCTION_HEALTH_URL,
        )

        # The three runtime assets are shared by every formal/Density runner,
        # so a Worker payload must enforce schema v2 even when the deploy-only
        # Transport CUDA gate remains disabled.
        legacy_candidate = self.make_asset_release("legacy-worker-candidate")
        legacy_manifest = legacy_candidate / "ASSET-MANIFEST.json"
        legacy_document = json.loads(legacy_manifest.read_text(encoding="utf-8"))
        legacy_document["schema_version"] = 1
        legacy_document.pop("byteff2_source")
        legacy_document.pop("byteff2_audited_overlays")
        os.chmod(legacy_manifest, 0o644)
        legacy_manifest.write_text(
            json.dumps(legacy_document, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        os.chmod(legacy_manifest, 0o444)
        _, legacy_digest, _ = release_controller.inspect_asset_release(
            legacy_candidate
        )
        legacy_target = store / legacy_digest.removeprefix("sha256:")
        os.chmod(legacy_candidate, 0o755)
        legacy_candidate.rename(legacy_target)
        os.chmod(legacy_target, 0o555)
        controller.document["asset_manifest_digest"] = legacy_digest
        with (
            mock.patch.object(release_controller, "ASSET_RELEASES_ROOT", store),
            mock.patch.object(controller, "drain") as drain,
            mock.patch.object(controller, "run") as run,
            mock.patch.object(controller, "backup_database") as backup,
            self.assertRaisesRegex(release_controller.ReleaseError, "schema v2"),
        ):
            controller.environment()
        self.assertFalse(controller.deploy_transport_required)
        drain.assert_not_called()
        run.assert_not_called()
        backup.assert_not_called()
        controller.document["asset_manifest_digest"] = digest

        worker_env_path = controller.config_dir / "worker.env"
        worker_env = worker_env_path.read_text(encoding="utf-8")
        strict_worker_env = (
            worker_env
            + "BYTEFF2_OPENMM_DIR=/home/devuser/miniconda3/envs/byteff2-repro/byteff2_openmm/openmm\n"
            + "MONOMER_MD_TRANSPORT_CUDA_SMOKE_ENABLED=true\n"
        )
        worker_env_path.write_text(strict_worker_env, encoding="utf-8")
        os.chmod(worker_env_path, 0o600)
        controller.env_file.write_text(
            deploy_values + "MONOMER_MD_REQUIRE_TRANSPORT_READY=true\n",
            encoding="utf-8",
        )
        os.chmod(controller.env_file, 0o600)
        with (
            mock.patch.object(release_controller, "ASSET_RELEASES_ROOT", store),
            mock.patch.dict(
                os.environ,
                {"MONOMER_MD_REQUIRE_TRANSPORT_READY": "false"},
            ),
        ):
            strict_environment = controller.environment()
        self.assertTrue(controller.deploy_transport_required)
        self.assertNotIn(
            "MONOMER_MD_REQUIRE_TRANSPORT_READY",
            strict_environment,
        )

        worker_env_path.write_text(
            strict_worker_env + "MONOMER_MD_REQUIRE_TRANSPORT_READY=false\n",
            encoding="utf-8",
        )
        os.chmod(worker_env_path, 0o600)
        with (
            mock.patch.object(release_controller, "ASSET_RELEASES_ROOT", store),
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "MONOMER_MD_REQUIRE_TRANSPORT_READY",
            ),
        ):
            controller.environment()
        worker_env_path.write_text(worker_env, encoding="utf-8")
        os.chmod(worker_env_path, 0o600)
        controller.env_file.write_text(deploy_values, encoding="utf-8")
        os.chmod(controller.env_file, 0o600)

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
        ) as contract_run:
            guard_json = "{}"
            guard_sha256 = release_controller.sha256_bytes(
                guard_json.encode("utf-8")
            )
            self.assertEqual(
                controller.run_migrations(
                    {},
                    mode="contract-0012",
                    contract_guard_json=guard_json,
                    contract_guard_sha256=guard_sha256,
                ),
                ["0012_drop_polytao_jobs"],
            )
        self.assertEqual(
            contract_run.call_args.args[0][-4:],
            [
                "--contract-guard-json",
                guard_json,
                "--contract-guard-sha256",
                guard_sha256,
            ],
        )
        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "requires an exact canonical transaction guard",
        ):
            controller.run_migrations({}, mode="contract-0012")

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
        self.assertEqual(
            plan["archive_policy"],
            {
                "relation": "generation.polytao_jobs",
                "rows": "all-at-maintenance-window",
                "status_counts": "dynamic",
                "seal_after": "admission-drained-and-active-jobs-zero",
            },
        )

    def test_0012_archive_evidence_seals_dynamic_business_rows(self) -> None:
        maintenance = release_controller.PolytaoContractMaintenance(
            self.root / "production",
            self.build_v2(),
            "contract-0012-fixture",
            False,
        )
        evidence = {
            "schema_version": 2,
            "row_count": 37,
            "status_counts": {
                "completed": 20,
                "failed": 7,
                "cancelled": 10,
            },
            "rows_sha256": "a" * 64,
            "schema_sha256": "b" * 64,
            "structure_counts": {
                "columns": 1,
                "indexes": 1,
                "constraints": 1,
                "triggers": 0,
            },
        }

        self.assertEqual(maintenance._validate_archive_evidence(evidence), evidence)

        incomplete = json.loads(json.dumps(evidence))
        incomplete["status_counts"]["completed"] = 19
        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "complete dynamic business-row set",
        ):
            maintenance._validate_archive_evidence(incomplete)

        unknown_status = json.loads(json.dumps(evidence))
        unknown_status["status_counts"] = {"completed": 36, "unexpected": 1}
        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "complete dynamic business-row set",
        ):
            maintenance._validate_archive_evidence(unknown_status)

        active = json.loads(json.dumps(evidence))
        active["status_counts"] = {"completed": 36, "running": 1}
        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "complete dynamic business-row set",
        ):
            maintenance._validate_archive_evidence(active)

    def _prepare_0012_transaction_guard_fixture(self):
        maintenance = release_controller.PolytaoContractMaintenance(
            self.root / "guard-production",
            self.build_v2(),
            "contract-0012-guard-fixture",
            False,
        )
        policy_source = REPOSITORY_ROOT / "backend" / "migrations" / "postgres"
        policy_target = (
            maintenance.controller.candidate_dir
            / "backend"
            / "migrations"
            / "postgres"
        )
        policy_target.parent.mkdir(parents=True)
        shutil.copytree(policy_source, policy_target)
        ledger = maintenance._canonical_contract_ledger_prefix(
            include_contract=False
        )
        evidence = {
            "schema_version": 2,
            "row_count": 9,
            "status_counts": {
                "completed": 7,
                "failed": 2,
            },
            "rows_sha256": "1" * 64,
            "schema_sha256": "2" * 64,
            "structure_counts": {
                "columns": 8,
                "indexes": 2,
                "constraints": 3,
                "triggers": 1,
            },
        }
        maintenance.audit_dir.mkdir(parents=True)
        os.chmod(maintenance.audit_dir, 0o700)
        release_controller.atomic_json(
            maintenance.audit_dir / "legacy-table-evidence.json",
            evidence,
        )
        audit_manifest = maintenance._audit_manifest()
        marker = {
            "schema_version": 1,
            "operation_id": maintenance.operation_id,
            "source_sha": maintenance.document["source_sha"],
            "phase": "backed-up",
            "database_transaction_intent": False,
            "database_change_started": False,
        }
        control = {
            "control_key": "production",
            "drain_enabled": True,
            "reason": f"0012 maintenance {maintenance.operation_id}",
            "release_sha": maintenance.document["source_sha"],
            "activated_by": "pull-contract-0012",
        }
        pre_snapshot = {
            "schema_version": 1,
            "database": "nexpoly",
            "system_identifier": "7429518609726683136",
            "generation_namespace_oid": 16401,
            "relation": {
                "namespace_oid": 16401,
                "relation_oid": 16427,
                "relkind": "r",
            },
            "ledger": ledger,
            "deployment_control": control,
            "active_jobs": {
                relation: 0
                for relation in maintenance.GUARDED_JOB_RELATIONS
            },
        }
        with mock.patch.object(
            maintenance,
            "_capture_json",
            return_value=pre_snapshot,
        ):
            guard, guard_json, guard_digest = (
                maintenance._prepare_contract_transaction_guard(
                    {},
                    marker,
                    evidence,
                    {"ledger": ledger},
                    audit_manifest,
                )
            )
        return (
            maintenance,
            marker,
            guard,
            guard_json,
            guard_digest,
            pre_snapshot,
            evidence,
        )

    def test_0012_transaction_guard_is_canonical_and_cross_binds_preconditions(
        self,
    ) -> None:
        (
            maintenance,
            marker,
            guard,
            guard_json,
            guard_digest,
            _pre_snapshot,
            evidence,
        ) = self._prepare_0012_transaction_guard_fixture()
        precondition_path = (
            maintenance.audit_dir / "transaction-guard-marker-precondition.json"
        )
        pre_manifest_path = (
            maintenance.audit_dir / "PRE-TRANSACTION-AUDIT-MANIFEST.json"
        )
        guard_path = maintenance.audit_dir / "transaction-guard.json"

        self.assertEqual(
            guard_json,
            maintenance._canonical_contract_guard_json(guard),
        )
        self.assertNotIn("\n", guard_json)
        self.assertEqual(
            guard_digest,
            release_controller.sha256_bytes(guard_json.encode("utf-8")),
        )
        self.assertEqual(guard_path.read_text(encoding="utf-8"), guard_json)
        self.assertEqual(
            guard["archive_evidence_sha256"],
            release_controller.canonical_json_digest(evidence),
        )
        self.assertEqual(
            guard["maintenance"],
            {
                "operation_id": maintenance.operation_id,
                "marker_sha256": release_controller.sha256_file(
                    precondition_path
                ),
                "audit_manifest_sha256": release_controller.sha256_file(
                    pre_manifest_path
                ),
            },
        )
        precondition = release_controller.load_manifest(precondition_path)
        self.assertEqual(
            precondition["transaction_guard_audit_manifest_path"],
            str(pre_manifest_path),
        )
        self.assertEqual(
            precondition["transaction_guard_audit_manifest_sha256"],
            guard["maintenance"]["audit_manifest_sha256"],
        )
        self.assertEqual(
            maintenance._load_contract_transaction_guard(marker),
            (guard, guard_json, guard_digest),
        )

        original_precondition = precondition_path.read_bytes()
        original_pre_manifest = pre_manifest_path.read_bytes()
        try:
            # Even if an attacker re-seals both the marker evidence and the
            # live marker digest, the immutable guard still binds the original.
            changed_precondition = release_controller.load_manifest(
                precondition_path
            )
            changed_precondition["unbound_field"] = "tampered"
            release_controller.atomic_json(
                precondition_path,
                changed_precondition,
            )
            changed_marker = json.loads(json.dumps(marker))
            changed_marker["transaction_guard_marker_sha256"] = (
                release_controller.sha256_file(precondition_path)
            )
            with self.assertRaisesRegex(
                release_controller.ReleaseError,
                "transaction guard identity differs",
            ):
                maintenance._load_contract_transaction_guard(changed_marker)
        finally:
            precondition_path.write_bytes(original_precondition)
            os.chmod(precondition_path, 0o600)

        try:
            # Re-sealing a different, individually valid pre-audit manifest
            # and updating its nested marker reference is rejected by the
            # guard's original pair of cross-bound digests.
            changed_manifest = release_controller.load_manifest(pre_manifest_path)
            changed_manifest["files"] = []
            release_controller.atomic_json(pre_manifest_path, changed_manifest)
            changed_precondition = release_controller.load_manifest(
                precondition_path
            )
            changed_precondition[
                "transaction_guard_audit_manifest_sha256"
            ] = release_controller.sha256_file(pre_manifest_path)
            release_controller.atomic_json(
                precondition_path,
                changed_precondition,
            )
            changed_marker = json.loads(json.dumps(marker))
            changed_marker["transaction_guard_audit_manifest_sha256"] = (
                release_controller.sha256_file(pre_manifest_path)
            )
            changed_marker["transaction_guard_marker_sha256"] = (
                release_controller.sha256_file(precondition_path)
            )
            with self.assertRaisesRegex(
                release_controller.ReleaseError,
                "transaction guard identity differs",
            ):
                maintenance._load_contract_transaction_guard(changed_marker)
        finally:
            pre_manifest_path.write_bytes(original_pre_manifest)
            os.chmod(pre_manifest_path, 0o600)
            precondition_path.write_bytes(original_precondition)
            os.chmod(precondition_path, 0o600)

        original_guard = guard_path.read_bytes()
        try:
            release_controller.atomic_json(guard_path, guard)
            changed_marker = json.loads(json.dumps(marker))
            changed_marker["transaction_guard_sha256"] = (
                release_controller.sha256_file(guard_path)
            )
            with self.assertRaisesRegex(
                release_controller.ReleaseError,
                "not exact canonical JSON",
            ):
                maintenance._load_contract_transaction_guard(changed_marker)
        finally:
            guard_path.write_bytes(original_guard)
            os.chmod(guard_path, 0o600)

    def test_0012_transaction_guard_classifies_only_exact_database_endpoints(
        self,
    ) -> None:
        (
            maintenance,
            marker,
            guard,
            _guard_json,
            _guard_digest,
            pre_snapshot,
            evidence,
        ) = self._prepare_0012_transaction_guard_fixture()
        with mock.patch.object(
            maintenance,
            "_capture_json",
            side_effect=[pre_snapshot, evidence],
        ):
            self.assertEqual(
                maintenance._classify_contract_database_endpoint({}, marker),
                "exact-pre",
            )

        post_snapshot = json.loads(json.dumps(pre_snapshot))
        post_snapshot["ledger"] = [
            *guard["ledger"],
            {
                "version": release_controller.POLYTAO_SCHEMA_COMPATIBILITY_FLOOR,
                "checksum": release_controller.POLYTAO_CONTRACT_CHECKSUM,
            },
        ]
        post_snapshot["generation_namespace_oid"] = None
        post_snapshot["relation"] = None
        with mock.patch.object(
            maintenance,
            "_capture_json",
            return_value=post_snapshot,
        ):
            self.assertEqual(
                maintenance._classify_contract_database_endpoint({}, marker),
                "exact-post",
            )

        mixed_snapshot = json.loads(json.dumps(post_snapshot))
        mixed_snapshot["generation_namespace_oid"] = pre_snapshot[
            "generation_namespace_oid"
        ]
        mixed_snapshot["relation"] = pre_snapshot["relation"]
        with (
            mock.patch.object(
                maintenance,
                "_capture_json",
                return_value=mixed_snapshot,
            ),
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "retained the generation schema",
            ),
        ):
            maintenance._classify_contract_database_endpoint({}, marker)

        changed_archive = json.loads(json.dumps(evidence))
        changed_archive["rows_sha256"] = "9" * 64
        with (
            mock.patch.object(
                maintenance,
                "_capture_json",
                side_effect=[pre_snapshot, changed_archive],
            ),
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "pre-contract database endpoint differs",
            ),
        ):
            maintenance._classify_contract_database_endpoint({}, marker)

    def test_0012_post_completion_reuses_durable_approval_timestamp(self) -> None:
        maintenance = release_controller.PolytaoContractMaintenance(
            self.root / "approval-production",
            self.build_v2(),
            "contract-0012-approval-fixture",
            False,
        )
        marker = {
            "schema_version": 1,
            "operation_id": maintenance.operation_id,
            "source_sha": maintenance.document["source_sha"],
            "database_transaction_intent": True,
        }
        previous_state = {
            "status": "success",
            "source_sha": maintenance.document["source_sha"],
            "migrations": [release_controller.POLYTAO_CONTRACT_PREVIOUS_VERSION],
            "approved_contracts": [],
        }
        approved_at = "2026-07-19T00:00:00+00:00"
        with (
            mock.patch.object(
                release_controller,
                "utc_now",
                return_value=approved_at,
            ),
            mock.patch.object(
                maintenance.controller,
                "finalize_contract_0012_external_audit",
                side_effect=release_controller.ReleaseError(
                    "external audit response lost"
                ),
            ),
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "external audit response lost",
            ),
        ):
            maintenance._complete_contract_post_state(
                marker,
                {},
                self.root / "current-release",
                previous_state,
                worker_was_drained=False,
            )

        durable_marker = release_controller.load_manifest(maintenance.marker_path)
        approval = durable_marker["contract_approval"]
        self.assertEqual(approval["approved_at"], approved_at)
        self.assertEqual(
            durable_marker["contract_approval_sha256"],
            release_controller.canonical_json_digest(approval),
        )

        with (
            mock.patch.object(
                release_controller,
                "utc_now",
                side_effect=AssertionError(
                    "retry must not mint a new approval timestamp"
                ),
            ) as now,
            mock.patch.object(
                maintenance.controller,
                "finalize_contract_0012_external_audit",
                side_effect=release_controller.ReleaseError(
                    "external audit still unavailable"
                ),
            ),
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "external audit still unavailable",
            ),
        ):
            maintenance._complete_contract_post_state(
                durable_marker,
                {},
                self.root / "current-release",
                previous_state,
                worker_was_drained=False,
            )
        now.assert_not_called()
        retried_marker = release_controller.load_manifest(maintenance.marker_path)
        self.assertEqual(retried_marker["contract_approval"], approval)
        self.assertEqual(
            retried_marker["contract_approval_sha256"],
            release_controller.canonical_json_digest(approval),
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
            "NEXPOLY_CONTRACT_0012_MEDIA_AUTHORITY_RULES_SHA256": (
                "sha256:" + "8" * 64
            ),
        }
        backup_digest = "sha256:" + "b" * 64
        volume_id = "docker-volume:nexpoly_app_postgres_data"
        media_id = "postgres-backup:/private/backups/nexpoly.dump"
        media_ids = [volume_id, media_id]
        external_inventory = {
            "schema_version": 2,
            "inventory_complete": True,
            "writable_target": {
                "stack": "production",
                "database": "nexpoly",
            },
            "media_registry": {
                "schema_version": 1,
                "sha256": "sha256:" + "c" * 64,
                "captured_at": "2026-07-17T12:00:00Z",
                "expected_media_ids": media_ids,
                "discovered_media_ids": media_ids,
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
            "media": [
                {
                    "media_id": volume_id,
                    "kind": "docker_volume",
                    "database": "nexpoly",
                    "source_identity_before": {
                        "name": "nexpoly_app_postgres_data",
                        "driver": "local",
                        "mountpoint": (
                            "/var/lib/docker/volumes/"
                            "nexpoly_app_postgres_data/_data"
                        ),
                        "labels_sha256": "sha256:" + "1" * 64,
                        "inspect_sha256": "sha256:" + "2" * 64,
                        "attached_container_ids": ["3" * 64],
                    },
                    "source_identity_after": {
                        "name": "nexpoly_app_postgres_data",
                        "driver": "local",
                        "mountpoint": (
                            "/var/lib/docker/volumes/"
                            "nexpoly_app_postgres_data/_data"
                        ),
                        "labels_sha256": "sha256:" + "1" * 64,
                        "inspect_sha256": "sha256:" + "2" * 64,
                        "attached_container_ids": ["3" * 64],
                    },
                    "source_content_sha256": "sha256:" + "4" * 64,
                    "audit": {
                        "method": "live-read-only",
                        "complete": True,
                        "evidence_sha256": "sha256:" + "5" * 64,
                        "auditor_sha256": "sha256:" + "6" * 64,
                        "postgres_major": 16,
                        "audited_at": "2026-07-17T12:00:00Z",
                    },
                    "ledger": through_0008,
                    "ledger_analysis": {
                        "status": "canonical",
                        "checksum_mismatches": [],
                    },
                    "legacy_relation_present": True,
                    "migration_0013": {"state": "absent", "checksum": None},
                    "disposition": "writable-target",
                },
                {
                    "media_id": media_id,
                    "kind": "postgres_backup",
                    "database": "nexpoly",
                    "source_identity_before": {
                        "path": "/private/backups/nexpoly.dump",
                        "device": 1,
                        "inode": 2,
                        "size_bytes": 3,
                        "mtime_ns": 4,
                        "mode": 0o600,
                        "uid": os.geteuid(),
                        "sha256": backup_digest,
                    },
                    "source_identity_after": {
                        "path": "/private/backups/nexpoly.dump",
                        "device": 1,
                        "inode": 2,
                        "size_bytes": 3,
                        "mtime_ns": 4,
                        "mode": 0o600,
                        "uid": os.geteuid(),
                        "sha256": backup_digest,
                    },
                    "source_content_sha256": backup_digest,
                    "audit": {
                        "method": "isolated-backup-restore-read-only",
                        "complete": True,
                        "evidence_sha256": "sha256:" + "d" * 64,
                        "auditor_sha256": "sha256:" + "e" * 64,
                        "postgres_major": 16,
                        "audited_at": "2026-07-17T12:00:00Z",
                    },
                    "ledger": through_0008,
                    "ledger_analysis": {
                        "status": "canonical",
                        "checksum_mismatches": [],
                    },
                    "legacy_relation_present": True,
                    "migration_0013": {"state": "absent", "checksum": None},
                    "disposition": "retained-private-isolated",
                }
            ],
            "requires_0014": False,
        }
        external_inventory = external_inventory_fixture(
            dev_ledger=after_contract,
            health_ledger=through_0008,
            registry_digest="sha256:" + "c" * 64,
            dev_user="nexpoly_dev_auditor",
            health_user="nexpoly_health_auditor",
        )
        self.assertEqual(
            maintenance._validate_external_database_inventory(
                external_inventory,
                external_environment,
            ),
            external_inventory,
        )
        missing_stack = json.loads(json.dumps(external_inventory))
        missing_stack["databases"].pop()
        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "external database inventory is unsafe",
        ):
            maintenance._validate_external_database_inventory(
                missing_stack,
                external_environment,
            )
        writable_stack = json.loads(json.dumps(external_inventory))
        writable_stack["databases"][0]["transaction_read_only"] = False
        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "external database inventory is unsafe",
        ):
            maintenance._validate_external_database_inventory(
                writable_stack,
                external_environment,
            )
        wrong_writable_target = json.loads(json.dumps(external_inventory))
        wrong_writable_target["writable_target"] = {
            "stack": "nexpoly_dev",
            "database": "nexpoly_dev",
        }
        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "external database inventory is unsafe",
        ):
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
        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "external database inventory is unsafe",
        ):
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
                "endpoint is uncertain",
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
        maintenance._seal_current_state_precondition(marker, previous_state)
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
        maintenance._seal_current_state_precondition(marker, previous_state)
        marker["current_state_postcondition"] = committed_state
        marker["current_state_postcondition_sha256"] = (
            release_controller.canonical_json_digest(committed_state)
        )
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
        # Recovery now stops nginx, temporarily resumes the internal API for
        # the governed canary, re-drains it, and only then restores nginx.
        self.assertEqual(run.call_count, 5)
        self.assertTrue(
            all(call == mock.call(["compose"], env={}) for call in run.call_args_list)
        )
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
        maintenance._seal_current_state_precondition(marker, previous_state)
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

    def test_0012_state_commit_response_loss_accepts_only_sealed_postcondition(
        self,
    ) -> None:
        operation_id = "contract-0012-fixture"
        maintenance = release_controller.PolytaoContractMaintenance(
            self.root / "production",
            self.build_v2(),
            operation_id,
            False,
        )
        approval = {
            "version": release_controller.POLYTAO_SCHEMA_COMPATIBILITY_FLOOR,
            "checksum": release_controller.POLYTAO_CONTRACT_CHECKSUM,
            "operation_id": operation_id,
            "approved_at": "2026-07-18T00:00:00+00:00",
        }
        previous_state = {
            "status": "success",
            "source_sha": SHA,
            "migrations": [release_controller.POLYTAO_CONTRACT_PREVIOUS_VERSION],
            "approved_contracts": [],
        }
        postcondition = {
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
        release_controller.atomic_json(maintenance.state_path, previous_state)
        marker = {
            "operation_id": operation_id,
            "source_sha": SHA,
            "previous_state": previous_state,
            "phase": "state-commit-started",
        }
        maintenance._seal_current_state_precondition(marker, previous_state)
        maintenance._seal_current_state_postcondition(marker, postcondition)
        release_controller.atomic_json(maintenance.marker_path, marker)

        real_atomic_json = release_controller.atomic_json
        response_lost = False

        def write_then_lose_response(path, document, mode=0o600):  # type: ignore[no-untyped-def]
            nonlocal response_lost
            real_atomic_json(path, document, mode)
            if path == maintenance.state_path and not response_lost:
                response_lost = True
                raise OSError("injected response loss after state replace")

        with (
            mock.patch.object(
                release_controller,
                "atomic_json",
                side_effect=write_then_lose_response,
            ),
            self.assertRaisesRegex(OSError, "response loss"),
        ):
            maintenance._write_current_state(postcondition)

        self.assertEqual(
            release_controller.load_manifest(maintenance.state_path),
            postcondition,
        )
        # Recovery/retry treats only the exact sealed candidate as a lost
        # successful response and does not replace it again.
        with mock.patch.object(release_controller, "atomic_json") as rewrite:
            maintenance._write_current_state(postcondition)
        rewrite.assert_not_called()

    def test_0012_restore_rejects_foreign_valid_state_before_database_restore(
        self,
    ) -> None:
        operation_id = "contract-0012-fixture"
        maintenance = release_controller.PolytaoContractMaintenance(
            self.root / "production",
            self.build_v2(),
            operation_id,
            False,
        )
        previous_state = {
            "status": "success",
            "source_sha": SHA,
            "migrations": [release_controller.POLYTAO_CONTRACT_PREVIOUS_VERSION],
            "approved_contracts": [],
            "deployed_at": "2026-07-18T00:00:00+00:00",
        }
        postcondition = {
            **previous_state,
            "deployed_at": "2026-07-18T00:01:00+00:00",
        }
        foreign_state = {
            **previous_state,
            "deployed_at": "2026-07-18T00:02:00+00:00",
        }
        release_controller.atomic_json(maintenance.state_path, foreign_state)
        marker = {
            "operation_id": operation_id,
            "source_sha": SHA,
            "previous_state": previous_state,
        }
        maintenance._seal_current_state_precondition(marker, previous_state)
        marker["current_state_postcondition"] = postcondition
        marker["current_state_postcondition_sha256"] = (
            release_controller.canonical_json_digest(postcondition)
        )
        release_controller.atomic_json(maintenance.marker_path, marker)

        with (
            mock.patch.object(
                maintenance,
                "_pre_destructive_database_gate",
            ) as database_gate,
            mock.patch.object(
                maintenance.controller,
                "restore_database",
            ) as restore_database,
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "automatic 0012 full-database restore is disabled",
            ),
        ):
            maintenance._restore_previous_database({}, previous_state)

        database_gate.assert_not_called()
        restore_database.assert_not_called()
        self.assertEqual(
            release_controller.load_manifest(maintenance.state_path),
            foreign_state,
        )

    def test_0012_state_restore_retry_accepts_exact_precondition_without_rewrite(
        self,
    ) -> None:
        operation_id = "contract-0012-fixture"
        maintenance = release_controller.PolytaoContractMaintenance(
            self.root / "production",
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
        postcondition = {
            **previous_state,
            "migrations": [
                release_controller.POLYTAO_CONTRACT_PREVIOUS_VERSION,
                release_controller.POLYTAO_SCHEMA_COMPATIBILITY_FLOOR,
            ],
        }
        release_controller.atomic_json(maintenance.state_path, previous_state)
        marker = {
            "operation_id": operation_id,
            "source_sha": SHA,
            "previous_state": previous_state,
        }
        maintenance._seal_current_state_precondition(marker, previous_state)
        marker["current_state_postcondition"] = postcondition
        marker["current_state_postcondition_sha256"] = (
            release_controller.canonical_json_digest(postcondition)
        )
        release_controller.atomic_json(maintenance.marker_path, marker)

        with mock.patch.object(release_controller, "atomic_json") as rewrite:
            maintenance._restore_current_state(previous_state)
        rewrite.assert_not_called()
        self.assertEqual(
            release_controller.load_manifest(maintenance.state_path),
            previous_state,
        )





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
            self.prepare_mock_ready_release(controller)

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
            self.prepare_mock_ready_release(controller)

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

        patched["rebuild_datasets"].assert_not_called()
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
        self.assertTrue(controller.release_dir.is_dir())
        self.assertFalse(controller.staging.exists())



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

    def test_bootstrap_rollback_detaches_but_retains_its_ready_release(self) -> None:
        manifest = self.build()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            True,
        )
        self.prepare_mock_ready_release(controller)
        controller.ops.mkdir(exist_ok=True)
        current = controller.ops / "current"
        current.symlink_to(controller.release_dir.relative_to(controller.ops))

        controller.clear_failed_bootstrap_release()
        self.assertFalse(current.exists())
        self.assertFalse(current.is_symlink())
        self.assertTrue(controller.release_dir.is_dir())

        other = controller.ops / "releases" / ("b" * 40)
        other.mkdir()
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
        release_controller.atomic_json(
            controller.staging / release_controller.PROVISIONING_OWNER_NAME,
            {
                "schema_version": release_controller.PROVISIONING_SCHEMA_VERSION,
                "source_sha": controller.sha,
                "release_manifest_sha256": release_controller.sha256_file(manifest),
                "release_bundle_sha256": controller.document["release_bundle"]["sha256"],
                "owner_token": "1" * 64,
            },
        )

        controller.cleanup_unrecorded_staging()
        self.assertFalse(controller.staging.exists())

        outside = self.root / "outside-staging"
        outside.mkdir()
        controller.staging.symlink_to(outside)
        with self.assertRaisesRegex(release_controller.ReleaseError, "unsafe"):
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

    def test_interrupted_recovery_rejects_staging_without_any_candidate_action(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            True,
        )
        controller.staging.mkdir(parents=True)
        marker = {
            "source_sha": SHA,
            "phase": "prepared",
            "previous_state": {"status": "success", "source_sha": "1" * 40},
            "bootstrap": False,
            "provisioning_ready_sha256": "sha256:" + "1" * 64,
        }
        with (
            mock.patch.object(controller, "environment", return_value={}),
            mock.patch.object(controller, "_validate_provisioned_ready") as validate,
            mock.patch.object(controller, "run") as run,
            mock.patch.object(controller, "drain") as drain,
            self.assertRaisesRegex(release_controller.ReleaseError, "staging"),
        ):
            controller.recover_interrupted_deployment(marker)
        validate.assert_not_called()
        run.assert_not_called()
        drain.assert_not_called()

    def test_interrupted_recovery_rejects_tampered_ready_before_candidate_action(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            True,
        )
        marker = {
            "source_sha": SHA,
            "phase": "prepared",
            "previous_state": {"status": "success", "source_sha": "1" * 40},
            "bootstrap": False,
        }
        self.seal_mock_interrupted_release(controller, marker)
        ready = controller.release_dir / release_controller.PROVISIONING_READY_NAME
        ready.write_text('{"status":"changed"}\n', encoding="utf-8")
        os.chmod(ready, 0o600)
        with (
            mock.patch.object(controller, "environment", return_value={}),
            mock.patch.object(controller, "_validate_provisioned_ready") as validate,
            mock.patch.object(controller, "run") as run,
            mock.patch.object(controller, "drain") as drain,
            self.assertRaisesRegex(release_controller.ReleaseError, "digest does not match"),
        ):
            controller.recover_interrupted_deployment(marker)
        validate.assert_not_called()
        run.assert_not_called()
        drain.assert_not_called()

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
        self.seal_mock_interrupted_release(controller, marker)
        controller.in_progress_path.write_text(json.dumps(marker), encoding="utf-8")

        with (
            mock.patch.object(controller, "environment", return_value={}),
            mock.patch.object(controller, "_validate_provisioned_ready") as validate_ready,
            mock.patch.object(controller, "validate_current_runtime") as validate_runtime,
            mock.patch.object(controller, "drain") as drain,
            mock.patch.object(controller, "restore_database") as restore_database,
            mock.patch.object(controller, "rollback_runtime") as rollback_runtime,
        ):
            controller.recover_interrupted_deployment(marker)

        validate_ready.assert_called_once_with({}, require_bundle_artifact=False)
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
        marker = {
            "source_sha": SHA,
            "phase": "prepared",
            "previous_state": {},
            "bootstrap": True,
            "drain_attempted": True,
        }
        self.seal_mock_interrupted_release(controller, marker)
        controller.in_progress_path.parent.mkdir(parents=True, exist_ok=True)
        controller.in_progress_path.write_text(json.dumps(marker), encoding="utf-8")
        events: list[str] = []

        with (
            mock.patch.object(controller, "environment", return_value={}),
            mock.patch.object(controller, "_validate_provisioned_ready"),
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
        self.seal_mock_interrupted_release(controller, marker)
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
            mock.patch.object(controller, "_validate_provisioned_ready"),
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

    def test_interrupted_asset_pointer_switch_restores_pointer_not_database(self) -> None:
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
        old_assets = self.root / "old-assets"
        old_digest = "sha256:" + "f" * 64
        old_commit = "2" * 40
        marker = {
            "source_sha": SHA,
            "phase": "db-changed",
            "previous_state": {"status": "success", "source_sha": previous_sha},
            "bootstrap": False,
            "database_change_started": False,
            "data_change_started": False,
            "asset_switch_started": True,
            "runtime_switch_started": False,
            "previous_asset_root": str(old_assets),
            "previous_asset_digest": old_digest,
        }
        self.seal_mock_interrupted_release(controller, marker)
        controller.in_progress_path.parent.mkdir(parents=True, exist_ok=True)
        controller.in_progress_path.write_text(json.dumps(marker), encoding="utf-8")
        environment = {"NEXPOLY_ASSET_MANIFEST_DIGEST": DIGEST}

        with (
            mock.patch.object(controller, "environment", return_value=environment),
            mock.patch.object(controller, "_validate_provisioned_ready"),
            mock.patch.object(
                release_controller,
                "inspect_asset_release",
                return_value=(old_assets, old_digest, old_commit),
            ),
            mock.patch.object(controller, "switch_asset_pointer") as switch_assets,
            mock.patch.object(controller, "restore_database") as restore_database,
            mock.patch.object(controller, "rollback_runtime") as rollback_runtime,
            mock.patch.object(controller, "drain") as drain,
        ):
            controller.recover_interrupted_deployment(marker)

        switch_assets.assert_called_once_with(old_assets)
        restore_database.assert_not_called()
        rollback_runtime.assert_not_called()
        drain.assert_called_once_with(environment, False)
        self.assertEqual(environment["NEXPOLY_ASSET_MANIFEST_DIGEST"], old_digest)

    def test_prepared_interrupted_deploy_retains_ready_release_for_same_sha_retry(self) -> None:
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
        self.seal_mock_interrupted_release(controller, marker)
        controller.in_progress_path.parent.mkdir(parents=True, exist_ok=True)
        controller.in_progress_path.write_text(json.dumps(marker), encoding="utf-8")

        with (
            mock.patch.object(controller, "environment", return_value={}),
            mock.patch.object(controller, "_validate_provisioned_ready"),
            mock.patch.object(controller, "resume_worker") as resume_worker,
            mock.patch.object(controller, "drain") as drain,
        ):
            controller.recover_interrupted_deployment(marker)

        resume_worker.assert_called_once_with({})
        drain.assert_called_once_with({}, False)
        self.assertFalse(controller.staging.exists())
        self.assertTrue(controller.release_dir.is_dir())
        self.assertFalse(controller.in_progress_path.exists())






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
        opener = mock.Mock()
        opener.open.side_effect = [
            Response(html, "text/html"),
            Response(b"console.log('ok')", "application/javascript"),
        ]
        with mock.patch.object(
            release_controller.urllib.request,
            "build_opener",
            return_value=opener,
        ) as build_opener:
            controller.public_web_static_smoke(
                {"NEXPOLY_WEB_BASE_URL": "http://127.0.0.1:65535"}
            )

        self.assertEqual(
            [call.args[0] for call in opener.open.call_args_list],
            [
                "http://127.0.0.1:9000/",
                "http://127.0.0.1:9000/assets/app-123.js",
            ],
        )
        self.assertEqual(build_opener.call_args.args[0].proxies, {})
        self.assertIsInstance(
            build_opener.call_args.args[1],
            release_controller._NoRedirectHandler,
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
        self.assertIn("--operation-id", smoke_command)
        self.assertIn(f"release-smoke-{SHA}", smoke_command)
        self.assertIn("--source-sha", smoke_command)
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
        (controller.release_dir / "wheelhouse").mkdir(parents=True)
        lock = controller.release_dir / "workers" / "monomer_md_worker" / "requirements.lock"
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
            "PIP_CONFIG_FILE": str(self.root / "malicious-pip.conf"),
            "PIP_PREFIX": str(self.root / "frozen-prefix"),
            "PIP_TARGET": str(self.root / "frozen-site-packages"),
            "PIP_USER": "1",
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
            mock.patch.object(controller, "verify_worker_venv") as verify_venv,
        ):
            controller.prepare_worker(environment)
        self.assertEqual(run.call_count, 2)
        create_command = run.call_args_list[0].args[0]
        self.assertIn("-I", create_command)
        install_command = run.call_args_list[1].args[0]
        self.assertEqual(install_command[0], base_identity["resolved_path"])
        self.assertIn("--isolated", install_command)
        self.assertIn("--python", install_command)
        self.assertEqual(install_command.count("-r"), 1)
        self.assertIn("--ignore-installed", install_command)
        self.assertIn("--only-binary=:all:", install_command)
        self.assertEqual(verify_venv.call_count, 2)
        for call in run.call_args_list:
            self.assertNotEqual(call.args[0][0], str(controller.release_dir / "worker-venv" / "bin" / "python"))
        for call in run.call_args_list:
            child_environment = call.kwargs["env"]
            for unsafe_key in (
                "PIP_PREFIX",
                "PIP_TARGET",
                "PIP_USER",
            ):
                self.assertNotIn(unsafe_key, child_environment)
            self.assertEqual(child_environment["PIP_CONFIG_FILE"], os.devnull)
            build_home = Path(child_environment["HOME"])
            self.assertEqual(
                build_home.parent.parent,
                controller.ops / "state" / "worker-build-scratch",
            )
            self.assertFalse(build_home.exists())
        self.assertFalse((self.root / "frozen-site-packages").exists())
        self.assertEqual(inspect_base.call_count, 2)
        self.assertEqual(inspect_toolchain.call_count, 2)
        self.assertEqual(controller.worker_base_python_identity, base_identity)
        self.assertEqual(controller.worker_toolchain_identity, toolchain_identity)
        recorded_base = json.loads(
            (controller.release_dir / "worker-base-python-identity.json").read_text(encoding="utf-8")
        )
        recorded_toolchain = json.loads(
            (controller.release_dir / "worker-toolchain-identity.json").read_text(encoding="utf-8")
        )
        self.assertEqual(recorded_base, base_identity)
        self.assertEqual(recorded_toolchain, toolchain_identity)

    def test_deploy_path_contains_no_worker_provisioning_or_pip_install(self) -> None:
        source = inspect.getsource(release_controller.ReleaseController.deploy)

        self.assertNotIn("_provision_staging", source)
        self.assertNotIn("prepare_worker", source)
        self.assertNotIn("pip", source)
        self.assertIn("prepare_staging", source)

    def test_prepare_staging_only_consumes_exact_ready_evidence(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            False,
        )
        controller.release_dir.mkdir(parents=True)
        evidence = {
            "schema_version": release_controller.PROVISIONING_SCHEMA_VERSION,
            "status": "ready",
            "source_sha": controller.sha,
            "release_manifest_sha256": "sha256:" + "1" * 64,
            "release_bundle_sha256": controller.document["release_bundle"]["sha256"],
            "requirements_sha256": "sha256:" + "2" * 64,
            "wheelhouse_inventory_sha256": "sha256:" + "3" * 64,
            "payload_inventory_sha256": "sha256:" + "4" * 64,
            "venv_inventory_sha256": "sha256:" + "5" * 64,
            "venv_prefix": str(controller.release_dir / "worker-venv"),
            "worker_base_identity_sha256": "sha256:" + "6" * 64,
            "worker_toolchain_identity_sha256": "sha256:" + "7" * 64,
            "owner_token": "8" * 64,
        }
        release_controller.atomic_json(
            controller.release_dir / release_controller.PROVISIONING_READY_NAME,
            {**evidence, "provisioned_at": "2026-07-15T00:00:00+00:00"},
        )

        with (
            mock.patch.object(
                controller,
                "_provisioning_evidence",
                return_value=evidence,
            ) as validate,
            mock.patch.object(controller, "_provision_staging") as provision,
            mock.patch.object(controller, "prepare_worker") as install,
        ):
            controller.prepare_staging({})

        validate.assert_called_once_with(
            controller.release_dir,
            {},
            require_bundle_artifact=True,
            sealed_ready={**evidence, "provisioned_at": "2026-07-15T00:00:00+00:00"},
        )
        provision.assert_not_called()
        install.assert_not_called()
        self.assertEqual(controller.candidate_dir, controller.release_dir)

        ready_path = controller.release_dir / release_controller.PROVISIONING_READY_NAME
        tampered = json.loads(ready_path.read_text(encoding="utf-8"))
        tampered["venv_inventory_sha256"] = "sha256:" + "9" * 64
        release_controller.atomic_json(ready_path, tampered)
        with (
            mock.patch.object(
                controller,
                "_provisioning_evidence",
                return_value=evidence,
            ),
            self.assertRaisesRegex(release_controller.ReleaseError, "does not match"),
        ):
            controller.prepare_staging({})

    def test_prepare_staging_rejects_missing_ready_before_validation(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            False,
        )
        controller.release_dir.mkdir(parents=True)
        with (
            mock.patch.object(controller, "_provisioning_evidence") as validate,
            self.assertRaisesRegex(release_controller.ReleaseError, "READY"),
        ):
            controller.prepare_staging({})
        validate.assert_not_called()

    def test_provision_allows_only_matching_sealed_interrupted_retry(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            True,
        )
        marker: dict[str, object] = {
            "source_sha": SHA,
            "phase": "prepared",
            "bootstrap": False,
            "release_manifest_sha256": release_controller.sha256_file(
                controller.manifest_path
            ),
        }
        self.seal_mock_interrupted_release(controller, marker)
        controller.in_progress_path.parent.mkdir(parents=True, exist_ok=True)
        controller.in_progress_path.write_text(json.dumps(marker), encoding="utf-8")
        os.chmod(controller.in_progress_path, 0o600)
        with (
            mock.patch.object(controller, "ensure_root"),
            mock.patch.object(controller, "environment", return_value={}),
            mock.patch.object(controller, "prepare_staging") as validate_ready,
            mock.patch.object(
                controller,
                "assert_still_current_main",
                side_effect=release_controller.DeploymentSuperseded(
                    "main advanced after interruption"
                ),
            ) as freshness,
        ):
            result = controller.provision()
        self.assertEqual(result["status"], "interrupted-ready")
        validate_ready.assert_called_once_with({})
        freshness.assert_not_called()

        marker["source_sha"] = "1" * 40
        controller.in_progress_path.write_text(json.dumps(marker), encoding="utf-8")
        os.chmod(controller.in_progress_path, 0o600)
        with (
            mock.patch.object(controller, "ensure_root"),
            mock.patch.object(controller, "environment") as environment,
            self.assertRaisesRegex(release_controller.ReleaseError, "different unfinished"),
        ):
            controller.provision()
        environment.assert_not_called()

    def test_provisioned_directory_inventory_detects_file_tampering(self) -> None:
        tree = self.root / "inventory"
        tree.mkdir()
        payload = tree / "package.py"
        payload.write_text("value = 1\n", encoding="utf-8")
        before = release_controller.directory_inventory_digest(tree)

        payload.write_text("value = 2\n", encoding="utf-8")

        self.assertNotEqual(
            release_controller.directory_inventory_digest(tree),
            before,
        )

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
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            False,
        )
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
        pth_side_effect = self.root / "candidate-pth-executed"
        (purelib / "candidate-hook.pth").write_text(
            "import pathlib; pathlib.Path("
            + repr(str(pth_side_effect))
            + ").touch()\n",
            encoding="utf-8",
        )
        base_identity = {"resolved_path": str(Path(sys.executable).resolve())}
        controller.verify_worker_venv(
            venv,
            {
                "schema_version": 1,
                "requirements": [{"name": "fixture-pkg", "version": "1.0"}],
            },
            base_identity,
        )
        self.assertFalse(pth_side_effect.exists())
        with self.assertRaisesRegex(release_controller.ReleaseError, "local versions"):
            controller.verify_worker_venv(
                venv,
                {
                    "schema_version": 1,
                    "requirements": [
                        {"name": "pip", "version": importlib.metadata.version("pip")}
                    ],
                },
                base_identity,
            )

    @unittest.skipUnless(
        importlib.util.find_spec("uvicorn") is not None,
        "uvicorn is required for the real relocation smoke",
    )
    def test_final_release_venv_has_no_staging_paths_and_runs_entrypoints(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            False,
        )
        controller.release_dir.mkdir(parents=True)
        os.chmod(controller.release_dir, 0o700)
        venv = controller.release_dir / "worker-venv"
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "venv",
                "--system-site-packages",
                str(venv),
            ],
            check=True,
        )
        prefix = subprocess.run(
            [
                str(venv / "bin" / "python"),
                "-I",
                "-c",
                "import sys; print(sys.prefix)",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        self.assertEqual(Path(prefix).resolve(), venv.resolve())
        subprocess.run(
            [str(venv / "bin" / "pip"), "--version"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            [str(venv / "bin" / "python"), "-I", "-m", "uvicorn", "--help"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        staging_fragment = f"{SHA}.staging"
        self.assertNotIn(
            staging_fragment,
            (venv / "pyvenv.cfg").read_text(encoding="utf-8"),
        )
        for script in (venv / "bin").iterdir():
            if script.is_file() and not script.is_symlink():
                with script.open("rb") as source:
                    first_line = source.readline(16 * 1024)
                if first_line.startswith(b"#!"):
                    self.assertNotIn(staging_fragment.encode(), first_line)
        controller.verify_worker_venv(
            venv,
            {"schema_version": 1, "requirements": []},
            {"resolved_path": str(Path(sys.executable).resolve())},
        )

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
            self.prepare_mock_ready_release(controller)

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
        self.assertTrue(controller.release_dir.is_dir())
        self.assertFalse(controller.staging.exists())

    def test_lost_worker_drain_response_forces_resume_before_global_admission(
        self,
    ) -> None:
        controller = self.existing_release_controller()
        drain_calls: list[bool] = []

        def prepare(_environment: dict[str, str]) -> None:
            self.prepare_mock_ready_release(controller)

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
        self.assertTrue(controller.release_dir.is_dir())
        self.assertFalse(controller.staging.exists())

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
            self.prepare_mock_ready_release(controller)

        def run(command: list[str], **_kwargs: object) -> None:
            if "app.deployment_control_cli" not in command:
                return
            drain_commands.append(command)
            operation = command[command.index("app.deployment_control_cli") + 1]
            if (
                operation == "resume"
                and str(controller.release_dir / "docker-compose.yml") in command
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
        self.assertIn(str(controller.release_dir / "docker-compose.yml"), drain_commands[0])
        self.assertIn("resume", drain_commands[1])
        self.assertIn(str(previous_release / "docker-compose.yml"), drain_commands[1])
        self.assertNotIn(str(controller.release_dir / "docker-compose.yml"), drain_commands[1])
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
            self.prepare_mock_ready_release(controller)

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
                    side_effect=lambda _environment, **_kwargs: events.append(
                        "monomer"
                    ),
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
        self.assertEqual(freshness.call_count, 5)
        pull_command = next(command for command in run_commands if "pull" in command)
        backend_up = next(
            command
            for command in run_commands
            if "up" in command and command[-1] == "backend"
        )
        self.assertIn("lab-postgres", pull_command)
        self.assertNotIn("lab-postgres", backend_up)
        self.assertIn("--no-deps", backend_up)
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
            mock.patch.object(
                controller,
                "prepare_staging",
                side_effect=lambda _environment: self.prepare_mock_ready_release(
                    controller
                ),
            ),
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
        self.assertNotIn("lab-postgres", commands[1])
        self.assertIn("--no-deps", commands[1])
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
        worker_smoke.assert_called_once_with(
            mock.ANY,
            release=previous_release,
            operation_id=f"rollback-smoke-{SHA}-{previous_sha}",
        )
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
            self.prepare_mock_ready_release(controller)

        def create_backup(_environment: dict[str, str], _from_sha: str) -> None:
            controller.backup_path = backup

        with ExitStack() as stack:
            stack.enter_context(
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


    def test_asset_change_database_rebuild_entrypoint_fails_closed(self) -> None:
        manifest = self.build_single_bundle()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            False,
        )
        controller.document["datasets_on_asset_change"] = ["online"]
        with mock.patch.object(controller, "run") as run:
            with self.assertRaisesRegex(
                release_controller.ReleaseError,
                "asset-triggered database rebuilds are retired",
            ):
                controller.rebuild_datasets({})

        run.assert_not_called()

    def test_byteff2_only_asset_change_does_not_run_database_rebuild(self) -> None:
        manifest = self.build_single_bundle()
        controller = release_controller.ReleaseController(
            self.root / "production",
            manifest,
            "auto",
            False,
        )
        controller.document["datasets_on_asset_change"] = []

        with mock.patch.object(controller, "run") as run:
            controller.rebuild_datasets({})

        run.assert_not_called()

    def test_release_manifest_rejects_legacy_asset_database_rebuilds(self) -> None:
        manifest_path = self.build_single_bundle()
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        document["datasets_on_asset_change"] = ["core"]

        with self.assertRaisesRegex(
            release_controller.ReleaseError,
            "must not request asset-triggered database rebuilds",
        ):
            release_controller.validate_manifest(document)

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

    def test_legacy_release_cli_commands_are_retired_fail_closed(self) -> None:
        for command in (
            "build-manifest",
            "verify-manifest",
            "deploy",
            "provision-release",
            "maintain-contract-0012",
        ):
            result = subprocess.run(
                [sys.executable, str(CONTROLLER_PATH), command],
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 2, command)
            self.assertIn("legacy command is retired", result.stderr)
            self.assertIn("nexpoly-pull-deploy", result.stderr)

    def test_parser_exposes_no_legacy_release_command(self) -> None:
        choices = next(
            action.choices
            for action in release_controller.parser()._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        self.assertEqual(set(choices), {"worker-base-identity"})

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

    def test_worker_request_ignores_inherited_socket_override(self) -> None:
        controller = release_controller.ReleaseController(
            self.root,
            self.build(),
            "auto",
            False,
        )
        completed = subprocess.CompletedProcess(
            args=["curl"],
            returncode=0,
            stdout="{}",
            stderr="",
        )
        with mock.patch.object(
            release_controller.subprocess,
            "run",
            return_value=completed,
        ) as run:
            controller.worker_request(
                {"MONOMER_MD_WORKER_UDS": "/tmp/forged-worker.sock"},
                "GET",
                "/health",
            )

        command = run.call_args.args[0]
        socket_path = command[command.index("--unix-socket") + 1]
        self.assertEqual(
            socket_path,
            str(
                controller.ops
                / "state"
                / "monomer-md-worker-socket"
                / "worker.sock"
            ),
        )

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
            "protocols": {
                "Transport": {
                    "supported": True,
                    "runtime_ready": True,
                    "runtime_error": None,
                }
            },
        }
        controller.deploy_transport_required = True
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

    def test_transport_readiness_helpers_require_all_three_strict_fields(self) -> None:
        ready = {
            "protocols": {
                "Transport": {
                    "supported": True,
                    "runtime_ready": True,
                    "runtime_error": None,
                }
            }
        }
        self.assertTrue(release_controller.worker_transport_is_strict_ready(ready))
        catalog = {
            "protocols": [
                {
                    "protocol": "Transport",
                    "supported": True,
                    "runtime_ready": True,
                    "runtime_error": None,
                }
            ]
        }
        self.assertTrue(
            release_controller.protocol_catalog_transport_is_strict_ready(catalog)
        )
        preflight = {
            "schema_version": 1,
            "runtime_ready": True,
            "transport": {
                "supported": True,
                "runtime_ready": True,
                "runtime_error": None,
            },
        }
        self.assertTrue(
            release_controller.candidate_preflight_transport_is_strict_ready(preflight)
        )

        invalid_transports = (
            {"supported": True, "runtime_ready": True},
            {"supported": False, "runtime_ready": True, "runtime_error": None},
            {"supported": True, "runtime_ready": False, "runtime_error": None},
            {"supported": True, "runtime_ready": True, "runtime_error": "secret"},
        )
        for transport in invalid_transports:
            with self.subTest(transport=transport):
                self.assertFalse(
                    release_controller.worker_transport_is_strict_ready(
                        {"protocols": {"Transport": transport}}
                    )
                )
                self.assertFalse(
                    release_controller.protocol_catalog_transport_is_strict_ready(
                        {"protocols": [{"protocol": "Transport", **transport}]}
                    )
                )

    def test_current_worker_transport_repair_allows_only_isolated_degradation(self) -> None:
        isolated = {
            "status": "degraded",
            "runtime_ready": False,
            "active_jobs": 0,
            "db_configured": True,
            "byteff2_root_exists": True,
            "gpu_broker_enabled": True,
            "gpu_broker_ready": True,
            "protocols": {
                "Density": {
                    "supported": True,
                    "runtime_ready": True,
                    "runtime_error": None,
                },
                "Transport": {
                    "supported": True,
                    "runtime_ready": False,
                    "runtime_error": "redacted Transport dependency failure",
                },
            },
        }
        self.assertTrue(
            release_controller.current_worker_allows_transport_repair(isolated)
        )

        unsafe_variants = (
            {**isolated, "active_jobs": True},
            {**isolated, "runtime_ready": True},
            {**isolated, "db_configured": False},
            {**isolated, "byteff2_root_exists": False},
            {**isolated, "gpu_broker_ready": False},
            {
                **isolated,
                "protocols": {
                    **isolated["protocols"],
                    "Density": {
                        "supported": True,
                        "runtime_ready": False,
                        "runtime_error": "common runtime failure",
                    },
                },
            },
            {
                **isolated,
                "protocols": {
                    **isolated["protocols"],
                    "Transport": {
                        "supported": True,
                        "runtime_ready": False,
                        "runtime_error": None,
                    },
                },
            },
            {
                **isolated,
                "protocols": {
                    "Transport": isolated["protocols"]["Transport"],
                },
            },
        )
        for payload in unsafe_variants:
            with self.subTest(payload=payload):
                self.assertFalse(
                    release_controller.current_worker_allows_transport_repair(payload)
                )

    def test_local_json_fetch_disables_proxies_and_rejects_non_loopback(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            False,
        )

        class Response:
            status = 200

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, limit: int) -> bytes:
                self.limit = limit
                return b'{"ready":true}'

        response = Response()
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(
            release_controller.urllib.request,
            "build_opener",
            return_value=opener,
        ) as build_opener:
            payload = controller.fetch_local_json_object(
                "http://127.0.0.1:9000/health",
                label="local health",
            )
        self.assertEqual(payload, {"ready": True})
        proxy_handler = build_opener.call_args.args[0]
        self.assertEqual(proxy_handler.proxies, {})
        self.assertIsInstance(
            build_opener.call_args.args[1],
            release_controller._NoRedirectHandler,
        )
        opener.open.assert_called_once_with(
            "http://127.0.0.1:9000/health",
            timeout=30,
        )
        self.assertEqual(
            response.limit,
            release_controller.MAX_RUNTIME_RESPONSE_BYTES + 1,
        )

        with self.assertRaisesRegex(release_controller.ReleaseError, "loopback"):
            controller.fetch_local_json_object(
                "http://example.test/health",
                label="local health",
            )

    def test_stable_worker_environment_helper_must_match_candidate(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            False,
        )
        controller.config_dir.mkdir(parents=True)
        helper = controller.config_dir / "monomer_worker_env.py"
        shutil.copy2(REPOSITORY_ROOT / "scripts" / "monomer_worker_env.py", helper)
        os.chmod(helper, 0o700)
        controller.validate_stable_worker_env_helper()

        helper.write_text("# tampered\n", encoding="utf-8")
        os.chmod(helper, 0o700)
        with self.assertRaisesRegex(release_controller.ReleaseError, "differs"):
            controller.validate_stable_worker_env_helper()

    def test_broker_candidate_preflight_skips_direct_gpu_query_and_is_child_only(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            False,
        )
        controller.deploy_transport_required = True
        controller.release_dir.mkdir(parents=True)
        candidate_python = controller.release_dir / "worker-venv" / "bin" / "python"
        candidate_python.parent.mkdir(parents=True)
        candidate_python.write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(candidate_python, 0o700)
        module = (
            controller.release_dir
            / "workers"
            / "monomer_md_worker"
            / "app"
            / "runtime_preflight.py"
        )
        module.parent.mkdir(parents=True)
        module.write_text("# fixture\n", encoding="utf-8")
        asset_root = self.root / "target-assets"
        (asset_root / "byteff2").mkdir(parents=True)
        controller.document["resolved_asset_root"] = str(asset_root)
        controller.worker_values = {
            "BYTEFF2_PYTHON": "/opt/byteff2/bin/python",
            "BYTEFF2_ROOT": "/old/byteff2",
            "MONOMER_MD_PYTHON": "/old/venv/bin/python",
            "MONOMER_MD_GPU_BROKER_ENABLED": "TRUE",
            "MONOMER_MD_HEALTH_PROBE_TIMEOUT_SECONDS": "30",
            "PYTHONPATH": "/old/source",
        }
        payload = {
            "schema_version": 1,
            "runtime_ready": True,
            "transport": {
                "supported": True,
                "runtime_ready": True,
                "runtime_error": None,
            },
        }
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(payload).encode("utf-8"),
            stderr=b"",
        )
        parent_environment = {
            "MONOMER_MD_REQUIRE_TRANSPORT_READY": "unsafe-inherited",
            "LD_LIBRARY_PATH": "/unsafe",
            "NEXPOLY_POSTGRES_PASSWORD": "must-not-reach-candidate",
            "PI_POSTGRES_DSN": "postgresql://must-not-reach-candidate",
            "REGISTRY_TOKEN": "must-not-reach-candidate",
            "HOME": "/safe-home",
            "UNRELATED": "preserved",
        }
        with (
            mock.patch.object(
                controller,
                "assert_direct_transport_gpu_idle",
            ) as direct_idle,
            mock.patch.object(
                controller,
                "_run_candidate_preflight_process",
                return_value=completed,
            ) as run,
        ):
            summary = controller.run_candidate_runtime_preflight(parent_environment)

        direct_idle.assert_not_called()
        self.assertTrue(summary["broker_governed"])
        command = run.call_args.args[0]
        child_environment = run.call_args.args[1]
        self.assertEqual(run.call_args.kwargs["timeout_seconds"], 55.0)
        self.assertIs(run.call_args.kwargs["broker_governed"], True)
        self.assertEqual(
            command,
            [
                str(candidate_python),
                "-m",
                "workers.monomer_md_worker.app.runtime_preflight",
                "--require-transport-ready",
            ],
        )
        self.assertNotIn("MONOMER_MD_REQUIRE_TRANSPORT_READY", child_environment)
        self.assertNotIn("LD_LIBRARY_PATH", child_environment)
        self.assertEqual(child_environment["APP_POSTGRES_DSN"], "")
        preflight_root = Path(child_environment["MONOMER_MD_JOB_ROOT"])
        self.assertEqual(
            preflight_root.parent,
            controller.ops / "state" / "candidate-preflight",
        )
        self.assertFalse(preflight_root.exists())
        self.assertEqual(child_environment["HOME"], "/safe-home")
        for secret_key in (
            "NEXPOLY_POSTGRES_PASSWORD",
            "PI_POSTGRES_DSN",
            "REGISTRY_TOKEN",
            "UNRELATED",
        ):
            self.assertNotIn(secret_key, child_environment)
        self.assertEqual(
            child_environment["MONOMER_MD_PYTHON"],
            str(candidate_python),
        )
        self.assertEqual(parent_environment["LD_LIBRARY_PATH"], "/unsafe")

    def test_candidate_preflight_rejects_oversized_or_non_strict_payload_safely(self) -> None:
        secret = "DO_NOT_LOG_TRANSPORT_ERROR"
        with self.assertRaisesRegex(release_controller.ReleaseError, "64 KiB") as oversized:
            release_controller.decode_bounded_json_object(
                b"x" * (release_controller.MAX_RUNTIME_RESPONSE_BYTES + 1),
                "candidate preflight",
            )
        self.assertNotIn(secret, str(oversized.exception))
        self.assertFalse(
            release_controller.candidate_preflight_transport_is_strict_ready(
                {
                    "schema_version": 1,
                    "runtime_ready": True,
                    "transport": {
                        "supported": True,
                        "runtime_ready": False,
                        "runtime_error": secret,
                    },
                }
            )
        )

    def test_direct_candidate_preflight_checks_gpu_idle_before_and_after_probe(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            False,
        )
        controller.deploy_transport_required = True
        controller.release_dir.mkdir(parents=True)
        candidate_python = controller.release_dir / "worker-venv" / "bin" / "python"
        candidate_python.parent.mkdir(parents=True)
        candidate_python.write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(candidate_python, 0o700)
        module = (
            controller.release_dir
            / "workers"
            / "monomer_md_worker"
            / "app"
            / "runtime_preflight.py"
        )
        module.parent.mkdir(parents=True)
        module.write_text("# fixture\n", encoding="utf-8")
        asset_root = self.root / "direct-target-assets"
        (asset_root / "byteff2").mkdir(parents=True)
        controller.document["resolved_asset_root"] = str(asset_root)
        controller.worker_values = {
            "BYTEFF2_PYTHON": "/opt/byteff2/bin/python",
            "BYTEFF2_ROOT": "/old/byteff2",
            "MONOMER_MD_PYTHON": "/old/venv/bin/python",
            "MONOMER_MD_CUDA_VISIBLE_DEVICES": "1",
            "MONOMER_MD_GPU_BROKER_ENABLED": "false",
            "MONOMER_MD_HEALTH_PROBE_TIMEOUT_SECONDS": "30",
            "PYTHONPATH": "/old/source",
        }
        payload = {
            "schema_version": 1,
            "runtime_ready": True,
            "transport": {
                "supported": True,
                "runtime_ready": True,
                "runtime_error": None,
            },
        }
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(payload).encode("utf-8"),
            stderr=b"",
        )
        events: list[str] = []

        def idle(_environment: dict[str, str]) -> None:
            events.append("idle")

        def probe(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            events.append("probe")
            return completed

        with (
            mock.patch.object(
                controller,
                "assert_direct_transport_gpu_idle",
                side_effect=idle,
            ),
            mock.patch.object(
                controller,
                "_run_candidate_preflight_process",
                side_effect=probe,
            ),
        ):
            summary = controller.run_candidate_runtime_preflight({})
        self.assertFalse(summary["broker_governed"])
        self.assertEqual(events, ["idle", "probe", "idle"])

        with (
            mock.patch.object(
                controller,
                "assert_direct_transport_gpu_idle",
                side_effect=[
                    None,
                    release_controller.DeploymentDeferred(
                        "target GPU became busy"
                    ),
                ],
            ) as idle_gate,
            mock.patch.object(
                controller,
                "_run_candidate_preflight_process",
                return_value=completed,
            ),
            self.assertRaises(release_controller.DeploymentDeferred),
        ):
            controller.run_candidate_runtime_preflight({})
        self.assertEqual(idle_gate.call_count, 2)

    @unittest.skipUnless(Path("/proc/self/stat").is_file(), "Linux /proc is required")
    def test_candidate_preflight_process_returns_completed_result(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            False,
        )
        controller.release_dir.mkdir(parents=True)
        completed = controller._run_candidate_preflight_process(
            [sys.executable, "-c", "print('{\"ready\":true}')"],
            os.environ.copy(),
            timeout_seconds=2.0,
            broker_governed=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b'{"ready":true}\n')

    @unittest.skipUnless(Path("/proc/self/stat").is_file(), "Linux /proc is required")
    def test_candidate_preflight_captures_fast_double_fork_daemon(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            False,
        )
        controller.release_dir.mkdir(parents=True)
        pid_file = self.root / "escaped-grandchild.pid"
        daemon_script = self.root / "fast-daemon.py"
        daemon_script.write_text(
            """import os, signal, sys, time
time.sleep(0.03)
if os.fork() == 0:
    os.setsid()
    if os.fork() != 0:
        os._exit(0)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(sys.argv[1], "w", encoding="ascii") as stream:
        stream.write(str(os.getpid()))
        stream.flush()
        os.fsync(stream.fileno())
    while True:
        time.sleep(1)
os._exit(0)
""",
            encoding="utf-8",
        )
        escaped_pid: int | None = None
        try:
            with (
                mock.patch.object(
                    release_controller,
                    "CANDIDATE_PREFLIGHT_TERM_GRACE_SECONDS",
                    0.2,
                ),
                mock.patch.object(
                    release_controller,
                    "CANDIDATE_PREFLIGHT_KILL_WAIT_SECONDS",
                    2.0,
                ),
                self.assertRaisesRegex(
                    release_controller.ReleaseError,
                    "left a running descendant",
                ),
            ):
                controller._run_candidate_preflight_process(
                    [sys.executable, str(daemon_script), str(pid_file)],
                    os.environ.copy(),
                    timeout_seconds=2.0,
                    broker_governed=False,
                )
            if pid_file.exists():
                escaped_pid = int(pid_file.read_text(encoding="ascii"))
                identity = release_controller._read_process_identity(escaped_pid)
                self.assertTrue(identity is None or identity.state == "Z")
        finally:
            if escaped_pid is not None:
                try:
                    os.kill(escaped_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @unittest.skipUnless(Path("/proc/self/stat").is_file(), "Linux /proc is required")
    def test_candidate_preflight_pipe_enforces_output_limit_with_backpressure(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            False,
        )
        controller.release_dir.mkdir(parents=True)
        started = time.monotonic()
        with self.assertRaisesRegex(release_controller.ReleaseError, "64 KiB"):
            controller._run_candidate_preflight_process(
                [
                    sys.executable,
                    "-c",
                    "import os; os.write(1, b'x' * (16 * 1024 * 1024))",
                ],
                os.environ.copy(),
                timeout_seconds=2.0,
                broker_governed=False,
            )
        self.assertLess(time.monotonic() - started, 2.0)

    def test_candidate_signal_scope_defers_and_restores_controller_handlers(self) -> None:
        original = object()
        active: dict[signal.Signals, object] = {}

        def install(signal_number, handler) -> None:
            active[signal_number] = handler

        with (
            mock.patch.object(
                release_controller.signal,
                "getsignal",
                return_value=original,
            ),
            mock.patch.object(
                release_controller.signal,
                "signal",
                side_effect=install,
            ) as set_handler,
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "interrupted safely",
            ),
        ):
            with release_controller._deferred_candidate_signals() as pending:
                self.assertIsNone(pending())
                handler = active[signal.SIGTERM]
                self.assertTrue(callable(handler))
                handler(signal.SIGTERM, None)
                self.assertEqual(pending(), signal.SIGTERM)

        for signal_number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            self.assertIn(
                mock.call(signal_number, original),
                set_handler.call_args_list,
            )

    def test_candidate_signal_arriving_in_inner_finalizer_cannot_be_lost(self) -> None:
        active: dict[signal.Signals, object] = {}

        def install(signal_number, handler) -> None:
            active[signal_number] = handler

        @contextmanager
        def signal_on_exit():
            try:
                yield
            finally:
                handler = active[signal.SIGTERM]
                self.assertTrue(callable(handler))
                handler(signal.SIGTERM, None)

        with (
            mock.patch.object(
                release_controller.signal,
                "getsignal",
                return_value=signal.SIG_DFL,
            ),
            mock.patch.object(
                release_controller.signal,
                "signal",
                side_effect=install,
            ),
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "interrupted safely",
            ),
        ):
            with release_controller._deferred_candidate_signals():
                with signal_on_exit():
                    pass

    def test_candidate_proc_inventory_fails_closed_on_unreadable_or_malformed_data(
        self,
    ) -> None:
        with (
            mock.patch.object(
                Path,
                "read_text",
                side_effect=PermissionError("hidden proc"),
            ),
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "identity is unreadable",
            ),
        ):
            release_controller._read_process_identity(os.getpid())

        with (
            mock.patch.object(Path, "read_text", return_value="malformed"),
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "identity is malformed",
            ),
        ):
            release_controller._read_process_identity(os.getpid())

        for payload in ("not-a-pid", "-1", "123 123"):
            with (
                self.subTest(payload=payload),
                mock.patch.object(Path, "read_text", return_value=payload),
                self.assertRaisesRegex(
                    release_controller.ReleaseError,
                    "child inventory is malformed",
                ),
            ):
                release_controller._direct_process_children(os.getpid())

        with (
            mock.patch.object(
                Path,
                "read_text",
                side_effect=PermissionError("hidden proc"),
            ),
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "child inventory is unreadable",
            ),
        ):
            release_controller._direct_process_children(os.getpid())

        with (
            mock.patch.object(
                Path,
                "read_text",
                side_effect=FileNotFoundError,
            ),
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "child inventory is unavailable",
            ),
        ):
            release_controller._direct_process_children(os.getpid())

    @unittest.skipUnless(Path("/proc/self/stat").is_file(), "Linux /proc is required")
    def test_candidate_exec_gate_prevents_command_when_identity_binding_fails(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            False,
        )
        controller.release_dir.mkdir(parents=True)
        marker = self.root / "candidate-executed"
        with (
            mock.patch.object(
                release_controller,
                "_read_process_identity",
                side_effect=release_controller.ReleaseError(
                    "candidate process identity unavailable"
                ),
            ),
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "identity unavailable",
            ),
        ):
            controller._run_candidate_preflight_process(
                [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).touch()",
                ],
                os.environ.copy(),
                timeout_seconds=2.0,
                broker_governed=False,
            )
        self.assertFalse(marker.exists())

    def test_candidate_output_selector_failure_occurs_before_spawn(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            False,
        )
        controller.release_dir.mkdir(parents=True)
        selector = mock.Mock()
        selector.register.side_effect = OSError("selector exhausted")
        with (
            mock.patch.object(
                release_controller.selectors,
                "DefaultSelector",
                return_value=selector,
            ),
            mock.patch.object(release_controller.subprocess, "Popen") as spawn,
            self.assertRaisesRegex(OSError, "selector exhausted"),
        ):
            controller._run_candidate_preflight_process(
                [sys.executable, "-c", "pass"],
                os.environ.copy(),
                timeout_seconds=2.0,
                broker_governed=False,
            )
        spawn.assert_not_called()
        selector.close.assert_called_once_with()

    def test_candidate_subreaper_restores_only_after_empty_child_proof(self) -> None:
        with (
            mock.patch.object(
                release_controller,
                "_child_subreaper_enabled",
                side_effect=[False, False],
            ),
            mock.patch.object(
                release_controller,
                "_set_child_subreaper",
            ) as set_subreaper,
            mock.patch.object(
                release_controller,
                "_direct_child_identities",
                return_value={},
            ),
        ):
            with release_controller._candidate_child_subreaper():
                pass
        self.assertEqual(
            set_subreaper.call_args_list,
            [mock.call(True), mock.call(False)],
        )

        with (
            mock.patch.object(
                release_controller,
                "_child_subreaper_enabled",
                return_value=True,
            ),
            mock.patch.object(
                release_controller,
                "_set_child_subreaper",
            ) as keep_subreaper,
            mock.patch.object(
                release_controller,
                "_direct_child_identities",
                return_value={},
            ),
        ):
            with release_controller._candidate_child_subreaper():
                pass
        keep_subreaper.assert_not_called()

    def test_candidate_subreaper_cleanup_failure_never_restores(self) -> None:
        child = release_controller._ProcessIdentity(101, 11, os.getpid(), 101, "S")
        with (
            mock.patch.object(
                release_controller,
                "_child_subreaper_enabled",
                return_value=False,
            ),
            mock.patch.object(
                release_controller,
                "_set_child_subreaper",
            ) as set_subreaper,
            mock.patch.object(
                release_controller,
                "_direct_child_identities",
                side_effect=[{}, {child.pid: child}, {child.pid: child}],
            ),
            mock.patch.object(release_controller, "_reap_candidate_zombies"),
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "cleanup could not be proven",
            ),
        ):
            with release_controller._candidate_child_subreaper():
                pass

        set_subreaper.assert_called_once_with(True)

    @unittest.skipUnless(Path("/proc/self/stat").is_file(), "Linux /proc is required")
    def test_candidate_cleanup_does_not_signal_same_uid_external_sibling(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            False,
        )
        controller.release_dir.mkdir(parents=True)
        pid_file = self.root / "external-sibling.pid"
        helper = self.root / "external-sibling.py"
        helper.write_text(
            """import os, signal, sys, time
child = os.fork()
if child:
    with open(sys.argv[1], "w", encoding="ascii") as stream:
        stream.write(str(child))
        stream.flush()
        os.fsync(stream.fileno())
    os._exit(0)
os.setsid()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(1)
""",
            encoding="utf-8",
        )
        subprocess.run(
            [sys.executable, str(helper), str(pid_file)],
            check=True,
        )
        sibling_pid = int(pid_file.read_text(encoding="ascii"))
        try:
            sibling = release_controller._read_process_identity(sibling_pid)
            self.assertIsNotNone(sibling)
            self.assertNotEqual(sibling.parent_pid, os.getpid())
            with (
                mock.patch.object(
                    release_controller,
                    "CANDIDATE_PREFLIGHT_TERM_GRACE_SECONDS",
                    0.1,
                ),
                mock.patch.object(
                    release_controller,
                    "CANDIDATE_PREFLIGHT_KILL_WAIT_SECONDS",
                    1.0,
                ),
                self.assertRaises(subprocess.TimeoutExpired),
            ):
                controller._run_candidate_preflight_process(
                    [sys.executable, "-c", "import time; time.sleep(5)"],
                    os.environ.copy(),
                    timeout_seconds=0.1,
                    broker_governed=False,
                )
            surviving = release_controller._read_process_identity(sibling_pid)
            self.assertIsNotNone(surviving)
            self.assertEqual(surviving.start_ticks, sibling.start_ticks)
        finally:
            try:
                os.kill(sibling_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_broker_cleanup_signals_only_the_cooperative_root(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            False,
        )
        root = release_controller._ProcessIdentity(100, 10, os.getpid(), 100, "S")
        child = release_controller._ProcessIdentity(101, 11, 100, 101, "S")
        process = mock.Mock(pid=100)
        process.poll.side_effect = [None, 0, 0]
        identities = {100: root, 101: child}

        with (
            mock.patch.object(
                release_controller,
                "_read_process_identity",
                return_value=root,
            ),
            mock.patch.object(
                release_controller,
                "_signal_verified_processes",
            ) as send_signal,
            mock.patch.object(
                release_controller,
                "_wait_for_candidate_process_tree",
                return_value=False,
            ),
            mock.patch.object(release_controller, "_adopt_candidate_children"),
            mock.patch.object(release_controller, "_reap_candidate_zombies"),
            mock.patch.object(
                release_controller,
                "_process_identity_is_live",
                side_effect=lambda identity: identity.pid == child.pid,
            ),
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "Broker-governed runtime cleanup",
            ),
        ):
            controller._terminate_candidate_preflight_process(
                process,
                identities,
                {},
                broker_governed=True,
            )

        self.assertEqual(send_signal.call_count, 2)
        for call in send_signal.call_args_list:
            self.assertEqual(set(call.args[0]), {root.pid})
        self.assertEqual(send_signal.call_args_list[0].args[1], signal.SIGTERM)
        self.assertEqual(send_signal.call_args_list[1].args[1], signal.SIGKILL)

    @unittest.skipUnless(Path("/proc/self/stat").is_file(), "Linux /proc is required")
    def test_candidate_preflight_timeout_kills_stubborn_independent_descendants(self) -> None:
        controller = release_controller.ReleaseController(
            self.root / "production",
            self.build(),
            "auto",
            False,
        )
        controller.release_dir.mkdir(parents=True)
        pid_file = self.root / "candidate-processes.txt"
        grandchild_script = self.root / "grandchild.py"
        child_script = self.root / "child.py"
        root_script = self.root / "preflight.py"
        grandchild_script.write_text(
            """import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(sys.argv[1], "a", encoding="utf-8") as stream:
    stream.write(f"grandchild:{os.getpid()}\\n")
    stream.flush()
    os.fsync(stream.fileno())
while True:
    time.sleep(1)
""",
            encoding="utf-8",
        )
        child_script.write_text(
            """import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(sys.argv[1], "a", encoding="utf-8") as stream:
    stream.write(f"child:{os.getpid()}\\n")
    stream.flush()
    os.fsync(stream.fileno())
subprocess.Popen(
    [sys.executable, sys.argv[2], sys.argv[1]],
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
while True:
    time.sleep(1)
""",
            encoding="utf-8",
        )
        root_script.write_text(
            """import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(sys.argv[1], "a", encoding="utf-8") as stream:
    stream.write(f"root:{os.getpid()}\\n")
    stream.flush()
    os.fsync(stream.fileno())
subprocess.Popen(
    [sys.executable, sys.argv[2], sys.argv[1], sys.argv[3]],
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
while True:
    time.sleep(1)
""",
            encoding="utf-8",
        )

        recorded_pids: list[int] = []
        try:
            with (
                mock.patch.object(
                    release_controller,
                    "CANDIDATE_PREFLIGHT_TERM_GRACE_SECONDS",
                    0.2,
                ),
                mock.patch.object(
                    release_controller,
                    "CANDIDATE_PREFLIGHT_KILL_WAIT_SECONDS",
                    2.0,
                ),
                self.assertRaises(subprocess.TimeoutExpired),
            ):
                controller._run_candidate_preflight_process(
                    [
                        sys.executable,
                        str(root_script),
                        str(pid_file),
                        str(child_script),
                        str(grandchild_script),
                    ],
                    os.environ.copy(),
                    timeout_seconds=1.5,
                    broker_governed=False,
                )

            records = pid_file.read_text(encoding="utf-8").splitlines()
            labels = {record.partition(":")[0] for record in records}
            recorded_pids = [int(record.partition(":")[2]) for record in records]
            self.assertEqual(labels, {"root", "child", "grandchild"})
            deadline = time.monotonic() + 2.0
            while (
                any(
                    (identity := release_controller._read_process_identity(pid))
                    is not None
                    and identity.state != "Z"
                    for pid in recorded_pids
                )
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            for pid in recorded_pids:
                identity = release_controller._read_process_identity(pid)
                self.assertTrue(identity is None or identity.state == "Z")
        finally:
            if pid_file.exists():
                for record in pid_file.read_text(encoding="utf-8").splitlines():
                    raw_pid = record.partition(":")[2]
                    if raw_pid.isdigit() and int(raw_pid) not in recorded_pids:
                        recorded_pids.append(int(raw_pid))
            for pid in recorded_pids:
                try:
                    os.killpg(pid, release_controller.signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def test_strict_candidate_failure_resumes_worker_before_pull_or_backup(self) -> None:
        controller = self.existing_release_controller()
        controller.deploy_transport_required = True

        def prepare(_environment: dict[str, str]) -> None:
            self.prepare_mock_ready_release(controller)

        with (
            mock.patch.object(controller, "ensure_root"),
            mock.patch.object(controller, "environment", return_value={}),
            mock.patch.object(controller, "validate_current_runtime"),
            mock.patch.object(controller, "prepare_staging", side_effect=prepare),
            mock.patch.object(controller, "assert_still_current_main"),
            mock.patch.object(
                controller,
                "drain_worker",
                return_value={
                    "supported": True,
                    "active_jobs": 0,
                    "worker_instance_id": "old-instance",
                },
            ) as drain_worker,
            mock.patch.object(
                controller,
                "wait_for_worker_idle",
                return_value={
                    "active_jobs": 0,
                    "draining": True,
                    "accepting_jobs": False,
                },
            ),
            mock.patch.object(
                controller,
                "run_candidate_runtime_preflight",
                side_effect=release_controller.ReleaseError(
                    "candidate Worker runtime preflight failed"
                ),
            ),
            mock.patch.object(
                controller,
                "recover_drained_worker",
                return_value="resumed-after-failure",
            ) as recover_worker,
            mock.patch.object(controller, "run") as run,
            mock.patch.object(controller, "backup_database") as backup,
            self.assertRaisesRegex(
                release_controller.ReleaseError,
                "candidate Worker runtime preflight failed",
            ),
        ):
            controller.deploy()

        drain_worker.assert_called_once_with({})
        recover_worker.assert_called_once_with({})
        run.assert_not_called()
        backup.assert_not_called()

    def test_deploy_waits_all_jobs_and_uses_authoritative_smoke(self) -> None:
        source = CONTROLLER_PATH.read_text(encoding="utf-8")
        self.assertNotIn("self.wait_for_jobs(environment, ignore_monomer_md=True)", source)
        self.assertIn("self.wait_for_jobs(environment)", source)
        self.assertIn("self.restart_or_defer_worker(environment)", source)
        self.assertIn("self.resume_worker(environment)", source)
        self.assertIn("self.run_ingress_isolated_contract_smoke(environment)", source)
        self.assertIn(
            'operation_id=f"deploy-smoke-{self.sha}"',
            source,
        )


if __name__ == "__main__":
    unittest.main()

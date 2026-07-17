from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/asset_release_contract.py"
SPEC = importlib.util.spec_from_file_location(
    "asset_release_contract_test",
    SCRIPT,
)
assert SPEC is not None and SPEC.loader is not None
ASSET = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ASSET)


def digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def manifest_record(path: str, payload: bytes) -> dict[str, object]:
    return {
        "path": path,
        "size": len(payload),
        "sha256": digest_bytes(payload),
    }


def git(
    root: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        env=environment,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


class AssetFixture:
    def __init__(
        self,
        root: Path,
        *,
        changed_unchanged_tree: bool = False,
        wrong_source: bool = False,
        builder: dict[str, str] | None = None,
    ) -> None:
        self.wrong_source = wrong_source
        self.releases = root / "releases"
        self.releases.mkdir(mode=0o700)
        self.v1_contents = {
            "model": {"weights.bin": b"model-v1"},
            "database": {"data.txt": b"database-v1"},
            "backend-data": {"config.json": b"backend-v1"},
            "byteff2": {"BYTEFF2-COMMIT": (("1" * 40) + "\n").encode()},
        }
        self.v1_root, self.v1_digest, self.v1_assets = self._write_v1()
        model_payload = b"model-v2-drift" if changed_unchanged_tree else b"model-v1"
        self.v2_contents = {
            "model": {"weights.bin": model_payload},
            "database": {"data.txt": b"database-v1"},
            "backend-data": {"config.json": b"backend-v1"},
            "byteff2": {
                "BYTEFF2-COMMIT": (("2" * 40) + "\n").encode(),
                "runtime.bin": b"runtime-contract",
                "byteff2/overlay.bin": b"audited-overlay",
            },
        }
        self.v2_assets = self._assets(self.v2_contents)
        unchanged = {
            name: ASSET._tree_digest(self.v2_assets[name])
            for name in ASSET.UNCHANGED_ASSET_TREES
        }
        all_trees = {
            name: ASSET._tree_digest(self.v2_assets[name])
            for name in ASSET.ASSET_TREES
        }
        overlay = manifest_record(
            "byteff2/overlay.bin",
            self.v2_contents["byteff2"]["byteff2/overlay.bin"],
        )
        overlay["source_path"] = "overlay.bin"
        self.contract = {
            "predecessor_asset_digest": self.v1_digest,
            "unchanged_asset_tree_digests": unchanged,
            "asset_tree_digests": all_trees,
            "byteff2_commit": "2" * 40,
            "byteff2_tree": "3" * 40,
            "byteff2_submodules": {},
            "byteff2_submodule_trees": {},
            "byteff2_source": {
                "source": "https://example.test/byteff2.git",
                "revision": "2" * 40,
            },
            "byteff2_audited_overlays": {
                "source": "https://example.test/byteff2-overlay",
                "revision": "4" * 40,
                "files": [overlay],
            },
            "byteff2_required_runtime_files": {
                "runtime.bin": [
                    len(self.v2_contents["byteff2"]["runtime.bin"]),
                    digest_bytes(self.v2_contents["byteff2"]["runtime.bin"]),
                ],
                "byteff2/overlay.bin": [
                    len(self.v2_contents["byteff2"]["byteff2/overlay.bin"]),
                    digest_bytes(
                        self.v2_contents["byteff2"]["byteff2/overlay.bin"]
                    ),
                ],
            },
            "builder_repository": ASSET.BUILD_SOURCE_REPOSITORY,
            "builder_script": ASSET.BUILD_SOURCE_SCRIPT,
            "build_evidence": {
                **ASSET.BUILD_EVIDENCE,
                "predecessor_manifest_digest": self.v1_digest,
            },
        }
        self.builder = builder or {
            "repository": ASSET.BUILD_SOURCE_REPOSITORY,
            "commit": "5" * 40,
            "tree": "6" * 40,
            "script_path": ASSET.BUILD_SOURCE_SCRIPT,
            "script_blob": "7" * 40,
        }
        self.v2_root, self.v2_digest = self._write_v2()

    @staticmethod
    def _assets(
        contents: dict[str, dict[str, bytes]],
    ) -> dict[str, list[dict[str, object]]]:
        return {
            tree: [
                manifest_record(path, payload)
                for path, payload in sorted(contents[tree].items())
            ]
            for tree in ASSET.ASSET_TREES
        }

    @staticmethod
    def _seal(root: Path, *, directory_mode: int, file_mode: int) -> None:
        directories = [root]
        for path in root.rglob("*"):
            if path.is_dir():
                directories.append(path)
            else:
                path.chmod(file_mode)
        for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            path.chmod(directory_mode)

    @staticmethod
    def _write_contents(
        root: Path,
        contents: dict[str, dict[str, bytes]],
    ) -> None:
        for tree, files in contents.items():
            for relative, payload in files.items():
                target = root / tree / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)

    def _write_v1(
        self,
    ) -> tuple[Path, str, dict[str, list[dict[str, object]]]]:
        staging = self.releases / ".v1-staging"
        staging.mkdir(mode=0o700)
        self._write_contents(staging, self.v1_contents)
        assets = self._assets(self.v1_contents)
        document = {
            "schema_version": 1,
            "byteff2_commit": "1" * 40,
            "byteff2_submodules": {},
            "assets": assets,
        }
        payload = ASSET.canonical_json_bytes(document, newline=True)
        (staging / "ASSET-MANIFEST.json").write_bytes(payload)
        digest = ASSET.sha256_bytes(payload)
        root = self.releases / digest.removeprefix("sha256:")
        staging.rename(root)
        self._seal(root, directory_mode=0o555, file_mode=0o444)
        return root, digest, assets

    def _write_v2(self) -> tuple[Path, str]:
        staging = self.releases / ".v2-staging"
        staging.mkdir(mode=0o700)
        self._write_contents(staging, self.v2_contents)
        document = {
            "schema_version": 2,
            "byteff2_commit": self.contract["byteff2_commit"],
            "byteff2_submodules": self.contract["byteff2_submodules"],
            "assets": self.v2_assets,
            "predecessor_asset_digest": self.v1_digest,
            "changed_asset_trees": ["byteff2"],
            "unchanged_asset_tree_digests": self.contract[
                "unchanged_asset_tree_digests"
            ],
            "asset_tree_digests": self.contract["asset_tree_digests"],
            "byteff2_tree": self.contract["byteff2_tree"],
            "byteff2_submodule_trees": self.contract[
                "byteff2_submodule_trees"
            ],
            "byteff2_source": (
                {
                    **self.contract["byteff2_source"],
                    "source": "https://invalid.example/byteff2.git",
                }
                if self.wrong_source
                else self.contract["byteff2_source"]
            ),
            "byteff2_audited_overlays": self.contract[
                "byteff2_audited_overlays"
            ],
            "build_provenance": {
                "schema_version": 1,
                "builder_source": self.builder,
                "evidence": self.contract["build_evidence"],
            },
        }
        payload = ASSET.canonical_json_bytes(document, newline=True)
        (staging / "ASSET-MANIFEST.json").write_bytes(payload)
        digest = ASSET.sha256_bytes(payload)
        root = self.releases / digest.removeprefix("sha256:")
        staging.rename(root)
        self._seal(root, directory_mode=0o500, file_mode=0o400)
        return root, digest


class AssetReleaseValidationTests(unittest.TestCase):
    def test_deep_validation_rehashes_v1_and_v2(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = AssetFixture(Path(raw))
            evidence = ASSET.validate_schema_v2_release(
                fixture.v2_root,
                expected_digest=fixture.v2_digest,
                releases_root=fixture.releases,
                contract=fixture.contract,
            )
        self.assertEqual(evidence["manifest_sha256"], fixture.v2_digest)
        self.assertEqual(evidence["predecessor_manifest_sha256"], fixture.v1_digest)
        self.assertTrue(evidence["read_only"])

    def test_rejects_tampered_predecessor_even_when_v2_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = AssetFixture(Path(raw))
            predecessor = fixture.v1_root / "database/data.txt"
            predecessor.chmod(0o600)
            predecessor.write_bytes(b"tampered")
            predecessor.chmod(0o444)
            with self.assertRaisesRegex(
                ASSET.AssetContractError,
                "differs from manifest",
            ):
                ASSET.validate_schema_v2_release(
                    fixture.v2_root,
                    expected_digest=fixture.v2_digest,
                    releases_root=fixture.releases,
                    contract=fixture.contract,
                )

    def test_rejects_self_consistent_drift_in_an_unchanged_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = AssetFixture(
                Path(raw),
                changed_unchanged_tree=True,
            )
            with self.assertRaisesRegex(
                ASSET.AssetContractError,
                "inherited tree differs byte-for-byte",
            ):
                ASSET.validate_schema_v2_release(
                    fixture.v2_root,
                    expected_digest=fixture.v2_digest,
                    releases_root=fixture.releases,
                    contract=fixture.contract,
                )

    def test_rejects_schema_v2_provenance_outside_the_fixed_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = AssetFixture(Path(raw), wrong_source=True)
            with self.assertRaisesRegex(
                ASSET.AssetContractError,
                "fixed provenance contract differs",
            ):
                ASSET.validate_schema_v2_release(
                    fixture.v2_root,
                    expected_digest=fixture.v2_digest,
                    releases_root=fixture.releases,
                    contract=fixture.contract,
                )

    def test_rejects_writable_extra_and_symlink_entries(self) -> None:
        for mutation, message in (
            ("writable", "unsafe"),
            ("extra", "unmanifested root entries"),
            ("symlink", "missing or extra entries"),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as raw:
                fixture = AssetFixture(Path(raw))
                fixture.v2_root.chmod(0o700)
                if mutation == "writable":
                    target = fixture.v2_root / "model/weights.bin"
                    target.chmod(0o600)
                elif mutation == "extra":
                    target = fixture.v2_root / "EXTRA"
                    target.write_bytes(b"extra")
                    target.chmod(0o400)
                else:
                    tree = fixture.v2_root / "model"
                    tree.chmod(0o700)
                    (tree / "alias").symlink_to("weights.bin")
                    tree.chmod(0o500)
                fixture.v2_root.chmod(0o500)
                with self.assertRaisesRegex(ASSET.AssetContractError, message):
                    ASSET.validate_schema_v2_release(
                        fixture.v2_root,
                        expected_digest=fixture.v2_digest,
                        releases_root=fixture.releases,
                        contract=fixture.contract,
                    )

    def test_rejects_noncanonical_inventory_paths(self) -> None:
        with self.assertRaisesRegex(ASSET.AssetContractError, "unsafe"):
            ASSET._normalized_records(
                [
                    {
                        "path": "safe/../escape",
                        "size": 0,
                        "sha256": "0" * 64,
                    }
                ],
                label="test",
            )


class BuilderBundleProofTests(unittest.TestCase):
    def test_bundle_proves_exact_builder_target_authority_chain(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw)
            parent.chmod(0o700)
            repository = parent / "source"
            repository.mkdir(mode=0o700)
            git(repository, "init", "-q", "-b", "main")
            environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Asset Test",
                "GIT_AUTHOR_EMAIL": "asset@example.test",
                "GIT_COMMITTER_NAME": "Asset Test",
                "GIT_COMMITTER_EMAIL": "asset@example.test",
            }
            script = repository / ASSET.BUILD_SOURCE_SCRIPT
            script.parent.mkdir(parents=True)
            script.write_text("print('builder')\n", encoding="utf-8")
            git(repository, "add", ".", environment=environment)
            git(repository, "commit", "-q", "-m", "B0", environment=environment)
            builder_sha = git(repository, "rev-parse", "HEAD")
            builder_tree = git(repository, "rev-parse", "HEAD^{tree}")
            builder_blob = git(
                repository,
                "rev-parse",
                f"HEAD:{ASSET.BUILD_SOURCE_SCRIPT}",
            )
            (repository / "target.txt").write_text("B1\n", encoding="utf-8")
            git(repository, "add", ".", environment=environment)
            git(repository, "commit", "-q", "-m", "B1", environment=environment)
            target_sha = git(repository, "rev-parse", "HEAD")
            target_tree = git(repository, "rev-parse", "HEAD^{tree}")
            (repository / "authority.txt").write_text("F\n", encoding="utf-8")
            git(repository, "add", ".", environment=environment)
            git(repository, "commit", "-q", "-m", "F", environment=environment)
            authority_sha = git(repository, "rev-parse", "HEAD")
            authority_tree = git(repository, "rev-parse", "HEAD^{tree}")
            bundle = parent / "authority.bundle"
            git(
                repository,
                "bundle",
                "create",
                str(bundle),
                "refs/heads/main",
            )
            bundle.chmod(0o600)
            bundle_digest = ASSET.sha256_bytes(bundle.read_bytes())
            builder = {
                "repository": ASSET.BUILD_SOURCE_REPOSITORY,
                "commit": builder_sha,
                "tree": builder_tree,
                "script_path": ASSET.BUILD_SOURCE_SCRIPT,
                "script_blob": builder_blob,
            }
            proof = ASSET.verify_builder_from_bundle(
                bundle,
                expected_bundle_sha256=bundle_digest,
                builder_source=builder,
                target={"sha": target_sha, "tree": target_tree},
                authority={"sha": authority_sha, "tree": authority_tree},
            )
            self.assertTrue(proof["ancestry"]["builder_to_target"])
            self.assertFalse(proof["network_used"])

            fixture_root = parent / "assets"
            fixture_root.mkdir(mode=0o700)
            fixture = AssetFixture(
                fixture_root,
                builder=builder,
            )
            pointer = {
                "path": str(parent / "runtime/state/current-assets"),
                "present": False,
            }
            evidence = ASSET.build_asset_evidence(
                expected_digest=fixture.v2_digest,
                bundle_path=bundle,
                expected_bundle_sha256=bundle_digest,
                target={"sha": target_sha, "tree": target_tree},
                authority={"sha": authority_sha, "tree": authority_tree},
                live_pointer_start=pointer,
                live_pointer_end=pointer,
                datasets_on_asset_change=[],
                releases_root=fixture.releases,
                contract=fixture.contract,
            )
            self.assertEqual(evidence["builder_proof"], proof)
            self.assertEqual(evidence["database_effect"], "none")
            self.assertEqual(
                evidence["mutable_data_seal"]["authority"],
                "pull-descriptor",
            )

            with self.assertRaisesRegex(
                ASSET.AssetContractError,
                "builder script blob differs",
            ):
                ASSET.verify_builder_from_bundle(
                    bundle,
                    expected_bundle_sha256=bundle_digest,
                    builder_source={**builder, "script_blob": "f" * 40},
                    target={"sha": target_sha, "tree": target_tree},
                    authority={"sha": authority_sha, "tree": authority_tree},
                )


class LivePointerSnapshotTests(unittest.TestCase):
    def test_snapshot_is_inode_bound_and_detects_pointer_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / "state"
            state.mkdir()
            releases = root / "releases"
            releases.mkdir()
            targets = []
            for name in ("one", "two"):
                payload = f'{{"target":"{name}"}}\n'.encode()
                digest = digest_bytes(payload)
                target = releases / digest
                target.mkdir()
                manifest = target / "ASSET-MANIFEST.json"
                manifest.write_bytes(payload)
                manifest.chmod(0o400)
                target.chmod(0o500)
                targets.append(target)
            pointer = state / "current-assets"
            pointer.symlink_to(targets[0])
            first = ASSET.snapshot_live_asset_pointer(
                pointer,
                releases_root=releases,
            )
            pointer.unlink()
            pointer.symlink_to(targets[1])
            second = ASSET.snapshot_live_asset_pointer(
                pointer,
                releases_root=releases,
            )
            self.assertNotEqual(first, second)
            self.assertNotEqual(
                first["manifest_sha256"],
                second["manifest_sha256"],
            )

    def test_absent_pointer_is_explicit_and_relative_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pointer = root / "current-assets"
            self.assertEqual(
                ASSET.snapshot_live_asset_pointer(pointer),
                {"path": str(pointer), "present": False},
            )
            target = root / "target"
            target.mkdir()
            pointer.symlink_to("target")
            with self.assertRaisesRegex(
                ASSET.AssetContractError,
                "absolute",
            ):
                ASSET.snapshot_live_asset_pointer(pointer)


if __name__ == "__main__":
    unittest.main()

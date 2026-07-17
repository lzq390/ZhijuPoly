from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "bootstrap_asset_release.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_asset_release", SCRIPT)
assert SPEC and SPEC.loader
assets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(assets)

BYTEFF2_RUNTIME_ASSET_PATH = Path(assets.BYTEFF2_RUNTIME_REQUIRED_FILES[0][0])
BYTEFF2_RUNTIME_ASSET = b"""elem_i,elem_j,length
6,6,1.5363873
6,7,1.4651788
6,8,1.4313077
6,16,1.834275
7,7,1.4078844
7,8,1.4093844
8,8,1.4897662
1,16,1.3495871
16,16,2.0898104
9,16,1.6
16,17,2.1403589
16,35,2.2360892
16,53,2.5999999
7,16,1.698557
8,16,1.6829032
1,15,1.4123673
6,15,1.9002142
7,15,1.6796112
8,15,1.6054208
15,16,2.1043606
6,9,1.3551362
6,17,1.8122059
6,35,2.0027883
6,53,2.2647898
7,9,1.4233449
7,17,1.8143761
7,35,1.8648889
7,53,2.0999999
9,15,1.64
15,17,2.0355189
15,35,2.2727704
15,53,2.5999999
1,6,1.094539
1,7,1.0124171
1,8,0.9722268
1,1,0.64
1,9,0.96
1,17,1.31
1,35,1.46
1,53,1.6500000000000001
8,9,1.27
8,17,1.62
8,35,1.77
8,53,1.96
9,9,1.28
9,17,1.63
9,35,1.7799999999999998
9,53,1.9700000000000002
15,15,2.22
17,17,1.98
17,35,2.13
17,53,2.3200000000000003
35,35,2.28
35,53,2.4699999999999998
53,53,2.66
"""
PRODUCTION_BYTEFF2_AUDITED_OVERLAY_FILES = assets.BYTEFF2_AUDITED_OVERLAY_FILES
TEST_BYTEFF2_AUDITED_OVERLAYS = (
    ("byteff2/trained_models/fftrainer_config_in_use.yaml", b"fixture model config\n"),
    ("byteff2/trained_models/optimal.pt", b"fixture model weights\n"),
)
TEST_BYTEFF2_AUDITED_OVERLAY_FILES = tuple(
    (path, len(payload), hashlib.sha256(payload).hexdigest())
    for path, payload in TEST_BYTEFF2_AUDITED_OVERLAYS
)


class AssetBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        overlay_patcher = mock.patch.object(
            assets,
            "BYTEFF2_AUDITED_OVERLAY_FILES",
            TEST_BYTEFF2_AUDITED_OVERLAY_FILES,
        )
        symlink_patcher = mock.patch.object(
            assets,
            "BYTEFF2_MATERIALIZED_SYMLINKS",
            {},
        )
        overlay_patcher.start()
        symlink_patcher.start()
        self.addCleanup(overlay_patcher.stop)
        self.addCleanup(symlink_patcher.stop)

    @staticmethod
    def git(root: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()

    @staticmethod
    def required_runtime_inventory() -> list[dict[str, object]]:
        runtime_path, runtime_digest = assets.BYTEFF2_RUNTIME_REQUIRED_FILES[0]
        return [
            {
                "path": runtime_path,
                "size": len(BYTEFF2_RUNTIME_ASSET),
                "sha256": runtime_digest,
            },
            *[
                {"path": path, "size": size, "sha256": digest}
                for path, size, digest in assets.BYTEFF2_AUDITED_OVERLAY_FILES
            ],
        ]

    @staticmethod
    def predecessor_tree_digests() -> dict[str, str]:
        return {
            tree_name: assets.tree_inventory_digest([])
            for tree_name in assets.UNCHANGED_ASSET_TREES
        }

    @staticmethod
    def builder_source() -> dict[str, str]:
        return {
            "repository": assets.BUILD_SOURCE_REPOSITORY,
            "commit": "1" * 40,
            "tree": "2" * 40,
            "script_path": assets.BUILD_SOURCE_SCRIPT,
            "script_blob": "3" * 40,
        }

    def initialize_repository(
        self,
        root: Path,
        filename: str = "tracked.txt",
        *,
        runtime_asset: bytes | None = BYTEFF2_RUNTIME_ASSET,
        audited_overlays: tuple[tuple[str, bytes], ...] = TEST_BYTEFF2_AUDITED_OVERLAYS,
    ) -> str:
        root.mkdir(parents=True)
        self.git(root, "init", "--quiet")
        self.git(root, "config", "user.email", "ci@example.invalid")
        self.git(root, "config", "user.name", "CI")
        (root / filename).write_text(f"{root.name}\n", encoding="utf-8")
        overlay_paths = tuple(path for path, _size, _digest in assets.BYTEFF2_AUDITED_OVERLAY_FILES)
        (root / ".gitignore").write_text(
            "".join(f"/{path}\n" for path in overlay_paths),
            encoding="utf-8",
        )
        for relative, payload in audited_overlays:
            overlay_path = root / relative
            overlay_path.parent.mkdir(parents=True, exist_ok=True)
            overlay_path.write_bytes(payload)
        tracked = [filename, ".gitignore"]
        if runtime_asset is not None:
            runtime_path = root / BYTEFF2_RUNTIME_ASSET_PATH
            runtime_path.parent.mkdir(parents=True)
            runtime_path.write_bytes(runtime_asset)
            tracked.append(BYTEFF2_RUNTIME_ASSET_PATH.as_posix())
        self.git(root, "add", "--", *tracked)
        self.git(root, "commit", "--quiet", "-m", "initial")
        return self.git(root, "rev-parse", "HEAD")

    def initialize_predecessor(
        self,
        store: Path,
    ) -> tuple[str, Path, dict[str, list[dict[str, object]]]]:
        staging = store / "predecessor-staging"
        staging.mkdir(parents=True)
        inventories: dict[str, list[dict[str, object]]] = {}
        for tree_name in assets.ASSET_KEYS:
            tree = staging / tree_name
            tree.mkdir()
            (tree / "asset.bin").write_bytes(tree_name.encode("ascii"))
            inventories[tree_name] = assets.inspect_tree(tree, hash_files=True)
        manifest = {
            "schema_version": 1,
            "byteff2_commit": "c" * 40,
            "byteff2_submodules": {},
            "assets": inventories,
        }
        manifest_bytes = assets.canonical(manifest)
        digest = hashlib.sha256(manifest_bytes).hexdigest()
        (staging / "ASSET-MANIFEST.json").write_bytes(manifest_bytes)
        releases = store / "releases"
        releases.mkdir()
        destination = releases / digest
        staging.replace(destination)
        return f"sha256:{digest}", destination, inventories

    def test_tree_inventory_is_stable_and_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "nested").mkdir()
            (root / "nested" / "model.bin").write_bytes(b"model")
            records = assets.inspect_tree(root, hash_files=True)
            self.assertEqual([record["path"] for record in records], ["nested/model.bin"])
            self.assertEqual(len(records[0]["sha256"]), 64)
            (root / "link").symlink_to("nested/model.bin")
            with self.assertRaisesRegex(assets.AssetError, "symlinks"):
                assets.inspect_tree(root, hash_files=False)

    def test_predecessor_is_fully_rehashed_and_rejects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = Path(raw) / "assets"
            predecessor_digest, predecessor, inventories = self.initialize_predecessor(
                store
            )
            with mock.patch.object(
                assets,
                "PREDECESSOR_ASSET_DIGEST",
                predecessor_digest,
            ):
                loaded_path, _manifest, verified, tree_digests = (
                    assets.load_verified_predecessor(store, predecessor_digest)
                )
                self.assertEqual(loaded_path, predecessor)
                self.assertEqual(verified, inventories)
                self.assertEqual(set(tree_digests), set(assets.UNCHANGED_ASSET_TREES))

                (predecessor / "model" / "asset.bin").write_bytes(b"drift")
                with self.assertRaisesRegex(
                    assets.AssetError,
                    "tree differs from manifest",
                ):
                    assets.load_verified_predecessor(store, predecessor_digest)

    def test_predecessor_rejects_unapproved_digest_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = Path(raw) / "assets"
            predecessor_digest, predecessor, _inventories = (
                self.initialize_predecessor(store)
            )
            with self.assertRaisesRegex(assets.AssetError, "only the approved"):
                assets.load_verified_predecessor(store, predecessor_digest)
            predecessor.rename(predecessor.with_name("real-release"))
            predecessor.symlink_to("real-release", target_is_directory=True)
            with mock.patch.object(
                assets,
                "PREDECESSOR_ASSET_DIGEST",
                predecessor_digest,
            ):
                with self.assertRaisesRegex(assets.AssetError, "unavailable"):
                    assets.load_verified_predecessor(store, predecessor_digest)

    def test_manifest_digest_is_canonical(self) -> None:
        left = assets.canonical({"schema_version": 1, "assets": {"model": []}})
        right = assets.canonical({"assets": {"model": []}, "schema_version": 1})
        self.assertEqual(left, right)

    def test_manifest_records_byteff2_parent_and_submodule_commits(self) -> None:
        parent = "a" * 40
        child = "b" * 40
        manifest = assets.build_manifest(
            {
                **{tree: [] for tree in assets.UNCHANGED_ASSET_TREES},
                "byteff2": self.required_runtime_inventory(),
            },
            byteff2_commit=parent,
            byteff2_tree="d" * 40,
            byteff2_submodules={"vendor/nested": child},
            byteff2_submodule_trees={"vendor/nested": "e" * 40},
            predecessor_digest=assets.PREDECESSOR_ASSET_DIGEST,
            predecessor_tree_digests=self.predecessor_tree_digests(),
            builder_source=self.builder_source(),
        )
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(
            manifest["predecessor_asset_digest"],
            assets.PREDECESSOR_ASSET_DIGEST,
        )
        self.assertEqual(manifest["changed_asset_trees"], ["byteff2"])
        self.assertEqual(manifest["byteff2_commit"], parent)
        self.assertEqual(manifest["byteff2_tree"], "d" * 40)
        self.assertEqual(
            manifest["byteff2_submodule_trees"],
            {"vendor/nested": "e" * 40},
        )
        self.assertEqual(
            manifest["byteff2_source"],
            {
                "source": assets.BYTEFF2_GIT_SOURCE,
                "revision": parent,
            },
        )
        self.assertEqual(manifest["byteff2_submodules"], {"vendor/nested": child})
        self.assertEqual(
            manifest["byteff2_audited_overlays"],
            assets.byteff2_audited_overlays_manifest(),
        )
        self.assertEqual(set(manifest["asset_tree_digests"]), set(assets.ASSET_KEYS))
        self.assertEqual(
            manifest["build_provenance"]["builder_source"],
            self.builder_source(),
        )
        self.assertEqual(
            manifest["build_provenance"]["evidence"],
            {
                "predecessor_manifest_digest": assets.PREDECESSOR_ASSET_DIGEST,
                "predecessor_all_trees_rehashed": list(assets.ASSET_KEYS),
                "unchanged_trees_byte_identical": list(assets.UNCHANGED_ASSET_TREES),
                "asset_tree_digest_algorithm": "canonical-manifest-inventory-v1",
                "byteff2_source_verification": "clean-recursive-commit-and-tree",
                "staging_directory_mode": "0700",
                "file_and_directory_fsync": True,
                "publication": "atomic-rename",
                "existing_target": "full-content-revalidation",
            },
        )

    def test_manifest_rejects_missing_or_changed_required_runtime_asset(self) -> None:
        runtime_path, _runtime_digest = assets.BYTEFF2_RUNTIME_REQUIRED_FILES[0]
        cases = (
            ([], "missing from manifest"),
            (
                [{"path": runtime_path, "size": 1, "sha256": "0" * 64}],
                "manifest digest mismatch",
            ),
        )
        for inventory, error in cases:
            with self.subTest(error=error):
                with self.assertRaisesRegex(assets.AssetError, error):
                    assets.build_manifest(
                        {
                            **{tree: [] for tree in assets.UNCHANGED_ASSET_TREES},
                            "byteff2": inventory,
                        },
                        byteff2_commit="a" * 40,
                        byteff2_tree="d" * 40,
                        byteff2_submodules={},
                        byteff2_submodule_trees={},
                        predecessor_digest=assets.PREDECESSOR_ASSET_DIGEST,
                        predecessor_tree_digests=self.predecessor_tree_digests(),
                        builder_source=self.builder_source(),
                    )

    def test_required_runtime_asset_contract_pins_audited_digest(self) -> None:
        self.assertEqual(
            assets.BYTEFF2_RUNTIME_REQUIRED_FILES,
            (
                (
                    BYTEFF2_RUNTIME_ASSET_PATH.as_posix(),
                    "caa78ff02c7e65fb0c8bcf240382fa8d90b0dfea85a4d9888c96eab04cc4a40e",
                ),
            ),
        )
        self.assertEqual(
            hashlib.sha256(BYTEFF2_RUNTIME_ASSET).hexdigest(),
            assets.BYTEFF2_RUNTIME_REQUIRED_FILES[0][1],
        )

    def test_byteff2_git_source_pins_official_v1_revision(self) -> None:
        self.assertEqual(
            assets.PREDECESSOR_ASSET_DIGEST,
            "sha256:ad19a4f1cb954b3ee6999b7157c798fd887ecd3fd7ae12e40ac20a97637575e2",
        )
        self.assertEqual(
            assets.BYTEFF2_GIT_SOURCE,
            "https://github.com/ByteDance-Seed/byteff2.git",
        )
        self.assertEqual(
            assets.BYTEFF2_GIT_REVISION,
            "8f2813407ba5fbecfb5ec5c69e10b124c5b5bdc2",
        )
        self.assertEqual(
            assets.BYTEFF2_GIT_TREE,
            "2d9ab46fc185e0e830be53c0ad077100e693ce68",
        )
        assets.require_approved_byteff2_revision(assets.BYTEFF2_GIT_REVISION)
        with self.assertRaisesRegex(assets.AssetError, "official v1.0.0"):
            assets.require_approved_byteff2_revision("0" * 40)

    def test_builder_source_identity_binds_clean_commit_tree_and_script_blob(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "builder"
            root.mkdir()
            self.git(root, "init", "--quiet")
            self.git(root, "config", "user.email", "ci@example.invalid")
            self.git(root, "config", "user.name", "CI")
            (root / "builder.py").write_text("print('builder')\n", encoding="utf-8")
            self.git(root, "add", "builder.py")
            self.git(root, "commit", "--quiet", "-m", "add builder")
            commit = self.git(root, "rev-parse", "HEAD")
            self.git(root, "remote", "add", "origin", assets.BUILD_SOURCE_REPOSITORY)

            identity = assets.inspect_builder_source(
                root,
                script_path="builder.py",
                expected_source=assets.BUILD_SOURCE_REPOSITORY,
            )

            self.assertEqual(identity["repository"], assets.BUILD_SOURCE_REPOSITORY)
            self.assertEqual(identity["commit"], commit)
            self.assertEqual(identity["tree"], self.git(root, "rev-parse", "HEAD^{tree}"))
            self.assertEqual(
                identity["script_blob"],
                self.git(root, "rev-parse", "HEAD:builder.py"),
            )

            (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
            self.git(root, "add", ".gitignore")
            self.git(root, "commit", "--quiet", "-m", "ignore cache")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "builder.pyc").write_bytes(b"unreviewed")
            with self.assertRaisesRegex(assets.AssetError, "no modified, untracked, or ignored"):
                assets.inspect_builder_source(
                    root,
                    script_path="builder.py",
                    expected_source=assets.BUILD_SOURCE_REPOSITORY,
                )

    def test_canonical_isolated_invocation_creates_no_ignored_bytecode(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "fresh"
            root.mkdir()
            tracked = subprocess.run(
                ["git", "-C", str(REPOSITORY_ROOT), "ls-files", "-z"],
                check=True,
                stdout=subprocess.PIPE,
            ).stdout.split(b"\0")
            for encoded in tracked:
                if not encoded:
                    continue
                relative = Path(os.fsdecode(encoded))
                source = REPOSITORY_ROOT / relative
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            self.git(root, "init", "--quiet")
            self.git(root, "config", "user.email", "ci@example.invalid")
            self.git(root, "config", "user.name", "CI")
            self.git(root, "add", "-f", "--all")
            self.git(root, "commit", "--quiet", "-m", "fresh builder source")
            command = [
                sys.executable,
                "-I",
                "-B",
                str(root / assets.BUILD_SOURCE_SCRIPT),
                "--help",
            ]
            completed = subprocess.run(
                command,
                cwd=root,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            status = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=all",
                    "--ignored=matching",
                ],
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
            self.assertEqual(status, b"")

            unsafe = subprocess.run(
                [
                    sys.executable,
                    str(root / assets.BUILD_SOURCE_SCRIPT),
                    "--help",
                ],
                cwd=root,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(unsafe.returncode, 2)
            self.assertIn(b"python3 -I -B", unsafe.stderr)

    def test_audited_overlay_contract_pins_hugging_face_revision_size_and_digest(self) -> None:
        self.assertEqual(
            assets.BYTEFF2_AUDITED_OVERLAY_SOURCE,
            "https://huggingface.co/ByteDance-Seed/byteff2",
        )
        self.assertEqual(
            assets.BYTEFF2_AUDITED_OVERLAY_REVISION,
            "b92ac49058c113625012c1f50d98a7bf9cf4e46e",
        )
        self.assertEqual(
            PRODUCTION_BYTEFF2_AUDITED_OVERLAY_FILES,
            (
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
        self.assertEqual(
            assets.BYTEFF2_AUDITED_OVERLAY_SOURCE_PATHS,
            {
                "byteff2/trained_models/fftrainer_config_in_use.yaml": (
                    "trained_models/fftrainer_config_in_use.yaml"
                ),
                "byteff2/trained_models/optimal.pt": "trained_models/optimal.pt",
            },
        )

    def test_checkout_validation_rejects_missing_audited_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "byteff2"
            self.initialize_repository(root, audited_overlays=TEST_BYTEFF2_AUDITED_OVERLAYS[:1])

            with self.assertRaisesRegex(assets.AssetError, "runtime asset is missing"):
                assets.inspect_byteff2_checkout(root)

    def test_checkout_validation_rejects_changed_audited_overlay_digest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "byteff2"
            model_path, model_payload = TEST_BYTEFF2_AUDITED_OVERLAYS[1]
            changed = (
                TEST_BYTEFF2_AUDITED_OVERLAYS[0],
                (model_path, b"x" * len(model_payload)),
            )
            self.initialize_repository(root, audited_overlays=changed)

            with self.assertRaisesRegex(assets.AssetError, "overlay digest mismatch"):
                assets.inspect_byteff2_checkout(root)

    def test_checkout_validation_rejects_missing_required_runtime_asset(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "byteff2"
            self.initialize_repository(root, runtime_asset=None)

            with self.assertRaisesRegex(assets.AssetError, "must be Git-tracked"):
                assets.inspect_byteff2_checkout(root)

    def test_checkout_validation_rejects_ignored_untracked_runtime_asset_lookalike(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "byteff2"
            self.initialize_repository(root, runtime_asset=None)
            with (root / ".gitignore").open("a", encoding="utf-8") as ignore_file:
                ignore_file.write("**.csv\n")
            self.git(root, "add", ".gitignore")
            self.git(root, "commit", "--quiet", "-m", "ignore csv files")
            runtime_path = root / BYTEFF2_RUNTIME_ASSET_PATH
            runtime_path.parent.mkdir(parents=True)
            runtime_path.write_bytes(BYTEFF2_RUNTIME_ASSET)

            with self.assertRaisesRegex(assets.AssetError, "must be Git-tracked"):
                assets.inspect_byteff2_checkout(root)

    def test_checkout_validation_rejects_wrong_runtime_asset_digest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "byteff2"
            self.initialize_repository(root, runtime_asset=b"not the audited runtime data\n")

            with self.assertRaisesRegex(assets.AssetError, "digest mismatch"):
                assets.inspect_byteff2_checkout(root)

    def test_valid_runtime_assets_survive_copy_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            source = workspace / "byteff2"
            destination = workspace / "copied"
            expected_commit = self.initialize_repository(source)

            commit, submodules = assets.inspect_byteff2_checkout(source)
            byteff2_tree, submodule_trees = assets.inspect_byteff2_tree_identity(
                source,
                expected_commit=commit,
                expected_submodules=submodules,
            )
            assets.copy_verified_byteff2(
                source,
                destination,
                expected_commit=commit,
                expected_submodules=submodules,
            )
            inventory = assets.inspect_tree(destination, hash_files=True)
            manifest = assets.build_manifest(
                {
                    **{tree: [] for tree in assets.UNCHANGED_ASSET_TREES},
                    "byteff2": inventory,
                },
                byteff2_commit=expected_commit,
                byteff2_tree=byteff2_tree,
                byteff2_submodules={},
                byteff2_submodule_trees=submodule_trees,
                predecessor_digest=assets.PREDECESSOR_ASSET_DIGEST,
                predecessor_tree_digests=self.predecessor_tree_digests(),
                builder_source=self.builder_source(),
            )

            runtime_path, runtime_digest = assets.BYTEFF2_RUNTIME_REQUIRED_FILES[0]
            runtime_records = [
                record for record in manifest["assets"]["byteff2"] if record["path"] == runtime_path
            ]
            self.assertEqual(
                runtime_records,
                [
                    {
                        "path": runtime_path,
                        "size": len(BYTEFF2_RUNTIME_ASSET),
                        "sha256": runtime_digest,
                    }
                ],
            )

    def test_remove_git_metadata_removes_root_and_nested_forms(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".git" / "objects").mkdir(parents=True)
            (root / ".git" / "config").write_text("metadata", encoding="utf-8")
            (root / "vendor" / "module").mkdir(parents=True)
            (root / "vendor" / "module" / ".git").write_text(
                "gitdir: ../../../.git/modules/vendor/module\n", encoding="utf-8"
            )
            (root / "vendor" / "module" / "code.py").write_text("pass\n", encoding="utf-8")

            assets.remove_git_metadata(root)

            self.assertFalse(any(path.name == ".git" for path in root.rglob(".git")))
            self.assertTrue((root / "vendor" / "module" / "code.py").is_file())

    def test_checkout_validation_covers_submodules_and_dirty_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            child = workspace / "child-source"
            child_commit = self.initialize_repository(child)
            parent = workspace / "byteff2"
            self.initialize_repository(parent)
            self.git(
                parent,
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "--quiet",
                str(child),
                "vendor/child",
            )
            self.git(parent, "commit", "--quiet", "-am", "pin child")

            parent_commit, submodules = assets.inspect_byteff2_checkout(parent)

            self.assertEqual(parent_commit, self.git(parent, "rev-parse", "HEAD"))
            self.assertEqual(submodules, {"vendor/child": child_commit})

            (parent / "vendor" / "child" / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(assets.AssetError, "submodule vendor/child must be clean"):
                assets.inspect_byteff2_checkout(parent)

            child_checkout = parent / "vendor" / "child"
            self.git(child_checkout, "config", "user.email", "ci@example.invalid")
            self.git(child_checkout, "config", "user.name", "CI")
            self.git(child_checkout, "add", "tracked.txt")
            self.git(child_checkout, "commit", "--quiet", "-m", "unpublished child commit")
            with self.assertRaisesRegex(assets.AssetError, "does not match parent gitlink"):
                assets.inspect_byteff2_checkout(parent)

    def test_checkout_validation_rejects_uninitialized_submodule(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            child = workspace / "child-source"
            self.initialize_repository(child)
            parent = workspace / "byteff2"
            self.initialize_repository(parent)
            self.git(
                parent,
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "--quiet",
                str(child),
                "vendor/child",
            )
            self.git(parent, "commit", "--quiet", "-am", "pin child")
            subprocess.run(
                ["git", "-C", str(parent), "submodule", "deinit", "--force", "vendor/child"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            with self.assertRaisesRegex(assets.AssetError, "submodule is not initialized"):
                assets.inspect_byteff2_checkout(parent)

    def test_copy_verified_tree_rejects_source_change_during_copy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            source = workspace / "source"
            destination = workspace / "destination"
            source.mkdir()
            (source / "asset.bin").write_bytes(b"reviewed")

            def changing_copy(copy_source: Path, copy_destination: Path) -> None:
                shutil.copytree(copy_source, copy_destination)
                (copy_source / "asset.bin").write_bytes(b"changed")

            with mock.patch.object(assets, "copy_tree", side_effect=changing_copy):
                with self.assertRaisesRegex(assets.AssetError, "source changed while it was copied"):
                    assets.copy_verified_tree(source, destination)

    def test_copy_verified_tree_rejects_destination_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            source = workspace / "source"
            destination = workspace / "destination"
            source.mkdir()
            (source / "asset.bin").write_bytes(b"reviewed")

            def corrupting_copy(copy_source: Path, copy_destination: Path) -> None:
                shutil.copytree(copy_source, copy_destination)
                (copy_destination / "asset.bin").write_bytes(b"corrupt")

            with mock.patch.object(assets, "copy_tree", side_effect=corrupting_copy):
                with self.assertRaisesRegex(assets.AssetError, "does not match its source fingerprint"):
                    assets.copy_verified_tree(source, destination)

    def test_byteff2_copy_rechecks_commit_even_when_files_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            source = workspace / "byteff2"
            destination = workspace / "copied"
            expected_commit = self.initialize_repository(source)

            def commit_switching_copy(copy_source: Path, copy_destination: Path) -> None:
                shutil.copytree(copy_source, copy_destination)
                self.git(copy_source, "commit", "--quiet", "--allow-empty", "-m", "concurrent commit")

            with mock.patch.object(assets, "copy_tree", side_effect=commit_switching_copy):
                with self.assertRaisesRegex(assets.AssetError, "identity changed while copying"):
                    assets.copy_verified_byteff2(
                        source,
                        destination,
                        expected_commit=expected_commit,
                        expected_submodules={},
                    )

    def test_byteff2_copy_success_writes_verified_marker_and_removes_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            source = workspace / "byteff2"
            destination = workspace / "copied"
            expected_commit = self.initialize_repository(source)

            assets.copy_verified_byteff2(
                source,
                destination,
                expected_commit=expected_commit,
                expected_submodules={},
            )

            self.assertEqual(
                (destination / "BYTEFF2-COMMIT").read_text(encoding="ascii"),
                expected_commit + "\n",
            )
            self.assertFalse((destination / ".git").exists())
            self.assertEqual((destination / "tracked.txt").read_text(encoding="utf-8"), "byteff2\n")

    def test_byteff2_copy_materializes_only_the_audited_internal_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            source = workspace / "byteff2"
            self.initialize_repository(source)
            target = source / "submodules" / "bytemol" / "bytemol"
            target.mkdir(parents=True, exist_ok=True)
            (target / "__init__.py").write_text("AUDITED = True\n", encoding="utf-8")
            (source / "bytemol").symlink_to("submodules/bytemol/bytemol/")
            self.git(source, "add", "bytemol", "submodules/bytemol/bytemol/__init__.py")
            self.git(source, "commit", "--quiet", "-m", "add audited import symlink")
            expected_commit = self.git(source, "rev-parse", "HEAD")
            destination = workspace / "copied"

            with mock.patch.object(
                assets,
                "BYTEFF2_MATERIALIZED_SYMLINKS",
                {"bytemol": "submodules/bytemol/bytemol/"},
            ):
                assets.copy_verified_byteff2(
                    source,
                    destination,
                    expected_commit=expected_commit,
                    expected_submodules={},
                )

            self.assertTrue((destination / "bytemol").is_dir())
            self.assertFalse((destination / "bytemol").is_symlink())
            self.assertEqual(
                (destination / "bytemol" / "__init__.py").read_text(encoding="utf-8"),
                "AUDITED = True\n",
            )

    def test_byteff2_copy_rechecks_recursive_submodule_after_copy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            child = workspace / "child-source"
            child_commit = self.initialize_repository(child)
            parent = workspace / "byteff2"
            self.initialize_repository(parent)
            self.git(
                parent,
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "--quiet",
                str(child),
                "vendor/child",
            )
            self.git(parent, "commit", "--quiet", "-am", "pin child")
            parent_commit = self.git(parent, "rev-parse", "HEAD")

            def submodule_switching_copy(copy_source: Path, copy_destination: Path) -> None:
                shutil.copytree(copy_source, copy_destination)
                checkout = copy_source / "vendor" / "child"
                self.git(checkout, "config", "user.email", "ci@example.invalid")
                self.git(checkout, "config", "user.name", "CI")
                self.git(checkout, "commit", "--quiet", "--allow-empty", "-m", "concurrent commit")

            with mock.patch.object(assets, "copy_tree", side_effect=submodule_switching_copy):
                with self.assertRaisesRegex(assets.AssetError, "does not match parent gitlink"):
                    assets.copy_verified_byteff2(
                        parent,
                        workspace / "copied",
                        expected_commit=parent_commit,
                        expected_submodules={"vendor/child": child_commit},
                    )

    def test_checkout_validation_rejects_ignored_and_hidden_index_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "byteff2"
            self.initialize_repository(root)
            with (root / ".gitignore").open("a", encoding="utf-8") as ignore_file:
                ignore_file.write("ignored.bin\n")
            self.git(root, "add", ".gitignore")
            self.git(root, "commit", "--quiet", "-m", "ignore generated content")
            (root / "ignored.bin").write_bytes(b"not committed")
            with self.assertRaisesRegex(assets.AssetError, "must be clean"):
                assets.inspect_byteff2_checkout(root)

            (root / "ignored.bin").unlink()
            self.git(root, "update-index", "--skip-worktree", "tracked.txt")
            with self.assertRaisesRegex(assets.AssetError, "hidden index state"):
                assets.inspect_byteff2_checkout(root)


if __name__ == "__main__":
    unittest.main()

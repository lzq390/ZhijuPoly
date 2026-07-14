from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "bootstrap_asset_release.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_asset_release", SCRIPT)
assert SPEC and SPEC.loader
assets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(assets)


class AssetBootstrapTests(unittest.TestCase):
    @staticmethod
    def git(root: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()

    def initialize_repository(self, root: Path, filename: str = "tracked.txt") -> str:
        root.mkdir(parents=True)
        self.git(root, "init", "--quiet")
        self.git(root, "config", "user.email", "ci@example.invalid")
        self.git(root, "config", "user.name", "CI")
        (root / filename).write_text(f"{root.name}\n", encoding="utf-8")
        self.git(root, "add", filename)
        self.git(root, "commit", "--quiet", "-m", "initial")
        return self.git(root, "rev-parse", "HEAD")

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

    def test_manifest_digest_is_canonical(self) -> None:
        left = assets.canonical({"schema_version": 1, "assets": {"model": []}})
        right = assets.canonical({"assets": {"model": []}, "schema_version": 1})
        self.assertEqual(left, right)

    def test_manifest_records_byteff2_parent_and_submodule_commits(self) -> None:
        parent = "a" * 40
        child = "b" * 40
        manifest = assets.build_manifest(
            {"byteff2": []},
            byteff2_commit=parent,
            byteff2_submodules={"vendor/nested": child},
        )
        self.assertEqual(manifest["byteff2_commit"], parent)
        self.assertEqual(manifest["byteff2_submodules"], {"vendor/nested": child})

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
            (root / ".gitignore").write_text("ignored.bin\n", encoding="utf-8")
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

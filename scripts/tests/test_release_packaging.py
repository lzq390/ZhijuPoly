from __future__ import annotations

import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_SCRIPT = REPOSITORY_ROOT / "scripts" / "package_release.sh"
PACKAGE_HELPER = REPOSITORY_ROOT / "scripts" / "release_package.py"
MODEL_MANIFEST = REPOSITORY_ROOT / "backend" / "app" / "model_asset_manifest.py"

RELEASE_PACKAGE_SPEC = importlib.util.spec_from_file_location("nexpoly_release_package", PACKAGE_HELPER)
assert RELEASE_PACKAGE_SPEC is not None and RELEASE_PACKAGE_SPEC.loader is not None
RELEASE_PACKAGE_MODULE = importlib.util.module_from_spec(RELEASE_PACKAGE_SPEC)
RELEASE_PACKAGE_SPEC.loader.exec_module(RELEASE_PACKAGE_MODULE)

DATA_ASSETS = (
    "database/data1.csv",
    "database/data_txt.zip",
    "database/polymer_process_material_filtered_cleaned_office_utf8_bom.csv",
    "database/polymer_property_detail_cleaned_office_utf8_bom.csv",
    "database/PolymerDatabaseV2.0_reliable085_standardized.csv",
    "backend/data/polyprop.db",
    "backend/data/pi_reverse_design.db",
    "backend/data/fumol.db",
)


def run_command(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def release_model_assets() -> list[dict[str, str]]:
    result = run_command(
        ["python3", str(MODEL_MANIFEST), "--profile", "release", "--format", "json"],
        cwd=REPOSITORY_ROOT,
    )
    return json.loads(result.stdout)["assets"]


class ReleaseFixture:
    def __init__(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="nexpoly-release-test-")
        self.root = Path(self.temporary_directory.name) / "repository"
        self.root.mkdir()
        run_command(["git", "init", "-b", "main"], cwd=self.root)
        run_command(["git", "config", "user.name", "NexPoly Release Test"], cwd=self.root)
        run_command(["git", "config", "user.email", "release-test@example.invalid"], cwd=self.root)

        tracked_files = {
            ".gitignore": (
                "/release\n/model/*.pkl\n/model/ignored-*\n/model/conditional_generation/\n/model/polytao/\n"
                "/model/reactiont5-retrosynthesis\n/model/ocsr/*.pth\n"
                "/database/\n/backend/data/\n/.env*\n"
            ),
            ".nexpoly-release-test-fixture": "fixture\n",
            "Dockerfile": "FROM scratch\n",
            "frontend/Dockerfile": "FROM scratch\n",
            "docker-compose.yml": "services: {}\n",
            "nginx.conf": "events {}\n",
            "backend/.env.example": "ONLINE_KNOWLEDGE_API_KEY=\n",
            "frontend/.env.example": "VITE_EXAMPLE=\n",
            "frontend/package.json": '{"scripts":{"build":"false"}}\n',
            "model/ocsr/README.md": "tracked OCSR documentation\n",
        }
        for relative, content in tracked_files.items():
            self.write(relative, content.encode("utf-8"))

        self.copy(PACKAGE_SCRIPT, "scripts/package_release.sh", mode=0o755)
        self.copy(PACKAGE_HELPER, "scripts/release_package.py")
        self.copy(MODEL_MANIFEST, "backend/app/model_asset_manifest.py")
        run_command(["git", "add", "."], cwd=self.root)
        run_command(["git", "commit", "-m", "release fixture"], cwd=self.root)
        self.create_assets()

    def close(self) -> None:
        self.temporary_directory.cleanup()

    def write(self, relative: str, content: bytes = b"fixture-asset\n") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def copy(self, source: Path, relative: str, mode: int = 0o644) -> Path:
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(mode)
        return destination

    def create_assets(self) -> None:
        for asset in release_model_assets():
            if asset["kind"] == "file":
                self.write(asset["path"], f"model:{asset['path']}\n".encode("utf-8"))
            else:
                self.write(f"{asset['path']}/config.json", b'{"fixture":true}\n')
                (self.root / asset["path"] / "empty-directory").mkdir()
        for data_path in DATA_ASSETS:
            self.write(data_path, f"data:{data_path}\n".encode("utf-8"))

    def run_package(
        self,
        *,
        include_data: str = "0",
        extra_env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        environment = self.package_environment(include_data=include_data)
        if extra_env:
            environment.update(extra_env)
        return run_command(
            ["bash", "scripts/package_release.sh"],
            cwd=self.root,
            env=environment,
            check=check,
        )

    def package_environment(self, *, include_data: str = "0") -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "INCLUDE_DATA": include_data,
                "NEXPOLY_RELEASE_TEST_SKIP_BUILD": "1",
            }
        )
        environment.pop("RELEASE_ALLOWED_MODEL_ROOTS", None)
        environment.pop("RELEASE_ALLOWED_DATA_ROOTS", None)
        return environment

    def archive_path(self, include_data: str = "0", *, commit: str | None = None) -> Path:
        commit = commit or run_command(["git", "rev-parse", "HEAD"], cwd=self.root).stdout.strip()
        matches = sorted(
            (self.root / "release").glob(
                f"nexpoly-release-{commit[:12]}-data{include_data}-*.tar.gz"
            )
        )
        if len(matches) != 1:
            raise AssertionError(f"Expected one release archive for data{include_data}, found {matches}")
        return matches[0]

    def members(self, include_data: str = "0") -> set[str]:
        with tarfile.open(self.archive_path(include_data), "r:gz") as archive:
            return {member.name.rstrip("/") for member in archive.getmembers()}

    def manifest(self, include_data: str = "0") -> dict[str, object]:
        with tarfile.open(self.archive_path(include_data), "r:gz") as archive:
            source = archive.extractfile("RELEASE-MANIFEST.json")
            assert source is not None
            return json.load(source)


class ModelManifestCliTests(unittest.TestCase):
    def test_legacy_paths_and_release_json_contract(self) -> None:
        legacy = run_command(
            ["python3", str(MODEL_MANIFEST), "--format", "paths"],
            cwd=REPOSITORY_ROOT,
        ).stdout.splitlines()
        release_document = json.loads(
            run_command(
                ["python3", str(MODEL_MANIFEST), "--profile", "release", "--format", "json"],
                cwd=REPOSITORY_ROOT,
            ).stdout
        )
        assets = release_document["assets"]

        self.assertEqual(len(legacy), 21)
        self.assertEqual(legacy, [asset["path"] for asset in assets if asset["category"] == "required-model"])
        self.assertEqual(release_document["schema_version"], 1)
        self.assertEqual(
            {category: sum(asset["category"] == category for asset in assets) for category in {asset["category"] for asset in assets}},
            {"required-model": 21, "reactiont5": 1, "polytao": 4},
        )
        self.assertEqual({asset["kind"] for asset in assets if asset["category"] == "reactiont5"}, {"tree"})


class SecretScannerUnitTests(unittest.TestCase):
    def test_credential_keys_require_a_delimited_terminal_suffix(self) -> None:
        for key in (
            "API_KEY",
            "ONLINE_KNOWLEDGE_API_KEY",
            "ACCESS_TOKEN",
            "CLIENT_SECRET",
            "POSTGRES_PASSWORD",
            "SERVICE_CREDENTIAL",
            "CREDENTIALS",
        ):
            with self.subTest(key=key):
                self.assertTrue(RELEASE_PACKAGE_MODULE._is_credential_key(key))

        for key in (
            "IUPAC_TOKEN_SEPARATOR",
            "TOKEN_SEPARATOR",
            "PASSWORD_POLICY",
            "MONKEY_PATCH",
            "SECRETARY_NAME",
        ):
            with self.subTest(key=key):
                self.assertFalse(RELEASE_PACKAGE_MODULE._is_credential_key(key))

    def test_real_source_token_separator_is_not_reported_but_credentials_are(self) -> None:
        source_path = REPOSITORY_ROOT / "backend" / "app" / "services" / "knowledge_search.py"
        with source_path.open("rb") as source:
            self.assertEqual(RELEASE_PACKAGE_MODULE._secret_keys_in_stream(source, {}), set())

        positive_source = io.BytesIO(
            b"API_KEY=abcdefghijklmnop\n"
            b"POSTGRES_PASSWORD=0123456789abcdef\n"
            b"IUPAC_TOKEN_SEPARATOR=abcdefghijklmnop\n"
        )
        self.assertEqual(
            RELEASE_PACKAGE_MODULE._secret_keys_in_stream(positive_source, {}),
            {"API_KEY", "POSTGRES_PASSWORD"},
        )


class ReleasePackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ReleaseFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_data_modes_are_exact_and_ignored_extras_are_not_packaged(self) -> None:
        self.fixture.write("model/ignored-extra.bin", b"must-not-be-packaged\n")
        self.fixture.write(".env.dev.ai", b"IGNORED_API_KEY=ignored-secret-value-12345\n")

        self.fixture.run_package(include_data="0")
        members_without_data = self.fixture.members()
        manifest_without_data = self.fixture.manifest()

        self.assertNotIn("nexpoly-release", members_without_data)
        self.assertIn("model/ocsr/README.md", members_without_data)
        self.assertNotIn("model/ignored-extra.bin", members_without_data)
        self.assertFalse(any(member == "database" or member.startswith("database/") for member in members_without_data))
        self.assertFalse(any(member == "backend/data" or member.startswith("backend/data/") for member in members_without_data))
        self.assertFalse(any(".env.dev" in member for member in members_without_data))
        self.assertFalse(manifest_without_data["include_data"])
        self.assertNotIn("model/reactiont5-retrosynthesis/empty-directory", members_without_data)

        self.fixture.run_package(include_data="1")
        members_with_data = self.fixture.members("1")
        manifest_with_data = self.fixture.manifest("1")
        self.assertTrue(manifest_with_data["include_data"])
        self.assertTrue(set(DATA_ASSETS).issubset(members_with_data))
        data_manifest_paths = {
            entry["path"] for entry in manifest_with_data["files"] if entry["category"] == "data"
        }
        self.assertEqual(data_manifest_paths, set(DATA_ASSETS))

    def test_dirty_and_unignored_untracked_worktrees_are_rejected(self) -> None:
        self.fixture.write("Dockerfile", b"dirty\n")
        dirty_result = self.fixture.run_package(check=False)
        self.assertNotEqual(dirty_result.returncode, 0)
        self.assertIn("unstaged", dirty_result.stderr)

        run_command(["git", "restore", "Dockerfile"], cwd=self.fixture.root)
        self.fixture.write("Dockerfile", b"staged\n")
        run_command(["git", "add", "Dockerfile"], cwd=self.fixture.root)
        staged_result = self.fixture.run_package(check=False)
        self.assertNotEqual(staged_result.returncode, 0)
        self.assertIn("Staged changes", staged_result.stderr)

        run_command(["git", "restore", "--staged", "Dockerfile"], cwd=self.fixture.root)
        run_command(["git", "restore", "Dockerfile"], cwd=self.fixture.root)
        self.fixture.write("untracked-notes.txt", b"untracked\n")
        untracked_result = self.fixture.run_package(check=False)
        self.assertNotEqual(untracked_result.returncode, 0)
        self.assertIn("Unignored untracked", untracked_result.stderr)

    def test_assume_unchanged_and_skip_worktree_flags_are_rejected(self) -> None:
        required_path = release_model_assets()[0]["path"]
        run_command(["git", "add", "-f", required_path], cwd=self.fixture.root)
        run_command(["git", "commit", "-m", "track required model fixture"], cwd=self.fixture.root)
        head_bytes = (self.fixture.root / required_path).read_bytes()

        run_command(["git", "update-index", "--assume-unchanged", required_path], cwd=self.fixture.root)
        self.fixture.write(required_path, b"tampered-assume-unchanged\n")
        assume_result = self.fixture.run_package(check=False)
        self.assertNotEqual(assume_result.returncode, 0)
        self.assertIn("assume-unchanged or skip-worktree", assume_result.stderr)

        run_command(["git", "update-index", "--no-assume-unchanged", required_path], cwd=self.fixture.root)
        self.fixture.write(required_path, head_bytes)
        run_command(["git", "update-index", "--skip-worktree", required_path], cwd=self.fixture.root)
        self.fixture.write(required_path, b"tampered-skip-worktree\n")
        skip_result = self.fixture.run_package(check=False)
        self.assertNotEqual(skip_result.returncode, 0)
        self.assertIn("assume-unchanged or skip-worktree", skip_result.stderr)

    def test_git_replace_refs_are_rejected(self) -> None:
        original_commit = run_command(["git", "rev-parse", "HEAD"], cwd=self.fixture.root).stdout.strip()
        self.fixture.write("Dockerfile", b"replacement-tree\n")
        run_command(["git", "add", "Dockerfile"], cwd=self.fixture.root)
        replacement_tree = run_command(["git", "write-tree"], cwd=self.fixture.root).stdout.strip()
        replacement_commit = run_command(
            ["git", "commit-tree", replacement_tree, "-p", original_commit, "-m", "replacement"],
            cwd=self.fixture.root,
        ).stdout.strip()
        run_command(["git", "restore", "--staged", "Dockerfile"], cwd=self.fixture.root)
        run_command(["git", "restore", "Dockerfile"], cwd=self.fixture.root)
        run_command(["git", "replace", original_commit, replacement_commit], cwd=self.fixture.root)

        result = self.fixture.run_package(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Git replace refs are not allowed", result.stderr)

    def test_nonempty_local_git_attributes_and_grafts_are_rejected(self) -> None:
        for metadata_name in ("info/attributes", "info/grafts"):
            with self.subTest(metadata_name=metadata_name):
                raw_path = run_command(
                    ["git", "rev-parse", "--git-path", metadata_name],
                    cwd=self.fixture.root,
                ).stdout.strip()
                metadata_path = Path(raw_path)
                if not metadata_path.is_absolute():
                    metadata_path = self.fixture.root / metadata_path
                metadata_path.parent.mkdir(parents=True, exist_ok=True)
                metadata_path.write_text("local provenance override\n", encoding="utf-8")

                result = self.fixture.run_package(check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"Git {metadata_name}", result.stderr)
                metadata_path.unlink()

        raw_path = run_command(
            ["git", "rev-parse", "--git-path", "info/attributes"],
            cwd=self.fixture.root,
        ).stdout.strip()
        fifo_path = Path(raw_path)
        if not fifo_path.is_absolute():
            fifo_path = self.fixture.root / fifo_path
        fifo_path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(fifo_path)
        fifo_result = self.fixture.run_package(check=False)
        self.assertNotEqual(fifo_result.returncode, 0)
        self.assertIn("must be an empty regular file", fifo_result.stderr)

    def test_archive_environment_ignores_tar_gzip_and_git_config_injection(self) -> None:
        result = self.fixture.run_package(
            extra_env={
                "TAR_OPTIONS": "--definitely-invalid-release-test-option",
                "GZIP": "--definitely-invalid-release-test-option",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.attributesFile",
                "GIT_CONFIG_VALUE_0": "/definitely/missing/attributes",
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_and_empty_required_assets_are_rejected(self) -> None:
        required_path = release_model_assets()[0]["path"]
        (self.fixture.root / required_path).unlink()
        missing_result = self.fixture.run_package(check=False)
        self.assertNotEqual(missing_result.returncode, 0)
        self.assertIn("Missing, broken, or cyclic", missing_result.stderr)

        self.fixture.write(required_path, b"")
        empty_result = self.fixture.run_package(check=False)
        self.assertNotEqual(empty_result.returncode, 0)
        self.assertIn("Asset is empty", empty_result.stderr)

    def test_include_data_requires_all_nonempty_governed_data_assets(self) -> None:
        data_path = DATA_ASSETS[0]
        (self.fixture.root / data_path).unlink()
        missing_result = self.fixture.run_package(include_data="1", check=False)
        self.assertNotEqual(missing_result.returncode, 0)
        self.assertIn("Missing, broken, or cyclic", missing_result.stderr)

        self.fixture.write(data_path, b"")
        empty_result = self.fixture.run_package(include_data="1", check=False)
        self.assertNotEqual(empty_result.returncode, 0)
        self.assertIn("Asset is empty", empty_result.stderr)

        self.fixture.run_package(include_data="0")

    def test_explicit_external_symlink_requires_an_approved_root(self) -> None:
        required_path = release_model_assets()[0]["path"]
        entry = self.fixture.root / required_path
        entry.unlink()
        external_root = Path(self.fixture.temporary_directory.name) / "approved-models"
        external_root.mkdir()
        external_asset = external_root / "model.bin"
        external_asset.write_bytes(b"external-model\n")
        entry.symlink_to(external_asset)

        blocked_result = self.fixture.run_package(check=False)
        self.assertNotEqual(blocked_result.returncode, 0)
        self.assertIn("outside approved roots", blocked_result.stderr)

        self.fixture.run_package(extra_env={"RELEASE_ALLOWED_MODEL_ROOTS": str(external_root)})
        self.assertIn(required_path, self.fixture.members())

    def test_default_model_root_rejects_other_repository_private_files(self) -> None:
        required_path = release_model_assets()[0]["path"]
        entry = self.fixture.root / required_path
        entry.unlink()

        for target in (self.fixture.root / ".git" / "config", self.fixture.write(".env.private", b"private-model-bytes\n")):
            with self.subTest(target=target):
                entry.unlink(missing_ok=True)
                entry.symlink_to(target)
                result = self.fixture.run_package(check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("outside approved roots", result.stderr)

    def test_nested_symlink_in_model_tree_is_rejected(self) -> None:
        reaction_tree = self.fixture.root / "model" / "reactiont5-retrosynthesis"
        external = Path(self.fixture.temporary_directory.name) / "nested-model.bin"
        external.write_bytes(b"nested\n")
        (reaction_tree / "nested-link.bin").symlink_to(external)

        result = self.fixture.run_package(
            extra_env={"RELEASE_ALLOWED_MODEL_ROOTS": str(external.parent)},
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Nested symlink", result.stderr)

    def test_reaction_tree_root_symlink_requires_an_approved_root(self) -> None:
        reaction_tree = self.fixture.root / "model" / "reactiont5-retrosynthesis"
        shutil.rmtree(reaction_tree)
        external_root = Path(self.fixture.temporary_directory.name) / "approved-reaction-models"
        external_tree = external_root / "reactiont5-retrosynthesis"
        external_tree.mkdir(parents=True)
        (external_tree / "config.json").write_bytes(b'{"external":true}\n')
        reaction_tree.symlink_to(external_tree, target_is_directory=True)

        blocked_result = self.fixture.run_package(check=False)
        self.assertNotEqual(blocked_result.returncode, 0)
        self.assertIn("outside approved roots", blocked_result.stderr)

        self.fixture.run_package(extra_env={"RELEASE_ALLOWED_MODEL_ROOTS": str(external_root)})
        self.assertIn("model/reactiont5-retrosynthesis/config.json", self.fixture.members())

    def test_local_secret_value_in_payload_is_rejected_without_value_disclosure(self) -> None:
        secret_value = "fixture-secret-value-123456789"
        self.fixture.write(".env.dev.ai", f"ONLINE_KNOWLEDGE_API_KEY={secret_value}\n".encode("utf-8"))
        self.fixture.write("database/data1.csv", f"header\n{secret_value}\n".encode("utf-8"))

        result = self.fixture.run_package(include_data="1", check=False)
        combined_output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ONLINE_KNOWLEDGE_API_KEY", combined_output)
        self.assertIn("database/data1.csv", combined_output)
        self.assertNotIn(secret_value, combined_output)

    def test_symlinked_local_env_secret_is_scanned(self) -> None:
        secret_value = "symlinked-secret-value-123456789"
        external_env = Path(self.fixture.temporary_directory.name) / "external.env"
        external_env.write_text(f"ONLINE_KNOWLEDGE_API_KEY={secret_value}\n", encoding="utf-8")
        (self.fixture.root / ".env.dev.ai").symlink_to(external_env)
        self.fixture.write("database/data1.csv", f"header\n{secret_value}\n".encode("utf-8"))

        result = self.fixture.run_package(include_data="1", check=False)
        combined_output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ONLINE_KNOWLEDGE_API_KEY", combined_output)
        self.assertIn("database/data1.csv", combined_output)
        self.assertNotIn(secret_value, combined_output)

    def test_broken_and_cyclic_local_env_links_are_rejected(self) -> None:
        env_entry = self.fixture.root / ".env.ai"
        env_entry.symlink_to(Path(self.fixture.temporary_directory.name) / "missing.env")
        broken_result = self.fixture.run_package(check=False)
        self.assertNotEqual(broken_result.returncode, 0)
        self.assertIn("Missing, broken, or cyclic local environment file: .env.ai", broken_result.stderr)

        env_entry.unlink()
        env_entry.symlink_to(env_entry)
        cyclic_result = self.fixture.run_package(check=False)
        self.assertNotEqual(cyclic_result.returncode, 0)
        self.assertIn("Missing, broken, or cyclic local environment file: .env.ai", cyclic_result.stderr)

    def test_archive_is_reproducible_for_the_same_head_and_assets(self) -> None:
        self.fixture.run_package(include_data="1")
        first_digest = hashlib.sha256(self.fixture.archive_path("1").read_bytes()).hexdigest()
        first_checksum = self.fixture.archive_path("1").with_name(f"{self.fixture.archive_path('1').name}.sha256").read_text()

        self.fixture.run_package(include_data="1")
        second_digest = hashlib.sha256(self.fixture.archive_path("1").read_bytes()).hexdigest()
        second_checksum = self.fixture.archive_path("1").with_name(f"{self.fixture.archive_path('1').name}.sha256").read_text()

        self.assertEqual(first_digest, second_digest)
        self.assertEqual(first_checksum, second_checksum)
        self.assertEqual(first_checksum, f"{first_digest}  {self.fixture.archive_path('1').name}\n")

    def test_gzip_environment_cannot_change_reproducible_output(self) -> None:
        self.fixture.run_package(extra_env={"GZIP": "-1"})
        first_archive = self.fixture.archive_path()
        first_digest = hashlib.sha256(first_archive.read_bytes()).hexdigest()

        self.fixture.run_package(extra_env={"GZIP": "-9"})
        second_archive = self.fixture.archive_path()
        second_digest = hashlib.sha256(second_archive.read_bytes()).hexdigest()

        self.assertEqual(first_archive, second_archive)
        self.assertEqual(first_digest, second_digest)

    def test_archive_and_sidecar_tampering_break_checksum_verification(self) -> None:
        self.fixture.run_package(include_data="0")
        archive_path = self.fixture.archive_path()
        checksum_path = archive_path.with_name(f"{archive_path.name}.sha256")

        valid_result = run_command(
            ["sha256sum", "--check", checksum_path.name],
            cwd=checksum_path.parent,
            check=False,
        )
        self.assertEqual(valid_result.returncode, 0)

        with archive_path.open("ab") as archive:
            archive.write(b"tampered")
        archive_tamper_result = run_command(
            ["sha256sum", "--check", checksum_path.name],
            cwd=checksum_path.parent,
            check=False,
        )
        self.assertNotEqual(archive_tamper_result.returncode, 0)

        archive_conflict_result = self.fixture.run_package(include_data="0", check=False)
        self.assertNotEqual(archive_conflict_result.returncode, 0)
        self.assertIn("conflicts with the deterministic candidate", archive_conflict_result.stderr)
        archive_path.unlink()
        checksum_path.unlink()
        self.fixture.run_package(include_data="0")
        archive_path = self.fixture.archive_path()
        checksum_path = archive_path.with_name(f"{archive_path.name}.sha256")
        checksum_path.write_text(f"{'0' * 64}  {archive_path.name}\n", encoding="ascii")
        sidecar_tamper_result = run_command(
            ["sha256sum", "--check", checksum_path.name],
            cwd=checksum_path.parent,
            check=False,
        )
        self.assertNotEqual(sidecar_tamper_result.returncode, 0)

        checksum_conflict_result = self.fixture.run_package(include_data="0", check=False)
        self.assertNotEqual(checksum_conflict_result.returncode, 0)
        self.assertIn("checksum conflicts", checksum_conflict_result.stderr)

    def test_release_output_path_rejects_symlinks_and_non_directories(self) -> None:
        release_path = self.fixture.root / "release"
        outside_directory = Path(self.fixture.temporary_directory.name) / "outside-release"
        outside_directory.mkdir()
        release_path.symlink_to(outside_directory, target_is_directory=True)

        symlink_result = self.fixture.run_package(check=False)
        self.assertNotEqual(symlink_result.returncode, 0)
        self.assertIn("must be a real directory", symlink_result.stderr)
        self.assertEqual(list(outside_directory.iterdir()), [])

        release_path.unlink()
        release_path.write_bytes(b"not-a-directory\n")
        file_result = self.fixture.run_package(check=False)
        self.assertNotEqual(file_result.returncode, 0)
        self.assertIn("must be a real directory", file_result.stderr)

    def test_legacy_predictable_temp_symlink_is_never_followed(self) -> None:
        release_path = self.fixture.root / "release"
        release_path.mkdir()
        victim = Path(self.fixture.temporary_directory.name) / "victim.txt"
        victim.write_bytes(b"do-not-overwrite\n")
        commit = run_command(["git", "rev-parse", "HEAD"], cwd=self.fixture.root).stdout.strip()
        legacy_temp = release_path / f".nexpoly-release-{commit[:12]}.tar.gz.{os.getpid()}.tmp"
        legacy_temp.symlink_to(victim)

        self.fixture.run_package(include_data="0")
        self.assertTrue(legacy_temp.is_symlink())
        self.assertEqual(victim.read_bytes(), b"do-not-overwrite\n")
        private_temp_directories = [
            path for path in release_path.iterdir() if path.name.startswith(".nexpoly-release-output-")
        ]
        self.assertEqual(private_temp_directories, [])

    def test_concurrent_package_attempt_fails_fast_under_lock(self) -> None:
        first_environment = self.fixture.package_environment()
        first_environment["NEXPOLY_RELEASE_TEST_HOLD_LOCK_SECONDS"] = "1"
        first = subprocess.Popen(
            ["bash", "scripts/package_release.sh"],
            cwd=self.fixture.root,
            env=first_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        lock_path = self.fixture.root / "release" / ".package-release.lock"
        deadline = time.monotonic() + 5
        while not lock_path.exists() and first.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(lock_path.exists())
        time.sleep(0.05)

        second = self.fixture.run_package(check=False)
        first_stdout, first_stderr = first.communicate(timeout=10)
        self.assertEqual(first.returncode, 0, first_stdout + first_stderr)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("Another release packaging process is already running", second.stderr)

        archive_path = self.fixture.archive_path()
        checksum_path = archive_path.with_name(f"{archive_path.name}.sha256")
        verified = run_command(
            ["sha256sum", "--check", checksum_path.name],
            cwd=checksum_path.parent,
            check=False,
        )
        self.assertEqual(verified.returncode, 0)

    def test_head_change_while_locked_still_packages_the_captured_commit(self) -> None:
        old_commit = run_command(["git", "rev-parse", "HEAD"], cwd=self.fixture.root).stdout.strip()
        old_dockerfile = (self.fixture.root / "Dockerfile").read_bytes()
        environment = self.fixture.package_environment()
        environment["NEXPOLY_RELEASE_TEST_HOLD_LOCK_SECONDS"] = "1"
        process = subprocess.Popen(
            ["bash", "scripts/package_release.sh"],
            cwd=self.fixture.root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        lock_path = self.fixture.root / "release" / ".package-release.lock"
        deadline = time.monotonic() + 5
        while not lock_path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(lock_path.exists())
        time.sleep(0.05)

        self.fixture.write("Dockerfile", b"new-head\n")
        run_command(["git", "add", "Dockerfile"], cwd=self.fixture.root)
        run_command(["git", "commit", "-m", "advance head during packaging"], cwd=self.fixture.root)

        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        archive_path = self.fixture.archive_path(commit=old_commit)
        with tarfile.open(archive_path, "r:gz") as archive:
            dockerfile = archive.extractfile("Dockerfile")
            manifest_file = archive.extractfile("RELEASE-MANIFEST.json")
            assert dockerfile is not None and manifest_file is not None
            self.assertEqual(dockerfile.read(), old_dockerfile)
            self.assertEqual(json.load(manifest_file)["commit"], old_commit)

    def test_noncanonical_release_paths_are_rejected(self) -> None:
        for path in ("./README.md", "a//b", "a/./b", "README.md/"):
            with self.subTest(path=path), self.assertRaises(RELEASE_PACKAGE_MODULE.ReleaseError):
                RELEASE_PACKAGE_MODULE._validated_relative(path)

    def test_duplicate_manifest_paths_are_rejected(self) -> None:
        self.fixture.run_package(include_data="0")
        manifest = self.fixture.manifest()
        entries = manifest["files"]
        assert isinstance(entries, list) and entries
        entries.append(dict(entries[0]))
        epoch = int(run_command(["git", "show", "-s", "--format=%ct", "HEAD"], cwd=self.fixture.root).stdout)

        with self.assertRaisesRegex(RELEASE_PACKAGE_MODULE.ReleaseError, "duplicate path"):
            RELEASE_PACKAGE_MODULE._validate_archive(
                self.fixture.archive_path(),
                manifest,
                head_epoch=epoch,
            )

    def test_include_data_accepts_only_zero_or_one(self) -> None:
        result = self.fixture.run_package(include_data="yes", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("INCLUDE_DATA must be exactly 0 or 1", result.stderr)


if __name__ == "__main__":
    unittest.main()

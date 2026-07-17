from __future__ import annotations

import importlib.util
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "maintenance_prefetch_test",
    SCRIPTS / "maintenance_prefetch.py",
)
assert SPEC is not None and SPEC.loader is not None
PREFETCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREFETCH)


AUTHORITY_SHA = "a" * 40
AUTHORITY_TREE = "b" * 40
TARGET_SHA = "c" * 40
TARGET_TREE = "d" * 40
DIGEST_A = "sha256:" + "1" * 64
DIGEST_B = "sha256:" + "2" * 64
DIGEST_C = "sha256:" + "3" * 64
DIGEST_D = "sha256:" + "4" * 64
LOCAL_IMAGE_ID = "sha256:" + "e" * 64
OPERATION_ID = "prefetch-20260717-complete"


def image_record(
    reference: str,
    *,
    revision: str | None,
    local_id: str = LOCAL_IMAGE_ID,
) -> dict[str, object]:
    return {
        "digest_ref": reference,
        "oci_reference_digest": reference.split("@", 1)[1],
        "local_image_id": local_id,
        "repo_digests": [PREFETCH.canonical_repo_digest(reference)],
        "revision": revision,
        "source": PREFETCH.SOURCE_URL if revision is not None else None,
        "version": f"sha-{revision}" if revision is not None else None,
    }


class MaintenancePrefetchPrimitiveTests(unittest.TestCase):
    def test_image_evidence_separates_oci_digest_from_local_image_id(self) -> None:
        reference = (
            f"{PREFETCH.ROLE_IMAGE_ROOTS['backend']}@{DIGEST_A}"
        )
        record = image_record(
            reference,
            revision=AUTHORITY_SHA,
            local_id=LOCAL_IMAGE_ID,
        )

        validated = PREFETCH.validate_image_evidence(
            record,
            expected_reference=reference,
            expected_revision=AUTHORITY_SHA,
        )

        self.assertEqual(validated["oci_reference_digest"], DIGEST_A)
        self.assertEqual(validated["local_image_id"], LOCAL_IMAGE_ID)
        self.assertNotEqual(
            validated["oci_reference_digest"],
            validated["local_image_id"],
        )

        confused = dict(record)
        confused["local_image_id"] = DIGEST_A
        # Equal values are syntactically possible, but the RepoDigest remains
        # mandatory and the evidence keeps two explicitly typed fields.
        self.assertEqual(
            PREFETCH.validate_image_evidence(
                confused,
                expected_reference=reference,
                expected_revision=AUTHORITY_SHA,
            )["local_image_id"],
            DIGEST_A,
        )
        missing_repo_digest = dict(record)
        missing_repo_digest["repo_digests"] = []
        with self.assertRaisesRegex(
            PREFETCH.MaintenancePrefetchError,
            "RepoDigests",
        ):
            PREFETCH.validate_image_evidence(
                missing_repo_digest,
                expected_reference=reference,
                expected_revision=AUTHORITY_SHA,
            )

    def test_postgres_tagged_reference_uses_canonical_repo_digest(self) -> None:
        reference = PREFETCH.POSTGRES16_IMAGE
        record = image_record(reference, revision=None)
        self.assertNotEqual(record["repo_digests"], [reference])
        validated = PREFETCH.validate_image_evidence(
            record,
            expected_reference=reference,
            expected_revision=None,
            enforce_revision=False,
        )
        self.assertEqual(
            validated["repo_digests"],
            [PREFETCH.canonical_repo_digest(reference)],
        )

    def test_wheel_cache_key_binds_raw_lock_base_python_and_platform(self) -> None:
        first = PREFETCH.wheel_cache_key(
            b"package==1\\\n  --hash=sha256:" + b"a" * 64 + b"\n",
            base_python_identity_sha256=DIGEST_A,
            platform="linux",
        )
        same = PREFETCH.wheel_cache_key(
            b"package==1\\\n  --hash=sha256:" + b"a" * 64 + b"\n",
            base_python_identity_sha256=DIGEST_A,
            platform="linux",
        )
        another_python = PREFETCH.wheel_cache_key(
            b"package==1\\\n  --hash=sha256:" + b"a" * 64 + b"\n",
            base_python_identity_sha256=DIGEST_B,
            platform="linux",
        )
        another_platform = PREFETCH.wheel_cache_key(
            b"package==1\\\n  --hash=sha256:" + b"a" * 64 + b"\n",
            base_python_identity_sha256=DIGEST_A,
            platform="darwin",
        )

        self.assertEqual(first, same)
        self.assertNotEqual(
            first["wheel_cache_key"],
            another_python["wheel_cache_key"],
        )
        self.assertNotEqual(
            first["wheel_cache_key"],
            another_platform["wheel_cache_key"],
        )

    def test_atomic_json_is_owner_private_and_replaces_complete_payload(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            os.chmod(root, 0o700)
            path = root / "ready.json"

            PREFETCH.atomic_json(path, {"generation": 1})
            PREFETCH.atomic_json(path, {"generation": 2})

            self.assertEqual(json.loads(path.read_text()), {"generation": 2})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                list(root.glob(".ready.json.*.tmp")),
                [],
            )

    def test_private_inventory_rejects_symlink_and_group_readable_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            os.chmod(root, 0o700)
            safe = root / "wheel.whl"
            safe.write_bytes(b"wheel")
            os.chmod(safe, 0o600)
            digest, count = PREFETCH.directory_inventory_digest(root)
            self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(count, 1)

            os.chmod(safe, 0o640)
            with self.assertRaisesRegex(
                PREFETCH.MaintenancePrefetchError,
                "unsafe",
            ):
                PREFETCH.directory_inventory_digest(root)
            os.chmod(safe, 0o600)
            (root / "link").symlink_to(safe)
            with self.assertRaisesRegex(
                PREFETCH.MaintenancePrefetchError,
                "symlink",
            ):
                PREFETCH.directory_inventory_digest(root)

    def test_private_inventory_binds_executable_mode(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            os.chmod(root, 0o700)
            executable = root / "tool"
            executable.write_bytes(b"tool\n")
            os.chmod(executable, 0o600)
            first, _ = PREFETCH.directory_inventory_digest(root)
            os.chmod(executable, 0o700)
            second, _ = PREFETCH.directory_inventory_digest(root)
            self.assertNotEqual(first, second)

    def test_wheel_completion_rejects_extra_or_tampered_archive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            os.chmod(root, 0o700)
            owner = root / ".owner.json"
            owner.write_text("{}\n", encoding="utf-8")
            os.chmod(owner, 0o600)
            lock = root / "requirements.lock"
            lock.write_bytes(b"fixture==1\n")
            os.chmod(lock, 0o600)
            package = b"VALUE = 1\n"
            encoded = PREFETCH.base64.urlsafe_b64encode(
                hashlib.sha256(package).digest()
            ).decode("ascii").rstrip("=")
            record_path = "fixture-1.dist-info/RECORD"
            record = (
                f"fixture/__init__.py,sha256={encoded},{len(package)}\n"
                f"{record_path},,\n"
            ).encode("utf-8")
            wheel = root / "fixture-1-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("fixture/__init__.py", package)
                archive.writestr(record_path, record)
            os.chmod(wheel, 0o600)
            wheel_evidence = PREFETCH.wheel_archive_evidence(wheel)
            complete = root / ".complete.json"
            completion = {
                "schema_version": 1,
                "wheel_cache_key": DIGEST_A,
                "worker_lock_sha256": PREFETCH.sha256_file(lock),
                "base_python_identity_sha256": DIGEST_B,
                "offline_install_verified": True,
                "pip_check_verified": True,
                "wheels": [wheel_evidence],
            }
            PREFETCH.atomic_json(complete, completion)

            PREFETCH.validate_wheel_cache_completion(
                root,
                wheel_cache_key_value=DIGEST_A,
                worker_lock_sha256=PREFETCH.sha256_file(lock),
                base_python_identity_sha256=DIGEST_B,
            )
            extra = root / "extra.whl"
            extra.write_bytes(b"not a wheel")
            os.chmod(extra, 0o600)
            with self.assertRaises(PREFETCH.MaintenancePrefetchError):
                PREFETCH.validate_wheel_cache_completion(
                    root,
                    wheel_cache_key_value=DIGEST_A,
                    worker_lock_sha256=PREFETCH.sha256_file(lock),
                    base_python_identity_sha256=DIGEST_B,
                )

    def test_bundle_rejects_advertised_main_drift(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "source"
            root.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", root], check=True)
            subprocess.run(
                ["git", "-C", root, "config", "user.name", "Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", root, "config", "user.email", "test@example.invalid"],
                check=True,
            )
            (root / "tracked").write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "-C", root, "add", "tracked"], check=True)
            subprocess.run(["git", "-C", root, "commit", "-qm", "one"], check=True)
            first = (
                subprocess.run(
                    ["git", "-C", root, "rev-parse", "HEAD"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                )
                .stdout.strip()
            )
            first_tree = (
                subprocess.run(
                    ["git", "-C", root, "rev-parse", "HEAD^{tree}"],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                )
                .stdout.strip()
            )
            bundle = Path(raw) / "main.bundle"
            subprocess.run(
                ["git", "-C", root, "bundle", "create", bundle, "refs/heads/main"],
                check=True,
            )
            os.chmod(bundle, 0o600)
            controller = PREFETCH.MaintenancePrefetch(
                source_root=root,
                runtime_root=Path(raw) / "runtime",
                operation_id=OPERATION_ID,
                authority_images={
                    "backend": (
                        f"{PREFETCH.ROLE_IMAGE_ROOTS['backend']}@{DIGEST_A}"
                    ),
                    "web": f"{PREFETCH.ROLE_IMAGE_ROOTS['web']}@{DIGEST_B}",
                },
                docker_config=Path(raw) / "runtime/config/docker",
                base_python=Path("/private/python"),
                base_python_identity_sha256=DIGEST_A,
                allow_test=True,
            )
            with self.assertRaisesRegex(
                PREFETCH.MaintenancePrefetchError,
                "advertised main",
            ):
                controller._verify_bundle(
                    bundle,
                    authority={"sha": "f" * 40, "tree": first_tree},
                    target={"sha": first, "tree": first_tree},
                )


class MaintenancePrefetchEvidenceTests(unittest.TestCase):
    def _policy(self) -> dict[str, object]:
        return {
            "target_sha": TARGET_SHA,
            "target_tree": TARGET_TREE,
            "target_images": {
                "backend": (
                    f"{PREFETCH.ROLE_IMAGE_ROOTS['backend']}@{DIGEST_C}"
                ),
                "web": f"{PREFETCH.ROLE_IMAGE_ROOTS['web']}@{DIGEST_D}",
            },
            "asset_manifest_digest": DIGEST_A,
        }

    def _document(self) -> dict[str, object]:
        policy = self._policy()
        source_readiness = {
            "schema_version": 2,
            "ready": True,
            "source_root": "/private/source",
            "source_sha": AUTHORITY_SHA,
            "source_tree": AUTHORITY_TREE,
            "branch": "main",
            "origin": PREFETCH.bootstrap_pull_deploy.REPOSITORY_SSH_URL,
            "remote_names": ["origin"],
            "origin_fetch_urls": [
                PREFETCH.bootstrap_pull_deploy.REPOSITORY_SSH_URL
            ],
            "origin_push_urls": [
                PREFETCH.bootstrap_pull_deploy.REPOSITORY_SSH_URL
            ],
            "origin_main_sha": AUTHORITY_SHA,
            "standalone_object_database": True,
            "shallow": False,
            "dirty_entries": 0,
            "ignored_entries": 0,
            "unreachable_objects": 0,
            "replace_refs": 0,
            "special_index_entries": 0,
            "sparse_index": False,
            "owner_private": True,
            "group_or_world_writable": False,
        }
        authority_backend = (
            f"{PREFETCH.ROLE_IMAGE_ROOTS['backend']}@{DIGEST_A}"
        )
        authority_web = (
            f"{PREFETCH.ROLE_IMAGE_ROOTS['web']}@{DIGEST_B}"
        )
        document: dict[str, object] = {
            "schema_version": PREFETCH.PREFETCH_SCHEMA_VERSION,
            "status": PREFETCH.PREFETCH_STATUS,
            "operation_id": OPERATION_ID,
            "source": {
                "authority": {
                    "sha": AUTHORITY_SHA,
                    "tree": AUTHORITY_TREE,
                },
                "target": {"sha": TARGET_SHA, "tree": TARGET_TREE},
            },
            "source_readiness": source_readiness,
            "source_readiness_sha256": PREFETCH.sha256_bytes(
                PREFETCH.canonical_json_bytes(source_readiness)
            ),
            "controller": {
                "source_root": "/private/source",
                "source_sha": AUTHORITY_SHA,
                "source_tree": AUTHORITY_TREE,
                "files": {
                    relative: DIGEST_A
                    for relative in PREFETCH.PREFETCH_CONTROLLER_PATHS
                },
            },
            "policy": policy,
            "policy_sha256": PREFETCH.sha256_bytes(
                PREFETCH.canonical_json_bytes(policy)
            ),
            "docker_config": {
                "path": "/private/runtime/config/docker",
            },
            "git_bundle": {"mocked": True},
            "images": {
                "authority": {
                    "backend": image_record(
                        authority_backend,
                        revision=AUTHORITY_SHA,
                    ),
                    "web": image_record(
                        authority_web,
                        revision=AUTHORITY_SHA,
                    ),
                },
                "target": {
                    "backend": image_record(
                        str(policy["target_images"]["backend"]),
                        revision=TARGET_SHA,
                    ),
                    "web": image_record(
                        str(policy["target_images"]["web"]),
                        revision=TARGET_SHA,
                    ),
                },
                "postgres_restore": image_record(
                    PREFETCH.POSTGRES16_IMAGE,
                    revision=None,
                ),
            },
            "wheel_caches": [
                {"source_sha": AUTHORITY_SHA},
                {"source_sha": TARGET_SHA},
            ],
            "asset": {"mocked": True},
            "recovery_tools": {"mocked": True},
            "created_at": "2026-07-17T00:00:00Z",
            "identity_sha256": "",
        }
        document["identity_sha256"] = PREFETCH.sha256_bytes(
            PREFETCH.canonical_json_bytes(PREFETCH.ready_identity(document))
        )
        return document

    def test_ready_evidence_binds_every_artifact_class(self) -> None:
        document = self._document()
        wheel_outputs = [
            {
                "source_sha": AUTHORITY_SHA,
            },
            {
                "source_sha": TARGET_SHA,
            },
        ]
        with (
            mock.patch.object(
                PREFETCH.bridge_deploy_core,
                "validate_policy",
                return_value=self._policy(),
            ),
            mock.patch.object(PREFETCH, "validate_git_bundle_evidence"),
            mock.patch.object(PREFETCH, "validate_controller_evidence"),
            mock.patch.object(PREFETCH, "validate_asset_evidence"),
            mock.patch.object(PREFETCH, "validate_recovery_tools"),
            mock.patch.object(PREFETCH, "require_private_directory"),
            mock.patch.object(PREFETCH, "require_private_file"),
            mock.patch.object(PREFETCH, "sha256_file", return_value=DIGEST_B),
            mock.patch.object(
                PREFETCH,
                "validate_wheel_record",
                side_effect=wheel_outputs,
            ),
        ):
            validated = PREFETCH.validate_ready_evidence(
                document,
                runtime_root=Path("/private/runtime"),
            )
        self.assertEqual(validated["identity_sha256"], document["identity_sha256"])

    def test_ready_evidence_rejects_tampering_and_missing_f_or_b_wheels(self) -> None:
        document = self._document()
        document["created_at"] = "2026-07-17T00:00:01Z"
        # Timestamp and every artifact field are part of the stable identity.
        document["images"]["target"]["backend"]["local_image_id"] = (
            "sha256:" + "9" * 64
        )
        with (
            mock.patch.object(
                PREFETCH.bridge_deploy_core,
                "validate_policy",
                return_value=self._policy(),
            ),
            mock.patch.object(PREFETCH, "validate_git_bundle_evidence"),
            mock.patch.object(PREFETCH, "validate_controller_evidence"),
            mock.patch.object(PREFETCH, "validate_asset_evidence"),
            mock.patch.object(PREFETCH, "validate_recovery_tools"),
            mock.patch.object(PREFETCH, "require_private_directory"),
            mock.patch.object(PREFETCH, "require_private_file"),
            mock.patch.object(PREFETCH, "sha256_file", return_value=DIGEST_B),
            mock.patch.object(
                PREFETCH,
                "validate_wheel_record",
                side_effect=[
                    {"source_sha": AUTHORITY_SHA},
                    {"source_sha": TARGET_SHA},
                ],
            ),
        ):
            with self.assertRaisesRegex(
                PREFETCH.MaintenancePrefetchError,
                "identity differs",
            ):
                PREFETCH.validate_ready_evidence(
                    document,
                    runtime_root=Path("/private/runtime"),
                )

        missing = self._document()
        missing["wheel_caches"] = [{"source_sha": AUTHORITY_SHA}]
        missing["identity_sha256"] = PREFETCH.sha256_bytes(
            PREFETCH.canonical_json_bytes(PREFETCH.ready_identity(missing))
        )
        with (
            mock.patch.object(
                PREFETCH.bridge_deploy_core,
                "validate_policy",
                return_value=self._policy(),
            ),
            mock.patch.object(PREFETCH, "validate_git_bundle_evidence"),
            mock.patch.object(PREFETCH, "validate_controller_evidence"),
            mock.patch.object(PREFETCH, "validate_asset_evidence"),
            mock.patch.object(PREFETCH, "validate_recovery_tools"),
            mock.patch.object(PREFETCH, "require_private_directory"),
            mock.patch.object(PREFETCH, "require_private_file"),
            mock.patch.object(PREFETCH, "sha256_file", return_value=DIGEST_B),
            mock.patch.object(
                PREFETCH,
                "validate_wheel_record",
                return_value={"source_sha": AUTHORITY_SHA},
            ),
        ):
            with self.assertRaisesRegex(
                PREFETCH.MaintenancePrefetchError,
                "wheel",
            ):
                PREFETCH.validate_ready_evidence(
                    missing,
                    runtime_root=Path("/private/runtime"),
                )

    def test_constructor_rejects_mutable_tags_and_partial_image_sets(self) -> None:
        with self.assertRaisesRegex(
            PREFETCH.MaintenancePrefetchError,
            "incomplete",
        ):
            PREFETCH.MaintenancePrefetch(
                source_root=Path("/private/source"),
                runtime_root=Path("/private/runtime"),
                operation_id=OPERATION_ID,
                authority_images={
                    "backend": (
                        f"{PREFETCH.ROLE_IMAGE_ROOTS['backend']}@{DIGEST_A}"
                    )
                },
                docker_config=Path("/private/runtime/config/docker"),
                base_python=Path("/private/python"),
                base_python_identity_sha256=DIGEST_A,
                allow_test=True,
            )
        with self.assertRaisesRegex(
            PREFETCH.MaintenancePrefetchError,
            "digest",
        ):
            PREFETCH.MaintenancePrefetch(
                source_root=Path("/private/source"),
                runtime_root=Path("/private/runtime"),
                operation_id=OPERATION_ID,
                authority_images={
                    "backend": (
                        f"{PREFETCH.ROLE_IMAGE_ROOTS['backend']}:latest"
                    ),
                    "web": f"{PREFETCH.ROLE_IMAGE_ROOTS['web']}@{DIGEST_B}",
                },
                docker_config=Path("/private/runtime/config/docker"),
                base_python=Path("/private/python"),
                base_python_identity_sha256=DIGEST_A,
                allow_test=True,
            )

    def test_shared_deploy_lock_blocks_before_any_prefetch_action(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "runtime"
            state = runtime / "state"
            docker_config = runtime / "config/docker"
            state.mkdir(parents=True, mode=0o700)
            docker_config.mkdir(parents=True, mode=0o700)
            for directory in (runtime, runtime / "config", state, docker_config):
                os.chmod(directory, 0o700)
            lock = state / "deploy.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o600)
            config = docker_config / "config.json"
            config.write_text('{"auths":{}}\n', encoding="utf-8")
            os.chmod(config, 0o600)
            controller = PREFETCH.MaintenancePrefetch(
                source_root=Path("/private/source"),
                runtime_root=runtime,
                operation_id=OPERATION_ID,
                authority_images={
                    "backend": (
                        f"{PREFETCH.ROLE_IMAGE_ROOTS['backend']}@{DIGEST_A}"
                    ),
                    "web": f"{PREFETCH.ROLE_IMAGE_ROOTS['web']}@{DIGEST_B}",
                },
                docker_config=docker_config,
                base_python=Path("/private/python"),
                base_python_identity_sha256=DIGEST_A,
                allow_test=True,
            )
            with lock.open("r+b", buffering=0) as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with (
                    mock.patch.object(
                        controller,
                        "_source_and_policy",
                    ) as source,
                    self.assertRaisesRegex(
                        PREFETCH.MaintenancePrefetchError,
                        "deploy.lock",
                    ),
                ):
                    controller.run()
            source.assert_not_called()
            self.assertFalse(controller.ready_path.exists())
            self.assertFalse(controller.prefetch_root.exists())

    def test_failed_prefetch_never_publishes_a_partial_ready_record(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "runtime"
            state = runtime / "state"
            docker_config = runtime / "config/docker"
            state.mkdir(parents=True, mode=0o700)
            docker_config.mkdir(parents=True, mode=0o700)
            for directory in (runtime, runtime / "config", state, docker_config):
                os.chmod(directory, 0o700)
            lock = state / "deploy.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o600)
            config = docker_config / "config.json"
            config.write_text('{"auths":{}}\n', encoding="utf-8")
            os.chmod(config, 0o600)
            controller = PREFETCH.MaintenancePrefetch(
                source_root=Path("/private/source"),
                runtime_root=runtime,
                operation_id=OPERATION_ID,
                authority_images={
                    "backend": (
                        f"{PREFETCH.ROLE_IMAGE_ROOTS['backend']}@{DIGEST_A}"
                    ),
                    "web": f"{PREFETCH.ROLE_IMAGE_ROOTS['web']}@{DIGEST_B}",
                },
                docker_config=docker_config,
                base_python=Path("/private/python"),
                base_python_identity_sha256=DIGEST_A,
                allow_test=True,
            )
            policy = self._policy()
            with (
                mock.patch.object(
                    controller,
                    "_source_and_policy",
                    return_value=(
                        {"ready": True},
                        {"sha": AUTHORITY_SHA, "tree": AUTHORITY_TREE},
                        {"sha": TARGET_SHA, "tree": TARGET_TREE},
                        policy,
                    ),
                ),
                mock.patch.object(
                    controller,
                    "_publish_bundle",
                    return_value={"complete": True},
                ),
                mock.patch.object(
                    controller,
                    "_controller_evidence",
                    return_value={"complete": True},
                ),
                mock.patch.object(
                    controller,
                    "_prefetch_images",
                    side_effect=PREFETCH.MaintenancePrefetchError(
                        "simulated interrupted image pull"
                    ),
                ),
                self.assertRaisesRegex(
                    PREFETCH.MaintenancePrefetchError,
                    "interrupted",
                ),
            ):
                controller.run()
            self.assertFalse(controller.ready_path.exists())


if __name__ == "__main__":
    unittest.main()

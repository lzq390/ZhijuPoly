from __future__ import annotations

import importlib.util
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "pull_deploy_controller.py"
SPEC = importlib.util.spec_from_file_location("pull_deploy_controller", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CONTROLLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTROLLER)


PREVIOUS_SHA = "1" * 40
PREVIOUS_TREE = "2" * 40
TARGET_SHA = "3" * 40
TARGET_TREE = "4" * 40
OPERATION_ID = "deploy-20260716-0001"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
B_MANIFEST_PAYLOAD = (
    REPOSITORY_ROOT / "backend/migrations/postgres/manifest.json"
).read_bytes()
B_MANIFEST_RECORDS = json.loads(B_MANIFEST_PAYLOAD)["migrations"]
B_MANIFEST_DIGEST = CONTROLLER.sha256_bytes(B_MANIFEST_PAYLOAD)
F_MANIFEST_DIGEST = "sha256:" + "e" * 64


def write_private(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    path.write_text(payload, encoding="utf-8")
    os.chmod(path, 0o600)


def image_record(role: str, sha: str = TARGET_SHA) -> dict[str, str]:
    root = CONTROLLER.BACKEND_TAG_ROOT if role == "backend" else CONTROLLER.WEB_TAG_ROOT
    return {
        "tag": f"{root}:sha-{sha}",
        "digest_ref": f"{root}@{DIGEST_A if role == 'backend' else DIGEST_B}",
        "image_id": "sha256:" + ("c" if role == "backend" else "d") * 64,
        "revision": sha,
        "source": CONTROLLER.SOURCE_URL,
        "version": f"sha-{sha}",
    }


def mutable_data_evidence() -> dict[str, object]:
    tables = [
        {
            "schema": "online_knowledge",
            "table": "history",
            "row_count": 17,
            "schema_sha256": "sha256:" + "1" * 64,
            "content_sha256": "sha256:" + "2" * 64,
        },
        {
            "schema": "online_knowledge",
            "table": "jobs",
            "row_count": 9,
            "schema_sha256": "sha256:" + "3" * 64,
            "content_sha256": "sha256:" + "4" * 64,
        },
    ]
    identity = {
        "database": "nexpoly",
        "database_system_identifier": "7659245354718314530",
        "connection": {
            "service": CONTROLLER.MUTABLE_DATA_SERVICE,
            "host": CONTROLLER.MUTABLE_DATA_HOST,
            "port": CONTROLLER.MUTABLE_DATA_PORT,
            "database": CONTROLLER.MUTABLE_DATA_DATABASE,
            "user": CONTROLLER.MUTABLE_DATA_USER,
        },
        "postgres_runtime": {
            "container_id": "a" * 64,
            "image_id": "sha256:" + "b" * 64,
            "configured_image": "postgres:16-alpine",
            "data_volume": {
                "type": "volume",
                "name": "nexpoly_postgres_data",
                "source": (
                    "/var/lib/docker/volumes/nexpoly_postgres_data/_data"
                ),
                "destination": "/var/lib/postgresql/data",
                "driver": "local",
                "read_write": True,
            },
            "host_endpoint": {
                "host": CONTROLLER.MUTABLE_DATA_HOST,
                "port": CONTROLLER.MUTABLE_DATA_PORT,
                "container_port": 5432,
                "protocol": "tcp",
            },
            "system_identifier": "7659245354718314530",
        },
        "digest_algorithm": "sha256-postgres-jsonb-copy-v1",
        "tables": tables,
    }
    return {
        "schema_version": 2,
        **identity,
        "transaction_isolation": "repeatable read",
        "transaction_read_only": True,
        "transaction_deferrable": True,
        "snapshot_sha256": CONTROLLER.canonical_json_digest(identity),
        "captured_at": "2026-07-17T00:00:00Z",
    }


def seed_completed_alias_gate(
    runtime: Path, manifest: dict[str, object], control_root: Path
) -> None:
    selector = CONTROLLER._control_runtime
    operation_id = "alias-0005-fixture"
    audit_dir = runtime / selector.ALIAS_AUDIT_ROOT_RELATIVE / operation_id
    backup_dir = runtime / selector.ALIAS_BACKUP_ROOT_RELATIVE / operation_id
    for directory in (audit_dir, backup_dir):
        directory.mkdir(parents=True, mode=0o700)
        os.chmod(directory, 0o700)
    dump = backup_dir / "nexpoly-before.dump"
    write_private(dump, "fixture database dump\n")
    dump_sha = selector.sha256_file(dump).removeprefix("sha256:")
    write_private(backup_dir / "nexpoly-before.dump.sha256", dump_sha + "\n")
    restore_list = audit_dir / "pg-restore.list"
    write_private(
        restore_list,
        "TABLE DATA generation polytao_jobs\n"
        "TABLE DATA governance schema_migrations\n",
    )
    def ledger_rows(pairs: list[tuple[str, str]]) -> list[dict[str, str]]:
        return [
            {
                "version": version,
                "checksum": checksum,
                "applied_at": (
                    selector.ALIAS_APPLIED_AT
                    if version == selector.ALIAS_VERSION
                    else f"2026-07-08T02:{index:02d}:00.000000Z"
                ),
            }
            for index, (version, checksum) in enumerate(pairs)
        ]

    archive = {
        "row_count": 12,
        "status_counts": {"completed": 8, "failed": 4},
        "rows_sha256": "c" * 64,
        "schema_sha256": selector.ALIAS_EXPECTED_SCHEMA_SHA256,
        "structure_counts": selector.ALIAS_EXPECTED_STRUCTURE_COUNTS,
    }
    relation = {
        "kind": "r",
        "persistence": "p",
        "is_partition": False,
        "row_security": False,
        "force_row_security": False,
        "owner": "polyprop",
        "parents": 0,
        "children": 0,
    }
    before = {
        "database": "nexpoly",
        "current_user": "polyprop",
        "database_owner": "polyprop",
        "server_version_num": 160014,
        "in_recovery": False,
        "system_identifier": selector.ALIAS_SYSTEM_IDENTIFIER,
        "ledger": ledger_rows(selector.ALIAS_PRE_LEDGER),
        "archive": archive,
        "ledger_schema_sha256": selector.ALIAS_EXPECTED_LEDGER_SCHEMA_SHA256,
        "ledger_structure_counts": selector.ALIAS_EXPECTED_LEDGER_STRUCTURE_COUNTS,
        "polytao_relation": relation,
        "ledger_relation": relation,
    }
    after = {
        **before,
        "ledger": [
            row
            for row in before["ledger"]
            if row["version"] != selector.ALIAS_VERSION
        ],
    }
    restored = {
        **before,
        "database": "nexpoly_alias_restore",
        "current_user": "postgres",
        "database_owner": "postgres",
        "system_identifier": "123456789",
        "polytao_relation": {**relation, "owner": "postgres"},
        "ledger_relation": {**relation, "owner": "postgres"},
    }
    entrypoint = manifest["entrypoints"]["reconcile-production-0005-alias"]
    control = {
        "release_id": manifest["release_id"],
        "source_sha": manifest["source_sha"],
        "source_tree": manifest["source_tree"],
        "manifest_sha256": selector.sha256_file(
            control_root / selector.CONTROL_MANIFEST_NAME
        ).removeprefix("sha256:"),
        "script_sha256": selector.sha256_file(
            control_root / entrypoint["file"]
        ).removeprefix("sha256:"),
    }
    identity = {
        "operation_id": operation_id,
        "control": control,
        "legacy_source": {"sha": PREVIOUS_SHA, "tree": PREVIOUS_TREE},
        "binaries_sha256": {"/fixture/bin": "b" * 64},
        "database_endpoint": selector.ALIAS_DATABASE_ENDPOINT,
        "database_system_identifier": selector.ALIAS_SYSTEM_IDENTIFIER,
        "restore_image": {
            "digest_ref": selector.ALIAS_RESTORE_IMAGE,
            "image_id": "sha256:" + "d" * 64,
        },
        "alias": {
            "version": selector.ALIAS_VERSION,
            "checksum": selector.ALIAS_CHECKSUM,
            "applied_at": selector.ALIAS_APPLIED_AT,
        },
    }
    backup = {
        "dump_path": str(dump),
        "dump_sha256": dump_sha,
        "dump_size": dump.stat().st_size,
        "restore_list_sha256": selector.sha256_file(restore_list).removeprefix(
            "sha256:"
        ),
    }
    restore = {
        "image": {
            "digest_ref": selector.ALIAS_RESTORE_IMAGE,
            "image_id": "sha256:" + "d" * 64,
        },
        "container_name": "nexpoly-alias-restore-fixture",
        "network_mode": "none",
        "dump_sha256": dump_sha,
        "archive": before["archive"],
        "ledger_schema_sha256": before["ledger_schema_sha256"],
        "database_inventory": restored,
        "verified_at": "2026-07-17T00:00:00Z",
    }
    CONTROLLER.atomic_json(audit_dir / "isolated-postgres16-restore.json", restore)
    CONTROLLER.atomic_json(audit_dir / "database-after.json", after)
    files = selector._alias_evidence_files(audit_dir, backup_dir)
    completed_at = "2026-07-17T00:00:01Z"
    audit = {
        "schema_version": 1,
        "operation_id": operation_id,
        "outcome": "completed",
        "identity": identity,
        "database_before": before,
        "database_after": after,
        "database_backup": backup,
        "isolated_restore": restore,
        "binaries": {"/fixture/bin": {"sha256": "b" * 64}},
        "files": files,
        "completed_at": completed_at,
    }
    audit_path = audit_dir / "AUDIT-MANIFEST.json"
    CONTROLLER.atomic_json(audit_path, audit)
    marker = {
        "schema_version": 1,
        "action": selector.ALIAS_ACTION,
        "phase": "completed",
        "identity": identity,
        "operation_directories": {
            "audit": str(audit_dir),
            "backup": str(backup_dir),
        },
        "started_at": "2026-07-17T00:00:00Z",
        "updated_at": completed_at,
        "runtime_stop_fence": {"fixture": True},
        "before": before,
        "database_backup": backup,
        "restore_container": {"name": "fixture"},
        "isolated_restore": restore,
        "mutation_intent": {
            "database_system_identifier": selector.ALIAS_SYSTEM_IDENTIFIER,
            "alias": identity["alias"],
            "pre_ledger": before["ledger"],
            "archive": before["archive"],
            "dump_sha256": dump_sha,
            "restore_dump_sha256": dump_sha,
        },
        "after": after,
        "audit_manifest_sha256": selector.sha256_file(audit_path).removeprefix(
            "sha256:"
        ),
        "completed_at": completed_at,
    }
    marker_path = runtime / selector.ALIAS_MARKER_RELATIVE
    marker_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(marker_path.parent, 0o700)
    CONTROLLER.atomic_json(marker_path, marker)


class GitRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(
        self, command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[0] != "git":
            raise AssertionError(command)
        index = 1
        while index + 1 < len(command) and command[index] == "-c":
            index += 2
        arguments = command[index:]
        output = ""
        returncode = 0
        if arguments == ["symbolic-ref", "--short", "HEAD"]:
            output = "main\n"
        elif arguments == ["status", "--porcelain=v1", "--untracked-files=all"]:
            output = ""
        elif arguments == ["ls-files", "-z", "--cached"]:
            output = ""
        elif arguments == [
            "ls-files",
            "-z",
            "--others",
            "--ignored",
            "--exclude-standard",
        ]:
            output = ""
        elif arguments == ["remote", "get-url", "origin"]:
            output = CONTROLLER.REPOSITORY_HTTPS_URL + "\n"
        elif arguments == ["rev-parse", "HEAD"]:
            output = PREVIOUS_SHA + "\n"
        elif arguments == ["rev-parse", "HEAD^{tree}"]:
            output = PREVIOUS_TREE + "\n"
        elif arguments == [
            "ls-remote",
            "--exit-code",
            CONTROLLER.REPOSITORY_SSH_URL,
            "refs/heads/main",
        ]:
            output = f"{TARGET_SHA}\trefs/heads/main\n"
        else:
            raise AssertionError(arguments)
        return subprocess.CompletedProcess(command, returncode, output, "")

    def request_json(self, _url: str, _token: str) -> dict[str, object]:
        raise AssertionError("unexpected network request")


class GithubRunner:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def run(
        self, *_args: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError("unexpected subprocess")

    def request_json(self, url: str, _token: str) -> dict[str, object]:
        self.urls.append(url)
        if "/jobs?" in url:
            return {
                "jobs": [
                    {"name": "ci-gate", "conclusion": "success"},
                    {
                        "name": "Publish and smoke immutable main images",
                        "conclusion": "success",
                    },
                ]
            }
        return {
            "workflow_runs": [
                {
                    "id": 42,
                    "run_attempt": 2,
                    "head_sha": TARGET_SHA,
                    "head_branch": "main",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                    "path": ".github/workflows/ci.yml",
                }
            ]
        }


class ImageRunner:
    def __init__(self, *, wrong_revision: bool = False) -> None:
        self.commands: list[list[str]] = []
        self.wrong_revision = wrong_revision

    def run(
        self, command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            tag = command[3]
            root = tag.split(":sha-", 1)[0].split("@", 1)[0]
            sha = TARGET_SHA if not self.wrong_revision else PREVIOUS_SHA
            output = json.dumps(
                [
                    {
                        "Id": "sha256:" + "9" * 64,
                        "RepoDigests": [f"{root}@{DIGEST_A}"],
                        "Config": {
                            "Labels": {
                                "org.opencontainers.image.revision": sha,
                                "org.opencontainers.image.source": CONTROLLER.SOURCE_URL,
                                "org.opencontainers.image.version": f"sha-{TARGET_SHA}",
                            }
                        },
                    }
                ]
            )
        else:
            output = ""
        return subprocess.CompletedProcess(command, 0, output, "")

    def request_json(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("unexpected request")


class FakeLifecycle:
    def __init__(
        self, *, fail_at: str | None = None, admission_open: bool = False
    ) -> None:
        self.events: list[str] = []
        self.fail_at = fail_at
        self.admission_open = admission_open
        self.recovery_fence: dict[str, object] = {"fixture_instance": "instance-1"}
        self.runtime_state = "live"

    def _event(self, name: str) -> None:
        self.events.append(name)
        if self.fail_at == name:
            raise CONTROLLER.PullDeployError(f"injected {name} failure")

    def postgres_runtime_identity(
        self, _controller: object, _descriptor: object
    ) -> dict[str, object]:
        runtime = dict(mutable_data_evidence()["postgres_runtime"])
        return {
            "schema_version": 1,
            **runtime,
            "captured_at": CONTROLLER.utc_now(),
        }

    def drain(self, _controller: object, _descriptor: object) -> dict[str, object]:
        self._event("drain")
        self.admission_open = False
        return {"active_total": 0}

    def ensure_candidate_drained(
        self, _controller: object, _descriptor: object
    ) -> dict[str, object]:
        self._event("ensure-candidate-drained")
        self.admission_open = False
        return {"active_total": 0}

    def backup(
        self, controller: object, descriptor: dict[str, object]
    ) -> dict[str, object]:
        self._event("backup")
        backup_root = Path(getattr(controller, "backups_dir"))
        directory = backup_root / str(descriptor["operation_id"])
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        dump = directory / "database.dump"
        if not dump.exists():
            dump.write_bytes(b"fixture database dump\n")
            os.chmod(dump, 0o600)
        digest = CONTROLLER.sha256_file(dump)
        return {
            "path": str(dump),
            "sha256": digest,
            "restore_verification": {
                "schema_version": 1,
                "restored": True,
                "postgres_major": 16,
                "image": CONTROLLER.POSTGRES16_IMAGE,
                "dump_sha256": digest,
                "ledger": [
                    {
                        "version": "0010_deployment_control",
                        "checksum": "a" * 64,
                    }
                ],
            },
        }

    def backup_rollback(
        self,
        controller: object,
        descriptor: dict[str, object],
        backup_operation_id: str,
    ) -> dict[str, object]:
        projected = dict(descriptor)
        projected["operation_id"] = backup_operation_id
        return self.backup(controller, projected)

    def stop(self, _controller: object, _descriptor: object) -> None:
        self._event("stop")
        self.runtime_state = "stopped"

    def restore_database(
        self, _controller: object, _descriptor: object, backup: dict[str, object]
    ) -> dict[str, object]:
        self._event("restore_database")
        return {
            "restored": True,
            "dump_sha256": backup["sha256"],
            "ledger": backup["restore_verification"].get("ledger", []),
        }

    def migrate(self, _controller: object, _descriptor: object) -> dict[str, object]:
        self._event("migrate")
        return {
            "newly_applied": ["0010_deployment_control"],
            "ledger": [
                {
                    "version": "0010_deployment_control",
                    "kind": "expand",
                    "epoch": 1,
                    "checksum": "a" * 64,
                    "requires_contracts": [],
                }
            ],
        }

    def start(self, _controller: object, _descriptor: object) -> None:
        self._event("start")
        self.runtime_state = "live"
        self.admission_open = False

    def verification(self) -> dict[str, object]:
        return {
            "health": "ok",
            "recovery_fence": dict(self.recovery_fence),
        }

    def verify(self, _controller: object, _descriptor: object) -> dict[str, object]:
        self._event("verify")
        return self.verification()

    def resume(
        self,
        _controller: object,
        _descriptor: object,
        expected_verification: object,
    ) -> None:
        self._event("resume")
        if expected_verification != self.verification():
            raise CONTROLLER.PullDeployError(
                "resumed runtime instance differs from committed verification"
            )
        self.admission_open = True

    def resume_unchanged(
        self,
        _controller: object,
        _descriptor: object,
        persist_verification,  # type: ignore[no-untyped-def]
        expected_verification=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self._event("resume-unchanged")
        if (
            expected_verification is not None
            and expected_verification != self.verification()
        ):
            raise CONTROLLER.PullDeployError(
                "unchanged runtime differs from committed verification"
            )
        persist_verification(self.verification())
        self.admission_open = True

    def resume_bootstrap_unchanged(
        self, _controller: object, _descriptor: object
    ) -> None:
        self._event("resume-bootstrap-unchanged")
        self.admission_open = True

    def bootstrap_can_resume_unchanged(
        self, _controller: object, _descriptor: object
    ) -> bool:
        self._event("bootstrap-admission-status")
        return self.admission_open

    def admission_is_open(self, _controller: object, _descriptor: object) -> bool:
        self._event("admission-status")
        return self.admission_open

    def verify_open_runtime(
        self,
        _controller: object,
        _descriptor: object,
        expected_verification: object,
    ) -> None:
        self._event("verify-open")
        if expected_verification != self.verification():
            raise CONTROLLER.PullDeployError(
                "open runtime instance differs from committed verification"
            )

    def prepare_recovery_runtime(
        self,
        _controller: object,
        _descriptor: object,
        expected_verification: object,
        *,
        allow_unfenced: bool,
    ) -> dict[str, object]:
        self._event("recovery-isolate")
        if self.runtime_state == "partial":
            raise CONTROLLER.PullDeployError(
                "runtime is partially stopped during recovery"
            )
        if self.runtime_state == "stopped":
            return {
                "runtime_state": "stopped",
                "ingress_isolated": True,
            }
        if expected_verification is not None:
            if expected_verification != self.verification():
                raise CONTROLLER.PullDeployError(
                    "recovery runtime instance differs from committed verification"
                )
        elif not allow_unfenced:
            raise CONTROLLER.PullDeployError(
                "runtime recovery lacks committed verification evidence"
            )
        self._event("recovery-redrain")
        self.admission_open = False
        return {
            "runtime_state": "drained",
            "ingress_isolated": True,
            "drain": {"active_total": 0},
            "verification": self.verification(),
        }


class LostResumeLifecycle(FakeLifecycle):
    def __init__(self) -> None:
        super().__init__()
        self.lose_next_resume = False

    def resume(
        self,
        controller: object,
        descriptor: object,
        expected_verification: object,
    ) -> None:
        marker = CONTROLLER.load_private_json(getattr(controller, "marker_path"))
        if marker.get("verification") != expected_verification:
            raise AssertionError("runtime fence was not durable before resume")
        super().resume(controller, descriptor, expected_verification)
        if self.lose_next_resume:
            self.lose_next_resume = False
            raise CONTROLLER.PullDeployError(
                "injected lost response after admission commit"
            )


class LostUnchangedResumeLifecycle(FakeLifecycle):
    def __init__(self) -> None:
        super().__init__()
        self.lose_next_unchanged_resume = False

    def resume_unchanged(
        self,
        controller: object,
        descriptor: object,
        persist_verification,  # type: ignore[no-untyped-def]
        expected_verification=None,  # type: ignore[no-untyped-def]
    ) -> None:
        super().resume_unchanged(
            controller,
            descriptor,
            persist_verification,
            expected_verification,
        )
        marker = CONTROLLER.load_private_json(getattr(controller, "marker_path"))
        if marker.get("verification") != self.verification():
            raise AssertionError(
                "unchanged runtime fence was not durable before resume"
            )
        if self.lose_next_unchanged_resume:
            self.lose_next_unchanged_resume = False
            raise CONTROLLER.PullDeployError("injected lost unchanged-resume response")


class FixtureController(CONTROLLER.PullDeployController):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.source_sha = PREVIOUS_SHA
        self.source_tree = PREVIOUS_TREE
        self.rollback_called = False
        if self.active_control_path.exists():
            return
        candidate = super().prepare_control_release(
            operation_id="bootstrap-controls-fixture",
            target_sha=TARGET_SHA,
            target_tree=TARGET_TREE,
        )
        manifest, _root = CONTROLLER._control_runtime.load_control_release(
            self.runtime_root, candidate["release_id"]
        )
        active = {
            "schema_version": CONTROLLER._control_runtime.ACTIVE_CONTROL_SCHEMA_VERSION,
            "protocol_version": CONTROLLER._control_runtime.PROTOCOL_VERSION,
            "component": "deployment-controls",
            "generation": 1,
            "release_id": candidate["release_id"],
            "source_sha": TARGET_SHA,
            "source_tree": TARGET_TREE,
            "manifest_sha256": CONTROLLER.sha256_file(
                _root / CONTROLLER._control_runtime.CONTROL_MANIFEST_NAME
            ),
            "operation_id": "bootstrap-controls-fixture",
            "previous_release_id": None,
            "activated_at": CONTROLLER.utc_now(),
        }
        CONTROLLER.atomic_json(self.active_control_path, active)
        source_readiness = {
            "schema_version": 1,
            "ready": True,
            "source_root": str(
                self.runtime_root / "fixture-bootstrap-source"
            ),
            "source_sha": candidate["source_sha"],
            "source_tree": candidate["source_tree"],
            "branch": "main",
            "origin": CONTROLLER.REPOSITORY_SSH_URL,
            "standalone_object_database": True,
            "shallow": False,
            "dirty_entries": 0,
            "ignored_entries": 0,
            "unreachable_objects": 0,
            "owner_private": True,
            "group_or_world_writable": False,
        }
        takeover = {
            "schema_version": 1,
            "operation_id": "takeover-pull-fixture",
            "authority_sha": candidate["source_sha"],
            "authority_tree": candidate["source_tree"],
            "install_manifest_sha256": "sha256:" + "3" * 64,
            "classification_sha256": "sha256:" + "4" * 64,
            "runtime_identity_sha256": "sha256:" + "5" * 64,
            "git_identity": {
                "branch": "refs/heads/main",
                "head_sha": PREVIOUS_SHA,
                "head_tree": PREVIOUS_TREE,
                "local_main_sha": PREVIOUS_SHA,
            },
            "pre_stopped_fence_sha256": "sha256:" + "6" * 64,
            "control_layout_sha256": "sha256:" + "7" * 64,
            "checkout_permissions_sha256": "sha256:" + "8" * 64,
            "applied_record_sha256": "sha256:" + "9" * 64,
        }
        takeover["binding_sha256"] = CONTROLLER.canonical_json_digest(
            takeover
        )
        CONTROLLER.atomic_json(
            self.state_dir / "bootstrap-control.json",
            {
                "schema_version": 2,
                "status": "completed",
                "source_sha": candidate["source_sha"],
                "source_tree": candidate["source_tree"],
                "source_readiness": source_readiness,
                "source_readiness_sha256": (
                    CONTROLLER.canonical_json_digest(source_readiness)
                ),
                "legacy_takeover": takeover,
                "delivery_gate": {"fixture": True},
                "production_repository": {"fixture": True},
                "immutable_files": {
                    name: CONTROLLER.sha256_file(self.bin_dir / name)
                    for name in CONTROLLER.STABLE_HELPER_FILES
                },
                "worker_unit_takeover": {"fixture": True},
                "candidate_control": candidate,
                "active_control": active,
            },
        )
        seed_completed_alias_gate(self.runtime_root, manifest, _root)

    def _git_show(self, _target_sha: str, relative: str) -> bytes:
        return (REPOSITORY_ROOT / relative).read_bytes()

    def repository_identity(
        self, *, require_ssh_origin: bool = False
    ) -> dict[str, str]:
        return {
            "sha": self.source_sha,
            "tree": self.source_tree,
            "origin": (
                CONTROLLER.REPOSITORY_SSH_URL
                if require_ssh_origin
                else CONTROLLER.REPOSITORY_HTTPS_URL
            ),
        }

    def remote_main(self) -> str:
        return TARGET_SHA

    def fetch_target(self, target_sha: str, _operation_id: str) -> str:
        if target_sha != TARGET_SHA:
            raise AssertionError(target_sha)
        return TARGET_TREE

    def ci_evidence(self, target_sha: str) -> dict[str, object]:
        return {
            "workflow_run_id": 42,
            "run_attempt": 1,
            "head_sha": target_sha,
            "head_branch": "main",
            "event": "push",
            "path": ".github/workflows/ci.yml",
            "conclusion": "success",
            "required_jobs": ["Publish and smoke immutable main images", "ci-gate"],
        }

    def image_evidence(self, role: str, target_sha: str) -> dict[str, str]:
        return image_record(role, target_sha)

    def _revalidate_materialized_images(
        self,
        _images: object,
        *,
        source_sha: str,
        pull: bool,
    ) -> None:
        del source_sha, pull

    def postgres_restore_image_evidence(self) -> dict[str, str]:
        return {
            "digest_ref": CONTROLLER.POSTGRES16_IMAGE,
            "image_id": "sha256:" + "5" * 64,
        }

    def controller_digest(self) -> str:
        return CONTROLLER.sha256_file(SCRIPT)

    def stable_helper_evidence(self) -> dict[str, str]:
        return {name: "sha256:" + "d" * 64 for name in CONTROLLER.STABLE_HELPER_FILES}

    def validate_installed_controls_against_target(self, _target_sha: str) -> None:
        return

    def production_deploy_values(self, *, check_free_space: bool) -> dict[str, str]:
        return {"fixture": str(check_free_space)}

    def _capture_mutable_data(
        self, _descriptor: dict[str, object]
    ) -> dict[str, object]:
        return mutable_data_evidence()

    def asset_evidence(self, expected_digest: str) -> dict[str, object]:
        target = self.runtime_root / "fixture-assets" / expected_digest.split(":", 1)[1]
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        return {
            "pointer_path": str(self.state_dir / "current-assets"),
            "root": str(target),
            "manifest_sha256": expected_digest,
            "schema_version": 2,
            "byteff2_commit": "a" * 40,
            "inventory_sha256": "sha256:" + "b" * 64,
            "previous": None,
        }

    def prepare_worker_controls(
        self,
        *,
        operation_id: str,
        target_sha: str,
        executor_control: dict[str, object],
    ) -> dict[str, object]:
        operation = self.prepared_root / operation_id
        candidate = operation / CONTROLLER.MONOMER_MD_UNIT_NAME
        candidate.write_text("fixture unit\n", encoding="utf-8")
        os.chmod(candidate, 0o600)
        target = self.runtime_root / "config" / CONTROLLER.MONOMER_MD_UNIT_NAME
        worker_env = self.runtime_root / "config/worker.env"
        if not worker_env.exists():
            write_private(worker_env, "MONOMER_MD_WORKER_MODE=real\n")
        return {
            "worker_env": {
                "path": str(worker_env),
                "sha256": CONTROLLER.sha256_file(worker_env),
                "byteff2_python": "/opt/byteff2/bin/python",
                "byteff2_openmm_dir": "/opt/byteff2/openmm",
                "gmx_sha256": "sha256:" + "c" * 64,
            },
            "systemd_unit": {
                "source_path": CONTROLLER.MONOMER_MD_UNIT_SOURCE,
                "candidate_path": str(candidate),
                "target_path": str(target),
                "sha256": CONTROLLER.sha256_file(candidate),
                "previous_present": False,
                "previous_sha256": None,
                "previous_backup_path": None,
                "previous_unit_state": {
                    "LoadState": "not-found",
                    "FragmentPath": "",
                    "DropInPaths": "",
                    "NeedDaemonReload": "no",
                    "UnitFileState": "",
                },
                "control_release_id": executor_control["release_id"],
                "launcher_sha256": "sha256:" + "a" * 64,
            },
        }

    def _revalidate_worker_controls(self, _descriptor: object) -> None:
        return

    def _install_candidate_worker_unit(self, _descriptor: object) -> None:
        return

    def _restore_previous_worker_unit(self, _descriptor: object) -> None:
        return

    def _source_evidence(self, _target_sha: str):  # type: ignore[no-untyped-def]
        return (
            {
                "sha256": DIGEST_A,
                "schema_version": 2,
                "asset_manifest_digest": (
                    CONTROLLER.SCHEMA_V2_ASSET_MANIFEST_DIGEST
                ),
                "predecessor_asset_manifest_digest": (
                    CONTROLLER.SCHEMA_V2_PREDECESSOR_ASSET_MANIFEST_DIGEST
                ),
                "changed_asset_trees": ["byteff2"],
                "datasets_on_asset_change": [],
            },
            {
                "sha256": DIGEST_B,
                "schema_version": 2,
                "records": [
                    {
                        "version": "0010_deployment_control",
                        "kind": "expand",
                        "epoch": 1,
                        "checksum": "a" * 64,
                        "requires_contracts": [],
                    }
                ],
            },
            {
                "sha256": "sha256:" + "6" * 64,
                "files": {
                    "docker-compose.yml": DIGEST_A,
                    "docker-compose.prod.yml": DIGEST_B,
                },
            },
            b"fixture==1.0 --hash=sha256:" + b"1" * 64 + b"\n",
        )

    def prepare_md_slot(
        self,
        *,
        operation_id: str,
        target_sha: str,
        target_tree: str,
        lock_payload: bytes,
    ) -> dict[str, object]:
        slot = self.choose_inactive_slot()
        self._remove_owned_slot(slot, operation_id)
        venv = self.venv_root / f"md-{slot}" / "venv"
        (venv / "bin").mkdir(parents=True, mode=0o700)
        (venv / "bin/python").write_text("fixture\n", encoding="utf-8")
        record = {
            "schema_version": CONTROLLER.SLOT_RECORD_SCHEMA_VERSION,
            "component": "monomer-md",
            "status": "ready",
            "slot": slot,
            "source_sha": target_sha,
            "source_tree": target_tree,
            "worker_lock_sha256": CONTROLLER.sha256_bytes(lock_payload),
            "requirements_sha256": CONTROLLER.sha256_bytes(lock_payload),
            "wheel_cache_key": "sha256:" + "7" * 64,
            "wheel_inventory_sha256": "sha256:" + "8" * 64,
            "venv_prefix": str(venv.resolve()),
            "venv_inventory_sha256": CONTROLLER.worker_directory_inventory_digest(venv),
            "base_python_configured_path": sys.executable,
            "base_python_identity_sha256": "sha256:" + "9" * 64,
            "prepared_operation_id": operation_id,
            "prepared_at": CONTROLLER.utc_now(),
        }
        CONTROLLER.validate_slot_record(record, slot)
        CONTROLLER.atomic_json(self.slots_state_dir / f"md-{slot}.json", record)
        return record

    def _revalidate_pre_switch(self, descriptor: dict[str, object]) -> None:
        if self.production_config_evidence(check_free_space=True) != descriptor.get(
            "production_config"
        ):
            raise CONTROLLER.PullDeployError(
                "production configuration changed after prepare"
            )
        repository = descriptor["repository"]
        assert isinstance(repository, dict)
        if self.source_sha != repository["previous_sha"]:
            raise CONTROLLER.PullDeployError("fixture source changed")

    def _switch_source(self, _descriptor: dict[str, object]) -> None:
        self.source_sha = TARGET_SHA
        self.source_tree = TARGET_TREE

    def _restore_source(self, _descriptor: dict[str, object]) -> None:
        previous = _descriptor.get("previous_deployment")
        if isinstance(previous, dict):
            self.source_sha = str(previous["source_sha"])
            self.source_tree = str(previous["source_tree"])
        else:
            self.source_sha = PREVIOUS_SHA
            self.source_tree = PREVIOUS_TREE

    def _rollback_failed_attempt(self, _descriptor: object, _marker: object) -> None:
        self.rollback_called = True
        self.source_sha = PREVIOUS_SHA
        self.source_tree = PREVIOUS_TREE


class PullDeployTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = mock.patch.dict(os.environ, {"NEXPOLY_ALLOW_TEST_ROOT": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix="nexpoly-pull-controller-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.production = self.root / "production"
        self.runtime = self.root / "runtime"
        self.production.mkdir(mode=0o755)
        (self.production / ".git").mkdir(mode=0o700)
        for relative in (
            "bin",
            "config",
            "config/docker",
            "state",
            "state/prepared",
            "state/worker-slots",
            "state/control-handoffs",
            "audit",
            "backups",
            "wheel-cache",
            "worker-venvs",
            "control-releases",
        ):
            path = self.runtime / relative
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        lock = self.runtime / "state/deploy.lock"
        lock.write_text("", encoding="utf-8")
        os.chmod(lock, 0o600)
        for name, content in (
            ("git-deploy-key", "fixture-key\n"),
            ("known_hosts", "github.com ssh-ed25519 fixture\n"),
            ("github-api-token", "fixture-token\n"),
            (
                "deploy.env",
                "\n".join(
                    (
                        f"NEXPOLY_RUNTIME_ROOT={self.runtime}",
                        f"NEXPOLY_APP_ENV_FILE={self.runtime / 'config/app.env'}",
                        f"NEXPOLY_ASSET_ROOT={self.runtime / 'state/current-assets'}",
                        "NEXPOLY_POSTGRES_USER=fixture_user",
                        "NEXPOLY_POSTGRES_PASSWORD=fixture-secret-0123456789",
                        "NEXPOLY_POSTGRES_DB=nexpoly",
                        "NEXPOLY_POSTGRES_PORT=55432",
                        "APP_POSTGRES_DSN=postgresql://fixture_user:fixture-secret-0123456789@lab-postgres:5432/nexpoly",
                        "PI_POSTGRES_DSN=postgresql://fixture_user:fixture-secret-0123456789@lab-postgres:5432/nexpoly",
                        "LAB_DATA_POSTGRES_DSN=postgresql://fixture_user:fixture-secret-0123456789@lab-postgres:5432/nexpoly",
                        "POLYTAO_ENABLED=true",
                        "MONOMER_MD_REQUIRE_TRANSPORT_READY=true",
                        "NEXPOLY_HEALTH_URLS=http://127.0.0.1:9000/health",
                        "NEXPOLY_MIN_FREE_BYTES=1073741824",
                        "NEXPOLY_POSTGRES_RESTORE_TMPFS_BYTES=8589934592",
                        f"NEXPOLY_WORKER_BASE_PYTHON={sys.executable}",
                        "",
                    )
                ),
            ),
            ("app.env", "ONLINE_KNOWLEDGE_API_KEY=fixture\n"),
        ):
            write_private(self.runtime / "config" / name, content)
        for name in CONTROLLER.STABLE_HELPER_FILES:
            helper = self.runtime / "bin" / name
            helper.write_text(f"fixture {name}\n", encoding="utf-8")
            os.chmod(helper, 0o700)
        for name in (
            "bootstrap-quiesce",
            "bootstrap-status",
            "bootstrap-resume-unchanged",
            "bootstrap-rollback",
            "bootstrap-active-jobs-probe",
            "bootstrap-legacy-runtime-status",
            "bootstrap-legacy-runtime-resume-unchanged",
            "bootstrap-legacy-runtime-restore",
            CONTROLLER.MUTABLE_DATA_AUDIT_HELPER,
        ):
            hook = self.runtime / "config" / name
            hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(hook, 0o700)
        write_private(
            self.runtime / "config" / CONTROLLER.MUTABLE_DATA_SERVICE_CONFIG,
            (
                "[nexpoly-mutable-audit]\n"
                "host=127.0.0.1\n"
                "port=55432\n"
                "dbname=nexpoly\n"
                "user=nexpoly_mutable_audit\n"
                "sslmode=disable\n"
                f"passfile={self.runtime / 'config' / CONTROLLER.MUTABLE_DATA_PGPASS}\n"
            ),
        )
        write_private(
            self.runtime / "config" / CONTROLLER.MUTABLE_DATA_PGPASS,
            "127.0.0.1:55432:nexpoly:nexpoly_mutable_audit:fixture\n",
        )
        write_private(
            self.runtime / "config/docker/config.json",
            json.dumps(
                {
                    "auths": {
                        "ghcr.io": {
                            "auth": base64.b64encode(b"fixture:read-only-token").decode(
                                "ascii"
                            )
                        }
                    }
                }
            ),
        )
        # Bootstrap normally installs the target control authority before the
        # first governed runtime takeover.  Seed that exact condition for both
        # base-controller plan tests and mutating fixture tests.
        FixtureController(
            self.production,
            self.runtime,
            runner=GitRunner(),
            lifecycle=FakeLifecycle(),
            apply=True,
        )

    def controller(
        self,
        *,
        runner: object | None = None,
        lifecycle: FakeLifecycle | None = None,
    ) -> FixtureController:
        return FixtureController(
            self.production,
            self.runtime,
            runner=runner or GitRunner(),
            lifecycle=lifecycle or FakeLifecycle(),
            apply=True,
        )


class RepositoryAndEvidenceTests(PullDeployTestCase):
    def test_ambient_test_mode_cannot_authorize_production_roots(self) -> None:
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "forbidden for production paths"
        ):
            CONTROLLER.PullDeployController(
                CONTROLLER.PRODUCTION_ROOT,
                self.runtime,
                runner=GitRunner(),
                apply=True,
            )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "forbidden for production paths"
        ):
            CONTROLLER.PullDeployController(
                self.production,
                CONTROLLER.RUNTIME_ROOT,
                runner=GitRunner(),
                apply=True,
            )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "forbidden for production paths"
        ):
            CONTROLLER.clean_control_environment(CONTROLLER.RUNTIME_ROOT)

    def test_plan_is_read_only_and_requires_requested_sha_to_equal_remote_main(
        self,
    ) -> None:
        runner = GitRunner()
        controller = CONTROLLER.PullDeployController(
            self.production, self.runtime, runner=runner, apply=False
        )
        plan = controller.plan(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        self.assertEqual(plan["target_sha"], TARGET_SHA)
        self.assertFalse(plan["service_mutation"])
        self.assertFalse((self.runtime / "state/deploy-in-progress.json").exists())
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "not current remote main"
        ):
            controller.plan(target_sha="5" * 40, operation_id=OPERATION_ID)

    def test_ci_gate_binds_successful_main_push_and_both_required_jobs(self) -> None:
        runner = GithubRunner()
        controller = CONTROLLER.PullDeployController(
            self.production, self.runtime, runner=runner, apply=False
        )
        evidence = controller.ci_evidence(TARGET_SHA)
        self.assertEqual(evidence["head_sha"], TARGET_SHA)
        self.assertEqual(evidence["conclusion"], "success")
        self.assertEqual(len(runner.urls), 2)

    def test_ci_gate_rejects_missing_image_publication_job(self) -> None:
        runner = GithubRunner()
        original = runner.request_json

        def incomplete(url: str, token: str):  # type: ignore[no-untyped-def]
            payload = original(url, token)
            if "/jobs?" in url:
                return {"jobs": [{"name": "ci-gate", "conclusion": "success"}]}
            return payload

        runner.request_json = incomplete  # type: ignore[method-assign]
        controller = CONTROLLER.PullDeployController(
            self.production, self.runtime, runner=runner, apply=False
        )
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "lacks a successful"):
            controller.ci_evidence(TARGET_SHA)

    def test_image_gate_resolves_digest_and_rejects_wrong_revision(self) -> None:
        runner = ImageRunner()
        controller = CONTROLLER.PullDeployController(
            self.production, self.runtime, runner=runner, apply=False
        )
        evidence = controller.image_evidence("backend", TARGET_SHA)
        self.assertEqual(
            evidence["digest_ref"], f"{CONTROLLER.BACKEND_TAG_ROOT}@{DIGEST_A}"
        )
        self.assertEqual(
            runner.commands[-2], ["docker", "pull", evidence["digest_ref"]]
        )
        self.assertEqual(
            runner.commands[-1],
            ["docker", "image", "inspect", evidence["digest_ref"]],
        )

        controller.runner = ImageRunner(wrong_revision=True)
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "OCI identity"):
            controller.image_evidence("backend", TARGET_SHA)


class EphemeralContainerOwnershipTests(unittest.TestCase):
    @staticmethod
    def web_record(operation_id: str) -> dict[str, object]:
        name = f"nexpoly-web-smoke-{operation_id}-{TARGET_SHA[:12]}"
        return {
            "Id": "1" * 64,
            "Name": f"/{name}",
            "Config": {
                "Image": f"example.invalid/web@{DIGEST_A}",
                "Labels": {"com.nexpoly.deploy-operation": operation_id},
                "Env": [],
            },
            "HostConfig": {
                "NetworkMode": "none",
                "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                "AutoRemove": False,
                "Privileged": False,
                "PublishAllPorts": False,
            },
            "NetworkSettings": {
                "Ports": {"80/tcp": None},
                "Networks": {
                    "none": {
                        "Gateway": "",
                        "IPAddress": "",
                        "IPPrefixLen": 0,
                        "IPv6Gateway": "",
                        "GlobalIPv6Address": "",
                        "GlobalIPv6PrefixLen": 0,
                    }
                },
            },
            "Mounts": [],
        }

    def test_web_run_and_remove_unknown_commit_are_proven_by_inspection(self) -> None:
        record = self.web_record(OPERATION_ID)
        commands: list[list[str]] = []
        inspect_count = 0

        def run(command, **_kwargs):  # type: ignore[no-untyped-def]
            nonlocal inspect_count
            commands.append(command)
            if command[:3] == ["docker", "container", "inspect"]:
                inspect_count += 1
                if inspect_count == 1:
                    return SimpleNamespace(returncode=1, stdout="")
                if inspect_count == 2:
                    return SimpleNamespace(returncode=0, stdout=json.dumps([record]))
                return SimpleNamespace(returncode=1, stdout="")
            if command[:2] == ["docker", "run"]:
                raise OSError("response lost after committed run")
            if command[:3] == ["docker", "exec", record["Name"][1:]]:
                if command[-1] == "http://127.0.0.1/":
                    return SimpleNamespace(
                        returncode=0,
                        stdout='<script src="/assets/app-abcdef12.js"></script>',
                    )
                return SimpleNamespace(returncode=0, stdout=b"asset")
            if command[:3] == ["docker", "rm", "--force"]:
                raise OSError("response lost after committed removal")
            raise AssertionError(command)

        controller = SimpleNamespace(
            runner=SimpleNamespace(run=run),
            control_environment=lambda: {},
        )
        descriptor = {
            "operation_id": OPERATION_ID,
            "repository": {"target_sha": TARGET_SHA},
            "images": {"web": {"digest_ref": f"example.invalid/web@{DIGEST_A}"}},
        }
        evidence = CONTROLLER.SystemLifecycle()._verify_web_image(
            controller, descriptor
        )
        self.assertEqual(evidence["image"], descriptor["images"]["web"]["digest_ref"])
        self.assertEqual(inspect_count, 3)

    def test_same_sha_different_operation_and_extra_resources_are_foreign(self) -> None:
        record = self.web_record("deploy-20260716-another-operation")
        record["Name"] = f"/nexpoly-web-smoke-{OPERATION_ID}-{TARGET_SHA[:12]}"
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "operation"):
            CONTROLLER.SystemLifecycle._validate_isolated_container(
                record,
                name=f"nexpoly-web-smoke-{OPERATION_ID}-{TARGET_SHA[:12]}",
                image=f"example.invalid/web@{DIGEST_A}",
                operation_label="com.nexpoly.deploy-operation",
                operation_id=OPERATION_ID,
                tmpfs_capacity=None,
            )
        record = self.web_record(OPERATION_ID)
        record["HostConfig"]["PortBindings"] = {"80/tcp": [{"HostPort": "8080"}]}
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "host resources"):
            CONTROLLER.SystemLifecycle._validate_isolated_container(
                record,
                name=f"nexpoly-web-smoke-{OPERATION_ID}-{TARGET_SHA[:12]}",
                image=f"example.invalid/web@{DIGEST_A}",
                operation_label="com.nexpoly.deploy-operation",
                operation_id=OPERATION_ID,
                tmpfs_capacity=None,
            )


class PostgresRuntimeFencingTests(unittest.TestCase):
    class Runner:
        def __init__(self, *, replace_on_up: bool = False) -> None:
            self.container_id = "a" * 64
            self.replace_on_up = replace_on_up
            self.commands: list[list[str]] = []

        def _container(self) -> dict[str, object]:
            return {
                "Id": self.container_id,
                "Image": "sha256:" + "b" * 64,
                "Config": {
                    "Image": "postgres:16-alpine",
                    "Labels": {
                        "com.docker.compose.project": "nexpoly",
                        "com.docker.compose.service": "lab-postgres",
                    },
                },
                "State": {"Running": True},
                "NetworkSettings": {
                    "Ports": {
                        "5432/tcp": [
                            {"HostIp": "127.0.0.1", "HostPort": "55432"}
                        ]
                    }
                },
                "Mounts": [
                    {
                        "Type": "volume",
                        "Name": "nexpoly_postgres_data",
                        "Source": "/var/lib/docker/volumes/nexpoly_postgres_data/_data",
                        "Destination": "/var/lib/postgresql/data",
                        "Driver": "local",
                        "RW": True,
                    }
                ],
            }

        def run(
            self, command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            self.commands.append(command)
            output = ""
            returncode = 0
            if (
                command[0:3] == ["docker", "container", "inspect"]
                and len(command) == 4
            ):
                output = json.dumps([self._container()])
            elif command[:3] == ["docker", "inspect", "--format"]:
                output = "true\n"
            elif command[:2] == ["docker", "compose"]:
                if "ps" in command and command[-1] == "lab-postgres":
                    output = self.container_id + "\n"
                elif "ps" in command:
                    output = ""
                elif any("pg_control_system()" in value for value in command):
                    output = "7659245354718314530\n"
                elif "up" in command and self.replace_on_up:
                    self.container_id = "c" * 64
            elif command[:3] == ["systemctl", "--user", "is-active"]:
                output = "inactive\n"
                returncode = 3
            elif command[:2] == ["systemctl", "is-active"]:
                output = "inactive\n"
                returncode = 3
            return subprocess.CompletedProcess(command, returncode, output, "")

        def request_json(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            raise AssertionError("unexpected network request")

    class Lifecycle(CONTROLLER.SystemLifecycle):
        def _drain_started_runtime(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {"fixture": True}

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="postgres-fence-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.production = self.root / "production"
        self.runtime = self.root / "runtime"
        self.config = self.runtime / "config"
        self.state = self.runtime / "state"
        for path in (self.production, self.runtime, self.config, self.state):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        self.descriptor = {
            "images": {
                "backend": {"digest_ref": "example.invalid/backend@" + DIGEST_A},
                "web": {"digest_ref": "example.invalid/web@" + DIGEST_B},
            }
        }

    def controller(self, runner: object) -> object:
        marker_path = self.state / "deploy-in-progress.json"
        return SimpleNamespace(
            runner=runner,
            production_root=self.production,
            runtime_root=self.runtime,
            config_dir=self.config,
            state_dir=self.state,
            marker_path=marker_path,
            control_environment=lambda: {},
            production_deploy_values=lambda **_kwargs: {
                "NEXPOLY_POSTGRES_USER": "nexpoly",
                "NEXPOLY_POSTGRES_DB": "nexpoly",
            },
        )

    def test_stop_and_start_preserve_exact_container_volume_and_system_id(self) -> None:
        runner = self.Runner()
        controller = self.controller(runner)
        lifecycle = self.Lifecycle()
        fence = lifecycle.stop(controller, self.descriptor)
        self.assertEqual(fence["container_id"], "a" * 64)
        self.assertEqual(fence["data_volume"]["name"], "nexpoly_postgres_data")
        self.assertEqual(fence["system_identifier"], "7659245354718314530")
        CONTROLLER.atomic_json(
            controller.marker_path,
            {
                "runtime_stopped": True,
                "postgres_runtime_fence": fence,
            },
        )
        lifecycle.start(controller, self.descriptor)
        up = next(command for command in runner.commands if "up" in command)
        self.assertIn("--no-deps", up)
        self.assertIn("backend", up)
        self.assertNotIn("lab-postgres", up)
        self.assertFalse(
            any(
                "stop" in command and "lab-postgres" in command
                for command in runner.commands
            )
        )

    def test_start_fails_closed_if_compose_replaces_postgres(self) -> None:
        runner = self.Runner(replace_on_up=True)
        controller = self.controller(runner)
        lifecycle = self.Lifecycle()
        fence = lifecycle.postgres_runtime_identity(controller, self.descriptor)
        CONTROLLER.atomic_json(
            controller.marker_path,
            {
                "runtime_stopped": True,
                "postgres_runtime_fence": fence,
            },
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "changed during application start",
        ):
            lifecycle.start(controller, self.descriptor)

    def test_fence_rejects_bind_or_writable_identity_drift(self) -> None:
        fence = {
            "schema_version": 1,
            "container_id": "a" * 64,
            "image_id": "sha256:" + "b" * 64,
            "configured_image": "postgres:16-alpine",
            "data_volume": {
                "type": "bind",
                "name": "foreign",
                "source": "/tmp/foreign",
                "destination": "/var/lib/postgresql/data",
                "driver": "local",
                "read_write": True,
            },
            "host_endpoint": {
                "host": "127.0.0.1",
                "port": 55432,
                "container_port": 5432,
                "protocol": "tcp",
            },
            "system_identifier": "7659245354718314530",
            "captured_at": CONTROLLER.utc_now(),
        }
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "data-volume fence",
        ):
            CONTROLLER.validate_postgres_runtime_fence(fence)


class SlotAndDescriptorTests(PullDeployTestCase):
    def test_alias_gate_allows_preparation_only_before_reconciliation_starts(
        self,
    ) -> None:
        controller = self.controller()
        marker_path = (
            controller.runtime_root
            / CONTROLLER._control_runtime.ALIAS_MARKER_RELATIVE
        )
        completed = CONTROLLER.load_private_json(marker_path)
        marker_path.unlink()
        CONTROLLER.fsync_directory(marker_path.parent)

        controller._require_no_contract_maintenance(
            require_alias_completed=False
        )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "reconciliation is required"
        ):
            controller._require_no_contract_maintenance()

        CONTROLLER.atomic_json(marker_path, {**completed, "phase": "planned"})
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "must recover first"
        ):
            controller._require_no_contract_maintenance(
                require_alias_completed=False
            )

    def bridge_descriptor(
        self, controller: FixtureController
    ) -> dict[str, object]:
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready_path = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        authority_sha = "5" * 40
        authority_tree = "6" * 40
        previous_control = dict(
            descriptor["controller"]["previous_active_control"]
        )
        previous_control.update(
            {
                "release_id": "7" * 64,
                "source_sha": authority_sha,
                "source_tree": authority_tree,
            }
        )
        descriptor["controller"]["previous_active_control"] = previous_control
        descriptor["controller"][
            "previous_active_control_sha256"
        ] = CONTROLLER.canonical_json_digest(previous_control)
        required_jobs = sorted(CONTROLLER._bridge_core.REQUIRED_CI_JOBS)
        descriptor["ci"] = {
            "workflow_run_id": 99,
            "run_attempt": 1,
            "head_sha": authority_sha,
            "head_branch": "main",
            "event": "push",
            "path": ".github/workflows/ci.yml",
            "conclusion": "success",
            "required_jobs": required_jobs,
        }
        policy = {
            "schema_version": 1,
            "mode": CONTROLLER._bridge_core.BRIDGE_MODE,
            "authority_ref": CONTROLLER._bridge_core.AUTHORITY_REF,
            "target_sha": TARGET_SHA,
            "target_tree": TARGET_TREE,
            "target_ref": f"refs/nexpoly/bridge-target/{TARGET_SHA}",
            "target_images": {
                role: descriptor["images"][role]["digest_ref"]
                for role in ("backend", "web")
            },
            "asset_manifest_digest": descriptor["release_input"][
                "asset_manifest_digest"
            ],
            "datasets_on_asset_change": [],
            "final_migration": dict(CONTROLLER._bridge_core.FINAL_MIGRATION),
            "accepted_migration_ledgers": (
                CONTROLLER._bridge_core.expected_migration_registry(
                    target_manifest_sha256=B_MANIFEST_DIGEST,
                    target_records=B_MANIFEST_RECORDS,
                    authority_manifest_sha256=F_MANIFEST_DIGEST,
                    authority_records=[
                        *B_MANIFEST_RECORDS,
                        CONTROLLER._bridge_core.FINAL_MIGRATION_RECORD,
                    ],
                )
            ),
            "required_ci_jobs": required_jobs,
            "policy_id": None,
        }
        policy["policy_id"] = CONTROLLER._bridge_core.canonical_json_digest(
            {key: value for key, value in policy.items() if key != "policy_id"}
        )
        descriptor["migrations"] = {
            "sha256": B_MANIFEST_DIGEST,
            "schema_version": 2,
            "records": json.loads(json.dumps(B_MANIFEST_RECORDS)),
        }
        descriptor["schema_version"] = CONTROLLER.BRIDGE_DESCRIPTOR_SCHEMA_VERSION
        descriptor["bridge"] = CONTROLLER._bridge_core.build_bridge_descriptor(
            operation_id=OPERATION_ID,
            authority_sha=authority_sha,
            authority_tree=authority_tree,
            authority_control_release_id=previous_control["release_id"],
            ci_evidence=descriptor["ci"],
            target_control_release_id=descriptor["controller"][
                "executor_control"
            ]["release_id"],
            policy=policy,
            token_id="sha256:" + "8" * 64,
            token_sha256="sha256:" + "9" * 64,
        )
        takeover = {
            "schema_version": 1,
            "operation_id": "takeover-fixture-operation",
            "authority_sha": authority_sha,
            "authority_tree": authority_tree,
            "install_manifest_sha256": "sha256:" + "a" * 64,
            "classification_sha256": "sha256:" + "b" * 64,
            "runtime_identity_sha256": "sha256:" + "c" * 64,
            "git_identity": {
                "branch": "refs/heads/main",
                "head_sha": descriptor["repository"]["previous_sha"],
                "head_tree": descriptor["repository"]["previous_tree"],
                "local_main_sha": descriptor["repository"]["previous_sha"],
            },
            "pre_stopped_fence_sha256": "sha256:" + "d" * 64,
            "control_layout_sha256": "sha256:" + "e" * 64,
            "checkout_permissions_sha256": "sha256:" + "f" * 64,
            "applied_record_sha256": "sha256:" + "1" * 64,
        }
        takeover["binding_sha256"] = CONTROLLER.canonical_json_digest(
            takeover
        )
        descriptor["legacy_takeover"] = takeover
        prefetch = {
            "schema_version": 1,
            "operation_id": "prefetch-fixture-operation",
            "ready_path": str(
                controller.runtime_root
                / "prefetch/prefetch-fixture-operation/ready.json"
            ),
            "ready_sha256": "sha256:" + "2" * 64,
            "identity_sha256": "sha256:" + "3" * 64,
            "source": {
                "authority": {
                    "sha": authority_sha,
                    "tree": authority_tree,
                },
                "target": {
                    "sha": TARGET_SHA,
                    "tree": TARGET_TREE,
                },
            },
            "source_readiness_sha256": "sha256:" + "4" * 64,
            "policy_sha256": descriptor["bridge"]["policy_sha256"],
            "docker_config_sha256": "sha256:" + "5" * 64,
            "git_bundle_sha256": "sha256:" + "6" * 64,
            "images_sha256": "sha256:" + "7" * 64,
            "wheel_caches_sha256": "sha256:" + "8" * 64,
            "asset_manifest_sha256": descriptor["release_input"][
                "asset_manifest_digest"
            ],
            "asset_inventory_sha256": "sha256:" + "9" * 64,
            "recovery_tools_sha256": "sha256:" + "a" * 64,
        }
        prefetch["binding_sha256"] = CONTROLLER.canonical_json_digest(
            prefetch
        )
        descriptor["prefetch"] = prefetch
        return descriptor

    def test_v3_descriptor_binds_f_authority_exact_b_and_empty_datasets(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        self.assertEqual(CONTROLLER.validate_descriptor(descriptor), descriptor)

        mutations = (
            (
                "authority control",
                lambda value: value["bridge"]["authority"].__setitem__(
                    "control_release_id", "a" * 64
                ),
            ),
            (
                "authority CI",
                lambda value: value["ci"].__setitem__("workflow_run_id", 100),
            ),
            (
                "target ref",
                lambda value: value["bridge"]["target"].__setitem__(
                    "exact_ref", f"refs/nexpoly/bridge-target/{'a' * 40}"
                ),
            ),
            (
                "dataset rebuild",
                lambda value: value["release_input"][
                    "datasets_on_asset_change"
                ].append("online"),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                changed = json.loads(json.dumps(descriptor))
                mutate(changed)
                with self.assertRaises(CONTROLLER.PullDeployError):
                    CONTROLLER.validate_descriptor(changed)

    def test_bridge_ledger_registry_is_consumed_by_runtime_validation(self) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        accepted = descriptor["bridge"]["policy"]["accepted_migration_ledgers"]
        manifest = descriptor["migrations"]["records"]
        for name, rows in (
            ("pre-0012", manifest[:-1]),
            ("post-0012", manifest),
            (
                "post-0013",
                [
                    *manifest,
                    CONTROLLER._bridge_core.FINAL_MIGRATION_RECORD,
                ],
            ),
        ):
            history = CONTROLLER.canonical_ledger_history(
                [
                    {
                        "version": record["version"],
                        "checksum": record["checksum"],
                    }
                    for record in rows
                ],
                manifest,
                accepted_ledgers=accepted,
                require_registry_match=True,
            )
            self.assertEqual(history, rows)
            compatibility = CONTROLLER.build_migration_compatibility_state(
                descriptor["bridge"]["policy"],
                code_manifest_sha256=(
                    F_MANIFEST_DIGEST
                    if name == "post-0013"
                    else B_MANIFEST_DIGEST
                ),
                migrations=history,
            )
            self.assertEqual(compatibility["ledger_state"]["name"], name)

        for rows in (
            [
                *manifest,
                {
                    **CONTROLLER._bridge_core.FINAL_MIGRATION,
                    "checksum": "f" * 64,
                },
            ],
            [
                *manifest,
                CONTROLLER._bridge_core.FINAL_MIGRATION,
                {"version": "0014_future", "checksum": "e" * 64},
            ],
        ):
            with self.assertRaises(CONTROLLER.PullDeployError):
                CONTROLLER.canonical_ledger_history(
                    rows,
                    manifest,
                    accepted_ledgers=accepted,
                    require_registry_match=True,
                )

    def test_b_state_can_truthfully_record_f_0013_ledger(self) -> None:
        descriptor = self.bridge_descriptor(self.controller())
        migrations = [
            *descriptor["migrations"]["records"],
            CONTROLLER._bridge_core.FINAL_MIGRATION_RECORD,
        ]
        compatibility = CONTROLLER.build_migration_compatibility_state(
            descriptor["bridge"]["policy"],
            code_manifest_sha256=B_MANIFEST_DIGEST,
            migrations=migrations,
        )
        self.assertEqual(
            compatibility["code_manifest_sha256"],
            B_MANIFEST_DIGEST,
        )
        self.assertEqual(
            compatibility["ledger_manifest_sha256"],
            F_MANIFEST_DIGEST,
        )
        self.assertEqual(
            compatibility["ledger_state"]["name"],
            "post-0013",
        )

    def test_mutable_online_tables_are_sealed_before_and_after_apply(self) -> None:
        before = mutable_data_evidence()
        pair = CONTROLLER.validate_mutable_data_pair(
            {
                "before": before,
                "after": json.loads(json.dumps(before)),
                "identity_sha256": CONTROLLER.canonical_json_digest(
                    CONTROLLER.mutable_data_identity(before)
                ),
            }
        )
        self.assertEqual(pair["before"], pair["after"])

        for label, field, replacement in (
            ("row count", "row_count", 18),
            ("content", "content_sha256", "sha256:" + "f" * 64),
            ("schema", "schema_sha256", "sha256:" + "e" * 64),
        ):
            with self.subTest(label=label):
                after = json.loads(json.dumps(before))
                after["tables"][0][field] = replacement
                identity = {
                    "database": after["database"],
                    "database_system_identifier": after[
                        "database_system_identifier"
                    ],
                    "connection": after["connection"],
                    "postgres_runtime": after["postgres_runtime"],
                    "digest_algorithm": after["digest_algorithm"],
                    "tables": after["tables"],
                }
                after["snapshot_sha256"] = CONTROLLER.canonical_json_digest(
                    identity
                )
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "changed during deployment",
                ):
                    CONTROLLER.validate_mutable_data_pair(
                        {
                            "before": before,
                            "after": after,
                            "identity_sha256": CONTROLLER.canonical_json_digest(
                                CONTROLLER.mutable_data_identity(before)
                            ),
                        }
                    )

    def test_mutable_helper_is_descriptor_bound_and_asset_rebuild_is_empty(
        self,
    ) -> None:
        controller = self.controller()
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        self.assertEqual(
            descriptor["release_input"]["datasets_on_asset_change"],
            [],
        )
        self.assertEqual(
            descriptor["mutable_data"]["helper_sha256"],
            descriptor["production_config"][
                "deployment_mutable_data_audit_sha256"
            ],
        )
        descriptor["mutable_data"]["helper_sha256"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "mutable-data helper differs",
        ):
            CONTROLLER.validate_descriptor(descriptor)

        pgpass = controller.config_dir / CONTROLLER.MUTABLE_DATA_PGPASS
        pgpass.write_text(
            (
                "127.0.0.1:55432:nexpoly:"
                "nexpoly_mutable_audit:changed\n"
            ),
            encoding="utf-8",
        )
        os.chmod(pgpass, 0o600)
        self.assertNotEqual(
            controller.mutable_data_contract(),
            descriptor["mutable_data"],
        )
        pgpass.unlink()
        pgpass.symlink_to(controller.config_dir / "app.env")
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "dependency is unsafe",
        ):
            controller.mutable_data_contract()

    def test_mutable_connection_rejects_indirection_and_wrong_audit_identity(
        self,
    ) -> None:
        passfile = Path("/private/mutable-data-audit.pgpass")
        canonical = (
            "[nexpoly-mutable-audit]\n"
            "host=127.0.0.1\n"
            "port=55432\n"
            "dbname=nexpoly\n"
            "user=nexpoly_mutable_audit\n"
            "sslmode=disable\n"
            f"passfile={passfile}\n"
        ).encode()
        pgpass = (
            b"127.0.0.1:55432:nexpoly:nexpoly_mutable_audit:secret\\:value\n"
        )
        self.assertEqual(
            CONTROLLER.validate_mutable_data_connection_inputs(
                canonical,
                pgpass,
                expected_passfile=passfile,
            )["user"],
            "nexpoly_mutable_audit",
        )
        for service in (
            canonical + b"include=/tmp/redirect.conf\n",
            canonical.replace(b"host=127.0.0.1", b"host=localhost"),
            canonical.replace(
                b"passfile=/private/mutable-data-audit.pgpass",
                b"servicefile=/tmp/other.conf",
            ),
        ):
            with self.subTest(service=service):
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError,
                    "one exact loopback audit endpoint",
                ):
                    CONTROLLER.validate_mutable_data_connection_inputs(
                        service,
                        pgpass,
                        expected_passfile=passfile,
                    )
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "does not match",
        ):
            CONTROLLER.validate_mutable_data_connection_inputs(
                canonical,
                b"127.0.0.1:55432:nexpoly:postgres:secret\n",
                expected_passfile=passfile,
            )

    def test_same_system_identifier_clone_cannot_satisfy_mutable_audit(
        self,
    ) -> None:
        controller = self.controller()
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(
            OPERATION_ID
        )
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        expected = FakeLifecycle().postgres_runtime_identity(
            controller, descriptor
        )
        CONTROLLER.atomic_json(
            controller.marker_path,
            {
                "postgres_runtime_fence": expected,
            },
        )

        class CloneLifecycle(FakeLifecycle):
            def __init__(self) -> None:
                super().__init__()
                self.captures = 0

            def postgres_runtime_identity(
                self, target_controller: object, target_descriptor: object
            ) -> dict[str, object]:
                result = super().postgres_runtime_identity(
                    target_controller, target_descriptor
                )
                self.captures += 1
                if self.captures == 2:
                    result["container_id"] = "c" * 64
                return result

        controller.lifecycle = CloneLifecycle()
        with mock.patch.object(
            controller.runner,
            "run",
            return_value=subprocess.CompletedProcess(
                ["deployment-mutable-data-audit"],
                0,
                json.dumps(mutable_data_evidence()),
                "",
            ),
        ), self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "exact PostgreSQL container",
        ):
            CONTROLLER.PullDeployController._capture_mutable_data(
                controller,
                descriptor,
            )

    def test_bridge_source_switch_uses_exact_policy_ref_not_remote_main(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        commands: list[tuple[object, ...]] = []

        def fake_git(*arguments, **_kwargs):  # type: ignore[no-untyped-def]
            commands.append(arguments)
            if arguments[:3] == ("show-ref", "--verify", "--hash"):
                return subprocess.CompletedProcess([], 1, "", "")
            return subprocess.CompletedProcess([], 0, "", "")

        controller._git = fake_git  # type: ignore[method-assign]
        controller.repository_identity = lambda **_kwargs: {  # type: ignore[method-assign]
            "sha": TARGET_SHA,
            "tree": TARGET_TREE,
            "origin": CONTROLLER.REPOSITORY_SSH_URL,
        }
        CONTROLLER.PullDeployController._switch_source(controller, descriptor)
        merge = next(command for command in commands if command[0] == "merge")
        self.assertEqual(
            merge,
            ("merge", "--ff-only", f"refs/nexpoly/bridge-target/{TARGET_SHA}"),
        )

    def test_bridge_commit_intent_crash_finishes_exact_current_state(
        self,
    ) -> None:
        controller = self.controller()
        descriptor = self.bridge_descriptor(controller)
        # First build a structurally valid governed state using the existing
        # v2 fixture lifecycle; bridge recovery only changes its descriptor
        # authority before committing it through the v3 token.
        state = controller.apply(
            target_sha=TARGET_SHA,
            operation_id=OPERATION_ID,
        )
        token = CONTROLLER._bridge_core.reserve_token(
            controller.state_dir,
            operation_id=OPERATION_ID,
            policy_id=descriptor["bridge"]["policy"]["policy_id"],
            token=b"bridge-token-fixture-entropy-0001",
        )
        descriptor["bridge"]["token"] = {
            "token_id": token["token_id"],
            "token_sha256": token["token_sha256"],
        }
        CONTROLLER.validate_descriptor(descriptor)
        descriptor_digest = CONTROLLER.sha256_bytes(
            CONTROLLER.canonical_json_bytes(descriptor) + b"\n"
        )
        CONTROLLER._bridge_core.bind_token_descriptor(
            controller.state_dir,
            operation_id=OPERATION_ID,
            policy_id=descriptor["bridge"]["policy"]["policy_id"],
            descriptor_sha256=descriptor_digest,
        )
        state["descriptor_sha256"] = descriptor_digest
        candidate_digest = CONTROLLER.sha256_bytes(
            CONTROLLER.canonical_json_bytes(state) + b"\n"
        )
        CONTROLLER._bridge_core.begin_state_commit(
            controller.state_dir,
            operation_id=OPERATION_ID,
            descriptor_sha256=descriptor_digest,
            candidate_state_sha256=candidate_digest,
        )
        controller.current_state_path.unlink()
        marker = {
            "candidate_state": state,
            "candidate_state_sha256": candidate_digest,
        }
        recovered = controller._candidate_current_state(
            descriptor,
            descriptor_digest,
            marker,
        )
        self.assertEqual(recovered, state)
        self.assertEqual(
            CONTROLLER.sha256_file(controller.current_state_path),
            candidate_digest,
        )
        self.assertEqual(
            CONTROLLER._bridge_core.load_token_authority(
                controller.state_dir
            )["status"],
            "consumed",
        )

    def test_pending_contract_marker_blocks_every_code_deployment_command(self) -> None:
        controller = self.controller()
        CONTROLLER.atomic_json(
            controller.contract_marker_path,
            {"operation_id": "contract-0012-20260716"},
        )
        actions = (
            lambda: controller.plan(target_sha=TARGET_SHA, operation_id=OPERATION_ID),
            lambda: controller.prepare(
                target_sha=TARGET_SHA, operation_id=OPERATION_ID
            ),
            lambda: controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID),
            lambda: controller.rollback(operation_id=OPERATION_ID),
        )
        for action in actions:
            with self.subTest(action=action):
                with self.assertRaisesRegex(
                    CONTROLLER.PullDeployError, "0012 maintenance"
                ):
                    action()

    def test_active_controller_seals_once_then_hands_prepare_to_target_controller(
        self,
    ) -> None:
        controller = self.controller()
        captured: list[tuple[str, list[str], dict[str, str]]] = []

        def capture(path, argv, environment):  # type: ignore[no-untyped-def]
            captured.append((path, argv, environment))
            raise RuntimeError("exec captured")

        controller.control_environment = lambda: {}  # type: ignore[method-assign]
        with mock.patch.object(CONTROLLER.os, "execve", capture):
            with self.assertRaisesRegex(RuntimeError, "exec captured"):
                controller._handoff_prepare_to_target_controller(
                    target_sha=TARGET_SHA, operation_id=OPERATION_ID
                )
        handoff_path = controller.control_handoffs_dir / f"{OPERATION_ID}.json"
        first = CONTROLLER.load_private_json(handoff_path)
        first_digest = CONTROLLER.sha256_file(handoff_path)
        with mock.patch.object(CONTROLLER.os, "execve", capture):
            with self.assertRaisesRegex(RuntimeError, "exec captured"):
                controller._handoff_prepare_to_target_controller(
                    target_sha=TARGET_SHA, operation_id=OPERATION_ID
                )
        self.assertEqual(CONTROLLER.load_private_json(handoff_path), first)
        self.assertEqual(CONTROLLER.sha256_file(handoff_path), first_digest)
        self.assertEqual(len(captured), 2)
        for _path, argv, environment in captured:
            self.assertEqual(argv[:3], ["/usr/bin/python3", "-I", "-B"])
            self.assertEqual(
                Path(argv[3]).parent.name,
                first["executor_control"]["release_id"],
            )
            self.assertEqual(
                environment["NEXPOLY_PREPARE_HANDOFF_SHA256"], first_digest
            )

    def test_control_pointer_accepts_only_sealed_previous_or_candidate(self) -> None:
        controller = self.controller()
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        descriptor = CONTROLLER.validate_descriptor(
            CONTROLLER.load_private_json(descriptor_path)
        )
        previous = controller.active_control_evidence()
        candidate = controller._activate_control(descriptor)
        self.assertEqual(candidate["generation"], previous["generation"] + 1)
        controller._restore_previous_control(descriptor)
        self.assertEqual(controller.active_control_evidence(), previous)
        foreign = dict(previous)
        foreign["operation_id"] = "foreign-controls-0001"
        CONTROLLER.atomic_json(controller.active_control_path, foreign)
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "neither sealed"):
            controller._activate_control(descriptor)

    def test_slot_recycling_detects_venv_python_via_bounded_proc_cmdline(self) -> None:
        controller = self.controller()
        slot_root = controller.venv_root / "md-a"
        binary = slot_root / "venv/bin/python"
        binary.parent.mkdir(parents=True, mode=0o700)
        binary.symlink_to(Path(sys.executable).resolve())
        process = subprocess.Popen(
            [str(binary), "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            with self.assertRaisesRegex(
                CONTROLLER.PullDeployError,
                "inactive slot is still used",
            ):
                controller._assert_slot_not_running(slot_root)
        finally:
            process.terminate()
            process.wait(timeout=10)

    def test_active_slot_uses_a_b_values_canonical_digest_and_no_current_symlink(
        self,
    ) -> None:
        controller = self.controller()
        record = controller.prepare_md_slot(
            operation_id=OPERATION_ID,
            target_sha=TARGET_SHA,
            target_tree=TARGET_TREE,
            lock_payload=b"fixture-lock\n",
        )
        descriptor = {
            "operation_id": OPERATION_ID,
            "monomer_md": {
                "slot": record["slot"],
                "slot_record": record,
                "slot_record_sha256": CONTROLLER.canonical_json_digest(record),
            },
        }
        active = controller._activate_slot(descriptor)
        self.assertIn(active["slot"], {"a", "b"})
        self.assertEqual(
            active["slot_record_sha256"], CONTROLLER.canonical_json_digest(record)
        )
        self.assertFalse((self.runtime / "worker-venvs/md-current").exists())
        self.assertEqual(controller.choose_inactive_slot(), "b")

    def test_prepare_seals_descriptor_and_apply_rejects_tampering(self) -> None:
        controller = self.controller()
        prepared = controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        self.assertEqual(prepared["status"], "ready")
        descriptor_path = (
            self.runtime / "state/prepared" / OPERATION_ID / "descriptor.json"
        )
        descriptor = CONTROLLER.validate_descriptor(
            CONTROLLER.load_private_json(descriptor_path)
        )
        self.assertEqual(descriptor["repository"]["target_tree"], TARGET_TREE)
        descriptor["compose"]["sha256"] = "sha256:" + "0" * 64
        CONTROLLER.atomic_json(descriptor_path, descriptor)
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "READY record differs"):
            controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)

    def test_new_prepare_accepts_token_rotation_but_apply_rejects_same_operation_drift(
        self,
    ) -> None:
        controller = self.controller()
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        token = self.runtime / "config/github-api-token"
        write_private(token, "rotated-token\n")
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "production configuration changed after prepare",
        ):
            controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)

        # A distinct operation may intentionally seal the rotated credential.
        next_operation = "deploy-20260716-0002"
        next_controller = self.controller()
        prepared = next_controller.prepare(
            target_sha=TARGET_SHA,
            operation_id=next_operation,
        )
        self.assertEqual(prepared["status"], "ready")

    def test_slots_rotate_a_b_a(self) -> None:
        controller = self.controller()

        def prepare_and_activate(operation: str, payload: bytes) -> dict[str, object]:
            record = controller.prepare_md_slot(
                operation_id=operation,
                target_sha=TARGET_SHA,
                target_tree=TARGET_TREE,
                lock_payload=payload,
            )
            CONTROLLER.atomic_json(
                controller.active_slot_path,
                {
                    "schema_version": CONTROLLER.ACTIVE_SLOT_SCHEMA_VERSION,
                    "component": "monomer-md",
                    "slot": record["slot"],
                    "source_sha": TARGET_SHA,
                    "source_tree": TARGET_TREE,
                    "worker_lock_sha256": record["worker_lock_sha256"],
                    "slot_record_sha256": CONTROLLER.worker_record_digest(record),
                    "operation_id": operation,
                    "activated_at": CONTROLLER.utc_now(),
                },
            )
            return record

        first = prepare_and_activate("deploy-round-a1", b"one\n")
        second = prepare_and_activate("deploy-round-b2", b"two\n")
        third = prepare_and_activate("deploy-round-a3", b"three\n")
        self.assertEqual(
            [first["slot"], second["slot"], third["slot"]], ["a", "b", "a"]
        )


class StrictLifecycleEvidenceTests(unittest.TestCase):
    def test_test_root_mode_rejects_parent_child_overlap_with_production(self) -> None:
        with mock.patch.dict(os.environ, {"NEXPOLY_ALLOW_TEST_ROOT": "1"}):
            for runtime_root, production_root in (
                (CONTROLLER.RUNTIME_ROOT / "child", Path("/tmp/isolated-source")),
                (CONTROLLER.RUNTIME_ROOT.parent, Path("/tmp/isolated-source")),
                (Path("/tmp/isolated-runtime"), CONTROLLER.PRODUCTION_ROOT / "child"),
                (Path("/tmp/shared/root/runtime"), Path("/tmp/shared/root")),
            ):
                with (
                    self.subTest(
                        runtime_root=runtime_root, production_root=production_root
                    ),
                    self.assertRaisesRegex(
                        CONTROLLER.PullDeployError, "forbidden for production paths"
                    ),
                ):
                    CONTROLLER.test_root_mode(
                        runtime_root=runtime_root,
                        production_root=production_root,
                    )

    def active_payload(self, *, version: int = 1) -> dict[str, object]:
        fields = (
            CONTROLLER.ACTIVE_JOB_FIELDS_V1
            if version == 1
            else CONTROLLER.ACTIVE_JOB_FIELDS_V2
        )
        return {
            "active_jobs_schema_version": version,
            "drain": {
                "enabled": True,
                "reason": "fixture drain",
                "release_sha": TARGET_SHA,
                "activated_at": CONTROLLER.utc_now(),
                "activated_by": "pull-deploy-controller",
                "updated_at": CONTROLLER.utc_now(),
            },
            "active_jobs": {name: 0 for name in fields},
            "active_total": 0,
        }

    def test_active_jobs_rejects_unknown_categories_and_boolean_counts(self) -> None:
        payload = self.active_payload()
        payload["active_jobs"]["unexpected"] = 0  # type: ignore[index]
        with self.assertRaises(CONTROLLER.PullDeployError):
            CONTROLLER.validate_active_jobs_evidence(payload, require_drained=True)
        payload = self.active_payload()
        payload["active_jobs"]["monomer_md"] = False  # type: ignore[index]
        with self.assertRaises(CONTROLLER.PullDeployError):
            CONTROLLER.validate_active_jobs_evidence(payload, require_drained=True)

    def test_worker_resume_requires_accepting_zero_work(self) -> None:
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "did not resume"):
            CONTROLLER.validate_worker_control_evidence(
                {
                    "status": "ready",
                    "accepting_jobs": False,
                    "active_jobs": 0,
                    "worker_instance_id": "worker-1",
                },
                action="resume",
                require_zero=True,
            )

    def test_worker_unchanged_resume_allows_capacity_full_active_job(self) -> None:
        evidence = CONTROLLER.validate_worker_control_evidence(
            {
                "status": "ready",
                "accepting_jobs": False,
                "active_jobs": 1,
                "worker_instance_id": "worker-1",
            },
            action="resume-unchanged",
            require_zero=False,
        )
        self.assertEqual(evidence["active_jobs"], 1)
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "did not resume unchanged"
        ):
            CONTROLLER.validate_worker_control_evidence(
                {
                    "status": "ready",
                    "accepting_jobs": False,
                    "active_jobs": 0,
                    "worker_instance_id": "worker-1",
                },
                action="resume-unchanged",
                require_zero=False,
            )

    def test_backend_resume_rejects_a_still_drained_response(self) -> None:
        payload = self.active_payload()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "differs from the expected admission",
        ):
            CONTROLLER.validate_active_jobs_evidence(
                payload,
                require_drained=False,
                require_resumed=True,
            )


class SystemDrainFencingTests(unittest.TestCase):
    class Harness(CONTROLLER.SystemLifecycle):
        def __init__(self) -> None:
            self.backend_statuses: list[dict[str, object]] = []
            self.backend_processes: list[dict[str, object]] = []
            self.socket_sets: list[list[tuple[str, Path]]] = []
            self.worker_health: list[dict[str, object]] = []
            self.identity_checks = 0

        @staticmethod
        def _next(values):  # type: ignore[no-untyped-def]
            if len(values) > 1:
                return values.pop(0)
            return values[0]

        def _backend_active_status(self, _controller, _descriptor):  # type: ignore[no-untyped-def]
            return self._next(self.backend_statuses)

        def _backend_process_identity(self, _controller, _descriptor):  # type: ignore[no-untyped-def]
            return self._next(self.backend_processes)

        def _worker_sockets(self, _controller, *, require_md=False):  # type: ignore[no-untyped-def]
            del require_md
            return self._next(self.socket_sets)

        def _worker_request(self, _controller, _socket, *, method, endpoint):  # type: ignore[no-untyped-def]
            self.assert_request(method, endpoint)
            return self._next(self.worker_health)

        @staticmethod
        def assert_request(method: str, endpoint: str) -> None:
            if (method, endpoint) != ("GET", "/health"):
                raise AssertionError((method, endpoint))

        def _validate_worker_runtime_identity(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            del args, kwargs
            self.identity_checks += 1
            return {}

    @staticmethod
    def backend_status(
        active: int, *, actor: str = "pull-deploy-controller"
    ) -> dict[str, object]:
        counts = {name: 0 for name in CONTROLLER.ACTIVE_JOB_FIELDS_V1}
        counts["monomer_md"] = active
        return {
            "active_jobs_schema_version": 1,
            "drain": {
                "enabled": True,
                "reason": "fixture drain",
                "release_sha": TARGET_SHA,
                "activated_at": CONTROLLER.utc_now(),
                "activated_by": actor,
                "updated_at": CONTROLLER.utc_now(),
            },
            "active_jobs": counts,
            "active_total": active,
        }

    @staticmethod
    def worker_health(active: int) -> dict[str, object]:
        return {
            "status": "ok",
            "accepting_jobs": False,
            "draining": True,
            "active_jobs": active,
            "worker_instance_id": "worker-fixed",
        }

    @staticmethod
    def descriptor() -> dict[str, object]:
        return {"repository": {"target_sha": TARGET_SHA}}

    def configured(self) -> "SystemDrainFencingTests.Harness":
        harness = self.Harness()
        process = {
            "container_id": "a" * 64,
            "image_id": "sha256:" + "b" * 64,
            "pid": 123,
            "started_at": "2026-07-17T00:00:00Z",
            "restart_count": 0,
        }
        harness.backend_statuses = [
            self.backend_status(1),
            self.backend_status(0),
        ]
        harness.backend_processes = [process, process]
        harness.socket_sets = [
            [("monomer-md", Path("/fixture/md.sock"))],
            [("monomer-md", Path("/fixture/md.sock"))],
        ]
        harness.worker_health = [self.worker_health(1), self.worker_health(0)]
        return harness

    def test_wait_allows_work_to_finish_while_fencing_exact_instances(self) -> None:
        harness = self.configured()
        with mock.patch.object(CONTROLLER.time, "sleep", return_value=None):
            evidence = harness._wait_for_zero_work(
                object(),
                self.descriptor(),
                {"monomer-md": "worker-fixed"},
                harness.backend_processes[0],
            )
        self.assertEqual(evidence["backend"]["active_total"], 0)
        self.assertEqual(harness.identity_checks, 2)

    def test_wait_rejects_foreign_drain_owner_and_instance_or_socket_drift(
        self,
    ) -> None:
        harness = self.configured()
        harness.backend_statuses[0] = self.backend_status(1, actor="other")
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "ownership"):
            harness._wait_for_zero_work(
                object(),
                self.descriptor(),
                {"monomer-md": "worker-fixed"},
                harness.backend_processes[0],
            )

        harness = self.configured()
        harness.worker_health[1] = {
            **harness.worker_health[1],
            "worker_instance_id": "worker-restarted",
        }
        with (
            mock.patch.object(CONTROLLER.time, "sleep", return_value=None),
            self.assertRaisesRegex(CONTROLLER.PullDeployError, "instance changed"),
        ):
            harness._wait_for_zero_work(
                object(),
                self.descriptor(),
                {"monomer-md": "worker-fixed"},
                harness.backend_processes[0],
            )

        harness = self.configured()
        harness.backend_processes[1] = {**harness.backend_processes[0], "pid": 999}
        with (
            mock.patch.object(CONTROLLER.time, "sleep", return_value=None),
            self.assertRaisesRegex(
                CONTROLLER.PullDeployError, "Backend instance changed"
            ),
        ):
            harness._wait_for_zero_work(
                object(),
                self.descriptor(),
                {"monomer-md": "worker-fixed"},
                harness.backend_processes[0],
            )

        harness = self.configured()
        harness.socket_sets[1] = [
            ("monomer-md", Path("/fixture/md.sock")),
            ("monomer-dft", Path("/fixture/dft.sock")),
        ]
        with (
            mock.patch.object(CONTROLLER.time, "sleep", return_value=None),
            self.assertRaisesRegex(CONTROLLER.PullDeployError, "socket set changed"),
        ):
            harness._wait_for_zero_work(
                object(),
                self.descriptor(),
                {"monomer-md": "worker-fixed"},
                harness.backend_processes[0],
            )

    def test_resume_unchanged_accepts_full_capacity_without_restarting(self) -> None:
        process = {
            "container_id": "a" * 64,
            "image_id": "sha256:" + "b" * 64,
            "pid": 123,
            "started_at": "2026-07-17T00:00:00Z",
            "restart_count": 0,
        }
        worker_process = {
            "main_pid": 456,
            "invocation_id": "worker-invocation",
            "active_enter_monotonic": 789,
        }

        class ResumeHarness(CONTROLLER.SystemLifecycle):
            def __init__(self) -> None:
                self.requests: list[tuple[str, str]] = []
                self.health_reads = 0
                self.backend_reads = 0
                self.mutate_final = False
                self.control_called = False

            def _environment(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return {}

            def _isolate_ingress(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return None

            def prepare_recovery_runtime(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return {
                    "runtime_state": "drained",
                    "ingress_isolated": True,
                    "verification": {
                        "health": "ok",
                        "recovery_fence": {
                            "backend_process": process,
                            "monomer_md_process": worker_process,
                            "workers": {
                                "monomer-md": {
                                    "socket": "/fixture/md.sock",
                                    "worker_instance_id": "worker-fixed",
                                }
                            },
                        },
                    },
                }

            def _backend_process_identity(self, _controller, _descriptor):  # type: ignore[no-untyped-def]
                self.backend_reads += 1
                if self.mutate_final and self.backend_reads >= 2:
                    return {**process, "pid": 999}
                return process

            def _worker_process_identity(self, _controller, _descriptor):  # type: ignore[no-untyped-def]
                return worker_process

            def _worker_sockets(self, _controller, *, require_md=False):  # type: ignore[no-untyped-def]
                del require_md
                return [("monomer-md", Path("/fixture/md.sock"))]

            def _worker_request(self, _controller, _socket, *, method, endpoint):  # type: ignore[no-untyped-def]
                self.requests.append((method, endpoint))
                return {
                    "status": "ready",
                    "accepting_jobs": True,
                    "active_jobs": 0,
                    "worker_instance_id": "worker-fixed",
                }

            def _validate_worker_runtime_identity(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                del args, kwargs
                return {}

            def verify_runtime_identity(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                del args, kwargs
                return {}

            def _capture_runtime_recovery_fence(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                backend = self._backend_process_identity(None, None)
                return {
                    "backend_process": backend,
                    "monomer_md_process": worker_process,
                    "workers": {
                        "monomer-md": {
                            "socket": "/fixture/md.sock",
                            "worker_instance_id": "worker-fixed",
                        }
                    },
                }

            def admission_is_open(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return self.control_called

            def verify_open_runtime(self, _controller, _descriptor, verification):  # type: ignore[no-untyped-def]
                if (
                    self._expected_runtime_recovery_fence(verification)[
                        "backend_process"
                    ]
                    != process
                ):
                    raise AssertionError("unexpected final fence")

            def _control_cli(self, _controller, _descriptor, *arguments):  # type: ignore[no-untyped-def]
                self.assertEqual(arguments[0], "resume")
                self.control_called = True
                counts = {name: 0 for name in CONTROLLER.PERSISTENT_JOB_FIELDS_V1}
                return {
                    "drain": {
                        "enabled": False,
                        "reason": None,
                        "release_sha": None,
                        "activated_at": None,
                        "activated_by": None,
                        "updated_at": CONTROLLER.utc_now(),
                    },
                    "active_jobs": counts,
                    "active_total": 0,
                }

            @staticmethod
            def assertEqual(left, right):  # type: ignore[no-untyped-def]
                if left != right:
                    raise AssertionError((left, right))

        class Runner:
            @staticmethod
            def run(*args, **kwargs):  # type: ignore[no-untyped-def]
                del args, kwargs
                return subprocess.CompletedProcess([], 0, "", "")

        controller = type(
            "Controller",
            (),
            {
                "runner": Runner(),
                "production_root": Path("/fixture/source"),
                "config_dir": Path("/fixture/config"),
                "control_environment": lambda _self: {},
            },
        )()
        descriptor = {"repository": {"target_sha": TARGET_SHA}}
        harness = ResumeHarness()
        persisted: list[dict[str, object]] = []
        harness.resume_unchanged(controller, descriptor, persisted.append)
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["recovery_fence"]["backend_process"], process)
        self.assertEqual(
            harness.requests,
            [("POST", "/resume")],
        )
        self.assertTrue(harness.control_called)

        restarted = ResumeHarness()
        restarted.mutate_final = True
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "changed"):
            restarted.resume_unchanged(controller, descriptor, lambda _value: None)
        self.assertFalse(restarted.control_called)

    def test_open_runtime_recovery_rejects_instance_different_from_marker(self) -> None:
        expected = {
            "backend_process": {
                "container_id": "a" * 64,
                "image_id": "sha256:" + "b" * 64,
                "pid": 123,
                "started_at": "2026-07-17T00:00:00Z",
                "restart_count": 0,
            },
            "monomer_md_process": {
                "main_pid": 456,
                "invocation_id": "worker-invocation",
                "active_enter_monotonic": 789,
            },
            "workers": {
                "monomer-md": {
                    "socket": "/fixture/md.sock",
                    "worker_instance_id": "worker-fixed",
                }
            },
        }

        class OpenHarness(CONTROLLER.SystemLifecycle):
            def admission_is_open(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return True

            def verify_runtime_identity(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                del args, kwargs
                return {
                    "containers": {"backend": {"container_id": "a" * 64}},
                    "worker": {"worker_instance_id": "worker-fixed"},
                }

            def _capture_runtime_recovery_fence(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                del args, kwargs
                return {
                    **expected,
                    "backend_process": {
                        **expected["backend_process"],
                        "pid": 999,
                    },
                }

        class Runner:
            @staticmethod
            def run(*args, **kwargs):  # type: ignore[no-untyped-def]
                del args, kwargs
                return None

        controller = type(
            "Controller",
            (),
            {"runner": Runner(), "control_environment": lambda _self: {}},
        )()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "differs from committed verification"
        ):
            OpenHarness().verify_open_runtime(
                controller,
                {},
                {"recovery_fence": expected},
            )

    def test_resume_keeps_ingress_isolated_until_backend_and_fence_are_open(
        self,
    ) -> None:
        fence = {
            "backend_process": {
                "container_id": "a" * 64,
                "image_id": "sha256:" + "b" * 64,
                "pid": 123,
                "started_at": "2026-07-17T00:00:00Z",
                "restart_count": 0,
            },
            "monomer_md_process": {
                "main_pid": 456,
                "invocation_id": "worker-invocation",
                "active_enter_monotonic": 789,
            },
            "workers": {
                "monomer-md": {
                    "socket": "/fixture/md.sock",
                    "worker_instance_id": "worker-fixed",
                }
            },
        }

        class OrderingHarness(CONTROLLER.SystemLifecycle):
            def __init__(self, *, fail_final: bool = False) -> None:
                self.events: list[str] = []
                self.fail_final = fail_final
                self.backend_open = False

            def _environment(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return {}

            def _isolate_ingress(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.events.append("isolate-ingress")

            def _capture_runtime_recovery_fence(self, *_args, resumed, **_kwargs):  # type: ignore[no-untyped-def]
                self.events.append(f"capture-{resumed}")
                return fence

            def _worker_sockets(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                return [("monomer-md", Path("/fixture/md.sock"))]

            def _worker_request(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.events.append("worker-resume")
                return {
                    "status": "ready",
                    "accepting_jobs": True,
                    "active_jobs": 0,
                    "worker_instance_id": "worker-fixed",
                }

            def _control_cli(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.events.append("backend-resume")
                self.backend_open = True
                counts = {name: 0 for name in CONTROLLER.PERSISTENT_JOB_FIELDS_V1}
                return {
                    "drain": {
                        "enabled": False,
                        "reason": None,
                        "release_sha": None,
                        "activated_at": None,
                        "activated_by": None,
                        "updated_at": CONTROLLER.utc_now(),
                    },
                    "active_jobs": counts,
                    "active_total": 0,
                }

            def admission_is_open(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.events.append("admission-status")
                return self.backend_open

            def verify_runtime_identity(self, *_args, **kwargs):  # type: ignore[no-untyped-def]
                if kwargs.get("require_ingress") is not False:
                    raise AssertionError("internal verification exposed ingress")
                self.events.append("verify-internal")
                return {}

            def verify_open_runtime(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.events.append("verify-open")
                if self.fail_final:
                    raise CONTROLLER.PullDeployError(
                        "injected final open verification failure"
                    )

            def _compose(self, _controller, *arguments):  # type: ignore[no-untyped-def]
                return ["compose", *arguments]

        class Runner:
            def __init__(self, lifecycle: OrderingHarness) -> None:
                self.lifecycle = lifecycle

            def run(self, command, **_kwargs):  # type: ignore[no-untyped-def]
                if "up" in command and command[-1] == "nginx":
                    if not self.lifecycle.backend_open:
                        raise AssertionError("nginx started before Backend admission")
                    self.lifecycle.events.append("nginx-start")
                return subprocess.CompletedProcess(command, 0, "", "")

        descriptor = {"repository": {"target_sha": TARGET_SHA}}
        verification = {"health": "ok", "recovery_fence": fence}
        lifecycle = OrderingHarness()
        controller = type(
            "Controller",
            (),
            {
                "runner": Runner(lifecycle),
                "production_root": Path("/fixture/source"),
                "control_environment": lambda _self: {},
            },
        )()
        lifecycle.resume(controller, descriptor, verification)
        self.assertEqual(
            lifecycle.events,
            [
                "isolate-ingress",
                "capture-False",
                "worker-resume",
                "capture-True",
                "backend-resume",
                "admission-status",
                "verify-internal",
                "capture-True",
                "nginx-start",
                "verify-open",
            ],
        )

        failing = OrderingHarness(fail_final=True)
        failing_controller = type(
            "Controller",
            (),
            {
                "runner": Runner(failing),
                "production_root": Path("/fixture/source"),
                "control_environment": lambda _self: {},
            },
        )()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "final open verification"
        ):
            failing.resume(failing_controller, descriptor, verification)
        self.assertEqual(failing.events[-1], "isolate-ingress")


class LifecycleStateMachineTests(PullDeployTestCase):
    def test_stopped_start_unknown_commit_recovers_authorized_new_instance(
        self,
    ) -> None:
        class LostStartLifecycle(FakeLifecycle):
            lose_start = True

            def start(self, controller, _descriptor):  # type: ignore[no-untyped-def]
                persisted = CONTROLLER.load_private_json(controller.marker_path)
                self.assert_postgres_fence(persisted)
                self._event("start")
                self.runtime_state = "live"
                self.admission_open = False
                self.recovery_fence = {"fixture_instance": "started-instance"}
                if self.lose_start:
                    self.lose_start = False
                    raise CONTROLLER.PullDeployError("injected start response loss")

            @staticmethod
            def assert_postgres_fence(marker):  # type: ignore[no-untyped-def]
                expected = CONTROLLER.postgres_runtime_fence_identity(
                    {
                        "schema_version": 1,
                        **mutable_data_evidence()["postgres_runtime"],
                        "captured_at": marker["postgres_runtime_fence"][
                            "captured_at"
                        ],
                    }
                )
                actual = CONTROLLER.postgres_runtime_fence_identity(
                    marker["postgres_runtime_fence"]
                )
                if actual != expected:
                    raise AssertionError(
                        "PostgreSQL runtime fence was not durable before start"
                    )

        lifecycle = LostStartLifecycle()
        lifecycle.runtime_state = "stopped"
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        marker = {
            "phase": "state-committed",
            "updated_at": CONTROLLER.utc_now(),
        }
        CONTROLLER.atomic_json(controller.marker_path, marker)

        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "start response loss"):
            controller._recover_runtime_and_resume(
                marker,
                descriptor,
                allow_unfenced=False,
            )
        persisted = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(
            persisted["runtime_start_intent"]["target_sha"],
            TARGET_SHA,
        )
        self.assertEqual(
            CONTROLLER.postgres_runtime_fence_identity(
                persisted["postgres_runtime_fence"]
            ),
            {
                "schema_version": 1,
                **mutable_data_evidence()["postgres_runtime"],
            },
        )
        self.assertNotIn("verification", persisted)

        lifecycle.events.clear()
        controller._recover_runtime_and_resume(
            persisted,
            descriptor,
            allow_unfenced=False,
        )
        self.assertEqual(
            lifecycle.events,
            ["recovery-isolate", "recovery-redrain", "verify", "resume"],
        )
        final_marker = CONTROLLER.load_private_json(controller.marker_path)
        self.assertNotIn("runtime_start_intent", final_marker)
        self.assertEqual(
            final_marker["verification"]["recovery_fence"],
            {"fixture_instance": "started-instance"},
        )

    def test_apply_uses_prepared_evidence_and_commits_state_after_verification(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        self.assertEqual(state["source_sha"], TARGET_SHA)
        self.assertEqual(controller.source_sha, TARGET_SHA)
        self.assertEqual(
            lifecycle.events,
            [
                "drain",
                "recovery-isolate",
                "recovery-redrain",
                "stop",
                "backup",
                "migrate",
                "start",
                "verify",
                "resume",
            ],
        )
        self.assertFalse((self.runtime / "state/deploy-in-progress.json").exists())
        self.assertTrue(
            (self.runtime / "audit" / OPERATION_ID / "success.json").is_file()
        )
        current = CONTROLLER.load_private_json(
            self.runtime / "state/current-deployment.json"
        )
        self.assertEqual(current["active_monomer_md_slot"]["slot"], "a")
        self.assertEqual(
            [record["version"] for record in current["migrations"]],
            ["0010_deployment_control"],
        )

    def test_failure_is_rolled_back_and_audited_without_leaving_marker(self) -> None:
        lifecycle = FakeLifecycle(fail_at="start")
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "injected start"):
            controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        self.assertTrue(controller.rollback_called)
        self.assertEqual(controller.source_sha, PREVIOUS_SHA)
        self.assertFalse((self.runtime / "state/deploy-in-progress.json").exists())
        failed = CONTROLLER.load_private_json(
            self.runtime / "audit" / OPERATION_ID / "failed.json"
        )
        self.assertEqual(failed["rollback"], "success")

    def test_committed_state_crash_recovers_forward_instead_of_restoring_database(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": CONTROLLER.load_private_json(descriptor_path)[
                "controller"
            ]["executor_control"],
            "executor_control_sha256": CONTROLLER.load_private_json(descriptor_path)[
                "controller"
            ]["executor_control_sha256"],
            "phase": "state-committed",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": True,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": True,
            "database_change_started": True,
            "verification": lifecycle.verification(),
            "candidate_state": state,
            "candidate_state_sha256": CONTROLLER.sha256_bytes(
                CONTROLLER.canonical_json_bytes(state) + b"\n"
            ),
        }
        CONTROLLER.atomic_json(controller.marker_path, marker)
        lifecycle.events.clear()
        recovered = controller.recover_interrupted()
        self.assertEqual(recovered, state)
        self.assertEqual(
            lifecycle.events,
            ["recovery-isolate", "recovery-redrain", "verify", "resume"],
        )
        self.assertFalse(controller.marker_path.exists())

    def test_open_admission_unknown_commit_keeps_marker_on_instance_drift(self) -> None:
        initial = FakeLifecycle()
        controller = self.controller(lifecycle=initial)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        verification = {"health": "ok", "recovery_fence": {"fixture": True}}
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "state-committed",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": True,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": True,
            "database_change_started": True,
            "verification": verification,
            "candidate_state": state,
            "candidate_state_sha256": CONTROLLER.sha256_bytes(
                CONTROLLER.canonical_json_bytes(state) + b"\n"
            ),
        }
        CONTROLLER.atomic_json(controller.marker_path, marker)

        class RestartedOpenLifecycle(FakeLifecycle):
            def verify_open_runtime(
                self,
                _controller: object,
                _descriptor: object,
                expected_verification: object | None = None,
            ) -> None:
                self._event("verify-open-restarted")
                if expected_verification != verification:
                    raise AssertionError(expected_verification)
                raise CONTROLLER.PullDeployError(
                    "open runtime instance differs from committed verification"
                )

        restarted = RestartedOpenLifecycle(admission_open=True)
        controller.lifecycle = restarted
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "differs from committed verification"
        ):
            controller.recover_interrupted()
        self.assertTrue(controller.marker_path.is_file())
        self.assertEqual(
            restarted.events,
            ["recovery-isolate"],
        )
        self.assertNotIn("stop", restarted.events)
        self.assertNotIn("start", restarted.events)

    def test_candidate_reverify_fence_survives_lost_resume_response(self) -> None:
        lifecycle = LostResumeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        state = controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _operation, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "state-committed",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": True,
            "control_switched": True,
            "unit_switched": True,
            "asset_switched": True,
            "database_change_started": True,
            "verification": {
                "health": "ok",
                "recovery_fence": {"fixture_instance": "stale-instance"},
            },
            "candidate_state": state,
            "candidate_state_sha256": CONTROLLER.sha256_bytes(
                CONTROLLER.canonical_json_bytes(state) + b"\n"
            ),
        }
        CONTROLLER.atomic_json(controller.marker_path, marker)
        lifecycle.admission_open = False
        lifecycle.runtime_state = "stopped"
        lifecycle.recovery_fence = {"fixture_instance": "reverified-instance"}
        lifecycle.lose_next_resume = True
        lifecycle.events.clear()

        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "lost response after admission commit"
        ):
            controller.recover_interrupted()
        persisted = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(persisted["verification"], lifecycle.verification())
        self.assertTrue(lifecycle.admission_open)

        lifecycle.events.clear()
        recovered = controller.recover_interrupted()
        self.assertEqual(recovered, state)
        self.assertEqual(
            lifecycle.events,
            ["recovery-isolate", "recovery-redrain", "verify", "resume"],
        )
        self.assertFalse(controller.marker_path.exists())
        self.assertNotIn("stop", lifecycle.events)

    def test_explicit_rollback_unknown_commit_rejects_changed_instance(self) -> None:
        lifecycle = LostResumeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        second_operation = "deploy-20260716-explicit-fence"
        controller.prepare(target_sha=TARGET_SHA, operation_id=second_operation)
        controller.apply(target_sha=TARGET_SHA, operation_id=second_operation)
        lifecycle.lose_next_resume = True
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "lost response after admission commit"
        ):
            controller.rollback(operation_id=second_operation)
        persisted = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(persisted["verification"], lifecycle.verification())

        lifecycle.recovery_fence = {"fixture_instance": "replacement-instance"}
        lifecycle.events.clear()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "differs from committed verification"
        ):
            controller.recover_interrupted()
        self.assertTrue(controller.marker_path.is_file())
        self.assertEqual(lifecycle.events, ["recovery-isolate"])
        self.assertNotIn("start", lifecycle.events)
        self.assertNotIn("stop", lifecycle.events)

    def test_failed_deploy_rollback_unknown_commit_rejects_changed_instance(
        self,
    ) -> None:
        class FailedCandidateLifecycle(LostResumeLifecycle):
            fail_next_verify = False

            def verify(self, controller, descriptor):  # type: ignore[no-untyped-def]
                if self.fail_next_verify:
                    self._event("verify")
                    self.fail_next_verify = False
                    self.lose_next_resume = True
                    raise CONTROLLER.PullDeployError(
                        "injected candidate verification failure"
                    )
                return super().verify(controller, descriptor)

        lifecycle = FailedCandidateLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller._rollback_failed_attempt = (  # type: ignore[method-assign]
            CONTROLLER.PullDeployController._rollback_failed_attempt.__get__(
                controller, CONTROLLER.PullDeployController
            )
        )
        # The fixture uses one synthetic target SHA for every operation.  A
        # real failed upgrade has a distinct candidate, so project the sealed
        # previous state as non-candidate for this rollback-path test.
        controller._candidate_current_state = lambda *_args: None  # type: ignore[method-assign]
        second_operation = "deploy-20260716-failed-rollback-fence"
        controller.prepare(target_sha=TARGET_SHA, operation_id=second_operation)
        lifecycle.fail_next_verify = True
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError,
            "deployment and rollback failed",
        ):
            controller.apply(target_sha=TARGET_SHA, operation_id=second_operation)
        persisted = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(persisted["verification"], lifecycle.verification())
        self.assertTrue(lifecycle.admission_open)

        controller._reconcile_effect_commit_windows = lambda *_args: None  # type: ignore[method-assign]
        lifecycle.recovery_fence = {"fixture_instance": "replacement-instance"}
        lifecycle.events.clear()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "differs from committed verification"
        ):
            controller.recover_interrupted()
        self.assertTrue(controller.marker_path.is_file())
        self.assertEqual(lifecycle.events, ["recovery-isolate"])
        self.assertNotIn("start", lifecycle.events)
        self.assertNotIn("stop", lifecycle.events)

    def test_pre_stop_unknown_commit_rejects_changed_instance(self) -> None:
        lifecycle = LostUnchangedResumeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller._rollback_failed_attempt = (  # type: ignore[method-assign]
            CONTROLLER.PullDeployController._rollback_failed_attempt.__get__(
                controller, CONTROLLER.PullDeployController
            )
        )
        operation = "deploy-20260716-prestop-fence"
        controller.prepare(target_sha=TARGET_SHA, operation_id=operation)
        _directory, descriptor_path, _ready = controller._operation_paths(operation)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": operation,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "drained",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": False,
            "source_switched": False,
            "slot_switched": False,
            "control_switched": False,
            "unit_switched": False,
            "asset_switched": False,
            "database_change_started": False,
            "drain": {"active_total": 0},
        }
        CONTROLLER.atomic_json(controller.marker_path, marker)
        lifecycle.admission_open = False
        lifecycle.lose_next_unchanged_resume = True
        lifecycle.events.clear()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "lost unchanged-resume response"
        ):
            controller.recover_interrupted()
        persisted = CONTROLLER.load_private_json(controller.marker_path)
        self.assertEqual(persisted["verification"], lifecycle.verification())
        self.assertTrue(lifecycle.admission_open)

        lifecycle.recovery_fence = {"fixture_instance": "replacement-instance"}
        lifecycle.events.clear()
        with self.assertRaisesRegex(
            CONTROLLER.PullDeployError, "differs from committed verification"
        ):
            controller.recover_interrupted()
        self.assertTrue(controller.marker_path.is_file())
        self.assertEqual(lifecycle.events, ["recovery-isolate"])
        self.assertNotIn("start", lifecycle.events)
        self.assertNotIn("stop", lifecycle.events)

    def test_pre_stop_crashes_resume_unchanged_without_reconcile_or_restart(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller._rollback_failed_attempt = (  # type: ignore[method-assign]
            CONTROLLER.PullDeployController._rollback_failed_attempt.__get__(
                controller, CONTROLLER.PullDeployController
            )
        )

        phases = [
            ("drain-started", None),
            ("drained", None),
            ("failed", "drained"),
        ]
        for index, (phase, failed_phase) in enumerate(phases, start=2):
            operation = f"deploy-20260716-prestop-{index}"
            controller.prepare(target_sha=TARGET_SHA, operation_id=operation)
            _directory, descriptor_path, _ready = controller._operation_paths(operation)
            descriptor = CONTROLLER.load_private_json(descriptor_path)
            marker = {
                "schema_version": 2,
                "action": "deploy",
                "operation_id": operation,
                "source_sha": TARGET_SHA,
                "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
                "executor_control": descriptor["controller"]["executor_control"],
                "executor_control_sha256": descriptor["controller"][
                    "executor_control_sha256"
                ],
                "phase": phase,
                "started_at": CONTROLLER.utc_now(),
                "updated_at": CONTROLLER.utc_now(),
                "runtime_stopped": False,
                "source_switched": False,
                "slot_switched": False,
                "control_switched": False,
                "unit_switched": False,
                "asset_switched": False,
                "database_change_started": False,
            }
            if phase in {"drained", "failed"}:
                marker["drain"] = {"active_total": 0}
            if failed_phase is not None:
                marker["failed_phase"] = failed_phase
            CONTROLLER.atomic_json(controller.marker_path, marker)
            lifecycle.admission_open = False
            lifecycle.events.clear()
            recovered = controller.recover_interrupted()
            self.assertIsNone(recovered)
            expected = [
                "recovery-isolate",
                "recovery-redrain",
                "resume-unchanged",
            ]
            self.assertEqual(lifecycle.events, expected)
            self.assertNotIn("start", lifecycle.events)
            self.assertNotIn("stop", lifecycle.events)
            self.assertFalse(controller.marker_path.exists())

    def test_open_pre_stop_intent_without_fence_isolated_and_redrained(self) -> None:
        lifecycle = FakeLifecycle(admission_open=True)
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller._rollback_failed_attempt = (  # type: ignore[method-assign]
            CONTROLLER.PullDeployController._rollback_failed_attempt.__get__(
                controller, CONTROLLER.PullDeployController
            )
        )
        operation = "deploy-20260716-prestop-no-fence"
        controller.prepare(target_sha=TARGET_SHA, operation_id=operation)
        _directory, descriptor_path, _ready = controller._operation_paths(operation)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": operation,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "prepared",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": False,
            "source_switched": False,
            "slot_switched": False,
            "control_switched": False,
            "unit_switched": False,
            "asset_switched": False,
            "database_change_started": False,
        }
        CONTROLLER.atomic_json(controller.marker_path, marker)
        lifecycle.events.clear()
        self.assertIsNone(controller.recover_interrupted())
        self.assertFalse(controller.marker_path.exists())
        self.assertEqual(
            lifecycle.events,
            ["recovery-isolate", "recovery-redrain", "resume-unchanged"],
        )
        self.assertNotIn("start", lifecycle.events)
        self.assertNotIn("stop", lifecycle.events)

    def test_first_bootstrap_pre_stop_uses_only_legacy_unchanged_resume(self) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _directory, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        self.assertIsNone(descriptor["previous_deployment"])
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "drain-started",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": False,
            "source_switched": False,
            "slot_switched": False,
            "control_switched": False,
            "unit_switched": False,
            "asset_switched": False,
            "database_change_started": False,
        }
        CONTROLLER.atomic_json(controller.marker_path, marker)
        controller._rollback_failed_attempt = (  # type: ignore[method-assign]
            CONTROLLER.PullDeployController._rollback_failed_attempt.__get__(
                controller, CONTROLLER.PullDeployController
            )
        )
        controller.recover_interrupted()
        self.assertEqual(lifecycle.events, ["resume-bootstrap-unchanged"])
        self.assertNotIn("stop", lifecycle.events)

    def test_first_bootstrap_lost_restore_response_never_restarts_open_legacy(
        self,
    ) -> None:
        lifecycle = FakeLifecycle(admission_open=True)
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _directory, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": CONTROLLER.sha256_file(descriptor_path),
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "failed",
            "failed_phase": "runtime-started",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": False,
            "slot_switched": False,
            "control_switched": False,
            "unit_switched": False,
            "asset_switched": False,
            "database_change_started": False,
        }
        controller._reconcile_effect_commit_windows = lambda *_args: None  # type: ignore[method-assign]
        CONTROLLER.PullDeployController._rollback_failed_attempt(
            controller, descriptor, marker
        )
        self.assertEqual(
            lifecycle.events,
            ["bootstrap-admission-status", "resume-bootstrap-unchanged"],
        )
        self.assertNotIn("stop", lifecycle.events)

    def test_post_canary_crash_persists_redrain_before_idempotent_stop_retry(
        self,
    ) -> None:
        lifecycle = FakeLifecycle(fail_at="stop")
        controller = self.controller(lifecycle=lifecycle)
        controller._rollback_failed_attempt = (  # type: ignore[method-assign]
            CONTROLLER.PullDeployController._rollback_failed_attempt.__get__(
                controller, CONTROLLER.PullDeployController
            )
        )
        controller._reconcile_effect_commit_windows = lambda *_args: None  # type: ignore[method-assign]
        descriptor = {"previous_deployment": {"source_sha": PREVIOUS_SHA}}
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": "sha256:" + "a" * 64,
            "phase": "failed",
            "failed_phase": "verifying",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": True,
            "source_switched": False,
            "slot_switched": False,
            "control_switched": False,
            "unit_switched": False,
            "asset_switched": False,
            "database_change_started": False,
        }
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "injected stop"):
            controller._rollback_failed_attempt(descriptor, marker)
        self.assertEqual(
            lifecycle.events,
            ["recovery-isolate", "recovery-redrain", "stop"],
        )
        self.assertEqual(marker["phase"], "runtime-stop-started")
        self.assertIn("drain", marker)

        lifecycle.events.clear()
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "injected stop"):
            controller._rollback_failed_attempt(descriptor, marker)
        self.assertEqual(
            lifecycle.events,
            ["recovery-isolate", "recovery-redrain", "stop"],
        )

    def test_recovery_marker_rejects_invalid_pre_stop_and_rollback_evidence(
        self,
    ) -> None:
        controller = self.controller(lifecycle=FakeLifecycle())
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        _directory, descriptor_path, _ready = controller._operation_paths(OPERATION_ID)
        descriptor = CONTROLLER.load_private_json(descriptor_path)
        descriptor_digest = CONTROLLER.sha256_file(descriptor_path)
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": OPERATION_ID,
            "source_sha": TARGET_SHA,
            "descriptor_sha256": descriptor_digest,
            "executor_control": descriptor["controller"]["executor_control"],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "phase": "drain-started",
            "started_at": CONTROLLER.utc_now(),
            "updated_at": CONTROLLER.utc_now(),
            "runtime_stopped": False,
            "source_switched": False,
            "slot_switched": False,
            "control_switched": False,
            "unit_switched": False,
            "asset_switched": False,
            "database_change_started": False,
            "pre_stop_abort": False,
        }
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "pre-stop abort flag"):
            CONTROLLER.validate_recovery_marker(
                marker,
                descriptor=descriptor,
                descriptor_digest=descriptor_digest,
            )
        marker.pop("pre_stop_abort")
        marker.update(
            action="explicit-rollback",
            phase="explicit-rollback-stop-started",
            rollback_current_state_sha256="sha256:" + "a" * 64,
            rollback_backup_operation_id="rollback-independent-001",
        )
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "drain evidence"):
            CONTROLLER.validate_recovery_marker(
                marker,
                descriptor=descriptor,
                descriptor_digest=descriptor_digest,
            )

    def test_explicit_rollback_uses_independent_backup_and_reuses_it_after_crash(
        self,
    ) -> None:
        lifecycle = FakeLifecycle()
        controller = self.controller(lifecycle=lifecycle)
        controller.prepare(target_sha=TARGET_SHA, operation_id=OPERATION_ID)
        controller.apply(target_sha=TARGET_SHA, operation_id=OPERATION_ID)

        second_operation = "deploy-20260716-0002"
        controller.prepare(target_sha=TARGET_SHA, operation_id=second_operation)
        controller.apply(target_sha=TARGET_SHA, operation_id=second_operation)
        lifecycle.events.clear()

        original_restore = controller._restore_source
        restore_calls = 0

        def fail_restore_once(descriptor):  # type: ignore[no-untyped-def]
            nonlocal restore_calls
            restore_calls += 1
            if restore_calls == 1:
                raise CONTROLLER.PullDeployError("injected source restore crash")
            return original_restore(descriptor)

        controller._restore_source = fail_restore_once  # type: ignore[method-assign]
        with self.assertRaisesRegex(CONTROLLER.PullDeployError, "source restore crash"):
            controller.rollback(operation_id=second_operation)
        marker = CONTROLLER.load_private_json(controller.marker_path)
        rollback_backup = marker["rollback_backup"]
        self.assertNotEqual(
            Path(rollback_backup["path"]).parent,
            controller.backups_dir / second_operation,
        )
        self.assertEqual(lifecycle.events.count("backup"), 1)

        controller._restore_source = original_restore  # type: ignore[method-assign]
        recovered = controller.recover_interrupted()
        self.assertEqual(recovered["operation_id"], OPERATION_ID)
        self.assertEqual(lifecycle.events.count("backup"), 1)
        self.assertFalse(controller.marker_path.exists())

    def test_stable_cli_uses_fixed_roots_and_has_no_extra_mutation_flags(self) -> None:
        parsed = CONTROLLER.parser().parse_args(
            [
                "prepare",
                "--sha",
                TARGET_SHA,
                "--operation-id",
                OPERATION_ID,
            ]
        )
        self.assertFalse(hasattr(parsed, "production_root"))
        self.assertFalse(hasattr(parsed, "runtime_root"))
        self.assertFalse(hasattr(parsed, "apply"))
        with self.assertRaises(SystemExit):
            CONTROLLER.parser().parse_args(
                [
                    "prepare",
                    "--sha",
                    TARGET_SHA,
                    "--operation-id",
                    OPERATION_ID,
                    "--apply",
                ]
            )


class UnitTransitionRunner:
    def __init__(self, target: Path, *, enabled: bool) -> None:
        self.target = target
        self.enabled = enabled
        self.commands: list[list[str]] = []

    def run(
        self, command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[:3] == ["systemctl", "--user", "enable"]:
            self.enabled = True
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["systemctl", "--user", "disable"]:
            self.enabled = False
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["systemctl", "--user", "show"]:
            present = self.target.exists()
            values = {
                "LoadState": "loaded" if present else "not-found",
                "FragmentPath": str(self.target) if present else "",
                "DropInPaths": "",
                "NeedDaemonReload": "no",
                "UnitFileState": "enabled" if present and self.enabled else "",
            }
            output = "".join(f"{key}={value}\n" for key, value in values.items())
            return subprocess.CompletedProcess(command, 0, output, "")
        return subprocess.CompletedProcess(command, 0, "", "")


class WorkerUnitTransitionTests(PullDeployTestCase):
    def _unit_descriptor(
        self, *, previous: bytes | None
    ) -> tuple[object, dict[str, object], Path]:
        target_parent = self.root / "systemd"
        target_parent.mkdir(mode=0o700)
        target = target_parent / CONTROLLER.MONOMER_MD_UNIT_NAME
        candidate = self.runtime / "state/prepared/candidate.service"
        candidate.write_bytes(b"candidate unit\n")
        os.chmod(candidate, 0o600)
        backup: Path | None = None
        if previous is not None:
            backup = self.runtime / "state/prepared/previous.service"
            backup.write_bytes(previous)
            os.chmod(backup, 0o600)
            target.write_bytes(b"candidate unit\n")
            os.chmod(target, 0o600)
        runner = UnitTransitionRunner(target, enabled=previous is not None)
        controller = CONTROLLER.PullDeployController(
            self.production, self.runtime, runner=runner, apply=True
        )
        controller.control_environment = lambda: {}  # type: ignore[method-assign]
        controller._revalidate_worker_controls = lambda _descriptor: None  # type: ignore[method-assign]
        unit = {
            "candidate_path": str(candidate),
            "target_path": str(target),
            "sha256": CONTROLLER.sha256_file(candidate),
            "previous_present": previous is not None,
            "previous_sha256": (
                CONTROLLER.sha256_bytes(previous) if previous is not None else None
            ),
            "previous_backup_path": str(backup) if backup is not None else None,
        }
        return controller, {"monomer_md": {"systemd_unit": unit}}, target

    def test_absent_unit_is_enabled_then_disabled_and_removed_on_rollback(self) -> None:
        controller, descriptor, target = self._unit_descriptor(previous=None)
        controller._install_candidate_worker_unit(descriptor)
        self.assertTrue(target.is_file())
        self.assertIn(
            ["systemctl", "--user", "enable", CONTROLLER.MONOMER_MD_UNIT_NAME],
            controller.runner.commands,
        )
        controller._restore_previous_worker_unit(descriptor)
        self.assertFalse(target.exists())
        self.assertIn(
            ["systemctl", "--user", "disable", CONTROLLER.MONOMER_MD_UNIT_NAME],
            controller.runner.commands,
        )

    def test_existing_enabled_unit_restores_exact_previous_bytes(self) -> None:
        controller, descriptor, target = self._unit_descriptor(previous=b"old unit\n")
        controller._restore_previous_worker_unit(descriptor)
        self.assertEqual(target.read_bytes(), b"old unit\n")
        self.assertNotIn(
            ["systemctl", "--user", "disable", CONTROLLER.MONOMER_MD_UNIT_NAME],
            controller.runner.commands,
        )


class BootstrapQuiesceContractTests(unittest.TestCase):
    def test_example_output_is_accepted_by_dedicated_controller_validator(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bootstrap-quiesce-") as temporary:
            root = Path(temporary)
            production = root / "production"
            runtime = root / "runtime"
            fake_bin = root / "bin"
            production.mkdir()
            (runtime / "config").mkdir(parents=True)
            fake_bin.mkdir()
            docker = fake_bin / "docker"
            docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(docker, 0o700)
            probe = runtime / "config/bootstrap-active-jobs-probe"
            jobs = {name: 0 for name in CONTROLLER.ACTIVE_JOB_FIELDS_V2}
            evidence_source = {
                "ingress_isolated": True,
                "active_jobs": jobs,
                "active_total": 0,
                "active_jobs_schema_version": 2,
            }
            probe.write_text(
                "#!/bin/sh\nprintf '%s\\n' '"
                + json.dumps(evidence_source, separators=(",", ":"))
                + "'\n",
                encoding="utf-8",
            )
            os.chmod(probe, 0o700)
            source = (
                REPOSITORY_ROOT / "ops/config/bootstrap-quiesce.example"
            ).read_text(encoding="utf-8")
            source = source.replace(
                "/data/lzq/gith/nexpoly-runtime", str(runtime)
            ).replace("/data/lzq/gith/nexpoly", str(production))
            hook = root / "bootstrap-quiesce"
            hook.write_text(source, encoding="utf-8")
            os.chmod(hook, 0o700)
            result = subprocess.run(
                [str(hook)],
                cwd=production,
                env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            evidence = json.loads(result.stdout)
            self.assertEqual(
                CONTROLLER.validate_bootstrap_quiesce_evidence(evidence), evidence
            )
            with self.assertRaisesRegex(CONTROLLER.PullDeployError, "invalid shape"):
                CONTROLLER.validate_active_jobs_evidence(evidence, require_drained=True)


if __name__ == "__main__":
    unittest.main()

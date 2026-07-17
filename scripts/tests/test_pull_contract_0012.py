from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import pull_contract_0012 as contract


SHA = "a" * 40
TREE = "b" * 40
DESCRIPTOR_DIGEST = "sha256:" + "c" * 64
ASSET_DIGEST = "sha256:" + "d" * 64
BACKEND_DIGEST = "ghcr.io/lzq390/nexpoly-backend@sha256:" + "e" * 64
WEB_DIGEST = "ghcr.io/lzq390/nexpoly-web@sha256:" + "f" * 64
DEPLOY_OPERATION = "deploy-20260716-0001"
CONTRACT_OPERATION = "contract-0012-20260716"


def _write_private_json(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    path.write_text(json.dumps(document), encoding="utf-8")
    os.chmod(path, 0o600)


def _write_private(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    path.write_text(payload, encoding="utf-8")
    os.chmod(path, 0o600)


def _mutable_data_evidence(
    *,
    operation_id: str = DEPLOY_OPERATION,
    ledger_length: int = 11,
) -> dict[str, object]:
    helpers = contract.pull._site_helper_contracts

    def table_record(
        relation: tuple[str, str],
        index: int,
        *,
        present: bool = True,
        rows: int | None = None,
    ) -> dict[str, object]:
        return {
            "schema": relation[0],
            "table": relation[1],
            "state": "present" if present else "absent",
            "row_count": (
                index + 1 if rows is None else rows
            )
            if present
            else None,
            "schema_sha256": (
                "sha256:" + f"{(index + 1) % 16:x}" * 64
                if present
                else None
            ),
            "content_sha256": (
                "sha256:" + f"{(index + 9) % 16:x}" * 64
                if present
                else None
            ),
        }

    dft_ready = ledger_length == 13
    contract_applied = ledger_length >= 12
    business_tables = [
        table_record(relation, index)
        for index, relation in enumerate(helpers.BUSINESS_MUTABLE_TABLES)
    ]
    business_tables.extend(
        table_record(
            relation,
            index + len(helpers.BUSINESS_MUTABLE_TABLES),
            present=dft_ready,
            rows=0,
        )
        for index, relation in enumerate(
            helpers.POST_0013_BUSINESS_MUTABLE_TABLES
        )
    )
    static_tables = [
        table_record(relation, index + 8)
        for index, relation in enumerate(helpers.STATIC_IMPORT_TABLES)
    ]
    sequences: list[dict[str, object]] = []
    for schema, sequence, owned_by in helpers.DATA_SEQUENCES:
        present = schema != "monomer_dft" or dft_ready
        sequences.append(
            {
                "schema": schema,
                "sequence": sequence,
                "owned_by": owned_by,
                "state": "present" if present else "absent",
                "data_type": "bigint" if present else None,
                "start_value": 1 if present else None,
                "min_value": 1 if present else None,
                "max_value": 9223372036854775807 if present else None,
                "increment_by": 1 if present else None,
                "cache_size": 1 if present else None,
                "cycle": False if present else None,
                "last_value": 1 if present else None,
                "is_called": (
                    False
                    if present and schema == "monomer_dft"
                    else (True if present else None)
                ),
            }
        )
    deployment_table = table_record(
        helpers.GOVERNED_CONTROL_TABLES[0], 23, rows=1
    )
    analytics_table = table_record(
        helpers.GOVERNED_CONTROL_TABLES[1], 24, rows=0
    )
    identity = {
        "operation_id": operation_id,
        "database": contract.pull.MUTABLE_DATA_DATABASE,
        "database_system_identifier": "7659245354718314530",
        "connection": {
            "service": contract.pull.MUTABLE_DATA_SERVICE,
            "host": contract.pull.MUTABLE_DATA_HOST,
            "port": contract.pull.MUTABLE_DATA_PORT,
            "database": contract.pull.MUTABLE_DATA_DATABASE,
            "user": contract.pull.MUTABLE_DATA_USER,
        },
        "postgres_runtime": {
            "container_id": "1" * 64,
            "image_id": "sha256:" + "2" * 64,
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
                "host": contract.pull.MUTABLE_DATA_HOST,
                "port": contract.pull.MUTABLE_DATA_PORT,
                "container_port": 5432,
                "protocol": "tcp",
            },
            "system_identifier": "7659245354718314530",
        },
        "digest_algorithm": "sha256-postgres-jsonb-copy-v2",
        "migration_ledger": [
            {"version": version, "checksum": checksum}
            for version, checksum in helpers.CANONICAL_MIGRATION_LEDGER[
                :ledger_length
            ]
        ],
        "business_tables": business_tables,
        "governed_controls": {
            "deployment_control": {
                "table": deployment_table,
                "row": {
                    "control_key": "production",
                    "drain_enabled": True,
                    "reason": f"pull deployment {operation_id}",
                    "release_sha": SHA,
                    "activated_at": "2026-07-17T00:00:00Z",
                    "activated_by": "pull-deploy-controller",
                    "updated_at": "2026-07-17T00:00:00Z",
                },
            },
            "database_analytics_snapshots": {
                "table": analytics_table,
                "entries": [],
            },
        },
        "static_tables": static_tables,
        "migration_exception": table_record(
            helpers.CONTRACT_0012_EXCEPTION_TABLE,
            25,
            present=not contract_applied,
            rows=9,
        ),
        "sequences": sequences,
    }
    return {
        "schema_version": 3,
        **identity,
        "transaction_isolation": "repeatable read",
        "transaction_read_only": True,
        "transaction_deferrable": True,
        "snapshot_sha256": contract.pull.canonical_json_digest(identity),
        "captured_at": "2026-07-17T00:00:00Z",
    }


def _reseal_mutable_data_evidence(
    document: dict[str, object],
) -> dict[str, object]:
    fields = (
        "operation_id",
        "database",
        "database_system_identifier",
        "connection",
        "postgres_runtime",
        "digest_algorithm",
        "migration_ledger",
        "business_tables",
        "governed_controls",
        "static_tables",
        "migration_exception",
        "sequences",
    )
    document["snapshot_sha256"] = contract.pull.canonical_json_digest(
        {name: document[name] for name in fields}
    )
    return document


def _contract_mutable_data_pair() -> dict[str, object]:
    before = _mutable_data_evidence(
        operation_id=CONTRACT_OPERATION,
        ledger_length=11,
    )
    before_control = before["governed_controls"]["deployment_control"]
    before_control["table"]["content_sha256"] = "sha256:" + "d" * 64
    before_control["row"].update(
        {
            "reason": f"0012 maintenance {CONTRACT_OPERATION}",
            "activated_by": "pull-contract-0012",
        }
    )
    _reseal_mutable_data_evidence(before)
    after = _mutable_data_evidence(
        operation_id=CONTRACT_OPERATION,
        ledger_length=12,
    )
    control = after["governed_controls"]["deployment_control"]
    control["table"]["content_sha256"] = "sha256:" + "e" * 64
    control["row"].update(
        {
            "reason": f"0012 maintenance {CONTRACT_OPERATION}",
            "activated_by": "pull-contract-0012",
            "activated_at": "2026-07-17T00:10:00Z",
            "updated_at": "2026-07-17T00:10:00Z",
        }
    )
    _reseal_mutable_data_evidence(after)
    return contract.pull.build_mutable_data_pair(before, after)


def _seed_completed_alias_gate(
    runtime: Path, manifest: dict[str, object], control_root: Path
) -> None:
    selector = contract.pull._control_runtime
    operation_id = "alias-0005-fixture"
    audit_dir = runtime / selector.ALIAS_AUDIT_ROOT_RELATIVE / operation_id
    backup_dir = runtime / selector.ALIAS_BACKUP_ROOT_RELATIVE / operation_id
    for directory in (audit_dir, backup_dir):
        directory.mkdir(parents=True, mode=0o700)
        os.chmod(directory, 0o700)
    dump = backup_dir / "nexpoly-before.dump"
    _write_private(dump, "fixture database dump\n")
    dump_sha = selector.sha256_file(dump).removeprefix("sha256:")
    _write_private(backup_dir / "nexpoly-before.dump.sha256", dump_sha + "\n")
    restore_list = audit_dir / "pg-restore.list"
    _write_private(
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
    identity = {
        "operation_id": operation_id,
        "control": {
            "release_id": manifest["release_id"],
            "source_sha": manifest["source_sha"],
            "source_tree": manifest["source_tree"],
            "manifest_sha256": selector.sha256_file(
                control_root / selector.CONTROL_MANIFEST_NAME
            ).removeprefix("sha256:"),
            "script_sha256": selector.sha256_file(
                control_root / entrypoint["file"]
            ).removeprefix("sha256:"),
        },
        "legacy_source": {"sha": "1" * 40, "tree": "2" * 40},
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
    _write_private_json(audit_dir / "isolated-postgres16-restore.json", restore)
    _write_private_json(audit_dir / "database-after.json", after)
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
    _write_private_json(audit_path, audit)
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
    _write_private_json(runtime / selector.ALIAS_MARKER_RELATIVE, marker)


class FakePullController:
    def __init__(
        self,
        production_root: Path,
        runtime_root: Path,
        *,
        descriptor: dict[str, object],
        state: dict[str, object],
    ) -> None:
        self.production_root = production_root
        self.runtime_root = runtime_root
        self.test_root_mode = True
        self.state_dir = runtime_root / "state"
        self.config_dir = runtime_root / "config"
        self.prepared_root = self.state_dir / "prepared"
        self.current_state_path = self.state_dir / "current-deployment.json"
        self.marker_path = self.state_dir / "deploy-in-progress.json"
        self._descriptor = descriptor
        self._state = state
        _write_private_json(self.current_state_path, state)
        _write_private_json(
            self.prepared_root / DEPLOY_OPERATION / "descriptor.json",
            descriptor,
        )

    def ensure_roots(self, *, mutating: bool) -> None:
        del mutating

    def _load_prepared(self, operation_id: str):  # type: ignore[no-untyped-def]
        if operation_id != DEPLOY_OPERATION:
            raise AssertionError(operation_id)
        return self._descriptor, DESCRIPTOR_DIGEST

    def repository_identity(
        self, *, require_ssh_origin: bool = False
    ) -> dict[str, str]:
        return {
            "sha": SHA,
            "tree": TREE,
            "origin": (
                contract.pull.REPOSITORY_SSH_URL
                if require_ssh_origin
                else contract.pull.REPOSITORY_HTTPS_URL
            ),
        }

    def _validate_database_backup(self, descriptor, backup, **_kwargs):  # type: ignore[no-untyped-def]
        del descriptor
        return backup

    def production_config_evidence(self, *, check_free_space: bool):  # type: ignore[no-untyped-def]
        del check_free_space
        return self._state["production_config"]

    def active_control_evidence(self):  # type: ignore[no-untyped-def]
        return self._state["active_control"]

    @staticmethod
    def _active_matches_candidate(active, candidate):  # type: ignore[no-untyped-def]
        return all(
            active.get(key) == candidate.get(key)
            for key in (
                "protocol_version",
                "release_id",
                "source_sha",
                "source_tree",
                "manifest_sha256",
                "operation_id",
            )
        )


class ContractRuntimeLifecycle(contract.pull.SystemLifecycle):
    """Stateful low-level harness exercising the real resume/fence methods."""

    def __init__(self) -> None:
        self.admission_open = False
        self.worker_accepting = False
        self.worker_draining = True
        self.nginx_running = False
        self.backend_running = True
        self.worker_running = True
        self.fail_stage: str | None = None
        self.events: list[str] = []
        self.backend_process = {
            "container_id": "1" * 64,
            "image_id": "sha256:" + "2" * 64,
            "pid": 101,
            "started_at": "2026-07-17T00:00:00Z",
            "restart_count": 0,
        }
        self.worker_process = {
            "main_pid": 202,
            "invocation_id": "contract-worker-instance",
            "active_enter_monotonic": 303,
        }
        self.marker_path: Path | None = None
        self.wait_backend_processes: list[dict[str, object]] = []

    def _isolate_ingress(self, _controller, _descriptor):  # type: ignore[no-untyped-def]
        self.events.append("ingress:isolate")
        self.nginx_running = False

    def _recovery_runtime_presence(self, _controller, _descriptor):  # type: ignore[no-untyped-def]
        self.events.append("runtime:presence")
        if self.backend_running and self.worker_running:
            return "live"
        if not self.backend_running and not self.worker_running:
            return "stopped"
        return "partial"

    def ensure_candidate_drained(self, controller, descriptor):  # type: ignore[no-untyped-def]
        self.events.append("runtime:redrain")
        backend = self._backend_process_identity(controller, descriptor)
        initial = self._control_cli(controller, descriptor, "drain")
        worker_instances: dict[str, str] = {}
        for name, socket in self._worker_sockets(controller, require_md=True):
            evidence = self._worker_request(
                controller, socket, method="POST", endpoint="/drain"
            )
            worker_instances[name] = str(evidence["worker_instance_id"])
        settled = self._wait_for_zero_work(
            controller, descriptor, worker_instances, backend
        )
        return {
            "persistent_drain": True,
            "initial": initial,
            "settled": settled,
            "worker_instances": worker_instances,
            "backend_process": backend,
        }

    def start(self, _controller, _descriptor):  # type: ignore[no-untyped-def]
        self.events.append("runtime:start")
        self.backend_running = True
        self.worker_running = True
        self.backend_process = {
            **self.backend_process,
            "pid": int(self.backend_process["pid"]) + 1,
            "started_at": "2026-07-17T00:00:01Z",
        }
        self.worker_process = {
            **self.worker_process,
            "main_pid": int(self.worker_process["main_pid"]) + 1,
            "invocation_id": "contract-worker-restarted",
            "active_enter_monotonic": int(self.worker_process["active_enter_monotonic"])
            + 1,
        }
        self.admission_open = False
        self.worker_accepting = False
        self.worker_draining = True
        if self.fail_stage == "start-response-lost":
            self.fail_stage = None
            raise contract.pull.PullDeployError("injected runtime start response loss")

    def verify(self, controller, descriptor):  # type: ignore[no-untyped-def]
        self.events.append("runtime:verify")
        return {
            "health": "ok",
            "recovery_fence": self._capture_runtime_recovery_fence(
                controller, descriptor, resumed=False
            ),
        }

    def _environment(self, _controller, _descriptor):  # type: ignore[no-untyped-def]
        return {}

    def _compose(self, _controller, *arguments):  # type: ignore[no-untyped-def]
        return ["fixture-compose", *arguments]

    def _backend_process_identity(self, _controller, _descriptor):  # type: ignore[no-untyped-def]
        return dict(self.backend_process)

    def _worker_process_identity(self, _controller, _descriptor):  # type: ignore[no-untyped-def]
        return dict(self.worker_process)

    def _worker_sockets(self, _controller, *, require_md=False):  # type: ignore[no-untyped-def]
        del require_md
        return [("monomer-md", Path("/fixture/monomer-md.sock"))]

    def _validate_worker_runtime_identity(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        return {}

    def _worker_request(self, _controller, _socket, *, method, endpoint):  # type: ignore[no-untyped-def]
        self.events.append(f"worker:{method}:{endpoint}")
        if method == "POST" and endpoint == "/drain":
            self.worker_accepting = False
            self.worker_draining = True
            return self._worker_evidence(status="draining")
        if method == "POST" and endpoint == "/resume":
            self.worker_accepting = True
            self.worker_draining = False
            evidence = self._worker_evidence(status="ready")
            if self.fail_stage == "worker-response-lost":
                self.fail_stage = None
                raise contract.pull.PullDeployError(
                    "injected Worker resume response loss"
                )
            return evidence
        if method == "GET" and endpoint == "/health":
            return self._worker_evidence(status="ok")
        raise AssertionError((method, endpoint))

    def _worker_evidence(self, *, status: str) -> dict[str, object]:
        return {
            "status": status,
            "accepting_jobs": self.worker_accepting,
            "draining": self.worker_draining,
            "active_jobs": 0,
            "worker_instance_id": "contract-worker-instance",
        }

    def _wait_for_zero_work(
        self,
        _controller,
        _descriptor,
        worker_instances,
        backend_process,
    ):  # type: ignore[no-untyped-def]
        self.events.append("wait-zero")
        if worker_instances != {"monomer-md": "contract-worker-instance"}:
            raise AssertionError(worker_instances)
        if backend_process != self.backend_process:
            raise AssertionError(backend_process)
        self.wait_backend_processes.append(dict(backend_process))
        return self._active_jobs_evidence(enabled=True)

    def admission_is_open(self, _controller, _descriptor):  # type: ignore[no-untyped-def]
        self.events.append("admission-status")
        return self.admission_open

    def _control_cli(self, _controller, _descriptor, action, *arguments):  # type: ignore[no-untyped-def]
        self.events.append(f"backend:{action}")
        full = "pull-contract-0012" in arguments
        if action == "drain":
            self.admission_open = False
            return self._active_jobs_evidence(enabled=True, full=full)
        if action == "resume":
            if self.marker_path is not None:
                marker = contract.pull.load_private_json(self.marker_path)
                if not isinstance(marker.get("runtime_recovery_verification"), dict):
                    raise AssertionError("runtime fence was not durable before resume")
            if self.fail_stage == "before-backend-resume":
                self.fail_stage = None
                raise contract.pull.PullDeployError(
                    "injected failure before Backend admission"
                )
            self.admission_open = True
            evidence = self._active_jobs_evidence(enabled=False, full=full)
            if self.fail_stage == "backend-response-lost":
                self.fail_stage = None
                raise contract.pull.PullDeployError(
                    "injected Backend resume response loss"
                )
            return evidence
        if action == "status":
            return self._active_jobs_evidence(enabled=not self.admission_open)
        raise AssertionError(action)

    def _active_jobs_evidence(
        self, *, enabled: bool, full: bool = False
    ) -> dict[str, object]:
        fields = (
            contract.pull.ACTIVE_JOB_FIELDS_V1
            if full
            else contract.pull.PERSISTENT_JOB_FIELDS_V1
        )
        counts = {name: 0 for name in fields}
        evidence: dict[str, object] = {
            "drain": {
                "enabled": enabled,
                "reason": "fixture" if enabled else None,
                "release_sha": SHA if enabled else None,
                "activated_at": "2026-07-17T00:00:00Z" if enabled else None,
                "activated_by": "fixture" if enabled else None,
                "updated_at": "2026-07-17T00:00:00Z",
            },
            "active_jobs": counts,
            "active_total": 0,
        }
        if full:
            evidence["active_jobs_schema_version"] = 1
        return evidence

    def _verify_resumed_runtime(self, _controller, _descriptor):  # type: ignore[no-untyped-def]
        self.events.append("verify-resumed")
        if self.fail_stage == "nginx-started-before-backend":
            self.fail_stage = None
            raise contract.pull.PullDeployError("injected failure after nginx startup")

    def verify_runtime_identity(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return {
            "containers": {
                "backend": {"container_id": self.backend_process["container_id"]}
            },
            "worker": {"worker_instance_id": "contract-worker-instance"},
        }


class ContractRuntimeRunner:
    def __init__(self, lifecycle: ContractRuntimeLifecycle) -> None:
        self.lifecycle = lifecycle
        self.commands: list[list[str]] = []

    def run(self, command, **_kwargs):  # type: ignore[no-untyped-def]
        command = list(command)
        self.commands.append(command)
        if command[:2] == ["fixture-compose", "stop"]:
            self.lifecycle.nginx_running = False
            self.lifecycle.events.append("nginx:stop")
        elif command[:2] == ["fixture-compose", "up"]:
            self.lifecycle.nginx_running = True
            self.lifecycle.events.append("nginx:up")
            if self.lifecycle.fail_stage == "nginx-response-lost":
                self.lifecycle.fail_stage = None
                raise contract.pull.PullDeployError(
                    "injected nginx startup response loss"
                )
        return SimpleNamespace(stdout="", returncode=0)


class PullContract0012Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = mock.patch.dict(os.environ, {"NEXPOLY_ALLOW_TEST_ROOT": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix="nexpoly-pull-contract-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.production = self.root / "production"
        self.runtime = self.root / "runtime"
        config = self.runtime / "config"
        config.mkdir(parents=True, mode=0o700)
        os.chmod(config, 0o700)
        self.external_audit_helper = config / "contract-0012-external-database-audit"
        self.external_audit_helper.write_text(
            "#!/bin/sh\nprintf '%s\\n' '{}'\n",
            encoding="utf-8",
        )
        os.chmod(self.external_audit_helper, 0o700)
        deploy_env = config / "deploy.env"
        deploy_env.write_text(
            "NEXPOLY_POSTGRES_RESTORE_TMPFS_BYTES=8589934592\n"
            f"{contract.legacy.CONTRACT_0012_EXTERNAL_AUDIT_COMMAND}="
            f"{self.external_audit_helper}\n",
            encoding="utf-8",
        )
        os.chmod(deploy_env, 0o600)
        migration_source = (
            Path(__file__).resolve().parents[2] / "backend/migrations/postgres"
        )
        self.manifest = self.production / "backend/migrations/postgres/manifest.json"
        shutil.copytree(migration_source, self.manifest.parent)
        self.raw_manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.descriptor: dict[str, object] = {
            "repository": {"target_sha": SHA, "target_tree": TREE},
            "ci": {"workflow_run_id": 42},
            "images": {
                "backend": {
                    "tag": f"{contract.pull.BACKEND_TAG_ROOT}:sha-{SHA}",
                    "digest_ref": BACKEND_DIGEST,
                    "image_id": "sha256:" + "1" * 64,
                    "revision": SHA,
                    "source": contract.pull.SOURCE_URL,
                    "version": f"sha-{SHA}",
                },
                "web": {
                    "tag": f"{contract.pull.WEB_TAG_ROOT}:sha-{SHA}",
                    "digest_ref": WEB_DIGEST,
                    "image_id": "sha256:" + "2" * 64,
                    "revision": SHA,
                    "source": contract.pull.SOURCE_URL,
                    "version": f"sha-{SHA}",
                },
            },
            "release_input": {
                "asset_manifest_digest": ASSET_DIGEST,
                "datasets_on_asset_change": [],
                "asset": {
                    "pointer_path": str(self.runtime / "state/current-assets"),
                    "root": str(self.runtime / "fixture-asset"),
                    "manifest_sha256": ASSET_DIGEST,
                    "schema_version": 2,
                    "byteff2_commit": "7" * 40,
                    "inventory_sha256": "sha256:" + "8" * 64,
                    "previous": None,
                },
            },
            "migrations": {
                "sha256": contract.pull.sha256_file(self.manifest),
                "schema_version": 2,
                "records": self.raw_manifest["migrations"],
            },
            "production_config": {
                "deploy_env_sha256": "sha256:" + "3" * 64,
                "app_env_sha256": "sha256:" + "4" * 64,
                "git_deploy_key_sha256": "sha256:" + "d" * 64,
                "known_hosts_sha256": "sha256:" + "e" * 64,
                "github_api_token_sha256": "sha256:" + "f" * 64,
                "docker_config_sha256": "sha256:" + "0" * 64,
                "bootstrap_quiesce_sha256": "sha256:" + "1" * 64,
                "bootstrap_status_sha256": "sha256:" + "b" * 64,
                "bootstrap_resume_unchanged_sha256": "sha256:" + "a" * 64,
                "bootstrap_rollback_sha256": "sha256:" + "2" * 64,
                "bootstrap_active_jobs_probe_sha256": "sha256:" + "3" * 64,
                "bootstrap_legacy_runtime_status_sha256": "sha256:" + "c" * 64,
                "bootstrap_legacy_runtime_resume_unchanged_sha256": "sha256:"
                + "d" * 64,
                "bootstrap_legacy_runtime_restore_sha256": "sha256:" + "4" * 64,
                "deployment_mutable_data_audit_sha256": "sha256:" + "5" * 64,
                "mutable_data_audit_pg_service_sha256": "sha256:" + "6" * 64,
                "mutable_data_audit_pgpass_sha256": "sha256:" + "7" * 64,
            },
            "controller": {
                "helpers": {
                    name: "sha256:" + "5" * 64
                    for name in contract.pull.STABLE_HELPER_FILES
                },
            },
            "monomer_md": {
                "slot_record_sha256": "sha256:" + "6" * 64,
                "worker_env": {
                    "path": str(self.runtime / "config/worker.env"),
                    "sha256": "sha256:" + "9" * 64,
                    "byteff2_python": "/opt/byteff2/bin/python",
                    "byteff2_openmm_dir": "/opt/byteff2/openmm",
                    "gmx_sha256": "sha256:" + "a" * 64,
                },
                "systemd_unit": {
                    "target_path": "/home/devuser/.config/systemd/user/nexpoly-monomer-md-worker.service",
                    "sha256": "sha256:" + "b" * 64,
                    "previous_unit_state": {
                        "LoadState": "loaded",
                        "FragmentPath": "/home/devuser/.config/systemd/user/nexpoly-monomer-md-worker.service",
                        "DropInPaths": "",
                        "NeedDaemonReload": "no",
                        "UnitFileState": "enabled",
                    },
                },
            },
        }
        source_manifest = contract.pull._control_runtime.parse_source_manifest(
            (
                Path(__file__).resolve().parents[2] / "scripts/control-release.json"
            ).read_bytes()
        )
        payloads = {
            record["name"]: (
                Path(__file__).resolve().parents[2] / record["source"]
            ).read_bytes()
            for record in source_manifest["files"]
        }
        control_identity = {
            "schema_version": 1,
            "protocol_version": 1,
            "source_sha": SHA,
            "source_tree": TREE,
            "compatibility": source_manifest["compatibility"],
            "entrypoints": source_manifest["entrypoints"],
            "files": {
                name: {
                    "sha256": contract.pull.sha256_bytes(payload),
                    "size": len(payload),
                    "mode": 0o700,
                }
                for name, payload in payloads.items()
            },
        }
        release_id = contract.pull._control_runtime.release_identity(control_identity)
        control_manifest = {**control_identity, "release_id": release_id}
        control_root = self.runtime / "control-releases" / release_id
        control_root.mkdir(parents=True, mode=0o700)
        os.chmod(control_root.parent, 0o700)
        os.chmod(control_root, 0o700)
        for name, payload in payloads.items():
            path = control_root / name
            path.write_bytes(payload)
            os.chmod(path, 0o700)
        _write_private_json(
            control_root / contract.pull._control_runtime.CONTROL_MANIFEST_NAME,
            control_manifest,
        )
        candidate_control = {
            "schema_version": 1,
            "protocol_version": 1,
            "component": "deployment-controls",
            "release_id": release_id,
            "source_sha": SHA,
            "source_tree": TREE,
            "manifest_sha256": contract.pull.sha256_file(
                control_root / contract.pull._control_runtime.CONTROL_MANIFEST_NAME
            ),
            "operation_id": DEPLOY_OPERATION,
            "prepared_at": "2026-07-16T00:00:00Z",
        }
        active_control = {
            "schema_version": 1,
            "protocol_version": 1,
            "component": "deployment-controls",
            "generation": 1,
            "release_id": release_id,
            "source_sha": SHA,
            "source_tree": TREE,
            "manifest_sha256": candidate_control["manifest_sha256"],
            "operation_id": DEPLOY_OPERATION,
            "previous_release_id": None,
            "activated_at": "2026-07-16T00:00:00Z",
        }
        self.descriptor["controller"]["executor_control"] = candidate_control
        self.descriptor["controller"]["executor_control_sha256"] = (
            contract.pull.canonical_json_digest(candidate_control)
        )
        launcher_sha = control_manifest["files"]["monomer_md_worker_launcher.py"][
            "sha256"
        ]
        self.descriptor["monomer_md"]["systemd_unit"].update(
            {
                "control_release_id": release_id,
                "launcher_sha256": launcher_sha,
            }
        )
        active_slot = {
            "schema_version": contract.pull.ACTIVE_SLOT_SCHEMA_VERSION,
            "component": "monomer-md",
            "slot": "a",
            "source_sha": SHA,
            "source_tree": TREE,
            "worker_lock_sha256": "sha256:" + "0" * 64,
            "slot_record_sha256": "sha256:" + "6" * 64,
            "operation_id": DEPLOY_OPERATION,
            "activated_at": "2026-07-16T00:00:00Z",
        }
        current_history = [
            dict(record)
            for record in self.raw_manifest["migrations"]
            if record["version"] != contract.CONTRACT_VERSION
        ]
        accepted_ledgers = (
            contract.pull._bridge_core.expected_migration_registry(
                target_manifest_sha256=self.descriptor["migrations"][
                    "sha256"
                ],
                target_records=self.raw_manifest["migrations"],
                authority_manifest_sha256="sha256:" + "f" * 64,
                authority_records=[
                    *self.raw_manifest["migrations"],
                    contract.pull._bridge_core.FINAL_MIGRATION_RECORD,
                ],
            )
        )
        migration_compatibility = (
            contract.pull.build_migration_compatibility_state(
                {
                    "policy_id": "sha256:" + "e" * 64,
                    "accepted_migration_ledgers": accepted_ledgers,
                },
                code_manifest_sha256=self.descriptor["migrations"]["sha256"],
                migrations=current_history,
            )
        )
        mutable_before = _mutable_data_evidence()
        mutable_after = json.loads(json.dumps(mutable_before))
        mutable_identity = contract.pull.canonical_json_digest(
            contract.pull.mutable_data_identity(mutable_before)
        )
        mutable_data_audit = contract.pull.build_mutable_data_pair(
            mutable_before,
            mutable_after,
        )
        self.state: dict[str, object] = {
            "schema_version": 2,
            "status": "success",
            "operation_id": DEPLOY_OPERATION,
            "source_sha": SHA,
            "source_tree": TREE,
            "previous_release": "0" * 40,
            "descriptor_sha256": DESCRIPTOR_DIGEST,
            "images": self.descriptor["images"],
            "asset_manifest_digest": ASSET_DIGEST,
            "asset_identity": self.descriptor["release_input"]["asset"],
            "byteff2_commit": "7" * 40,
            "migrations": current_history,
            "approved_contracts": [],
            "migration_epoch_barrier": None,
            "schema_compatibility_floor": None,
            "last_contract_operation": None,
            "migration_compatibility": migration_compatibility,
            "active_monomer_md_slot": active_slot,
            "monomer_md_worker_env": self.descriptor["monomer_md"]["worker_env"],
            "monomer_md_systemd_unit": {
                "target_path": self.descriptor["monomer_md"]["systemd_unit"][
                    "target_path"
                ],
                "sha256": self.descriptor["monomer_md"]["systemd_unit"]["sha256"],
                "control_release_id": release_id,
                "launcher_sha256": launcher_sha,
            },
            "control_helpers": self.descriptor["controller"]["helpers"],
            "active_control": active_control,
            "production_config": self.descriptor["production_config"],
            "database_backup": {
                "path": str(self.runtime / "backups/deploy/database.dump"),
                "sha256": "sha256:" + "c" * 64,
                "restore_verification": {},
                "mutable_data_before_sha256": mutable_identity,
            },
            "mutable_data_audit": mutable_data_audit,
            "deployed_at": "2026-07-16T00:00:00Z",
        }
        _seed_completed_alias_gate(self.runtime, control_manifest, control_root)

    def _fake_controller(self) -> FakePullController:
        return FakePullController(
            self.production,
            self.runtime,
            descriptor=self.descriptor,
            state=self.state,
        )

    def _stateful_runtime(
        self,
        *,
        marker_overrides: dict[str, object] | None = None,
    ) -> tuple[
        contract.PullRuntimeController,
        ContractRuntimeLifecycle,
        FakePullController,
        Path,
    ]:
        fake = self._fake_controller()
        with mock.patch.object(
            contract.pull,
            "PullDeployController",
            return_value=fake,
        ):
            binding = contract.load_binding(
                self.production,
                self.runtime,
                apply=False,
            )
        runtime = contract.PullRuntimeController(
            binding,
            CONTRACT_OPERATION,
            apply=False,
        )
        lifecycle = ContractRuntimeLifecycle()
        runner = ContractRuntimeRunner(lifecycle)
        fake.runner = runner  # type: ignore[attr-defined]
        fake.control_environment = lambda: {}  # type: ignore[attr-defined]
        runtime.lifecycle = lifecycle
        marker_path = runtime.contract_marker_path
        authority = {
            "source_sha": SHA,
            "source_tree": TREE,
            "pull_descriptor_sha256": DESCRIPTOR_DIGEST,
        }
        marker: dict[str, object] = {
            "operation_id": CONTRACT_OPERATION,
            "source_sha": SHA,
            "pull_descriptor_sha256": DESCRIPTOR_DIGEST,
            "pull_maintenance_authority": authority,
            "pull_maintenance_authority_sha256": (
                contract.pull.canonical_json_digest(authority)
            ),
            "status": "running",
            "phase": "prepared",
            "drain_attempted": True,
            "database_change_started": False,
        }
        if marker_overrides:
            marker.update(marker_overrides)
        contract.legacy.atomic_json(marker_path, marker)

        def loader() -> dict[str, object]:
            return contract.pull.load_private_json(marker_path)

        def writer(document: dict[str, object]) -> None:
            contract.legacy.atomic_json(marker_path, document)

        runtime.bind_contract_marker_persistence(
            loader=loader,
            writer=writer,
        )
        lifecycle.marker_path = marker_path
        return runtime, lifecycle, fake, marker_path

    def _owned_contract_restore(self) -> list[dict[str, object]]:
        destination = "/var/lib/postgresql/data"
        return [
            {
                "Id": "1" * 64,
                "Name": f"/nexpoly-contract-restore-{CONTRACT_OPERATION}",
                "Config": {
                    "Image": contract.pull.POSTGRES16_IMAGE,
                    "Labels": {
                        "com.nexpoly.contract-restore-operation": CONTRACT_OPERATION,
                    },
                    "Env": ["POSTGRES_HOST_AUTH_METHOD=trust"],
                },
                "HostConfig": {
                    "NetworkMode": "none",
                    "Tmpfs": {
                        destination: "nodev,size=8589934592,rw,nosuid",
                    },
                    "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                    "AutoRemove": False,
                    "Privileged": False,
                    "PublishAllPorts": False,
                },
                "NetworkSettings": {
                    "Ports": {},
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
                "Mounts": [{"Type": "tmpfs", "Destination": destination, "RW": True}],
            }
        ]

    def test_load_binding_uses_current_state_descriptor_and_live_policy(self) -> None:
        fake = self._fake_controller()
        with mock.patch.object(
            contract.pull, "PullDeployController", return_value=fake
        ):
            binding = contract.load_binding(self.production, self.runtime, apply=True)

        self.assertEqual(binding.descriptor_sha256, DESCRIPTOR_DIGEST)
        self.assertEqual(binding.repository["sha"], SHA)
        record = next(
            record
            for record in binding.migration_records
            if record["version"] == contract.CONTRACT_VERSION
        )
        self.assertEqual(record["checksum"], contract.CONTRACT_CHECKSUM)
        self.assertEqual(
            binding.adapter_sha256,
            contract.pull.sha256_file(Path(contract.__file__).resolve()),
        )
        self.assertEqual(
            binding.governance_core_sha256,
            contract.pull.sha256_file(contract._governance_core_path()),
        )
        self.assertEqual(
            binding.governance_helper_sha256,
            contract.pull.sha256_file(
                contract._governance_sibling_path("monomer_worker_env.py")
            ),
        )
        self.assertEqual(
            binding.external_database_audit_helper,
            {
                "path": str(self.external_audit_helper),
                "sha256": contract.pull.sha256_file(self.external_audit_helper),
            },
        )

    def test_external_audit_helper_path_is_fixed(self) -> None:
        replacement = self.runtime / "config/other-audit-helper"
        replacement.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(replacement, 0o700)
        deploy_env = self.runtime / "config/deploy.env"
        deploy_env.write_text(
            "NEXPOLY_POSTGRES_RESTORE_TMPFS_BYTES=8589934592\n"
            f"{contract.legacy.CONTRACT_0012_EXTERNAL_AUDIT_COMMAND}="
            f"{replacement}\n",
            encoding="utf-8",
        )
        os.chmod(deploy_env, 0o600)
        fake = self._fake_controller()

        with (
            mock.patch.object(
                contract.pull,
                "PullDeployController",
                return_value=fake,
            ),
            self.assertRaisesRegex(
                contract.PullContractError,
                "path is not fixed",
            ),
        ):
            contract.load_binding(self.production, self.runtime, apply=False)

    def test_external_audit_helper_rejects_symlink(self) -> None:
        target = self.external_audit_helper.with_name("external-audit-target")
        self.external_audit_helper.rename(target)
        self.external_audit_helper.symlink_to(target)
        fake = self._fake_controller()

        with (
            mock.patch.object(
                contract.pull,
                "PullDeployController",
                return_value=fake,
            ),
            self.assertRaisesRegex(
                contract.PullContractError,
                "owner-only mode 0700",
            ),
        ):
            contract.load_binding(self.production, self.runtime, apply=False)

    def test_external_audit_helper_requires_exact_mode_0700(self) -> None:
        os.chmod(self.external_audit_helper, 0o755)
        fake = self._fake_controller()

        with (
            mock.patch.object(
                contract.pull,
                "PullDeployController",
                return_value=fake,
            ),
            self.assertRaisesRegex(
                contract.PullContractError,
                "owner-only mode 0700",
            ),
        ):
            contract.load_binding(self.production, self.runtime, apply=False)

    def test_external_audit_bootstrap_hook_rehashes_bound_helper(self) -> None:
        fake = self._fake_controller()
        with mock.patch.object(
            contract.pull,
            "PullDeployController",
            return_value=fake,
        ):
            binding = contract.load_binding(
                self.production,
                self.runtime,
                apply=False,
            )
        runtime = contract.PullRuntimeController(
            binding,
            CONTRACT_OPERATION,
            apply=False,
        )
        key = contract.legacy.CONTRACT_0012_EXTERNAL_AUDIT_COMMAND
        environment = {key: str(self.external_audit_helper)}
        self.assertEqual(
            runtime.bootstrap_hook_command(environment, key),
            [str(self.external_audit_helper)],
        )

        self.external_audit_helper.write_text(
            "#!/bin/sh\nprintf '%s\\n' changed\n",
            encoding="utf-8",
        )
        os.chmod(self.external_audit_helper, 0o700)
        with self.assertRaisesRegex(
            contract.PullContractError,
            "changed during the operation",
        ):
            runtime.bootstrap_hook_command(environment, key)

    def test_external_audit_bootstrap_hook_rejects_environment_override(
        self,
    ) -> None:
        fake = self._fake_controller()
        with mock.patch.object(
            contract.pull,
            "PullDeployController",
            return_value=fake,
        ):
            binding = contract.load_binding(
                self.production,
                self.runtime,
                apply=False,
            )
        runtime = contract.PullRuntimeController(
            binding,
            CONTRACT_OPERATION,
            apply=False,
        )
        replacement = self.runtime / "config/other-audit-helper"
        replacement.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(replacement, 0o700)
        key = contract.legacy.CONTRACT_0012_EXTERNAL_AUDIT_COMMAND

        with self.assertRaisesRegex(
            contract.PullContractError,
            "differs from the sealed binding",
        ):
            runtime.bootstrap_hook_command({key: str(replacement)}, key)

    def test_external_audit_helper_is_rehashed_before_inventory(self) -> None:
        fake = self._fake_controller()
        with mock.patch.object(
            contract.pull,
            "PullDeployController",
            return_value=fake,
        ):
            maintenance = contract.PullContractMaintenance(
                self.production,
                self.runtime,
                CONTRACT_OPERATION,
                apply=False,
            )
        self.external_audit_helper.write_text(
            "#!/bin/sh\nprintf '%s\\n' changed-before\n",
            encoding="utf-8",
        )
        os.chmod(self.external_audit_helper, 0o700)
        with (
            mock.patch.object(
                contract.legacy.PolytaoContractMaintenance,
                "_capture_external_database_inventory",
                return_value={},
            ) as capture,
            self.assertRaisesRegex(
                contract.PullContractError,
                "changed during the operation",
            ),
        ):
            maintenance._capture_external_database_inventory({})
        capture.assert_not_called()

    def test_external_audit_helper_is_rehashed_after_inventory(self) -> None:
        fake = self._fake_controller()
        with mock.patch.object(
            contract.pull,
            "PullDeployController",
            return_value=fake,
        ):
            maintenance = contract.PullContractMaintenance(
                self.production,
                self.runtime,
                CONTRACT_OPERATION,
                apply=False,
            )

        def replace_after_start(_environment):  # type: ignore[no-untyped-def]
            self.external_audit_helper.write_text(
                "#!/bin/sh\nprintf '%s\\n' changed-after\n",
                encoding="utf-8",
            )
            os.chmod(self.external_audit_helper, 0o700)
            return {}

        with (
            mock.patch.object(
                contract.legacy.PolytaoContractMaintenance,
                "_capture_external_database_inventory",
                side_effect=replace_after_start,
            ) as capture,
            self.assertRaisesRegex(
                contract.PullContractError,
                "changed during the operation",
            ),
        ):
            maintenance._capture_external_database_inventory({})
        capture.assert_called_once_with({})

    def test_external_audit_helper_sha_is_in_authority_marker_and_audit(
        self,
    ) -> None:
        fake = self._fake_controller()
        with mock.patch.object(
            contract.pull,
            "PullDeployController",
            return_value=fake,
        ):
            maintenance = contract.PullContractMaintenance(
                self.production,
                self.runtime,
                CONTRACT_OPERATION,
                apply=False,
            )
            marker = {
                "operation_id": CONTRACT_OPERATION,
                "source_sha": SHA,
                "pull_descriptor_sha256": DESCRIPTOR_DIGEST,
            }
            maintenance._write_marker(marker)
            persisted = contract.pull.load_private_json(maintenance.marker_path)
            helper_evidence = maintenance.binding.external_database_audit_helper
            self.assertEqual(
                persisted["pull_maintenance_authority"][
                    "external_database_audit_helper"
                ],
                helper_evidence,
            )
            self.assertEqual(
                persisted["pull_maintenance_authority_sha256"],
                contract.pull.canonical_json_digest(
                    persisted["pull_maintenance_authority"]
                ),
            )

            maintenance.audit_dir.mkdir(parents=True, mode=0o700)
            os.chmod(maintenance.audit_dir, 0o700)
            audit_manifest = maintenance._audit_manifest()

        helper_audit = maintenance.audit_dir / "external-database-audit-helper.json"
        self.assertEqual(
            contract.pull.load_private_json(helper_audit),
            helper_evidence,
        )
        audit_files = {record["name"]: record for record in audit_manifest["files"]}
        self.assertIn("pull-maintenance-authority.json", audit_files)
        self.assertEqual(
            audit_files["external-database-audit-helper.json"]["sha256"],
            contract.pull.sha256_file(helper_audit),
        )

    def test_runtime_gate_records_actual_live_identity_before_marker(self) -> None:
        fake = self._fake_controller()
        runtime_evidence = {
            "repository": {"sha": SHA, "tree": TREE},
            "asset": {"manifest_sha256": ASSET_DIGEST},
            "unit": {"FragmentPath": "/runtime/unit"},
            "containers": {
                "backend": {"image_id": "sha256:backend"},
                "web": {"image_id": "sha256:web"},
            },
            "worker": {"status": "ok", "runtime_ready": True},
            "postgres_loopback": True,
            "verified_at": "2026-07-16T00:00:00+00:00",
        }
        fake._active_slot = mock.Mock(return_value=self.state["active_monomer_md_slot"])
        with mock.patch.object(
            contract.pull,
            "PullDeployController",
            return_value=fake,
        ):
            binding = contract.load_binding(self.production, self.runtime, apply=False)
        runtime = contract.PullRuntimeController(
            binding,
            CONTRACT_OPERATION,
            apply=False,
        )
        runtime.lifecycle = mock.Mock()
        runtime.lifecycle.verify_runtime_identity.return_value = runtime_evidence
        with mock.patch.object(contract, "load_binding", return_value=binding):
            runtime.validate_current_runtime({})

        runtime.lifecycle.verify_runtime_identity.assert_called_once_with(
            fake,
            runtime.runtime_descriptor,
        )
        self.assertEqual(runtime._runtime_identity_evidence, runtime_evidence)
        runtime_evidence["worker"]["status"] = "changed"
        self.assertEqual(runtime._runtime_identity_evidence["worker"]["status"], "ok")

        maintenance = object.__new__(contract.PullContractMaintenance)
        maintenance.controller = runtime
        maintenance.binding = binding
        maintenance.runtime_root = self.runtime
        maintenance.operation_id = CONTRACT_OPERATION
        maintenance.apply = False
        maintenance.root = self.production
        maintenance.document = runtime.document
        maintenance.audit_dir = self.runtime / "audit/contracts/0012/test"
        maintenance.contract_record = next(
            record
            for record in binding.migration_records
            if record["version"] == contract.CONTRACT_VERSION
        )
        plan = maintenance.plan()
        self.assertEqual(plan["runtime_identity"], runtime._runtime_identity_evidence)

    def test_governance_core_has_no_live_checkout_fallback(self) -> None:
        self.assertEqual(
            contract._governance_core_path(),
            Path(contract.__file__).resolve().parent / "release_controller.py",
        )

    def test_load_binding_requires_exact_ordered_canonical_history(self) -> None:
        # A pre-bridge governed deployment legitimately has no frozen B/F
        # compatibility registry.  Exercise the contract adapter's own
        # canonical-prefix guard independently of the stricter bridge guard.
        self.state["migration_compatibility"] = None
        histories = (
            list(self.state["migrations"])[1:],
            list(reversed(self.state["migrations"])),
            [
                *list(self.state["migrations"]),
                {
                    "version": "0099_unreviewed",
                    "kind": "expand",
                    "epoch": 2,
                    "checksum": "f" * 64,
                    "requires_contracts": [],
                },
            ],
        )
        for history in histories:
            with self.subTest(history=history):
                self.state["migrations"] = history
                fake = self._fake_controller()
                with (
                    mock.patch.object(
                        contract.pull,
                        "PullDeployController",
                        return_value=fake,
                    ),
                    self.assertRaisesRegex(
                        contract.PullContractError,
                        "exact ordered canonical migration history",
                    ),
                ):
                    contract.load_binding(self.production, self.runtime, apply=False)

    def test_load_binding_rejects_current_state_descriptor_mismatch(self) -> None:
        self.state["descriptor_sha256"] = "sha256:" + "0" * 64
        fake = self._fake_controller()
        with (
            mock.patch.object(contract.pull, "PullDeployController", return_value=fake),
            self.assertRaisesRegex(
                contract.PullContractError, "sealed pull descriptor"
            ),
        ):
            contract.load_binding(self.production, self.runtime, apply=False)

    def test_pull_projection_has_no_bundle_or_release_path_authority(self) -> None:
        fake = self._fake_controller()
        with mock.patch.object(
            contract.pull, "PullDeployController", return_value=fake
        ):
            binding = contract.load_binding(self.production, self.runtime, apply=False)
        document = contract._pull_document(binding)

        self.assertNotIn("release_bundle", document)
        self.assertTrue(document["worker_runtime_present"])
        serialized = json.dumps(document, sort_keys=True)
        self.assertNotIn("ops/releases", serialized)
        self.assertNotIn("release-manifest", serialized)
        self.assertEqual(document["source_sha"], SHA)

    def test_legacy_transition_round_trips_record_based_pull_state(self) -> None:
        mutable_data_audit = json.loads(
            json.dumps(self.state["mutable_data_audit"])
        )
        projected = contract._legacy_state_projection(self.state)
        self.assertTrue(all(isinstance(item, str) for item in projected["migrations"]))
        projected["migrations"].append(contract.CONTRACT_VERSION)
        projected["applied_migrations"] = [contract.CONTRACT_VERSION]
        fake = self._fake_controller()
        with mock.patch.object(
            contract.pull, "PullDeployController", return_value=fake
        ):
            binding = contract.load_binding(self.production, self.runtime, apply=False)
        persisted = contract._pull_state_projection(binding, projected)

        self.assertTrue(all(isinstance(item, dict) for item in persisted["migrations"]))
        self.assertEqual(
            persisted["migrations"][-1]["checksum"],
            contract.CONTRACT_CHECKSUM,
        )
        self.assertEqual(
            persisted["migration_compatibility"]["ledger_state"]["name"],
            "post-0012",
        )
        self.assertEqual(
            persisted["mutable_data_audit"],
            mutable_data_audit,
        )
        self.assertEqual(
            contract.pull.validate_current_deployment_state(persisted),
            persisted,
        )
        self.assertNotIn("applied_migrations", persisted)

    def test_first_takeover_descriptor_uses_governed_runtime_drain(self) -> None:
        self.descriptor["previous_deployment"] = None
        fake = self._fake_controller()
        with mock.patch.object(
            contract.pull, "PullDeployController", return_value=fake
        ):
            binding = contract.load_binding(self.production, self.runtime, apply=False)
        runtime = contract.PullRuntimeController(
            binding,
            CONTRACT_OPERATION,
            apply=False,
        )
        runtime.lifecycle = mock.Mock()
        runtime.lifecycle.drain.return_value = {
            "worker_instances": {"monomer-md": "worker-1"}
        }
        runtime.lifecycle._capture_runtime_recovery_fence.return_value = {
            "fixture": "fence"
        }

        with mock.patch.object(
            runtime,
            "_persist_runtime_recovery_verification",
        ) as persist:
            runtime.drain({}, True)

        descriptor = runtime.lifecycle.drain.call_args.args[1]
        self.assertIsNotNone(descriptor["previous_deployment"])
        self.assertTrue(descriptor["previous_deployment"]["governed_current_runtime"])
        persist.assert_called_once()

    def test_contract_smoke_never_uses_full_resume_or_starts_nginx(self) -> None:
        fake = self._fake_controller()
        fake.runner = mock.Mock()
        fake.runner.run.side_effect = [
            SimpleNamespace(stdout=""),
            SimpleNamespace(stdout=b"monomer MD 300-step smoke completed: fixture\n"),
        ]
        fake._git_show = mock.Mock(return_value=b"# smoke fixture\n")
        with mock.patch.object(
            contract.pull, "PullDeployController", return_value=fake
        ):
            binding = contract.load_binding(self.production, self.runtime, apply=False)
        runtime = contract.PullRuntimeController(
            binding,
            CONTRACT_OPERATION,
            apply=False,
        )
        runtime.lifecycle = mock.Mock()
        runtime.lifecycle._compose.return_value = ["docker", "compose", "ps"]
        internal_drain = {"persistent_drain": True}
        with (
            mock.patch.object(runtime, "_internal_resume_without_ingress") as resume,
            mock.patch.object(
                runtime,
                "_internal_drain_without_ingress",
                return_value=internal_drain,
            ) as redrain,
            mock.patch.object(runtime, "run_contract_gpu_api_smoke") as smoke,
        ):
            runtime.run_ingress_isolated_contract_smoke({})

        resume.assert_called_once_with()
        smoke.assert_called_once_with({}, release=None)
        redrain.assert_called_once_with()
        runtime.lifecycle.resume.assert_not_called()
        self.assertEqual(runtime._drain_evidence, internal_drain)
        smoke_compose = next(
            call.args
            for call in runtime.lifecycle._compose.call_args_list
            if "--operation-id" in call.args
        )
        self.assertIn(CONTRACT_OPERATION, smoke_compose)
        self.assertIn("--source-sha", smoke_compose)
        self.assertIn(SHA, smoke_compose)

    def test_contract_resume_opens_backend_before_nginx_and_recovers_lost_response(
        self,
    ) -> None:
        runtime, lifecycle, _fake, marker_path = self._stateful_runtime()

        runtime.drain({}, False)

        self.assertTrue(lifecycle.admission_open)
        self.assertTrue(lifecycle.nginx_running)
        self.assertLess(
            lifecycle.events.index("backend:resume"),
            lifecycle.events.index("nginx:up"),
        )
        marker = contract.pull.load_private_json(marker_path)
        self.assertIsInstance(marker.get("runtime_recovery_verification"), dict)

        # A Backend CAS can commit while its response is lost.  The first
        # attempt must leave ingress isolated; the retry re-identifies and
        # re-drains the same process/Worker before reopening.
        lifecycle.fail_stage = "backend-response-lost"
        lifecycle.events.clear()
        with self.assertRaisesRegex(
            contract.pull.PullDeployError,
            "Backend resume response loss",
        ):
            runtime.drain({}, False)
        self.assertFalse(lifecycle.nginx_running)
        self.assertTrue(lifecycle.admission_open)
        self.assertEqual(lifecycle.events[-1], "ingress:isolate")

        lifecycle.events.clear()
        runtime.drain({}, False)
        self.assertTrue(lifecycle.nginx_running)
        self.assertIn("runtime:redrain", lifecycle.events)
        self.assertLess(
            lifecycle.events.index("runtime:redrain"),
            lifecycle.events.index("backend:resume"),
        )

    def test_contract_initial_drain_persists_full_runtime_fence(self) -> None:
        runtime, lifecycle, _fake, marker_path = self._stateful_runtime()

        runtime.drain({}, True)

        marker = contract.pull.load_private_json(marker_path)
        verification = marker.get("runtime_recovery_verification")
        self.assertIsInstance(verification, dict)
        self.assertEqual(
            verification["mode"],
            "contract-0012-initial-drain",
        )
        self.assertEqual(
            verification["recovery_fence"]["backend_process"],
            lifecycle.backend_process,
        )
        self.assertFalse(lifecycle.admission_open)

    def test_contract_stopped_start_unknown_commit_accepts_only_authorized_replacement(
        self,
    ) -> None:
        runtime, lifecycle, _fake, marker_path = self._stateful_runtime()
        lifecycle.backend_running = False
        lifecycle.worker_running = False
        lifecycle.fail_stage = "start-response-lost"

        with self.assertRaisesRegex(
            contract.pull.PullDeployError,
            "runtime start response loss",
        ):
            runtime.drain({}, False)

        marker = contract.pull.load_private_json(marker_path)
        self.assertEqual(
            marker["runtime_recovery_start_intent"]["reason"],
            "final-resume",
        )
        self.assertTrue(lifecycle.backend_running)
        self.assertTrue(lifecycle.worker_running)
        first_pid = lifecycle.backend_process["pid"]

        lifecycle.events.clear()
        runtime.drain({}, False)
        self.assertEqual(lifecycle.backend_process["pid"], first_pid)
        self.assertIn("runtime:redrain", lifecycle.events)
        self.assertNotIn("runtime:start", lifecycle.events)
        self.assertTrue(lifecycle.nginx_running)
        self.assertNotIn(
            "runtime_recovery_start_intent",
            contract.pull.load_private_json(marker_path),
        )

    def test_contract_database_restore_start_intent_accepts_sealed_replacement(
        self,
    ) -> None:
        runtime, lifecycle, _fake, marker_path = self._stateful_runtime()
        runtime.drain({}, True)
        marker = contract.pull.load_private_json(marker_path)
        marker["phase"] = "database-change-started"
        marker["database_change_started"] = True
        contract.legacy.atomic_json(marker_path, marker)
        maintenance = object.__new__(contract.PullContractMaintenance)
        maintenance.controller = runtime
        maintenance.binding = runtime.binding
        maintenance.root = self.production
        maintenance.operation_id = CONTRACT_OPERATION
        maintenance._write_marker = lambda document: contract.legacy.atomic_json(  # type: ignore[method-assign]
            marker_path, document
        )

        contract.PullContractMaintenance._reestablish_recovery_drain(
            maintenance, marker
        )
        committed = contract.pull.load_private_json(marker_path)
        self.assertEqual(
            committed["runtime_recovery_start_intent"]["reason"],
            "database-restore",
        )

        lifecycle.backend_process = {
            **lifecycle.backend_process,
            "pid": int(lifecycle.backend_process["pid"]) + 10,
            "started_at": "2026-07-17T00:00:10Z",
        }
        lifecycle.worker_process = {
            **lifecycle.worker_process,
            "main_pid": int(lifecycle.worker_process["main_pid"]) + 10,
            "invocation_id": "post-restore-worker",
        }
        lifecycle.worker_accepting = True
        lifecycle.worker_draining = False
        lifecycle.admission_open = True
        lifecycle.events.clear()
        contract.PullContractMaintenance._reestablish_recovery_drain(
            maintenance, committed
        )
        self.assertEqual(lifecycle.events[0], "ingress:isolate")
        self.assertIn("runtime:redrain", lifecycle.events)
        self.assertFalse(lifecycle.admission_open)

    def test_contract_unknown_commit_rejects_replaced_worker_before_redrain(
        self,
    ) -> None:
        runtime, lifecycle, _fake, _marker_path = self._stateful_runtime()
        lifecycle.fail_stage = "backend-response-lost"
        with self.assertRaises(contract.pull.PullDeployError):
            runtime.drain({}, False)
        lifecycle.worker_process = {
            **lifecycle.worker_process,
            "invocation_id": "replacement-worker-instance",
        }
        lifecycle.events.clear()

        with self.assertRaisesRegex(
            contract.pull.PullDeployError,
            "differs from committed verification",
        ):
            runtime.drain({}, False)

        self.assertEqual(lifecycle.events[0], "ingress:isolate")
        self.assertNotIn("runtime:redrain", lifecycle.events)
        self.assertFalse(lifecycle.nginx_running)

    def test_contract_authority_drift_blocks_admission_before_backend_resume(
        self,
    ) -> None:
        runtime, lifecycle, _fake, _marker_path = self._stateful_runtime()

        def reject_marker(_document):  # type: ignore[no-untyped-def]
            raise contract.PullContractError(
                "installed 0012 maintenance authority changed"
            )

        runtime._contract_marker_writer = reject_marker
        with self.assertRaisesRegex(
            contract.PullContractError,
            "maintenance authority changed",
        ):
            runtime.drain({}, False)

        self.assertNotIn("backend:resume", lifecycle.events)
        self.assertFalse(lifecycle.nginx_running)

    def test_contract_internal_canary_fences_then_recovery_redrains_same_instance(
        self,
    ) -> None:
        runtime, lifecycle, _fake, marker_path = self._stateful_runtime()

        runtime._internal_resume_without_ingress()

        self.assertTrue(lifecycle.admission_open)
        self.assertFalse(lifecycle.nginx_running)
        self.assertTrue(lifecycle.wait_backend_processes)
        self.assertEqual(
            lifecycle.wait_backend_processes[-1],
            lifecycle.backend_process,
        )
        self.assertLess(
            lifecycle.events.index("worker:POST:/resume"),
            lifecycle.events.index("backend:resume"),
        )
        marker = contract.pull.load_private_json(marker_path)
        self.assertIsInstance(marker.get("runtime_recovery_verification"), dict)

        # Model a hard kill before the canary's finally block.  Recovery must
        # isolate first, validate the stored instance, and close both control
        # planes before any inherited database gate can run.
        lifecycle.events.clear()
        maintenance = object.__new__(contract.PullContractMaintenance)
        maintenance.controller = runtime
        maintenance.binding = runtime.binding
        maintenance.root = self.production
        maintenance.operation_id = CONTRACT_OPERATION
        maintenance._write_marker = lambda document: contract.legacy.atomic_json(  # type: ignore[method-assign]
            marker_path, document
        )
        contract.PullContractMaintenance._reestablish_recovery_drain(
            maintenance,
            marker,
        )
        self.assertEqual(lifecycle.events[0], "ingress:isolate")
        self.assertIn("runtime:redrain", lifecycle.events)
        self.assertFalse(lifecycle.admission_open)

    def test_database_recovery_entries_share_only_current_in_process_drain_gate(
        self,
    ) -> None:
        runtime, lifecycle, _fake, marker_path = self._stateful_runtime()
        runtime._internal_resume_without_ingress()
        marker = contract.pull.load_private_json(marker_path)
        marker["phase"] = "verifying"
        marker["database_change_started"] = True
        marker["mutable_data_before"] = _contract_mutable_data_pair()[
            "before"
        ]
        marker["mutable_data_before_sha256"] = (
            contract.pull.canonical_json_digest(
                contract.pull.mutable_data_identity(
                    marker["mutable_data_before"]
                )
            )
        )
        contract.legacy.atomic_json(marker_path, marker)

        maintenance = object.__new__(contract.PullContractMaintenance)
        maintenance.controller = runtime
        maintenance.binding = runtime.binding
        maintenance.root = self.production
        maintenance.operation_id = CONTRACT_OPERATION
        maintenance.marker_path = marker_path
        maintenance._database_recovery_drain_gate = None
        maintenance._load_runtime_recovery_marker = (  # type: ignore[method-assign]
            lambda: contract.pull.load_private_json(marker_path)
        )
        maintenance._write_marker = lambda document: contract.legacy.atomic_json(  # type: ignore[method-assign]
            marker_path, document
        )
        lifecycle.events.clear()

        with (
            mock.patch.object(
                contract.legacy.PolytaoContractMaintenance,
                "_reconcile_owned_verification_database",
                side_effect=lambda *_args, **_kwargs: (
                    lifecycle.events.append("database:reconcile")
                    or {"status": "not-created"}
                ),
            ),
            mock.patch.object(
                contract.legacy.PolytaoContractMaintenance,
                "_restore_previous_database",
                side_effect=lambda *_args, **_kwargs: lifecycle.events.append(
                    "database:restore"
                ),
            ),
            mock.patch.object(
                runtime,
                "capture_mutable_data",
                return_value=marker["mutable_data_before"],
            ),
        ):
            maintenance._reconcile_owned_verification_database({})
            maintenance._restore_previous_database({}, {})

        self.assertEqual(lifecycle.events[0], "ingress:isolate")
        self.assertEqual(lifecycle.events.count("runtime:redrain"), 1)
        self.assertLess(
            lifecycle.events.index("runtime:redrain"),
            lifecycle.events.index("database:reconcile"),
        )
        self.assertLess(
            lifecycle.events.index("database:reconcile"),
            lifecycle.events.index("database:restore"),
        )
        self.assertFalse(lifecycle.admission_open)

    def test_database_recovery_gate_rejects_malformed_start_intent(self) -> None:
        with self.assertRaisesRegex(
            contract.PullContractError,
            "runtime start intent is invalid",
        ):
            contract.PullContractMaintenance._database_recovery_gate_identity(
                {
                    "operation_id": CONTRACT_OPERATION,
                    "source_sha": SHA,
                    "runtime_recovery_start_intent": "corrupt",
                }
            )

    def test_contract_recovery_phase_handles_pre_drain_stopped_and_partial_states(
        self,
    ) -> None:
        def maintenance_for(runtime, marker_path):  # type: ignore[no-untyped-def]
            maintenance = object.__new__(contract.PullContractMaintenance)
            maintenance.controller = runtime
            maintenance.binding = runtime.binding
            maintenance.root = self.production
            maintenance.operation_id = CONTRACT_OPERATION

            def write_marker(document):  # type: ignore[no-untyped-def]
                contract.legacy.atomic_json(marker_path, document)

            maintenance._write_marker = write_marker
            return maintenance

        # A crash after persisting drain intent but before issuing the CLI is
        # the sole live/unfenced recovery case.  Isolation is the first side
        # effect and the fresh sealed runtime is then re-drained.
        runtime, lifecycle, _fake, marker_path = self._stateful_runtime()
        marker = contract.pull.load_private_json(marker_path)
        maintenance = maintenance_for(runtime, marker_path)
        contract.PullContractMaintenance._reestablish_recovery_drain(
            maintenance, marker
        )
        self.assertEqual(lifecycle.events[0], "ingress:isolate")
        self.assertIn("runtime:redrain", lifecycle.events)
        persisted = contract.pull.load_private_json(marker_path)
        self.assertEqual(persisted["recovery_admission_phase"], "redrained")

        # If every source reader is already absent, DB recovery may continue
        # without querying Backend admission or starting a replacement.
        runtime, lifecycle, _fake, marker_path = self._stateful_runtime()
        lifecycle.backend_running = False
        lifecycle.worker_running = False
        marker = contract.pull.load_private_json(marker_path)
        maintenance = maintenance_for(runtime, marker_path)
        contract.PullContractMaintenance._reestablish_recovery_drain(
            maintenance, marker
        )
        self.assertEqual(
            contract.pull.load_private_json(marker_path)["recovery_admission_phase"],
            "runtime-stopped",
        )
        self.assertNotIn("admission-status", lifecycle.events)
        self.assertNotIn("runtime:start", lifecycle.events)

        # Final admission recovery may idempotently start a fully absent
        # runtime; start() returns it drained and nginx still remains last.
        lifecycle.events.clear()
        runtime.drain({}, False)
        self.assertIn("runtime:start", lifecycle.events)
        self.assertLess(
            lifecycle.events.index("runtime:start"),
            lifecycle.events.index("backend:resume"),
        )
        self.assertLess(
            lifecycle.events.index("backend:resume"),
            lifecycle.events.index("nginx:up"),
        )

        # One live and one stopped source reader is not a safe restart state.
        runtime, lifecycle, _fake, marker_path = self._stateful_runtime()
        lifecycle.worker_running = False
        marker = contract.pull.load_private_json(marker_path)
        maintenance = maintenance_for(runtime, marker_path)
        with self.assertRaisesRegex(
            contract.pull.PullDeployError,
            "partially stopped",
        ):
            contract.PullContractMaintenance._reestablish_recovery_drain(
                maintenance, marker
            )
        self.assertEqual(lifecycle.events[0], "ingress:isolate")
        self.assertNotIn("runtime:start", lifecycle.events)

    def test_contract_recovery_requires_fence_after_canary_phase(self) -> None:
        runtime, lifecycle, _fake, marker_path = self._stateful_runtime(
            marker_overrides={
                "phase": "verifying",
                "ingress_isolated_canary": {"status": "started"},
            }
        )
        maintenance = object.__new__(contract.PullContractMaintenance)
        maintenance.controller = runtime
        maintenance.binding = runtime.binding
        maintenance.root = self.production
        maintenance.operation_id = CONTRACT_OPERATION
        maintenance._write_marker = lambda document: contract.legacy.atomic_json(  # type: ignore[method-assign]
            marker_path, document
        )
        marker = contract.pull.load_private_json(marker_path)

        with self.assertRaisesRegex(
            contract.pull.PullDeployError,
            "lacks committed verification",
        ):
            contract.PullContractMaintenance._reestablish_recovery_drain(
                maintenance, marker
            )
        self.assertEqual(lifecycle.events[0], "ingress:isolate")
        self.assertNotIn("runtime:redrain", lifecycle.events)

    def test_post_state_commit_binding_accepts_previous_state_fence(self) -> None:
        fake = self._fake_controller()
        with mock.patch.object(
            contract.pull, "PullDeployController", return_value=fake
        ):
            before = contract.load_binding(self.production, self.runtime, apply=False)
        previous_projection = contract._legacy_state_projection(before.current_state)
        self.state["migrations"].append(self.raw_manifest["migrations"][-1])
        approved_at = "2026-07-16T00:00:00+00:00"
        self.state["approved_contracts"] = [
            {
                "version": contract.CONTRACT_VERSION,
                "checksum": contract.CONTRACT_CHECKSUM,
                "operation_id": CONTRACT_OPERATION,
                "approved_at": approved_at,
            }
        ]
        self.state["schema_compatibility_floor"] = {
            "version": contract.CONTRACT_VERSION,
            "checksum": contract.CONTRACT_CHECKSUM,
        }
        self.state["migration_epoch_barrier"] = {
            "epoch": 1,
            "contract": {
                "version": contract.CONTRACT_VERSION,
                "checksum": contract.CONTRACT_CHECKSUM,
            },
            "operation_id": CONTRACT_OPERATION,
            "approved_at": approved_at,
        }
        self.state["last_contract_operation"] = CONTRACT_OPERATION
        self.state["migration_compatibility"] = (
            contract.pull.build_migration_compatibility_state(
                self.state["migration_compatibility"],
                code_manifest_sha256=self.descriptor["migrations"]["sha256"],
                migrations=self.state["migrations"],
            )
        )
        after_fake = self._fake_controller()
        with mock.patch.object(
            contract.pull,
            "PullDeployController",
            return_value=after_fake,
        ):
            after = contract.load_binding(self.production, self.runtime, apply=False)
        maintenance = object.__new__(contract.PullContractMaintenance)
        maintenance.root = self.production
        maintenance.runtime_root = self.runtime
        maintenance.apply = False
        maintenance.binding = before
        with mock.patch.object(contract, "load_binding", return_value=after):
            self.assertEqual(
                maintenance._bind_current_release(previous_projection),
                self.production,
            )

    def test_archive_requires_pinned_isolated_postgres16_restore(self) -> None:
        backup = self.runtime / "backups/database.dump"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(b"dump")
        audit = self.runtime / "audit"
        audit.mkdir(exist_ok=True)
        pull_controller = object()
        lifecycle = mock.Mock()
        archive_evidence = {
            "schema_version": 2,
            "row_count": 9,
            "status_counts": {"completed": 7, "failed": 2},
            "rows_sha256": "1" * 64,
            "schema_sha256": "2" * 64,
            "structure_counts": {
                "columns": 1,
                "indexes": 1,
                "constraints": 1,
                "triggers": 0,
            },
        }
        lifecycle.verify_contract_postgres16_restore.return_value = {
            "schema_version": 2,
            "restored": True,
            "postgres_major": 16,
            "postgres_version_num": "160008",
            "image": contract.pull.POSTGRES16_IMAGE,
            "dump_sha256": contract.legacy.sha256_file(backup),
            "ledger": [],
            "archive": archive_evidence,
            "operation_id": CONTRACT_OPERATION,
            "verified_at": "2026-07-16T00:00:00+00:00",
        }
        maintenance = object.__new__(contract.PullContractMaintenance)
        maintenance.controller = SimpleNamespace(
            backup_path=backup,
            lifecycle=lifecycle,
        )
        maintenance.binding = SimpleNamespace(
            controller=pull_controller,
            descriptor={"operation_id": DEPLOY_OPERATION},
        )
        maintenance.operation_id = CONTRACT_OPERATION
        maintenance.audit_dir = audit
        before = _contract_mutable_data_pair()["before"]
        maintenance._contract_mutable_data_before = None
        maintenance._contract_mutable_data_pair = None
        maintenance._load_runtime_recovery_marker = (  # type: ignore[method-assign]
            lambda: {
                "mutable_data_before": before,
                "mutable_data_before_sha256": (
                    contract.pull.canonical_json_digest(
                        contract.pull.mutable_data_identity(before)
                    )
                ),
            }
        )
        with mock.patch.object(
            contract.legacy.PolytaoContractMaintenance,
            "_archive_legacy_table",
            return_value=archive_evidence,
        ):
            evidence = maintenance._archive_legacy_table({}, {}, {})

        self.assertEqual(evidence, archive_evidence)
        lifecycle.verify_contract_postgres16_restore.assert_called_once()
        restore_descriptor = (
            lifecycle.verify_contract_postgres16_restore.call_args.args[1]
        )
        self.assertEqual(restore_descriptor["operation_id"], CONTRACT_OPERATION)
        restored = json.loads(
            (audit / "isolated-postgres16-restore.json").read_text(encoding="utf-8")
        )
        self.assertEqual(restored["postgres_major"], 16)

    def test_archive_fails_before_contract_when_isolated_restore_fails(self) -> None:
        backup = self.runtime / "backups/database.dump"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(b"dump")
        audit = self.runtime / "audit"
        audit.mkdir(exist_ok=True)
        lifecycle = mock.Mock()
        lifecycle.verify_contract_postgres16_restore.side_effect = (
            contract.pull.PullDeployError("restore failed")
        )
        maintenance = object.__new__(contract.PullContractMaintenance)
        maintenance.controller = SimpleNamespace(
            backup_path=backup,
            lifecycle=lifecycle,
        )
        maintenance.binding = SimpleNamespace(
            controller=object(),
            descriptor={"operation_id": DEPLOY_OPERATION},
        )
        maintenance.operation_id = CONTRACT_OPERATION
        maintenance.audit_dir = audit
        before = _contract_mutable_data_pair()["before"]
        maintenance._contract_mutable_data_before = None
        maintenance._contract_mutable_data_pair = None
        maintenance._load_runtime_recovery_marker = (  # type: ignore[method-assign]
            lambda: {
                "mutable_data_before": before,
                "mutable_data_before_sha256": (
                    contract.pull.canonical_json_digest(
                        contract.pull.mutable_data_identity(before)
                    )
                ),
            }
        )
        with (
            mock.patch.object(
                contract.legacy.PolytaoContractMaintenance,
                "_archive_legacy_table",
                return_value={
                    "schema_version": 2,
                    "row_count": 9,
                    "status_counts": {"completed": 7, "failed": 2},
                    "rows_sha256": "1" * 64,
                    "schema_sha256": "2" * 64,
                    "structure_counts": {
                        "columns": 1,
                        "indexes": 1,
                        "constraints": 1,
                        "triggers": 0,
                    },
                },
            ),
            self.assertRaisesRegex(contract.pull.PullDeployError, "restore failed"),
        ):
            maintenance._archive_legacy_table({}, {}, {})

        self.assertFalse((audit / "isolated-postgres16-restore.json").exists())

    def test_owned_interrupted_restore_is_removed_and_foreign_is_rejected(self) -> None:
        docker_config = self.runtime / "config/docker"
        docker_config.mkdir(parents=True, mode=0o700)
        os.chmod(docker_config, 0o700)
        (docker_config / "config.json").write_text(
            '{"auths":{"ghcr.io":{"auth":"dXNlcjp0b2tlbg=="}}}\n',
            encoding="utf-8",
        )
        os.chmod(docker_config / "config.json", 0o600)
        owned = self._owned_contract_restore()
        runner = mock.Mock()
        runner.run.side_effect = [
            SimpleNamespace(returncode=0, stdout=json.dumps(owned)),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=1, stdout=""),
        ]
        controller = SimpleNamespace(
            runtime_root=self.runtime,
            config_dir=self.runtime / "config",
            runner=runner,
        )
        lifecycle = contract.PullContractLifecycle()

        self.assertTrue(
            lifecycle.cleanup_contract_restore_container(
                controller,
                CONTRACT_OPERATION,
            )
        )
        self.assertEqual(runner.run.call_args_list[1].args[0][1:3], ["rm", "--force"])

        foreign = json.loads(json.dumps(owned))
        foreign[0]["Config"]["Image"] = "postgres:unreviewed"
        runner.reset_mock()
        runner.run.side_effect = [
            SimpleNamespace(returncode=0, stdout=json.dumps(foreign)),
        ]
        with self.assertRaisesRegex(
            contract.PullContractError,
            "foreign or mismatched",
        ):
            lifecycle.cleanup_contract_restore_container(
                controller,
                CONTRACT_OPERATION,
            )
        self.assertEqual(runner.run.call_count, 1)

    def test_pg16_restore_crashes_cleanup_owned_container_for_retry(self) -> None:
        docker_config = self.runtime / "config/docker"
        docker_config.mkdir(parents=True, mode=0o700)
        os.chmod(docker_config, 0o700)
        (docker_config / "config.json").write_text(
            '{"auths":{"ghcr.io":{"auth":"dXNlcjp0b2tlbg=="}}}\n',
            encoding="utf-8",
        )
        os.chmod(docker_config / "config.json", 0o600)
        dump = self.runtime / "database.dump"
        dump.write_bytes(b"dump")
        descriptor = json.loads(json.dumps(self.descriptor))
        descriptor["operation_id"] = CONTRACT_OPERATION
        expected_archive = {
            "schema_version": 2,
            "row_count": 9,
            "status_counts": {"completed": 7, "failed": 2},
            "rows_sha256": "1" * 64,
            "schema_sha256": "2" * 64,
            "structure_counts": {
                "columns": 1,
                "indexes": 1,
                "constraints": 1,
                "triggers": 0,
            },
        }

        for crash_phase in ("create", "restore", "evidence"):
            with self.subTest(crash_phase=crash_phase):
                commands: list[list[str]] = []
                container_exists = False

                def run(command, **_kwargs):  # type: ignore[no-untyped-def]
                    nonlocal container_exists
                    commands.append(command)
                    if command[:3] == ["docker", "container", "inspect"]:
                        if not container_exists:
                            return SimpleNamespace(returncode=1, stdout="")
                        owned = self._owned_contract_restore()
                        return SimpleNamespace(
                            returncode=0,
                            stdout=json.dumps(owned),
                        )
                    if command[:2] == ["docker", "run"]:
                        container_exists = True
                        if crash_phase == "create":
                            raise contract.pull.PullDeployError(
                                "ambiguous docker run result"
                            )
                        return SimpleNamespace(returncode=0, stdout="container")
                    if "pg_isready" in command or "createdb" in command:
                        return SimpleNamespace(returncode=0, stdout="")
                    if "pg_restore" in command:
                        if crash_phase == "restore":
                            raise contract.pull.PullDeployError("restore interrupted")
                        return SimpleNamespace(returncode=0, stdout="")
                    if "SHOW server_version_num" in command:
                        return SimpleNamespace(returncode=0, stdout="160008\n")
                    if "psql" in command:
                        if crash_phase in {"create", "evidence"}:
                            return SimpleNamespace(returncode=0, stdout="not-json")
                        raise AssertionError(command)
                    if command[:3] == ["docker", "rm", "--force"]:
                        container_exists = False
                        return SimpleNamespace(returncode=0, stdout="")
                    raise AssertionError(command)

                controller = SimpleNamespace(
                    runtime_root=self.runtime,
                    config_dir=self.runtime / "config",
                    runner=SimpleNamespace(run=run),
                )
                lifecycle = contract.PullContractLifecycle()
                with self.assertRaises(
                    (contract.PullContractError, contract.pull.PullDeployError)
                ):
                    lifecycle.verify_contract_postgres16_restore(
                        controller,
                        descriptor,
                        dump,
                        contract.legacy.sha256_file(dump),
                        expected_archive,
                    )
                self.assertFalse(container_exists)
                self.assertTrue(
                    any(
                        command[:3] == ["docker", "rm", "--force"]
                        for command in commands
                    )
                )

    def test_private_recovery_marker_rejects_wrong_mode_and_broken_symlink(
        self,
    ) -> None:
        maintenance = object.__new__(contract.PullContractMaintenance)
        marker = self.runtime / "state/contract-0012-in-progress.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}", encoding="utf-8")
        os.chmod(marker, 0o644)
        with self.assertRaisesRegex(contract.PullContractError, "unsafe or invalid"):
            maintenance._load_operation_document(marker, "recovery marker")
        marker.unlink()
        marker.symlink_to(self.runtime / "missing-marker")
        with self.assertRaisesRegex(contract.PullContractError, "unsafe or invalid"):
            maintenance._load_operation_document(marker, "recovery marker")

    def test_deploy_environment_cannot_override_control_plane(self) -> None:
        base = {
            "NEXPOLY_RUNTIME_ROOT": str(self.runtime),
            "NEXPOLY_APP_ENV_FILE": str(self.runtime / "config/app.env"),
            "NEXPOLY_ASSET_ROOT": str(self.runtime / "state/current-assets"),
        }
        for key in (
            "PATH",
            "HOME",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "COMPOSE_FILE",
            "LD_PRELOAD",
            "PYTHONPATH",
            "XDG_RUNTIME_DIR",
        ):
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(
                    contract.pull.PullDeployError,
                    "control-plane redirect",
                ),
            ):
                contract.pull.validate_deploy_control_values(
                    {**base, key: "attacker"},
                    runtime_root=self.runtime,
                )

    def test_inherited_apply_state_machine_preserves_pull_state_and_order(self) -> None:
        fake = self._fake_controller()
        events: list[str] = []
        archive_evidence = {
            "schema_version": 2,
            "row_count": 9,
            "status_counts": {"completed": 7, "failed": 2},
            "rows_sha256": "1" * 64,
            "schema_sha256": "2" * 64,
            "structure_counts": {
                "columns": 1,
                "indexes": 1,
                "constraints": 1,
                "triggers": 0,
            },
        }
        with mock.patch.object(
            contract.pull,
            "PullDeployController",
            return_value=fake,
        ):
            maintenance = contract.PullContractMaintenance(
                self.production,
                self.runtime,
                CONTRACT_OPERATION,
                apply=True,
            )
        maintenance.apply = True
        maintenance.controller.apply = True
        mutable_pair = _contract_mutable_data_pair()
        maintenance._contract_mutable_data_before = mutable_pair["before"]
        maintenance._contract_mutable_data_pair = mutable_pair

        def archive(*_args):  # type: ignore[no-untyped-def]
            events.append("backup-pg16")
            maintenance.audit_dir.mkdir(parents=True, mode=0o700)
            os.chmod(maintenance.audit_dir, 0o700)
            contract.legacy.atomic_json(
                maintenance.audit_dir / "legacy-table-evidence.json",
                archive_evidence,
            )
            maintenance.controller.backup_root.mkdir(parents=True, mode=0o700)
            backup = maintenance.controller.backup_root / "database.dump"
            backup.write_bytes(b"verified dump")
            os.chmod(backup, 0o600)
            maintenance.controller.backup_path = backup
            return archive_evidence

        real_write_state = maintenance._write_current_state
        real_write_journal = maintenance._write_success_journal

        def write_state(state):  # type: ignore[no-untyped-def]
            events.append("state")
            real_write_state(state)

        def write_journal(journal):  # type: ignore[no-untyped-def]
            events.append("journal")
            real_write_journal(journal)

        def migrate(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            events.append("ddl")
            return [contract.CONTRACT_VERSION]

        def resume(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            events.append("resume")

        with (
            mock.patch.object(
                contract.pull,
                "PullDeployController",
                return_value=fake,
            ),
            mock.patch.object(maintenance.controller, "ensure_root"),
            mock.patch.object(
                maintenance.controller,
                "deployment_lock",
                return_value=contextlib.nullcontext(),
            ),
            mock.patch.object(maintenance.controller, "environment", return_value={}),
            mock.patch.object(maintenance.controller, "validate_current_runtime"),
            mock.patch.object(
                maintenance,
                "_pre_destructive_database_gate",
                return_value={"external_registered_database_inventory": {}},
            ),
            mock.patch.object(maintenance.controller, "drain"),
            mock.patch.object(
                maintenance.controller,
                "drain_worker",
                return_value={"worker_instance_id": "worker-1"},
            ),
            mock.patch.object(maintenance.controller, "wait_for_jobs"),
            mock.patch.object(
                maintenance,
                "_archive_legacy_table",
                side_effect=archive,
            ),
            mock.patch.object(maintenance, "_verify_full_restore"),
            mock.patch.object(
                maintenance.controller,
                "run_migrations",
                side_effect=migrate,
            ),
            mock.patch.object(
                maintenance,
                "_capture_json",
                return_value={"schema_version": 1, "verified": True},
            ),
            mock.patch.object(maintenance.controller, "backend_healthcheck"),
            mock.patch.object(
                maintenance.controller, "compose", return_value=["compose"]
            ),
            mock.patch.object(maintenance.controller, "run"),
            mock.patch.object(
                maintenance.controller,
                "run_ingress_isolated_contract_smoke",
                side_effect=lambda *_args, **_kwargs: setattr(
                    maintenance.controller,
                    "contract_canary_evidence",
                    {
                        "schema_version": 1,
                        "status": "passed",
                        "ingress_isolated": True,
                    },
                ),
            ),
            mock.patch.object(
                maintenance,
                "_write_current_state",
                side_effect=write_state,
            ),
            mock.patch.object(
                maintenance,
                "_write_success_journal",
                side_effect=write_journal,
            ),
            mock.patch.object(
                maintenance,
                "_resume_admission",
                side_effect=resume,
            ),
        ):
            result = maintenance.run()

        self.assertEqual(
            events,
            ["backup-pg16", "ddl", "state", "journal", "resume"],
        )
        self.assertTrue(all(isinstance(item, dict) for item in result["migrations"]))
        self.assertEqual(result["migrations"][-1]["version"], contract.CONTRACT_VERSION)
        self.assertFalse(maintenance.marker_path.exists())
        self.assertTrue(maintenance.journal_path.is_file())
        self.assertEqual(maintenance.journal_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(maintenance.audit_dir.stat().st_mode & 0o777, 0o700)
        self.assertIsNotNone(maintenance.controller.backup_path)
        self.assertEqual(
            maintenance.controller.backup_path.stat().st_mode & 0o777,
            0o600,
        )
        for evidence_path in maintenance.audit_dir.iterdir():
            if evidence_path.is_file():
                self.assertEqual(evidence_path.stat().st_mode & 0o777, 0o600)

    def test_canary_redrain_failure_blocks_same_invocation_database_recovery(
        self,
    ) -> None:
        fake = self._fake_controller()
        archive_evidence = {
            "schema_version": 2,
            "row_count": 9,
            "status_counts": {"completed": 7, "failed": 2},
            "rows_sha256": "1" * 64,
            "schema_sha256": "2" * 64,
            "structure_counts": {
                "columns": 1,
                "indexes": 1,
                "constraints": 1,
                "triggers": 0,
            },
        }
        with mock.patch.object(
            contract.pull,
            "PullDeployController",
            return_value=fake,
        ):
            maintenance = contract.PullContractMaintenance(
                self.production,
                self.runtime,
                CONTRACT_OPERATION,
                apply=True,
            )
        maintenance.apply = True
        maintenance.controller.apply = True

        def archive(*_args):  # type: ignore[no-untyped-def]
            maintenance.audit_dir.mkdir(parents=True, mode=0o700)
            os.chmod(maintenance.audit_dir, 0o700)
            contract.legacy.atomic_json(
                maintenance.audit_dir / "legacy-table-evidence.json",
                archive_evidence,
            )
            maintenance.controller.backup_root.mkdir(parents=True, mode=0o700)
            backup = maintenance.controller.backup_root / "database.dump"
            backup.write_bytes(b"verified dump")
            os.chmod(backup, 0o600)
            maintenance.controller.backup_path = backup
            return archive_evidence

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    contract.pull,
                    "PullDeployController",
                    return_value=fake,
                )
            )
            stack.enter_context(
                mock.patch.object(maintenance.controller, "ensure_root")
            )
            stack.enter_context(
                mock.patch.object(
                    maintenance.controller,
                    "deployment_lock",
                    return_value=contextlib.nullcontext(),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    maintenance.controller,
                    "environment",
                    return_value={},
                )
            )
            stack.enter_context(
                mock.patch.object(maintenance.controller, "validate_current_runtime")
            )
            stack.enter_context(
                mock.patch.object(
                    maintenance,
                    "_pre_destructive_database_gate",
                    return_value={"external_registered_database_inventory": {}},
                )
            )
            stack.enter_context(mock.patch.object(maintenance.controller, "drain"))
            stack.enter_context(
                mock.patch.object(
                    maintenance.controller,
                    "drain_worker",
                    return_value={"worker_instance_id": "worker-1"},
                )
            )
            stack.enter_context(
                mock.patch.object(maintenance.controller, "wait_for_jobs")
            )
            stack.enter_context(
                mock.patch.object(
                    maintenance,
                    "_archive_legacy_table",
                    side_effect=archive,
                )
            )
            stack.enter_context(mock.patch.object(maintenance, "_verify_full_restore"))
            stack.enter_context(
                mock.patch.object(
                    maintenance.controller,
                    "run_migrations",
                    return_value=[contract.CONTRACT_VERSION],
                )
            )
            stack.enter_context(
                mock.patch.object(
                    maintenance,
                    "_capture_json",
                    return_value={"schema_version": 1, "verified": True},
                )
            )
            stack.enter_context(
                mock.patch.object(maintenance.controller, "backend_healthcheck")
            )
            stack.enter_context(
                mock.patch.object(
                    maintenance.controller,
                    "compose",
                    return_value=["compose"],
                )
            )
            stack.enter_context(mock.patch.object(maintenance.controller, "run"))
            stack.enter_context(
                mock.patch.object(
                    maintenance.controller,
                    "run_ingress_isolated_contract_smoke",
                    side_effect=contract.pull.PullDeployError(
                        "injected canary redrain response loss"
                    ),
                )
            )
            recovery_drain = stack.enter_context(
                mock.patch.object(
                    maintenance,
                    "_reestablish_recovery_drain",
                    side_effect=contract.pull.PullDeployError(
                        "injected recovery redrain failure"
                    ),
                )
            )
            legacy_reconcile = stack.enter_context(
                mock.patch.object(
                    contract.legacy.PolytaoContractMaintenance,
                    "_reconcile_owned_verification_database",
                )
            )
            legacy_restore = stack.enter_context(
                mock.patch.object(
                    contract.legacy.PolytaoContractMaintenance,
                    "_restore_previous_database",
                )
            )
            with self.assertRaisesRegex(
                contract.legacy.ReleaseError,
                "rollback is incomplete",
            ):
                maintenance.run()

        recovery_drain.assert_called_once()
        legacy_reconcile.assert_not_called()
        legacy_restore.assert_not_called()
        marker = contract.pull.load_private_json(maintenance.marker_path)
        self.assertEqual(marker["status"], "failed")
        self.assertEqual(marker["phase"], "verifying")
        self.assertTrue(marker["database_change_started"])
        self.assertIn("canary redrain response loss", marker["error"])

    def test_post_commit_crash_rebuilds_journal_from_record_state(self) -> None:
        fake = self._fake_controller()
        with mock.patch.object(
            contract.pull,
            "PullDeployController",
            return_value=fake,
        ):
            maintenance = contract.PullContractMaintenance(
                self.production,
                self.runtime,
                CONTRACT_OPERATION,
                apply=True,
            )
        approved_at = "2026-07-16T00:00:00+00:00"
        approval = {
            "version": contract.CONTRACT_VERSION,
            "checksum": contract.CONTRACT_CHECKSUM,
            "operation_id": CONTRACT_OPERATION,
            "approved_at": approved_at,
        }
        previous = contract._legacy_state_projection(self.state)
        committed = json.loads(json.dumps(previous))
        committed["migrations"].append(contract.CONTRACT_VERSION)
        committed["approved_contracts"] = [approval]
        committed["schema_compatibility_floor"] = {
            "version": contract.CONTRACT_VERSION,
            "checksum": contract.CONTRACT_CHECKSUM,
        }
        committed["migration_epoch_barrier"] = {
            "epoch": 1,
            "contract": {
                "version": contract.CONTRACT_VERSION,
                "checksum": contract.CONTRACT_CHECKSUM,
            },
            "operation_id": CONTRACT_OPERATION,
            "approved_at": approved_at,
        }
        committed["last_contract_operation"] = CONTRACT_OPERATION
        mutable_pair = _contract_mutable_data_pair()
        maintenance._contract_mutable_data_pair = mutable_pair
        maintenance._write_current_state(committed)

        archive_evidence = {
            "schema_version": 2,
            "row_count": 9,
            "status_counts": {"completed": 7, "failed": 2},
            "rows_sha256": "1" * 64,
            "schema_sha256": "2" * 64,
            "structure_counts": {
                "columns": 1,
                "indexes": 1,
                "constraints": 1,
                "triggers": 0,
            },
        }
        maintenance.audit_dir.mkdir(parents=True, mode=0o700)
        os.chmod(maintenance.audit_dir, 0o700)
        contract.legacy.atomic_json(
            maintenance.audit_dir / "legacy-table-evidence.json",
            archive_evidence,
        )
        with mock.patch.object(maintenance, "_validate_installed_authority"):
            audit_manifest = maintenance._audit_manifest()
        audit_path = maintenance.audit_dir / "AUDIT-MANIFEST.json"
        maintenance.controller.backup_root.mkdir(parents=True, mode=0o700)
        backup = maintenance.controller.backup_root / "database.dump"
        backup.write_bytes(b"verified dump")
        os.chmod(backup, 0o600)
        marker = {
            "operation_id": CONTRACT_OPERATION,
            "source_sha": SHA,
            "previous_state": previous,
            "database_backup": str(backup),
            "database_backup_sha256": contract.legacy.sha256_file(backup),
            "audit_manifest_sha256": contract.legacy.sha256_file(audit_path),
            "database_change_started": True,
            "worker_drain_attempted": True,
            "archive_evidence": archive_evidence,
            "mutable_data_before": mutable_pair["before"],
            "mutable_data_before_sha256": (
                contract.pull.canonical_json_digest(
                    contract.pull.mutable_data_identity(
                        mutable_pair["before"]
                    )
                )
            ),
            "contract_mutable_data_audit": mutable_pair,
            "database_inventory": {
                "external_registered_database_inventory": {},
            },
        }
        canary = {
            "schema_version": 1,
            "status": "passed",
            "ingress_isolated": True,
        }
        authority = maintenance._maintenance_authority()
        marker.update(
            {
                "pull_maintenance_authority": authority,
                "pull_maintenance_authority_sha256": (
                    contract.pull.canonical_json_digest(authority)
                ),
                "ingress_isolated_canary": canary,
                "ingress_isolated_canary_sha256": (
                    contract.pull.canonical_json_digest(canary)
                ),
            }
        )
        contract.legacy.atomic_json(maintenance.marker_path, marker)

        with (
            mock.patch.object(
                contract.pull,
                "PullDeployController",
                return_value=fake,
            ),
            mock.patch.object(
                maintenance.controller.lifecycle,
                "cleanup_contract_restore_container",
            ) as cleanup,
            mock.patch.object(maintenance, "_reestablish_recovery_drain"),
            mock.patch.object(maintenance.controller, "environment", return_value={}),
            mock.patch.object(
                maintenance,
                "_pre_destructive_database_gate",
                return_value={},
            ),
            mock.patch.object(
                maintenance,
                "_reconcile_owned_verification_database",
                return_value={"verified_absent": True},
            ),
            mock.patch.object(
                maintenance,
                "_capture_json",
                return_value={"schema_version": 1, "verified": True},
            ),
            mock.patch.object(
                maintenance.controller, "compose", return_value=["compose"]
            ),
            mock.patch.object(maintenance.controller, "run"),
            mock.patch.object(
                maintenance.controller,
                "run_ingress_isolated_contract_smoke",
                return_value=canary,
            ),
            mock.patch.object(maintenance, "_resume_admission") as resume,
        ):
            recovered = maintenance._recover(marker)

        cleanup.assert_called_once_with(fake, CONTRACT_OPERATION)
        self.assertIn(contract.CONTRACT_VERSION, recovered["migrations"])
        self.assertFalse(maintenance.marker_path.exists())
        journal = contract.pull.load_private_json(maintenance.journal_path)
        self.assertEqual(journal["approval"], approval)
        self.assertNotEqual(journal["audit_manifest"], audit_manifest)
        self.assertEqual(
            journal["audit_manifest"],
            contract.pull.load_private_json(audit_path),
        )
        self.assertEqual(
            journal["contract_mutable_data_audit"],
            mutable_pair,
        )
        resume.assert_called_once_with({}, worker_was_drained=True)

    def test_recovery_rejects_wrong_operation_before_cleanup(self) -> None:
        maintenance = object.__new__(contract.PullContractMaintenance)
        maintenance.operation_id = CONTRACT_OPERATION
        maintenance.controller = SimpleNamespace(
            lifecycle=mock.Mock(),
        )
        maintenance.binding = SimpleNamespace(controller=object())
        with self.assertRaisesRegex(
            contract.PullContractError,
            "different 0012 operation",
        ):
            maintenance._recover({"operation_id": "contract-0012-foreign"})
        maintenance.controller.lifecycle.cleanup_contract_restore_container.assert_not_called()

    def test_external_backup_marker_rejects_any_other_directory(self) -> None:
        fake = self._fake_controller()
        with mock.patch.object(
            contract.pull, "PullDeployController", return_value=fake
        ):
            binding = contract.load_binding(self.production, self.runtime, apply=False)
        runtime = contract.PullRuntimeController(
            binding,
            CONTRACT_OPERATION,
            apply=False,
        )
        runtime.backup_root.mkdir(parents=True, mode=0o700)
        backup = runtime.backup_root / "database.dump"
        backup.write_bytes(b"verified backup")
        os.chmod(backup, 0o600)
        digest = contract.legacy.sha256_file(backup)

        self.assertEqual(
            runtime.marker_backup(
                {"database_backup": str(backup), "database_backup_sha256": digest}
            ),
            backup,
        )
        outside = self.production / "backups/database.dump"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(backup.read_bytes())
        with self.assertRaisesRegex(contract.PullContractError, "missing or changed"):
            runtime.marker_backup(
                {"database_backup": str(outside), "database_backup_sha256": digest}
            )

    def test_cli_exposes_only_plan_and_explicit_apply(self) -> None:
        parser = contract.build_parser()
        choices = next(
            action.choices
            for action in parser._actions
            if isinstance(action, contract.argparse._SubParsersAction)
        )
        self.assertEqual(set(choices), {"plan", "apply"})
        for command in choices.values():
            option_names = {
                option
                for action in command._actions
                for option in action.option_strings
            }
            self.assertNotIn("--manifest", option_names)
            self.assertNotIn("--release-bundle", option_names)

    def test_apply_confirmation_fails_before_constructing_maintenance(self) -> None:
        error = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"NEXPOLY_ALLOW_TEST_ROOT": ""}),
            mock.patch.object(contract, "PullContractMaintenance") as maintenance,
            contextlib.redirect_stderr(error),
        ):
            result = contract.main(
                ["apply", "--operation-id", CONTRACT_OPERATION, "--apply"]
            )

        self.assertEqual(result, 2)
        maintenance.assert_not_called()
        self.assertIn("requires the exact production/runtime roots", error.getvalue())

    def test_ambient_test_mode_cannot_authorize_production_contract(self) -> None:
        error = io.StringIO()
        with (
            mock.patch.object(contract, "PullContractMaintenance") as maintenance,
            contextlib.redirect_stderr(error),
        ):
            result = contract.main(
                [
                    "apply",
                    "--operation-id",
                    CONTRACT_OPERATION,
                    "--apply",
                    "--confirm-production-root",
                    str(contract.PRODUCTION_ROOT),
                    "--confirm-runtime-root",
                    str(contract.RUNTIME_ROOT),
                ]
            )

        self.assertEqual(result, 2)
        maintenance.assert_not_called()
        self.assertIn("forbidden for production paths", error.getvalue())

    def test_apply_without_apply_flag_is_dry_run(self) -> None:
        instance = mock.Mock()
        instance.run.return_value = {"apply": False, "mutation_performed": False}
        output = io.StringIO()
        with (
            mock.patch.object(contract.os, "umask") as umask,
            mock.patch.object(
                contract,
                "PullContractMaintenance",
                return_value=instance,
            ) as maintenance,
            contextlib.redirect_stdout(output),
        ):
            result = contract.main(
                [
                    "apply",
                    "--operation-id",
                    CONTRACT_OPERATION,
                    "--production-root",
                    str(self.production),
                    "--runtime-root",
                    str(self.runtime),
                ]
            )

        self.assertEqual(result, 0)
        umask.assert_called_once_with(0o077)
        self.assertFalse(maintenance.call_args.kwargs["apply"])
        self.assertFalse(json.loads(output.getvalue())["mutation_performed"])

    def test_pending_code_deploy_marker_blocks_contract_plan_and_apply(self) -> None:
        fake = self._fake_controller()
        contract.legacy.atomic_json(
            fake.marker_path,
            {"operation_id": DEPLOY_OPERATION},
        )
        with mock.patch.object(
            contract.pull, "PullDeployController", return_value=fake
        ):
            for apply in (False, True):
                maintenance = contract.PullContractMaintenance(
                    self.production,
                    self.runtime,
                    CONTRACT_OPERATION,
                    apply=apply,
                )
                with (
                    mock.patch.object(maintenance.controller, "ensure_root"),
                    self.assertRaisesRegex(
                        contract.PullContractError, "code deployment"
                    ),
                ):
                    maintenance.run()


if __name__ == "__main__":
    unittest.main()

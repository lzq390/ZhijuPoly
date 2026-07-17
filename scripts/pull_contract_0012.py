#!/usr/bin/env python3
"""Checksum-pinned 0012 maintenance bound to the pull-deployment state.

This adapter deliberately contains no source-bundle, ``ops/current`` or
``ops/releases`` compatibility path.  It reuses the already-reviewed database
archive/recovery core from :mod:`release_controller`, but supplies a runtime
adapter whose authority is the sealed pull descriptor, external deployment
state and clean live checkout.
"""

from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import time
from typing import Any, BinaryIO, Callable, Iterator


PRODUCTION_ROOT = Path("/data/lzq/gith/nexpoly")
RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

import pull_deploy_controller as pull  # noqa: E402


def _governance_sibling_path(name: str) -> Path:
    """Return one immutable sibling from the installed governance core.

    Bootstrap installs this adapter, the retired-CLI governance core and its
    helper as one byte-identical set under the external runtime root.  Loading
    live-checkout Python before that checkout has been bound would invert the
    trust relationship, so there is deliberately no source-tree fallback.
    """

    if name not in {"release_controller.py", "monomer_worker_env.py"}:
        raise RuntimeError("unsupported 0012 governance sibling")
    path = SCRIPT_DIRECTORY / name
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"0012 governance core is missing: {path}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise RuntimeError(
            f"0012 governance core must be owner-controlled and immutable: {path}"
        )
    return path


def _governance_core_path() -> Path:
    return _governance_sibling_path("release_controller.py")


def _load_governance_core() -> Any:
    path = _governance_core_path()
    source_directory = str(path.parent)
    if source_directory not in sys.path:
        sys.path.insert(0, source_directory)
    spec = importlib.util.spec_from_file_location(
        "_nexpoly_pull_contract_0012_governance",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the 0012 governance core")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


legacy = _load_governance_core()


CONTRACT_VERSION = "0012_drop_polytao_jobs"
CONTRACT_CHECKSUM = "c59b6f1efe9f926ad135379bd1a7141a7920730fa93c0e802646b1b913511728"
REQUIRED_STATE_DIRECTORIES = (
    Path("config/docker"),
    Path("state/contract-operations"),
    Path("state/contract-verification-databases"),
    Path("audit/contracts/0012"),
    Path("backups/contracts/0012"),
)


class PullContractError(RuntimeError):
    """A fail-closed pull-state binding or maintenance error."""


@dataclass(frozen=True)
class PullBinding:
    controller: pull.PullDeployController
    current_state: dict[str, Any]
    descriptor: dict[str, Any]
    descriptor_sha256: str
    descriptor_path: Path
    repository: dict[str, str]
    migration_manifest_path: Path
    migration_records: list[dict[str, Any]]
    adapter_sha256: str
    governance_core_sha256: str
    governance_helper_sha256: str
    active_control: dict[str, Any]
    active_control_sha256: str
    control_manifest_sha256: str
    live_production_config: dict[str, str]
    external_database_audit_helper: dict[str, str]


def _require_private_file(path: Path, *, executable: bool = False) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PullContractError(f"required private file is missing: {path}") from exc
    expected_mode = 0o700 if executable else 0o600
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != expected_mode
    ):
        raise PullContractError(
            f"required private file must be owner-only mode {expected_mode:04o}: {path}"
        )


def _external_database_audit_helper_evidence(
    controller: pull.PullDeployController,
) -> dict[str, str]:
    values = pull.parse_literal_env(controller.config_dir / "deploy.env")
    key = legacy.CONTRACT_0012_EXTERNAL_AUDIT_COMMAND
    configured = values.get(key)
    command = shlex.split(configured) if isinstance(configured, str) else []
    expected = controller.config_dir / "contract-0012-external-database-audit"
    if len(command) != 1 or Path(command[0]) != expected:
        raise PullContractError("0012 external database audit helper path is not fixed")
    _require_private_file(expected, executable=True)
    return {
        "path": str(expected),
        "sha256": pull.sha256_file(expected),
    }


def _validate_current_state_shape(state: dict[str, Any]) -> None:
    try:
        pull.validate_current_deployment_state(state)
    except (pull.PullDeployError, OSError, ValueError, TypeError) as exc:
        raise PullContractError(
            "current pull-deployment state has an invalid exact schema"
        ) from exc


def _validate_contract_policy(records: list[dict[str, Any]]) -> None:
    by_version = {record.get("version"): record for record in records}
    contract = by_version.get(CONTRACT_VERSION)
    if (
        not isinstance(contract, dict)
        or contract.get("kind") != "contract"
        or contract.get("epoch") != 1
        or contract.get("checksum") != CONTRACT_CHECKSUM
    ):
        raise PullContractError(
            "live migration policy has the wrong 0012 contract identity"
        )


def load_binding(
    production_root: Path,
    runtime_root: Path,
    *,
    apply: bool,
) -> PullBinding:
    controller = pull.PullDeployController(
        production_root,
        runtime_root,
        apply=apply,
    )
    controller.ensure_roots(mutating=apply)
    state = pull.load_private_json(controller.current_state_path)
    _validate_current_state_shape(state)
    deployment_operation = pull.require_operation_id(state["operation_id"])
    descriptor, descriptor_sha256 = controller._load_prepared(deployment_operation)
    descriptor_path = (
        controller.prepared_root / deployment_operation / "descriptor.json"
    )
    if state["descriptor_sha256"] != descriptor_sha256:
        raise PullContractError(
            "current state is not bound to its sealed pull descriptor"
        )
    if (
        state["source_sha"] != descriptor["repository"]["target_sha"]
        or state["source_tree"] != descriptor["repository"]["target_tree"]
        or state["images"] != descriptor["images"]
        or state["asset_manifest_digest"]
        != descriptor["release_input"]["asset_manifest_digest"]
        or state["asset_identity"] != descriptor["release_input"]["asset"]
        or state["byteff2_commit"]
        != descriptor["release_input"]["asset"]["byteff2_commit"]
        or state["active_monomer_md_slot"].get("source_sha")
        != descriptor["repository"]["target_sha"]
        or state["active_monomer_md_slot"].get("source_tree")
        != descriptor["repository"]["target_tree"]
        or state["active_monomer_md_slot"].get("slot_record_sha256")
        != descriptor["monomer_md"]["slot_record_sha256"]
        or state["monomer_md_worker_env"] != descriptor["monomer_md"]["worker_env"]
        or state["monomer_md_systemd_unit"]
        != {
            "target_path": descriptor["monomer_md"]["systemd_unit"]["target_path"],
            "sha256": descriptor["monomer_md"]["systemd_unit"]["sha256"],
            "control_release_id": descriptor["monomer_md"]["systemd_unit"][
                "control_release_id"
            ],
            "launcher_sha256": descriptor["monomer_md"]["systemd_unit"][
                "launcher_sha256"
            ],
        }
        or state["control_helpers"] != descriptor["controller"]["helpers"]
        or state["production_config"] != descriptor["production_config"]
    ):
        raise PullContractError("current state differs from its sealed pull descriptor")
    active_control = controller.active_control_evidence()
    if active_control != state[
        "active_control"
    ] or not controller._active_matches_candidate(
        active_control, descriptor["controller"]["executor_control"]
    ):
        raise PullContractError(
            "active control authority differs from current deployment"
        )
    try:
        control_manifest, control_root = pull._control_runtime.load_control_release(
            runtime_root, active_control["release_id"]
        )
    except Exception as exc:
        raise PullContractError("active control release is invalid") from exc
    contract_entry = control_manifest["entrypoints"].get("contract-0012")
    if (
        not isinstance(contract_entry, dict)
        or contract_entry.get("kind") != "python"
        or contract_entry.get("file") != Path(__file__).name
        or (
            not controller.test_root_mode
            and Path(__file__).resolve()
            != (control_root / contract_entry["file"]).resolve()
        )
    ):
        raise PullContractError(
            "0012 adapter is not the active manifest-authorized entrypoint"
        )
    controller._validate_database_backup(
        descriptor,
        state["database_backup"],
        require_operation_backup=True,
    )
    # The pull deployment state records the configuration used by that
    # deployment. Rotatable credentials are not cross-operation identity; a
    # new contract operation seals the currently valid evidence instead.
    live_production_config = controller.production_config_evidence(
        check_free_space=False
    )
    repository = controller.repository_identity(require_ssh_origin=apply)
    if (
        repository["sha"] != state["source_sha"]
        or repository["tree"] != state["source_tree"]
    ):
        raise PullContractError(
            "live checkout differs from the current pull deployment"
        )

    manifest_path = production_root / "backend/migrations/postgres/manifest.json"
    if pull.sha256_file(manifest_path) != descriptor["migrations"]["sha256"]:
        raise PullContractError(
            "live migration manifest differs from the sealed descriptor"
        )
    records = legacy.release_migrations_from_policy_manifest(
        manifest_path,
        include_baseline=False,
    )
    canonical_records = legacy.release_migrations_from_policy_manifest(
        manifest_path,
        include_baseline=True,
    )
    if descriptor["migrations"].get("schema_version") != 2 or descriptor[
        "migrations"
    ].get("records") != json.loads(manifest_path.read_text(encoding="utf-8")).get(
        "migrations"
    ):
        raise PullContractError(
            "sealed migration evidence differs from the live policy"
        )
    _validate_contract_policy(records)
    history = state["migrations"]
    contract_index = next(
        index
        for index, record in enumerate(canonical_records)
        if record["version"] == CONTRACT_VERSION
    )
    expected_before = [dict(record) for record in canonical_records[:contract_index]]
    expected_after = [
        dict(record) for record in canonical_records[: contract_index + 1]
    ]
    if history not in (expected_before, expected_after):
        raise PullContractError(
            "0012 requires the exact ordered canonical migration history through 0011"
        )
    if history == expected_after:
        approvals = [
            approval
            for approval in state["approved_contracts"]
            if isinstance(approval, dict)
            and approval.get("version") == CONTRACT_VERSION
        ]
        if len(approvals) != 1 or approvals[0].get("checksum") != CONTRACT_CHECKSUM:
            raise PullContractError("0012 is recorded without one canonical approval")
    legacy.approved_contract_migrations(_legacy_state_projection(state))
    return PullBinding(
        controller=controller,
        current_state=state,
        descriptor=descriptor,
        descriptor_sha256=descriptor_sha256,
        descriptor_path=descriptor_path,
        repository=repository,
        migration_manifest_path=manifest_path,
        migration_records=records,
        adapter_sha256=pull.sha256_file(Path(__file__).resolve()),
        governance_core_sha256=pull.sha256_file(_governance_core_path()),
        governance_helper_sha256=pull.sha256_file(
            _governance_sibling_path("monomer_worker_env.py")
        ),
        active_control=active_control,
        active_control_sha256=pull.canonical_json_digest(active_control),
        control_manifest_sha256=pull.sha256_file(
            control_root / pull._control_runtime.CONTROL_MANIFEST_NAME
        ),
        live_production_config=live_production_config,
        external_database_audit_helper=(
            _external_database_audit_helper_evidence(controller)
        ),
    )


def _pull_document(binding: PullBinding) -> dict[str, Any]:
    """Project sealed pull evidence into the legacy governance core."""

    asset_digest = binding.descriptor["release_input"]["asset_manifest_digest"]
    return {
        "schema_version": 2,
        "source_sha": binding.repository["sha"],
        "ci_run_id": str(binding.descriptor["ci"]["workflow_run_id"]),
        "images": {
            role: binding.descriptor["images"][role]["digest_ref"]
            for role in ("backend", "web")
        },
        "worker_runtime_present": True,
        "asset_manifest_digest": asset_digest,
        "datasets_on_asset_change": binding.descriptor["release_input"].get(
            "datasets_on_asset_change", []
        ),
        "migrations": binding.migration_records,
        "resolved_asset_manifest_digest": asset_digest,
        "current_asset_manifest_digest": asset_digest,
        "resolved_asset_root": str(binding.controller.state_dir / "current-assets"),
        "current_asset_root": str(binding.controller.state_dir / "current-assets"),
    }


def _legacy_state_projection(state: dict[str, Any]) -> dict[str, Any]:
    """Expose record-based Pull state to the retired name-based state machine."""

    projected = json.loads(json.dumps(state))
    migrations = projected.get("migrations")
    if not isinstance(migrations, list) or any(
        not isinstance(record, dict) or not isinstance(record.get("version"), str)
        for record in migrations
    ):
        raise PullContractError("pull deployment has invalid migration records")
    projected["migrations"] = [record["version"] for record in migrations]
    return projected


def _pull_state_projection(
    binding: PullBinding,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Persist a legacy transition without losing checksum-complete records."""

    projected = json.loads(json.dumps(state))
    versions = projected.get("migrations")
    canonical = binding.descriptor["migrations"]["records"]
    if not isinstance(versions, list) or not isinstance(canonical, list):
        raise PullContractError("contract state has invalid migration history")
    canonical_by_version = {
        record.get("version"): record
        for record in canonical
        if isinstance(record, dict)
    }
    contract_index = next(
        index
        for index, record in enumerate(canonical)
        if record["version"] == CONTRACT_VERSION
    )
    expected_before = [record["version"] for record in canonical[:contract_index]]
    expected_after = [*expected_before, CONTRACT_VERSION]
    if versions not in (expected_before, expected_after):
        raise PullContractError(
            "contract transition does not preserve the canonical migration prefix"
        )
    projected["migrations"] = [
        dict(canonical_by_version[version]) for version in versions
    ]
    compatibility = projected.get("migration_compatibility")
    if isinstance(compatibility, dict):
        projected["migration_compatibility"] = (
            pull.build_migration_compatibility_state(
                compatibility,
                code_manifest_sha256=binding.descriptor["migrations"]["sha256"],
                migrations=projected["migrations"],
            )
        )
    projected.pop("applied_migrations", None)
    return projected


class PullContractLifecycle(pull.SystemLifecycle):
    """System lifecycle with a sanitized contract control environment."""

    def _environment(
        self,
        controller: pull.PullDeployController,
        descriptor: dict[str, Any],
    ) -> dict[str, str]:
        values = pull.parse_literal_env(controller.config_dir / "deploy.env")
        pull.validate_deploy_control_values(
            values,
            runtime_root=controller.runtime_root,
        )
        environment = pull.clean_control_environment(controller.runtime_root)
        environment.update(values)
        environment.update(
            {
                "NEXPOLY_BACKEND_IMAGE": descriptor["images"]["backend"]["digest_ref"],
                "NEXPOLY_WEB_IMAGE": descriptor["images"]["web"]["digest_ref"],
                "NEXPOLY_RUNTIME_ROOT": str(controller.runtime_root),
                "NEXPOLY_APP_ENV_FILE": str(controller.config_dir / "app.env"),
                "NEXPOLY_ASSET_ROOT": str(controller.state_dir / "current-assets"),
                "COMPOSE_PROJECT_NAME": "nexpoly",
            }
        )
        return environment

    def isolate_recovery_ingress(
        self,
        controller: pull.PullDeployController,
        descriptor: dict[str, Any],
    ) -> dict[str, Any]:
        """Idempotently stop nginx and prove absence despite a lost response."""

        environment = self._environment(controller, descriptor)
        stop_error: BaseException | None = None
        try:
            controller.runner.run(
                self._compose(controller, "stop", "nginx"),
                cwd=controller.production_root,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError, pull.PullDeployError) as exc:
            stop_error = exc
        observed = controller.runner.run(
            self._compose(controller, "ps", "--quiet", "nginx"),
            cwd=controller.production_root,
            env=environment,
        )
        if str(observed.stdout).strip():
            raise PullContractError(
                "0012 recovery could not prove nginx ingress isolation"
            ) from stop_error
        return {
            "isolated": True,
            "stop_response_lost": stop_error is not None,
            "verified_at": legacy.utc_now(),
        }

    def _isolate_ingress(
        self,
        controller: pull.PullDeployController,
        descriptor: dict[str, Any],
    ) -> None:
        self.isolate_recovery_ingress(controller, descriptor)

    def recovery_runtime_presence(
        self,
        controller: pull.PullDeployController,
        descriptor: dict[str, Any],
    ) -> str:
        """Classify the source-reading runtime as live, fully stopped or partial."""

        environment = self._environment(controller, descriptor)
        backend = controller.runner.run(
            self._compose(controller, "ps", "--quiet", "backend"),
            cwd=controller.production_root,
            env=environment,
        )
        backend_ids = [value for value in str(backend.stdout).splitlines() if value]
        worker = controller.runner.run(
            [
                "systemctl",
                "--user",
                "is-active",
                pull.MONOMER_MD_UNIT_NAME,
            ],
            env=environment,
            check=False,
        )
        worker_status = str(worker.stdout).strip()
        worker_live = worker.returncode == 0 and worker_status == "active"
        worker_stopped = worker.returncode in {3, 4} and worker_status in {
            "inactive",
            "unknown",
        }
        if len(backend_ids) == 1 and worker_live:
            return "live"
        if not backend_ids and worker_stopped:
            return "stopped"
        return "partial"

    def _recovery_runtime_presence(
        self,
        controller: pull.PullDeployController,
        descriptor: dict[str, Any],
    ) -> str:
        return self.recovery_runtime_presence(controller, descriptor)

    def _restore_capacity_bytes(
        self,
        controller: pull.PullDeployController,
        *,
        dump_size: int | None = None,
    ) -> int:
        values = pull.parse_literal_env(controller.config_dir / "deploy.env")
        raw = values.get("NEXPOLY_POSTGRES_RESTORE_TMPFS_BYTES")
        if raw is None or not raw.isdigit():
            raise PullContractError(
                "deploy.env must pin NEXPOLY_POSTGRES_RESTORE_TMPFS_BYTES"
            )
        capacity = int(raw)
        minimum = max(8 * 1024**3, (dump_size or 0) * 8)
        if capacity < minimum or capacity > 256 * 1024**3:
            raise PullContractError(
                "contract restore tmpfs capacity is outside the governed bound"
            )
        return capacity

    def verify_contract_postgres16_restore(
        self,
        controller: pull.PullDeployController,
        descriptor: dict[str, Any],
        dump: Path,
        dump_digest: str,
        expected_archive: dict[str, Any],
    ) -> dict[str, Any]:
        """Restore and scientifically compare the legacy history in PG16."""

        operation_id = pull.require_operation_id(descriptor["operation_id"])
        name = f"nexpoly-contract-restore-{operation_id}"
        clean = pull.clean_control_environment(controller.runtime_root)
        capacity = self._restore_capacity_bytes(
            controller,
            dump_size=dump.stat().st_size,
        )
        self.cleanup_contract_restore_container(controller, operation_id)

        failure: BaseException | None = None

        def run(*arguments: str, **kwargs: Any) -> Any:
            return controller.runner.run(list(arguments), env=clean, **kwargs)

        def psql_json(sql: str) -> Any:
            result = run(
                "docker",
                "exec",
                name,
                "psql",
                "--set",
                "ON_ERROR_STOP=1",
                "--tuples-only",
                "--no-align",
                "--username",
                "postgres",
                "--dbname",
                "nexpoly_restore",
                "--command",
                sql,
            )
            raw = str(result.stdout).strip()
            if not raw or len(raw.encode("utf-8")) > 8 * 1024 * 1024:
                raise PullContractError(
                    "isolated PostgreSQL evidence is empty or oversized"
                )
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise PullContractError(
                    "isolated PostgreSQL evidence is invalid JSON"
                ) from exc

        try:
            run_error: BaseException | None = None
            try:
                run(
                    "docker",
                    "run",
                    "--detach",
                    "--pull=never",
                    "--name",
                    name,
                    "--network",
                    "none",
                    "--tmpfs",
                    f"/var/lib/postgresql/data:rw,nosuid,nodev,size={capacity}",
                    "--env",
                    "POSTGRES_HOST_AUTH_METHOD=trust",
                    "--label",
                    f"com.nexpoly.contract-restore-operation={operation_id}",
                    pull.POSTGRES16_IMAGE,
                )
            except BaseException as exc:
                run_error = exc
            committed = run("docker", "container", "inspect", name, check=False)
            if committed.returncode != 0:
                raise PullContractError(
                    "cannot prove contract restore container startup"
                ) from run_error
            try:
                committed_values = json.loads(str(committed.stdout))
                committed_record = committed_values[0]
            except (json.JSONDecodeError, IndexError, TypeError) as exc:
                raise PullContractError(
                    "started contract restore inspection is malformed"
                ) from exc
            try:
                pull.SystemLifecycle._validate_isolated_container(
                    committed_record,
                    name=name,
                    image=pull.POSTGRES16_IMAGE,
                    operation_label="com.nexpoly.contract-restore-operation",
                    operation_id=operation_id,
                    tmpfs_capacity=capacity,
                )
            except pull.PullDeployError as exc:
                raise PullContractError(
                    "started contract restore has foreign identity"
                ) from exc
            deadline = time.monotonic() + 120
            while True:
                ready = run(
                    "docker",
                    "exec",
                    name,
                    "pg_isready",
                    "--username",
                    "postgres",
                    check=False,
                )
                if ready.returncode == 0:
                    break
                if ready.returncode not in {1, 2} or time.monotonic() >= deadline:
                    raise PullContractError(
                        "isolated contract PostgreSQL 16 did not become ready"
                    )
                time.sleep(1)
            run(
                "docker",
                "exec",
                name,
                "createdb",
                "--username",
                "postgres",
                "nexpoly_restore",
            )
            with dump.open("rb") as source:
                run(
                    "docker",
                    "exec",
                    "--interactive",
                    name,
                    "pg_restore",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-acl",
                    "--username",
                    "postgres",
                    "--dbname",
                    "nexpoly_restore",
                    text=False,
                    stdin=source,
                    timeout=1800,
                )
            version = str(
                run(
                    "docker",
                    "exec",
                    name,
                    "psql",
                    "--tuples-only",
                    "--no-align",
                    "--username",
                    "postgres",
                    "--dbname",
                    "nexpoly_restore",
                    "--command",
                    "SHOW server_version_num",
                ).stdout
            ).strip()
            if not version.isdigit() or not version.startswith("16"):
                raise PullContractError("contract restore did not use PostgreSQL 16")

            ledger = psql_json(
                "SELECT COALESCE(json_agg(json_build_object("
                "'version', version, 'checksum', checksum) ORDER BY version), "
                "'[]'::json)::text FROM governance.schema_migrations"
            )
            canonical = descriptor["migrations"]["records"]
            contract_index = next(
                index
                for index, record in enumerate(canonical)
                if record["version"] == CONTRACT_VERSION
            )
            expected_ledger = [
                {"version": record["version"], "checksum": record["checksum"]}
                for record in canonical[:contract_index]
            ]
            if ledger != expected_ledger:
                raise PullContractError(
                    "isolated restore ledger differs from canonical 0001-0011"
                )

            rows = psql_json(
                "SELECT COALESCE(json_agg(to_jsonb(jobs) ORDER BY job_id::text), "
                "'[]'::json)::text FROM generation.polytao_jobs AS jobs"
            )
            statuses = psql_json(
                "SELECT COALESCE(json_object_agg(status, count), '{}'::json)::text "
                "FROM (SELECT status, COUNT(*) AS count FROM "
                "generation.polytao_jobs GROUP BY status ORDER BY status) AS counts"
            )
            columns = psql_json(
                "SELECT COALESCE(json_agg(row_to_json(records) ORDER BY ordinal_position), "
                "'[]'::json)::text FROM (SELECT column_name, ordinal_position, "
                "data_type, udt_schema, udt_name, is_nullable, column_default "
                "FROM information_schema.columns WHERE table_schema='generation' "
                "AND table_name='polytao_jobs') AS records"
            )
            indexes = psql_json(
                "SELECT COALESCE(json_agg(row_to_json(records) ORDER BY indexname), "
                "'[]'::json)::text FROM (SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname='generation' AND tablename='polytao_jobs') AS records"
            )
            constraints = psql_json(
                "SELECT COALESCE(json_agg(row_to_json(records) ORDER BY name), "
                "'[]'::json)::text FROM (SELECT constraint_row.conname AS name, "
                "constraint_row.contype AS type, constraint_row.condeferrable AS deferrable, "
                "constraint_row.condeferred AS initially_deferred, "
                "constraint_row.convalidated AS validated, "
                "pg_get_constraintdef(constraint_row.oid, true) AS definition "
                "FROM pg_constraint AS constraint_row JOIN pg_class AS relation "
                "ON relation.oid=constraint_row.conrelid JOIN pg_namespace AS namespace "
                "ON namespace.oid=relation.relnamespace WHERE namespace.nspname='generation' "
                "AND relation.relname='polytao_jobs') AS records"
            )
            triggers = psql_json(
                "SELECT COALESCE(json_agg(row_to_json(records) ORDER BY name), "
                "'[]'::json)::text FROM (SELECT trigger_row.tgname AS name, "
                "trigger_row.tgenabled AS enabled, pg_get_triggerdef(trigger_row.oid, true) "
                "AS definition FROM pg_trigger AS trigger_row JOIN pg_class AS relation "
                "ON relation.oid=trigger_row.tgrelid JOIN pg_namespace AS namespace "
                "ON namespace.oid=relation.relnamespace WHERE namespace.nspname='generation' "
                "AND relation.relname='polytao_jobs' AND NOT trigger_row.tgisinternal) AS records"
            )
            structure = {
                "columns": columns,
                "indexes": indexes,
                "constraints": constraints,
                "triggers": triggers,
            }

            def canonical_digest(value: Any) -> str:
                payload = json.dumps(
                    value,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    default=str,
                ).encode("utf-8")
                return hashlib.sha256(payload).hexdigest()

            archive = {
                "schema_version": 2,
                "row_count": len(rows),
                "status_counts": {
                    str(key): int(value) for key, value in statuses.items()
                },
                "rows_sha256": canonical_digest(rows),
                "schema_sha256": canonical_digest(structure),
                "structure_counts": {
                    key: len(value) for key, value in structure.items()
                },
            }
            if archive != expected_archive:
                raise PullContractError(
                    "isolated PostgreSQL 16 history differs from live archive evidence"
                )
            return {
                "schema_version": 2,
                "restored": True,
                "postgres_major": 16,
                "postgres_version_num": version,
                "image": pull.POSTGRES16_IMAGE,
                "dump_sha256": dump_digest,
                "ledger": ledger,
                "archive": archive,
                "operation_id": operation_id,
                "verified_at": legacy.utc_now(),
            }
        except BaseException as exc:
            failure = exc
            raise
        finally:
            cleanup_error: BaseException | None = None
            try:
                self.cleanup_contract_restore_container(controller, operation_id)
            except BaseException as exc:
                cleanup_error = exc
            if cleanup_error is not None:
                if failure is not None:
                    raise cleanup_error from failure
                raise cleanup_error

    def cleanup_contract_restore_container(
        self,
        controller: pull.PullDeployController,
        operation_id: str,
    ) -> bool:
        """Remove only an exact operation-owned interrupted restore container."""

        operation_id = pull.require_operation_id(operation_id)
        name = f"nexpoly-contract-restore-{operation_id}"
        clean = pull.clean_control_environment(controller.runtime_root)
        capacity = self._restore_capacity_bytes(controller)
        probe = controller.runner.run(
            ["docker", "container", "inspect", name],
            env=clean,
            check=False,
        )
        if probe.returncode == 1:
            return False
        if probe.returncode != 0:
            raise PullContractError("cannot inspect interrupted contract restore")
        try:
            values = json.loads(str(probe.stdout))
        except json.JSONDecodeError as exc:
            raise PullContractError(
                "interrupted contract restore inspection is invalid JSON"
            ) from exc
        if not isinstance(values, list) or len(values) != 1:
            raise PullContractError("interrupted contract restore identity is invalid")
        record = values[0]
        try:
            container_id = pull.SystemLifecycle._validate_isolated_container(
                record,
                name=name,
                image=pull.POSTGRES16_IMAGE,
                operation_label="com.nexpoly.contract-restore-operation",
                operation_id=operation_id,
                tmpfs_capacity=capacity,
            )
        except pull.PullDeployError as exc:
            raise PullContractError(
                "refusing to remove a foreign or mismatched restore container"
            ) from exc
        removal_error: BaseException | None = None
        try:
            controller.runner.run(
                ["docker", "rm", "--force", container_id],
                env=clean,
                check=False,
            )
        except BaseException as exc:
            removal_error = exc
        try:
            absent = controller.runner.run(
                ["docker", "container", "inspect", name],
                env=clean,
                check=False,
            )
        except BaseException as exc:
            raise PullContractError(
                "cannot prove interrupted contract restore was removed"
            ) from (removal_error or exc)
        if absent.returncode != 1:
            raise PullContractError(
                "cannot prove interrupted contract restore was removed"
            ) from removal_error
        return True


class PullRuntimeController(legacy.ReleaseController):
    """Legacy database primitives rebound to live pull-deployment identities."""

    def __init__(
        self,
        binding: PullBinding,
        operation_id: str,
        *,
        apply: bool,
    ) -> None:
        self.binding = binding
        self.pull_controller = binding.controller
        self.lifecycle = PullContractLifecycle()
        self.operation_id = operation_id
        self.root = self.pull_controller.production_root
        self.runtime_root = self.pull_controller.runtime_root
        self.ops = self.runtime_root
        self.config_dir = self.pull_controller.config_dir
        self.env_file = self.config_dir / "deploy.env"
        self.document = _pull_document(binding)
        self.runtime_descriptor = json.loads(json.dumps(binding.descriptor))
        if self.runtime_descriptor.get("previous_deployment") is None:
            # The sealed first-takeover descriptor correctly records that no
            # *previous governed deployment* existed.  Once that takeover is
            # current, contract maintenance must use the persistent Backend /
            # Worker control plane, not replay the one-time legacy bootstrap
            # quiesce hook.
            self.runtime_descriptor["previous_deployment"] = {
                "source_sha": binding.current_state["source_sha"],
                "source_tree": binding.current_state["source_tree"],
                "governed_current_runtime": True,
            }
        self.sha = binding.repository["sha"]
        self.manifest_path = binding.descriptor_path
        self.mode = "auto"
        self.apply = apply
        self.release_dir = self.root
        self.candidate_dir = self.root
        self.staging = self.runtime_root / "state" / "forbidden-bundle-staging"
        self.state_path = self.pull_controller.current_state_path
        self.in_progress_path = self.pull_controller.marker_path
        self.previous_state: dict[str, Any] = {}
        self.backup_path: Path | None = None
        self.database_changed = False
        self.worker_restart_deferred = False
        self.worker_drain_info: dict[str, Any] | None = None
        self.worker_previous_instance: str | None = None
        self.worker_base_python_identity: dict[str, Any] | None = None
        self.worker_toolchain_identity: dict[str, Any] | None = None
        self.deploy_transport_required = True
        self.worker_values: dict[str, str] = {}
        self.bootstrap = False
        self.attempt_path: Path | None = None
        self._drain_evidence: dict[str, Any] | None = None
        self._runtime_identity_evidence: dict[str, Any] | None = None
        self.contract_canary_evidence: dict[str, Any] | None = None
        self.contract_marker_path = (
            self.runtime_root / "state/contract-0012-in-progress.json"
        )
        self._contract_marker_loader: Callable[[], dict[str, Any]] | None = None
        self._contract_marker_writer: Callable[[dict[str, Any]], None] | None = None
        self.backup_root = self.runtime_root / "backups/contracts/0012" / operation_id

    def ensure_root(self) -> None:
        self.pull_controller.ensure_roots(mutating=self.apply)
        for relative in REQUIRED_STATE_DIRECTORIES:
            directory = self.runtime_root / relative
            pull.ensure_private_directory(directory)
        if self.apply:
            try:
                manifest, release_root = pull._control_runtime.load_control_release(
                    self.runtime_root, self.binding.active_control["release_id"]
                )
            except Exception as exc:
                raise PullContractError(
                    "0012 active content-addressed controls are invalid"
                ) from exc
            entrypoint = manifest["entrypoints"].get("contract-0012")
            if (
                not isinstance(entrypoint, dict)
                or entrypoint.get("kind") != "python"
                or Path(__file__).resolve()
                != (release_root / str(entrypoint.get("file"))).resolve()
            ):
                raise PullContractError(
                    "0012 mutation must use the active manifest-authorized adapter"
                )
        legacy.require_docker_compose_version(
            pull.clean_control_environment(self.runtime_root)
        )

    @contextlib.contextmanager
    def deployment_lock(self) -> Iterator[None]:
        with self.pull_controller.deployment_lock():
            if (
                self.pull_controller.marker_path.exists()
                or self.pull_controller.marker_path.is_symlink()
            ):
                raise PullContractError(
                    "interrupted code deployment must be recovered before 0012"
                )
            yield

    def _validate_asset_pointer(self) -> tuple[Path, str, str]:
        pointer = self.pull_controller.state_dir / "current-assets"
        expected = self.binding.descriptor["release_input"]["asset_manifest_digest"]
        root, digest, commit = legacy.inspect_managed_asset_pointer(pointer, expected)
        if digest != expected:
            raise PullContractError(
                "managed asset pointer differs from the sealed descriptor"
            )
        legacy.validate_candidate_byteff2_runtime_assets(root)
        self.document.update(
            {
                "resolved_asset_manifest_digest": digest,
                "current_asset_manifest_digest": digest,
                "resolved_asset_root": str(root),
                "current_asset_root": str(root),
                "resolved_byteff2_commit": commit,
                "current_byteff2_commit": commit,
            }
        )
        return root, digest, commit

    def environment(self) -> dict[str, str]:
        for path in (
            self.env_file,
            self.config_dir / "app.env",
            self.config_dir / "worker.env",
        ):
            _require_private_file(path)
        values = pull.parse_literal_env(self.env_file)
        pull.validate_deploy_control_values(
            values,
            runtime_root=self.runtime_root,
        )
        if values.get("NEXPOLY_POSTGRES_DB") != "nexpoly":
            raise PullContractError("0012 is locked to the nexpoly production database")
        if not values.get("NEXPOLY_POSTGRES_USER") or not values.get(
            "NEXPOLY_POSTGRES_PASSWORD"
        ):
            raise PullContractError("production PostgreSQL credentials are incomplete")
        self._validate_asset_pointer()
        environment = pull.clean_control_environment(self.runtime_root)
        environment.update(values)
        environment.update(
            {
                "NEXPOLY_BACKEND_IMAGE": self.binding.descriptor["images"]["backend"][
                    "digest_ref"
                ],
                "NEXPOLY_WEB_IMAGE": self.binding.descriptor["images"]["web"][
                    "digest_ref"
                ],
                "NEXPOLY_RUNTIME_ROOT": str(self.runtime_root),
                "NEXPOLY_APP_ENV_FILE": str(self.config_dir / "app.env"),
                "NEXPOLY_ASSET_ROOT": str(
                    self.pull_controller.state_dir / "current-assets"
                ),
                "COMPOSE_PROJECT_NAME": "nexpoly",
            }
        )
        return environment

    def validate_external_database_audit_helper(self) -> dict[str, str]:
        """Re-hash the fixed audit helper against the sealed pull binding."""

        evidence = _external_database_audit_helper_evidence(self.pull_controller)
        if evidence != self.binding.external_database_audit_helper:
            raise PullContractError(
                "0012 external database audit helper changed during the operation"
            )
        return evidence

    def bootstrap_hook_command(
        self,
        environment: dict[str, str],
        key: str,
    ) -> list[str]:
        command = super().bootstrap_hook_command(environment, key)
        if key == legacy.CONTRACT_0012_EXTERNAL_AUDIT_COMMAND:
            evidence = self.validate_external_database_audit_helper()
            if command != [evidence["path"]]:
                raise PullContractError(
                    "0012 external database audit helper differs from the sealed binding"
                )
        return command

    def run(
        self,
        command: list[str],
        *,
        env: dict[str, str],
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | None = None,
    ) -> None:
        """Defer public ingress to the fenced pull lifecycle resume.

        The inherited 0012 state machine starts nginx immediately before it
        calls ``drain(False)``.  Under pull deployment, that would expose a
        partially resumed Worker/Backend.  Exact nginx-only starts are
        therefore no-ops here; ``PullContractLifecycle.resume`` opens Backend
        admission internally, verifies the process fence, and starts nginx as
        the final step.
        """

        try:
            up_index = command.index("up")
        except ValueError:
            up_index = -1
        if (
            up_index >= 0
            and command[-1:] == ["nginx"]
            and not any(
                value in {"backend", "lab-postgres", "postgres-init"}
                for value in command[up_index + 1 :]
            )
        ):
            return
        super().run(command, env=env, stdin=stdin, stdout=stdout)

    def validate_current_runtime(self, _environment: dict[str, str]) -> None:
        fresh = load_binding(self.root, self.runtime_root, apply=self.apply)
        if (
            fresh.descriptor_sha256 != self.binding.descriptor_sha256
            or fresh.current_state != self.binding.current_state
            or fresh.repository != self.binding.repository
        ):
            raise PullContractError("pull deployment identity changed before 0012")
        active = self.pull_controller._active_slot()
        if active != self.binding.current_state.get("active_monomer_md_slot"):
            raise PullContractError(
                "active Worker slot differs from current deployment state"
            )
        evidence = self.lifecycle.verify_runtime_identity(
            self.pull_controller,
            self.runtime_descriptor,
        )
        if not isinstance(evidence, dict) or set(evidence) != {
            "repository",
            "asset",
            "unit",
            "containers",
            "worker",
            "postgres_loopback",
            "verified_at",
        }:
            raise PullContractError("live runtime identity evidence is incomplete")
        if evidence.get("postgres_loopback") is not True:
            raise PullContractError("live PostgreSQL loopback identity was not proven")
        try:
            self._runtime_identity_evidence = json.loads(
                json.dumps(evidence, sort_keys=True)
            )
        except (TypeError, ValueError) as exc:
            raise PullContractError(
                "live runtime identity evidence is not serializable"
            ) from exc

    def bind_contract_marker_persistence(
        self,
        *,
        loader: Callable[[], dict[str, Any]],
        writer: Callable[[dict[str, Any]], None],
    ) -> None:
        if (
            self._contract_marker_loader is not None
            or self._contract_marker_writer is not None
        ):
            raise PullContractError("0012 runtime marker persistence is already bound")
        self._contract_marker_loader = loader
        self._contract_marker_writer = writer

    def _contract_marker(self) -> dict[str, Any]:
        if self._contract_marker_loader is None:
            raise PullContractError(
                "0012 runtime marker persistence is not authority-bound"
            )
        try:
            marker = self._contract_marker_loader()
        except (pull.PullDeployError, OSError, ValueError, TypeError) as exc:
            raise PullContractError(
                "0012 admission recovery marker is unavailable"
            ) from exc
        authority = marker.get("pull_maintenance_authority")
        if (
            marker.get("operation_id") != self.operation_id
            or marker.get("source_sha") != self.binding.repository["sha"]
            or marker.get("pull_descriptor_sha256") != self.binding.descriptor_sha256
            or not isinstance(authority, dict)
            or authority.get("source_sha") != self.binding.repository["sha"]
            or authority.get("source_tree") != self.binding.repository["tree"]
            or authority.get("pull_descriptor_sha256") != self.binding.descriptor_sha256
            or marker.get("pull_maintenance_authority_sha256")
            != pull.canonical_json_digest(authority)
        ):
            raise PullContractError("0012 admission recovery marker identity differs")
        return marker

    def _persist_runtime_recovery_verification(
        self, verification: object
    ) -> dict[str, Any]:
        sealed = pull.PullDeployController._sealed_runtime_verification(verification)
        marker = self._contract_marker()
        marker["runtime_recovery_verification"] = sealed
        marker["runtime_recovery_verification_sha256"] = pull.canonical_json_digest(
            sealed
        )
        marker["runtime_recovery_verification_persisted_at"] = legacy.utc_now()
        marker.pop("runtime_recovery_start_intent", None)
        if self._contract_marker_writer is None:
            raise PullContractError("0012 runtime marker writer is not authority-bound")
        self._contract_marker_writer(marker)
        committed = self._contract_marker()
        if committed.get("runtime_recovery_verification") != sealed or committed.get(
            "runtime_recovery_verification_sha256"
        ) != pull.canonical_json_digest(sealed):
            raise PullContractError(
                "0012 runtime recovery fence did not commit exactly"
            )
        return sealed

    def _persist_runtime_recovery_start_intent(
        self,
        *,
        reason: str,
    ) -> dict[str, Any]:
        if reason not in {"final-resume", "database-restore"}:
            raise PullContractError("0012 runtime start intent reason is invalid")
        marker = self._contract_marker()
        intent = {
            "target_sha": self.binding.repository["sha"],
            "reason": reason,
            "recorded_at": legacy.utc_now(),
        }
        marker["runtime_recovery_start_intent"] = intent
        if self._contract_marker_writer is None:
            raise PullContractError("0012 runtime marker writer is not authority-bound")
        self._contract_marker_writer(marker)
        committed = self._contract_marker()
        if committed.get("runtime_recovery_start_intent") != intent:
            raise PullContractError("0012 runtime start intent did not commit exactly")
        return committed

    def _runtime_start_intent_authorized(self, marker: dict[str, Any]) -> bool:
        intent = marker.get("runtime_recovery_start_intent")
        if intent is None:
            return False
        if (
            not isinstance(intent, dict)
            or set(intent) != {"target_sha", "reason", "recorded_at"}
            or intent.get("target_sha") != self.binding.repository["sha"]
            or intent.get("reason") not in {"final-resume", "database-restore"}
            or not isinstance(intent.get("recorded_at"), str)
            or not intent["recorded_at"]
        ):
            raise PullContractError("0012 runtime start intent is invalid")
        return True

    def _runtime_recovery_verification(self) -> dict[str, Any]:
        marker = self._contract_marker()
        verification = pull.PullDeployController._sealed_runtime_verification(
            marker.get("runtime_recovery_verification")
        )
        if marker.get("runtime_recovery_verification_sha256") != (
            pull.canonical_json_digest(verification)
        ):
            raise PullContractError("0012 runtime recovery fence digest differs")
        return verification

    @staticmethod
    def _allows_unfenced_pre_drain_recovery(marker: dict[str, Any]) -> bool:
        return bool(
            marker.get("runtime_recovery_verification") is None
            and marker.get("phase") == "prepared"
            and marker.get("database_change_started") is not True
            and marker.get("ingress_isolated_canary") is None
            and marker.get("status") in {"running", "failed"}
        )

    def drain(self, environment: dict[str, str], enabled: bool) -> None:
        if enabled:
            self._drain_evidence = self.lifecycle.drain(
                self.pull_controller,
                self.runtime_descriptor,
            )
            verification = {
                "health": "ok",
                "mode": "contract-0012-initial-drain",
                "recovery_fence": self.lifecycle._capture_runtime_recovery_fence(
                    self.pull_controller,
                    self.runtime_descriptor,
                    resumed=False,
                ),
                "verified_at": legacy.utc_now(),
            }
            self._persist_runtime_recovery_verification(verification)
        else:
            del environment
            marker = self._contract_marker()
            start_intent = self._runtime_start_intent_authorized(marker)
            expected = (
                None if start_intent else marker.get("runtime_recovery_verification")
            )
            if expected is not None and not isinstance(expected, dict):
                raise PullContractError("0012 runtime recovery verification is invalid")
            recovery = self.lifecycle.prepare_recovery_runtime(
                self.pull_controller,
                self.runtime_descriptor,
                expected,
                allow_unfenced=(
                    start_intent or self._allows_unfenced_pre_drain_recovery(marker)
                ),
            )
            state = recovery.get("runtime_state")
            if state == "stopped":
                self._persist_runtime_recovery_start_intent(reason="final-resume")
                self.lifecycle.start(
                    self.pull_controller,
                    self.runtime_descriptor,
                )
                verification = self.lifecycle.verify(
                    self.pull_controller,
                    self.runtime_descriptor,
                )
            elif state == "drained" and isinstance(recovery.get("verification"), dict):
                self._drain_evidence = recovery.get("drain")
                verification = recovery["verification"]
            else:
                raise PullContractError(
                    "0012 runtime recovery did not reach a resumable state"
                )
            verification = self._persist_runtime_recovery_verification(verification)
            self.lifecycle.resume(
                self.pull_controller,
                self.runtime_descriptor,
                verification,
            )

    def drain_worker(self, _environment: dict[str, str]) -> dict[str, Any]:
        if self._drain_evidence is None:
            raise PullContractError("Worker drain requested without a global drain")
        instances = self._drain_evidence.get("worker_instances", {})
        return {
            "supported": True,
            "active_jobs": 0,
            "worker_instance_id": instances.get("monomer-md"),
        }

    def wait_for_jobs(
        self,
        _environment: dict[str, str],
        *,
        ignore_monomer_md: bool = False,
    ) -> None:
        del ignore_monomer_md
        if self._drain_evidence is None:
            raise PullContractError("zero-work state was not established")

    def resume_worker(self, _environment: dict[str, str]) -> None:
        # `drain(False)` atomically resumes Backend and every registered Worker.
        return

    def _internal_resume_without_ingress(self) -> None:
        # A crash/restart or an inherited restore can replace a Worker whose
        # process-local default is accepting.  Backend persistent drain alone
        # does not prove that Worker idle, so normalize the exact live
        # instance before taking the canary fence.
        self._drain_evidence = self._internal_drain_without_ingress()
        drained_fence = self.lifecycle._capture_runtime_recovery_fence(
            self.pull_controller,
            self.runtime_descriptor,
            resumed=False,
        )
        verification = {
            "health": "ok",
            "mode": "contract-0012-ingress-isolated-canary",
            "recovery_fence": drained_fence,
            "verified_at": legacy.utc_now(),
        }
        verification = self._persist_runtime_recovery_verification(verification)
        if self.lifecycle._capture_runtime_recovery_fence(
            self.pull_controller,
            self.runtime_descriptor,
            resumed=False,
        ) != self.lifecycle._expected_runtime_recovery_fence(verification):
            raise PullContractError(
                "0012 canary runtime changed after recovery fence commit"
            )
        for _name, socket in self.lifecycle._worker_sockets(self.pull_controller):
            pull.validate_worker_control_evidence(
                self.lifecycle._worker_request(
                    self.pull_controller,
                    socket,
                    method="POST",
                    endpoint="/resume",
                ),
                action="resume",
                require_zero=True,
            )
        if self.lifecycle._capture_runtime_recovery_fence(
            self.pull_controller,
            self.runtime_descriptor,
            resumed=True,
        ) != self.lifecycle._expected_runtime_recovery_fence(verification):
            raise PullContractError(
                "0012 canary Worker changed before Backend admission"
            )
        resumed = self.lifecycle._control_cli(
            self.pull_controller,
            self.runtime_descriptor,
            "resume",
            "--actor",
            "pull-contract-0012",
            "--release-sha",
            self.binding.repository["sha"],
        )
        pull.validate_active_jobs_evidence(
            resumed,
            require_drained=False,
            require_resumed=True,
        )
        if not self.lifecycle.admission_is_open(
            self.pull_controller,
            self.runtime_descriptor,
        ):
            raise PullContractError("0012 canary Backend admission did not open")
        self.lifecycle.verify_runtime_identity(
            self.pull_controller,
            self.runtime_descriptor,
            require_ingress=False,
            allow_active_worker=True,
        )
        if self.lifecycle._capture_runtime_recovery_fence(
            self.pull_controller,
            self.runtime_descriptor,
            resumed=True,
        ) != self.lifecycle._expected_runtime_recovery_fence(verification):
            raise PullContractError(
                "0012 canary runtime changed after Backend admission"
            )

    def _internal_drain_without_ingress(self) -> dict[str, Any]:
        backend_process = self.lifecycle._backend_process_identity(
            self.pull_controller,
            self.runtime_descriptor,
        )
        initial = pull.validate_active_jobs_evidence(
            self.lifecycle._control_cli(
                self.pull_controller,
                self.runtime_descriptor,
                "drain",
                "--actor",
                "pull-contract-0012",
                "--release-sha",
                self.binding.repository["sha"],
                "--reason",
                f"0012 maintenance {self.operation_id}",
            ),
            require_drained=False,
        )
        worker_instances: dict[str, str] = {}
        for name, socket in self.lifecycle._worker_sockets(self.pull_controller):
            evidence = pull.validate_worker_control_evidence(
                self.lifecycle._worker_request(
                    self.pull_controller,
                    socket,
                    method="POST",
                    endpoint="/drain",
                ),
                action="drain",
                require_zero=False,
            )
            worker_instances[name] = evidence["worker_instance_id"]
        settled = self.lifecycle._wait_for_zero_work(
            self.pull_controller,
            self.runtime_descriptor,
            worker_instances,
            backend_process,
        )
        return {
            "persistent_drain": True,
            "initial": initial,
            "settled": settled,
            "worker_instances": worker_instances,
        }

    def run_ingress_isolated_contract_smoke(
        self,
        environment: dict[str, str],
        *,
        release: Path | None = None,
    ) -> dict[str, Any]:
        nginx = self.pull_controller.runner.run(
            self.lifecycle._compose(
                self.pull_controller,
                "ps",
                "--quiet",
                "nginx",
            ),
            cwd=self.root,
            env=environment,
        )
        if str(nginx.stdout).strip():
            raise PullContractError("contract smoke requires nginx to remain stopped")
        try:
            self._internal_resume_without_ingress()
            self.run_contract_gpu_api_smoke(environment, release=release)
            smoke_payload = self.pull_controller._git_show(
                self.binding.repository["sha"],
                "scripts/monomer_md_smoke.py",
            )
            smoke = self.pull_controller.runner.run(
                self.lifecycle._compose(
                    self.pull_controller,
                    "exec",
                    "-T",
                    "backend",
                    "python",
                    "-I",
                    "-",
                    "--base-url",
                    "http://127.0.0.1:8000",
                    "--timeout-seconds",
                    "600",
                    "--expected-byteff2-commit",
                    self.binding.descriptor["release_input"]["asset"]["byteff2_commit"],
                ),
                cwd=self.root,
                env=environment,
                text=False,
                stdin=io.BytesIO(smoke_payload),
                stdout=subprocess.PIPE,
                timeout=900,
            )
            smoke_output = bytes(smoke.stdout).decode("utf-8", "strict").strip()
            if not smoke_output.startswith("monomer MD 300-step smoke completed: "):
                raise PullContractError(
                    "0012 monomer MD 300-step canary returned malformed evidence"
                )
        finally:
            self._drain_evidence = self._internal_drain_without_ingress()
        evidence = {
            "schema_version": 1,
            "status": "passed",
            "ingress_isolated": True,
            "gpu_models": ["conditional_generation", "polytao"],
            "monomer_md": smoke_output[:500],
            "redrain": self._drain_evidence,
            "verified_at": legacy.utc_now(),
        }
        self.contract_canary_evidence = evidence
        return evidence

    def backup_database(self, environment: dict[str, str], from_sha: str) -> None:
        if self.backup_root.exists() or self.backup_root.is_symlink():
            raise PullContractError("0012 backup directory already exists")
        legacy.ensure_durable_directory(self.backup_root)
        self.backup_path = self.backup_root / "database.dump"
        user = environment["NEXPOLY_POSTGRES_USER"]
        database = environment["NEXPOLY_POSTGRES_DB"]
        with self.backup_path.open("xb") as output:
            os.chmod(self.backup_path, 0o600)
            self.run(
                self.compose(
                    self.candidate_dir,
                    "exec",
                    "-T",
                    "lab-postgres",
                    "pg_dump",
                    "-U",
                    user,
                    "-d",
                    database,
                    "-Fc",
                ),
                env=environment,
                stdout=output,
            )
        legacy.fsync_regular_file(self.backup_path)
        with self.backup_path.open("rb") as source:
            self.run(
                self.compose(
                    self.candidate_dir,
                    "exec",
                    "-T",
                    "lab-postgres",
                    "pg_restore",
                    "--list",
                ),
                env=environment,
                stdin=source,
                stdout=subprocess.DEVNULL,
            )
        digest = legacy.sha256_file(self.backup_path)
        sidecar = {
            "schema_version": 1,
            "created_at": legacy.utc_now(),
            "from_sha": from_sha,
            "to_sha": self.sha,
            "file": self.backup_path.name,
            "sha256": digest,
        }
        legacy.atomic_json(self.backup_path.with_suffix(".dump.json"), sidecar)
        legacy.atomic_text(
            self.backup_path.with_suffix(".dump.sha256"),
            f"{digest.removeprefix('sha256:')}  {self.backup_path.name}\n",
        )

    def marker_backup(self, marker: dict[str, Any]) -> Path:
        raw_path = marker.get("database_backup")
        raw_digest = marker.get("database_backup_sha256")
        if not isinstance(raw_path, str) or not isinstance(raw_digest, str):
            raise PullContractError("interrupted 0012 operation has no backup evidence")
        backup = Path(raw_path)
        if (
            not backup.is_absolute()
            or backup.parent != self.backup_root
            or not backup.is_file()
            or backup.is_symlink()
            or legacy.sha256_file(backup) != raw_digest
        ):
            raise PullContractError("interrupted 0012 backup is missing or changed")
        return backup

    def backend_healthcheck(
        self,
        environment: dict[str, str],
        *,
        release: Path | None = None,
    ) -> None:
        source = release or self.root
        self.run(
            self.compose(
                source,
                "exec",
                "-T",
                "backend",
                "python",
                "-m",
                "app.postgres_preflight",
                "--mode",
                "runtime",
                "--strict",
                "--expected-source-sha",
                self.sha,
            ),
            env=environment,
        )
        self.run(
            self.compose(
                source,
                "exec",
                "-T",
                "backend",
                "python",
                "-m",
                "app.gpu_preflight",
                "--mode",
                "ready",
            ),
            env=environment,
        )

    def wait_for_worker_health(
        self,
        _environment: dict[str, str],
        *,
        expected_release: Path,
        previous_instance_id: str | None = None,
    ) -> dict[str, Any]:
        if expected_release.resolve() != self.root.resolve():
            raise PullContractError("0012 Worker health is bound to the live checkout")
        deadline = time.monotonic() + 120
        socket = self.pull_controller.state_dir / "monomer-md-worker-socket/worker.sock"
        while True:
            try:
                health = self.lifecycle._worker_request(
                    self.pull_controller,
                    socket,
                    method="GET",
                    endpoint="/health",
                )
                self.lifecycle._validate_worker_runtime_identity(
                    self.pull_controller,
                    self.runtime_descriptor,
                    health,
                    expected_accepting=True,
                )
                if (
                    previous_instance_id is None
                    or health.get("worker_instance_id") != previous_instance_id
                ):
                    return health
            except (pull.PullDeployError, OSError, subprocess.SubprocessError):
                pass
            if time.monotonic() >= deadline:
                raise PullContractError("timed out waiting for the governed MD Worker")
            time.sleep(2)


class PullContractMaintenance(legacy.PolytaoContractMaintenance):
    """The reviewed 0012 state machine with pull-deployment path bindings."""

    def __init__(
        self,
        production_root: Path,
        runtime_root: Path,
        operation_id: str,
        *,
        apply: bool,
    ) -> None:
        if legacy.OPERATION_ID_RE.fullmatch(operation_id) is None:
            raise PullContractError(
                "contract operation ID must be 8-128 lowercase safe characters"
            )
        self.binding = load_binding(production_root, runtime_root, apply=apply)
        self.controller = PullRuntimeController(
            self.binding,
            operation_id,
            apply=apply,
        )
        self.root = self.controller.root
        self.runtime_root = self.controller.runtime_root
        self.document = self.controller.document
        self.operation_id = operation_id
        self.apply = apply
        self.state_path = self.controller.state_path
        self.marker_path = self.runtime_root / "state/contract-0012-in-progress.json"
        self.journal_path = (
            self.runtime_root / "state/contract-operations" / f"{operation_id}.json"
        )
        self.audit_dir = self.runtime_root / "audit/contracts/0012" / operation_id
        self.verification_owner_path = (
            self.runtime_root
            / "state/contract-verification-databases"
            / f"{operation_id}.json"
        )
        self.contract_record = self._contract_record()
        self._database_recovery_drain_gate: dict[str, Any] | None = None
        self.controller.bind_contract_marker_persistence(
            loader=self._load_runtime_recovery_marker,
            writer=self._write_marker,
        )

    def plan(self) -> dict[str, Any]:
        plan = {
            **super().plan(),
            "runtime_root": str(self.runtime_root),
            "deployment_operation_id": self.binding.current_state["operation_id"],
            "pull_descriptor_sha256": self.binding.descriptor_sha256,
            "source_tree": self.binding.repository["tree"],
            "migration_manifest": str(self.binding.migration_manifest_path),
            "maintenance_adapter_sha256": self.binding.adapter_sha256,
            "governance_core_sha256": self.binding.governance_core_sha256,
            "governance_helper_sha256": self.binding.governance_helper_sha256,
            "active_control": self.binding.active_control,
            "active_control_sha256": self.binding.active_control_sha256,
            "control_manifest_sha256": self.binding.control_manifest_sha256,
            "production_config": self.binding.live_production_config,
            "external_database_audit_helper": (
                self.binding.external_database_audit_helper
            ),
            "legacy_release_bundle": False,
        }
        if self.controller._runtime_identity_evidence is not None:
            plan["runtime_identity"] = self.controller._runtime_identity_evidence
        return plan

    def _load_current_state(
        self,
        *,
        allow_completed_contract: bool = False,
    ) -> dict[str, Any]:
        fresh = load_binding(self.root, self.runtime_root, apply=self.apply)
        if fresh.live_production_config != self.binding.live_production_config:
            raise PullContractError(
                "live production configuration changed during 0012 maintenance"
            )
        state = _legacy_state_projection(fresh.current_state)
        history = state["migrations"]
        if CONTRACT_VERSION in history and not allow_completed_contract:
            raise PullContractError(
                "pull deployment already records 0012 without this maintenance operation"
            )
        legacy_approvals = state.get("approved_contract_migrations")
        if legacy_approvals not in (None, []):
            raise PullContractError(
                "name-only contract approvals are forbidden in pull deployment state"
            )
        legacy.approved_contract_migrations(state)
        return state

    def _load_operation_document(
        self,
        path: Path,
        label: str,
    ) -> dict[str, Any]:
        try:
            return pull.load_private_json(path)
        except pull.PullDeployError as exc:
            raise PullContractError(
                f"unsafe or invalid private {label}: {path}"
            ) from exc

    def _load_runtime_recovery_marker(self) -> dict[str, Any]:
        self._validate_installed_authority()
        return self._load_operation_document(
            self.marker_path,
            "runtime recovery marker",
        )

    def _write_current_state(self, state: dict[str, Any]) -> None:
        legacy.atomic_json(
            self.state_path,
            _pull_state_projection(self.binding, state),
        )

    def _maintenance_authority(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "source_sha": self.binding.repository["sha"],
            "source_tree": self.binding.repository["tree"],
            "pull_descriptor_sha256": self.binding.descriptor_sha256,
            "maintenance_adapter_sha256": self.binding.adapter_sha256,
            "governance_core_sha256": self.binding.governance_core_sha256,
            "governance_helper_sha256": self.binding.governance_helper_sha256,
            "active_control": self.binding.active_control,
            "active_control_sha256": self.binding.active_control_sha256,
            "control_manifest_sha256": self.binding.control_manifest_sha256,
            "production_config": self.binding.live_production_config,
            "external_database_audit_helper": (
                self.binding.external_database_audit_helper
            ),
        }

    def _validate_installed_authority(self) -> None:
        fresh = load_binding(self.root, self.runtime_root, apply=self.apply)
        if (
            fresh.repository != self.binding.repository
            or fresh.descriptor_sha256 != self.binding.descriptor_sha256
            or fresh.active_control != self.binding.active_control
            or fresh.active_control_sha256 != self.binding.active_control_sha256
            or fresh.control_manifest_sha256 != self.binding.control_manifest_sha256
            or fresh.adapter_sha256 != self.binding.adapter_sha256
            or fresh.governance_core_sha256 != self.binding.governance_core_sha256
            or fresh.governance_helper_sha256 != self.binding.governance_helper_sha256
            or fresh.live_production_config != self.binding.live_production_config
            or fresh.external_database_audit_helper
            != self.binding.external_database_audit_helper
        ):
            raise PullContractError(
                "installed 0012 maintenance authority changed during the operation"
            )

    def _write_marker(self, marker: dict[str, Any]) -> None:
        self._validate_installed_authority()
        authority = self._maintenance_authority()
        marker["pull_maintenance_authority"] = authority
        marker["pull_maintenance_authority_sha256"] = pull.canonical_json_digest(
            authority
        )
        canary = self.controller.contract_canary_evidence
        if canary is not None:
            marker["ingress_isolated_canary"] = canary
            marker["ingress_isolated_canary_sha256"] = pull.canonical_json_digest(
                canary
            )
        legacy.atomic_json(self.marker_path, marker)

    def _audit_manifest(self) -> dict[str, Any]:
        self._validate_installed_authority()
        legacy.atomic_json(
            self.audit_dir / "pull-maintenance-authority.json",
            self._maintenance_authority(),
        )
        legacy.atomic_json(
            self.audit_dir / "external-database-audit-helper.json",
            self.binding.external_database_audit_helper,
        )
        return super()._audit_manifest()

    def _validate_audit_manifest(self, manifest: object) -> dict[str, Any]:
        validated = super()._validate_audit_manifest(manifest)
        authority_path = self.audit_dir / "pull-maintenance-authority.json"
        if (
            not authority_path.is_file()
            or authority_path.is_symlink()
            or stat.S_IMODE(authority_path.stat().st_mode) != 0o600
            or self._load_operation_document(
                authority_path,
                "maintenance authority",
            )
            != self._maintenance_authority()
        ):
            raise PullContractError(
                "0012 audit is not bound to its installed maintenance authority"
            )
        helper_path = self.audit_dir / "external-database-audit-helper.json"
        if (
            not helper_path.is_file()
            or helper_path.is_symlink()
            or stat.S_IMODE(helper_path.stat().st_mode) != 0o600
            or self._load_operation_document(
                helper_path,
                "external database audit helper identity",
            )
            != self.binding.external_database_audit_helper
        ):
            raise PullContractError(
                "0012 audit is not bound to its external database audit helper"
            )
        return validated

    def _bind_current_release(self, state: dict[str, Any]) -> Path:
        fresh = load_binding(self.root, self.runtime_root, apply=self.apply)
        supplied_identity = {
            key: state.get(key)
            for key in (
                "schema_version",
                "status",
                "operation_id",
                "source_sha",
                "source_tree",
                "descriptor_sha256",
                "images",
                "asset_manifest_digest",
                "active_monomer_md_slot",
            )
        }
        current_projection = _legacy_state_projection(fresh.current_state)
        current_identity = {
            key: current_projection.get(key) for key in supplied_identity
        }
        if (
            supplied_identity != current_identity
            or fresh.descriptor_sha256 != self.binding.descriptor_sha256
            or fresh.repository != self.binding.repository
            or fresh.adapter_sha256 != self.binding.adapter_sha256
            or fresh.governance_core_sha256 != self.binding.governance_core_sha256
            or fresh.governance_helper_sha256 != self.binding.governance_helper_sha256
            or fresh.active_control != self.binding.active_control
            or fresh.active_control_sha256 != self.binding.active_control_sha256
            or fresh.control_manifest_sha256 != self.binding.control_manifest_sha256
            or fresh.live_production_config != self.binding.live_production_config
            or fresh.external_database_audit_helper
            != self.binding.external_database_audit_helper
        ):
            raise PullContractError(
                "live pull identity changed before 0012 maintenance"
            )
        return self.root

    def _capture_external_database_inventory(
        self,
        environment: dict[str, str],
    ) -> dict[str, Any]:
        """Fence the external audit helper immediately before and after use."""

        before = self.controller.validate_external_database_audit_helper()
        try:
            inventory = super()._capture_external_database_inventory(environment)
        finally:
            after = self.controller.validate_external_database_audit_helper()
        if after != before:
            raise PullContractError(
                "0012 external database audit helper changed while executing"
            )
        return inventory

    @staticmethod
    def _database_recovery_gate_identity(
        marker: dict[str, Any],
    ) -> dict[str, Any]:
        """Bind an in-process DB recovery gate to the current runtime fence.

        The gate is deliberately not persisted.  A new process must always
        re-isolate ingress and prove zero work again.  Within one recovery
        invocation the verification-database cleanup and production restore
        may share the same proof as long as no runtime fence or replacement
        intent changed between those operations.
        """

        start_intent = marker.get("runtime_recovery_start_intent")
        if start_intent is not None and not isinstance(start_intent, dict):
            raise PullContractError("0012 runtime start intent is invalid")
        return {
            "operation_id": marker.get("operation_id"),
            "source_sha": marker.get("source_sha"),
            "status": marker.get("status"),
            "phase": marker.get("phase"),
            "failed_at": marker.get("failed_at"),
            "error": marker.get("error"),
            "database_change_started": marker.get("database_change_started"),
            "runtime_recovery_verification_sha256": marker.get(
                "runtime_recovery_verification_sha256"
            ),
            "runtime_recovery_start_intent_sha256": (
                pull.canonical_json_digest(start_intent)
                if isinstance(start_intent, dict)
                else None
            ),
            "recovery_runtime_state": marker.get("recovery_runtime_state"),
            "recovery_phase_evidence_sha256": marker.get(
                "recovery_phase_evidence_sha256"
            ),
        }

    def _ensure_database_recovery_drain(self) -> None:
        """Fence admission before verification cleanup or database restore."""

        marker = self._load_runtime_recovery_marker()
        identity = self._database_recovery_gate_identity(marker)
        if getattr(self, "_database_recovery_drain_gate", None) == identity:
            return
        self._reestablish_recovery_drain(marker)
        committed = self._load_runtime_recovery_marker()
        identity = self._database_recovery_gate_identity(committed)
        if getattr(self, "_database_recovery_drain_gate", None) != identity:
            raise PullContractError(
                "0012 database recovery drain gate did not commit exactly"
            )

    def _reconcile_owned_verification_database(
        self,
        environment: dict[str, str],
        *,
        recorded_database_inventory: object | None = None,
        initial_inventory: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._ensure_database_recovery_drain()
        return super()._reconcile_owned_verification_database(
            environment,
            recorded_database_inventory=recorded_database_inventory,
            initial_inventory=initial_inventory,
        )

    def _restore_previous_database(
        self,
        environment: dict[str, str],
        previous_state: dict[str, Any],
        *,
        recorded_database_inventory: object | None = None,
    ) -> None:
        self._ensure_database_recovery_drain()
        super()._restore_previous_database(
            environment,
            previous_state,
            recorded_database_inventory=recorded_database_inventory,
        )

    def _reestablish_recovery_drain(self, marker: dict[str, Any]) -> None:
        """Close every admission plane before recovery can inspect or restore DB."""

        self._database_recovery_drain_gate = None
        start_intent = self.controller._runtime_start_intent_authorized(marker)
        expected = None if start_intent else marker.get("runtime_recovery_verification")
        if expected is not None and not isinstance(expected, dict):
            raise PullContractError("0012 recovery runtime verification is invalid")
        pre_drain_intent = self.controller._allows_unfenced_pre_drain_recovery(marker)
        recovery = self.controller.lifecycle.prepare_recovery_runtime(
            self.binding.controller,
            self.controller.runtime_descriptor,
            expected,
            allow_unfenced=pre_drain_intent or start_intent,
        )
        runtime_state = recovery.get("runtime_state")
        if runtime_state not in {"drained", "stopped"}:
            raise PullContractError(
                "0012 recovery did not establish a safe runtime phase"
            )

        if runtime_state == "drained":
            verification = recovery.get("verification")
            if not isinstance(verification, dict):
                raise PullContractError(
                    "0012 recovery redrain lacks instance verification"
                )
            self.controller._persist_runtime_recovery_verification(verification)
            committed = self.controller._contract_marker()
            marker.clear()
            marker.update(committed)
            self.controller._drain_evidence = recovery.get("drain")

        marker["recovery_admission_phase"] = (
            "redrained" if runtime_state == "drained" else "runtime-stopped"
        )
        marker["recovery_runtime_state"] = runtime_state
        marker["recovery_ingress_isolated"] = True
        marker["recovery_phase_evidence"] = recovery
        marker["recovery_phase_evidence_sha256"] = pull.canonical_json_digest(recovery)
        marker["recovery_phase_recorded_at"] = legacy.utc_now()
        self._write_marker(marker)

        # The inherited restore path stops and later replaces Backend/Worker.
        # Persist replacement authority before it can cross that start
        # boundary, so a hard kill after both processes start can re-identify
        # and redrain the sealed new instance rather than comparing it to the
        # deliberately obsolete pre-restore fence.
        if marker.get("database_change_started") is True:
            committed = self.controller._persist_runtime_recovery_start_intent(
                reason="database-restore"
            )
            marker.clear()
            marker.update(committed)

        if runtime_state == "drained" and self.controller.lifecycle.admission_is_open(
            self.binding.controller,
            self.controller.runtime_descriptor,
        ):
            raise PullContractError(
                "0012 recovery could not re-establish persistent drain"
            )
        self._database_recovery_drain_gate = self._database_recovery_gate_identity(
            marker
        )

    def _recover(self, marker: dict[str, Any]) -> dict[str, Any]:
        marker_operation = marker.get("operation_id")
        if marker_operation != self.operation_id:
            raise PullContractError(
                "a different 0012 operation owns the recovery marker"
            )
        self._validate_installed_authority()
        authority = self._maintenance_authority()
        if marker.get("pull_maintenance_authority") != authority or marker.get(
            "pull_maintenance_authority_sha256"
        ) != pull.canonical_json_digest(authority):
            raise PullContractError(
                "0012 recovery marker differs from sealed maintenance authority"
            )
        canary = marker.get("ingress_isolated_canary")
        canary_digest = marker.get("ingress_isolated_canary_sha256")
        if (canary is None) != (canary_digest is None) or (
            canary is not None and pull.canonical_json_digest(canary) != canary_digest
        ):
            raise PullContractError("0012 recovery canary evidence is invalid")
        # This must precede the inherited inventory and restore gates.  A hard
        # kill can bypass the canary's ``finally`` after Backend/Worker resume,
        # or lose the response from redrain.  Recovery never touches database
        # state until ingress is isolated and every control plane again proves
        # zero work on the same fenced runtime.
        self._reestablish_recovery_drain(marker)
        self.controller.lifecycle.cleanup_contract_restore_container(
            self.binding.controller,
            self.operation_id,
        )
        return super()._recover(marker)

    def _archive_legacy_table(
        self,
        environment: dict[str, str],
        previous_state: dict[str, Any],
        database_inventory: dict[str, Any],
    ) -> dict[str, Any]:
        evidence = super()._archive_legacy_table(
            environment,
            previous_state,
            database_inventory,
        )
        backup = self.controller.backup_path
        if backup is None:
            raise PullContractError("0012 PostgreSQL 16 restore has no full backup")
        restore_descriptor = json.loads(json.dumps(self.binding.descriptor))
        restore_descriptor["operation_id"] = self.operation_id
        backup_digest = legacy.sha256_file(backup)
        restore = self.controller.lifecycle.verify_contract_postgres16_restore(
            self.binding.controller,
            restore_descriptor,
            backup,
            backup_digest,
            evidence,
        )
        if (
            not isinstance(restore, dict)
            or set(restore)
            != {
                "schema_version",
                "restored",
                "postgres_major",
                "postgres_version_num",
                "image",
                "dump_sha256",
                "ledger",
                "archive",
                "operation_id",
                "verified_at",
            }
            or restore.get("schema_version") != 2
            or restore.get("restored") is not True
            or restore.get("postgres_major") != 16
            or restore.get("image") != pull.POSTGRES16_IMAGE
            or restore.get("dump_sha256") != backup_digest
            or restore.get("archive") != evidence
            or restore.get("operation_id") != self.operation_id
        ):
            raise PullContractError(
                "0012 isolated PostgreSQL 16 restore evidence is invalid"
            )
        legacy.atomic_json(
            self.audit_dir / "isolated-postgres16-restore.json",
            restore,
        )
        return evidence

    def _success_journal(
        self,
        marker: dict[str, Any],
        approval: dict[str, Any],
        audit_manifest: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        journal = super()._success_journal(
            marker, approval, audit_manifest, verification
        )
        canary = marker.get("ingress_isolated_canary")
        canary_digest = marker.get("ingress_isolated_canary_sha256")
        if (
            not isinstance(canary, dict)
            or canary.get("status") != "passed"
            or canary.get("ingress_isolated") is not True
            or pull.canonical_json_digest(canary) != canary_digest
        ):
            raise PullContractError(
                "0012 success requires sealed ingress-isolated canary evidence"
            )
        journal["ingress_isolated_canary"] = canary
        journal["ingress_isolated_canary_sha256"] = canary_digest
        return journal

    def _validate_success_journal(
        self,
        journal: object,
        approval: dict[str, Any],
    ) -> dict[str, Any]:
        expected_fields = {
            "schema_version",
            "status",
            "operation_id",
            "source_sha",
            "approval",
            "completed_at",
            "database_backup",
            "database_backup_sha256",
            "audit_manifest",
            "audit_manifest_sha256",
            "verification",
            "ingress_isolated_canary",
            "ingress_isolated_canary_sha256",
        }
        if not isinstance(journal, dict) or set(journal) != expected_fields:
            raise PullContractError(
                "existing 0012 success journal has an invalid shape"
            )
        if (
            journal.get("schema_version") != 1
            or journal.get("status") != "success"
            or journal.get("operation_id") != self.operation_id
            or journal.get("source_sha") != self.document["source_sha"]
            or journal.get("approval") != approval
            or journal.get("completed_at") != approval.get("approved_at")
            or journal.get("verification") != {"schema_version": 1, "verified": True}
            or not isinstance(journal.get("ingress_isolated_canary"), dict)
            or journal["ingress_isolated_canary"].get("status") != "passed"
            or journal["ingress_isolated_canary"].get("ingress_isolated") is not True
            or pull.canonical_json_digest(journal["ingress_isolated_canary"])
            != journal.get("ingress_isolated_canary_sha256")
        ):
            raise PullContractError(
                "existing 0012 success journal has an invalid identity"
            )
        backup = journal.get("database_backup")
        backup_digest = journal.get("database_backup_sha256")
        if (
            not isinstance(backup, str)
            or not isinstance(backup_digest, str)
            or legacy.DIGEST_RE.fullmatch(backup_digest) is None
        ):
            raise PullContractError(
                "existing 0012 success journal has invalid backup evidence"
            )
        backup_path = Path(backup)
        if (
            not backup_path.is_absolute()
            or backup_path.parent != self.controller.backup_root
            or not backup_path.is_file()
            or backup_path.is_symlink()
            or legacy.sha256_file(backup_path) != backup_digest
        ):
            raise PullContractError(
                "existing 0012 backup differs from external runtime"
            )
        audit_manifest = self._validate_audit_manifest(journal.get("audit_manifest"))
        audit_path = self.audit_dir / "AUDIT-MANIFEST.json"
        audit_digest = journal.get("audit_manifest_sha256")
        if (
            not isinstance(audit_digest, str)
            or legacy.DIGEST_RE.fullmatch(audit_digest) is None
            or not audit_path.is_file()
            or audit_path.is_symlink()
            or stat.S_IMODE(audit_path.stat().st_mode) != 0o600
            or legacy.sha256_file(audit_path) != audit_digest
            or self._load_operation_document(audit_path, "audit manifest")
            != audit_manifest
        ):
            raise PullContractError("existing 0012 audit evidence differs from disk")
        return dict(journal)

    def run(self) -> dict[str, Any]:
        self.controller.ensure_root()
        try:
            pull._control_runtime.load_production_0005_alias_gate(
                self.runtime_root, require_completed=True
            )
        except Exception as exc:
            raise PullContractError(
                "completed production 0005 alias reconciliation is required before 0012"
            ) from exc
        if (
            self.binding.controller.marker_path.exists()
            or self.binding.controller.marker_path.is_symlink()
        ):
            raise PullContractError(
                "interrupted code deployment must be recovered before 0012"
            )
        if self.apply:
            super().run()
            return load_binding(
                self.root,
                self.runtime_root,
                apply=True,
            ).current_state
        environment = self.controller.environment()
        self._bind_current_release(self.binding.current_state)
        self.controller.validate_current_runtime(environment)
        allow_contract = any(
            record.get("version") == CONTRACT_VERSION
            for record in self.binding.current_state["migrations"]
            if isinstance(record, dict)
        )
        inventory = self._pre_destructive_database_gate(
            environment,
            allow_contract=allow_contract,
        )
        return {
            **self.plan(),
            "database_inventory": inventory,
            "mutation_performed": False,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "apply"):
        command = commands.add_parser(name)
        command.add_argument("--operation-id", required=True)
        command.add_argument("--production-root", default=str(PRODUCTION_ROOT))
        command.add_argument("--runtime-root", default=str(RUNTIME_ROOT))
        if name == "apply":
            command.add_argument("--apply", action="store_true")
            command.add_argument("--confirm-production-root")
            command.add_argument("--confirm-runtime-root")
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    production_root = Path(args.production_root).resolve()
    runtime_root = Path(args.runtime_root).resolve()
    apply = bool(args.command == "apply" and args.apply)
    if apply:
        try:
            allow_test = pull.test_root_mode(
                production_root=production_root,
                runtime_root=runtime_root,
            )
        except pull.PullDeployError as exc:
            print(f"pull-contract-0012: error: {exc}", file=sys.stderr)
            return 2
        if not allow_test and (
            production_root != PRODUCTION_ROOT
            or runtime_root != RUNTIME_ROOT
            or args.confirm_production_root != str(PRODUCTION_ROOT)
            or args.confirm_runtime_root != str(RUNTIME_ROOT)
        ):
            print(
                "pull-contract-0012: error: --apply requires the exact production/runtime roots and confirmations",
                file=sys.stderr,
            )
            return 2
        if allow_test and (
            args.confirm_production_root != str(production_root)
            or args.confirm_runtime_root != str(runtime_root)
        ):
            print(
                "pull-contract-0012: error: test apply confirmations must match their roots",
                file=sys.stderr,
            )
            return 2
    try:
        maintenance = PullContractMaintenance(
            production_root,
            runtime_root,
            args.operation_id,
            apply=apply,
        )
        result = maintenance.run()
    except (
        PullContractError,
        pull.PullDeployError,
        legacy.ReleaseError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
    ) as exc:
        print(f"pull-contract-0012: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

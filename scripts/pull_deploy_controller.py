#!/usr/bin/env python3
"""Governed, commit-pinned production deployment from the live Git checkout.

The controller executes from a content-addressed control release outside the
checkout.  ``runtime/bin`` contains only an immutable selector and four stable
Python wrappers;
source fetch, candidate preparation and image pulls happen before the
maintenance window, while ``apply`` consumes sealed evidence using the target
release's controller.

The installed launcher fixes the production and runtime roots. ``plan`` is
read-only; ``prepare``, ``apply`` and ``rollback`` are explicit mutating
commands. The implementation has no bundle or per-SHA source-release
dependency.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import configparser
import contextlib
import ctypes
import datetime as dt
import errno
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, BinaryIO, Callable, Iterable, Mapping, Protocol
import urllib.error
import urllib.parse
import urllib.request


def _validated_executable_sibling(name: str) -> Path:
    """Validate a sibling before importing any of its Python payload."""

    controller = Path(__file__).absolute()
    parent = controller.parent
    path = parent / name
    try:
        controller_metadata = controller.lstat()
        parent_metadata = parent.lstat()
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"required controller sibling is missing: {name}") from exc
    installed = (
        controller
        == Path("/data/lzq/gith/nexpoly-runtime/bin/pull_deploy_controller.py")
        or stat.S_IMODE(controller_metadata.st_mode) == 0o700
    )
    expected_mode = 0o700 if installed else None
    if (
        not stat.S_ISREG(controller_metadata.st_mode)
        or controller.is_symlink()
        or controller_metadata.st_uid != os.geteuid()
        or controller_metadata.st_mode & 0o022
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent.is_symlink()
        or parent_metadata.st_uid != os.geteuid()
        or parent_metadata.st_mode & 0o022
        or (installed and stat.S_IMODE(parent_metadata.st_mode) != 0o700)
        or not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
        or (
            expected_mode is not None
            and stat.S_IMODE(metadata.st_mode) != expected_mode
        )
    ):
        raise RuntimeError(f"required controller sibling is unsafe: {name}")
    return path


def _load_worker_slot_runtime() -> Any:
    """Load the installed sibling even when Python isolated mode is active."""

    module_name = "nexpoly_pull_deploy_worker_slot_runtime"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    path = _validated_executable_sibling("worker_slot_runtime.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the installed Worker slot runtime")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _load_governance_core() -> Any:
    """Load the installed checksum-bound contract tuple validator."""

    module_name = "nexpoly_pull_deploy_governance_core"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    path = _validated_executable_sibling("release_controller.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PullDeployError("cannot load the installed migration governance core")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _load_bridge_core() -> Any:
    """Load the immutable authority/target bridge validator."""

    module_name = "nexpoly_pull_deploy_bridge_core"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    path = _validated_executable_sibling("bridge_deploy_core.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PullDeployError("cannot load the installed bridge deployment core")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _load_site_helper_contracts() -> Any:
    """Load immutable validators for site-specific helper JSON."""

    module_name = "nexpoly_pull_deploy_site_helper_contracts"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    path = _validated_executable_sibling("site_helper_contracts.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PullDeployError("cannot load installed site-helper contracts")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _load_git_source_trust() -> Any:
    """Load the immutable production Git interpretation policy."""

    return _load_exact_sibling_module(
        "nexpoly_pull_deploy_git_source_trust",
        "git_source_trust.py",
    )


def _load_control_runtime() -> Any:
    """Load the immutable stdlib-only control release validator."""

    module_name = "nexpoly_pull_deploy_control_runtime"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    source_sibling = Path(__file__).absolute().parent / "control_runtime_selector.py"
    path = (
        source_sibling
        if source_sibling.exists()
        else Path("/data/lzq/gith/nexpoly-runtime/bin/control_runtime_selector.py")
    )
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError("immutable control runtime is missing") from exc
    production_selector = path == Path(
        "/data/lzq/gith/nexpoly-runtime/bin/control_runtime_selector.py"
    )
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
        or (production_selector and stat.S_IMODE(metadata.st_mode) != 0o700)
    ):
        raise RuntimeError("immutable control runtime is unsafe")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load immutable control runtime")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _load_exact_sibling_module(module_name: str, filename: str) -> Any:
    """Load one manifest-sealed sibling without ambient import paths."""

    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    path = _validated_executable_sibling(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load installed controller sibling: {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _load_legacy_takeover_evidence() -> Any:
    return _load_exact_sibling_module(
        "nexpoly_pull_deploy_legacy_takeover_evidence",
        "legacy_takeover_evidence.py",
    )


def _load_prefetch_evidence(
    *,
    asset_release_contract: Any,
    bridge_core: Any,
    worker_slot_runtime: Any,
) -> Any:
    """Load the prefetch validator with exact sibling dependencies.

    ``maintenance_prefetch.py`` is also a standalone pre-bootstrap command,
    so it imports its three local dependencies by their ordinary names.
    During governed execution, temporarily bind those names to the exact
    manifest-sealed sibling modules and restore the caller's module table
    after loading.
    """

    module_name = "nexpoly_pull_deploy_maintenance_prefetch"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    bootstrap = _load_exact_sibling_module(
        "nexpoly_pull_deploy_bootstrap_source",
        "bootstrap_pull_deploy.py",
    )
    aliases = {
        "asset_release_contract": asset_release_contract,
        "bootstrap_pull_deploy": bootstrap,
        "bridge_deploy_core": bridge_core,
        "worker_slot_runtime": worker_slot_runtime,
    }
    prior = {name: sys.modules.get(name) for name in aliases}
    path = _validated_executable_sibling("maintenance_prefetch.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load installed maintenance prefetch validator")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        sys.modules.update(aliases)
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    finally:
        for name, value in prior.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
    return module


_worker_slot_runtime = _load_worker_slot_runtime()
_control_runtime = _load_control_runtime()
_bridge_core = _load_bridge_core()
_site_helper_contracts = _load_site_helper_contracts()
_git_source_trust = _load_git_source_trust()
_legacy_takeover_evidence = _load_legacy_takeover_evidence()
_asset_release_contract = _load_exact_sibling_module(
    "nexpoly_pull_deploy_asset_release_contract",
    "asset_release_contract.py",
)
_prefetch_evidence = _load_prefetch_evidence(
    asset_release_contract=_asset_release_contract,
    bridge_core=_bridge_core,
    worker_slot_runtime=_worker_slot_runtime,
)
WORKER_SLOTS = _worker_slot_runtime.SLOTS
WorkerSlotError = _worker_slot_runtime.WorkerSlotError
worker_record_digest = _worker_slot_runtime.canonical_json_digest
worker_directory_inventory_digest = _worker_slot_runtime.directory_inventory_digest
shared_validate_active_record = _worker_slot_runtime.validate_active_record
shared_validate_slot_record = _worker_slot_runtime.validate_slot_record
shared_inspect_base_python_identity = _worker_slot_runtime.inspect_base_python_identity
ACTIVE_SLOT_SCHEMA_VERSION = _worker_slot_runtime.ACTIVE_RECORD_SCHEMA_VERSION
SLOT_RECORD_SCHEMA_VERSION = _worker_slot_runtime.SLOT_RECORD_SCHEMA_VERSION


PRODUCTION_ROOT = Path("/data/lzq/gith/nexpoly")
RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
REPOSITORY_SSH_URL = "git@github.com:lzq390/ZhijuPoly.git"
REPOSITORY_HTTPS_URL = "https://github.com/lzq390/ZhijuPoly.git"
REPOSITORY_API_ROOT = "https://api.github.com/repos/lzq390/ZhijuPoly"
BACKEND_TAG_ROOT = "ghcr.io/lzq390/nexpoly-backend"
WEB_TAG_ROOT = "ghcr.io/lzq390/nexpoly-web"
POSTGRES16_IMAGE = "postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
MONOMER_MD_UNIT_NAME = "nexpoly-monomer-md-worker.service"
MONOMER_MD_UNIT_SOURCE = "ops/systemd/nexpoly-monomer-md-worker.service"
MUTABLE_DATA_AUDIT_HELPER = "deployment-mutable-data-audit"
MUTABLE_DATA_SERVICE_CONFIG = "mutable-data-audit.pg_service.conf"
MUTABLE_DATA_PGPASS = "mutable-data-audit.pgpass"
MUTABLE_DATA_SERVICE = "nexpoly-mutable-audit"
MUTABLE_DATA_HOST = "127.0.0.1"
MUTABLE_DATA_PORT = 55432
EXTERNAL_DATABASE_AUDIT_HELPER = "nexpoly-postgres-media-evidence"
EXTERNAL_DATABASE_MEDIA_AUTHORITY_RULES = (
    "postgres-media-authority-rules.json"
)
EXTERNAL_DATABASE_MEDIA_REGISTRY = "postgres-media-registry.json"
EXTERNAL_DATABASE_AUDIT_ROLE_SQL = (
    "postgres-media-audit-role.sql.example"
)
CONTRACT_0012_EXTERNAL_AUDIT_COMMAND = (
    "NEXPOLY_CONTRACT_0012_EXTERNAL_DATABASE_AUDIT_COMMAND"
)
CONTRACT_0012_MEDIA_AUTHORITY_RULES_DIGEST = (
    "NEXPOLY_CONTRACT_0012_MEDIA_AUTHORITY_RULES_SHA256"
)
CONTRACT_0012_EXTERNAL_AUDIT_USERS = {
    "nexpoly_dev": "NEXPOLY_CONTRACT_0012_DEV_AUDIT_USER",
    "nexpoly_md_health_opt": "NEXPOLY_CONTRACT_0012_MD_HEALTH_AUDIT_USER",
}
MUTABLE_DATA_DATABASE = "nexpoly"
MUTABLE_DATA_USER = "nexpoly_mutable_audit"
MUTABLE_DATA_BUSINESS_TABLES = tuple(
    f"{schema}.{table}"
    for schema, table in (
        _site_helper_contracts.BUSINESS_MUTABLE_TABLES
        + _site_helper_contracts.POST_0013_BUSINESS_MUTABLE_TABLES
    )
)
MUTABLE_DATA_GOVERNED_CONTROLS = tuple(
    f"{schema}.{table}"
    for schema, table in _site_helper_contracts.GOVERNED_CONTROL_TABLES
)
MUTABLE_DATA_STATIC_TABLES = tuple(
    f"{schema}.{table}"
    for schema, table in _site_helper_contracts.STATIC_IMPORT_TABLES
)
MUTABLE_DATA_EXCEPTION = ".".join(
    _site_helper_contracts.CONTRACT_0012_EXCEPTION_TABLE
)
MUTABLE_DATA_SEQUENCES = tuple(
    f"{schema}.{sequence}"
    for schema, sequence, _owned_by in _site_helper_contracts.DATA_SEQUENCES
)
BRIDGE_MUTABLE_MD_SCHEMA_SHA256_BEFORE = (
    "sha256:bff1998505bf95250587a0915020132b6d98f698cec03ec29120314e2bfcb9e6"
)
BRIDGE_MUTABLE_MD_SCHEMA_SHA256_AFTER = (
    "sha256:68a195d960d78fe4a0d8788302bfb6bb52ee2f6bd6de8a5aa3c2e60325b8e389"
)
BRIDGE_MUTABLE_DEPLOYMENT_CONTROL_SCHEMA_SHA256 = (
    "sha256:d706ec201b268578d0de50a7d93e78f163b1e3d34750440a3cf65457f8f5b52e"
)
BRIDGE_MUTABLE_ANALYTICS_SCHEMA_SHA256 = (
    "sha256:2ba80f557a7080d2992a735f76fc552ef796d21544ae18d2d4db4798bd858707"
)
DEPLOY_USER_HOME = Path("/home/devuser")
SOURCE_URL = "https://github.com/lzq390/ZhijuPoly"
ASSET_RELEASES_ROOT = Path("/data/lzq/nexpoly-assets/releases")
SCHEMA_V2_ASSET_MANIFEST_DIGEST = (
    "sha256:e5088b7954f7ee8f6cc4e45af36761fdc44d2fc374643441fe07283475de06c8"
)
SCHEMA_V2_PREDECESSOR_ASSET_MANIFEST_DIGEST = (
    "sha256:ad19a4f1cb954b3ee6999b7157c798fd887ecd3fd7ae12e40ac20a97637575e2"
)
SCHEMA_V2_UNCHANGED_ASSET_TREE_DIGESTS = {
    "backend-data": (
        "sha256:1e8dc53143d0676753805ba7a4bf167431e59d92d227ea3aff39e679e43402e1"
    ),
    "database": (
        "sha256:e6bf224836664723124bc7201d14afbdb6dc13cebd289df8b6f86e7a0be0bdcd"
    ),
    "model": (
        "sha256:40e88b7d9d5103ab5db4cd911219dfe37c2ac62319a10824c69c0b36d9556f25"
    ),
}
STABLE_HELPER_FILES = (
    "control_runtime_selector.py",
    "nexpoly-postgres-media-evidence",
    "nexpoly-production-readiness",
    "nexpoly-pull-contract-0012",
    "nexpoly-pull-deploy",
    "nexpoly-reconcile-production-0005-polytao-alias",
)
CONTROL_SOURCE_PATHS = {
    "control_runtime_selector.py": "scripts/control_runtime_selector.py",
    "nexpoly-postgres-media-evidence": (
        "scripts/nexpoly-postgres-media-evidence"
    ),
    "nexpoly-production-readiness": "scripts/nexpoly-production-readiness",
    "nexpoly-pull-contract-0012": "scripts/nexpoly-pull-contract-0012",
    "nexpoly-pull-deploy": "scripts/nexpoly-pull-deploy",
    "nexpoly-reconcile-production-0005-polytao-alias": (
        "scripts/nexpoly-reconcile-production-0005-polytao-alias"
    ),
}
CONTROL_SOURCE_MANIFEST = "scripts/control-release.json"
CONTROLLER_SCHEMA_VERSION = 1
DESCRIPTOR_SCHEMA_VERSION = 2
BRIDGE_DESCRIPTOR_SCHEMA_VERSION = 3
BRIDGE_RECOVERY_CAPSULE_FILES = (
    "bridge_deploy_core.py",
    "bridge_recovery_capsule.py",
    "legacy_takeover_evidence.py",
)
BRIDGE_RECOVERY_CAPSULE_ROOT_RELATIVE = Path(
    "legacy-takeover/runtime/bridge-recovery-capsules"
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
OPERATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
DATASET_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
SLOT_NAMES = tuple(sorted(WORKER_SLOTS))
MAX_GITHUB_RESPONSE_BYTES = 2 * 1024 * 1024
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
DRAIN_TIMEOUT_SECONDS = 1800
DRAIN_POLL_SECONDS = 2
ACTIVE_JOB_FIELDS_V1 = frozenset(
    {
        "monomer_md",
        "polytao",
        "online_knowledge",
        "conditional_generation",
        "reverse_design",
        "gpu_inference",
        "gpu_waiting",
        "inflight_api_writes",
    }
)
ACTIVE_JOB_FIELDS_V2 = ACTIVE_JOB_FIELDS_V1 | {"monomer_dft"}
PERSISTENT_JOB_FIELDS_V1 = frozenset({"monomer_md", "online_knowledge"})
PERSISTENT_JOB_FIELDS_V2 = PERSISTENT_JOB_FIELDS_V1 | {"monomer_dft"}
FORBIDDEN_IN_TREE_RUNTIME_PATHS = (
    ".env",
    ".env.ai",
    ".env.monomer-md-worker",
    "backups",
    "model",
    "backend/data",
    "ops/backups",
    "ops/incoming",
    "ops/logs",
)

DESCRIPTOR_FIELDS = {
    "schema_version",
    "operation_id",
    "controller",
    "repository",
    "ci",
    "images",
    "release_input",
    "migrations",
    "compose",
    "production_config",
    "postgres_restore_image",
    "mutable_data",
    "monomer_md",
    "previous_deployment",
    "previous_deployment_sha256",
    "prepared_at",
}
BRIDGE_DESCRIPTOR_FIELDS = DESCRIPTOR_FIELDS | {
    "bridge",
    "legacy_takeover",
    "prefetch",
    "external_database_audit",
}
ALIAS_BRIDGE_AUTHORITY_FIELDS = {
    "schema_version",
    "operation_id",
    "descriptor",
    "ready",
    "authority",
    "target",
    "repository_previous",
    "policy",
    "token",
    "takeover",
    "prefetch",
    "external_database_audit_sha256",
    "identity_sha256",
}
READY_FIELDS = {
    "schema_version",
    "status",
    "operation_id",
    "source_sha",
    "descriptor_sha256",
    "executor_control",
    "executor_control_sha256",
    "slot_record_sha256",
    "prepared_at",
}
PREPARE_OWNER_FIELDS = {
    "schema_version",
    "operation_id",
    "target_sha",
    "controller_sha256",
    "created_at",
}
PREPARE_ABORT_FIELDS = {
    "schema_version",
    "operation_id",
    "status",
    "phase",
    "prepare_owner",
    "prepare_owner_sha256",
    "target_sha",
    "target_tree",
    "control_handoff_sha256",
    "control_handoff_schema_version",
    "executor_control_sha256",
    "operation_inventory_sha256",
    "descriptor_sha256",
    "prepare_staging",
    "wheel_staging",
    "owned_slots",
    "prepared_ref",
    "current_state_sha256",
    "active_control_sha256",
    "active_slot_sha256",
    "active_slot",
    "bridge_token_sha256",
    "bridge_token_operation_id",
    "bridge_token_status",
    "archive_path",
    "archive_inventory_sha256",
    "created_at",
    "completed_at",
}
PREPARE_ABORT_PHASES = {
    "intent",
    "slot-cleanup-intent",
    "slots-cleaned",
    "operation-archive-intent",
    "completed",
}
SLOT_FIELDS = {
    "schema_version",
    "component",
    "status",
    "slot",
    "source_sha",
    "source_tree",
    "worker_lock_sha256",
    "requirements_sha256",
    "wheel_cache_key",
    "wheel_inventory_sha256",
    "venv_prefix",
    "venv_inventory_sha256",
    "base_python_configured_path",
    "base_python_identity_sha256",
    "prepared_operation_id",
    "prepared_at",
}
ACTIVE_SLOT_FIELDS = {
    "schema_version",
    "component",
    "slot",
    "source_sha",
    "source_tree",
    "worker_lock_sha256",
    "slot_record_sha256",
    "operation_id",
    "activated_at",
}
ASSET_RELEASE_FIELDS = {
    "root",
    "manifest_sha256",
    "schema_version",
    "byteff2_commit",
    "inventory_sha256",
}
CURRENT_STATE_FIELDS = {
    "schema_version",
    "status",
    "operation_id",
    "source_sha",
    "source_tree",
    "previous_release",
    "descriptor_sha256",
    "images",
    "asset_manifest_digest",
    "asset_identity",
    "byteff2_commit",
    "migrations",
    "approved_contracts",
    "migration_epoch_barrier",
    "schema_compatibility_floor",
    "last_contract_operation",
    "migration_compatibility",
    "active_monomer_md_slot",
    "monomer_md_worker_env",
    "monomer_md_systemd_unit",
    "control_helpers",
    "active_control",
    "production_config",
    "database_backup",
    "mutable_data_audit",
    "deployed_at",
}
CURRENT_STATE_OPTIONAL_FIELDS = {
    "contract_mutable_data_audit",
    "final_mutable_data_audit",
    "external_database_audit",
    "external_database_transition_chain",
    "contract_external_database_audit",
    "final_external_database_audit",
    "rollback_provenance",
}
ROLLBACK_PROVENANCE_FIELDS = {
    "schema_version",
    "kind",
    "rollback_operation_id",
    "from_operation_id",
    "from_source_sha",
    "from_source_tree",
    "from_descriptor_sha256",
    "from_state_sha256",
    "from_terminal_audit_sha256",
    "to_operation_id",
    "to_source_sha",
    "to_source_tree",
    "to_descriptor_sha256",
    "sealed_previous_state_sha256",
    "retained_ledger_sha256",
    "final_mutable_data_audit_sha256",
    "final_external_database_audit_sha256",
    "created_at",
}
MIGRATION_COMPATIBILITY_FIELDS = {
    "schema_version",
    "policy_id",
    "target_manifest_sha256",
    "authority_manifest_sha256",
    "code_manifest_sha256",
    "ledger_manifest_sha256",
    "ledger_state",
    "accepted_migration_ledgers",
}
PRODUCTION_CONFIG_FIELDS = {
    "deploy_env_sha256",
    "app_env_sha256",
    "git_deploy_key_sha256",
    "known_hosts_sha256",
    "github_api_token_sha256",
    "docker_config_sha256",
    "bootstrap_quiesce_sha256",
    "bootstrap_status_sha256",
    "bootstrap_resume_unchanged_sha256",
    "bootstrap_rollback_sha256",
    "bootstrap_active_jobs_probe_sha256",
    "bootstrap_legacy_runtime_status_sha256",
    "bootstrap_legacy_runtime_resume_unchanged_sha256",
    "bootstrap_legacy_runtime_restore_sha256",
    "deployment_mutable_data_audit_sha256",
    "mutable_data_audit_pg_service_sha256",
    "mutable_data_audit_pgpass_sha256",
}
OPERATION_STATE_FIELDS = {
    "schema_version",
    "operation_id",
    "descriptor_sha256",
    "outcome",
    "recorded_at",
}
TERMINAL_OPERATION_OUTCOMES = {
    "deployed",
    "failed",
    "rolled-back",
}
STOP_INTENT_PHASES = {
    "runtime-stop-started",
    "runtime-stopped",
    "asset-switch-started",
    "asset-switched",
    "source-switch-started",
    "source-switched",
    "worker-unit-install-started",
    "worker-unit-installed",
    "migrations-started",
    "migrations-complete",
    "slot-switch-started",
    "slot-switched",
    "control-switch-started",
    "control-switched",
    "runtime-start-started",
    "runtime-started",
    "verifying",
    "verified",
    "state-commit-started",
    "state-committed",
    "admission-resumed",
    "database-restore-started",
    "database-restored",
}
DEPLOY_MARKER_PHASES = {
    "prepared",
    "drain-started",
    "drained",
    "backup-started",
    "backup-verified",
    *STOP_INTENT_PHASES,
    "failed",
}
ROLLBACK_MARKER_PHASES = {
    "explicit-rollback-started",
    "explicit-rollback-drained",
    "explicit-rollback-stop-started",
    "explicit-rollback-runtime-stopped",
    "explicit-rollback-source-restored",
    "explicit-rollback-slot-restored",
    "explicit-rollback-unit-restored",
    "explicit-rollback-asset-restored",
    "explicit-rollback-control-restored",
    "explicit-rollback-state-commit-started",
    "explicit-rollback-state-committed",
    "explicit-rollback-admission-resumed",
    "explicit-rollback-recovered",
    "explicit-rollback-complete",
}
MARKER_BASE_FIELDS = {
    "schema_version",
    "action",
    "operation_id",
    "source_sha",
    "descriptor_sha256",
    "executor_control",
    "executor_control_sha256",
    "phase",
    "started_at",
    "updated_at",
    "runtime_stopped",
    "source_switched",
    "slot_switched",
    "control_switched",
    "unit_switched",
    "asset_switched",
    "database_change_started",
}
MARKER_OPTIONAL_FIELDS = {
    "drain",
    "database_backup",
    "applied_migrations",
    "migration_history",
    "active_slot",
    "active_control",
    "verification",
    "candidate_state",
    "candidate_state_sha256",
    "error",
    "failed_at",
    "failed_phase",
    "forward_recovery_error",
    "rollback",
    "rollback_error",
    "reconciled_at",
    "database_restore_started",
    "database_restored",
    "database_restore",
    "rollback_current_state_sha256",
    "rollback_source_terminal_audit_sha256",
    "rollback_attempt_id",
    "rollback_backup",
    "rollback_backup_operation_id",
    "rollback_candidate_state",
    "rollback_candidate_state_sha256",
    "pre_stop_abort",
    "runtime_start_intent",
    "postgres_runtime_fence",
    "mutable_data_before",
    "mutable_data_after",
    "mutable_data_restored",
    "takeover_pre_stopped_fence_sha256",
    "takeover_restore_started",
    "takeover_restored_terminal_sha256",
    "bridge_recovery_capsule",
    "alias_external_database_audit",
    "bridge_external_database_audit",
    "final_external_database_audit",
    "current_state_precondition_sha256",
}

POSTGRES_RUNTIME_FENCE_FIELDS = {
    "schema_version",
    "container_id",
    "image_id",
    "configured_image",
    "data_volume",
    "host_endpoint",
    "system_identifier",
    "captured_at",
}


class PullDeployError(RuntimeError):
    """A fail-closed deployment validation or operation error."""


class OfflineBridgeRunner:
    """Reject network-capable commands during the post-stop bridge gate.

    The first F -> B deployment has already stopped every legacy source
    reader.  From that point onward all Git objects, OCI images, wheels and
    policy evidence must come from the sealed prefetch record.  Keeping this
    guard at the runner boundary makes an accidental future call to GitHub,
    SSH, ``docker pull`` or an online package installer fail closed.
    """

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    @staticmethod
    def _is_local_bundle_fetch(arguments: list[str]) -> bool:
        try:
            index = arguments.index("fetch")
        except ValueError:
            return False
        tail = arguments[index + 1 :]
        if any(
            value.startswith("--upload-pack")
            or value in {"--all", "--multiple"}
            for value in tail
        ):
            return False
        candidates = [
            value
            for value in tail
            if value
            and not value.startswith("-")
            and not value.startswith("+")
        ]
        if len(candidates) != 1:
            return False
        bundle = Path(candidates[0])
        return bool(
            bundle.is_absolute()
            and bundle.suffix == ".bundle"
            and bundle.is_file()
            and not bundle.is_symlink()
        )

    @classmethod
    def _validate_command(cls, command: object) -> None:
        if (
            not isinstance(command, (list, tuple))
            or not command
            or any(not isinstance(value, (str, os.PathLike)) for value in command)
        ):
            raise PullDeployError(
                "offline bridge revalidation received an unsafe command"
            )
        arguments = [os.fspath(value) for value in command]
        executable = Path(arguments[0]).name
        if executable == "git":
            prohibited = {
                "ls-remote",
                "pull",
                "push",
                "clone",
                "submodule",
                "archive",
            }
            if prohibited.intersection(arguments):
                raise PullDeployError(
                    "offline bridge revalidation forbids Git network access"
                )
            if "fetch" in arguments and not cls._is_local_bundle_fetch(
                arguments
            ):
                raise PullDeployError(
                    "offline bridge revalidation only permits the sealed local bundle"
                )
        if executable in {"ssh", "scp", "sftp", "rsync"}:
            raise PullDeployError(
                "offline bridge revalidation forbids remote shell access"
            )
        if executable in {"docker", "podman"} and {
            "pull",
            "push",
            "login",
            "logout",
            "build",
            "buildx",
        }.intersection(arguments[1:]):
            raise PullDeployError(
                "offline bridge revalidation forbids registry or build access"
            )
        if "pip" in arguments:
            if "download" in arguments or (
                "install" in arguments and "--no-index" not in arguments
            ):
                raise PullDeployError(
                    "offline bridge revalidation forbids package network access"
                )
        if executable == "uv" and not {
            "--offline",
            "cache",
        }.intersection(arguments[1:]):
            raise PullDeployError(
                "offline bridge revalidation forbids uv network access"
            )
        if executable in {"curl", "wget"}:
            raise PullDeployError(
                "offline bridge revalidation forbids HTTP access"
            )
        for value in arguments:
            lowered = value.lower()
            if (
                "https://" in lowered
                or "git@" in lowered
                or (
                    "http://" in lowered
                    and "http://127.0.0.1" not in lowered
                    and "http://localhost" not in lowered
                )
            ):
                raise PullDeployError(
                    "offline bridge revalidation forbids network endpoints"
                )

    def run(self, command: list[str], **kwargs: Any) -> Any:
        self._validate_command(command)
        return self._delegate.run(command, **kwargs)

    def request_json(self, *_args: Any, **_kwargs: Any) -> Any:
        raise PullDeployError(
            "offline bridge revalidation forbids GitHub API access"
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class CommandRunner(Protocol):
    def run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
        text: bool = True,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | int | None = subprocess.PIPE,
        timeout: float | None = None,
        pass_fds: tuple[int, ...] = (),
        umask: int = -1,
    ) -> subprocess.CompletedProcess[Any]: ...

    def request_json(self, url: str, token: str) -> dict[str, Any]: ...


class SystemRunner:
    """Subprocess/network adapter kept injectable for state-machine tests."""

    def run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
        text: bool = True,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | int | None = subprocess.PIPE,
        timeout: float | None = None,
        pass_fds: tuple[int, ...] = (),
        umask: int = -1,
    ) -> subprocess.CompletedProcess[Any]:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=check,
            text=text,
            stdin=stdin,
            stdout=stdout,
            stderr=subprocess.PIPE,
            timeout=timeout,
            pass_fds=pass_fds,
            umask=umask,
        )

    def request_json(self, url: str, token: str) -> dict[str, Any]:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "api.github.com":
            raise PullDeployError("GitHub evidence URL is not the fixed HTTPS API")
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "nexpoly-pull-deploy/1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        opener = urllib.request.build_opener(_NoRedirectHandler())
        try:
            with opener.open(request, timeout=30) as response:
                payload = response.read(MAX_GITHUB_RESPONSE_BYTES + 1)
        except (OSError, urllib.error.URLError) as exc:
            raise PullDeployError("cannot obtain GitHub CI evidence") from exc
        if len(payload) > MAX_GITHUB_RESPONSE_BYTES:
            raise PullDeployError("GitHub CI evidence exceeds the size limit")
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PullDeployError("GitHub CI evidence is not valid JSON") from exc
        if not isinstance(document, dict):
            raise PullDeployError("GitHub CI evidence must be a JSON object")
        return document


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def require_sha(value: object, label: str = "SHA") -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise PullDeployError(f"{label} must be 40 lowercase hexadecimal characters")
    return value


def require_digest(value: object, label: str = "digest") -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise PullDeployError(f"{label} must be a lowercase sha256 digest")
    return value


def require_operation_id(value: str) -> str:
    if OPERATION_ID_RE.fullmatch(value) is None:
        raise PullDeployError("operation ID must be 8-128 lowercase safe characters")
    return value


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def private_regular_file(
    path: Path,
    *,
    mode: int,
    maximum_bytes: int,
) -> tuple[bytes, str]:
    """Read one owner-private regular file without following or racing links."""

    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise PullDeployError(f"private input is unavailable: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_size < 1
            or before.st_size > maximum_bytes
        ):
            raise PullDeployError(f"private input is unsafe: {path}")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(payload) > maximum_bytes
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_mode,
                before.st_uid,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_mode,
                after.st_uid,
            )
        ):
            raise PullDeployError(f"private input changed while reading: {path}")
        return payload, sha256_bytes(payload)
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def pinned_private_regular_file(
    path: Path,
    *,
    mode: int,
    maximum_bytes: int,
) -> Iterable[tuple[int, bytes, str]]:
    """Pin a verified private inode through subprocess exec."""

    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise PullDeployError(f"private input is unavailable: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_nlink != 1
            or before.st_size < 1
            or before.st_size > maximum_bytes
        ):
            raise PullDeployError(f"private input is unsafe: {path}")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, remaining),
            )
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(payload) > maximum_bytes
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
                before.st_mode,
                before.st_uid,
                before.st_nlink,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
                after.st_mode,
                after.st_uid,
                after.st_nlink,
            )
        ):
            raise PullDeployError(
                f"private input changed while reading: {path}"
            )
        os.lseek(descriptor, 0, os.SEEK_SET)
        yield descriptor, payload, sha256_bytes(payload)
    finally:
        os.close(descriptor)


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def canonical_json_digest(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def validate_production_config_evidence(document: object) -> dict[str, str]:
    if not isinstance(document, dict) or set(document) != PRODUCTION_CONFIG_FIELDS:
        raise PullDeployError("production configuration evidence has an invalid shape")
    for key in sorted(PRODUCTION_CONFIG_FIELDS):
        require_digest(document.get(key), f"production configuration {key}")
    return dict(document)


LEGACY_TAKEOVER_BINDING_FIELDS = {
    "schema_version",
    "operation_id",
    "authority_sha",
    "authority_tree",
    "install_manifest_sha256",
    "classification_sha256",
    "runtime_identity_sha256",
    "git_identity",
    "pre_stopped_fence_sha256",
    "control_layout_sha256",
    "checkout_permissions_sha256",
    "applied_record_sha256",
    "binding_sha256",
}
PREFETCH_BINDING_FIELDS = {
    "schema_version",
    "operation_id",
    "ready_path",
    "ready_sha256",
    "identity_sha256",
    "source",
    "source_readiness_sha256",
    "controller_sha256",
    "policy_sha256",
    "docker_config_path",
    "git_bundle_sha256",
    "images_sha256",
    "wheel_caches_sha256",
    "asset_manifest_sha256",
    "asset_inventory_sha256",
    "asset_contract_sha256",
    "asset_builder_proof_sha256",
    "asset_predecessor_inventory_sha256",
    "live_asset_pointer_sha256",
    "recovery_tools_sha256",
    "created_at",
    "binding_sha256",
}
EXTERNAL_DATABASE_AUDIT_BINDING_FIELDS = {
    "schema_version",
    "helper",
    "helper_control",
    "authority_rules",
    "role_sql",
    "role_provisioning",
    "registry",
    "expected_users",
    "snapshot",
    "snapshot_sha256",
    "state_sha256",
    "identity_sha256",
}
EXTERNAL_DATABASE_CONTRACT_PAIR_FIELDS = {
    "schema_version",
    "operation_id",
    "before_identity_sha256",
    "before_state_sha256",
    "after_snapshot",
    "after_snapshot_sha256",
    "after_state_sha256",
    "transition",
    "identity_sha256",
}
EXTERNAL_DATABASE_LEDGER_TRANSITION_FIELDS = {
    "schema_version",
    "kind",
    "operation_id",
    "descriptor_sha256",
    "before_identity_sha256",
    "before_state_sha256",
    "after_binding",
    "transition",
    "identity_sha256",
}
EXTERNAL_DATABASE_TRANSITION_REFERENCE_FIELDS = {
    "path",
    "sha256",
    "identity_sha256",
    "before_state_sha256",
    "after_state_sha256",
    "descriptor_sha256",
    "operation_id",
    "kind",
}
EXTERNAL_DATABASE_TRANSITION_CHAIN_FIELDS = {
    "schema_version",
    "alias",
    "bridge",
    "active_identity_sha256",
    "active_state_sha256",
    "identity_sha256",
}


def validate_legacy_takeover_binding(document: object) -> dict[str, Any]:
    if (
        not isinstance(document, dict)
        or set(document) != LEGACY_TAKEOVER_BINDING_FIELDS
        or document.get("schema_version") != 1
        or not isinstance(document.get("operation_id"), str)
        or not document["operation_id"].startswith("takeover-")
    ):
        raise PullDeployError("legacy takeover binding has an invalid shape")
    require_sha(document.get("authority_sha"), "takeover authority SHA")
    require_sha(document.get("authority_tree"), "takeover authority tree")
    for name in (
        "install_manifest_sha256",
        "classification_sha256",
        "runtime_identity_sha256",
        "pre_stopped_fence_sha256",
        "control_layout_sha256",
        "checkout_permissions_sha256",
        "applied_record_sha256",
        "binding_sha256",
    ):
        require_digest(document.get(name), f"takeover {name}")
    git_identity = document.get("git_identity")
    if (
        not isinstance(git_identity, dict)
        or set(git_identity)
        != {"branch", "head_sha", "head_tree", "local_main_sha"}
        or git_identity.get("branch") != "refs/heads/main"
        or git_identity.get("head_sha") != git_identity.get("local_main_sha")
    ):
        raise PullDeployError("legacy takeover Git binding is invalid")
    for name in ("head_sha", "head_tree", "local_main_sha"):
        require_sha(git_identity.get(name), f"takeover Git {name}")
    identity = {
        key: value
        for key, value in document.items()
        if key != "binding_sha256"
    }
    if document["binding_sha256"] != canonical_json_digest(identity):
        raise PullDeployError("legacy takeover binding digest differs")
    return dict(document)


def validate_prefetch_binding(document: object) -> dict[str, Any]:
    if (
        not isinstance(document, dict)
        or set(document) != PREFETCH_BINDING_FIELDS
        or document.get("schema_version") != 2
        or not isinstance(document.get("operation_id"), str)
        or not document["operation_id"].startswith("prefetch-")
        or not isinstance(document.get("ready_path"), str)
        or not Path(document["ready_path"]).is_absolute()
        or not isinstance(document.get("docker_config_path"), str)
        or not Path(document["docker_config_path"]).is_absolute()
        or not isinstance(document.get("created_at"), str)
        or not document["created_at"]
    ):
        raise PullDeployError("maintenance prefetch binding has an invalid shape")
    source = document.get("source")
    if not isinstance(source, dict) or set(source) != {"authority", "target"}:
        raise PullDeployError("maintenance prefetch source binding is invalid")
    for role in ("authority", "target"):
        identity = source.get(role)
        if not isinstance(identity, dict) or set(identity) != {"sha", "tree"}:
            raise PullDeployError("maintenance prefetch Git binding is invalid")
        require_sha(identity.get("sha"), f"prefetch {role} SHA")
        require_sha(identity.get("tree"), f"prefetch {role} tree")
    for name in PREFETCH_BINDING_FIELDS - {
        "schema_version",
        "operation_id",
        "ready_path",
        "source",
        "docker_config_path",
        "created_at",
    }:
        require_digest(document.get(name), f"prefetch {name}")
    identity = {
        key: value
        for key, value in document.items()
        if key != "binding_sha256"
    }
    if document["binding_sha256"] != canonical_json_digest(identity):
        raise PullDeployError("maintenance prefetch binding digest differs")
    return dict(document)


def external_database_audit_state(document: object) -> dict[str, Any]:
    """Return the freshness-free semantic identity of one media audit.

    ``evidence_sha256`` self-seals the complete media record and therefore
    necessarily changes when ``audited_at`` changes.  The full binding keeps
    both fields; the semantic state intentionally removes both so an
    independently fresh observation can prove the same database/media state.
    """

    if not isinstance(document, dict):
        raise PullDeployError("external database audit snapshot is invalid")
    stable = json.loads(json.dumps(document))
    registry = stable.get("media_registry")
    if not isinstance(registry, dict):
        raise PullDeployError("external database audit registry is invalid")
    # Discovery proves that one invocation inspected the whole configured
    # host boundary.  Its raw Docker/backup inventory also includes unrelated
    # containers, volumes and transient scan ordering, so it is retained in
    # the full snapshot seal but excluded from the cross-invocation database
    # endpoint.  Registered/discovered media IDs and every media record remain
    # in the semantic state; adding or changing PostgreSQL media still fails.
    for field in (
        "captured_at",
        "discovery_state_sha256_before",
        "discovery_state_sha256_after",
        "docker_inventory_sha256",
        "backup_inventory_sha256",
        "scanned_volume_names",
        "scanned_bind_sources",
        "scanned_container_ids",
    ):
        registry.pop(field, None)
    media = stable.get("media")
    if not isinstance(media, list):
        raise PullDeployError("external database media inventory is invalid")
    stable["media"] = [
        _external_media_semantic_record(record)
        for record in media
    ]
    return stable


def _external_database_audit_timestamp(value: object, label: str) -> dt.datetime:
    if not isinstance(value, str):
        raise PullDeployError(f"{label} timestamp is invalid")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PullDeployError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise PullDeployError(f"{label} timestamp is not UTC")
    return parsed


def validate_fresh_external_database_audit(
    document: dict[str, Any],
    *,
    started_at: dt.datetime,
    completed_at: dt.datetime,
) -> None:
    """Require every audit timestamp to originate in this capture invocation."""

    lower = started_at - dt.timedelta(seconds=5)
    upper = completed_at + dt.timedelta(seconds=5)
    timestamps = [
        (
            document.get("media_registry", {}).get("captured_at"),
            "external media registry",
        ),
        *[
            (
                record.get("audit", {}).get("audited_at"),
                f"external medium {record.get('media_id', '<unknown>')}",
            )
            for record in document.get("media", [])
            if isinstance(record, dict)
        ],
    ]
    for raw, label in timestamps:
        observed = _external_database_audit_timestamp(raw, label)
        if observed < lower or observed > upper:
            raise PullDeployError(
                f"{label} evidence was not captured by this invocation"
            )


def external_database_role_provisioning(
    snapshot: Mapping[str, Any],
    *,
    role_sql_sha256: str,
) -> dict[str, Any]:
    """Seal per-database proof of the reviewed NOLOGIN role contract."""

    role_sql_sha256 = require_digest(
        role_sql_sha256,
        "external database audit-role SQL",
    )
    role_fields = (
        "current_user",
        "transaction_read_only",
        "role_superuser",
        "role_create_db",
        "role_create_role",
        "role_replication",
        "role_bypass_rls",
        "role_inherit",
        "role_can_login",
        "role_memberships",
        "role_incoming_memberships",
        "role_settings",
        "role_owned_objects",
        "role_direct_acl",
        "role_default_acl",
        "role_effective_persistent_write",
    )
    media = snapshot.get("media")
    if not isinstance(media, list):
        raise PullDeployError(
            "external database role provisioning lacks media"
        )
    databases: list[dict[str, Any]] = []
    for medium in media:
        if (
            not isinstance(medium, dict)
            or medium.get("record_type") != "nexpoly-db"
            or medium.get("audit", {}).get("method")
            not in {"live-read-only", "live-read-only-adjacent"}
        ):
            continue
        raw_databases = medium.get("databases")
        if not isinstance(raw_databases, list):
            raise PullDeployError(
                "online external medium lacks database provisioning evidence"
            )
        for record in raw_databases:
            audit = (
                record.get("audit")
                if isinstance(record, dict)
                else None
            )
            if (
                not isinstance(record, dict)
                or record.get("audit_state") != "complete"
                or not isinstance(audit, dict)
                or any(field not in audit for field in role_fields)
                or record.get("audit_role") != audit.get("current_user")
                or not isinstance(
                    audit.get("database_identity"), dict
                )
            ):
                raise PullDeployError(
                    "online database role provisioning evidence is incomplete"
                )
            role_evidence = {
                "database_identity": audit["database_identity"],
                **{field: audit[field] for field in role_fields},
            }
            databases.append(
                {
                    "media_id": medium["media_id"],
                    "database": record["name"],
                    "database_oid": record["oid"],
                    "audit_role": record["audit_role"],
                    "phase": (
                        "post-provisioning-read-only-verification"
                    ),
                    "role_evidence_sha256": canonical_json_digest(
                        role_evidence
                    ),
                }
            )
    databases.sort(
        key=lambda value: (
            value["media_id"],
            value["database"],
            value["database_oid"],
        )
    )
    if not databases:
        raise PullDeployError(
            "external database audit lacks online role provisioning proof"
        )
    unsealed = {
        "schema_version": 1,
        "phase": "verified-before-external-audit-publication",
        "role_sql_sha256": role_sql_sha256,
        "databases": databases,
    }
    return {
        **unsealed,
        "evidence_sha256": canonical_json_digest(unsealed),
    }


def validate_external_database_audit_binding(
    document: object,
    *,
    expected_policy: object | None = None,
) -> dict[str, Any]:
    """Validate the exact fresh snapshot sealed by a bridge descriptor."""

    if (
        not isinstance(document, dict)
        or set(document) != EXTERNAL_DATABASE_AUDIT_BINDING_FIELDS
        or document.get("schema_version") != 2
    ):
        raise PullDeployError(
            "external database audit binding has an invalid shape"
        )
    helper = document.get("helper")
    helper_control = document.get("helper_control")
    authority_rules = document.get("authority_rules")
    role_sql = document.get("role_sql")
    registry = document.get("registry")
    for record, filename, mode, fields, label in (
        (
            helper,
            EXTERNAL_DATABASE_AUDIT_HELPER,
            "0700",
            {"path", "sha256", "mode"},
            "external database audit helper",
        ),
        (
            authority_rules,
            EXTERNAL_DATABASE_MEDIA_AUTHORITY_RULES,
            "0600",
            {"path", "sha256", "mode"},
            "external database media authority rules",
        ),
        (
            registry,
            EXTERNAL_DATABASE_MEDIA_REGISTRY,
            "0600",
            {"path", "sha256", "mode", "authority_rules_sha256"},
            "external database media registry",
        ),
    ):
        if (
            not isinstance(record, dict)
            or set(record) != fields
            or not isinstance(record.get("path"), str)
            or not Path(record["path"]).is_absolute()
            or Path(record["path"]).name != filename
            or record.get("mode") != mode
        ):
            raise PullDeployError(f"{label} binding is invalid")
        require_digest(record.get("sha256"), f"{label} digest")
    if (
        not isinstance(helper_control, dict)
        or set(helper_control)
        != {
            "release_id",
            "source_sha",
            "source_tree",
            "manifest_sha256",
            "launcher_sha256",
            "implementation_sha256",
            "authority_rules_sha256",
            "role_sql_sha256",
        }
        or not isinstance(helper_control.get("release_id"), str)
        or re.fullmatch(
            r"[0-9a-f]{64}",
            helper_control["release_id"],
        )
        is None
    ):
        raise PullDeployError(
            "external database helper control binding is invalid"
        )
    require_sha(
        helper_control.get("source_sha"),
        "external database helper control source SHA",
    )
    require_sha(
        helper_control.get("source_tree"),
        "external database helper control source tree",
    )
    for field in (
        "manifest_sha256",
        "launcher_sha256",
        "implementation_sha256",
        "authority_rules_sha256",
        "role_sql_sha256",
    ):
        require_digest(
            helper_control.get(field),
            f"external database helper control {field}",
        )
    if (
        not isinstance(role_sql, dict)
        or set(role_sql)
        != {
            "path",
            "sha256",
            "mode",
            "control_release_id",
            "source_sha",
            "source_tree",
        }
        or not isinstance(role_sql.get("path"), str)
        or not Path(role_sql["path"]).is_absolute()
        or Path(role_sql["path"]).name
        != EXTERNAL_DATABASE_AUDIT_ROLE_SQL
        or role_sql.get("mode") != "0700"
        or not isinstance(role_sql.get("control_release_id"), str)
        or re.fullmatch(
            r"[0-9a-f]{64}",
            role_sql["control_release_id"],
        )
        is None
    ):
        raise PullDeployError(
            "external database audit-role SQL binding is invalid"
        )
    require_digest(
        role_sql.get("sha256"),
        "external database audit-role SQL digest",
    )
    require_sha(
        role_sql.get("source_sha"),
        "external database audit-role SQL source SHA",
    )
    require_sha(
        role_sql.get("source_tree"),
        "external database audit-role SQL source tree",
    )
    if (
        registry.get("authority_rules_sha256")
        != authority_rules.get("sha256")
        or helper_control.get("authority_rules_sha256")
        != authority_rules.get("sha256")
        or helper_control.get("role_sql_sha256")
        != role_sql.get("sha256")
    ):
        raise PullDeployError(
            "external database media registry belongs to other authority rules"
        )
    expected_users = document.get("expected_users")
    if (
        not isinstance(expected_users, dict)
        or set(expected_users) != set(CONTRACT_0012_EXTERNAL_AUDIT_USERS)
        or any(
            not isinstance(value, str)
            or re.fullmatch(r"[a-z_][a-z0-9_-]{0,62}", value) is None
            for value in expected_users.values()
        )
    ):
        raise PullDeployError("external database audit users are invalid")
    try:
        snapshot = _site_helper_contracts.validate_external_database_audit(
            document.get("snapshot"),
            expected_users=expected_users,
            expected_media_authority_rules_digest=authority_rules["sha256"],
            expected_runtime_registry_digest=registry["sha256"],
        )
    except Exception as exc:
        raise PullDeployError(
            "external database audit snapshot is invalid"
        ) from exc
    expected_provisioning = external_database_role_provisioning(
        snapshot,
        role_sql_sha256=role_sql["sha256"],
    )
    if document.get("role_provisioning") != expected_provisioning:
        raise PullDeployError(
            "external database role provisioning evidence differs"
        )
    snapshot_sha256 = require_digest(
        document.get("snapshot_sha256"),
        "external database audit snapshot",
    )
    state_sha256 = require_digest(
        document.get("state_sha256"),
        "external database audit state",
    )
    if (
        snapshot_sha256 != canonical_json_digest(snapshot)
        or state_sha256
        != canonical_json_digest(external_database_audit_state(snapshot))
    ):
        raise PullDeployError("external database audit snapshot digest differs")
    if expected_policy is not None:
        required = {
            **_bridge_core.EXTERNAL_DATABASE_AUDIT_POLICY,
            "media_authority_rules_sha256": authority_rules["sha256"],
            "audit_role_sql_sha256": role_sql["sha256"],
        }
        if expected_policy != required:
            raise PullDeployError(
                "external database audit differs from F policy"
            )
        if (
            snapshot.get("schema_version")
            != expected_policy["evidence_schema_version"]
            or snapshot["media_registry"].get("schema_version")
            != expected_policy["runtime_registry_schema_version"]
            or snapshot["media_registry"].get(
                "media_authority_rules_sha256"
            )
            != authority_rules["sha256"]
            or snapshot["media_registry"].get(
                "runtime_registry_sha256"
            )
            != registry["sha256"]
        ):
            raise PullDeployError(
                "external database audit schema differs from F policy"
            )
    identity = {
        key: value
        for key, value in document.items()
        if key != "identity_sha256"
    }
    if document.get("identity_sha256") != canonical_json_digest(identity):
        raise PullDeployError("external database audit binding digest differs")
    return {
        **dict(document),
        "snapshot": snapshot,
    }


def _external_database_writable_medium(
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    writable = [
        record
        for record in binding["snapshot"]["media"]
        if record["disposition"] == "writable-target"
    ]
    if len(writable) != 1:
        raise PullDeployError(
            "external database audit lacks one writable production medium"
        )
    record = writable[0]
    if (
        record.get("database") != "nexpoly"
        or record.get("kind") not in {"docker_volume", "container_bind"}
    ):
        raise PullDeployError(
            "external database writable medium is not production PostgreSQL"
        )
    return record


def _external_media_semantic_record(record: Mapping[str, Any]) -> dict[str, Any]:
    stable = json.loads(json.dumps(record))
    audit = stable.get("audit")
    if not isinstance(audit, dict):
        raise PullDeployError("external media audit is invalid")
    audit.pop("audited_at", None)
    audit.pop("evidence_sha256", None)
    if audit.get("method") == "isolated-backup-restore-read-only":
        identity = stable.get("database_identity")
        if not isinstance(identity, dict):
            raise PullDeployError(
                "isolated backup database identity is invalid"
            )
        stable["database_identity"] = {
            "database": identity.get("database"),
            "system_identifier_scope": identity.get(
                "system_identifier_scope"
            ),
        }
        # These identify the newly initialized scratch cluster, not the
        # immutable logical backup.  Source file identity/content and the
        # restored ledger/relations remain fully bound below.
        stable.pop("database_identity_sha256", None)
    return stable


def _external_writable_transition_identity(
    record: Mapping[str, Any],
    *,
    mutable_fields: tuple[str, ...],
) -> dict[str, Any]:
    """Remove only fields a proven writable migration may change.

    Schema-v3 repeats the primary database audit inside the complete cluster
    inventory.  Its strict validator proves that nested audit is identical to
    the top-level primary projection.  Transition comparisons must therefore
    remove the same narrowly allowed fields from both projections; otherwise
    every legitimate ledger update appears to have changed cluster identity.
    """

    stable = json.loads(json.dumps(record))
    for field in mutable_fields:
        stable.pop(field, None)
    databases = stable.get("databases")
    primary_name = stable.get("database")
    if not isinstance(databases, list) or not isinstance(primary_name, str):
        raise PullDeployError(
            "external writable database inventory is invalid"
        )
    primary_count = 0
    for database in databases:
        if not isinstance(database, dict):
            raise PullDeployError(
                "external writable database inventory is invalid"
            )
        if database.get("name") != primary_name:
            continue
        audit = database.get("audit")
        if not isinstance(audit, dict):
            raise PullDeployError(
                "external writable primary audit is invalid"
            )
        primary_count += 1
        for field in mutable_fields:
            audit.pop(field, None)
    if primary_count != 1:
        raise PullDeployError(
            "external writable primary audit is not unique"
        )
    return stable


def _canonical_external_ledger_through(version: str) -> list[dict[str, str]]:
    ledger = [
        {"version": name, "checksum": checksum}
        for name, checksum in _site_helper_contracts.CANONICAL_MIGRATION_LEDGER
        if name <= version
    ]
    if not ledger or ledger[-1]["version"] != version:
        raise PullDeployError(
            "external database transition has an unknown migration boundary"
        )
    return ledger


def _external_database_transition_invariants(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    kind: str,
) -> dict[str, Any]:
    """Prove an exact writable-ledger-only bridge transition."""

    prior = before["snapshot"]
    observed = after["snapshot"]
    prior_state = external_database_audit_state(prior)
    observed_state = external_database_audit_state(observed)
    for field in (
        "schema_version",
        "inventory_complete",
        "writable_target",
        "databases",
        "requires_0014",
    ):
        if observed_state.get(field) != prior_state.get(field):
            raise PullDeployError(
                f"{kind} changed an external database outside production"
            )
    prior_registry = prior_state["media_registry"]
    observed_registry = observed_state["media_registry"]
    if prior_registry != observed_registry:
        raise PullDeployError(f"{kind} changed the external media registry")

    before_media = {
        record["media_id"]: record for record in prior["media"]
    }
    after_media = {
        record["media_id"]: record for record in observed["media"]
    }
    stable_before_media = {
        record["media_id"]: record for record in prior_state["media"]
    }
    stable_after_media = {
        record["media_id"]: record for record in observed_state["media"]
    }
    if set(before_media) != set(after_media):
        raise PullDeployError(f"{kind} changed the external media set")
    writable_before = _external_database_writable_medium(before)
    writable_after = _external_database_writable_medium(after)
    writable_id = writable_before["media_id"]
    if writable_after["media_id"] != writable_id:
        raise PullDeployError(f"{kind} selected another writable medium")

    for media_id in sorted(before_media):
        prior_record = before_media[media_id]
        observed_record = after_media[media_id]
        stable_prior_record = stable_before_media[media_id]
        stable_observed_record = stable_after_media[media_id]
        if media_id != writable_id:
            if stable_prior_record != stable_observed_record:
                raise PullDeployError(
                    f"{kind} changed dormant or read-only external media"
                )
            continue
        mutable_fields = (
            "source_content_sha256",
            "ledger",
            "ledger_sha256",
            "ledger_relation",
            "ledger_analysis",
        )
        if kind == "expand-to-0013":
            mutable_fields = (*mutable_fields, "migration_0013")
        stable_before = _external_writable_transition_identity(
            stable_prior_record,
            mutable_fields=mutable_fields,
        )
        stable_after = _external_writable_transition_identity(
            stable_observed_record,
            mutable_fields=mutable_fields,
        )
        if stable_before != stable_after:
            raise PullDeployError(
                f"{kind} changed writable PostgreSQL identity or "
                "non-ledger inventory"
            )
        if (
            prior_record["ledger_relation"]["schema_sha256"]
            != observed_record["ledger_relation"]["schema_sha256"]
            or prior_record["ledger_relation"]["state"] != "present"
            or observed_record["ledger_relation"]["state"] != "present"
            or prior_record["legacy_relation"]
            != observed_record["legacy_relation"]
        ):
            raise PullDeployError(
                f"{kind} changed an external relation outside its ledger rows"
            )

    through_0008 = _canonical_external_ledger_through(
        "0008_polytao_backend_runtime"
    )
    through_0011 = _canonical_external_ledger_through(
        "0011_monomer_md_demo_steps"
    )
    through_0012 = _canonical_external_ledger_through(
        "0012_drop_polytao_jobs"
    )
    through_0013 = _canonical_external_ledger_through(
        "0013_monomer_dft_jobs"
    )
    before_ledger = writable_before["ledger"]
    after_ledger = writable_after["ledger"]
    alias_row = {
        "version": _site_helper_contracts.LEGACY_0005_ALIAS_VERSION,
        "checksum": _site_helper_contracts.LEGACY_0005_ALIAS_CHECKSUM,
    }
    if kind == "alias-0005-reconciliation":
        expected_before = sorted(
            [*through_0008, alias_row],
            key=lambda record: record["version"],
        )
        if before_ledger != expected_before or after_ledger != through_0008:
            raise PullDeployError(
                "alias transition did not remove only the historical 0005 tuple"
            )
        removed = [alias_row]
        added: list[dict[str, str]] = []
        if (
            writable_before["migration_0013"]
            != {"state": "absent", "checksum": None}
            or writable_after["migration_0013"]
            != {"state": "absent", "checksum": None}
        ):
            raise PullDeployError(
                "alias transition changed the 0013 ledger state"
            )
    elif kind == "bridge-expand-to-0011":
        allowed_before = [
            _canonical_external_ledger_through(version)
            for version in (
                "0008_polytao_backend_runtime",
                "0009_monomer_md_job_leases",
                "0010_deployment_control",
                "0011_monomer_md_demo_steps",
            )
        ]
        if before_ledger not in allowed_before or after_ledger != through_0011:
            raise PullDeployError(
                "bridge expansion did not produce canonical 0011 state"
            )
        removed = []
        added = through_0011[len(before_ledger) :]
        if (
            writable_before["migration_0013"]
            != {"state": "absent", "checksum": None}
            or writable_after["migration_0013"]
            != {"state": "absent", "checksum": None}
        ):
            raise PullDeployError(
                "bridge transition changed the 0013 ledger state"
            )
    elif kind == "expand-to-0013":
        expected_0013 = {
            "state": "canonical",
            "checksum": _site_helper_contracts.CANONICAL_0013_CHECKSUM,
        }
        if (
            before_ledger != through_0012
            or after_ledger != through_0013
            or writable_before["legacy_relation"]["state"] != "absent"
            or writable_after["legacy_relation"]["state"] != "absent"
            or writable_before["migration_0013"]
            != {"state": "absent", "checksum": None}
            or writable_after["migration_0013"] != expected_0013
        ):
            raise PullDeployError(
                "0013 external database expansion is not canonical"
            )
        removed = []
        added = through_0013[len(through_0012) :]
    else:
        raise PullDeployError("external database transition kind is invalid")

    return {
        "kind": kind,
        "writable_media_id": writable_id,
        "before_content_sha256": writable_before[
            "source_content_sha256"
        ],
        "after_content_sha256": writable_after[
            "source_content_sha256"
        ],
        "before_ledger_sha256": writable_before["ledger_sha256"],
        "after_ledger_sha256": writable_after["ledger_sha256"],
        "removed": removed,
        "added": added,
    }


def _build_external_database_ledger_transition(
    before_binding: object,
    after_binding: object,
    *,
    operation_id: str,
    descriptor_sha256: str,
    kind: str,
) -> dict[str, Any]:
    before = validate_external_database_audit_binding(before_binding)
    after = validate_external_database_audit_binding(after_binding)
    operation_id = require_operation_id(operation_id)
    descriptor_sha256 = require_digest(
        descriptor_sha256,
        f"{kind} descriptor",
    )
    for field in (
        "helper",
        "authority_rules",
        "role_sql",
        "registry",
        "expected_users",
    ):
        if after[field] != before[field]:
            raise PullDeployError(
                f"{kind} changed external database audit authority"
            )
    transition = {
        **_external_database_transition_invariants(
            before,
            after,
            kind=kind,
        ),
        "operation_id": operation_id,
    }
    pair: dict[str, Any] = {
        "schema_version": 1,
        "kind": kind,
        "operation_id": operation_id,
        "descriptor_sha256": descriptor_sha256,
        "before_identity_sha256": before["identity_sha256"],
        "before_state_sha256": before["state_sha256"],
        "after_binding": after,
        "transition": transition,
        "identity_sha256": None,
    }
    pair["identity_sha256"] = canonical_json_digest(
        {
            field: value
            for field, value in pair.items()
            if field != "identity_sha256"
        }
    )
    return pair


def _validate_external_database_ledger_transition(
    document: object,
    *,
    before_binding: object,
    kind: str,
) -> dict[str, Any]:
    if (
        not isinstance(document, dict)
        or set(document) != EXTERNAL_DATABASE_LEDGER_TRANSITION_FIELDS
        or document.get("schema_version") != 1
        or document.get("kind") != kind
    ):
        raise PullDeployError(
            f"{kind} external database transition has an invalid shape"
        )
    rebuilt = _build_external_database_ledger_transition(
        before_binding,
        document.get("after_binding"),
        operation_id=str(document.get("operation_id", "")),
        descriptor_sha256=str(document.get("descriptor_sha256", "")),
        kind=kind,
    )
    if rebuilt != document:
        raise PullDeployError(
            f"{kind} external database transition identity differs"
        )
    return rebuilt


def build_external_database_alias_pair(
    before_binding: object,
    after_binding: object,
    *,
    operation_id: str,
    descriptor_sha256: str,
) -> dict[str, Any]:
    return _build_external_database_ledger_transition(
        before_binding,
        after_binding,
        operation_id=operation_id,
        descriptor_sha256=descriptor_sha256,
        kind="alias-0005-reconciliation",
    )


def validate_external_database_alias_pair(
    document: object,
    *,
    before_binding: object,
) -> dict[str, Any]:
    return _validate_external_database_ledger_transition(
        document,
        before_binding=before_binding,
        kind="alias-0005-reconciliation",
    )


def build_external_database_bridge_pair(
    before_binding: object,
    after_binding: object,
    *,
    operation_id: str,
    descriptor_sha256: str,
) -> dict[str, Any]:
    return _build_external_database_ledger_transition(
        before_binding,
        after_binding,
        operation_id=operation_id,
        descriptor_sha256=descriptor_sha256,
        kind="bridge-expand-to-0011",
    )


def validate_external_database_bridge_pair(
    document: object,
    *,
    before_binding: object,
) -> dict[str, Any]:
    return _validate_external_database_ledger_transition(
        document,
        before_binding=before_binding,
        kind="bridge-expand-to-0011",
    )


def build_external_database_final_pair(
    before_binding: object,
    after_binding: object,
    *,
    operation_id: str,
    descriptor_sha256: str,
) -> dict[str, Any]:
    return _build_external_database_ledger_transition(
        before_binding,
        after_binding,
        operation_id=operation_id,
        descriptor_sha256=descriptor_sha256,
        kind="expand-to-0013",
    )


def validate_external_database_final_pair(
    document: object,
    *,
    before_binding: object,
) -> dict[str, Any]:
    return _validate_external_database_ledger_transition(
        document,
        before_binding=before_binding,
        kind="expand-to-0013",
    )


def external_database_transition_reference(
    pair: object,
    *,
    path: Path,
) -> dict[str, Any]:
    if (
        not isinstance(pair, dict)
        or set(pair) != EXTERNAL_DATABASE_LEDGER_TRANSITION_FIELDS
        or not path.is_absolute()
    ):
        raise PullDeployError(
            "external database transition reference input is invalid"
        )
    reference = {
        "path": str(path),
        "sha256": sha256_file(path),
        "identity_sha256": require_digest(
            pair.get("identity_sha256"),
            "external database transition identity",
        ),
        "before_state_sha256": require_digest(
            pair.get("before_state_sha256"),
            "external database transition before state",
        ),
        "after_state_sha256": require_digest(
            pair.get("after_binding", {}).get("state_sha256"),
            "external database transition after state",
        ),
        "descriptor_sha256": require_digest(
            pair.get("descriptor_sha256"),
            "external database transition descriptor",
        ),
        "operation_id": require_operation_id(
            str(pair.get("operation_id", ""))
        ),
        "kind": pair.get("kind"),
    }
    if reference["kind"] not in {
        "alias-0005-reconciliation",
        "bridge-expand-to-0011",
    }:
        raise PullDeployError(
            "external database transition reference kind is invalid"
        )
    return reference


def validate_external_database_transition_reference(
    document: object,
    *,
    expected_kind: str | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(document, dict)
        or set(document) != EXTERNAL_DATABASE_TRANSITION_REFERENCE_FIELDS
        or not isinstance(document.get("path"), str)
        or not Path(document["path"]).is_absolute()
    ):
        raise PullDeployError(
            "external database transition reference is invalid"
        )
    for field in (
        "sha256",
        "identity_sha256",
        "before_state_sha256",
        "after_state_sha256",
        "descriptor_sha256",
    ):
        require_digest(document.get(field), f"transition reference {field}")
    require_operation_id(str(document.get("operation_id", "")))
    if (
        document.get("kind")
        not in {
            "alias-0005-reconciliation",
            "bridge-expand-to-0011",
        }
        or expected_kind is not None
        and document["kind"] != expected_kind
    ):
        raise PullDeployError(
            "external database transition reference kind differs"
        )
    return dict(document)


def build_external_database_transition_chain(
    *,
    alias_reference: object,
    bridge_reference: object,
    active_binding: object,
) -> dict[str, Any]:
    alias = validate_external_database_transition_reference(
        alias_reference,
        expected_kind="alias-0005-reconciliation",
    )
    bridge = validate_external_database_transition_reference(
        bridge_reference,
        expected_kind="bridge-expand-to-0011",
    )
    active = validate_external_database_audit_binding(active_binding)
    if (
        bridge["after_state_sha256"] != active["state_sha256"]
        or alias["after_state_sha256"] != bridge["before_state_sha256"]
        or bridge["identity_sha256"] == alias["identity_sha256"]
    ):
        raise PullDeployError(
            "external database transition chain endpoint differs"
        )
    chain: dict[str, Any] = {
        "schema_version": 1,
        "alias": alias,
        "bridge": bridge,
        "active_identity_sha256": active["identity_sha256"],
        "active_state_sha256": active["state_sha256"],
        "identity_sha256": None,
    }
    chain["identity_sha256"] = canonical_json_digest(
        {
            field: value
            for field, value in chain.items()
            if field != "identity_sha256"
        }
    )
    return chain


def validate_external_database_transition_chain(
    document: object,
    *,
    active_binding: object,
) -> dict[str, Any]:
    if (
        not isinstance(document, dict)
        or set(document) != EXTERNAL_DATABASE_TRANSITION_CHAIN_FIELDS
        or document.get("schema_version") != 1
    ):
        raise PullDeployError(
            "external database transition chain has an invalid shape"
        )
    rebuilt = build_external_database_transition_chain(
        alias_reference=document.get("alias"),
        bridge_reference=document.get("bridge"),
        active_binding=active_binding,
    )
    if rebuilt != document:
        raise PullDeployError(
            "external database transition chain identity differs"
        )
    return rebuilt


def build_external_database_contract_pair(
    before_binding: object,
    after_snapshot: object,
    *,
    operation_id: str,
) -> dict[str, Any]:
    """Prove that 0012 changed only the exact writable production medium."""

    before = validate_external_database_audit_binding(before_binding)
    operation_id = require_operation_id(operation_id)
    expected_users = before["expected_users"]
    authority_rules_sha256 = before["authority_rules"]["sha256"]
    runtime_registry_sha256 = before["registry"]["sha256"]
    try:
        after = _site_helper_contracts.validate_external_database_audit(
            after_snapshot,
            expected_users=expected_users,
            expected_media_authority_rules_digest=(
                authority_rules_sha256
            ),
            expected_runtime_registry_digest=runtime_registry_sha256,
        )
    except Exception as exc:
        raise PullDeployError(
            "post-0012 external database audit is invalid"
        ) from exc
    prior = before["snapshot"]
    prior_state = external_database_audit_state(prior)
    after_state = external_database_audit_state(after)
    for key in (
        "schema_version",
        "inventory_complete",
        "writable_target",
        "databases",
        "requires_0014",
    ):
        if after_state.get(key) != prior_state.get(key):
            raise PullDeployError(
                "0012 changed an external database outside production"
            )
    prior_registry = prior_state["media_registry"]
    after_registry = after_state["media_registry"]
    if prior_registry != after_registry:
        raise PullDeployError("0012 external media registry changed")
    before_media = {
        record["media_id"]: record for record in prior["media"]
    }
    after_media = {
        record["media_id"]: record for record in after["media"]
    }
    stable_before_media = {
        record["media_id"]: record for record in prior_state["media"]
    }
    stable_after_media = {
        record["media_id"]: record for record in after_state["media"]
    }
    if set(before_media) != set(after_media):
        raise PullDeployError("0012 external media set changed")
    writable = [
        media_id
        for media_id, record in before_media.items()
        if record["disposition"] == "writable-target"
    ]
    if len(writable) != 1:
        raise PullDeployError("0012 lacks one writable production medium")
    writable_id = writable[0]
    expected_before_ledger = [
        {"version": version, "checksum": checksum}
        for version, checksum in (
            _site_helper_contracts.CANONICAL_MIGRATION_LEDGER
        )
        if version <= "0011_monomer_md_demo_steps"
    ]
    expected_after_ledger = [
        {"version": version, "checksum": checksum}
        for version, checksum in (
            _site_helper_contracts.CANONICAL_MIGRATION_LEDGER
        )
        if version <= "0012_drop_polytao_jobs"
    ]
    for media_id in sorted(before_media):
        prior_record = before_media[media_id]
        after_record = after_media[media_id]
        if media_id != writable_id:
            if (
                stable_before_media[media_id]
                != stable_after_media[media_id]
            ):
                raise PullDeployError(
                    "0012 changed dormant or read-only external media"
                )
            continue
        mutable_fields = (
            "source_content_sha256",
            "ledger",
            "ledger_sha256",
            "ledger_relation",
            "ledger_analysis",
            "legacy_relation_present",
            "generation_schema",
            "legacy_relation",
            # Dropping generation.polytao_jobs removes its relation ACL, and
            # the idempotent audit-role contract removes the now-unneeded
            # generation schema USAGE grant. Both snapshots have already
            # passed the schema-v5 exact least-privilege validator.
            "role_direct_acl",
        )
        stable_before = _external_writable_transition_identity(
            stable_before_media[media_id],
            mutable_fields=mutable_fields,
        )
        stable_after = _external_writable_transition_identity(
            stable_after_media[media_id],
            mutable_fields=mutable_fields,
        )
        if stable_before != stable_after:
            raise PullDeployError(
                "0012 writable medium identity changed"
            )
        expected_before_analysis, _, _ = (
            _site_helper_contracts._external_media_ledger_v2(
                expected_before_ledger,
                legacy_relation_present=True,
                isolated=False,
            )
        )
        expected_after_analysis, _, _ = (
            _site_helper_contracts._external_media_ledger_v2(
                expected_after_ledger,
                legacy_relation_present=False,
                isolated=False,
            )
        )
        if (
            prior_record["kind"] not in {"docker_volume", "container_bind"}
            or prior_record["database"] != "nexpoly"
            or prior_record["ledger"] != expected_before_ledger
            or after_record["ledger"] != expected_after_ledger
            or prior_record["legacy_relation_present"] is not True
            or after_record["legacy_relation_present"] is not False
            or prior_record["migration_0013"]
            != {"state": "absent", "checksum": None}
            or after_record["migration_0013"]
            != {"state": "absent", "checksum": None}
            or prior_record["ledger_analysis"]
            != expected_before_analysis
            or after_record["ledger_analysis"]
            != expected_after_analysis
            or prior_record["source_content_sha256"]
            == after_record["source_content_sha256"]
            or prior_record["ledger_relation"]["schema_sha256"]
            != after_record["ledger_relation"]["schema_sha256"]
            or prior_record["legacy_relation"]["state"] != "present"
            or after_record["legacy_relation"]
            != {
                "state": "absent",
                "row_count": None,
                "schema_sha256": None,
                "schema_authority": None,
                "content_sha256": None,
            }
            or prior_record["audit"]["method"] != "live-read-only"
            or after_record["audit"]["method"] != "live-read-only"
            or prior_record["audit"]["auditor_sha256"]
            != after_record["audit"]["auditor_sha256"]
            or prior_record["audit"]["postgres_major"]
            != after_record["audit"]["postgres_major"]
        ):
            raise PullDeployError(
                "0012 writable external database transition is invalid"
            )
    transition = {
        "kind": "contract-0012",
        "operation_id": operation_id,
        "writable_media_id": writable_id,
        "before_content_sha256": before_media[writable_id][
            "source_content_sha256"
        ],
        "after_content_sha256": after_media[writable_id][
            "source_content_sha256"
        ],
    }
    pair: dict[str, Any] = {
        "schema_version": 1,
        "operation_id": operation_id,
        "before_identity_sha256": before["identity_sha256"],
        "before_state_sha256": before["state_sha256"],
        "after_snapshot": after,
        "after_snapshot_sha256": canonical_json_digest(after),
        "after_state_sha256": canonical_json_digest(
            external_database_audit_state(after)
        ),
        "transition": transition,
        "identity_sha256": None,
    }
    pair["identity_sha256"] = canonical_json_digest(
        {
            key: value
            for key, value in pair.items()
            if key != "identity_sha256"
        }
    )
    return pair


def validate_external_database_contract_pair(
    document: object,
    *,
    before_binding: object,
) -> dict[str, Any]:
    if (
        not isinstance(document, dict)
        or set(document) != EXTERNAL_DATABASE_CONTRACT_PAIR_FIELDS
        or document.get("schema_version") != 1
    ):
        raise PullDeployError(
            "0012 external database transition has an invalid shape"
        )
    rebuilt = build_external_database_contract_pair(
        before_binding,
        document.get("after_snapshot"),
        operation_id=str(document.get("operation_id", "")),
    )
    if rebuilt != document:
        raise PullDeployError(
            "0012 external database transition identity differs"
        )
    return rebuilt


def external_database_contract_after_binding(
    before_binding: object,
    pair: object,
) -> dict[str, Any]:
    """Materialize the exact post-0012 endpoint from its transition pair."""

    before = validate_external_database_audit_binding(before_binding)
    validated = validate_external_database_contract_pair(
        pair,
        before_binding=before,
    )
    binding: dict[str, Any] = {
        "schema_version": 2,
        "helper": before["helper"],
        "helper_control": before["helper_control"],
        "authority_rules": before["authority_rules"],
        "role_sql": before["role_sql"],
        "role_provisioning": external_database_role_provisioning(
            validated["after_snapshot"],
            role_sql_sha256=before["role_sql"]["sha256"],
        ),
        "registry": before["registry"],
        "expected_users": before["expected_users"],
        "snapshot": validated["after_snapshot"],
        "snapshot_sha256": validated["after_snapshot_sha256"],
        "state_sha256": validated["after_state_sha256"],
        "identity_sha256": None,
    }
    binding["identity_sha256"] = canonical_json_digest(
        {
            field: value
            for field, value in binding.items()
            if field != "identity_sha256"
        }
    )
    return validate_external_database_audit_binding(binding)


def external_database_endpoint(
    base_binding: object,
    *,
    contract_pair: object | None = None,
    final_pair: object | None = None,
) -> dict[str, Any]:
    """Fold the exact external PostgreSQL ledger chain to its live endpoint."""

    endpoint = validate_external_database_audit_binding(base_binding)
    if contract_pair is not None:
        endpoint = external_database_contract_after_binding(
            endpoint,
            contract_pair,
        )
    if final_pair is not None:
        validated_final = validate_external_database_final_pair(
            final_pair,
            before_binding=endpoint,
        )
        endpoint = validated_final["after_binding"]
    return endpoint


def validate_mutable_data_contract(document: object) -> dict[str, Any]:
    fields = {
        "schema_version",
        "helper_path",
        "helper_sha256",
        "dependencies",
        "connection",
        "business_tables",
        "governed_controls",
        "static_tables",
        "migration_exception",
        "migration_exception_archive_evidence",
        "sequences",
        "bridge_projection",
        "evidence_schema_version",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != 6
        or document.get("evidence_schema_version") != 6
        or document.get("business_tables")
        != list(MUTABLE_DATA_BUSINESS_TABLES)
        or document.get("governed_controls")
        != list(MUTABLE_DATA_GOVERNED_CONTROLS)
        or document.get("static_tables")
        != list(MUTABLE_DATA_STATIC_TABLES)
        or document.get("migration_exception") != MUTABLE_DATA_EXCEPTION
        or document.get("migration_exception_archive_evidence")
        != "generation.polytao_jobs:canonical-archive-v2"
        or document.get("sequences") != list(MUTABLE_DATA_SEQUENCES)
        or document.get("bridge_projection")
        != "md.monomer_md_jobs:pre-0009-row-json-v1"
        or not isinstance(document.get("helper_path"), str)
        or not Path(document["helper_path"]).is_absolute()
        or Path(document["helper_path"]).name != MUTABLE_DATA_AUDIT_HELPER
    ):
        raise PullDeployError("mutable-data audit contract has an invalid shape")
    require_digest(document.get("helper_sha256"), "mutable-data helper digest")
    dependencies = document.get("dependencies")
    if not isinstance(dependencies, dict) or set(dependencies) != {
        "pg_service",
        "pgpass",
    }:
        raise PullDeployError("mutable-data helper dependencies are invalid")
    for name, expected_filename in (
        ("pg_service", MUTABLE_DATA_SERVICE_CONFIG),
        ("pgpass", MUTABLE_DATA_PGPASS),
    ):
        record = dependencies.get(name)
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "sha256", "mode"}
            or not isinstance(record.get("path"), str)
            or not Path(record["path"]).is_absolute()
            or Path(record["path"]).name != expected_filename
            or record.get("mode") != "0600"
        ):
            raise PullDeployError("mutable-data helper dependency is invalid")
        require_digest(record.get("sha256"), f"mutable-data {name} digest")
    if document.get("connection") != {
        "service": MUTABLE_DATA_SERVICE,
        "host": MUTABLE_DATA_HOST,
        "port": MUTABLE_DATA_PORT,
        "database": MUTABLE_DATA_DATABASE,
        "user": MUTABLE_DATA_USER,
    }:
        raise PullDeployError("mutable-data connection contract differs")
    return dict(document)


def validate_mutable_data_evidence(document: object) -> dict[str, Any]:
    try:
        validated = _site_helper_contracts.validate_mutable_data_audit(document)
    except Exception as exc:
        raise PullDeployError("mutable-data audit evidence is invalid") from exc
    if [
        f"{record['schema']}.{record['table']}"
        for record in validated["business_tables"]
    ] != list(MUTABLE_DATA_BUSINESS_TABLES):
        raise PullDeployError(
            "mutable-data audit selected unexpected business tables"
        )
    if [
        f"{record['schema']}.{record['table']}"
        for record in validated["static_tables"]
    ] != list(MUTABLE_DATA_STATIC_TABLES):
        raise PullDeployError(
            "mutable-data audit selected unexpected static tables"
        )
    if [
        f"{record['schema']}.{record['sequence']}"
        for record in validated["sequences"]
    ] != list(MUTABLE_DATA_SEQUENCES):
        raise PullDeployError(
            "mutable-data audit selected unexpected sequences"
        )
    return validated


def mutable_data_identity(document: object) -> dict[str, Any]:
    validated = validate_mutable_data_evidence(document)
    return {
        "operation_id": validated["operation_id"],
        "database": validated["database"],
        "database_system_identifier": validated["database_system_identifier"],
        "connection": validated["connection"],
        "postgres_runtime": validated["postgres_runtime"],
        "digest_algorithm": validated["digest_algorithm"],
        "migration_ledger": validated["migration_ledger"],
        "business_tables": validated["business_tables"],
        "governed_controls": validated["governed_controls"],
        "static_tables": validated["static_tables"],
        "migration_exception": validated["migration_exception"],
        "migration_exception_archive_evidence": validated[
            "migration_exception_archive_evidence"
        ],
        "sequences": validated["sequences"],
        "bridge_projection": validated["bridge_projection"],
        "snapshot_sha256": validated["snapshot_sha256"],
    }


def _mutable_table_map(
    records: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        f"{record['schema']}.{record['table']}": record
        for record in records
    }


def _mutable_sequence_map(
    records: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        f"{record['schema']}.{record['sequence']}": record
        for record in records
    }


def _control_transition(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    migration_version: str | None,
    operation_id: str,
) -> dict[str, Any]:
    before_control = before["governed_controls"]["deployment_control"]
    after_control = after["governed_controls"]["deployment_control"]
    before_row = before_control["row"]
    after_row = after_control["row"]
    if (
        before_control == after_control
        and before_control["table"]["state"] == "absent"
        and before_row is None
    ):
        return {
            "mode": "pre-0010-unchanged",
            "release_sha": None,
            "activated_by": None,
            "reason": None,
            "before_content_sha256": None,
            "after_content_sha256": None,
        }
    if not isinstance(before_row, dict) or not isinstance(after_row, dict):
        raise PullDeployError(
            "deployment-control evidence changed across its creation boundary"
        )
    expected_actor = (
        "pull-contract-0012"
        if migration_version == "0012_drop_polytao_jobs"
        else "pull-deploy-controller"
    )
    expected_after_reason = (
        f"0012 maintenance {operation_id}"
        if migration_version == "0012_drop_polytao_jobs"
        else f"post-canary drain {operation_id}"
    )
    expected_before_reasons = (
        {expected_after_reason}
        if migration_version == "0012_drop_polytao_jobs"
        else {
            f"pull deployment {operation_id}",
            f"post-canary drain {operation_id}",
        }
    )
    if (
        before_control == after_control
        and before_row.get("drain_enabled") is True
        and before_row.get("activated_by") == expected_actor
        and before_row.get("reason") in expected_before_reasons
    ):
        return {
            "mode": "unchanged-operation-drain",
            "release_sha": before_row["release_sha"],
            "activated_by": before_row["activated_by"],
            "reason": before_row["reason"],
            "before_content_sha256": before_control["table"][
                "content_sha256"
            ],
            "after_content_sha256": after_control["table"][
                "content_sha256"
            ],
        }
    if (
        before_control["table"]["schema_sha256"]
        != after_control["table"]["schema_sha256"]
        or before_control["table"]["row_count"] != 1
        or after_control["table"]["row_count"] != 1
        or after_row.get("drain_enabled") is not True
        or (
            before_row.get("drain_enabled") is True
            and before_row.get("release_sha") != after_row.get("release_sha")
        )
        or (
            before_row.get("drain_enabled") is True
            and (
                before_row.get("activated_by") != expected_actor
                or before_row.get("reason") not in expected_before_reasons
            )
        )
        or after_row.get("activated_by") != expected_actor
        or after_row.get("reason") != expected_after_reason
    ):
        raise PullDeployError(
            "deployment-control row changed outside the current operation"
        )
    return {
        "mode": "post-canary-redrain",
        "release_sha": after_row["release_sha"],
        "activated_by": expected_actor,
        "reason": expected_after_reason,
        "before_content_sha256": before_control["table"]["content_sha256"],
        "after_content_sha256": after_control["table"]["content_sha256"],
    }


def _derive_mutable_data_transition(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    if before["operation_id"] != after["operation_id"]:
        raise PullDeployError(
            "mutable-data evidence belongs to different operations"
        )
    operation_id = before["operation_id"]
    before_ledger = before["migration_ledger"]
    after_ledger = after["migration_ledger"]
    migration: dict[str, Any] | None = None
    if after_ledger == before_ledger:
        kind = "code-deploy"
    elif (
        len(after_ledger) == len(before_ledger) + 1
        and after_ledger[:-1] == before_ledger
        and after_ledger[-1]["version"]
        in {"0012_drop_polytao_jobs", "0013_monomer_dft_jobs"}
    ):
        migration = dict(after_ledger[-1])
        kind = (
            "contract-0012"
            if migration["version"] == "0012_drop_polytao_jobs"
            else "expand-0013"
        )
    else:
        raise PullDeployError(
            "mutable-data audit observed an unauthorized migration transition"
        )
    migration_version = None if migration is None else migration["version"]

    before_business = _mutable_table_map(before["business_tables"])
    after_business = _mutable_table_map(after["business_tables"])
    dft_relations = set(
        MUTABLE_DATA_BUSINESS_TABLES[
            -len(_site_helper_contracts.POST_0013_BUSINESS_MUTABLE_TABLES) :
        ]
    )
    for relation in MUTABLE_DATA_BUSINESS_TABLES:
        if (
            migration_version == "0013_monomer_dft_jobs"
            and relation in dft_relations
        ):
            created = after_business[relation]
            if (
                before_business[relation]["state"] != "absent"
                or created["state"] != "present"
                or created["row_count"] != 0
            ):
                raise PullDeployError(
                    "0013 did not create one empty DFT business relation"
                )
        elif before_business[relation] != after_business[relation]:
            raise PullDeployError(
                f"mutable business table changed during deployment: {relation}"
            )
    if migration_version == "0013_monomer_dft_jobs":
        try:
            _site_helper_contracts.validate_monomer_dft_0013_creation(
                after
            )
        except Exception as exc:
            raise PullDeployError(
                "0013 DFT creation evidence is not pristine"
            ) from exc

    if before["static_tables"] != after["static_tables"]:
        raise PullDeployError(
            "static import tables changed although dataset import was disabled"
        )
    before_analytics = before["governed_controls"][
        "database_analytics_snapshots"
    ]
    after_analytics = after["governed_controls"][
        "database_analytics_snapshots"
    ]
    if before_analytics != after_analytics:
        raise PullDeployError(
            "database analytics snapshots changed without a release-bound refresh"
        )

    before_exception = before["migration_exception"]
    after_exception = after["migration_exception"]
    exception: dict[str, Any] | None = None
    if migration_version == "0012_drop_polytao_jobs":
        if (
            before_exception["state"] != "present"
            or after_exception
            != {
                "schema": "generation",
                "table": "polytao_jobs",
                "state": "absent",
                "row_count": None,
                "schema_sha256": None,
                "content_sha256": None,
            }
        ):
            raise PullDeployError(
                "0012 PolyTAO exception did not perform its exact sealed drop"
            )
        exception = {
            "relation": MUTABLE_DATA_EXCEPTION,
            "operation_id": operation_id,
            "row_count": before_exception["row_count"],
            "schema_sha256": before_exception["schema_sha256"],
            "content_sha256": before_exception["content_sha256"],
            "archive_evidence": before[
                "migration_exception_archive_evidence"
            ],
        }
    elif before_exception != after_exception:
        raise PullDeployError(
            "PolyTAO business rows changed outside the 0012 contract"
        )
    if before["bridge_projection"] != after["bridge_projection"]:
        raise PullDeployError(
            "MD lease-column projection changed during deployment"
        )

    before_sequences = _mutable_sequence_map(before["sequences"])
    after_sequences = _mutable_sequence_map(after["sequences"])
    dft_sequence = "monomer_dft.jobs_enqueue_sequence_seq"
    for name in MUTABLE_DATA_SEQUENCES:
        if (
            migration_version == "0013_monomer_dft_jobs"
            and name == dft_sequence
        ):
            created = after_sequences[name]
            if (
                before_sequences[name]["state"] != "absent"
                or created["state"] != "present"
                or created["last_value"] != created["start_value"]
                or created["is_called"] is not False
            ):
                raise PullDeployError(
                    "0013 DFT identity sequence is not pristine"
                )
        elif before_sequences[name] != after_sequences[name]:
            raise PullDeployError(
                f"database sequence changed during deployment: {name}"
            )

    control = _control_transition(
        before,
        after,
        migration_version=migration_version,
        operation_id=operation_id,
    )
    return {
        "kind": kind,
        "operation_id": operation_id,
        "migration": migration,
        "control": control,
        "analytics": "unchanged",
        "polytao_exception": exception,
        "dft_relations": (
            sorted(dft_relations)
            if migration_version == "0013_monomer_dft_jobs"
            else []
        ),
    }


def _derive_bridge_mutable_data_transition(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    descriptor_sha256: str,
) -> dict[str, Any]:
    descriptor_sha256 = require_digest(
        descriptor_sha256,
        "bridge mutable-data descriptor",
    )
    if before["operation_id"] != after["operation_id"]:
        raise PullDeployError(
            "bridge mutable-data evidence belongs to different operations"
        )
    operation_id = before["operation_id"]
    expected_before = [
        {"version": version, "checksum": checksum}
        for version, checksum in (
            _site_helper_contracts.CANONICAL_MIGRATION_LEDGER[:8]
        )
    ]
    expected_after = [
        {"version": version, "checksum": checksum}
        for version, checksum in (
            _site_helper_contracts.CANONICAL_MIGRATION_LEDGER[:11]
        )
    ]
    if (
        before["migration_ledger"] != expected_before
        or after["migration_ledger"] != expected_after
    ):
        raise PullDeployError(
            "bridge mutable-data audit did not prove exact 0008 to 0011 expansion"
        )

    before_business = _mutable_table_map(before["business_tables"])
    after_business = _mutable_table_map(after["business_tables"])
    md_relation = "md.monomer_md_jobs"
    for relation in MUTABLE_DATA_BUSINESS_TABLES:
        if relation == md_relation:
            prior = before_business[relation]
            observed = after_business[relation]
            if (
                prior["state"] != "present"
                or observed["state"] != "present"
                or prior["row_count"] != observed["row_count"]
                or prior["schema_sha256"]
                != BRIDGE_MUTABLE_MD_SCHEMA_SHA256_BEFORE
                or observed["schema_sha256"]
                != BRIDGE_MUTABLE_MD_SCHEMA_SHA256_AFTER
            ):
                raise PullDeployError(
                    "bridge MD lease/default migration changed rows or schema unexpectedly"
                )
        elif before_business[relation] != after_business[relation]:
            raise PullDeployError(
                f"mutable business table changed during bridge: {relation}"
            )
    if before["static_tables"] != after["static_tables"]:
        raise PullDeployError(
            "static import tables changed during bridge expansion"
        )
    if before["migration_exception"] != after["migration_exception"]:
        raise PullDeployError(
            "PolyTAO rows changed during bridge expansion"
        )
    if before["sequences"] != after["sequences"]:
        raise PullDeployError(
            "database sequence changed during bridge expansion"
        )

    absent_table = {
        "state": "absent",
        "row_count": None,
        "schema_sha256": None,
        "content_sha256": None,
    }
    before_projection = before["bridge_projection"]
    after_projection = after["bridge_projection"]
    if (
        {
            key: value
            for key, value in before_projection.items()
            if key != "lease_columns"
        }
        != {
            key: value
            for key, value in after_projection.items()
            if key != "lease_columns"
        }
        or before_projection["lease_columns"]
        != {
            "state": "absent",
            "non_null_counts": {
                "worker_instance_id": None,
                "heartbeat_at": None,
                "lease_expires_at": None,
            },
        }
        or after_projection["lease_columns"]
        != {
            "state": "present",
            "non_null_counts": {
                "worker_instance_id": 0,
                "heartbeat_at": 0,
                "lease_expires_at": 0,
            },
        }
    ):
        raise PullDeployError(
            "bridge MD lease columns changed existing business rows"
        )
    before_control = before["governed_controls"]["deployment_control"]
    before_analytics = before["governed_controls"][
        "database_analytics_snapshots"
    ]
    if (
        {key: before_control["table"][key] for key in absent_table}
        != absent_table
        or before_control["row"] is not None
        or {key: before_analytics["table"][key] for key in absent_table}
        != absent_table
        or before_analytics["entries"] != []
    ):
        raise PullDeployError(
            "bridge pre-0010 governed controls are not absent"
        )
    after_control = after["governed_controls"]["deployment_control"]
    after_analytics = after["governed_controls"][
        "database_analytics_snapshots"
    ]
    row = after_control["row"]
    if (
        after_control["table"]["state"] != "present"
        or after_control["table"]["row_count"] != 1
        or after_control["table"]["schema_sha256"]
        != BRIDGE_MUTABLE_DEPLOYMENT_CONTROL_SCHEMA_SHA256
        or not isinstance(row, dict)
        or row.get("control_key") != "production"
        or row.get("drain_enabled") is not True
        or row.get("activated_by") != "pull-deploy-controller"
        or row.get("reason")
        != f"post-canary drain {operation_id}"
        or row.get("activated_at") != row.get("updated_at")
        or after_analytics["table"]["state"] != "present"
        or after_analytics["table"]["row_count"] != 0
        or after_analytics["table"]["schema_sha256"]
        != BRIDGE_MUTABLE_ANALYTICS_SCHEMA_SHA256
        or after_analytics["entries"] != []
    ):
        raise PullDeployError(
            "bridge 0010 governed controls are not pristine"
        )
    return {
        "kind": "bridge-expand-to-0011",
        "operation_id": operation_id,
        "descriptor_sha256": descriptor_sha256,
        "added_migrations": expected_after[len(expected_before) :],
        "md_projection": {
            key: value
            for key, value in before_projection.items()
            if key != "lease_columns"
        },
        "control": {
            "mode": "created-operation-drain",
            "release_sha": row["release_sha"],
            "deployment_content_sha256": after_control["table"][
                "content_sha256"
            ],
            "analytics_content_sha256": after_analytics["table"][
                "content_sha256"
            ],
        },
    }


def build_mutable_data_pair(
    before_document: object,
    after_document: object,
) -> dict[str, Any]:
    before = validate_mutable_data_evidence(before_document)
    after = validate_mutable_data_evidence(after_document)
    return validate_mutable_data_pair(
        {
            "before": before,
            "after": after,
            "identity_sha256": canonical_json_digest(
                mutable_data_identity(before)
            ),
            "transition": _derive_mutable_data_transition(before, after),
        }
    )


def build_bridge_mutable_data_pair(
    before_document: object,
    after_document: object,
    *,
    descriptor_sha256: str,
) -> dict[str, Any]:
    before = validate_mutable_data_evidence(before_document)
    after = validate_mutable_data_evidence(after_document)
    return validate_mutable_data_pair(
        {
            "before": before,
            "after": after,
            "identity_sha256": canonical_json_digest(
                mutable_data_identity(before)
            ),
            "transition": _derive_bridge_mutable_data_transition(
                before,
                after,
                descriptor_sha256=descriptor_sha256,
            ),
        }
    )


def validate_mutable_data_pair(document: object) -> dict[str, Any]:
    if (
        not isinstance(document, dict)
        or set(document)
        != {"before", "after", "identity_sha256", "transition"}
    ):
        raise PullDeployError("mutable-data deployment evidence has an invalid shape")
    before = validate_mutable_data_evidence(document["before"])
    after = validate_mutable_data_evidence(document["after"])
    before_identity = mutable_data_identity(before)
    digest = canonical_json_digest(before_identity)
    if document.get("identity_sha256") != digest:
        raise PullDeployError("mutable-data deployment identity differs")
    raw_transition = document.get("transition")
    if (
        isinstance(raw_transition, dict)
        and raw_transition.get("kind") == "bridge-expand-to-0011"
    ):
        transition = _derive_bridge_mutable_data_transition(
            before,
            after,
            descriptor_sha256=str(
                raw_transition.get("descriptor_sha256", "")
            ),
        )
    else:
        transition = _derive_mutable_data_transition(before, after)
    if document.get("transition") != transition:
        raise PullDeployError("mutable-data transition evidence differs")
    return {
        "before": before,
        "after": after,
        "identity_sha256": digest,
        "transition": transition,
    }


def _split_pgpass_line(payload: bytes) -> tuple[str, str, str, str, str]:
    """Parse one libpq password line without ever returning it in evidence."""

    if (
        not payload
        or len(payload) > 64 * 1024
        or b"\0" in payload
        or b"\r" in payload
        or payload.count(b"\n") != 1
        or not payload.endswith(b"\n")
    ):
        raise PullDeployError("mutable-data pgpass must contain one exact line")
    try:
        line = payload[:-1].decode("utf-8")
    except UnicodeError as exc:
        raise PullDeployError("mutable-data pgpass is not UTF-8") from exc
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for character in line:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ":" and len(fields) < 4:
            fields.append("".join(current))
            current = []
        else:
            current.append(character)
    if escaped:
        raise PullDeployError("mutable-data pgpass has a trailing escape")
    fields.append("".join(current))
    if len(fields) != 5 or not fields[4]:
        raise PullDeployError("mutable-data pgpass has an invalid shape")
    return tuple(fields)  # type: ignore[return-value]


def validate_mutable_data_connection_inputs(
    service_payload: bytes,
    pgpass_payload: bytes,
    *,
    expected_passfile: Path,
) -> dict[str, object]:
    """Reject libpq indirection and bind one exact loopback audit identity."""

    if (
        not service_payload
        or len(service_payload) > 64 * 1024
        or b"\0" in service_payload
        or b"\r" in service_payload
    ):
        raise PullDeployError("mutable-data pg_service input is malformed")
    parser = configparser.RawConfigParser(
        interpolation=None,
        strict=True,
        empty_lines_in_values=False,
    )
    parser.optionxform = str.lower
    try:
        parser.read_string(service_payload.decode("utf-8"))
    except (UnicodeError, configparser.Error) as exc:
        raise PullDeployError("mutable-data pg_service input is malformed") from exc
    expected_options = {
        "host": MUTABLE_DATA_HOST,
        "port": str(MUTABLE_DATA_PORT),
        "dbname": MUTABLE_DATA_DATABASE,
        "user": MUTABLE_DATA_USER,
        "sslmode": "disable",
        "passfile": str(expected_passfile),
    }
    if (
        parser.sections() != [MUTABLE_DATA_SERVICE]
        or dict(parser.items(MUTABLE_DATA_SERVICE, raw=True)) != expected_options
        or parser.defaults()
    ):
        raise PullDeployError(
            "mutable-data pg_service must name one exact loopback audit endpoint"
        )
    host, port, database, user, _secret = _split_pgpass_line(pgpass_payload)
    if (host, port, database, user) != (
        MUTABLE_DATA_HOST,
        str(MUTABLE_DATA_PORT),
        MUTABLE_DATA_DATABASE,
        MUTABLE_DATA_USER,
    ):
        raise PullDeployError(
            "mutable-data pgpass does not match the exact audit endpoint"
        )
    return {
        "service": MUTABLE_DATA_SERVICE,
        "host": MUTABLE_DATA_HOST,
        "port": MUTABLE_DATA_PORT,
        "database": MUTABLE_DATA_DATABASE,
        "user": MUTABLE_DATA_USER,
    }


def validate_postgres_runtime_fence(document: object) -> dict[str, Any]:
    """Validate the immutable identity of the live production PostgreSQL."""

    if (
        not isinstance(document, dict)
        or set(document) != POSTGRES_RUNTIME_FENCE_FIELDS
        or document.get("schema_version") != 1
        or not isinstance(document.get("container_id"), str)
        or re.fullmatch(r"[0-9a-f]{64}", document["container_id"]) is None
        or not isinstance(document.get("image_id"), str)
        or DIGEST_RE.fullmatch(document["image_id"]) is None
        or not isinstance(document.get("configured_image"), str)
        or not document["configured_image"]
        or not isinstance(document.get("system_identifier"), str)
        or re.fullmatch(r"[0-9]{10,30}", document["system_identifier"]) is None
        or not isinstance(document.get("captured_at"), str)
        or not document["captured_at"]
    ):
        raise PullDeployError("PostgreSQL runtime fence has an invalid shape")
    volume = document.get("data_volume")
    if (
        not isinstance(volume, dict)
        or set(volume)
        != {"type", "name", "source", "destination", "driver", "read_write"}
        or volume.get("type") != "volume"
        or not isinstance(volume.get("name"), str)
        or not volume["name"]
        or not isinstance(volume.get("source"), str)
        or not Path(volume["source"]).is_absolute()
        or volume.get("destination") != "/var/lib/postgresql/data"
        or not isinstance(volume.get("driver"), str)
        or not volume["driver"]
        or volume.get("read_write") is not True
    ):
        raise PullDeployError("PostgreSQL data-volume fence is invalid")
    endpoint = document.get("host_endpoint")
    if (
        not isinstance(endpoint, dict)
        or set(endpoint) != {"host", "port", "container_port", "protocol"}
        or endpoint.get("host") != MUTABLE_DATA_HOST
        or endpoint.get("port") != MUTABLE_DATA_PORT
        or endpoint.get("container_port") != 5432
        or endpoint.get("protocol") != "tcp"
    ):
        raise PullDeployError("PostgreSQL host-endpoint fence is invalid")
    return {
        **document,
        "data_volume": dict(volume),
        "host_endpoint": dict(endpoint),
    }


def postgres_runtime_fence_identity(document: object) -> dict[str, Any]:
    fence = validate_postgres_runtime_fence(document)
    return {key: value for key, value in fence.items() if key != "captured_at"}


def validate_image_records(
    images: object, *, source_sha: str
) -> dict[str, dict[str, str]]:
    source_sha = require_sha(source_sha, "image source SHA")
    if not isinstance(images, dict) or set(images) != {"backend", "web"}:
        raise PullDeployError("deployment image identity is invalid")
    result: dict[str, dict[str, str]] = {}
    for role, root in (("backend", BACKEND_TAG_ROOT), ("web", WEB_TAG_ROOT)):
        record = images.get(role)
        expected_fields = {
            "tag",
            "digest_ref",
            "image_id",
            "revision",
            "source",
            "version",
        }
        if not isinstance(record, dict) or set(record) != expected_fields:
            raise PullDeployError(f"deployment {role} image identity is invalid")
        if record.get("tag") != f"{root}:sha-{source_sha}":
            raise PullDeployError(
                f"deployment {role} image tag differs from source SHA"
            )
        digest_ref = record.get("digest_ref")
        if (
            not isinstance(digest_ref, str)
            or not digest_ref.startswith(root + "@")
            or len(digest_ref.split("@", 1)) != 2
        ):
            raise PullDeployError(
                f"deployment {role} image digest reference is invalid"
            )
        require_digest(digest_ref.split("@", 1)[1], f"{role} image digest")
        require_digest(record.get("image_id"), f"{role} image ID")
        if (
            record.get("revision") != source_sha
            or record.get("source") != SOURCE_URL
            or record.get("version") != f"sha-{source_sha}"
        ):
            raise PullDeployError(f"deployment {role} OCI identity is invalid")
        result[role] = dict(record)
    return result


def validate_asset_identity(document: object) -> dict[str, Any]:
    expected_fields = {"pointer_path", "previous", *ASSET_RELEASE_FIELDS}
    if not isinstance(document, dict) or set(document) != expected_fields:
        raise PullDeployError("deployment external asset identity is invalid")
    pointer = document.get("pointer_path")
    root = document.get("root")
    if (
        not isinstance(pointer, str)
        or not Path(pointer).is_absolute()
        or not isinstance(root, str)
        or not Path(root).is_absolute()
    ):
        raise PullDeployError("deployment external asset path is invalid")
    require_digest(document.get("manifest_sha256"), "external asset manifest")
    require_digest(document.get("inventory_sha256"), "external asset inventory")
    require_sha(document.get("byteff2_commit"), "external asset ByteFF2 commit")
    if not isinstance(document.get("schema_version"), int) or isinstance(
        document.get("schema_version"), bool
    ):
        raise PullDeployError("external asset schema version is invalid")
    previous = document.get("previous")
    if previous is not None:
        if not isinstance(previous, dict) or set(previous) != ASSET_RELEASE_FIELDS:
            raise PullDeployError("previous external asset identity is invalid")
        previous_root = previous.get("root")
        if not isinstance(previous_root, str) or not Path(previous_root).is_absolute():
            raise PullDeployError("previous external asset path is invalid")
        require_digest(previous.get("manifest_sha256"), "previous asset manifest")
        require_digest(previous.get("inventory_sha256"), "previous asset inventory")
        require_sha(previous.get("byteff2_commit"), "previous asset ByteFF2 commit")
        if not isinstance(previous.get("schema_version"), int) or isinstance(
            previous.get("schema_version"), bool
        ):
            raise PullDeployError("previous asset schema version is invalid")
    return dict(document)


def validate_release_input(document: object) -> dict[str, Any]:
    """Validate the frozen non-activating schema-v2 asset selection."""

    fields = {
        "schema_version",
        "asset_manifest_digest",
        "predecessor_asset_manifest_digest",
        "changed_asset_trees",
        "datasets_on_asset_change",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != 2
        or document.get("asset_manifest_digest")
        != SCHEMA_V2_ASSET_MANIFEST_DIGEST
        or document.get("predecessor_asset_manifest_digest")
        != SCHEMA_V2_PREDECESSOR_ASSET_MANIFEST_DIGEST
        or document.get("changed_asset_trees") != ["byteff2"]
    ):
        raise PullDeployError("release input is not the frozen schema-v2 asset contract")
    datasets = document.get("datasets_on_asset_change")
    if (
        not isinstance(datasets, list)
        or len(datasets) > 64
        or len(set(datasets)) != len(datasets)
        or any(
            not isinstance(dataset, str)
            or DATASET_RE.fullmatch(dataset) is None
            or dataset in {"all", "none"}
            for dataset in datasets
        )
        or datasets
    ):
        raise PullDeployError(
            "schema-v2 asset contract must not rebuild database datasets"
        )
    return dict(document)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def rename_directory_noreplace(source: Path, target: Path) -> None:
    """Atomically publish one same-parent directory without clobbering.

    Every production host supported by this controller is Linux.  Using
    ``renameat2(RENAME_NOREPLACE)`` is deliberate: a preflight ``exists()``
    followed by ordinary ``rename`` can replace an empty directory created by
    another actor between those two operations.
    """

    if source.parent != target.parent:
        raise PullDeployError("directory publication must remain in one parent")
    ensure_private_directory(source.parent)
    ensure_private_directory(source)
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise PullDeployError("no-clobber directory publication is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,  # AT_FDCWD
        os.fsencode(source),
        -100,
        os.fsencode(target),
        1,  # RENAME_NOREPLACE
    )
    if result == 0:
        fsync_directory(source.parent)
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), str(target))
    raise PullDeployError(
        f"no-clobber directory publication failed: {os.strerror(error)}"
    )


def quarantine_directory_noreplace(source: Path, target: Path) -> None:
    """Atomically quarantine a private directory across two private parents."""

    ensure_private_directory(source.parent)
    ensure_private_directory(target.parent)
    ensure_private_directory(source)
    source_parent = source.parent.lstat()
    target_parent = target.parent.lstat()
    if source_parent.st_dev != target_parent.st_dev:
        raise PullDeployError("private directory quarantine crosses filesystems")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise PullDeployError("no-clobber directory quarantine is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(target),
        1,  # RENAME_NOREPLACE
    )
    if result == 0:
        fsync_directory(source.parent)
        fsync_directory(target.parent)
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), str(target))
    raise PullDeployError(
        f"no-clobber directory quarantine failed: {os.strerror(error)}"
    )


def quarantine_regular_file_noreplace(source: Path, target: Path) -> None:
    """Atomically quarantine one owner-private regular file without clobbering."""

    ensure_private_directory(source.parent)
    ensure_private_directory(target.parent)
    try:
        metadata = source.lstat()
    except OSError as exc:
        raise PullDeployError(
            f"private quarantine source is unavailable: {source}"
        ) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or source.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or source.parent.lstat().st_dev != target.parent.lstat().st_dev
    ):
        raise PullDeployError("private file quarantine source is unsafe")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise PullDeployError("no-clobber file quarantine is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(target),
        1,  # RENAME_NOREPLACE
    )
    if result == 0:
        fsync_directory(source.parent)
        fsync_directory(target.parent)
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), str(target))
    raise PullDeployError(
        f"no-clobber file quarantine failed: {os.strerror(error)}"
    )


def fsync_private_tree(root: Path) -> None:
    """Durably flush an owner-controlled tree without following links."""

    ensure_private_directory(root)

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise PullDeployError(
                f"cannot enumerate private staging tree: {directory}"
            ) from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise PullDeployError(
                    f"cannot inspect private staging entry: {path}"
                ) from exc
            if metadata.st_uid != os.geteuid() or stat.S_ISLNK(metadata.st_mode):
                raise PullDeployError(f"private staging entry is unsafe: {path}")
            if stat.S_ISDIR(metadata.st_mode):
                visit(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise PullDeployError(
                    f"private staging tree contains a special file: {path}"
                )
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(path, flags)
                try:
                    opened = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or opened.st_dev != metadata.st_dev
                        or opened.st_ino != metadata.st_ino
                    ):
                        raise PullDeployError(
                            f"private staging entry changed while flushing: {path}"
                        )
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError as exc:
                raise PullDeployError(
                    f"cannot flush private staging entry: {path}"
                ) from exc
        fsync_directory(directory)

    visit(root)


def ensure_private_directory(path: Path, *, create: bool = False) -> None:
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PullDeployError(f"private directory is missing: {path}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise PullDeployError(f"directory must be deploy-user-owned mode 0700: {path}")


def load_private_json(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        payload = path.read_bytes()
    except OSError as exc:
        raise PullDeployError(f"private JSON file is missing: {path}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > MAX_GITHUB_RESPONSE_BYTES
    ):
        raise PullDeployError(f"file must be deploy-user-owned mode 0600: {path}")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PullDeployError(f"private JSON file is invalid: {path}") from exc
    if not isinstance(document, dict):
        raise PullDeployError(f"private JSON file must contain an object: {path}")
    return document


def atomic_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    ensure_private_directory(path.parent)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def atomic_json(path: Path, document: dict[str, Any]) -> None:
    atomic_bytes(path, canonical_json_bytes(document) + b"\n")


def atomic_control_file(path: Path, payload: bytes, *, mode: int) -> None:
    """Atomically replace one file in an owner-controlled non-private config dir."""

    parent = path.parent
    metadata = parent.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or parent.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
    ):
        raise PullDeployError(f"control-file parent is unsafe: {parent}")
    temporary = parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        fsync_directory(parent)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def atomic_symlink(path: Path, target: str) -> None:
    ensure_private_directory(path.parent)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    temporary.symlink_to(target)
    os.replace(temporary, path)
    fsync_directory(path.parent)


def directory_inventory_digest(root: Path) -> str:
    """Hash a private tree without following links or accepting special files."""

    if not root.is_dir() or root.is_symlink():
        raise PullDeployError(f"inventory root is not a safe directory: {root}")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(path).encode("utf-8")
            digest.update(b"L\0" + relative + b"\0" + target + b"\0")
        elif stat.S_ISDIR(metadata.st_mode):
            digest.update(b"D\0" + relative + b"\0")
        elif stat.S_ISREG(metadata.st_mode):
            digest.update(b"F\0" + relative + b"\0")
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
        else:
            raise PullDeployError(f"inventory contains a special file: {path}")
    return "sha256:" + digest.hexdigest()


def wheel_payload_inventory(root: Path) -> dict[str, Any]:
    """Inventory only immutable wheel payloads, excluding cache metadata."""

    ensure_private_directory(root)
    files: list[dict[str, Any]] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.name in {".owner.json", "READY.json"}:
            continue
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or not path.name.endswith(".whl")
        ):
            raise PullDeployError(
                f"Worker wheel cache contains a non-wheel payload: {path.name}"
            )
        files.append(
            {
                "name": path.name,
                "size": metadata.st_size,
                "sha256": sha256_file(path),
            }
        )
    if not files:
        raise PullDeployError("Worker wheel cache download produced no wheel files")
    return {
        "files": files,
        "inventory_sha256": canonical_json_digest(files),
    }


def parse_literal_env(path: Path) -> dict[str, str]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PullDeployError(f"configuration is missing: {path}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise PullDeployError(f"configuration must be owner-only mode 0600: {path}")
    result: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PullDeployError(f"invalid configuration line {number}: {path}")
        key, value = line.split("=", 1)
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is None or key in result:
            raise PullDeployError(
                f"invalid configuration name on line {number}: {path}"
            )
        if any(character in value for character in ("\x00", "\r", "\n")):
            raise PullDeployError(
                f"invalid configuration value on line {number}: {path}"
            )
        result[key] = value
    return result


def validate_deploy_control_values(
    values: dict[str, str], *, runtime_root: Path
) -> dict[str, str]:
    forbidden_exact = {
        "PATH",
        "HOME",
        "PWD",
        "SHELL",
        "ENV",
        "BASH_ENV",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONNOUSERSITE",
        "LANG",
        "LC_ALL",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "TMPDIR",
        "TMP",
        "TEMP",
        "IFS",
        "CDPATH",
        "NEXPOLY_BACKEND_IMAGE",
        "NEXPOLY_WEB_IMAGE",
        "COMPOSE_PROJECT_NAME",
    }
    forbidden_prefixes = (
        "DOCKER_",
        "COMPOSE_",
        "GIT_",
        "SSH_",
        "LD_",
        "DYLD_",
        "PYTHON",
        "BUILDKIT_",
        "CONTAINER_",
    )
    dangerous = sorted(
        key
        for key in values
        if key in forbidden_exact or key.startswith(forbidden_prefixes)
    )
    if dangerous:
        raise PullDeployError(
            "deploy.env contains control-plane redirect variables: "
            + ", ".join(dangerous)
        )
    expected = {
        "NEXPOLY_RUNTIME_ROOT": str(runtime_root),
        "NEXPOLY_APP_ENV_FILE": str(runtime_root / "config/app.env"),
        "NEXPOLY_ASSET_ROOT": str(runtime_root / "state/current-assets"),
    }
    if any(values.get(key) != value for key, value in expected.items()):
        raise PullDeployError("deploy.env external runtime paths are not pinned")
    return dict(values)


def validate_private_docker_config(runtime_root: Path) -> Path:
    """Validate the non-executable, GHCR-only Docker credential document."""

    directory = runtime_root / "config/docker"
    ensure_private_directory(directory)
    path = directory / "config.json"
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PullDeployError("private Docker config.json is missing") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size < 1
        or metadata.st_size > 64 * 1024
    ):
        raise PullDeployError("private Docker config.json is unsafe")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PullDeployError("private Docker config.json is invalid") from exc
    if not isinstance(document, dict) or set(document) != {"auths"}:
        raise PullDeployError("Docker config must contain only a GHCR auth entry")
    auths = document.get("auths")
    if not isinstance(auths, dict) or set(auths) != {"ghcr.io"}:
        raise PullDeployError("Docker config must authenticate only ghcr.io")
    ghcr = auths.get("ghcr.io")
    if not isinstance(ghcr, dict) or set(ghcr) != {"auth"}:
        raise PullDeployError("Docker GHCR credential must use an inline auth field")
    encoded = ghcr.get("auth")
    if (
        not isinstance(encoded, str)
        or not 8 <= len(encoded) <= 16 * 1024
        or re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", encoded) is None
    ):
        raise PullDeployError("Docker GHCR credential encoding is invalid")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PullDeployError("Docker GHCR credential encoding is invalid") from exc
    if (
        len(decoded) > 8 * 1024
        or b":" not in decoded
        or not all(decoded.split(b":", 1))
        or any(value in decoded for value in (b"\x00", b"\r", b"\n"))
    ):
        raise PullDeployError("Docker GHCR credential payload is invalid")
    return directory


def test_root_mode(
    *,
    runtime_root: Path,
    production_root: Path | None = None,
) -> bool:
    """Return the unit-test mode only for roots disjoint from production.

    Tests execute the real state machines against private temporary trees.  The
    opt-in environment variable must never turn into an alternate production
    authorization path: even a direct invocation outside the stable selector
    is rejected when either resolved root is the production root.
    """

    enabled = os.environ.get("NEXPOLY_ALLOW_TEST_ROOT") == "1"
    if not enabled:
        return False
    resolved_runtime = runtime_root.resolve()
    resolved_production = (
        production_root.resolve() if production_root is not None else None
    )

    def overlaps(left: Path, right: Path) -> bool:
        return left == right or left in right.parents or right in left.parents

    supplied = [resolved_runtime]
    if resolved_production is not None:
        supplied.append(resolved_production)
    if any(
        overlaps(candidate, protected)
        for candidate in supplied
        for protected in (RUNTIME_ROOT, PRODUCTION_ROOT)
    ) or (
        resolved_production is not None
        and overlaps(resolved_runtime, resolved_production)
    ):
        raise PullDeployError("test-root mode is forbidden for production paths")
    return True


def clean_control_environment(runtime_root: Path) -> dict[str, str]:
    """Return the fixed deploy-user host control-plane environment.

    The caller's environment is deliberately not inherited.  In particular,
    Docker contexts, credential locations, user-bus addresses and ``HOME``
    must not be redirectable by an interactive shell or a service manager.
    """

    try:
        account = pwd.getpwuid(os.geteuid())
    except KeyError as exc:
        raise PullDeployError("deploy user has no passwd identity") from exc
    home = Path(account.pw_dir)
    if not home.is_absolute():
        raise PullDeployError("deploy-user passwd HOME must be absolute")
    allow_test_root = test_root_mode(runtime_root=runtime_root)
    if not allow_test_root and home != DEPLOY_USER_HOME:
        raise PullDeployError(
            "deploy user home differs from the fixed production identity"
        )

    docker_config = validate_private_docker_config(runtime_root)

    user_runtime = Path("/run/user") / str(os.geteuid())
    bus = user_runtime / "bus"
    try:
        runtime_metadata = user_runtime.lstat()
        bus_metadata = bus.lstat()
    except OSError as exc:
        raise PullDeployError("deploy-user systemd user bus is unavailable") from exc
    if (
        not stat.S_ISDIR(runtime_metadata.st_mode)
        or user_runtime.is_symlink()
        or runtime_metadata.st_uid != os.geteuid()
        or runtime_metadata.st_mode & 0o022
        or not stat.S_ISSOCK(bus_metadata.st_mode)
        or bus.is_symlink()
        or bus_metadata.st_uid != os.geteuid()
    ):
        raise PullDeployError("deploy-user systemd user bus is unsafe")

    return {
        "PATH": SAFE_PATH,
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "DOCKER_CONFIG": str(docker_config),
        "DOCKER_CONTEXT": "default",
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "XDG_RUNTIME_DIR": str(user_runtime),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={bus}",
        "PYTHONNOUSERSITE": "1",
    }


def parse_command_json(payload: object, label: str) -> dict[str, Any]:
    if (
        not isinstance(payload, str)
        or len(payload.encode("utf-8")) > MAX_GITHUB_RESPONSE_BYTES
    ):
        raise PullDeployError(f"{label} returned invalid JSON evidence")
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PullDeployError(f"{label} returned invalid JSON evidence") from exc
    if not isinstance(document, dict):
        raise PullDeployError(f"{label} evidence must be a JSON object")
    return document


def canonical_ledger_history(
    rows: object,
    manifest: object,
    *,
    accepted_ledgers: object | None = None,
    require_registry_match: bool = False,
) -> list[dict[str, Any]]:
    """Validate a canonical ledger or the unique registered trailing 0013.

    B never treats an arbitrary future row as compatible.  Its sole
    forward-compatible state is the checksum-exact 0013 extension registered
    by the F authority policy.
    """

    if not isinstance(rows, list) or not isinstance(manifest, list):
        raise PullDeployError("migration manifest or ledger evidence is invalid")
    if not rows or len(rows) > len(manifest) + 1:
        raise PullDeployError(
            "database migration ledger is empty or beyond the manifest"
        )
    history: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if index == len(manifest):
            if (
                accepted_ledgers is None
                or row != _bridge_core.FINAL_MIGRATION
                or not history
                or history[-1].get("version")
                != _bridge_core.CONTRACT_MIGRATION["version"]
                or history[-1].get("checksum")
                != _bridge_core.CONTRACT_MIGRATION["checksum"]
            ):
                raise PullDeployError(
                    "database migration ledger exceeds B without exact 0013 compatibility"
                )
            history.append(dict(_bridge_core.FINAL_MIGRATION_RECORD))
            continue
        expected = manifest[index]
        if (
            not isinstance(row, dict)
            or set(row) != {"version", "checksum"}
            or not isinstance(expected, dict)
            or set(expected)
            != {"version", "kind", "epoch", "checksum", "requires_contracts"}
            or row.get("version") != expected.get("version")
            or row.get("checksum") != expected.get("checksum")
        ):
            raise PullDeployError(
                "database migration ledger is not an exact canonical prefix"
            )
        history.append(dict(expected))
    if require_registry_match:
        if accepted_ledgers is None:
            raise PullDeployError(
                "migration ledger registry authority is unavailable"
            )
        try:
            _bridge_core.match_migration_ledger(accepted_ledgers, history)
        except Exception as exc:
            raise PullDeployError(
                "migration ledger is outside the exact bridge compatibility registry"
            ) from exc
    return history


def descriptor_accepted_ledgers(
    descriptor: dict[str, Any],
) -> list[dict[str, str]] | None:
    bridge = descriptor.get("bridge")
    if isinstance(bridge, dict):
        policy = bridge.get("policy")
        if isinstance(policy, dict):
            value = policy.get("accepted_migration_ledgers")
            if isinstance(value, list):
                return [dict(record) for record in value if isinstance(record, dict)]
    compatibility = descriptor.get("_migration_compatibility")
    if isinstance(compatibility, dict):
        value = compatibility.get("accepted_migration_ledgers")
        if isinstance(value, list):
            return [dict(record) for record in value if isinstance(record, dict)]
    return None


def _validate_drain_state(
    drain: object,
    *,
    expected_enabled: bool | None,
    authority_sha: str | None,
    label: str,
) -> dict[str, Any]:
    fields = {
        "enabled",
        "reason",
        "release_sha",
        "activated_at",
        "activated_by",
        "updated_at",
    }
    if (
        not isinstance(drain, dict)
        or set(drain) != fields
        or not isinstance(drain.get("enabled"), bool)
        or not isinstance(drain.get("updated_at"), str)
        or not drain["updated_at"]
    ):
        raise PullDeployError(f"{label} drain state is invalid")
    if expected_enabled is not None and drain["enabled"] is not expected_enabled:
        raise PullDeployError(
            f"{label} drain state differs from the expected admission"
        )
    if drain["enabled"]:
        if (
            not isinstance(drain.get("reason"), str)
            or not drain["reason"]
            or not isinstance(drain.get("activated_at"), str)
            or not drain["activated_at"]
            or not isinstance(drain.get("activated_by"), str)
            or not drain["activated_by"]
            or not isinstance(drain.get("release_sha"), str)
            or SHA_RE.fullmatch(drain["release_sha"]) is None
            or authority_sha is not None
            and (
                drain.get("activated_by") != "pull-deploy-controller"
                or drain.get("release_sha") != authority_sha
            )
        ):
            raise PullDeployError(f"{label} drain ownership differs")
    elif any(
        drain.get(field) is not None
        for field in ("reason", "release_sha", "activated_at", "activated_by")
    ):
        raise PullDeployError(f"{label} resumed drain retained owner state")
    return drain


def validate_active_jobs_evidence(
    document: dict[str, Any],
    *,
    require_drained: bool,
    require_resumed: bool = False,
    authority_sha: str | None = None,
) -> dict[str, Any]:
    if require_drained and require_resumed:
        raise PullDeployError(
            "active-job evidence cannot require drain and resume together"
        )
    allowed = {"drain", "active_jobs", "active_total", "active_jobs_schema_version"}
    if not {"drain", "active_jobs", "active_total"}.issubset(document) or not set(
        document
    ).issubset(allowed):
        raise PullDeployError("Backend active-job evidence has an invalid shape")
    version = document.get("active_jobs_schema_version", 1)
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in {1, 2}
    ):
        raise PullDeployError("Backend active-job schema version is unsupported")
    counts = document.get("active_jobs")
    expected = ACTIVE_JOB_FIELDS_V1 if version == 1 else ACTIVE_JOB_FIELDS_V2
    if not isinstance(counts, dict) or set(counts) != expected:
        raise PullDeployError(
            "Backend active-job categories differ from the selected schema"
        )
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in counts.values()
    ):
        raise PullDeployError("Backend active-job counts must be nonnegative integers")
    total = document.get("active_total")
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or total < 0
        or total != sum(counts.values())
    ):
        raise PullDeployError("Backend active-job total is inconsistent")
    drain = _validate_drain_state(
        document.get("drain"),
        expected_enabled=True
        if require_drained
        else False
        if require_resumed
        else None,
        authority_sha=authority_sha,
        label="Backend",
    )
    if require_drained and (drain["enabled"] is not True or total != 0):
        raise PullDeployError("Backend has not reached a drained zero-work state")
    if require_resumed and (drain["enabled"] is not False or total != 0):
        raise PullDeployError("Backend has not proved resumed zero-work admission")
    return document


def validate_persistent_drain_evidence(
    document: dict[str, Any],
    *,
    expected_enabled: bool | None = None,
    authority_sha: str | None = None,
) -> dict[str, Any]:
    """Validate the PostgreSQL-only deployment-control CLI response."""

    required = {"drain", "active_jobs", "active_total"}
    allowed = required | {"active_jobs_schema_version"}
    if (
        not isinstance(document, dict)
        or not required.issubset(document)
        or not set(document).issubset(allowed)
    ):
        raise PullDeployError("persistent drain evidence has an invalid shape")
    version = document.get("active_jobs_schema_version", 1)
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in {1, 2}
    ):
        raise PullDeployError(
            "persistent drain active-job schema version is unsupported"
        )
    counts = document.get("active_jobs")
    expected = (
        PERSISTENT_JOB_FIELDS_V1
        if version == 1
        else PERSISTENT_JOB_FIELDS_V2
    )
    if not isinstance(counts, dict) or set(counts) != expected:
        raise PullDeployError("persistent drain job categories are invalid")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in counts.values()
    ):
        raise PullDeployError("persistent drain job counts are invalid")
    total = document.get("active_total")
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or total < 0
        or total != sum(counts.values())
    ):
        raise PullDeployError("persistent drain total is inconsistent")
    _validate_drain_state(
        document.get("drain"),
        expected_enabled=expected_enabled,
        authority_sha=authority_sha,
        label="persistent",
    )
    return document


def validate_bootstrap_quiesce_evidence(
    document: dict[str, Any],
) -> dict[str, Any]:
    """Validate the legacy takeover hook without weakening Backend schema."""

    if set(document) != {
        "ingress_isolated",
        "active_jobs",
        "active_total",
        "active_jobs_schema_version",
    }:
        raise PullDeployError("bootstrap quiesce evidence has an invalid shape")
    version = document.get("active_jobs_schema_version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in {1, 2}
    ):
        raise PullDeployError("bootstrap active-job schema version is unsupported")
    expected = ACTIVE_JOB_FIELDS_V1 if version == 1 else ACTIVE_JOB_FIELDS_V2
    counts = document.get("active_jobs")
    if (
        document.get("ingress_isolated") is not True
        or not isinstance(counts, dict)
        or set(counts) != expected
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value != 0
            for value in counts.values()
        )
        or not isinstance(document.get("active_total"), int)
        or isinstance(document.get("active_total"), bool)
        or document["active_total"] != 0
    ):
        raise PullDeployError(
            "bootstrap quiesce did not prove isolated zero-work state"
        )
    return document


def validate_bootstrap_resume_unchanged_evidence(
    document: dict[str, Any], *, expected_runtime_digest: str
) -> dict[str, Any]:
    """Validate legacy ingress recovery that is forbidden to restart workers.

    The audited hook records the Backend container and Worker main PID on both
    sides of the ingress-only recovery.  Equality is essential: image/unit
    hashes alone would also accept a destructive restart of an active job.
    """

    expected_fields = {
        "schema_version",
        "legacy_runtime_unchanged",
        "backend_image_id",
        "web_image_id",
        "worker_unit_sha256",
        "backend_container_id_before",
        "backend_container_id_after",
        "backend_pid_before",
        "backend_pid_after",
        "backend_started_at_before",
        "backend_started_at_after",
        "backend_restart_count_before",
        "backend_restart_count_after",
        "worker_main_pid_before",
        "worker_main_pid_after",
        "worker_invocation_id_before",
        "worker_invocation_id_after",
        "worker_active_enter_monotonic_before",
        "worker_active_enter_monotonic_after",
        "backend_healthy",
        "web_healthy",
        "worker_healthy",
        "ingress_restored",
    }
    if not isinstance(document, dict) or set(document) != expected_fields:
        raise PullDeployError(
            "bootstrap unchanged-resume evidence has an invalid shape"
        )
    if document.get("schema_version") != 1:
        raise PullDeployError(
            "bootstrap unchanged-resume evidence has an unsupported schema"
        )
    identity = {
        key: require_digest(document.get(key), f"bootstrap unchanged-resume {key}")
        for key in ("backend_image_id", "web_image_id", "worker_unit_sha256")
    }
    if canonical_json_digest(identity) != require_digest(
        expected_runtime_digest, "bootstrap legacy runtime digest"
    ):
        raise PullDeployError(
            "bootstrap unchanged-resume selected a different legacy runtime identity"
        )
    container_before = document.get("backend_container_id_before")
    container_after = document.get("backend_container_id_after")
    if (
        not isinstance(container_before, str)
        or re.fullmatch(r"[0-9a-f]{64}", container_before) is None
        or container_after != container_before
    ):
        raise PullDeployError(
            "bootstrap unchanged-resume restarted or replaced the Backend"
        )
    backend_pid = document.get("backend_pid_before")
    backend_started_at = document.get("backend_started_at_before")
    backend_restart_count = document.get("backend_restart_count_before")
    if (
        not isinstance(backend_pid, int)
        or isinstance(backend_pid, bool)
        or backend_pid <= 0
        or document.get("backend_pid_after") != backend_pid
        or not isinstance(backend_started_at, str)
        or not backend_started_at
        or document.get("backend_started_at_after") != backend_started_at
        or not isinstance(backend_restart_count, int)
        or isinstance(backend_restart_count, bool)
        or backend_restart_count < 0
        or document.get("backend_restart_count_after") != backend_restart_count
    ):
        raise PullDeployError(
            "bootstrap unchanged-resume restarted the Backend process"
        )
    pid_before = document.get("worker_main_pid_before")
    pid_after = document.get("worker_main_pid_after")
    if (
        not isinstance(pid_before, int)
        or isinstance(pid_before, bool)
        or pid_before <= 0
        or pid_after != pid_before
    ):
        raise PullDeployError(
            "bootstrap unchanged-resume restarted or replaced the Worker"
        )
    invocation = document.get("worker_invocation_id_before")
    entered = document.get("worker_active_enter_monotonic_before")
    if (
        not isinstance(invocation, str)
        or not invocation
        or document.get("worker_invocation_id_after") != invocation
        or not isinstance(entered, int)
        or isinstance(entered, bool)
        or entered <= 0
        or document.get("worker_active_enter_monotonic_after") != entered
    ):
        raise PullDeployError(
            "bootstrap unchanged-resume restarted the Worker invocation"
        )
    for field in (
        "legacy_runtime_unchanged",
        "backend_healthy",
        "web_healthy",
        "worker_healthy",
        "ingress_restored",
    ):
        if document.get(field) is not True:
            raise PullDeployError(f"bootstrap unchanged-resume did not prove {field}")
    return document


def validate_bootstrap_status_evidence(
    document: dict[str, Any], *, expected_runtime_digest: str
) -> dict[str, Any]:
    """Validate a read-only, all-open or all-stopped legacy runtime probe."""

    fields = {
        "schema_version",
        "legacy_runtime_state",
        "backend_image_id",
        "web_image_id",
        "worker_unit_sha256",
        "backend_container_id",
        "backend_pid",
        "backend_started_at",
        "backend_restart_count",
        "worker_main_pid",
        "worker_invocation_id",
        "worker_active_enter_monotonic",
        "backend_healthy",
        "web_healthy",
        "worker_healthy",
        "ingress_open",
    }
    if not isinstance(document, dict) or set(document) != fields:
        raise PullDeployError("bootstrap legacy status evidence has an invalid shape")
    if document.get("schema_version") != 1:
        raise PullDeployError("bootstrap legacy status schema is unsupported")
    identity = {
        key: require_digest(document.get(key), f"bootstrap status {key}")
        for key in ("backend_image_id", "web_image_id", "worker_unit_sha256")
    }
    if canonical_json_digest(identity) != require_digest(
        expected_runtime_digest, "bootstrap legacy runtime digest"
    ):
        raise PullDeployError("bootstrap status selected a different legacy identity")
    state = document.get("legacy_runtime_state")
    process_fields = (
        "backend_container_id",
        "backend_pid",
        "backend_started_at",
        "backend_restart_count",
        "worker_main_pid",
        "worker_invocation_id",
        "worker_active_enter_monotonic",
    )
    health_fields = (
        "backend_healthy",
        "web_healthy",
        "worker_healthy",
        "ingress_open",
    )
    if state == "stopped":
        if any(document.get(field) is not None for field in process_fields) or any(
            document.get(field) is not False for field in health_fields
        ):
            raise PullDeployError(
                "bootstrap legacy status is neither fully stopped nor open"
            )
        return document
    if state not in {"open", "isolated"}:
        raise PullDeployError(
            "bootstrap legacy status is neither fully stopped nor open"
        )
    expected_health = {
        "backend_healthy": True,
        "web_healthy": state == "open",
        "worker_healthy": True,
        "ingress_open": state == "open",
    }
    if any(
        document.get(field) is not value for field, value in expected_health.items()
    ):
        raise PullDeployError(
            "bootstrap legacy running status has inconsistent ingress evidence"
        )
    if (
        not isinstance(document.get("backend_container_id"), str)
        or re.fullmatch(r"[0-9a-f]{64}", document["backend_container_id"]) is None
        or not isinstance(document.get("backend_pid"), int)
        or isinstance(document.get("backend_pid"), bool)
        or document["backend_pid"] <= 0
        or not isinstance(document.get("backend_started_at"), str)
        or not document["backend_started_at"]
        or not isinstance(document.get("backend_restart_count"), int)
        or isinstance(document.get("backend_restart_count"), bool)
        or document["backend_restart_count"] < 0
        or not isinstance(document.get("worker_main_pid"), int)
        or isinstance(document.get("worker_main_pid"), bool)
        or document["worker_main_pid"] <= 0
        or not isinstance(document.get("worker_invocation_id"), str)
        or not document["worker_invocation_id"]
        or not isinstance(document.get("worker_active_enter_monotonic"), int)
        or isinstance(document.get("worker_active_enter_monotonic"), bool)
        or document["worker_active_enter_monotonic"] <= 0
    ):
        raise PullDeployError("bootstrap legacy open-process identity is invalid")
    return document


def validate_worker_control_evidence(
    document: dict[str, Any], *, action: str, require_zero: bool
) -> dict[str, Any]:
    required = {"status", "accepting_jobs", "active_jobs", "worker_instance_id"}
    if not required.issubset(document):
        raise PullDeployError(f"Worker {action} evidence has an invalid shape")
    active = document.get("active_jobs")
    if not isinstance(active, int) or isinstance(active, bool) or active < 0:
        raise PullDeployError(f"Worker {action} active-job count is invalid")
    if (
        not isinstance(document.get("worker_instance_id"), str)
        or not document["worker_instance_id"]
    ):
        raise PullDeployError(f"Worker {action} instance identity is invalid")
    if not isinstance(document.get("accepting_jobs"), bool):
        raise PullDeployError(f"Worker {action} admission state is invalid")
    if action == "drain" and (
        document.get("status") != "draining"
        or document.get("accepting_jobs") is not False
    ):
        raise PullDeployError("Worker did not prove drained admission")
    if action == "health-drained" and (
        document.get("status") not in {"ok", "degraded"}
        or document.get("draining") is not True
        or document.get("accepting_jobs") is not False
    ):
        raise PullDeployError("Worker health did not prove drained admission")
    if action == "health-resumed" and (
        document.get("status") not in {"ok", "degraded"}
        or document.get("draining") is not False
        or (active == 0 and document.get("accepting_jobs") is not True)
    ):
        raise PullDeployError("Worker health did not prove resumed admission")
    if action == "resume" and (
        document.get("status") != "ready" or document.get("accepting_jobs") is not True
    ):
        raise PullDeployError("Worker did not resume")
    if action == "resume-unchanged" and (
        document.get("status") != "ready"
        or (active == 0 and document.get("accepting_jobs") is not True)
    ):
        # Reopening admission does not create capacity.  The unchanged-runtime
        # recovery path deliberately permits an already accepted job to keep
        # running, so a single-capacity Worker may correctly report
        # ``accepting_jobs=false`` until that job reaches a terminal state.
        raise PullDeployError("Worker did not resume unchanged admission")
    if require_zero and active != 0:
        raise PullDeployError("Worker still has active jobs")
    return document


def validate_slot_record(
    document: dict[str, Any], slot: str | None = None
) -> dict[str, Any]:
    try:
        prefix = Path(str(document.get("venv_prefix", "")))
        runtime_root = prefix.parents[2]
        shared_validate_slot_record(
            document,
            runtime_root=runtime_root,
            expected_slot=slot,
        )
    except (WorkerSlotError, IndexError) as exc:
        raise PullDeployError(f"monomer MD slot record is invalid: {exc}") from exc
    return document


def validate_active_slot_record(document: dict[str, Any]) -> dict[str, Any]:
    try:
        shared_validate_active_record(document)
    except WorkerSlotError as exc:
        raise PullDeployError(
            f"active monomer MD slot record is invalid: {exc}"
        ) from exc
    return document


def inspect_asset_release(asset_root: Path, expected_digest: str) -> dict[str, Any]:
    """Validate and bind the immutable external production asset release."""

    if expected_digest == SCHEMA_V2_ASSET_MANIFEST_DIGEST:
        try:
            evidence = _asset_release_contract.validate_schema_v2_release(
                asset_root,
                expected_digest=expected_digest,
                releases_root=ASSET_RELEASES_ROOT,
            )
        except _asset_release_contract.AssetContractError as exc:
            raise PullDeployError(
                "strict external schema-v2 asset validation failed"
            ) from exc
        return {
            "root": evidence["root"],
            "manifest_sha256": evidence["manifest_sha256"],
            "schema_version": evidence["schema_version"],
            "byteff2_commit": evidence["byteff2_commit"],
            "inventory_sha256": evidence["inventory_sha256"],
        }

    try:
        root_metadata = asset_root.lstat()
    except OSError as exc:
        raise PullDeployError(
            "external production asset release target is missing"
        ) from exc
    expected_name = require_digest(expected_digest, "target asset manifest").split(
        ":", 1
    )[1]
    if (
        asset_root.parent != ASSET_RELEASES_ROOT
        or asset_root.name != expected_name
        or not stat.S_ISDIR(root_metadata.st_mode)
        or asset_root.is_symlink()
        or root_metadata.st_uid not in {0, os.geteuid()}
        or root_metadata.st_mode & 0o222
    ):
        raise PullDeployError(
            "external production asset release target is not content-addressed and read-only"
        )
    manifest_path = asset_root / "ASSET-MANIFEST.json"
    try:
        manifest_metadata = manifest_path.lstat()
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PullDeployError(
            "external production asset manifest is unavailable"
        ) from exc
    if (
        not stat.S_ISREG(manifest_metadata.st_mode)
        or manifest_path.is_symlink()
        or manifest_metadata.st_uid not in {0, os.geteuid()}
        or manifest_metadata.st_mode & 0o222
        or sha256_file(manifest_path)
        != require_digest(expected_digest, "target asset manifest")
    ):
        raise PullDeployError(
            "external production asset manifest identity differs from target"
        )
    expected_trees = {"model", "database", "backend-data", "byteff2"}
    base_fields = {"schema_version", "byteff2_commit", "byteff2_submodules", "assets"}
    schema_v2_fields = base_fields | {
        "byteff2_source",
        "byteff2_audited_overlays",
        "predecessor_asset_digest",
        "changed_asset_trees",
        "unchanged_asset_tree_digests",
    }
    if (
        not isinstance(document, dict)
        or (document.get("schema_version") == 1 and set(document) != base_fields)
        or (
            document.get("schema_version") == 2
            and set(document) != schema_v2_fields
        )
        or document.get("schema_version") not in {1, 2}
    ):
        raise PullDeployError(
            "external production asset manifest schema is unsupported"
        )
    assets = document.get("assets")
    if not isinstance(assets, dict) or set(assets) != expected_trees:
        raise PullDeployError(
            "external production asset trees differ from the manifest"
        )
    byteff2_commit = require_sha(document.get("byteff2_commit"), "asset ByteFF2 commit")
    submodules = document.get("byteff2_submodules")
    if not isinstance(submodules, dict) or any(
        not isinstance(name, str)
        or not name
        or PurePosixPath(name).is_absolute()
        or ".." in PurePosixPath(name).parts
        or str(PurePosixPath(name)) != name
        or not isinstance(commit, str)
        or SHA_RE.fullmatch(commit) is None
        for name, commit in submodules.items()
    ):
        raise PullDeployError("external ByteFF2 submodule identity is invalid")
    if document["schema_version"] == 2:
        try:
            governance = _load_governance_core()
            governance.validate_byteff2_source(
                document["byteff2_source"],
                manifest_commit=byteff2_commit,
                require_exact_identity=True,
            )
            governance.validate_byteff2_audited_overlay(
                document["byteff2_audited_overlays"],
                require_exact_identity=True,
            )
        except Exception as exc:
            raise PullDeployError(
                "external schema-v2 ByteFF2 source identity is invalid"
            ) from exc
        if (
            expected_digest != SCHEMA_V2_ASSET_MANIFEST_DIGEST
            or document["predecessor_asset_digest"]
            != SCHEMA_V2_PREDECESSOR_ASSET_MANIFEST_DIGEST
            or document["changed_asset_trees"] != ["byteff2"]
            or document["unchanged_asset_tree_digests"]
            != SCHEMA_V2_UNCHANGED_ASSET_TREE_DIGESTS
        ):
            raise PullDeployError(
                "external schema-v2 asset predecessor evidence differs"
            )
    root_entries = {entry.name for entry in asset_root.iterdir()}
    if root_entries != expected_trees | {"ASSET-MANIFEST.json"}:
        raise PullDeployError("external production asset root has unmanifested entries")
    inventory = hashlib.sha256()
    for tree_name in sorted(expected_trees):
        tree = asset_root / tree_name
        if not tree.is_dir() or tree.is_symlink() or tree.stat().st_mode & 0o222:
            raise PullDeployError(
                f"external production asset tree is unsafe: {tree_name}"
            )
        records = assets[tree_name]
        if not isinstance(records, list):
            raise PullDeployError("external production asset records are malformed")
        expected: dict[str, tuple[int, str]] = {}
        for record in records:
            if not isinstance(record, dict) or set(record) != {
                "path",
                "size",
                "sha256",
            }:
                raise PullDeployError(
                    "external production asset record has an invalid shape"
                )
            relative = record.get("path")
            size = record.get("size")
            checksum = record.get("sha256")
            pure = (
                PurePosixPath(relative)
                if isinstance(relative, str)
                else PurePosixPath(".")
            )
            if (
                not isinstance(relative, str)
                or not relative
                or pure.is_absolute()
                or ".." in pure.parts
                or str(pure) != relative
                or relative in expected
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or not isinstance(checksum, str)
                or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
            ):
                raise PullDeployError("external production asset record is unsafe")
            expected[relative] = (size, checksum)
        actual: set[str] = set()
        for directory, names, files in os.walk(tree, followlinks=False):
            current = Path(directory)
            if current.stat().st_mode & 0o222:
                raise PullDeployError("external production asset directory is writable")
            for name in names:
                child = current / name
                if (
                    child.is_symlink()
                    or not child.is_dir()
                    or child.stat().st_mode & 0o222
                ):
                    raise PullDeployError(
                        "external production asset directory is unsafe"
                    )
            for name in files:
                child = current / name
                relative = child.relative_to(tree).as_posix()
                if (
                    child.is_symlink()
                    or not child.is_file()
                    or child.stat().st_mode & 0o222
                ):
                    raise PullDeployError("external production asset file is unsafe")
                actual.add(relative)
        if actual != set(expected):
            raise PullDeployError(
                f"external production asset inventory differs: {tree_name}"
            )
        for relative, (size, checksum) in sorted(expected.items()):
            child = tree.joinpath(*PurePosixPath(relative).parts)
            if (
                child.stat().st_size != size
                or sha256_file(child) != "sha256:" + checksum
            ):
                raise PullDeployError(
                    f"external production asset digest differs: {tree_name}/{relative}"
                )
            inventory.update(
                f"{tree_name}/{relative}\0{size}\0{checksum}\n".encode("utf-8")
            )
        if (
            document["schema_version"] == 2
            and tree_name in SCHEMA_V2_UNCHANGED_ASSET_TREE_DIGESTS
        ):
            tree_inventory = (
                json.dumps(
                    {"files": records},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            if sha256_bytes(tree_inventory) != document[
                "unchanged_asset_tree_digests"
            ][tree_name]:
                raise PullDeployError(
                    f"external unchanged asset tree evidence differs: {tree_name}"
                )
    commit_file = asset_root / "byteff2" / "BYTEFF2-COMMIT"
    if commit_file.read_text(encoding="ascii").strip() != byteff2_commit:
        raise PullDeployError("external ByteFF2 commit marker differs from manifest")
    if document["schema_version"] == 2:
        predecessor_root = ASSET_RELEASES_ROOT / (
            SCHEMA_V2_PREDECESSOR_ASSET_MANIFEST_DIGEST.split(":", 1)[1]
        )
        predecessor = inspect_asset_release(
            predecessor_root,
            SCHEMA_V2_PREDECESSOR_ASSET_MANIFEST_DIGEST,
        )
        try:
            predecessor_document = json.loads(
                (predecessor_root / "ASSET-MANIFEST.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PullDeployError(
                "external predecessor asset evidence is unavailable"
            ) from exc
        if (
            predecessor["schema_version"] != 1
            or any(
                predecessor_document["assets"][tree_name]
                != document["assets"][tree_name]
                for tree_name in SCHEMA_V2_UNCHANGED_ASSET_TREE_DIGESTS
            )
        ):
            raise PullDeployError(
                "external schema-v2 unchanged trees differ from predecessor"
            )
    return {
        "root": str(asset_root),
        "manifest_sha256": expected_digest,
        "schema_version": document["schema_version"],
        "byteff2_commit": byteff2_commit,
        "inventory_sha256": "sha256:" + inventory.hexdigest(),
    }


def build_migration_compatibility_state(
    authority: object,
    *,
    code_manifest_sha256: object,
    migrations: object,
) -> dict[str, Any]:
    """Bind code and live ledger identities to the frozen B/F registry."""

    if not isinstance(authority, dict):
        raise PullDeployError("migration compatibility authority is invalid")
    accepted = authority.get("accepted_migration_ledgers")
    policy_id = require_digest(
        authority.get("policy_id"), "migration compatibility policy"
    )
    code_manifest = require_digest(
        code_manifest_sha256, "migration compatibility code manifest"
    )
    try:
        ledger_state = _bridge_core.match_migration_ledger(accepted, migrations)
    except Exception as exc:
        raise PullDeployError(
            "migration history is outside the frozen B/F registry"
        ) from exc
    if not isinstance(accepted, list):
        raise PullDeployError("migration compatibility registry is invalid")
    normalized = [dict(record) for record in accepted if isinstance(record, dict)]
    if len(normalized) != 3:
        raise PullDeployError("migration compatibility registry is incomplete")
    by_name = {record.get("name"): record for record in normalized}
    if set(by_name) != set(_bridge_core.REQUIRED_LEDGER_ORDER):
        raise PullDeployError("migration compatibility registry names are invalid")
    target_manifest = require_digest(
        by_name["post-0012"].get("manifest_sha256"),
        "B migration manifest",
    )
    authority_manifest = require_digest(
        by_name["post-0013"].get("manifest_sha256"),
        "F migration manifest",
    )
    if (
        by_name["pre-0012"].get("manifest_sha256") != target_manifest
        or code_manifest not in {target_manifest, authority_manifest}
        or (
            ledger_state["name"] != "post-0013"
            and code_manifest != target_manifest
        )
    ):
        raise PullDeployError(
            "code/ledger migration compatibility pair is not registered"
        )
    return {
        "schema_version": 1,
        "policy_id": policy_id,
        "target_manifest_sha256": target_manifest,
        "authority_manifest_sha256": authority_manifest,
        "code_manifest_sha256": code_manifest,
        "ledger_manifest_sha256": ledger_state["manifest_sha256"],
        "ledger_state": ledger_state,
        "accepted_migration_ledgers": normalized,
    }


def validate_migration_compatibility_state(
    value: object,
    *,
    migrations: object,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != MIGRATION_COMPATIBILITY_FIELDS:
        raise PullDeployError("current migration compatibility has an invalid shape")
    if value.get("schema_version") != 1:
        raise PullDeployError("current migration compatibility schema is unsupported")
    rebuilt = build_migration_compatibility_state(
        value,
        code_manifest_sha256=value.get("code_manifest_sha256"),
        migrations=migrations,
    )
    if rebuilt != value:
        raise PullDeployError("current migration compatibility identity differs")
    return rebuilt


def validate_rollback_provenance(
    document: object,
    *,
    state: Mapping[str, Any],
    mutable_pair: Mapping[str, Any],
    final_mutable_pair: Mapping[str, Any] | None,
    final_external_pair: Mapping[str, Any] | None,
    history_ledger: list[dict[str, str]],
) -> dict[str, Any]:
    """Validate the exact F -> B authority while retaining the 0013 ledger."""

    if (
        not isinstance(document, dict)
        or set(document) != ROLLBACK_PROVENANCE_FIELDS
        or document.get("schema_version") != 1
        or document.get("kind")
        not in {
            "explicit-code-rollback",
            "explicit-f-to-b-retain-0013",
        }
    ):
        raise PullDeployError("rollback provenance has an invalid shape")
    for field in (
        "rollback_operation_id",
        "from_operation_id",
        "to_operation_id",
    ):
        require_operation_id(str(document.get(field, "")))
    for field in (
        "from_source_sha",
        "from_source_tree",
        "to_source_sha",
        "to_source_tree",
    ):
        require_sha(document.get(field), f"rollback provenance {field}")
    for field in (
        "from_descriptor_sha256",
        "from_state_sha256",
        "from_terminal_audit_sha256",
        "to_descriptor_sha256",
        "sealed_previous_state_sha256",
        "retained_ledger_sha256",
    ):
        require_digest(document.get(field), f"rollback provenance {field}")
    expected_final_mutable_digest = (
        canonical_json_digest(final_mutable_pair)
        if final_mutable_pair is not None
        else None
    )
    expected_final_external_digest = (
        canonical_json_digest(final_external_pair)
        if final_external_pair is not None
        else None
    )
    if (
        not isinstance(document.get("created_at"), str)
        or not document["created_at"]
        or document["rollback_operation_id"]
        != document["from_operation_id"]
        or document["to_operation_id"] != state.get("operation_id")
        or document["to_source_sha"] != state.get("source_sha")
        or document["to_source_tree"] != state.get("source_tree")
        or document["to_descriptor_sha256"]
        != state.get("descriptor_sha256")
        or document["retained_ledger_sha256"]
        != canonical_json_digest(history_ledger)
        or document["final_mutable_data_audit_sha256"]
        != expected_final_mutable_digest
        or document["final_external_database_audit_sha256"]
        != expected_final_external_digest
        or mutable_pair["transition"]["operation_id"]
        != document["rollback_operation_id"]
    ):
        raise PullDeployError("rollback provenance identity differs")
    if document["kind"] == "explicit-f-to-b-retain-0013" and (
        final_mutable_pair is None
        or final_external_pair is None
    ):
        raise PullDeployError("retained 0013 rollback provenance differs")
    return dict(document)


def validate_current_deployment_state(document: dict[str, Any]) -> dict[str, Any]:
    """Validate the durable state that a new prepare seals by digest."""

    if (
        not isinstance(document, dict)
        or not CURRENT_STATE_FIELDS.issubset(document)
        or not set(document).issubset(
            CURRENT_STATE_FIELDS | CURRENT_STATE_OPTIONAL_FIELDS
        )
    ):
        raise PullDeployError("current deployment state has an invalid shape")
    if document.get("schema_version") != 2 or document.get("status") != "success":
        raise PullDeployError("current deployment state is not successful schema V2")
    require_operation_id(str(document.get("operation_id", "")))
    require_sha(document.get("source_sha"), "current deployment source SHA")
    require_sha(document.get("source_tree"), "current deployment source tree")
    require_sha(document.get("previous_release"), "current previous release SHA")
    require_digest(document.get("descriptor_sha256"), "current descriptor digest")
    require_digest(document.get("asset_manifest_digest"), "current asset manifest")
    require_sha(document.get("byteff2_commit"), "current ByteFF2 commit")
    validate_image_records(document.get("images"), source_sha=document["source_sha"])
    asset = validate_asset_identity(document.get("asset_identity"))
    if asset["manifest_sha256"] != document["asset_manifest_digest"]:
        raise PullDeployError(
            "current deployment asset digest differs from its identity"
        )
    history = document.get("migrations")
    if not isinstance(history, list) or not history:
        raise PullDeployError("current deployment migration history is invalid")
    for record in history:
        if (
            not isinstance(record, dict)
            or set(record)
            != {"version", "kind", "epoch", "checksum", "requires_contracts"}
            or not isinstance(record.get("version"), str)
            or not isinstance(record.get("kind"), str)
            or not isinstance(record.get("epoch"), int)
            or isinstance(record.get("epoch"), bool)
            or re.fullmatch(r"[0-9a-f]{64}", str(record.get("checksum", ""))) is None
            or not isinstance(record.get("requires_contracts"), list)
        ):
            raise PullDeployError("current deployment migration history is invalid")
    history_ledger = [
        {
            "version": record["version"],
            "checksum": record["checksum"],
        }
        for record in history
    ]
    history_versions = {
        record["version"] for record in history_ledger
    }
    migration_compatibility = validate_migration_compatibility_state(
        document.get("migration_compatibility"),
        migrations=history,
    )
    active = validate_active_slot_record(document.get("active_monomer_md_slot"))
    if (
        active["source_sha"] != document["source_sha"]
        or active["source_tree"] != document["source_tree"]
    ):
        raise PullDeployError("current deployment Worker slot differs from its source")
    unit = document.get("monomer_md_systemd_unit")
    if (
        not isinstance(unit, dict)
        or set(unit)
        != {"target_path", "sha256", "control_release_id", "launcher_sha256"}
        or not isinstance(unit.get("target_path"), str)
        or not Path(unit["target_path"]).is_absolute()
    ):
        raise PullDeployError("current deployment Worker unit identity is invalid")
    require_digest(unit.get("sha256"), "current Worker unit digest")
    if (
        not isinstance(unit.get("control_release_id"), str)
        or _control_runtime.RELEASE_ID_RE.fullmatch(unit["control_release_id"]) is None
    ):
        raise PullDeployError("current Worker control release identity is invalid")
    require_digest(unit.get("launcher_sha256"), "current Worker launcher digest")
    worker_env = document.get("monomer_md_worker_env")
    if (
        not isinstance(worker_env, dict)
        or set(worker_env)
        != {
            "path",
            "sha256",
            "byteff2_python",
            "byteff2_openmm_dir",
            "gmx_sha256",
        }
        or not isinstance(worker_env.get("path"), str)
        or not Path(worker_env["path"]).is_absolute()
        or not isinstance(worker_env.get("byteff2_python"), str)
        or not Path(worker_env["byteff2_python"]).is_absolute()
        or not isinstance(worker_env.get("byteff2_openmm_dir"), str)
        or not Path(worker_env["byteff2_openmm_dir"]).is_absolute()
    ):
        raise PullDeployError(
            "current deployment Worker environment identity is invalid"
        )
    require_digest(worker_env.get("sha256"), "current Worker environment digest")
    require_digest(worker_env.get("gmx_sha256"), "current Worker GMX digest")
    control_helpers = document.get("control_helpers")
    if not isinstance(control_helpers, dict) or set(control_helpers) != set(
        STABLE_HELPER_FILES
    ):
        raise PullDeployError("current deployment helper identity is invalid")
    for name, digest in control_helpers.items():
        require_digest(digest, f"current stable helper {name}")
    try:
        active_control = _control_runtime.validate_active_control_record(
            document.get("active_control")
        )
    except Exception as exc:
        raise PullDeployError(
            "current deployment control authority is invalid"
        ) from exc
    if (
        active_control["source_sha"] != document["source_sha"]
        or active_control["source_tree"] != document["source_tree"]
        or unit["control_release_id"] != active_control["release_id"]
        or active_control["operation_id"] != document["operation_id"]
        or active["operation_id"] != document["operation_id"]
    ):
        raise PullDeployError(
            "current deployment controls differ from source or Worker"
        )
    validate_production_config_evidence(document.get("production_config"))
    mutable_pair = validate_mutable_data_pair(
        document.get("mutable_data_audit")
    )
    mutable_stage_pairs = [mutable_pair]
    if mutable_pair["transition"]["kind"] == "bridge-expand-to-0011":
        expected_bridge_ledger = [
            {"version": version, "checksum": checksum}
            for version, checksum in (
                _site_helper_contracts.CANONICAL_MIGRATION_LEDGER[:11]
            )
        ]
        if (
            mutable_pair["transition"]["descriptor_sha256"]
            != document["descriptor_sha256"]
            or history_ledger != expected_bridge_ledger
            or mutable_pair["after"]["governed_controls"][
                "deployment_control"
            ]["row"]["release_sha"]
            != document["source_sha"]
        ):
            raise PullDeployError(
                "current bridge mutable-data transition differs from deployment authority"
            )
    external_database_audit = document.get("external_database_audit")
    if external_database_audit is not None:
        external_database_audit = validate_external_database_audit_binding(
            external_database_audit
        )
    external_chain = document.get("external_database_transition_chain")
    if external_database_audit is not None and external_chain is None:
        raise PullDeployError(
            "external database audit lacks its transition chain"
        )
    if external_chain is not None:
        if external_database_audit is None:
            raise PullDeployError(
                "external database transition chain lacks active binding"
            )
        external_chain = validate_external_database_transition_chain(
            external_chain,
            active_binding=external_database_audit,
        )
    contract_external = document.get("contract_external_database_audit")
    final_external = document.get("final_external_database_audit")
    external_endpoint_binding = None
    validated_contract_external = None
    validated_final_external = None
    if contract_external is not None:
        if external_database_audit is None:
            raise PullDeployError(
                "0012 external database transition lacks its bridge baseline"
            )
        validated_contract_external = validate_external_database_contract_pair(
            contract_external,
            before_binding=external_database_audit,
        )
        if (
            validated_contract_external["operation_id"]
            != document.get("last_contract_operation")
        ):
            raise PullDeployError(
                "0012 external database transition belongs to another operation"
            )
    has_0012 = "0012_drop_polytao_jobs" in history_versions
    has_0013 = "0013_monomer_dft_jobs" in history_versions
    if (has_0012 or has_0013) and (
        migration_compatibility is None
        or external_database_audit is None
        or external_chain is None
    ):
        raise PullDeployError(
            "post-0011 migration history lacks compatibility and external database provenance"
        )
    if external_database_audit is not None:
        if has_0012 != (contract_external is not None):
            raise PullDeployError(
                "external database 0012 evidence differs from migration history"
            )
        if has_0013 != (final_external is not None):
            raise PullDeployError(
                "external database 0013 evidence differs from migration history"
            )
        external_endpoint_binding = external_database_endpoint(
            external_database_audit,
            contract_pair=contract_external,
            final_pair=final_external,
        )
        external_ledger = _external_database_writable_medium(
            external_endpoint_binding
        )["ledger"]
        if external_ledger != history_ledger:
            raise PullDeployError(
                "external database endpoint differs from migration history"
            )
    elif contract_external is not None or final_external is not None:
        raise PullDeployError(
            "external database transition lacks its bridge baseline"
        )
    if final_external is not None:
        validated_final_external = validate_external_database_final_pair(
            final_external,
            before_binding=external_database_endpoint(
                external_database_audit,
                contract_pair=contract_external,
            ),
        )
        if validated_final_external["kind"] != "expand-to-0013":
            raise PullDeployError(
                "final external database transition kind differs"
            )
    contract_mutable = document.get("contract_mutable_data_audit")
    validated_contract_mutable = None
    if contract_mutable is not None:
        validated_contract_mutable = validate_mutable_data_pair(
            contract_mutable
        )
        if (
            validated_contract_mutable["transition"]["kind"]
            != "contract-0012"
            or validated_contract_mutable["transition"]["operation_id"]
            != document.get("last_contract_operation")
            or not any(
                record.get("version") == "0012_drop_polytao_jobs"
                for record in history
            )
        ):
            raise PullDeployError(
                "current 0012 mutable-data evidence differs from contract state"
            )
        mutable_stage_pairs.append(validated_contract_mutable)
    final_mutable = document.get("final_mutable_data_audit")
    final_mutable_pair = None
    if final_mutable is not None:
        final_mutable_pair = validate_mutable_data_pair(final_mutable)
        if (
            final_mutable_pair["transition"]["kind"] != "expand-0013"
            or "0013_monomer_dft_jobs" not in history_versions
        ):
            raise PullDeployError(
                "current 0013 mutable-data evidence differs from final state"
            )
        mutable_stage_pairs.append(final_mutable_pair)
    if (
        validated_contract_external is not None
        and validated_contract_mutable is not None
        and validated_contract_external["operation_id"]
        != validated_contract_mutable["transition"]["operation_id"]
    ):
        raise PullDeployError(
            "0012 external and mutable evidence belong to different operations"
        )
    if (
        validated_final_external is not None
        and final_mutable_pair is not None
        and validated_final_external["operation_id"]
        != final_mutable_pair["transition"]["operation_id"]
    ):
        raise PullDeployError(
            "0013 external and mutable evidence belong to different operations"
        )
    if (
        validated_final_external is not None
        and validated_final_external["operation_id"]
        == document["operation_id"]
        and validated_final_external["descriptor_sha256"]
        != document["descriptor_sha256"]
    ):
        raise PullDeployError(
            "0013 external evidence differs from its deployment descriptor"
        )
    rollback_provenance = document.get("rollback_provenance")
    requires_retained_0013_provenance = bool(
        migration_compatibility is not None
        and migration_compatibility["code_manifest_sha256"]
        == migration_compatibility["target_manifest_sha256"]
        and migration_compatibility["ledger_state"]["name"]
        == "post-0013"
    )
    if requires_retained_0013_provenance and rollback_provenance is None:
        raise PullDeployError(
            "B code with retained 0013 ledger lacks rollback provenance"
        )
    if rollback_provenance is not None:
        validated_rollback_provenance = validate_rollback_provenance(
            rollback_provenance,
            state=document,
            mutable_pair=mutable_pair,
            final_mutable_pair=final_mutable_pair,
            final_external_pair=validated_final_external,
            history_ledger=history_ledger,
        )
        if (
            requires_retained_0013_provenance
            != (
                validated_rollback_provenance["kind"]
                == "explicit-f-to-b-retain-0013"
            )
        ):
            raise PullDeployError(
                "rollback provenance kind differs from code/ledger state"
            )
    elif (
        mutable_pair["transition"]["operation_id"]
        != document["operation_id"]
    ):
        raise PullDeployError(
            "current mutable-data evidence belongs to another deployment"
        )
    if has_0012 != (contract_mutable is not None):
        raise PullDeployError(
            "mutable-data 0012 evidence differs from migration history"
        )
    if has_0013 != (final_mutable is not None):
        raise PullDeployError(
            "mutable-data 0013 evidence differs from migration history"
        )
    latest_mutable = max(
        mutable_stage_pairs,
        key=lambda pair: len(pair["after"]["migration_ledger"]),
    )
    if latest_mutable["after"]["migration_ledger"] != history_ledger:
        raise PullDeployError(
            "mutable-data endpoint differs from migration history"
        )
    if mutable_pair["transition"]["kind"] == "bridge-expand-to-0011":
        if (
            migration_compatibility is None
            or external_database_audit is None
            or not isinstance(external_chain, dict)
            or external_chain["bridge"]["descriptor_sha256"]
            != document["descriptor_sha256"]
            or external_chain["bridge"]["operation_id"]
            != document["operation_id"]
        ):
            raise PullDeployError(
                "bridge mutable-data evidence lacks compatibility and external chain authority"
            )
    backup = document.get("database_backup")
    if (
        not isinstance(backup, dict)
        or set(backup)
        != {
            "path",
            "sha256",
            "restore_verification",
            "mutable_data_before_sha256",
        }
        or not isinstance(backup.get("path"), str)
        or not Path(backup["path"]).is_absolute()
        or not isinstance(backup.get("restore_verification"), dict)
    ):
        raise PullDeployError("current deployment database backup identity is invalid")
    require_digest(backup.get("sha256"), "current database backup digest")
    if (
        backup.get("mutable_data_before_sha256")
        != document["mutable_data_audit"]["identity_sha256"]
    ):
        raise PullDeployError(
            "current database backup differs from mutable-data evidence"
        )
    if not isinstance(document.get("deployed_at"), str) or not document["deployed_at"]:
        raise PullDeployError("current deployment timestamp is invalid")
    try:
        core = _load_governance_core()
        core.approved_contract_migrations(document)
        core.validated_migration_epoch_barrier(document)
    except Exception as exc:
        raise PullDeployError(
            "current deployment contract approval tuple is invalid"
        ) from exc
    return document


def validate_descriptor(document: dict[str, Any]) -> dict[str, Any]:
    schema_version = document.get("schema_version")
    expected_fields = (
        DESCRIPTOR_FIELDS
        if schema_version == DESCRIPTOR_SCHEMA_VERSION
        else (
            BRIDGE_DESCRIPTOR_FIELDS
            if schema_version == BRIDGE_DESCRIPTOR_SCHEMA_VERSION
            else None
        )
    )
    if expected_fields is None or set(document) != expected_fields:
        raise PullDeployError("prepared deployment descriptor has an invalid shape")
    operation_id = require_operation_id(str(document.get("operation_id", "")))
    repository = document.get("repository")
    if not isinstance(repository, dict) or set(repository) != {
        "path",
        "remote",
        "previous_sha",
        "previous_tree",
        "target_sha",
        "target_tree",
    }:
        raise PullDeployError("descriptor repository evidence has an invalid shape")
    require_sha(repository.get("previous_sha"), "previous source SHA")
    require_sha(repository.get("previous_tree"), "previous source tree")
    require_sha(repository.get("target_sha"), "target source SHA")
    require_sha(repository.get("target_tree"), "target source tree")
    if repository.get("remote") != REPOSITORY_SSH_URL:
        raise PullDeployError("descriptor uses an unexpected repository remote")
    controller = document.get("controller")
    if not isinstance(controller, dict) or set(controller) != {
        "schema_version",
        "sha256",
        "helpers",
        "executor_control",
        "executor_control_sha256",
        "previous_active_control",
        "previous_active_control_sha256",
    }:
        raise PullDeployError("descriptor controller evidence has an invalid shape")
    if controller.get("schema_version") != CONTROLLER_SCHEMA_VERSION:
        raise PullDeployError("descriptor controller schema is unsupported")
    require_digest(controller.get("sha256"), "controller digest")
    helpers = controller.get("helpers")
    if not isinstance(helpers, dict) or set(helpers) != set(STABLE_HELPER_FILES):
        raise PullDeployError("descriptor stable helper evidence is invalid")
    for name, digest in helpers.items():
        require_digest(digest, f"stable helper {name}")
    try:
        executor = _control_runtime.validate_candidate_record(
            controller.get("executor_control")
        )
        previous_control = _control_runtime.validate_active_control_record(
            controller.get("previous_active_control")
        )
    except Exception as exc:
        raise PullDeployError("descriptor control handoff evidence is invalid") from exc
    if (
        canonical_json_digest(executor) != controller.get("executor_control_sha256")
        or canonical_json_digest(previous_control)
        != controller.get("previous_active_control_sha256")
        or executor["operation_id"] != operation_id
        or executor["source_sha"] != repository["target_sha"]
        or executor["source_tree"] != repository["target_tree"]
    ):
        raise PullDeployError("descriptor control handoff differs from repository")
    ci = document.get("ci")
    if (
        not isinstance(ci, dict)
        or (
            schema_version == DESCRIPTOR_SCHEMA_VERSION
            and ci.get("head_sha") != repository["target_sha"]
        )
        or ci.get("conclusion") != "success"
    ):
        raise PullDeployError("descriptor CI evidence is invalid")
    validate_image_records(document.get("images"), source_sha=repository["target_sha"])
    monomer = document.get("monomer_md")
    if not isinstance(monomer, dict) or set(monomer) != {
        "slot",
        "slot_record",
        "slot_record_sha256",
        "worker_env",
        "systemd_unit",
    }:
        raise PullDeployError("descriptor monomer MD evidence is invalid")
    slot_record = validate_slot_record(monomer["slot_record"], str(monomer.get("slot")))
    if worker_record_digest(slot_record) != monomer.get("slot_record_sha256"):
        raise PullDeployError("descriptor monomer MD slot digest differs")
    if slot_record["prepared_operation_id"] != operation_id:
        raise PullDeployError("descriptor monomer MD slot belongs to another operation")
    worker_env = monomer.get("worker_env")
    if not isinstance(worker_env, dict) or set(worker_env) != {
        "path",
        "sha256",
        "byteff2_python",
        "byteff2_openmm_dir",
        "gmx_sha256",
    }:
        raise PullDeployError("descriptor Worker environment evidence is invalid")
    if (
        not isinstance(worker_env["path"], str)
        or not Path(worker_env["path"]).is_absolute()
    ):
        raise PullDeployError("descriptor Worker environment path is invalid")
    require_digest(worker_env["sha256"], "Worker environment digest")
    require_digest(worker_env["gmx_sha256"], "Worker GMX digest")
    for key in ("byteff2_python", "byteff2_openmm_dir"):
        if (
            not isinstance(worker_env[key], str)
            or not Path(worker_env[key]).is_absolute()
        ):
            raise PullDeployError("descriptor Worker runtime path is invalid")
    unit = monomer.get("systemd_unit")
    if not isinstance(unit, dict) or set(unit) != {
        "source_path",
        "candidate_path",
        "target_path",
        "sha256",
        "previous_present",
        "previous_sha256",
        "previous_backup_path",
        "previous_unit_state",
        "control_release_id",
        "launcher_sha256",
    }:
        raise PullDeployError("descriptor Worker systemd unit evidence is invalid")
    if unit["source_path"] != MONOMER_MD_UNIT_SOURCE:
        raise PullDeployError("descriptor Worker unit source is invalid")
    for key in ("candidate_path", "target_path"):
        if not isinstance(unit[key], str) or not Path(unit[key]).is_absolute():
            raise PullDeployError("descriptor Worker unit path is invalid")
    require_digest(unit["sha256"], "candidate Worker unit digest")
    if (
        not isinstance(unit.get("control_release_id"), str)
        or unit["control_release_id"] != executor["release_id"]
    ):
        raise PullDeployError("descriptor Worker control release is invalid")
    require_digest(unit.get("launcher_sha256"), "descriptor Worker launcher digest")
    if not isinstance(unit["previous_present"], bool):
        raise PullDeployError("descriptor previous Worker unit state is invalid")
    if unit["previous_present"]:
        require_digest(unit["previous_sha256"], "previous Worker unit digest")
        if (
            not isinstance(unit["previous_backup_path"], str)
            or not Path(unit["previous_backup_path"]).is_absolute()
        ):
            raise PullDeployError("descriptor previous Worker unit backup is invalid")
    elif (
        unit["previous_sha256"] is not None or unit["previous_backup_path"] is not None
    ):
        raise PullDeployError(
            "descriptor absent previous Worker unit has backup metadata"
        )
    previous_unit_state = unit.get("previous_unit_state")
    if not isinstance(previous_unit_state, dict) or set(previous_unit_state) != {
        "LoadState",
        "FragmentPath",
        "DropInPaths",
        "NeedDaemonReload",
        "UnitFileState",
    }:
        raise PullDeployError("descriptor previous Worker systemd state is invalid")
    for key in ("release_input", "migrations", "compose"):
        record = document.get(key)
        if not isinstance(record, dict) or "sha256" not in record:
            raise PullDeployError(f"descriptor {key} evidence is invalid")
        require_digest(record["sha256"], f"{key} digest")
    release_input = document["release_input"]
    if set(release_input) != {
        "sha256",
        "schema_version",
        "asset_manifest_digest",
        "predecessor_asset_manifest_digest",
        "changed_asset_trees",
        "datasets_on_asset_change",
        "asset",
    }:
        raise PullDeployError("descriptor release input evidence has an invalid shape")
    validate_release_input(
        {
            key: release_input[key]
            for key in (
                "schema_version",
                "asset_manifest_digest",
                "predecessor_asset_manifest_digest",
                "changed_asset_trees",
                "datasets_on_asset_change",
            )
        }
    )
    production_config = validate_production_config_evidence(
        document.get("production_config")
    )
    mutable_data = validate_mutable_data_contract(document.get("mutable_data"))
    if (
        mutable_data["helper_sha256"]
        != production_config["deployment_mutable_data_audit_sha256"]
        or mutable_data["dependencies"]["pg_service"]["sha256"]
        != production_config["mutable_data_audit_pg_service_sha256"]
        or mutable_data["dependencies"]["pgpass"]["sha256"]
        != production_config["mutable_data_audit_pgpass_sha256"]
    ):
        raise PullDeployError(
            "mutable-data helper differs from sealed production configuration"
        )
    postgres_image = document.get("postgres_restore_image")
    if (
        not isinstance(postgres_image, dict)
        or set(postgres_image) != {"digest_ref", "image_id"}
        or postgres_image.get("digest_ref") != POSTGRES16_IMAGE
    ):
        raise PullDeployError("descriptor PostgreSQL restore image is invalid")
    require_digest(postgres_image.get("image_id"), "PostgreSQL restore image ID")
    asset = validate_asset_identity(release_input.get("asset"))
    if asset["manifest_sha256"] != release_input.get(
        "asset_manifest_digest"
    ):
        raise PullDeployError(
            "descriptor external asset digest differs from release input"
        )
    if not isinstance(document.get("prepared_at"), str) or not document["prepared_at"]:
        raise PullDeployError("descriptor has no preparation timestamp")
    previous = document.get("previous_deployment")
    previous_digest = document.get("previous_deployment_sha256")
    if previous is None:
        if previous_digest is not None:
            raise PullDeployError("descriptor absent previous deployment has a digest")
    else:
        validate_current_deployment_state(previous)
        require_digest(previous_digest, "previous deployment state digest")
        if (
            previous_control != previous["active_control"]
            or previous_control["source_sha"] != repository["previous_sha"]
            or previous_control["source_tree"] != repository["previous_tree"]
        ):
            raise PullDeployError(
                "descriptor previous control authority differs from governed state"
            )
    if schema_version == BRIDGE_DESCRIPTOR_SCHEMA_VERSION:
        if previous is not None:
            raise PullDeployError(
                "historical bridge deployment is restricted to first takeover"
            )
        takeover = validate_legacy_takeover_binding(
            document.get("legacy_takeover")
        )
        prefetch = validate_prefetch_binding(document.get("prefetch"))
        raw_bridge = document.get("bridge")
        raw_policy = (
            raw_bridge.get("policy")
            if isinstance(raw_bridge, dict)
            else None
        )
        expected_external_policy = (
            raw_policy.get("external_database_audit")
            if isinstance(raw_policy, dict)
            else None
        )
        external_database_audit = validate_external_database_audit_binding(
            document.get("external_database_audit"),
            expected_policy=expected_external_policy,
        )
        try:
            bridge = _bridge_core.validate_bridge_descriptor(
                document.get("bridge")
            )
            registry = bridge["policy"]["accepted_migration_ledgers"]
            post_0013 = next(
                record for record in registry if record["name"] == "post-0013"
            )
            _bridge_core.validate_migration_registry(
                bridge["policy"],
                target_manifest_sha256=document["migrations"]["sha256"],
                target_records=document["migrations"]["records"],
                authority_manifest_sha256=post_0013["manifest_sha256"],
                authority_records=[
                    *document["migrations"]["records"],
                    _bridge_core.FINAL_MIGRATION_RECORD,
                ],
            )
        except Exception as exc:
            raise PullDeployError("bridge descriptor evidence is invalid") from exc
        target_images = {
            role: document["images"][role]["digest_ref"]
            for role in ("backend", "web")
        }
        if (
            bridge["operation_id"] != operation_id
            or bridge["authority"]["sha"] != previous_control["source_sha"]
            or bridge["authority"]["tree"] != previous_control["source_tree"]
            or bridge["authority"]["control_release_id"]
            != previous_control["release_id"]
            or bridge["authority"]["ci_evidence_sha256"]
            != canonical_json_digest(ci)
            or ci.get("head_sha") != bridge["authority"]["sha"]
            or set(ci.get("required_jobs", []))
            != set(bridge["policy"]["required_ci_jobs"])
            or bridge["target"]["control_release_id"] != executor["release_id"]
            or bridge["target"]["sha"] != repository["target_sha"]
            or bridge["target"]["tree"] != repository["target_tree"]
            or takeover["authority_sha"] != bridge["authority"]["sha"]
            or takeover["authority_tree"] != bridge["authority"]["tree"]
            or takeover["git_identity"]["head_sha"]
            != repository["previous_sha"]
            or takeover["git_identity"]["head_tree"]
            != repository["previous_tree"]
            or prefetch["source"]["authority"]
            != {
                "sha": bridge["authority"]["sha"],
                "tree": bridge["authority"]["tree"],
            }
            or prefetch["source"]["target"]
            != {
                "sha": bridge["target"]["sha"],
                "tree": bridge["target"]["tree"],
            }
            or prefetch["policy_sha256"] != bridge["policy_sha256"]
            or bridge["target"]["images"] != target_images
            or bridge["target"]["asset_manifest_digest"]
            != document["release_input"]["asset_manifest_digest"]
            or bridge["target"]["datasets_on_asset_change"]
            != document["release_input"]["datasets_on_asset_change"]
            or external_database_audit["authority_rules"]["sha256"]
            != bridge["policy"]["external_database_audit"][
                "media_authority_rules_sha256"
            ]
        ):
            raise PullDeployError(
                "bridge descriptor differs from deployment evidence"
            )
    elif previous is None and previous_control["release_id"] != executor["release_id"]:
        raise PullDeployError(
            "bootstrap takeover controls must already be the target release"
        )
    return document


def alias_bridge_authority_projection(
    descriptor: object,
    *,
    descriptor_path: Path,
    ready_path: Path,
) -> dict[str, Any]:
    """Project the immutable part of one prepared F -> exact-B authority.

    Alias maintenance separately requires the live token to be ``prepared``.
    Excluding that mutable status lets the same recorded projection remain
    verifiable after B atomically consumes the token.
    """

    if not isinstance(descriptor, dict):
        raise PullDeployError("alias bridge descriptor is malformed")
    descriptor = validate_descriptor(descriptor)
    if descriptor["schema_version"] != BRIDGE_DESCRIPTOR_SCHEMA_VERSION:
        raise PullDeployError(
            "production alias maintenance requires bridge descriptor v3"
        )
    if (
        not descriptor_path.is_absolute()
        or not ready_path.is_absolute()
        or descriptor_path.name != "descriptor.json"
        or ready_path.name != "ready.json"
        or descriptor_path.parent != ready_path.parent
    ):
        raise PullDeployError("alias bridge evidence paths are invalid")
    ready = load_private_json(ready_path)
    descriptor_sha256 = sha256_file(descriptor_path)
    ready_sha256 = sha256_file(ready_path)
    if (
        set(ready) != READY_FIELDS
        or ready.get("schema_version") != 1
        or ready.get("status") != "ready"
        or ready.get("operation_id") != descriptor["operation_id"]
        or ready.get("source_sha")
        != descriptor["repository"]["target_sha"]
        or ready.get("descriptor_sha256") != descriptor_sha256
        or ready.get("slot_record_sha256")
        != descriptor["monomer_md"]["slot_record_sha256"]
        or ready.get("executor_control")
        != descriptor["controller"]["executor_control"]
        or ready.get("executor_control_sha256")
        != descriptor["controller"]["executor_control_sha256"]
    ):
        raise PullDeployError(
            "alias bridge READY evidence differs from descriptor"
        )
    bridge = descriptor["bridge"]
    takeover = descriptor["legacy_takeover"]
    prefetch = descriptor["prefetch"]
    body: dict[str, Any] = {
        "schema_version": 1,
        "operation_id": descriptor["operation_id"],
        "descriptor": {
            "path": str(descriptor_path),
            "sha256": descriptor_sha256,
        },
        "ready": {
            "path": str(ready_path),
            "sha256": ready_sha256,
        },
        "authority": {
            "sha": bridge["authority"]["sha"],
            "tree": bridge["authority"]["tree"],
            "control_release_id": bridge["authority"][
                "control_release_id"
            ],
        },
        "target": {
            "sha": bridge["target"]["sha"],
            "tree": bridge["target"]["tree"],
            "control_release_id": bridge["target"]["control_release_id"],
        },
        "repository_previous": {
            "sha": descriptor["repository"]["previous_sha"],
            "tree": descriptor["repository"]["previous_tree"],
        },
        "policy": {
            "id": bridge["policy"]["policy_id"],
            "sha256": bridge["policy_sha256"],
        },
        "token": dict(bridge["token"]),
        "takeover": {
            key: takeover[key]
            for key in (
                "operation_id",
                "runtime_identity_sha256",
                "pre_stopped_fence_sha256",
                "applied_record_sha256",
                "binding_sha256",
            )
        },
        "prefetch": {
            key: prefetch[key]
            for key in (
                "operation_id",
                "ready_sha256",
                "identity_sha256",
                "binding_sha256",
            )
        },
        "external_database_audit_sha256": descriptor[
            "external_database_audit"
        ]["identity_sha256"],
    }
    body["identity_sha256"] = canonical_json_digest(body)
    return body


def validate_alias_bridge_authority(
    document: object,
    *,
    descriptor: object,
    descriptor_path: Path,
    ready_path: Path,
    state_root: Path | None = None,
    current_token: object = None,
) -> dict[str, Any]:
    if (
        not isinstance(document, dict)
        or set(document) != ALIAS_BRIDGE_AUTHORITY_FIELDS
    ):
        raise PullDeployError("production alias bridge authority is malformed")
    expected = alias_bridge_authority_projection(
        descriptor,
        descriptor_path=descriptor_path,
        ready_path=ready_path,
    )
    if document == expected:
        return dict(document)
    if state_root is None or current_token is None:
        raise PullDeployError(
            "production alias belongs to another bridge authority"
        )
    recorded_descriptor = document.get("descriptor")
    recorded_ready = document.get("ready")
    recorded_operation = document.get("operation_id")
    recorded_token = document.get("token")
    recorded_policy = document.get("policy")
    if (
        not isinstance(recorded_descriptor, dict)
        or set(recorded_descriptor) != {"path", "sha256"}
        or not isinstance(recorded_ready, dict)
        or set(recorded_ready) != {"path", "sha256"}
        or not isinstance(recorded_operation, str)
        or not isinstance(recorded_token, dict)
        or set(recorded_token) != {"token_id", "token_sha256"}
        or not isinstance(recorded_policy, dict)
        or set(recorded_policy) != {"id", "sha256"}
    ):
        raise PullDeployError(
            "production alias bridge lineage authority is malformed"
        )
    original_root = (
        state_root / "prepared" / require_operation_id(recorded_operation)
    )
    original_descriptor_path = Path(
        str(recorded_descriptor.get("path"))
    )
    original_ready_path = Path(str(recorded_ready.get("path")))
    if (
        original_descriptor_path
        != original_root / "descriptor.json"
        or original_ready_path != original_root / "ready.json"
    ):
        raise PullDeployError(
            "production alias original bridge paths are invalid"
        )
    original_descriptor = load_private_json(original_descriptor_path)
    original = alias_bridge_authority_projection(
        original_descriptor,
        descriptor_path=original_descriptor_path,
        ready_path=original_ready_path,
    )
    immutable_fields = {
        "schema_version",
        "authority",
        "target",
        "repository_previous",
        "policy",
    }
    if (
        document != original
        or any(document[field] != expected[field] for field in immutable_fields)
        or document["takeover"]["runtime_identity_sha256"]
        != expected["takeover"]["runtime_identity_sha256"]
    ):
        raise PullDeployError(
            "production alias bridge successor changed immutable authority"
        )
    try:
        in_lineage = _bridge_core.token_lineage_contains(
            state_root,
            current_token,
            operation_id=recorded_operation,
            policy_id=recorded_policy["id"],
            descriptor_sha256=recorded_descriptor["sha256"],
            token_id=recorded_token["token_id"],
            token_sha256=recorded_token["token_sha256"],
        )
    except Exception as exc:
        raise PullDeployError(
            "production alias bridge retirement lineage is invalid"
        ) from exc
    if not in_lineage:
        raise PullDeployError(
            "production alias is not an ancestor of this bridge token"
        )
    return dict(document)


def validate_recovery_marker(
    marker: object,
    *,
    descriptor: dict[str, Any],
    descriptor_digest: str,
) -> dict[str, Any]:
    if not isinstance(marker, dict):
        raise PullDeployError("interrupted deployment marker is invalid")
    if not MARKER_BASE_FIELDS.issubset(marker) or not set(marker).issubset(
        MARKER_BASE_FIELDS | MARKER_OPTIONAL_FIELDS
    ):
        raise PullDeployError("interrupted deployment marker has an invalid shape")
    if (
        marker.get("schema_version") != 2
        or marker.get("action") not in {"deploy", "explicit-rollback"}
        or marker.get("operation_id") != descriptor["operation_id"]
        or marker.get("source_sha") != descriptor["repository"]["target_sha"]
        or marker.get("descriptor_sha256") != descriptor_digest
        or marker.get("executor_control")
        != descriptor["controller"]["executor_control"]
        or marker.get("executor_control_sha256")
        != descriptor["controller"]["executor_control_sha256"]
    ):
        raise PullDeployError("interrupted deployment marker identity differs")
    if marker["action"] == "deploy":
        expected_precondition = descriptor.get(
            "previous_deployment_sha256"
        )
    else:
        expected_precondition = marker.get(
            "rollback_current_state_sha256"
        )
    # Older schema-v2 recovery markers predate the redundant adjacent-CAS
    # field and remain recoverable because the immutable descriptor (or the
    # explicit rollback source digest) already seals the same precondition.
    # Every newly-created marker writes the field; when present it must agree.
    precondition = marker.get(
        "current_state_precondition_sha256", expected_precondition
    )
    if (
        "current_state_precondition_sha256" in marker
        and precondition != expected_precondition
    ):
        raise PullDeployError(
            "interrupted deployment current-state precondition differs"
        )
    if precondition is not None:
        require_digest(
            precondition,
            "interrupted deployment current-state precondition",
        )
    for field in (
        "runtime_stopped",
        "source_switched",
        "slot_switched",
        "control_switched",
        "unit_switched",
        "asset_switched",
        "database_change_started",
    ):
        if not isinstance(marker.get(field), bool):
            raise PullDeployError(
                "interrupted deployment marker effect flag is invalid"
            )
    for field in ("database_restore_started", "database_restored"):
        if field in marker and not isinstance(marker[field], bool):
            raise PullDeployError("interrupted deployment restore flag is invalid")
    restore_started = marker.get("database_restore_started")
    database_restored = marker.get("database_restored")
    phase = marker.get("phase")
    if database_restored is True and restore_started is not True:
        raise PullDeployError(
            "interrupted deployment restore commit lacks intent"
        )
    if phase == "database-restore-started" and (
        restore_started is not True or database_restored is True
    ):
        raise PullDeployError(
            "interrupted deployment restore-started phase is inconsistent"
        )
    if phase == "database-restored" and database_restored is not True:
        raise PullDeployError(
            "interrupted deployment restored phase lacks proof"
        )
    if database_restored is True:
        restore = marker.get("database_restore")
        if (
            not isinstance(restore, dict)
            or restore.get("restored") is not True
            or marker.get("mutable_data_restored") is None
        ):
            raise PullDeployError(
                "interrupted deployment restored evidence is incomplete"
            )
        validate_mutable_data_pair(marker["mutable_data_restored"])
    if "pre_stop_abort" in marker and marker["pre_stop_abort"] is not True:
        raise PullDeployError("interrupted deployment pre-stop abort flag is invalid")
    if "takeover_pre_stopped_fence_sha256" in marker:
        if (
            descriptor.get("schema_version")
            != BRIDGE_DESCRIPTOR_SCHEMA_VERSION
            or marker["takeover_pre_stopped_fence_sha256"]
            != descriptor["legacy_takeover"][
                "pre_stopped_fence_sha256"
            ]
        ):
            raise PullDeployError(
                "interrupted takeover stop fence differs"
            )
    if "takeover_restore_started" in marker:
        restore = marker["takeover_restore_started"]
        if (
            descriptor.get("schema_version")
            != BRIDGE_DESCRIPTOR_SCHEMA_VERSION
            or not isinstance(restore, dict)
            or set(restore)
            != {
                "operation_id",
                "worker_unit_sha256",
                "control_layout_sha256",
                "checkout_permissions_sha256",
                "started_at",
            }
            or restore.get("operation_id")
            != descriptor["legacy_takeover"]["operation_id"]
            or not isinstance(restore.get("started_at"), str)
            or not restore["started_at"]
        ):
            raise PullDeployError(
                "interrupted takeover restore intent is invalid"
            )
        for name in (
            "worker_unit_sha256",
            "control_layout_sha256",
            "checkout_permissions_sha256",
        ):
            require_digest(
                restore.get(name),
                f"takeover restore {name}",
            )
    if "takeover_restored_terminal_sha256" in marker:
        require_digest(
            marker["takeover_restored_terminal_sha256"],
            "takeover restored terminal digest",
        )
    if "bridge_recovery_capsule" in marker:
        capsule = marker["bridge_recovery_capsule"]
        if (
            descriptor.get("schema_version")
            != BRIDGE_DESCRIPTOR_SCHEMA_VERSION
            or not isinstance(capsule, dict)
            or set(capsule)
            != {
                "capsule_sha256",
                "descriptor_sha256",
                "control_release_id",
                "recovery_entry_sha256",
            }
            or capsule.get("descriptor_sha256") != descriptor_digest
            or capsule.get("control_release_id")
            != descriptor["controller"]["executor_control"]["release_id"]
        ):
            raise PullDeployError(
                "interrupted bridge recovery capsule binding differs"
            )
        for name in ("capsule_sha256", "recovery_entry_sha256"):
            require_digest(
                capsule.get(name), f"bridge recovery capsule {name}"
            )
    if "runtime_start_intent" in marker:
        intent = marker["runtime_start_intent"]
        if (
            not isinstance(intent, dict)
            or set(intent) != {"target_sha", "recorded_at"}
            or intent.get("target_sha")
            not in {
                descriptor["repository"]["target_sha"],
                descriptor["repository"]["previous_sha"],
            }
            or not isinstance(intent.get("recorded_at"), str)
            or not intent["recorded_at"]
        ):
            raise PullDeployError("interrupted runtime start intent is invalid")
    if not isinstance(marker.get("started_at"), str) or not marker["started_at"]:
        raise PullDeployError("interrupted deployment marker timestamp is invalid")
    if not isinstance(marker.get("updated_at"), str) or not marker["updated_at"]:
        raise PullDeployError("interrupted deployment marker timestamp is invalid")
    phase = marker.get("phase")
    phases = (
        DEPLOY_MARKER_PHASES if marker["action"] == "deploy" else ROLLBACK_MARKER_PHASES
    )
    if phase not in phases:
        raise PullDeployError("interrupted deployment marker phase is invalid")
    if marker["action"] == "explicit-rollback":
        require_digest(
            marker.get("rollback_current_state_sha256"),
            "explicit rollback current-state digest",
        )
        require_digest(
            marker.get("rollback_source_terminal_audit_sha256"),
            "explicit rollback source terminal audit digest",
        )
        backup_operation_id = require_operation_id(
            str(marker.get("rollback_backup_operation_id", ""))
        )
        rollback_attempt_id = require_operation_id(
            str(marker.get("rollback_attempt_id", ""))
        )
        if not rollback_attempt_id.startswith("rollback-attempt-"):
            raise PullDeployError(
                "explicit rollback attempt identity is invalid"
            )
        if backup_operation_id == descriptor["operation_id"]:
            raise PullDeployError(
                "explicit rollback backup authority is not independent"
            )
        if phase != "explicit-rollback-started" and not isinstance(
            marker.get("drain"), dict
        ):
            raise PullDeployError(
                "explicit rollback transition lacks durable drain evidence"
            )
    elif "rollback_current_state_sha256" in marker:
        raise PullDeployError("deploy marker contains explicit rollback authority")
    candidate = marker.get("candidate_state")
    candidate_digest = marker.get("candidate_state_sha256")
    if (candidate is None) != (candidate_digest is None):
        raise PullDeployError("deployment marker has incomplete candidate state intent")
    if candidate is not None:
        if marker["action"] != "deploy" or not isinstance(candidate, dict):
            raise PullDeployError("deployment marker candidate state is invalid")
        validate_current_deployment_state(candidate)
        require_digest(candidate_digest, "deployment marker candidate-state digest")
    rollback_candidate = marker.get("rollback_candidate_state")
    rollback_candidate_digest = marker.get("rollback_candidate_state_sha256")
    if (rollback_candidate is None) != (rollback_candidate_digest is None):
        raise PullDeployError(
            "explicit rollback marker has incomplete candidate-state intent"
        )
    if rollback_candidate is not None:
        if marker["action"] != "explicit-rollback" or not isinstance(
            rollback_candidate, dict
        ):
            raise PullDeployError("explicit rollback candidate state is invalid")
        validate_current_deployment_state(rollback_candidate)
        expected_rollback_digest = require_digest(
            rollback_candidate_digest,
            "explicit rollback candidate-state digest",
        )
        if (
            sha256_bytes(canonical_json_bytes(rollback_candidate) + b"\n")
            != expected_rollback_digest
        ):
            raise PullDeployError(
                "explicit rollback candidate-state digest differs"
            )
    if "postgres_runtime_fence" in marker:
        validate_postgres_runtime_fence(marker["postgres_runtime_fence"])
    return marker


class Lifecycle(Protocol):
    def postgres_runtime_identity(
        self, controller: "PullDeployController", descriptor: dict[str, Any]
    ) -> dict[str, Any] | None: ...

    def drain(
        self, controller: "PullDeployController", descriptor: dict[str, Any]
    ) -> dict[str, Any]: ...

    def ensure_candidate_drained(
        self, controller: "PullDeployController", descriptor: dict[str, Any]
    ) -> dict[str, Any]: ...

    def backup(
        self, controller: "PullDeployController", descriptor: dict[str, Any]
    ) -> dict[str, Any]: ...

    def backup_rollback(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
        backup_operation_id: str,
    ) -> dict[str, Any]: ...

    def stop(
        self, controller: "PullDeployController", descriptor: dict[str, Any]
    ) -> dict[str, Any] | None: ...

    def restore_database(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
        backup: dict[str, Any],
    ) -> dict[str, Any]: ...

    def migrate(
        self, controller: "PullDeployController", descriptor: dict[str, Any]
    ) -> dict[str, Any]: ...

    def start(
        self, controller: "PullDeployController", descriptor: dict[str, Any]
    ) -> None: ...

    def verify(
        self, controller: "PullDeployController", descriptor: dict[str, Any]
    ) -> dict[str, Any]: ...

    def resume(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
        expected_verification: dict[str, Any],
    ) -> None: ...

    def resume_unchanged(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
        persist_verification: Callable[[dict[str, Any]], None],
        expected_verification: dict[str, Any] | None = None,
    ) -> None: ...

    def resume_bootstrap_unchanged(
        self, controller: "PullDeployController", descriptor: dict[str, Any]
    ) -> None: ...

    def bootstrap_can_resume_unchanged(
        self, controller: "PullDeployController", descriptor: dict[str, Any]
    ) -> bool: ...

    def admission_is_open(
        self, controller: "PullDeployController", descriptor: dict[str, Any]
    ) -> bool: ...

    def verify_open_runtime(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
        expected_verification: dict[str, Any],
    ) -> None: ...

    def prepare_recovery_runtime(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
        expected_verification: dict[str, Any] | None,
        *,
        allow_unfenced: bool,
    ) -> dict[str, Any]: ...


class SystemLifecycle:
    @staticmethod
    def _docker_exec(
        container_id: str,
        *arguments: str,
        interactive: bool = False,
    ) -> list[str]:
        """Address one already-fenced container without mutable Compose lookup."""

        if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
            raise PullDeployError("Docker exec target is not an exact container ID")
        return [
            "docker",
            "exec",
            *(["--interactive"] if interactive else []),
            container_id,
            *arguments,
        ]

    @staticmethod
    def _drain_authority_sha(descriptor: dict[str, Any]) -> str:
        """Return the owner token of the persistent Backend drain.

        Rollback projects previous runtime images/source into a candidate
        deployment attempt, but the drain was acquired by that candidate
        attempt.  Keeping this private authority separate prevents a previous
        source SHA from trying to steal or disable another owner's drain.
        """

        value = descriptor.get(
            "_drain_authority_sha", descriptor["repository"]["target_sha"]
        )
        return require_sha(value, "deployment drain authority SHA")

    @staticmethod
    def _validate_isolated_container(
        record: object,
        *,
        name: str,
        image: str,
        operation_label: str,
        operation_id: str,
        tmpfs_capacity: int | None,
    ) -> str:
        """Prove exact ownership of one network-isolated smoke container."""

        if not isinstance(record, dict):
            raise PullDeployError("isolated container inspection is malformed")
        container_id = record.get("Id")
        config = record.get("Config")
        host = record.get("HostConfig")
        network = record.get("NetworkSettings")
        mounts = record.get("Mounts")
        if (
            not isinstance(container_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", container_id) is None
            or record.get("Name") != f"/{name}"
            or not isinstance(config, dict)
            or not isinstance(host, dict)
            or not isinstance(network, dict)
            or not isinstance(mounts, list)
            or config.get("Image") != image
        ):
            raise PullDeployError("isolated container has foreign identity")
        labels = config.get("Labels")
        if not isinstance(labels, dict) or labels.get(operation_label) != operation_id:
            raise PullDeployError("isolated container has foreign operation authority")
        operation_labels = {
            "com.nexpoly.restore-operation",
            "com.nexpoly.deploy-operation",
            "com.nexpoly.contract-restore-operation",
        }
        if any(
            key in labels and (key != operation_label or labels[key] != operation_id)
            for key in operation_labels
        ):
            raise PullDeployError("isolated container has conflicting operation labels")

        restart = host.get("RestartPolicy")
        no_restart = (
            isinstance(restart, dict)
            and restart.get("Name") in {"", "no"}
            and restart.get("MaximumRetryCount") in {None, 0}
        )
        empty_host_fields = (
            "Binds",
            "PortBindings",
            "Devices",
            "DeviceRequests",
            "CapAdd",
            "CapDrop",
            "SecurityOpt",
            "Links",
            "ExtraHosts",
        )
        zero_resource_fields = (
            "Memory",
            "MemoryReservation",
            "NanoCpus",
            "CpuShares",
            "CpuPeriod",
            "CpuQuota",
            "PidsLimit",
        )
        if (
            host.get("NetworkMode") != "none"
            or not no_restart
            or host.get("AutoRemove") not in {None, False}
            or host.get("Privileged") not in {None, False}
            or host.get("PublishAllPorts") not in {None, False}
            or any(host.get(field) not in (None, [], {}) for field in empty_host_fields)
            or any(
                host.get(field) not in (None, 0, "") for field in zero_resource_fields
            )
        ):
            raise PullDeployError("isolated container has unexpected host resources")

        networks = network.get("Networks")
        ports = network.get("Ports")
        if not isinstance(networks, dict) or set(networks) != {"none"}:
            raise PullDeployError(
                "isolated container is not attached only to network none"
            )
        none_network = networks["none"]
        if not isinstance(none_network, dict) or any(
            none_network.get(field) not in {None, "", 0}
            for field in (
                "Gateway",
                "IPAddress",
                "IPPrefixLen",
                "IPv6Gateway",
                "GlobalIPv6Address",
                "GlobalIPv6PrefixLen",
            )
        ):
            raise PullDeployError("isolated container has an assigned network address")
        if ports not in (None, {}) and (
            not isinstance(ports, dict)
            or any(value not in (None, []) for value in ports.values())
        ):
            raise PullDeployError("isolated container publishes a port")

        tmpfs = host.get("Tmpfs")
        if tmpfs_capacity is None:
            if tmpfs not in (None, {}) or mounts:
                raise PullDeployError("isolated Web container has unexpected mounts")
        else:
            expected_destination = "/var/lib/postgresql/data"
            if not isinstance(tmpfs, dict) or set(tmpfs) != {expected_destination}:
                raise PullDeployError("isolated restore tmpfs mapping is invalid")
            raw_options = tmpfs[expected_destination]
            if not isinstance(raw_options, str):
                raise PullDeployError("isolated restore tmpfs options are invalid")
            options = {value.strip().lower() for value in raw_options.split(",")}
            if options != {"rw", "nosuid", "nodev", f"size={tmpfs_capacity}"}:
                raise PullDeployError("isolated restore tmpfs options differ")
            if len(mounts) != 1:
                raise PullDeployError("isolated restore has unexpected mounts")
            mount = mounts[0]
            if (
                not isinstance(mount, dict)
                or mount.get("Type") != "tmpfs"
                or mount.get("Destination") != expected_destination
                or mount.get("RW") is not True
            ):
                raise PullDeployError("isolated restore tmpfs mount differs")

        environment = config.get("Env")
        if tmpfs_capacity is not None:
            if not isinstance(environment, list) or [
                value
                for value in environment
                if isinstance(value, str)
                and value.startswith("POSTGRES_HOST_AUTH_METHOD=")
            ] != ["POSTGRES_HOST_AUTH_METHOD=trust"]:
                raise PullDeployError(
                    "isolated restore authentication environment differs"
                )
        return container_id

    @staticmethod
    def _remove_container_and_prove_absent(
        controller: "PullDeployController",
        name: str,
        *,
        container_id: str,
        label: str,
    ) -> None:
        removal_error: BaseException | None = None
        try:
            controller.runner.run(
                ["docker", "rm", "--force", container_id],
                env=controller.control_environment(),
                check=False,
            )
        except BaseException as exc:
            removal_error = exc
        try:
            absent = controller.runner.run(
                ["docker", "container", "inspect", name],
                env=controller.control_environment(),
                check=False,
            )
        except BaseException as exc:
            raise PullDeployError(f"cannot prove {label} container cleanup") from (
                removal_error or exc
            )
        if absent.returncode == 1:
            return
        if absent.returncode == 0:
            raise PullDeployError(f"{label} container still exists after cleanup")
        raise PullDeployError(
            f"cannot prove {label} container cleanup"
        ) from removal_error

    """Fixed production actions; tests replace this object, not shell strings."""

    def _compose(
        self, controller: "PullDeployController", *arguments: str
    ) -> list[str]:
        return [
            "docker",
            "compose",
            "-p",
            "nexpoly",
            "-f",
            str(controller.production_root / "docker-compose.yml"),
            "-f",
            str(controller.production_root / "docker-compose.prod.yml"),
            "--env-file",
            str(controller.config_dir / "deploy.env"),
            *arguments,
        ]

    def postgres_runtime_identity(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
    ) -> dict[str, Any]:
        """Capture the exact running PostgreSQL container and data authority."""

        environment = self._environment(controller, descriptor)
        listed = controller.runner.run(
            self._compose(controller, "ps", "--quiet", "lab-postgres"),
            cwd=controller.production_root,
            env=environment,
        )
        identities = [value for value in str(listed.stdout).splitlines() if value]
        if len(identities) != 1 or re.fullmatch(r"[0-9a-f]{64}", identities[0]) is None:
            raise PullDeployError("PostgreSQL container identity is unavailable")
        inspected = controller.runner.run(
            ["docker", "container", "inspect", identities[0]],
            env=controller.control_environment(),
        )
        try:
            records = json.loads(str(inspected.stdout))
            container = records[0]
        except (json.JSONDecodeError, IndexError, TypeError) as exc:
            raise PullDeployError("PostgreSQL container inspection is malformed") from exc
        config = container.get("Config") if isinstance(container, dict) else None
        state = container.get("State") if isinstance(container, dict) else None
        labels = config.get("Labels") if isinstance(config, dict) else None
        mounts = container.get("Mounts") if isinstance(container, dict) else None
        network = (
            container.get("NetworkSettings") if isinstance(container, dict) else None
        )
        ports = network.get("Ports") if isinstance(network, dict) else None
        published = ports.get("5432/tcp") if isinstance(ports, dict) else None
        data_mounts = (
            [
                value
                for value in mounts
                if isinstance(value, dict)
                and value.get("Destination") == "/var/lib/postgresql/data"
            ]
            if isinstance(mounts, list)
            else []
        )
        if (
            not isinstance(container, dict)
            or container.get("Id") != identities[0]
            or not isinstance(state, dict)
            or state.get("Running") is not True
            or not isinstance(config, dict)
            or not isinstance(config.get("Image"), str)
            or not config["Image"]
            or not isinstance(labels, dict)
            or labels.get("com.docker.compose.project") != "nexpoly"
            or labels.get("com.docker.compose.service") != "lab-postgres"
            or len(data_mounts) != 1
            or published
            != [{"HostIp": MUTABLE_DATA_HOST, "HostPort": str(MUTABLE_DATA_PORT)}]
        ):
            raise PullDeployError("PostgreSQL container runtime identity differs")
        mount = data_mounts[0]
        if (
            mount.get("Type") != "volume"
            or not isinstance(mount.get("Name"), str)
            or not mount["Name"]
            or not isinstance(mount.get("Source"), str)
            or not Path(mount["Source"]).is_absolute()
            or not isinstance(mount.get("Driver"), str)
            or not mount["Driver"]
            or mount.get("RW") is not True
        ):
            raise PullDeployError("PostgreSQL data volume identity is unsafe")
        image_id = container.get("Image")
        require_digest(image_id, "PostgreSQL running image ID")
        values = controller.production_deploy_values(check_free_space=False)
        system = controller.runner.run(
            self._compose(
                controller,
                "exec",
                "-T",
                "lab-postgres",
                "psql",
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                "--username",
                values["NEXPOLY_POSTGRES_USER"],
                "--dbname",
                values["NEXPOLY_POSTGRES_DB"],
                "--command",
                "SELECT system_identifier::text FROM pg_control_system()",
            ),
            cwd=controller.production_root,
            env=environment,
        )
        system_identifier = str(system.stdout).strip()
        return validate_postgres_runtime_fence(
            {
                "schema_version": 1,
                "container_id": identities[0],
                "image_id": image_id,
                "configured_image": config["Image"],
                "data_volume": {
                    "type": "volume",
                    "name": mount["Name"],
                    "source": mount["Source"],
                    "destination": mount["Destination"],
                    "driver": mount["Driver"],
                    "read_write": True,
                },
                "host_endpoint": {
                    "host": MUTABLE_DATA_HOST,
                    "port": MUTABLE_DATA_PORT,
                    "container_port": 5432,
                    "protocol": "tcp",
                },
                "system_identifier": system_identifier,
                "captured_at": utc_now(),
            }
        )

    @staticmethod
    def _sealed_postgres_runtime_fence(
        controller: "PullDeployController",
    ) -> dict[str, Any]:
        marker = load_private_json(controller.marker_path)
        return validate_postgres_runtime_fence(
            marker.get("postgres_runtime_fence")
        )

    def _assert_sealed_postgres_runtime(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
        *,
        expected: dict[str, Any] | None = None,
        phase: str,
    ) -> dict[str, Any]:
        """CAS the live PostgreSQL runtime against the durable deployment fence."""

        if expected is None:
            contract_identity = descriptor.get(
                "_expected_postgres_runtime_identity"
            )
            if contract_identity is None:
                sealed_identity = postgres_runtime_fence_identity(
                    self._sealed_postgres_runtime_fence(controller)
                )
            elif isinstance(contract_identity, dict):
                sealed_identity = dict(contract_identity)
            else:
                raise PullDeployError(
                    "sealed PostgreSQL contract identity is invalid"
                )
        elif "captured_at" in expected:
            sealed_identity = postgres_runtime_fence_identity(
                validate_postgres_runtime_fence(expected)
            )
        else:
            sealed_identity = dict(expected)
        observed = validate_postgres_runtime_fence(
            self.postgres_runtime_identity(controller, descriptor)
        )
        observed_identity = postgres_runtime_fence_identity(observed)
        if (
            set(sealed_identity) != set(observed_identity)
            or observed_identity != sealed_identity
        ):
            raise PullDeployError(f"PostgreSQL identity changed {phase}")
        return sealed_identity

    def _environment(
        self, controller: "PullDeployController", descriptor: dict[str, Any]
    ) -> dict[str, str]:
        values = controller.production_deploy_values(check_free_space=False)
        environment = dict(values)
        # Host control fields always win over business configuration even
        # after the explicit redirect-variable rejection above.
        environment.update(controller.control_environment())
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

    def _run_bootstrap_hook(
        self,
        controller: "PullDeployController",
        name: str,
        expected_config: dict[str, Any],
    ) -> dict[str, Any]:
        if (
            controller.production_config_evidence(check_free_space=False)
            != expected_config
        ):
            raise PullDeployError("bootstrap hook configuration changed after prepare")
        hook = controller.config_dir / name
        metadata = hook.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or hook.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise PullDeployError(f"bootstrap hook is unsafe: {hook}")
        environment = controller.control_environment()
        if name in {
            "bootstrap-rollback",
            "bootstrap-resume-unchanged",
            "bootstrap-status",
        }:
            # Pass only the reviewed, non-secret identity pin required by the
            # rollback hook.  Never export the complete deploy.env into a
            # site-specific executable.
            values = controller.production_deploy_values(check_free_space=False)
            environment["NEXPOLY_BOOTSTRAP_LEGACY_RUNTIME_SHA256"] = require_digest(
                values.get("NEXPOLY_BOOTSTRAP_LEGACY_RUNTIME_SHA256"),
                "bootstrap legacy runtime digest",
            )
        result = controller.runner.run(
            [str(hook)],
            cwd=controller.production_root,
            env=environment,
            text=True,
        )
        try:
            payload = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise PullDeployError(
                f"bootstrap hook returned invalid evidence: {name}"
            ) from exc
        if not isinstance(payload, dict):
            raise PullDeployError(f"bootstrap hook returned invalid evidence: {name}")
        if name == "bootstrap-rollback":
            expected_fields = {
                "schema_version",
                "legacy_runtime_restored",
                "backend_image_id",
                "web_image_id",
                "worker_unit_sha256",
                "backend_healthy",
                "web_healthy",
                "worker_healthy",
                "ingress_restored",
            }
            if set(payload) != expected_fields or payload.get("schema_version") != 1:
                raise PullDeployError(
                    "bootstrap rollback evidence has an invalid shape"
                )
            identity: dict[str, str] = {}
            for key in (
                "backend_image_id",
                "web_image_id",
                "worker_unit_sha256",
            ):
                identity[key] = require_digest(
                    payload.get(key), f"bootstrap rollback {key}"
                )
            values = controller.production_deploy_values(check_free_space=False)
            expected = require_digest(
                values.get("NEXPOLY_BOOTSTRAP_LEGACY_RUNTIME_SHA256"),
                "bootstrap legacy runtime digest",
            )
            if canonical_json_digest(identity) != expected:
                raise PullDeployError(
                    "bootstrap rollback restored a different legacy runtime identity"
                )
            for key in (
                "legacy_runtime_restored",
                "backend_healthy",
                "web_healthy",
                "worker_healthy",
                "ingress_restored",
            ):
                if payload.get(key) is not True:
                    raise PullDeployError(f"bootstrap rollback did not prove {key}")
        return payload

    def _control_cli(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
        *arguments: str,
    ) -> dict[str, Any]:
        environment = self._environment(controller, descriptor)
        result = controller.runner.run(
            self._compose(
                controller,
                "run",
                "--rm",
                "--no-deps",
                "postgres-init",
                "python",
                "-m",
                "app.deployment_control_cli",
                *arguments,
            ),
            cwd=controller.production_root,
            env=environment,
        )
        return parse_command_json(result.stdout, "Backend deployment control")

    def _backend_active_status(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
    ) -> dict[str, Any]:
        probe = (
            "import json,urllib.request;"
            "data=json.load(urllib.request.urlopen("
            "'http://127.0.0.1:8000/internal/deployment/status',timeout=10));"
            "print(json.dumps(data,separators=(',',':')))"
        )
        result = controller.runner.run(
            self._compose(
                controller,
                "exec",
                "-T",
                "backend",
                "python",
                "-I",
                "-c",
                probe,
            ),
            cwd=controller.production_root,
            env=self._environment(controller, descriptor),
        )
        return parse_command_json(result.stdout, "Backend active-job status")

    def _backend_process_identity(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
    ) -> dict[str, Any]:
        environment = self._environment(controller, descriptor)
        result = controller.runner.run(
            self._compose(controller, "ps", "--quiet", "backend"),
            cwd=controller.production_root,
            env=environment,
        )
        identities = [value for value in str(result.stdout).splitlines() if value]
        if len(identities) != 1:
            raise PullDeployError("governed Backend process identity is ambiguous")
        inspected = controller.runner.run(
            ["docker", "container", "inspect", identities[0]],
            env=controller.control_environment(),
        )
        try:
            container = json.loads(str(inspected.stdout))[0]
            state = container["State"]
            identity = {
                "container_id": container["Id"],
                "image_id": container["Image"],
                "pid": state["Pid"],
                "started_at": state["StartedAt"],
                "restart_count": container["RestartCount"],
            }
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise PullDeployError(
                "governed Backend process evidence is malformed"
            ) from exc
        if (
            container.get("Id") != identities[0]
            or state.get("Running") is not True
            or identity["image_id"] != descriptor["images"]["backend"]["image_id"]
            or not isinstance(identity["pid"], int)
            or isinstance(identity["pid"], bool)
            or identity["pid"] <= 0
            or not isinstance(identity["started_at"], str)
            or not identity["started_at"]
            or not isinstance(identity["restart_count"], int)
            or isinstance(identity["restart_count"], bool)
            or identity["restart_count"] < 0
        ):
            raise PullDeployError("governed Backend process identity differs")
        return identity

    def _worker_process_identity(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
    ) -> dict[str, Any]:
        shown = controller.runner.run(
            [
                "systemctl",
                "--user",
                "show",
                MONOMER_MD_UNIT_NAME,
                "--property=ActiveState",
                "--property=SubState",
                "--property=MainPID",
                "--property=InvocationID",
                "--property=ActiveEnterTimestampMonotonic",
            ],
            env=self._environment(controller, descriptor),
        )
        fields = dict(
            line.split("=", 1) for line in str(shown.stdout).splitlines() if "=" in line
        )
        expected = {
            "ActiveState",
            "SubState",
            "MainPID",
            "InvocationID",
            "ActiveEnterTimestampMonotonic",
        }
        try:
            identity = {
                "main_pid": int(fields["MainPID"]),
                "invocation_id": fields["InvocationID"],
                "active_enter_monotonic": int(fields["ActiveEnterTimestampMonotonic"]),
            }
        except (KeyError, ValueError) as exc:
            raise PullDeployError("Worker process evidence is malformed") from exc
        if (
            set(fields) != expected
            or fields["ActiveState"] != "active"
            or fields["SubState"] != "running"
            or identity["main_pid"] <= 0
            or not identity["invocation_id"]
            or identity["active_enter_monotonic"] <= 0
        ):
            raise PullDeployError("Worker process is not the active unchanged instance")
        return identity

    def admission_is_open(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
    ) -> bool:
        evidence = validate_persistent_drain_evidence(
            self._control_cli(controller, descriptor, "status"),
            authority_sha=self._drain_authority_sha(descriptor),
        )
        return evidence["drain"]["enabled"] is False

    def _capture_runtime_recovery_fence(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
        *,
        resumed: bool | None,
    ) -> dict[str, Any]:
        sockets = self._worker_sockets(controller, require_md=True)
        workers: dict[str, dict[str, str]] = {}
        for name, socket in sockets:
            health = self._worker_request(
                controller, socket, method="GET", endpoint="/health"
            )
            if name == "monomer-md":
                expected_accepting: bool | None
                allow_active: bool
                if resumed is None:
                    draining = health.get("draining")
                    if not isinstance(draining, bool):
                        raise PullDeployError(
                            "runtime recovery Worker drain state is invalid"
                        )
                    expected_accepting = False if draining else None
                    allow_active = True
                else:
                    expected_accepting = None if resumed else False
                    allow_active = resumed
                self._validate_worker_runtime_identity(
                    controller,
                    descriptor,
                    health,
                    expected_accepting=expected_accepting,
                    allow_active=allow_active,
                )
            else:
                if resumed is None:
                    draining = health.get("draining")
                    if not isinstance(draining, bool):
                        raise PullDeployError(f"{name} Worker drain state is invalid")
                    action = "health-drained" if draining else "health-resumed"
                else:
                    action = "health-resumed" if resumed else "health-drained"
                validate_worker_control_evidence(
                    health,
                    action=action,
                    require_zero=resumed is False,
                )
            workers[name] = {
                "socket": str(socket),
                "worker_instance_id": health["worker_instance_id"],
            }
        return {
            "backend_process": self._backend_process_identity(controller, descriptor),
            "monomer_md_process": self._worker_process_identity(controller, descriptor),
            "workers": workers,
        }

    @staticmethod
    def _expected_runtime_recovery_fence(
        verification: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not isinstance(verification, dict):
            raise PullDeployError(
                "committed runtime lacks verification recovery evidence"
            )
        fence = verification.get("recovery_fence")
        if (
            not isinstance(fence, dict)
            or set(fence) != {"backend_process", "monomer_md_process", "workers"}
            or not isinstance(fence.get("backend_process"), dict)
            or not isinstance(fence.get("monomer_md_process"), dict)
            or not isinstance(fence.get("workers"), dict)
            or "monomer-md" not in fence["workers"]
        ):
            raise PullDeployError("runtime recovery fence has an invalid shape")
        backend = fence["backend_process"]
        monomer_md = fence["monomer_md_process"]
        workers = fence["workers"]
        if (
            set(backend)
            != {"container_id", "image_id", "pid", "started_at", "restart_count"}
            or not isinstance(backend.get("container_id"), str)
            or re.fullmatch(r"[0-9a-f]{64}", backend["container_id"]) is None
            or not isinstance(backend.get("image_id"), str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", backend["image_id"]) is None
            or not isinstance(backend.get("pid"), int)
            or isinstance(backend.get("pid"), bool)
            or backend["pid"] <= 0
            or not isinstance(backend.get("started_at"), str)
            or not backend["started_at"]
            or not isinstance(backend.get("restart_count"), int)
            or isinstance(backend.get("restart_count"), bool)
            or backend["restart_count"] < 0
            or set(monomer_md)
            != {"main_pid", "invocation_id", "active_enter_monotonic"}
            or not isinstance(monomer_md.get("main_pid"), int)
            or isinstance(monomer_md.get("main_pid"), bool)
            or monomer_md["main_pid"] <= 0
            or not isinstance(monomer_md.get("invocation_id"), str)
            or not monomer_md["invocation_id"]
            or not isinstance(monomer_md.get("active_enter_monotonic"), int)
            or isinstance(monomer_md.get("active_enter_monotonic"), bool)
            or monomer_md["active_enter_monotonic"] <= 0
        ):
            raise PullDeployError("runtime recovery process fence is invalid")
        for name, worker in workers.items():
            if (
                name not in {"monomer-md", "monomer-dft"}
                or not isinstance(worker, dict)
                or set(worker) != {"socket", "worker_instance_id"}
                or not isinstance(worker.get("socket"), str)
                or not Path(worker["socket"]).is_absolute()
                or not isinstance(worker.get("worker_instance_id"), str)
                or not worker["worker_instance_id"]
            ):
                raise PullDeployError("runtime recovery Worker fence is invalid")
        return fence

    def _isolate_ingress(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
    ) -> None:
        """Idempotently remove public ingress before recovery mutates runtime."""

        environment = self._environment(controller, descriptor)
        stop_error: BaseException | None = None
        try:
            controller.runner.run(
                self._compose(controller, "stop", "nginx"),
                cwd=controller.production_root,
                env=environment,
            )
        except BaseException as exc:
            # A lost Docker response is an unknown commit.  The absence probe
            # below, rather than the response, determines whether isolation
            # completed.
            stop_error = exc
        try:
            ingress = controller.runner.run(
                self._compose(controller, "ps", "--quiet", "nginx"),
                cwd=controller.production_root,
                env=environment,
            )
        except BaseException as exc:
            raise PullDeployError(
                "cannot prove Web ingress isolation during recovery"
            ) from (stop_error or exc)
        if str(ingress.stdout).strip():
            raise PullDeployError(
                "Web ingress remains active during runtime recovery"
            ) from stop_error

    def _recovery_runtime_presence(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
    ) -> str:
        """Classify source-reading runtime processes without starting them."""

        environment = self._environment(controller, descriptor)
        backend = controller.runner.run(
            self._compose(controller, "ps", "--quiet", "backend"),
            cwd=controller.production_root,
            env=environment,
        )
        backend_ids = [value for value in str(backend.stdout).splitlines() if value]
        if len(backend_ids) > 1:
            raise PullDeployError(
                "multiple Backend processes exist during runtime recovery"
            )
        worker = controller.runner.run(
            [
                "systemctl",
                "--user",
                "is-active",
                "nexpoly-monomer-md-worker.service",
            ],
            env=environment,
            check=False,
        )
        worker_state = str(worker.stdout).strip()
        backend_live = len(backend_ids) == 1
        worker_live = worker.returncode == 0 and worker_state == "active"
        worker_stopped = worker.returncode in {3, 4} and worker_state in {
            "inactive",
            "unknown",
        }
        if not worker_live and not worker_stopped:
            raise PullDeployError(
                "Worker process state is unknown during runtime recovery"
            )
        if backend_live and worker_live:
            return "live"
        if not backend_live and worker_stopped:
            return "stopped"
        return "partial"

    def prepare_recovery_runtime(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
        expected_verification: dict[str, Any] | None,
        *,
        allow_unfenced: bool,
    ) -> dict[str, Any]:
        """Isolate, identify and drain a live runtime before stop/restart.

        Recovery never infers Worker idleness from the database-backed
        Backend drain.  A replacement Worker defaults to accepting work, so
        its exact process/socket instance is fenced before any drain or stop.
        """

        # Isolation is intentionally the first external side effect.  Do not
        # query admission before this has been proven.
        self._isolate_ingress(controller, descriptor)
        presence = self._recovery_runtime_presence(controller, descriptor)
        if presence == "partial":
            raise PullDeployError("runtime is partially stopped during recovery")
        if presence == "stopped":
            return {
                "runtime_state": "stopped",
                "ingress_isolated": True,
                "verified_at": utc_now(),
            }

        actual = self._capture_runtime_recovery_fence(
            controller, descriptor, resumed=None
        )
        if expected_verification is not None:
            expected = self._expected_runtime_recovery_fence(expected_verification)
            if actual != expected:
                raise PullDeployError(
                    "recovery runtime instance differs from committed verification"
                )
        elif not allow_unfenced:
            raise PullDeployError(
                "runtime recovery lacks committed verification evidence"
            )
        else:
            # Pre-drain intent can be durable before its first drain command.
            # In that sole unfenced case, the sealed descriptor is authority
            # for identifying the live source readers.
            self.verify_runtime_identity(
                controller,
                descriptor,
                require_ingress=False,
                allow_active_worker=True,
            )

        drain = self.ensure_candidate_drained(controller, descriptor)
        fence = self._capture_runtime_recovery_fence(
            controller, descriptor, resumed=False
        )
        if fence != actual:
            raise PullDeployError(
                "runtime instance changed while recovery re-established drain"
            )
        return {
            "runtime_state": "drained",
            "ingress_isolated": True,
            "drain": drain,
            "verification": {
                "health": "ok",
                "mode": "recovery-redrain",
                "recovery_fence": fence,
                "verified_at": utc_now(),
            },
            "verified_at": utc_now(),
        }

    def verify_open_runtime(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
        expected_verification: dict[str, Any],
    ) -> None:
        if not self.admission_is_open(controller, descriptor):
            raise PullDeployError("runtime admission is not open after resume")
        runtime = self.verify_runtime_identity(
            controller,
            descriptor,
            require_ingress=True,
            allow_active_worker=True,
        )
        for endpoint in ("/", "/health", "/api/v1/monomer-md/status"):
            controller.runner.run(
                [
                    "curl",
                    "--disable",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--noproxy",
                    "*",
                    "--proto",
                    "=http",
                    "--max-time",
                    "60",
                    f"http://127.0.0.1:9000{endpoint}",
                ],
                env=controller.control_environment(),
                stdout=subprocess.DEVNULL,
            )
        expected = self._expected_runtime_recovery_fence(expected_verification)
        actual = self._capture_runtime_recovery_fence(
            controller, descriptor, resumed=True
        )
        if actual != expected:
            raise PullDeployError(
                "open runtime instance differs from committed verification"
            )
        expected_backend = expected["backend_process"]["container_id"]
        expected_worker = expected["workers"]["monomer-md"]["worker_instance_id"]
        if (
            runtime["containers"]["backend"]["container_id"] != expected_backend
            or runtime["worker"]["worker_instance_id"] != expected_worker
        ):
            raise PullDeployError(
                "open runtime verification selected a different instance"
            )

    def _worker_request(
        self,
        controller: "PullDeployController",
        socket: Path,
        *,
        method: str,
        endpoint: str,
    ) -> dict[str, Any]:
        if not socket.is_absolute() or socket.is_symlink():
            raise PullDeployError("Worker socket path is unsafe")
        result = controller.runner.run(
            [
                "curl",
                "--disable",
                "--fail",
                "--silent",
                "--show-error",
                "--noproxy",
                "*",
                "--proto",
                "=http",
                "--max-time",
                "30",
                "--request",
                method,
                "--unix-socket",
                str(socket),
                f"http://worker{endpoint}",
            ],
            env=controller.control_environment(),
        )
        return parse_command_json(result.stdout, f"Worker {endpoint}")

    def _worker_sockets(
        self,
        controller: "PullDeployController",
        *,
        require_md: bool = False,
    ) -> list[tuple[str, Path]]:
        candidates = [
            (
                "monomer-md",
                controller.state_dir / "monomer-md-worker-socket" / "worker.sock",
            ),
            (
                "monomer-dft",
                controller.state_dir / "monomer-dft-worker-socket" / "worker.sock",
            ),
        ]
        result: list[tuple[str, Path]] = []
        for name, path in candidates:
            if not path.exists() and not path.is_symlink():
                if name == "monomer-md" and require_md:
                    raise PullDeployError(
                        "governed monomer MD Worker socket is missing"
                    )
                continue
            try:
                parent = path.parent.lstat()
                metadata = path.lstat()
            except OSError as exc:
                raise PullDeployError(f"{name} Worker socket is unavailable") from exc
            if (
                not stat.S_ISDIR(parent.st_mode)
                or path.parent.is_symlink()
                or parent.st_uid != os.geteuid()
                or parent.st_mode & 0o077
                or not stat.S_ISSOCK(metadata.st_mode)
                or path.is_symlink()
                or metadata.st_uid != os.geteuid()
            ):
                raise PullDeployError(f"{name} Worker socket is unsafe")
            result.append((name, path))
        return result

    def _validate_worker_runtime_identity(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
        worker: dict[str, Any],
        *,
        expected_accepting: bool | None,
        allow_active: bool = False,
    ) -> dict[str, Any]:
        slot_record = descriptor["monomer_md"]["slot_record"]
        expected_python = str(
            (Path(slot_record["venv_prefix"]) / "bin/python").resolve(strict=True)
        )
        expected_values: dict[str, Any] = {
            "status": "ok",
            "mode": "real",
            "source_sha": descriptor["repository"]["target_sha"],
            "source_tree": descriptor["repository"]["target_tree"],
            "source_root": str(controller.production_root),
            "venv_slot": slot_record["slot"],
            "venv_prefix": slot_record["venv_prefix"],
            "worker_lock_sha256": slot_record["worker_lock_sha256"],
            "slot_record_sha256": descriptor["monomer_md"]["slot_record_sha256"],
            "base_python_identity_sha256": slot_record["base_python_identity_sha256"],
            "python_executable": expected_python,
            "db_configured": True,
            "runtime_ready": True,
            "max_active_jobs": 1,
            "default_steps": 300,
            "max_steps": 300,
            "cuda_visible_devices": "2",
            "gpu_broker_enabled": False,
        }
        if any(worker.get(key) != value for key, value in expected_values.items()):
            raise PullDeployError(
                "monomer MD Worker live identity/readiness differs from deployment"
            )
        active_jobs = worker.get("active_jobs")
        if (
            not isinstance(active_jobs, int)
            or isinstance(active_jobs, bool)
            or active_jobs < 0
            or active_jobs > 1
            or (not allow_active and active_jobs != 0)
        ):
            raise PullDeployError(
                "monomer MD Worker active-job state differs from deployment"
            )
        if expected_accepting is None:
            accepting = active_jobs == 0
            draining = False
        else:
            accepting = expected_accepting
            draining = not expected_accepting
        if (
            worker.get("accepting_jobs") is not accepting
            or worker.get("draining") is not draining
        ):
            raise PullDeployError(
                "monomer MD Worker admission state differs from deployment"
            )
        instance = worker.get("worker_instance_id")
        if not isinstance(instance, str) or not instance:
            raise PullDeployError("monomer MD Worker instance identity is invalid")
        protocols = worker.get("protocols")
        if not isinstance(protocols, dict):
            raise PullDeployError(
                "monomer MD Worker protocol inventory is invalid"
            )
        transport = protocols.get("Transport")
        if (
            not isinstance(transport, dict)
            or transport.get("supported") is not True
            or transport.get("runtime_ready") is not True
            or "runtime_error" not in transport
            or transport.get("runtime_error") is not None
        ):
            raise PullDeployError("monomer MD Transport runtime is not strictly ready")
        return worker

    def _wait_for_zero_work(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
        worker_instances: dict[str, str],
        backend_process: dict[str, Any],
    ) -> dict[str, Any]:
        deadline = time.monotonic() + DRAIN_TIMEOUT_SECONDS
        while True:
            backend = validate_active_jobs_evidence(
                self._backend_active_status(controller, descriptor),
                require_drained=False,
                authority_sha=self._drain_authority_sha(descriptor),
            )
            if backend["drain"]["enabled"] is not True:
                raise PullDeployError(
                    "Backend persistent drain was lost while waiting for work"
                )
            if (
                self._backend_process_identity(controller, descriptor)
                != backend_process
            ):
                raise PullDeployError("Backend instance changed during drain")
            workers: dict[str, Any] = {}
            all_zero = backend["active_total"] == 0
            sockets = self._worker_sockets(controller, require_md=True)
            if {name for name, _socket in sockets} != set(worker_instances):
                raise PullDeployError("governed Worker socket set changed during drain")
            for name, socket in sockets:
                snapshot = validate_worker_control_evidence(
                    self._worker_request(
                        controller, socket, method="GET", endpoint="/health"
                    ),
                    action="health-drained",
                    require_zero=False,
                )
                if snapshot["worker_instance_id"] != worker_instances.get(name):
                    raise PullDeployError(
                        f"{name} Worker instance changed during drain"
                    )
                if name == "monomer-md":
                    self._validate_worker_runtime_identity(
                        controller,
                        descriptor,
                        snapshot,
                        expected_accepting=False,
                        allow_active=True,
                    )
                workers[name] = snapshot
                all_zero = all_zero and snapshot["active_jobs"] == 0
            if all_zero:
                return {
                    "backend": backend,
                    "workers": workers,
                    "verified_at": utc_now(),
                }
            if time.monotonic() >= deadline:
                raise PullDeployError(
                    "timed out waiting for Backend and Worker active jobs"
                )
            time.sleep(DRAIN_POLL_SECONDS)

    def _verify_postgres_restore(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
        dump: Path,
        dump_digest: str,
    ) -> dict[str, Any]:
        """Restore the full dump into an isolated, pinned PostgreSQL 16."""

        name = f"nexpoly-restore-{descriptor['operation_id']}"
        values = parse_literal_env(controller.config_dir / "deploy.env")
        capacity_value = values.get("NEXPOLY_POSTGRES_RESTORE_TMPFS_BYTES")
        if not capacity_value or not capacity_value.isdigit():
            raise PullDeployError(
                "deploy.env must pin NEXPOLY_POSTGRES_RESTORE_TMPFS_BYTES"
            )
        capacity = int(capacity_value)
        minimum_capacity = max(dump.stat().st_size * 8, 8 * 1024**3)
        if capacity < minimum_capacity or capacity > 256 * 1024**3:
            raise PullDeployError(
                "isolated restore tmpfs capacity is outside the governed bound"
            )
        probe = controller.runner.run(
            ["docker", "container", "inspect", name],
            env=controller.control_environment(),
            check=False,
        )
        if probe.returncode == 0:
            try:
                existing_values = json.loads(str(probe.stdout))
                existing = existing_values[0]
            except (json.JSONDecodeError, IndexError, TypeError) as exc:
                raise PullDeployError(
                    "existing isolated restore container evidence is malformed"
                ) from exc
            existing_id = self._validate_isolated_container(
                existing,
                name=name,
                image=POSTGRES16_IMAGE,
                operation_label="com.nexpoly.restore-operation",
                operation_id=descriptor["operation_id"],
                tmpfs_capacity=capacity,
            )
            self._remove_container_and_prove_absent(
                controller,
                name,
                container_id=existing_id,
                label="owned interrupted restore",
            )
        if probe.returncode != 1:
            if probe.returncode != 0:
                raise PullDeployError("cannot prove isolated restore container absence")
        started = False
        started_id: str | None = None
        failure: BaseException | None = None
        try:
            run_error: BaseException | None = None
            try:
                controller.runner.run(
                    [
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
                        f"com.nexpoly.restore-operation={descriptor['operation_id']}",
                        POSTGRES16_IMAGE,
                    ],
                    env=controller.control_environment(),
                )
            except BaseException as exc:
                run_error = exc
            committed = controller.runner.run(
                ["docker", "container", "inspect", name],
                env=controller.control_environment(),
                check=False,
            )
            if committed.returncode != 0:
                raise PullDeployError(
                    "cannot prove isolated restore container startup"
                ) from run_error
            try:
                committed_record = json.loads(str(committed.stdout))[0]
            except (json.JSONDecodeError, IndexError, TypeError) as exc:
                raise PullDeployError(
                    "started isolated restore container evidence is malformed"
                ) from exc
            started_id = self._validate_isolated_container(
                committed_record,
                name=name,
                image=POSTGRES16_IMAGE,
                operation_label="com.nexpoly.restore-operation",
                operation_id=descriptor["operation_id"],
                tmpfs_capacity=capacity,
            )
            started = True
            deadline = time.monotonic() + 120
            while True:
                ready = controller.runner.run(
                    self._docker_exec(
                        str(started_id),
                        "pg_isready",
                        "--username",
                        "postgres",
                    ),
                    env=controller.control_environment(),
                    check=False,
                )
                if ready.returncode == 0:
                    break
                if ready.returncode not in {1, 2} or time.monotonic() >= deadline:
                    raise PullDeployError("isolated PostgreSQL 16 did not become ready")
                time.sleep(1)
            controller.runner.run(
                self._docker_exec(
                    str(started_id),
                    "createdb",
                    "--username",
                    "postgres",
                    "nexpoly_restore",
                ),
                env=controller.control_environment(),
            )
            with dump.open("rb") as source:
                controller.runner.run(
                    self._docker_exec(
                        str(started_id),
                        "pg_restore",
                        "--exit-on-error",
                        "--no-owner",
                        "--no-acl",
                        "--username",
                        "postgres",
                        "--dbname",
                        "nexpoly_restore",
                        interactive=True,
                    ),
                    env=controller.control_environment(),
                    text=False,
                    stdin=source,
                    timeout=1800,
                )
            version = controller.runner.run(
                self._docker_exec(
                    str(started_id),
                    "psql",
                    "--tuples-only",
                    "--no-align",
                    "--username",
                    "postgres",
                    "--dbname",
                    "nexpoly_restore",
                    "--command",
                    "SHOW server_version_num",
                ),
                env=controller.control_environment(),
            )
            version_number = str(version.stdout).strip()
            if not version_number.startswith("16") or not version_number.isdigit():
                raise PullDeployError("isolated restore did not use PostgreSQL 16")
            ledger_result = controller.runner.run(
                self._docker_exec(
                    str(started_id),
                    "psql",
                    "--set",
                    "ON_ERROR_STOP=1",
                    "--username",
                    "postgres",
                    "--dbname",
                    "nexpoly_restore",
                    "--command",
                    "SELECT COALESCE(json_agg(json_build_object('version', version, 'checksum', checksum) ORDER BY version), '[]'::json)::text FROM governance.schema_migrations",
                ),
                env=controller.control_environment(),
            )
            try:
                ledger_rows = json.loads(str(ledger_result.stdout).strip())
            except json.JSONDecodeError as exc:
                raise PullDeployError(
                    "isolated restore migration ledger is invalid JSON"
                ) from exc
            ledger = canonical_ledger_history(
                ledger_rows,
                descriptor["migrations"].get("records"),
                accepted_ledgers=descriptor_accepted_ledgers(descriptor),
            )
            return {
                "schema_version": 1,
                "restored": True,
                "postgres_major": 16,
                "postgres_version_num": version_number,
                "image": POSTGRES16_IMAGE,
                "dump_sha256": dump_digest,
                "ledger": ledger,
                "verified_at": utc_now(),
            }
        except BaseException as exc:
            failure = exc
            raise
        finally:
            if started:
                try:
                    self._remove_container_and_prove_absent(
                        controller,
                        name,
                        container_id=str(started_id),
                        label="isolated restore",
                    )
                except BaseException as cleanup_exc:
                    if failure is not None:
                        raise cleanup_exc from failure
                    raise

    def _verify_web_image(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
    ) -> dict[str, Any]:
        name = (
            f"nexpoly-web-smoke-{descriptor['operation_id']}-"
            f"{descriptor['repository']['target_sha'][:12]}"
        )
        probe = controller.runner.run(
            ["docker", "container", "inspect", name],
            env=controller.control_environment(),
            check=False,
        )
        if probe.returncode == 0:
            try:
                existing = json.loads(str(probe.stdout))[0]
            except (json.JSONDecodeError, IndexError, TypeError) as exc:
                raise PullDeployError(
                    "existing Web smoke container evidence is malformed"
                ) from exc
            existing_id = self._validate_isolated_container(
                existing,
                name=name,
                image=descriptor["images"]["web"]["digest_ref"],
                operation_label="com.nexpoly.deploy-operation",
                operation_id=descriptor["operation_id"],
                tmpfs_capacity=None,
            )
            self._remove_container_and_prove_absent(
                controller,
                name,
                container_id=existing_id,
                label="owned interrupted Web smoke",
            )
        elif probe.returncode != 1:
            raise PullDeployError("cannot prove Web smoke container absence")
        started = False
        started_id: str | None = None
        failure: BaseException | None = None
        try:
            run_error: BaseException | None = None
            try:
                controller.runner.run(
                    [
                        "docker",
                        "run",
                        "--detach",
                        "--pull=never",
                        "--name",
                        name,
                        "--network",
                        "none",
                        "--label",
                        f"com.nexpoly.deploy-operation={descriptor['operation_id']}",
                        descriptor["images"]["web"]["digest_ref"],
                    ],
                    env=controller.control_environment(),
                )
            except BaseException as exc:
                run_error = exc
            committed = controller.runner.run(
                ["docker", "container", "inspect", name],
                env=controller.control_environment(),
                check=False,
            )
            if committed.returncode != 0:
                raise PullDeployError(
                    "cannot prove Web smoke container startup"
                ) from run_error
            try:
                committed_record = json.loads(str(committed.stdout))[0]
            except (json.JSONDecodeError, IndexError, TypeError) as exc:
                raise PullDeployError(
                    "started Web smoke evidence is malformed"
                ) from exc
            started_id = self._validate_isolated_container(
                committed_record,
                name=name,
                image=descriptor["images"]["web"]["digest_ref"],
                operation_label="com.nexpoly.deploy-operation",
                operation_id=descriptor["operation_id"],
                tmpfs_capacity=None,
            )
            started = True
            deadline = time.monotonic() + 60
            html = ""
            while True:
                response = controller.runner.run(
                    [
                        "docker",
                        "exec",
                        name,
                        "wget",
                        "-qO-",
                        "http://127.0.0.1/",
                    ],
                    env=controller.control_environment(),
                    check=False,
                )
                if response.returncode == 0 and isinstance(response.stdout, str):
                    html = response.stdout
                    break
                if response.returncode not in {1, 4, 8} or time.monotonic() >= deadline:
                    raise PullDeployError(
                        "isolated Web image did not serve its homepage"
                    )
                time.sleep(1)
            if not html or len(html.encode("utf-8")) > 2 * 1024 * 1024:
                raise PullDeployError("isolated Web homepage is empty or oversized")
            references = sorted(
                {
                    value
                    for value in re.findall(r"(?:src|href)=[\"']([^\"']+)[\"']", html)
                    if value.startswith("/assets/")
                }
            )
            if (
                not references
                or len(references) > 64
                or any(
                    re.search(r"-[0-9a-f]{8,}\.[A-Za-z0-9]+(?:\?|$)", value) is None
                    for value in references
                )
            ):
                raise PullDeployError(
                    "isolated Web homepage lacks bounded hashed assets"
                )
            asset_digests: dict[str, str] = {}
            for reference in references:
                response = controller.runner.run(
                    [
                        "docker",
                        "exec",
                        name,
                        "wget",
                        "-qO-",
                        f"http://127.0.0.1{reference}",
                    ],
                    env=controller.control_environment(),
                    text=False,
                )
                body = bytes(response.stdout)
                if not body or len(body) > 64 * 1024 * 1024:
                    raise PullDeployError(
                        "isolated Web hashed asset is empty or oversized"
                    )
                asset_digests[reference] = sha256_bytes(body)
            return {
                "image": descriptor["images"]["web"]["digest_ref"],
                "homepage_sha256": sha256_bytes(html.encode("utf-8")),
                "assets": asset_digests,
            }
        except BaseException as exc:
            failure = exc
            raise
        finally:
            if started:
                try:
                    self._remove_container_and_prove_absent(
                        controller,
                        name,
                        container_id=str(started_id),
                        label="isolated Web smoke",
                    )
                except BaseException as cleanup_exc:
                    if failure is not None:
                        raise cleanup_exc from failure
                    raise

    def drain(
        self, controller: "PullDeployController", descriptor: dict[str, Any]
    ) -> dict[str, Any]:
        if descriptor["previous_deployment"] is None:
            evidence = self._run_bootstrap_hook(
                controller,
                "bootstrap-quiesce",
                descriptor["production_config"],
            )
            return validate_bootstrap_quiesce_evidence(evidence)
        return self.ensure_candidate_drained(controller, descriptor)

    def ensure_candidate_drained(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
    ) -> dict[str, Any]:
        """Fence a running governed candidate before it may be stopped.

        This path is deliberately independent of ``previous_deployment``: the
        first pull takeover can crash after the new Backend/Worker temporarily
        reopen for canary execution, even though its previous runtime was the
        legacy bootstrap runtime.
        """

        backend_process = self._backend_process_identity(controller, descriptor)
        initial = validate_persistent_drain_evidence(
            self._control_cli(
                controller,
                descriptor,
                "drain",
                "--actor",
                "pull-deploy-controller",
                "--release-sha",
                self._drain_authority_sha(descriptor),
                "--reason",
                f"pull deployment {descriptor['operation_id']}",
            ),
            expected_enabled=True,
            authority_sha=self._drain_authority_sha(descriptor),
        )
        worker_instances: dict[str, str] = {}
        sockets = self._worker_sockets(controller, require_md=True)
        for name, socket in sockets:
            response = validate_worker_control_evidence(
                self._worker_request(
                    controller, socket, method="POST", endpoint="/drain"
                ),
                action="drain",
                require_zero=False,
            )
            worker_instances[name] = response["worker_instance_id"]
            health = validate_worker_control_evidence(
                self._worker_request(
                    controller, socket, method="GET", endpoint="/health"
                ),
                action="health-drained",
                require_zero=False,
            )
            if health["worker_instance_id"] != worker_instances[name]:
                raise PullDeployError(f"{name} Worker changed while entering drain")
            if name == "monomer-md":
                self._validate_worker_runtime_identity(
                    controller,
                    descriptor,
                    health,
                    expected_accepting=False,
                    allow_active=True,
                )
        settled = self._wait_for_zero_work(
            controller,
            descriptor,
            worker_instances,
            backend_process,
        )
        return {
            "persistent_drain": True,
            "initial": initial,
            "settled": settled,
            "worker_instances": worker_instances,
            "backend_process": backend_process,
        }

    def resume_bootstrap_unchanged(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
    ) -> None:
        evidence = self._run_bootstrap_hook(
            controller,
            "bootstrap-resume-unchanged",
            descriptor["production_config"],
        )
        validate_bootstrap_resume_unchanged_evidence(
            evidence,
            expected_runtime_digest=require_digest(
                controller.production_deploy_values(check_free_space=False).get(
                    "NEXPOLY_BOOTSTRAP_LEGACY_RUNTIME_SHA256"
                ),
                "bootstrap legacy runtime digest",
            ),
        )

    def bootstrap_can_resume_unchanged(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
    ) -> bool:
        evidence = self._run_bootstrap_hook(
            controller,
            "bootstrap-status",
            descriptor["production_config"],
        )
        status = validate_bootstrap_status_evidence(
            evidence,
            expected_runtime_digest=require_digest(
                controller.production_deploy_values(check_free_space=False).get(
                    "NEXPOLY_BOOTSTRAP_LEGACY_RUNTIME_SHA256"
                ),
                "bootstrap legacy runtime digest",
            ),
        )
        return status["legacy_runtime_state"] in {"open", "isolated"}

    def backup(
        self, controller: "PullDeployController", descriptor: dict[str, Any]
    ) -> dict[str, Any]:
        expected_postgres = self._assert_sealed_postgres_runtime(
            controller,
            descriptor,
            phase="before database backup",
        )
        postgres_container_id = expected_postgres["container_id"]
        values = parse_literal_env(controller.config_dir / "deploy.env")
        user = values.get("NEXPOLY_POSTGRES_USER")
        database = values.get("NEXPOLY_POSTGRES_DB")
        if not user or database != "nexpoly":
            raise PullDeployError(
                "production PostgreSQL identity is not pinned to nexpoly"
            )
        directory = controller.backups_dir / descriptor["operation_id"]
        ensure_private_directory(directory, create=True)
        dump = directory / "database.dump"
        temporary = directory / "database.dump.tmp"
        if dump.exists() or dump.is_symlink():
            metadata = dump.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or dump.is_symlink()
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise PullDeployError("existing database backup is unsafe")
        else:
            if temporary.exists() or temporary.is_symlink():
                metadata = temporary.lstat()
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or temporary.is_symlink()
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    raise PullDeployError("interrupted database backup is unsafe")
                # The deploy lock proves no second controller owns this
                # operation.  Rename the possibly still-open inode out of the
                # authoritative name before retrying; an orphan writer cannot
                # corrupt the new dump.
                interrupted = directory / f"database.dump.interrupted-{time.time_ns()}"
                os.rename(temporary, interrupted)
                fsync_directory(directory)
            with temporary.open("xb") as output:
                os.chmod(temporary, 0o600)
                controller.runner.run(
                    self._docker_exec(
                        postgres_container_id,
                        "pg_dump",
                        "--format=custom",
                        "--username",
                        user,
                        "--dbname",
                        database,
                    ),
                    cwd=controller.production_root,
                    env=controller.control_environment(),
                    text=False,
                    stdout=output,
                )
                output.flush()
                os.fsync(output.fileno())
            controller.runner.run(
                ["pg_restore", "--list", str(temporary)],
                env=controller.control_environment(),
            )
            os.rename(temporary, dump)
            fsync_directory(directory)
        # Revalidate a recovered final file as strictly as a newly created
        # one.  This makes an unknown return after the atomic rename safely
        # retryable without overwriting the sealed dump.
        controller.runner.run(
            ["pg_restore", "--list", str(dump)],
            env=controller.control_environment(),
        )
        sidecar = dump.with_suffix(".dump.sha256")
        digest = sha256_file(dump)
        atomic_bytes(sidecar, (digest + "  database.dump\n").encode("ascii"))
        restored = self._verify_postgres_restore(controller, descriptor, dump, digest)
        if (
            restored.get("schema_version") != 1
            or restored.get("restored") is not True
            or restored.get("postgres_major") != 16
            or restored.get("dump_sha256") != digest
        ):
            raise PullDeployError("isolated PostgreSQL 16 restore evidence is invalid")
        marker = load_private_json(controller.marker_path)
        source_before = validate_mutable_data_evidence(
            marker.get("mutable_data_before")
        )
        source_after = controller._capture_mutable_data(descriptor)
        source_identity_sha256 = canonical_json_digest(
            mutable_data_identity(source_before)
        )
        if (
            canonical_json_digest(mutable_data_identity(source_after))
            != source_identity_sha256
        ):
            raise PullDeployError(
                "database changed while producing the rollback backup"
            )
        restored["source_mutable_data_identity_sha256"] = (
            source_identity_sha256
        )
        self._assert_sealed_postgres_runtime(
            controller,
            descriptor,
            expected=expected_postgres,
            phase="during database backup",
        )
        return {"path": str(dump), "sha256": digest, "restore_verification": restored}

    def backup_rollback(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
        backup_operation_id: str,
    ) -> dict[str, Any]:
        backup_operation_id = require_operation_id(backup_operation_id)
        if backup_operation_id == descriptor.get("operation_id"):
            raise PullDeployError("rollback backup must use independent authority")
        projected = dict(descriptor)
        projected["operation_id"] = backup_operation_id
        return self.backup(controller, projected)

    def restore_database(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
        backup: dict[str, Any],
    ) -> dict[str, Any]:
        """Idempotently replace production with the sealed pre-deploy dump.

        Dropping and recreating the database is intentional: ``pg_restore
        --clean`` cannot remove relations introduced after the dump, which
        would leave an old Backend facing an unknown expand migration.
        """

        backup = controller._validate_database_backup(
            descriptor, backup, require_operation_backup=True
        )
        dump = Path(backup["path"])
        values = controller.production_deploy_values(check_free_space=False)
        user = values["NEXPOLY_POSTGRES_USER"]
        database = values["NEXPOLY_POSTGRES_DB"]
        expected_postgres = self._assert_sealed_postgres_runtime(
            controller,
            descriptor,
            phase="before rollback restore",
        )
        postgres_container_id = expected_postgres["container_id"]
        controller.runner.run(
            self._docker_exec(
                postgres_container_id,
                "psql",
                "--set",
                "ON_ERROR_STOP=1",
                "--username",
                user,
                "--dbname",
                "postgres",
                "--command",
                (
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname = '{database}' AND pid <> pg_backend_pid()"
                ),
            ),
            cwd=controller.production_root,
            env=controller.control_environment(),
        )
        controller.runner.run(
            self._docker_exec(
                postgres_container_id,
                "dropdb",
                "--if-exists",
                "--force",
                "--username",
                user,
                database,
            ),
            cwd=controller.production_root,
            env=controller.control_environment(),
        )
        controller.runner.run(
            self._docker_exec(
                postgres_container_id,
                "createdb",
                "--username",
                user,
                "--owner",
                user,
                database,
            ),
            cwd=controller.production_root,
            env=controller.control_environment(),
        )
        with dump.open("rb") as source:
            controller.runner.run(
                self._docker_exec(
                    postgres_container_id,
                    "pg_restore",
                    "--exit-on-error",
                    "--username",
                    user,
                    "--dbname",
                    database,
                    interactive=True,
                ),
                cwd=controller.production_root,
                env=controller.control_environment(),
                text=False,
                stdin=source,
                timeout=1800,
            )
        # The same target manifest used to verify the isolated restore is the
        # authoritative prefix after the production restore.
        result = controller.runner.run(
            self._docker_exec(
                postgres_container_id,
                "psql",
                "--set",
                "ON_ERROR_STOP=1",
                "--tuples-only",
                "--no-align",
                "--username",
                user,
                "--dbname",
                database,
                "--command",
                (
                    "SELECT COALESCE(json_agg(json_build_object("
                    "'version', version, 'checksum', checksum) ORDER BY version), "
                    "'[]'::json)::text FROM governance.schema_migrations"
                ),
            ),
            cwd=controller.production_root,
            env=controller.control_environment(),
        )
        try:
            rows = json.loads(str(result.stdout))
        except json.JSONDecodeError as exc:
            raise PullDeployError(
                "restored production ledger evidence is invalid"
            ) from exc
        ledger = canonical_ledger_history(
            rows,
            descriptor["migrations"].get("records"),
            accepted_ledgers=descriptor_accepted_ledgers(descriptor),
        )
        expected_ledger = backup["restore_verification"].get("ledger")
        if ledger != expected_ledger:
            raise PullDeployError(
                "restored production ledger differs from verified backup"
            )
        self._assert_sealed_postgres_runtime(
            controller,
            descriptor,
            expected=expected_postgres,
            phase="during rollback restore",
        )
        return {
            "restored": True,
            "dump_sha256": backup["sha256"],
            "ledger": ledger,
            "restored_at": utc_now(),
        }

    def stop(
        self, controller: "PullDeployController", descriptor: dict[str, Any]
    ) -> dict[str, Any]:
        environment = self._environment(controller, descriptor)
        before = self.postgres_runtime_identity(controller, descriptor)
        controller.runner.run(
            ["systemctl", "--user", "stop", "nexpoly-monomer-md-worker.service"],
            env=environment,
        )
        controller.runner.run(
            self._compose(controller, "stop", "nginx", "backend"),
            cwd=controller.production_root,
            env=environment,
        )
        unit = controller.runner.run(
            ["systemctl", "--user", "is-active", "nexpoly-monomer-md-worker.service"],
            env=environment,
            check=False,
        )
        if unit.returncode not in {3, 4} or str(unit.stdout).strip() not in {
            "inactive",
            "unknown",
        }:
            raise PullDeployError("monomer MD Worker did not stop")
        remaining = controller.runner.run(
            self._compose(controller, "ps", "--quiet", "backend", "nginx"),
            cwd=controller.production_root,
            env=environment,
        )
        if str(remaining.stdout).strip():
            raise PullDeployError("Backend or Web container remains active after stop")
        postgres = controller.runner.run(
            self._compose(controller, "ps", "--quiet", "lab-postgres"),
            cwd=controller.production_root,
            env=environment,
        )
        ids = [value for value in str(postgres.stdout).splitlines() if value]
        if len(ids) != 1:
            raise PullDeployError(
                "PostgreSQL container identity is unavailable after stop"
            )
        state = controller.runner.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", ids[0]],
            env=controller.control_environment(),
        )
        if str(state.stdout).strip() != "true":
            raise PullDeployError("PostgreSQL was not preserved during source switch")
        after = self.postgres_runtime_identity(controller, descriptor)
        if postgres_runtime_fence_identity(after) != postgres_runtime_fence_identity(
            before
        ):
            raise PullDeployError("PostgreSQL identity changed while stopping readers")
        for unit_name in (
            "nexpoly-gpu-broker.service",
            "nexpoly-gpu-mps@1.service",
            "nexpoly-gpu-mps@2.service",
            "nexpoly-gpu-mps@3.service",
        ):
            unit_state = controller.runner.run(
                ["systemctl", "is-active", unit_name],
                env=controller.control_environment(),
                check=False,
            )
            if unit_state.returncode not in {3, 4} or str(
                unit_state.stdout
            ).strip() not in {
                "inactive",
                "unknown",
            }:
                raise PullDeployError(
                    f"live-source GPU unit must be inactive before Git switch: {unit_name}"
                )
        return before

    def migrate(
        self, controller: "PullDeployController", descriptor: dict[str, Any]
    ) -> dict[str, Any]:
        expected_postgres = self._assert_sealed_postgres_runtime(
            controller,
            descriptor,
            phase="before migration",
        )
        environment = self._environment(controller, descriptor)
        mode = (
            "bootstrap-expand"
            if descriptor["previous_deployment"] is None
            else "expand"
        )
        try:
            result = controller.runner.run(
                self._compose(
                    controller,
                    "run",
                    "--rm",
                    "--no-deps",
                    "postgres-init",
                    "python",
                    "-m",
                    "app.postgres_migrations",
                    "--mode",
                    mode,
                ),
                cwd=controller.production_root,
                env=environment,
            )
        except BaseException as migration_error:
            try:
                self._assert_sealed_postgres_runtime(
                    controller,
                    descriptor,
                    expected=expected_postgres,
                    phase="during failed migration",
                )
            except BaseException as fence_error:
                raise fence_error from migration_error
            raise
        applied: list[str] = []
        observed_results: list[tuple[str, str, str]] = []
        for line in str(result.stdout).splitlines():
            fields = line.split("\t")
            if (
                len(fields) != 3
                or fields[1] not in {"applied", "skipped"}
                or re.fullmatch(r"[0-9a-f]{64}", fields[2]) is None
            ):
                raise PullDeployError("migration runner emitted malformed evidence")
            observed_results.append((fields[0], fields[1], fields[2]))
            if fields[1] == "applied":
                applied.append(fields[0])
        manifest = descriptor["migrations"].get("records")
        if not isinstance(manifest, list) or len(observed_results) != len(manifest):
            raise PullDeployError(
                "migration output does not cover the complete target manifest"
            )
        for observed, entry in zip(observed_results, manifest, strict=True):
            if (
                not isinstance(entry, dict)
                or set(entry)
                != {"version", "kind", "epoch", "checksum", "requires_contracts"}
                or observed[0] != entry["version"]
                or observed[2] != entry["checksum"]
            ):
                raise PullDeployError(
                    "migration output differs from the canonical target manifest"
                )
            if observed[1] == "applied" and entry["kind"] not in {"baseline", "expand"}:
                raise PullDeployError(
                    "expand deployment applied a destructive migration"
                )
        ledger_program = (
            "import json,os,psycopg;"
            "connection=psycopg.connect(os.environ['APP_POSTGRES_DSN']);"
            "rows=connection.execute('SELECT version, checksum FROM governance.schema_migrations ORDER BY version').fetchall();"
            "print(json.dumps([{'version':row[0],'checksum':row[1]} for row in rows],sort_keys=True));"
            "connection.close()"
        )
        ledger_result = controller.runner.run(
            self._compose(
                controller,
                "run",
                "--rm",
                "--no-deps",
                "postgres-init",
                "python",
                "-c",
                ledger_program,
            ),
            cwd=controller.production_root,
            env=environment,
        )
        try:
            ledger_rows = json.loads(str(ledger_result.stdout))
        except json.JSONDecodeError as exc:
            raise PullDeployError("migration ledger evidence is invalid JSON") from exc
        canonical_history = canonical_ledger_history(
            ledger_rows,
            manifest,
            accepted_ledgers=descriptor_accepted_ledgers(descriptor),
            require_registry_match=descriptor_accepted_ledgers(descriptor)
            is not None,
        )
        self._assert_sealed_postgres_runtime(
            controller,
            descriptor,
            expected=expected_postgres,
            phase="during migration",
        )
        return {"newly_applied": applied, "ledger": canonical_history}

    def start(
        self, controller: "PullDeployController", descriptor: dict[str, Any]
    ) -> None:
        environment = self._environment(controller, descriptor)
        expected_postgres = self._sealed_postgres_runtime_fence(controller)
        before = self.postgres_runtime_identity(controller, descriptor)
        if postgres_runtime_fence_identity(before) != postgres_runtime_fence_identity(
            expected_postgres
        ):
            raise PullDeployError("PostgreSQL identity changed before application start")
        controller.runner.run(
            self._compose(controller, "stop", "nginx"),
            cwd=controller.production_root,
            env=environment,
        )
        ingress = controller.runner.run(
            self._compose(controller, "ps", "--quiet", "nginx"),
            cwd=controller.production_root,
            env=environment,
        )
        if str(ingress.stdout).strip():
            raise PullDeployError("Web ingress remains active before runtime recovery")
        controller.runner.run(
            ["systemctl", "--user", "restart", "nexpoly-monomer-md-worker.service"],
            env=environment,
        )
        controller.runner.run(
            self._compose(
                controller,
                "up",
                "-d",
                "--no-build",
                "--wait",
                "--wait-timeout",
                "300",
                "--no-deps",
                "backend",
            ),
            cwd=controller.production_root,
            env=environment,
        )
        after = self.postgres_runtime_identity(controller, descriptor)
        if postgres_runtime_fence_identity(after) != postgres_runtime_fence_identity(
            expected_postgres
        ):
            raise PullDeployError("PostgreSQL identity changed during application start")
        self._drain_started_runtime(controller, descriptor)

    def _drain_started_runtime(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
    ) -> dict[str, Any]:
        """Re-establish maintenance admission after process restarts.

        Backend and Worker drain flags are process-local and intentionally
        default to accepting on a clean start.  Ingress is still isolated here,
        so re-drain both control planes and prove zero work before any identity
        verification or canary is allowed to run.
        """

        backend = validate_persistent_drain_evidence(
            self._control_cli(
                controller,
                descriptor,
                "drain",
                "--actor",
                "pull-deploy-controller",
                "--release-sha",
                self._drain_authority_sha(descriptor),
                "--reason",
                f"post-restart drain {descriptor['operation_id']}",
            ),
            expected_enabled=True,
            authority_sha=self._drain_authority_sha(descriptor),
        )
        backend_process = self._backend_process_identity(controller, descriptor)
        deadline = time.monotonic() + 120
        last_error: BaseException | None = None
        worker_instances: dict[str, str] = {}
        while True:
            try:
                worker_instances = {}
                for name, socket in self._worker_sockets(controller, require_md=True):
                    response = validate_worker_control_evidence(
                        self._worker_request(
                            controller,
                            socket,
                            method="POST",
                            endpoint="/drain",
                        ),
                        action="drain",
                        require_zero=False,
                    )
                    worker_instances[name] = response["worker_instance_id"]
                break
            except PullDeployError as exc:
                if not any(
                    token in str(exc)
                    for token in (
                        "Worker socket is missing",
                        "Worker socket is unavailable",
                    )
                ):
                    raise
                last_error = exc
                if time.monotonic() >= deadline:
                    raise PullDeployError(
                        "timed out re-draining the restarted Worker"
                    ) from last_error
                time.sleep(1)
            except (OSError, subprocess.SubprocessError) as exc:
                last_error = exc
                if time.monotonic() >= deadline:
                    raise PullDeployError(
                        "timed out re-draining the restarted Worker"
                    ) from last_error
                time.sleep(1)
        settled = self._wait_for_zero_work(
            controller,
            descriptor,
            worker_instances,
            backend_process,
        )
        return {
            "backend": backend,
            "workers": worker_instances,
            "backend_process": backend_process,
            "settled": settled,
            "verified_at": utc_now(),
        }

    def verify_runtime_identity(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
        *,
        require_ingress: bool = True,
        allow_active_worker: bool = False,
    ) -> dict[str, Any]:
        """Prove the actual running runtime without changing admission or data."""

        environment = self._environment(controller, descriptor)
        if controller.stable_helper_evidence() != descriptor["controller"]["helpers"]:
            raise PullDeployError("stable control helper identity changed")
        expected_active_control = descriptor.get("_runtime_active_control")
        if expected_active_control is None:
            live_active_control = controller.active_control_evidence()
            if not controller._active_matches_candidate(
                live_active_control, descriptor["controller"]["executor_control"]
            ):
                raise PullDeployError("live control authority differs from candidate")
        elif controller.active_control_evidence() != expected_active_control:
            raise PullDeployError("live control authority differs from rollback target")
        repository = controller.repository_identity(require_ssh_origin=True)
        if (
            repository["sha"] != descriptor["repository"]["target_sha"]
            or repository["tree"] != descriptor["repository"]["target_tree"]
        ):
            raise PullDeployError("live checkout identity differs during verification")
        asset = descriptor["release_input"]["asset"]
        if controller._asset_pointer_target(descriptor) != Path(asset["root"]):
            raise PullDeployError("live external asset pointer differs from candidate")
        if inspect_asset_release(Path(asset["root"]), asset["manifest_sha256"]) != {
            key: asset[key] for key in ASSET_RELEASE_FIELDS
        }:
            raise PullDeployError("live external asset release identity changed")

        unit = descriptor["monomer_md"]["systemd_unit"]
        if sha256_file(Path(unit["target_path"])) != unit["sha256"]:
            raise PullDeployError("running Worker unit file differs from candidate")
        unit_fields = controller._worker_unit_state(Path(unit["target_path"]))
        if unit_fields != {
            "LoadState": "loaded",
            "FragmentPath": unit["target_path"],
            "DropInPaths": "",
            "NeedDaemonReload": "no",
            "UnitFileState": "enabled",
        }:
            raise PullDeployError(
                "Worker systemd enabled runtime identity differs from candidate"
            )

        running: dict[str, Any] = {}
        roles = [("backend", "backend")]
        if require_ingress:
            roles.append(("web", "nginx"))
        else:
            web_state = controller.runner.run(
                self._compose(controller, "ps", "--quiet", "nginx"),
                cwd=controller.production_root,
                env=environment,
            )
            if str(web_state.stdout).strip():
                raise PullDeployError(
                    "Web ingress must remain stopped before admission"
                )
        for role, service in roles:
            record = descriptor["images"][role]
            listed = controller.runner.run(
                self._compose(controller, "ps", "--quiet", service),
                cwd=controller.production_root,
                env=environment,
            )
            identities = [value for value in str(listed.stdout).splitlines() if value]
            if len(identities) != 1:
                raise PullDeployError(
                    f"running {role} container identity is unavailable"
                )
            inspected = controller.runner.run(
                ["docker", "container", "inspect", identities[0]],
                env=controller.control_environment(),
            )
            try:
                values = json.loads(str(inspected.stdout))
                container = values[0]
            except (json.JSONDecodeError, IndexError, TypeError) as exc:
                raise PullDeployError(
                    f"running {role} container evidence is malformed"
                ) from exc
            labels = (
                container.get("Config", {}).get("Labels", {})
                if isinstance(container, dict)
                else {}
            )
            if (
                not isinstance(container, dict)
                or container.get("State", {}).get("Running") is not True
                or container.get("Image") != record["image_id"]
                or container.get("Config", {}).get("Image") != record["digest_ref"]
                or labels.get("org.opencontainers.image.revision")
                != descriptor["repository"]["target_sha"]
            ):
                raise PullDeployError(
                    f"running {role} container differs from sealed image"
                )
            running[role] = {
                "container_id": identities[0],
                "image_id": record["image_id"],
                "digest_ref": record["digest_ref"],
            }

        controller.runner.run(
            self._compose(
                controller,
                "exec",
                "-T",
                "backend",
                "python",
                "-m",
                "app.postgres_preflight",
                "--mode",
                "runtime",
                "--strict",
            ),
            cwd=controller.production_root,
            env=environment,
        )
        controller.runner.run(
            self._compose(
                controller,
                "exec",
                "-T",
                "backend",
                "python",
                "-I",
                "-c",
                "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=30).read()",
            ),
            cwd=controller.production_root,
            env=environment,
        )
        values = validate_deploy_control_values(
            parse_literal_env(controller.config_dir / "deploy.env"),
            runtime_root=controller.runtime_root,
        )
        postgres_user = values.get("NEXPOLY_POSTGRES_USER")
        postgres_database = values.get("NEXPOLY_POSTGRES_DB")
        if not postgres_user or postgres_database != "nexpoly":
            raise PullDeployError("production PostgreSQL identity is not pinned")
        controller.runner.run(
            self._compose(
                controller,
                "exec",
                "-T",
                "lab-postgres",
                "pg_isready",
                "--host",
                "127.0.0.1",
                "--username",
                postgres_user,
                "--dbname",
                postgres_database,
            ),
            cwd=controller.production_root,
            env=environment,
        )

        active = controller._active_slot()
        slot_record = descriptor["monomer_md"]["slot_record"]
        if active is None or any(
            active[key] != slot_record[key]
            for key in ("source_sha", "source_tree", "worker_lock_sha256")
        ):
            raise PullDeployError("active Worker slot identity differs from candidate")
        sockets = dict(self._worker_sockets(controller, require_md=True))
        worker = self._worker_request(
            controller, sockets["monomer-md"], method="GET", endpoint="/health"
        )
        expected_accepting: bool | None
        if allow_active_worker and worker.get("draining") is True:
            expected_accepting = False
        else:
            expected_accepting = None if allow_active_worker else require_ingress
        self._validate_worker_runtime_identity(
            controller,
            descriptor,
            worker,
            expected_accepting=expected_accepting,
            allow_active=allow_active_worker,
        )
        return {
            "repository": repository,
            "asset": {key: asset[key] for key in ASSET_RELEASE_FIELDS},
            "unit": unit_fields,
            "containers": running,
            "worker": worker,
            "postgres_loopback": True,
            "verified_at": utc_now(),
        }

    def verify(
        self, controller: "PullDeployController", descriptor: dict[str, Any]
    ) -> dict[str, Any]:
        runtime_identity = self.verify_runtime_identity(
            controller, descriptor, require_ingress=False
        )
        environment = self._environment(controller, descriptor)
        repository = controller.repository_identity(require_ssh_origin=True)
        if (
            repository["sha"] != descriptor["repository"]["target_sha"]
            or repository["tree"] != descriptor["repository"]["target_tree"]
        ):
            raise PullDeployError("live checkout identity differs during verification")
        asset = descriptor["release_input"]["asset"]
        if controller._asset_pointer_target(descriptor) != Path(asset["root"]):
            raise PullDeployError("live external asset pointer differs from candidate")
        if inspect_asset_release(Path(asset["root"]), asset["manifest_sha256"]) != {
            key: asset[key] for key in ASSET_RELEASE_FIELDS
        }:
            raise PullDeployError("live external asset release identity changed")
        unit = descriptor["monomer_md"]["systemd_unit"]
        if sha256_file(Path(unit["target_path"])) != unit["sha256"]:
            raise PullDeployError("running Worker unit file differs from candidate")
        unit_fields = controller._worker_unit_state(Path(unit["target_path"]))
        if unit_fields != {
            "LoadState": "loaded",
            "FragmentPath": unit["target_path"],
            "DropInPaths": "",
            "NeedDaemonReload": "no",
            "UnitFileState": "enabled",
        }:
            raise PullDeployError(
                "Worker systemd enabled runtime identity differs from candidate"
            )
        for role in ("backend", "web"):
            record = descriptor["images"][role]
            inspected = controller.runner.run(
                ["docker", "image", "inspect", record["digest_ref"]],
                env=controller.control_environment(),
            )
            try:
                values = json.loads(str(inspected.stdout))
            except json.JSONDecodeError as exc:
                raise PullDeployError(
                    f"{role} image inspection returned invalid JSON"
                ) from exc
            if not isinstance(values, list) or len(values) != 1:
                raise PullDeployError(f"{role} immutable image is unavailable")
            image = values[0]
            labels = (
                image.get("Config", {}).get("Labels", {})
                if isinstance(image, dict)
                else {}
            )
            if (
                record["digest_ref"] not in image.get("RepoDigests", [])
                or labels.get("org.opencontainers.image.revision")
                != descriptor["repository"]["target_sha"]
            ):
                raise PullDeployError(f"{role} immutable image identity changed")
        controller.runner.run(
            self._compose(
                controller,
                "exec",
                "-T",
                "backend",
                "python",
                "-m",
                "app.postgres_preflight",
                "--mode",
                "runtime",
                "--strict",
            ),
            cwd=controller.production_root,
            env=environment,
        )
        controller.runner.run(
            self._compose(
                controller,
                "exec",
                "-T",
                "backend",
                "python",
                "-I",
                "-c",
                "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=30).read()",
            ),
            cwd=controller.production_root,
            env=environment,
        )
        active = controller._active_slot()
        if active is None or any(
            active[key] != descriptor["monomer_md"]["slot_record"][key]
            for key in ("source_sha", "source_tree", "worker_lock_sha256")
        ):
            raise PullDeployError("active Worker slot identity differs from candidate")
        sockets = dict(self._worker_sockets(controller, require_md=True))
        md_socket = sockets.get("monomer-md")
        if md_socket is None:
            raise PullDeployError("monomer MD Worker socket is missing after start")
        worker = self._worker_request(
            controller, md_socket, method="GET", endpoint="/health"
        )
        self._validate_worker_runtime_identity(
            controller, descriptor, worker, expected_accepting=False
        )
        web = self._verify_web_image(controller, descriptor)
        # The public Web container is still stopped. Temporarily resume only
        # the internal Backend/Worker control planes, run the reviewed target
        # smoke inside the Backend network namespace, then re-drain before any
        # state is committed or ingress can start.
        resume: dict[str, Any] | None = None
        redrain: dict[str, Any] | None = None
        resume_attempted = False
        canary_backend_process = self._backend_process_identity(controller, descriptor)
        try:
            resume_attempted = True
            resume = validate_persistent_drain_evidence(
                self._control_cli(
                    controller,
                    descriptor,
                    "resume",
                    "--actor",
                    "pull-deploy-controller",
                    "--release-sha",
                    self._drain_authority_sha(descriptor),
                ),
                expected_enabled=False,
                authority_sha=self._drain_authority_sha(descriptor),
            )
            for _name, socket in self._worker_sockets(controller, require_md=True):
                validate_worker_control_evidence(
                    self._worker_request(
                        controller, socket, method="POST", endpoint="/resume"
                    ),
                    action="resume",
                    require_zero=True,
                )
            smoke_payload = controller._git_show(
                descriptor["repository"]["target_sha"],
                "scripts/monomer_md_smoke.py",
            )
            smoke = controller.runner.run(
                self._compose(
                    controller,
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
                    descriptor["release_input"]["asset"]["byteff2_commit"],
                    "--operation-id",
                    descriptor["operation_id"],
                    "--source-sha",
                    descriptor["repository"]["target_sha"],
                ),
                cwd=controller.production_root,
                env=environment,
                text=False,
                stdin=io.BytesIO(smoke_payload),
                stdout=subprocess.PIPE,
                timeout=900,
            )
            gpu_smoke = controller.runner.run(
                self._compose(
                    controller,
                    "exec",
                    "-T",
                    "backend",
                    "python",
                    "-I",
                    "-",
                    "600",
                ),
                cwd=controller.production_root,
                env=environment,
                text=False,
                stdin=io.BytesIO(
                    _load_governance_core().CONTRACT_GPU_API_SMOKE_PROGRAM.encode(
                        "utf-8"
                    )
                ),
                stdout=subprocess.PIPE,
                timeout=900,
            )
        finally:
            if resume_attempted:
                redrain = validate_persistent_drain_evidence(
                    self._control_cli(
                        controller,
                        descriptor,
                        "drain",
                        "--actor",
                        "pull-deploy-controller",
                        "--release-sha",
                        self._drain_authority_sha(descriptor),
                        "--reason",
                        f"post-canary drain {descriptor['operation_id']}",
                    ),
                    expected_enabled=True,
                    authority_sha=self._drain_authority_sha(descriptor),
                )
                worker_instances: dict[str, str] = {}
                for name, socket in self._worker_sockets(controller, require_md=True):
                    response = validate_worker_control_evidence(
                        self._worker_request(
                            controller, socket, method="POST", endpoint="/drain"
                        ),
                        action="drain",
                        require_zero=False,
                    )
                    worker_instances[name] = response["worker_instance_id"]
                self._wait_for_zero_work(
                    controller,
                    descriptor,
                    worker_instances,
                    canary_backend_process,
                )
        if resume is None or redrain is None:
            raise PullDeployError("candidate canary admission fencing is incomplete")
        smoke_output = bytes(smoke.stdout).decode("utf-8", "strict").strip()
        if not smoke_output.startswith("monomer MD 300-step smoke completed: "):
            raise PullDeployError(
                "authoritative monomer MD canary returned malformed evidence"
            )
        try:
            gpu_result = json.loads(
                bytes(gpu_smoke.stdout).decode("utf-8", "strict").strip()
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PullDeployError(
                "GPU model canary returned malformed evidence"
            ) from exc
        if gpu_result != {
            "conditional_generation": "completed",
            "polytao": "completed",
        }:
            raise PullDeployError(
                "GPU model canary did not complete all required models"
            )
        canary = {
            "status": "passed",
            "backend_resume": resume,
            "post_canary_drain": redrain,
            "output": smoke_output[:500],
            "gpu_models": gpu_result,
        }
        recovery_fence = self._capture_runtime_recovery_fence(
            controller, descriptor, resumed=False
        )
        return {
            "health": "ok",
            "runtime_identity": runtime_identity,
            "repository": repository,
            "worker": worker,
            "web": web,
            "canary": canary,
            "recovery_fence": recovery_fence,
            "verified_at": utc_now(),
        }

    def resume(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
        expected_verification: dict[str, Any],
    ) -> None:
        expected = self._expected_runtime_recovery_fence(expected_verification)
        environment = self._environment(controller, descriptor)
        expected_postgres = self._assert_sealed_postgres_runtime(
            controller,
            descriptor,
            phase="before admission resume",
        )
        self._isolate_ingress(controller, descriptor)
        try:
            # The persisted fence describes the fully drained runtime.  It is
            # checked before either process-local Worker admission or public
            # ingress is changed.
            if (
                self._capture_runtime_recovery_fence(
                    controller, descriptor, resumed=False
                )
                != expected
            ):
                raise PullDeployError(
                    "drained runtime instance differs from committed verification"
                )
            for _name, socket in self._worker_sockets(controller, require_md=True):
                validate_worker_control_evidence(
                    self._worker_request(
                        controller, socket, method="POST", endpoint="/resume"
                    ),
                    action="resume",
                    require_zero=True,
                )
            if (
                self._capture_runtime_recovery_fence(
                    controller, descriptor, resumed=True
                )
                != expected
            ):
                raise PullDeployError("Worker instance changed before ingress resume")
            validate_persistent_drain_evidence(
                self._control_cli(
                    controller,
                    descriptor,
                    "resume",
                    "--actor",
                    "pull-deploy-controller",
                    "--release-sha",
                    self._drain_authority_sha(descriptor),
                ),
                expected_enabled=False,
                authority_sha=self._drain_authority_sha(descriptor),
            )
            # Backend admission opens while ingress remains isolated.  Prove
            # the exact internal runtime before exposing a public listener.
            if not self.admission_is_open(controller, descriptor):
                raise PullDeployError("runtime admission is not open after resume")
            self.verify_runtime_identity(
                controller,
                descriptor,
                require_ingress=False,
                allow_active_worker=True,
            )
            if (
                self._capture_runtime_recovery_fence(
                    controller, descriptor, resumed=True
                )
                != expected
            ):
                raise PullDeployError(
                    "internally resumed runtime differs from committed verification"
                )
            self._assert_sealed_postgres_runtime(
                controller,
                descriptor,
                expected=expected_postgres,
                phase="before ingress resume",
            )
            controller.runner.run(
                self._compose(
                    controller,
                    "up",
                    "-d",
                    "--no-build",
                    "--wait",
                    "--wait-timeout",
                    "300",
                    "--no-deps",
                    "nginx",
                ),
                cwd=controller.production_root,
                env=environment,
            )
            self._assert_sealed_postgres_runtime(
                controller,
                descriptor,
                expected=expected_postgres,
                phase="while resuming ingress",
            )
            # Public ingress is the final admission mutation.  Verify the
            # exact open runtime immediately after it.
            self.verify_open_runtime(controller, descriptor, expected_verification)
        except BaseException:
            # Never leave public ingress exposed after a partial or
            # unverifiable resume.  Backend/Worker admission is reconciled by
            # the ingress-first recovery path on the next attempt.
            self._isolate_ingress(controller, descriptor)
            raise

    def _verify_resumed_runtime(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
    ) -> None:
        self.verify_runtime_identity(controller, descriptor, require_ingress=True)
        for endpoint in ("/", "/health", "/api/v1/monomer-md/status"):
            controller.runner.run(
                [
                    "curl",
                    "--disable",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--noproxy",
                    "*",
                    "--proto",
                    "=http",
                    "--max-time",
                    "60",
                    f"http://127.0.0.1:9000{endpoint}",
                ],
                env=controller.control_environment(),
                stdout=subprocess.DEVNULL,
            )

    def resume_unchanged(
        self,
        controller: "PullDeployController",
        descriptor: dict[str, Any],
        persist_verification: Callable[[dict[str, Any]], None],
        expected_verification: dict[str, Any] | None = None,
    ) -> None:
        """Re-open a sealed pre-stop runtime without restarting its readers.

        Unlike the normal post-switch path this never runs ``systemctl start``
        or Compose for Backend/PostgreSQL.  It may start only nginx with
        ``--no-deps`` after both process identities, the PostgreSQL fence and
        the Worker socket set survive unchanged.
        """

        recovery = self.prepare_recovery_runtime(
            controller,
            descriptor,
            expected_verification,
            allow_unfenced=expected_verification is None,
        )
        if recovery.get("runtime_state") != "drained" or not isinstance(
            recovery.get("verification"), dict
        ):
            raise PullDeployError("unchanged pre-stop runtime is not live and drained")
        verification = dict(recovery["verification"])
        verification["mode"] = "unchanged-runtime-resume"
        expected = self._expected_runtime_recovery_fence(verification)
        # Persist the exact drained processes and Worker instances before any
        # process-local or database-backed admission is changed.
        persist_verification(verification)
        environment = self._environment(controller, descriptor)
        expected_postgres = self._assert_sealed_postgres_runtime(
            controller,
            descriptor,
            phase="before unchanged admission resume",
        )
        try:
            if (
                self._capture_runtime_recovery_fence(
                    controller, descriptor, resumed=False
                )
                != expected
            ):
                raise PullDeployError(
                    "pre-stop runtime changed after recovery fence was persisted"
                )
            for name, socket in self._worker_sockets(controller, require_md=True):
                resumed = validate_worker_control_evidence(
                    self._worker_request(
                        controller, socket, method="POST", endpoint="/resume"
                    ),
                    action="resume-unchanged",
                    require_zero=True,
                )
                if (
                    resumed["worker_instance_id"]
                    != expected["workers"][name]["worker_instance_id"]
                ):
                    raise PullDeployError(
                        f"{name} Worker instance changed while resuming unchanged"
                    )
            if (
                self._capture_runtime_recovery_fence(
                    controller, descriptor, resumed=True
                )
                != expected
            ):
                raise PullDeployError(
                    "pre-stop Worker instance changed before Backend admission"
                )
            validate_persistent_drain_evidence(
                self._control_cli(
                    controller,
                    descriptor,
                    "resume",
                    "--actor",
                    "pull-deploy-controller",
                    "--release-sha",
                    self._drain_authority_sha(descriptor),
                ),
                expected_enabled=False,
                authority_sha=self._drain_authority_sha(descriptor),
            )
            if not self.admission_is_open(controller, descriptor):
                raise PullDeployError(
                    "unchanged runtime admission is not open after resume"
                )
            self.verify_runtime_identity(
                controller,
                descriptor,
                require_ingress=False,
                allow_active_worker=True,
            )
            if (
                self._capture_runtime_recovery_fence(
                    controller, descriptor, resumed=True
                )
                != expected
            ):
                raise PullDeployError(
                    "internally resumed unchanged runtime changed instance"
                )
            self._assert_sealed_postgres_runtime(
                controller,
                descriptor,
                expected=expected_postgres,
                phase="before unchanged ingress resume",
            )
            controller.runner.run(
                self._compose(
                    controller,
                    "up",
                    "-d",
                    "--no-build",
                    "--wait",
                    "--wait-timeout",
                    "300",
                    "--no-deps",
                    "nginx",
                ),
                cwd=controller.production_root,
                env=environment,
            )
            self._assert_sealed_postgres_runtime(
                controller,
                descriptor,
                expected=expected_postgres,
                phase="while resuming unchanged ingress",
            )
            self.verify_open_runtime(controller, descriptor, verification)
        except BaseException:
            self._isolate_ingress(controller, descriptor)
            raise


class PullDeployController:
    def __init__(
        self,
        production_root: Path,
        runtime_root: Path,
        *,
        runner: CommandRunner | None = None,
        lifecycle: Lifecycle | None = None,
        apply: bool = False,
    ) -> None:
        self.production_root = production_root.resolve()
        self.runtime_root = runtime_root.resolve()
        self.test_root_mode = test_root_mode(
            production_root=self.production_root,
            runtime_root=self.runtime_root,
        )
        self.runner = runner or SystemRunner()
        self.lifecycle = lifecycle or SystemLifecycle()
        self.apply_enabled = apply
        self.bin_dir = self.runtime_root / "bin"
        self.config_dir = self.runtime_root / "config"
        self.docker_config_dir = self.config_dir / "docker"
        self.state_dir = self.runtime_root / "state"
        self.audit_dir = self.runtime_root / "audit"
        self.backups_dir = self.runtime_root / "backups"
        self.wheel_cache_dir = self.runtime_root / "wheel-cache"
        self.venv_root = self.runtime_root / "worker-venvs"
        self.control_releases_dir = self.runtime_root / "control-releases"
        self.prepared_root = self.state_dir / "prepared"
        self.prepare_aborts_dir = self.state_dir / "prepare-aborts"
        self.prepare_abort_archives_dir = self.prepare_aborts_dir / "archives"
        self.control_handoffs_dir = self.state_dir / "control-handoffs"
        self.slots_state_dir = self.state_dir / "worker-slots"
        self.lock_path = self.state_dir / "deploy.lock"
        self.marker_path = self.state_dir / "deploy-in-progress.json"
        self.contract_marker_path = self.state_dir / "contract-0012-in-progress.json"
        self.bridge_token_path = self.state_dir / "bridge-takeover.json"
        self.git_permission_marker_path = (
            _git_source_trust.permission_takeover_marker_path(
                self.runtime_root
            )
        )
        self.current_state_path = self.state_dir / "current-deployment.json"
        self.active_slot_path = self.state_dir / "monomer-md-active-slot.json"
        self.active_control_path = self.state_dir / "active-control.json"
        self._held_deploy_lock_fd: int | None = None

    def _require_no_contract_maintenance(
        self, *, require_alias_completed: bool = True
    ) -> None:
        if self.contract_marker_path.exists() or self.contract_marker_path.is_symlink():
            raise PullDeployError(
                "interrupted 0012 maintenance must be recovered before code deployment"
            )
        try:
            alias_marker = _control_runtime.load_production_0005_alias_gate(
                self.runtime_root, require_completed=require_alias_completed
            )
        except Exception as exc:
            raise PullDeployError(
                "completed production 0005 alias reconciliation is required"
            ) from exc
        if (
            not require_alias_completed
            and alias_marker is not None
            and alias_marker.get("phase") != "completed"
        ):
            raise PullDeployError(
                "interrupted production 0005 alias reconciliation must recover first"
            )

    def _ensure_control_runtime_roots(self, *, mutating: bool) -> None:
        if mutating and self.apply_enabled and not self.test_root_mode:
            if (
                self.production_root != PRODUCTION_ROOT
                or self.runtime_root != RUNTIME_ROOT
            ):
                raise PullDeployError(
                    "production mutation is locked to the exact production and runtime roots"
                )
            active_root = os.environ.get("NEXPOLY_ACTIVE_CONTROL_ROOT")
            if (
                not active_root
                or Path(__file__).resolve().parent != Path(active_root).resolve()
            ):
                raise PullDeployError(
                    "production mutation must run a selector-authorized control release"
                )
            selected_id = os.environ.get("NEXPOLY_ACTIVE_CONTROL_RELEASE_ID")
            try:
                selected_manifest, selected_root = (
                    _control_runtime.load_control_release(
                        self.runtime_root, selected_id
                    )
                )
            except Exception as exc:
                raise PullDeployError(
                    "selector-authorized control release is invalid"
                ) from exc
            if (
                selected_root.resolve() != Path(active_root).resolve()
                or selected_manifest["release_id"] != selected_id
            ):
                raise PullDeployError(
                    "selector control release environment is inconsistent"
                )
        for directory in (
            self.runtime_root,
            self.bin_dir,
            self.config_dir,
            self.docker_config_dir,
            self.state_dir,
            self.audit_dir,
            self.backups_dir,
            self.wheel_cache_dir,
            self.venv_root,
            self.control_releases_dir,
            self.prepared_root,
            self.control_handoffs_dir,
            self.slots_state_dir,
        ):
            ensure_private_directory(directory)
        canary_state_directory = self.state_dir / "monomer-md-canaries"
        canary_state_existed = (
            canary_state_directory.exists()
            or canary_state_directory.is_symlink()
        )
        if canary_state_existed or mutating:
            ensure_private_directory(
                canary_state_directory,
                create=mutating,
            )
            if mutating and not canary_state_existed:
                fsync_directory(canary_state_directory)
                fsync_directory(self.state_dir)

    def _ensure_production_checkout_root(self) -> None:
        try:
            root_metadata = self.production_root.lstat()
            git_metadata = (self.production_root / ".git").lstat()
        except OSError as exc:
            raise PullDeployError("production Git checkout is missing") from exc
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or self.production_root.is_symlink()
            or not stat.S_ISDIR(git_metadata.st_mode)
            or (self.production_root / ".git").is_symlink()
            or root_metadata.st_uid != os.geteuid()
            or git_metadata.st_uid != os.geteuid()
            or root_metadata.st_mode & 0o022
            or git_metadata.st_mode & 0o022
        ):
            raise PullDeployError(
                "production checkout and .git must be owner-controlled and non-writable by group/other"
            )

    def ensure_roots(self, *, mutating: bool) -> None:
        self._ensure_control_runtime_roots(mutating=mutating)
        self._ensure_production_checkout_root()

    @contextlib.contextmanager
    def deployment_lock(self) -> Iterable[int]:
        try:
            descriptor = os.open(
                self.lock_path,
                os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as exc:
            raise PullDeployError("deploy.lock is unavailable or unsafe") from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise PullDeployError("deploy.lock must be deploy-user-owned mode 0600")
            stream = os.fdopen(descriptor, "a+", encoding="utf-8")
            descriptor = -1
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        with stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PullDeployError("another deployment holds deploy.lock") from exc
            if self._held_deploy_lock_fd is not None:
                raise PullDeployError("nested deployment lock ownership is invalid")
            self._held_deploy_lock_fd = stream.fileno()
            try:
                yield stream.fileno()
            finally:
                self._held_deploy_lock_fd = None

    @contextlib.contextmanager
    def offline_bridge_revalidation(self) -> Iterable[None]:
        """Enforce cache-only validation for the stopped first bridge."""

        if isinstance(self.runner, OfflineBridgeRunner):
            yield
            return
        original = self.runner
        self.runner = OfflineBridgeRunner(original)
        try:
            yield
        finally:
            self.runner = original

    def _clean_environment(self) -> dict[str, str]:
        key = self.config_dir / "git-deploy-key"
        known_hosts = self.config_dir / "known_hosts"
        for path in (key, known_hosts):
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or path.is_symlink()
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise PullDeployError(f"Git credential material is unsafe: {path}")
        ssh_command = (
            f"/usr/bin/ssh -i {key} -o IdentitiesOnly=yes "
            f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={known_hosts} "
            "-o BatchMode=yes"
        )
        if self.test_root_mode and not self._has_complete_test_git_layout():
            # Existing unit-test runners model Git without a filesystem
            # repository.  This branch is unreachable for the fixed
            # production root.
            return {
                **self.control_environment(),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_ATTR_NOSYSTEM": "1",
                "GIT_DIR": str(self.production_root / ".git"),
                "GIT_WORK_TREE": str(self.production_root),
                "GIT_SSH_COMMAND": ssh_command,
            }
        try:
            return _git_source_trust.safe_git_environment(
                self.production_root,
                ambient=os.environ,
                home=str(DEPLOY_USER_HOME),
                ssh_command=ssh_command,
            )
        except Exception as exc:
            raise PullDeployError(
                "production Git execution environment is unsafe"
            ) from exc

    def _has_complete_test_git_layout(self) -> bool:
        git_dir = self.production_root / ".git"
        return (
            git_dir.is_dir()
            and not git_dir.is_symlink()
            and (git_dir / "objects").is_dir()
            and (git_dir / "refs").is_dir()
            and (git_dir / "config").is_file()
            and (git_dir / "HEAD").is_file()
            and (git_dir / "index").is_file()
        )

    def _git_trust_preflight(self) -> dict[str, Any] | None:
        if self.test_root_mode and not self._has_complete_test_git_layout():
            return None
        self._git_permission_takeover()
        environment = self._clean_environment()
        try:
            return _git_source_trust.repository_preflight_evidence(
                self.production_root,
                ambient=os.environ,
                home=str(DEPLOY_USER_HOME),
                ssh_command=environment["GIT_SSH_COMMAND"],
            )
        except Exception as exc:
            raise PullDeployError(
                "production Git trust preflight failed"
            ) from exc

    def _git_permission_takeover(self) -> dict[str, Any] | None:
        if (
            self.test_root_mode
            and not self.git_permission_marker_path.exists()
            and not self.git_permission_marker_path.is_symlink()
        ):
            # Unit-test repositories are not production authority. Complete
            # tests may opt into the real marker by creating it explicitly.
            return None
        try:
            return (
                _git_source_trust.verify_repository_permission_takeover(
                    self.production_root,
                    self.git_permission_marker_path,
                    verify_content=False,
                )
            )
        except Exception as exc:
            raise PullDeployError(
                "production Git permission takeover is unavailable"
            ) from exc

    def control_environment(self) -> dict[str, str]:
        return clean_control_environment(self.runtime_root)

    def production_deploy_values(self, *, check_free_space: bool) -> dict[str, str]:
        values = validate_deploy_control_values(
            parse_literal_env(self.config_dir / "deploy.env"),
            runtime_root=self.runtime_root,
        )
        # app.env is application-owned but must still be outside Git and
        # owner-only before Compose is allowed to read it.  Control/database
        # values have exactly one authority: deploy.env.  A duplicate in the
        # lower-precedence env_file is rejected instead of silently shadowed.
        app_values = parse_literal_env(self.config_dir / "app.env")
        required = {
            "NEXPOLY_POSTGRES_USER",
            "NEXPOLY_POSTGRES_PASSWORD",
            "NEXPOLY_POSTGRES_DB",
            "APP_POSTGRES_DSN",
            "PI_POSTGRES_DSN",
            "LAB_DATA_POSTGRES_DSN",
            "POLYTAO_ENABLED",
            "MONOMER_MD_REQUIRE_TRANSPORT_READY",
            "NEXPOLY_POSTGRES_PORT",
            "NEXPOLY_HEALTH_URLS",
            "NEXPOLY_MIN_FREE_BYTES",
            "NEXPOLY_POSTGRES_RESTORE_TMPFS_BYTES",
        }
        missing = sorted(key for key in required if not values.get(key))
        if missing:
            raise PullDeployError(
                "deploy.env is missing required production values: "
                + ", ".join(missing)
            )
        if values["NEXPOLY_POSTGRES_DB"] != "nexpoly":
            raise PullDeployError("production database must be exactly nexpoly")
        if values["NEXPOLY_POSTGRES_PASSWORD"] in {
            "polyprop",
            "nexpoly",
            "password",
            "<rotate-to-a-random-password>",
        }:
            raise PullDeployError("production PostgreSQL password is a known default")
        overlap = sorted(set(app_values) & set(values))
        if overlap:
            raise PullDeployError(
                "app.env duplicates deploy-owned production values: "
                + ", ".join(overlap)
            )
        protected_app_names = sorted(
            set(app_values)
            & {
                "APP_POSTGRES_DSN",
                "PI_POSTGRES_DSN",
                "LAB_DATA_POSTGRES_DSN",
                "NEXPOLY_POSTGRES_USER",
                "NEXPOLY_POSTGRES_PASSWORD",
                "NEXPOLY_POSTGRES_DB",
                "NEXPOLY_POSTGRES_PORT",
                "MONOMER_MD_REQUIRE_TRANSPORT_READY",
            }
        )
        if protected_app_names:
            raise PullDeployError(
                "app.env contains deploy-owned database/readiness values: "
                + ", ".join(protected_app_names)
            )
        if values["NEXPOLY_POSTGRES_PORT"] != "55432":
            raise PullDeployError("production PostgreSQL host port must remain 55432")
        if values["NEXPOLY_HEALTH_URLS"] != ("http://127.0.0.1:9000/health"):
            raise PullDeployError(
                "production health URL must remain loopback port 9000"
            )
        if values["POLYTAO_ENABLED"] != "true":
            raise PullDeployError("PolyTAO must remain enabled in production")
        if values["MONOMER_MD_REQUIRE_TRANSPORT_READY"] != "true":
            raise PullDeployError(
                "production deployment must require Transport readiness"
            )
        try:
            core = _load_governance_core()
            for key in ("APP_POSTGRES_DSN", "PI_POSTGRES_DSN", "LAB_DATA_POSTGRES_DSN"):
                core.validate_postgres_dsn(
                    values[key],
                    key,
                    expected_user=values["NEXPOLY_POSTGRES_USER"],
                    expected_password=values["NEXPOLY_POSTGRES_PASSWORD"],
                    expected_host="lab-postgres",
                    expected_port=5432,
                    expected_database="nexpoly",
                )
        except Exception as exc:
            raise PullDeployError(
                "production PostgreSQL DSN identity is invalid"
            ) from exc
        if check_free_space:
            raw_minimum = values.get("NEXPOLY_MIN_FREE_BYTES", str(10 * 1024**3))
            try:
                minimum = int(raw_minimum)
            except ValueError as exc:
                raise PullDeployError(
                    "NEXPOLY_MIN_FREE_BYTES must be an integer"
                ) from exc
            if not 1024**3 <= minimum <= 1024**4:
                raise PullDeployError(
                    "deployment free-space threshold is outside 1 GiB..1 TiB"
                )
            for path in (self.production_root, self.runtime_root):
                if shutil.disk_usage(path).free < minimum:
                    raise PullDeployError(f"insufficient deployment free space: {path}")
        return values

    def production_config_evidence(self, *, check_free_space: bool) -> dict[str, str]:
        self.production_deploy_values(check_free_space=check_free_space)
        self._clean_environment()
        self._github_token()
        hook_digests: dict[str, str] = {}
        for name in (
            "bootstrap-quiesce",
            "bootstrap-status",
            "bootstrap-resume-unchanged",
            "bootstrap-rollback",
            "bootstrap-active-jobs-probe",
            "bootstrap-legacy-runtime-status",
            "bootstrap-legacy-runtime-resume-unchanged",
            "bootstrap-legacy-runtime-restore",
            MUTABLE_DATA_AUDIT_HELPER,
        ):
            path = self.config_dir / name
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or path.is_symlink()
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise PullDeployError(f"bootstrap hook is unsafe: {path}")
            hook_digests[name.replace("-", "_") + "_sha256"] = sha256_file(path)
        mutable_config_digests: dict[str, str] = {}
        for name, evidence_key in (
            (
                MUTABLE_DATA_SERVICE_CONFIG,
                "mutable_data_audit_pg_service_sha256",
            ),
            (MUTABLE_DATA_PGPASS, "mutable_data_audit_pgpass_sha256"),
        ):
            path = self.config_dir / name
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise PullDeployError(
                    f"mutable-data private input is unavailable: {name}"
                ) from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or path.is_symlink()
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size < 1
                or metadata.st_size > 64 * 1024
            ):
                raise PullDeployError(
                    f"mutable-data private input is unsafe: {name}"
                )
            mutable_config_digests[evidence_key] = sha256_file(path)
        evidence = {
            "deploy_env_sha256": sha256_file(self.config_dir / "deploy.env"),
            "app_env_sha256": sha256_file(self.config_dir / "app.env"),
            "git_deploy_key_sha256": sha256_file(self.config_dir / "git-deploy-key"),
            "known_hosts_sha256": sha256_file(self.config_dir / "known_hosts"),
            "github_api_token_sha256": sha256_file(
                self.config_dir / "github-api-token"
            ),
            "docker_config_sha256": sha256_file(self.config_dir / "docker/config.json"),
            **hook_digests,
            **mutable_config_digests,
        }
        return validate_production_config_evidence(evidence)

    def external_database_audit_evidence(
        self,
        policy: dict[str, Any],
        *,
        lightweight_revalidation: bool = False,
        role_sql_authority: object | None = None,
        helper_control_authority: object | None = None,
    ) -> dict[str, Any]:
        """Capture and seal a fresh, complete external PostgreSQL audit."""

        values = self.production_deploy_values(check_free_space=False)
        expected_helper = self.bin_dir / EXTERNAL_DATABASE_AUDIT_HELPER
        expected_authority_rules = (
            self.config_dir / EXTERNAL_DATABASE_MEDIA_AUTHORITY_RULES
        )
        expected_registry = (
            self.config_dir / EXTERNAL_DATABASE_MEDIA_REGISTRY
        )
        if (
            values.get(CONTRACT_0012_EXTERNAL_AUDIT_COMMAND)
            != str(expected_helper)
        ):
            raise PullDeployError(
                "external database audit command is not the fixed helper"
            )
        _authority_payload, authority_rules_sha256 = private_regular_file(
            expected_authority_rules,
            mode=0o600,
            maximum_bytes=4 * 1024 * 1024,
        )
        try:
            active_control, control_manifest, control_root = (
                _control_runtime.load_active_control(self.runtime_root)
            )
        except Exception as exc:
            raise PullDeployError(
                "active control release is unavailable for media audit"
            ) from exc
        helper_control_files = {
            "launcher_sha256": "postgres_media_launcher.py",
            "implementation_sha256": "postgres_media_evidence.py",
            "authority_rules_sha256": (
                "postgres-media-authority-rules.json"
            ),
            "role_sql_sha256": EXTERNAL_DATABASE_AUDIT_ROLE_SQL,
        }
        helper_control: dict[str, Any] = {
            "release_id": active_control["release_id"],
            "source_sha": control_manifest["source_sha"],
            "source_tree": control_manifest["source_tree"],
            "manifest_sha256": sha256_file(
                control_root / _control_runtime.CONTROL_MANIFEST_NAME
            ),
        }
        for field, name in helper_control_files.items():
            record = control_manifest["files"].get(name)
            path = control_root / name
            payload, digest = private_regular_file(
                path,
                mode=0o700,
                maximum_bytes=16 * 1024 * 1024,
            )
            if record != {
                "sha256": digest,
                "size": len(payload),
                "mode": 0o700,
            }:
                raise PullDeployError(
                    "active PostgreSQL media control closure differs"
                )
            helper_control[field] = digest
        if helper_control_authority is None:
            expected_helper_control = helper_control
        else:
            if not isinstance(helper_control_authority, dict):
                raise PullDeployError(
                    "sealed F helper control authority is invalid"
                )
            expected_helper_control = helper_control_authority
            for field in (
                "launcher_sha256",
                "implementation_sha256",
                "authority_rules_sha256",
                "role_sql_sha256",
            ):
                require_digest(
                    expected_helper_control.get(field),
                    f"sealed F helper control {field}",
                )
        if role_sql_authority is None:
            role_control = active_control
            role_manifest = control_manifest
            role_root = control_root
            role_sql_path = role_root / EXTERNAL_DATABASE_AUDIT_ROLE_SQL
        else:
            if (
                not isinstance(role_sql_authority, dict)
                or not isinstance(
                    role_sql_authority.get("control_release_id"),
                    str,
                )
            ):
                raise PullDeployError(
                    "sealed F audit-role SQL authority is invalid"
                )
            try:
                role_manifest, role_root = (
                    _control_runtime.load_control_release(
                        self.runtime_root,
                        role_sql_authority["control_release_id"],
                    )
                )
            except Exception as exc:
                raise PullDeployError(
                    "sealed F audit-role SQL control release is unavailable"
                ) from exc
            role_control = {
                "release_id": role_manifest["release_id"],
                "source_sha": role_manifest["source_sha"],
                "source_tree": role_manifest["source_tree"],
            }
            role_sql_path = role_root / EXTERNAL_DATABASE_AUDIT_ROLE_SQL
            if (
                role_sql_authority.get("path") != str(role_sql_path)
                or role_sql_authority.get("mode") != "0700"
                or role_sql_authority.get("source_sha")
                != role_manifest["source_sha"]
                or role_sql_authority.get("source_tree")
                != role_manifest["source_tree"]
            ):
                raise PullDeployError(
                    "sealed F audit-role SQL authority changed"
                )
        role_sql_payload, role_sql_sha256 = private_regular_file(
            role_sql_path,
            mode=0o700,
            maximum_bytes=4 * 1024 * 1024,
        )
        role_sql_manifest = role_manifest["files"].get(
            EXTERNAL_DATABASE_AUDIT_ROLE_SQL
        )
        if (
            not role_sql_payload
            or role_sql_manifest
            != {
                "sha256": role_sql_sha256,
                "size": len(role_sql_payload),
                "mode": 0o700,
            }
            or role_control.get("release_id")
            != role_manifest.get("release_id")
            or role_sql_authority is not None
            and role_sql_authority.get("sha256") != role_sql_sha256
        ):
            raise PullDeployError(
                "active control audit-role SQL differs from exact F"
            )
        configured_authority_rules = require_digest(
            values.get(CONTRACT_0012_MEDIA_AUTHORITY_RULES_DIGEST),
            "configured external media authority rules",
        )
        policy_authority_rules = require_digest(
            policy.get("media_authority_rules_sha256"),
            "policy external media authority rules",
        )
        policy_role_sql = require_digest(
            policy.get("audit_role_sql_sha256"),
            "policy external audit-role SQL",
        )
        if (
            authority_rules_sha256 != configured_authority_rules
            or authority_rules_sha256 != policy_authority_rules
            or role_sql_sha256 != policy_role_sql
            or helper_control["role_sql_sha256"] != policy_role_sql
        ):
            raise PullDeployError(
                "external database media authority rules differ from "
                "deploy.env or F policy"
            )
        expected_users: dict[str, str] = {}
        for stack, key in CONTRACT_0012_EXTERNAL_AUDIT_USERS.items():
            value = values.get(key)
            if (
                not isinstance(value, str)
                or re.fullmatch(r"[a-z_][a-z0-9_-]{0,62}", value) is None
            ):
                raise PullDeployError(
                    f"external database audit user is missing or invalid: {key}"
                )
            expected_users[stack] = value
        environment = self.control_environment()
        environment.update(
            {
                CONTRACT_0012_MEDIA_AUTHORITY_RULES_DIGEST: (
                    authority_rules_sha256
                ),
                "NEXPOLY_CONTRACT_0012_AUDIT_ROLE_SQL_SHA256": (
                    role_sql_sha256
                ),
                "NEXPOLY_MEDIA_LAUNCHER_SHA256": (
                    expected_helper_control["launcher_sha256"]
                ),
                "NEXPOLY_MEDIA_IMPLEMENTATION_SHA256": (
                    expected_helper_control["implementation_sha256"]
                ),
                **{
                    key: expected_users[stack]
                    for stack, key in CONTRACT_0012_EXTERNAL_AUDIT_USERS.items()
                },
            }
        )
        started_at = dt.datetime.now(dt.timezone.utc)
        with pinned_private_regular_file(
            expected_helper,
            mode=0o700,
            maximum_bytes=4 * 1024 * 1024,
        ) as (helper_descriptor, helper_payload, helper_sha256):
            if not helper_payload.startswith(b"#!"):
                raise PullDeployError(
                    "external database audit helper is not executable source"
                )
            bootstrap = load_private_json(
                self.state_dir / "bootstrap-control.json"
            )
            immutable = bootstrap.get("immutable_files")
            if (
                bootstrap.get("schema_version") != 2
                or bootstrap.get("status") != "completed"
                or not isinstance(immutable, dict)
                or immutable.get(EXTERNAL_DATABASE_AUDIT_HELPER)
                != helper_sha256
            ):
                raise PullDeployError(
                    "external database audit helper differs from bootstrap"
                )
            completed = self.runner.run(
                [
                    f"/proc/self/fd/{helper_descriptor}",
                    *(
                        ["revalidate"]
                        if lightweight_revalidation
                        else []
                    ),
                ],
                cwd=self.production_root,
                env=environment,
                timeout=60 * 60,
                pass_fds=(helper_descriptor,),
            )
        completed_at = dt.datetime.now(dt.timezone.utc)
        _helper_after, helper_after_sha256 = private_regular_file(
            expected_helper,
            mode=0o700,
            maximum_bytes=4 * 1024 * 1024,
        )
        _authority_after, authority_rules_after_sha256 = (
            private_regular_file(
                expected_authority_rules,
                mode=0o600,
                maximum_bytes=4 * 1024 * 1024,
            )
        )
        _registry_after, registry_after_sha256 = private_regular_file(
            expected_registry,
            mode=0o600,
            maximum_bytes=4 * 1024 * 1024,
        )
        if (
            helper_after_sha256 != helper_sha256
            or authority_rules_after_sha256 != authority_rules_sha256
        ):
            raise PullDeployError(
                "external database audit authority changed while executing"
            )
        snapshot = parse_command_json(
            completed.stdout,
            "external database audit",
        )
        try:
            snapshot = _site_helper_contracts.validate_external_database_audit(
                snapshot,
                expected_users=expected_users,
                expected_media_authority_rules_digest=(
                    authority_rules_sha256
                ),
                expected_runtime_registry_digest=registry_after_sha256,
            )
        except Exception as exc:
            raise PullDeployError(
                "external database audit helper returned unsafe evidence"
            ) from exc
        validate_fresh_external_database_audit(
            snapshot,
            started_at=started_at,
            completed_at=completed_at,
        )
        binding: dict[str, Any] = {
            "schema_version": 2,
            "helper": {
                "path": str(expected_helper),
                "sha256": helper_sha256,
                "mode": "0700",
            },
            "helper_control": helper_control,
            "authority_rules": {
                "path": str(expected_authority_rules),
                "sha256": authority_rules_sha256,
                "mode": "0600",
            },
            "role_sql": {
                "path": str(role_sql_path),
                "sha256": role_sql_sha256,
                "mode": "0700",
                "control_release_id": role_control["release_id"],
                "source_sha": role_manifest["source_sha"],
                "source_tree": role_manifest["source_tree"],
            },
            "role_provisioning": external_database_role_provisioning(
                snapshot,
                role_sql_sha256=role_sql_sha256,
            ),
            "registry": {
                "path": str(expected_registry),
                "sha256": registry_after_sha256,
                "mode": "0600",
                "authority_rules_sha256": authority_rules_sha256,
            },
            "expected_users": expected_users,
            "snapshot": snapshot,
            "snapshot_sha256": canonical_json_digest(snapshot),
            "state_sha256": canonical_json_digest(
                external_database_audit_state(snapshot)
            ),
            "identity_sha256": None,
        }
        binding["identity_sha256"] = canonical_json_digest(
            {
                key: value
                for key, value in binding.items()
                if key != "identity_sha256"
            }
        )
        return validate_external_database_audit_binding(
            binding,
            expected_policy=policy,
        )

    def _revalidate_external_database_audit(
        self,
        descriptor: dict[str, Any],
    ) -> dict[str, Any]:
        if (
            descriptor.get("schema_version")
            != BRIDGE_DESCRIPTOR_SCHEMA_VERSION
        ):
            raise PullDeployError(
                "external database audit CAS is restricted to the bridge"
            )
        policy = descriptor["bridge"]["policy"][
            "external_database_audit"
        ]
        expected = validate_external_database_audit_binding(
            descriptor.get("external_database_audit"),
            expected_policy=policy,
        )
        return self._revalidate_external_database_binding(
            expected,
            policy=policy,
        )

    def _revalidate_external_database_binding(
        self,
        expected_binding: object,
        *,
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        expected = validate_external_database_audit_binding(
            expected_binding,
            expected_policy=policy,
        )
        observed = self.external_database_audit_evidence(
            policy,
            lightweight_revalidation=True,
            role_sql_authority=expected["role_sql"],
            helper_control_authority=expected["helper_control"],
        )
        for field in (
            "helper",
            "authority_rules",
            "role_sql",
            "role_provisioning",
            "registry",
            "expected_users",
            "state_sha256",
        ):
            if observed[field] != expected[field]:
                raise PullDeployError(
                    "external database media changed after its sealed transition"
                )
        closure_fields = (
            "launcher_sha256",
            "implementation_sha256",
            "authority_rules_sha256",
            "role_sql_sha256",
        )
        if any(
            observed["helper_control"][field]
            != expected["helper_control"][field]
            for field in closure_fields
        ):
            raise PullDeployError(
                "external database helper control closure changed"
            )
        return observed

    def capture_alias_external_database_transition(
        self,
        *,
        bridge_authority: object,
        alias_operation_id: str,
    ) -> dict[str, Any]:
        """Capture the exact post-alias state without reusing pre-alias CAS."""

        if not isinstance(bridge_authority, dict):
            raise PullDeployError("alias bridge authority is malformed")
        descriptor_record = bridge_authority.get("descriptor")
        ready_record = bridge_authority.get("ready")
        if (
            not isinstance(descriptor_record, dict)
            or not isinstance(ready_record, dict)
            or set(descriptor_record) != {"path", "sha256"}
            or set(ready_record) != {"path", "sha256"}
        ):
            raise PullDeployError("alias bridge evidence paths are malformed")
        descriptor_path = Path(str(descriptor_record.get("path")))
        ready_path = Path(str(ready_record.get("path")))
        descriptor = validate_descriptor(load_private_json(descriptor_path))
        try:
            token = _bridge_core.load_token_authority(self.state_dir)
        except Exception as exc:
            raise PullDeployError(
                "alias bridge token authority is unavailable"
            ) from exc
        validate_alias_bridge_authority(
            bridge_authority,
            descriptor=descriptor,
            descriptor_path=descriptor_path,
            ready_path=ready_path,
            state_root=self.state_dir,
            current_token=token,
        )
        if (
            token.get("status") != "prepared"
            or token.get("operation_id") != descriptor["operation_id"]
            or token.get("descriptor_sha256")
            != descriptor_record["sha256"]
        ):
            raise PullDeployError(
                "alias transition requires the exact prepared bridge token"
            )
        policy = descriptor["bridge"]["policy"][
            "external_database_audit"
        ]
        after = self.external_database_audit_evidence(
            policy,
            lightweight_revalidation=True,
            role_sql_authority=descriptor[
                "external_database_audit"
            ]["role_sql"],
            helper_control_authority=descriptor[
                "external_database_audit"
            ]["helper_control"],
        )
        return build_external_database_alias_pair(
            descriptor["external_database_audit"],
            after,
            operation_id=alias_operation_id,
            descriptor_sha256=descriptor_record["sha256"],
        )

    def revalidate_alias_external_database_transition(
        self,
        descriptor: dict[str, Any],
        pair: object,
    ) -> dict[str, Any]:
        descriptor_path = (
            self.prepared_root
            / descriptor["operation_id"]
            / "descriptor.json"
        )
        validated = validate_external_database_alias_pair(
            pair,
            before_binding=descriptor["external_database_audit"],
        )
        if validated["descriptor_sha256"] != sha256_file(descriptor_path):
            raise PullDeployError(
                "alias external database transition belongs to another descriptor"
            )
        self._revalidate_external_database_binding(
            validated["after_binding"],
            policy=descriptor["bridge"]["policy"][
                "external_database_audit"
            ],
        )
        return validated

    def _completed_alias_external_database_baseline(
        self,
        descriptor: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Load the compact alias gate and prove its full transition chain."""

        try:
            marker = _control_runtime.load_production_0005_alias_gate(
                self.runtime_root,
                require_completed=True,
            )
        except Exception as exc:
            raise PullDeployError(
                "completed production alias evidence is unavailable"
            ) from exc
        identity = marker.get("identity") if isinstance(marker, dict) else None
        authority = (
            identity.get("bridge_authority")
            if isinstance(identity, dict)
            else None
        )
        reference = (
            marker.get("external_database_alias_transition")
            if isinstance(marker, dict)
            else None
        )
        if (
            not isinstance(authority, dict)
            or not isinstance(reference, dict)
            or not isinstance(reference.get("path"), str)
        ):
            raise PullDeployError(
                "completed production alias transition is malformed"
            )
        original_descriptor_record = authority.get("descriptor")
        if not isinstance(original_descriptor_record, dict):
            raise PullDeployError(
                "completed alias original descriptor is malformed"
            )
        original_descriptor_path = Path(
            str(original_descriptor_record.get("path"))
        )
        original_descriptor = validate_descriptor(
            load_private_json(original_descriptor_path)
        )
        pair_path = Path(reference["path"])
        if (
            pair_path
            != self.runtime_root
            / _control_runtime.ALIAS_AUDIT_ROOT_RELATIVE
            / identity["operation_id"]
            / "external-database-alias-transition.json"
            or reference.get("sha256") != sha256_file(pair_path)
        ):
            raise PullDeployError(
                "completed alias transition path or digest differs"
            )
        pair = validate_external_database_alias_pair(
            load_private_json(pair_path),
            before_binding=original_descriptor[
                "external_database_audit"
            ],
        )
        if (
            pair["descriptor_sha256"]
            != original_descriptor_record.get("sha256")
            or pair["identity_sha256"]
            != reference.get("identity_sha256")
            or pair["before_state_sha256"]
            != reference.get("before_state_sha256")
            or pair["after_binding"]["state_sha256"]
            != reference.get("after_state_sha256")
        ):
            raise PullDeployError(
                "completed alias transition reference differs"
            )
        current_descriptor_path = (
            self.prepared_root
            / descriptor["operation_id"]
            / "descriptor.json"
        )
        current_descriptor_sha256 = sha256_file(current_descriptor_path)
        if current_descriptor_sha256 == pair["descriptor_sha256"]:
            baseline = pair["after_binding"]
        else:
            # A successor is prepared only after the failed attempt restored
            # its exact pre-migration backup.  Its fresh snapshot may have new
            # audit timestamps, but it must describe the same semantic 0008
            # endpoint as the completed alias transition.  Any retained
            # 0009-0011 prefix would require its own durable failed-operation
            # transition and is deliberately not inferred here.
            baseline = validate_external_database_audit_binding(
                descriptor["external_database_audit"],
                expected_policy=descriptor["bridge"]["policy"][
                    "external_database_audit"
                ],
            )
            alias_after = pair["after_binding"]
            for field in (
                "helper",
                "registry",
                "expected_users",
                "state_sha256",
            ):
                if baseline[field] != alias_after[field]:
                    raise PullDeployError(
                        "successor bridge baseline differs from completed alias"
                    )
        return baseline, pair, reference

    def _bridge_external_database_transition_path(
        self,
        operation_id: str,
    ) -> Path:
        normal, _external = self._operation_directories(operation_id)
        return normal / "external-database-bridge-transition.json"

    def _capture_bridge_external_database_transition(
        self,
        descriptor: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        baseline, alias_pair, alias_reference = (
            self._completed_alias_external_database_baseline(descriptor)
        )
        policy = descriptor["bridge"]["policy"][
            "external_database_audit"
        ]
        observed = self.external_database_audit_evidence(
            policy,
            lightweight_revalidation=True,
            role_sql_authority=baseline["role_sql"],
            helper_control_authority=baseline["helper_control"],
        )
        descriptor_path = (
            self.prepared_root
            / descriptor["operation_id"]
            / "descriptor.json"
        )
        pair = build_external_database_bridge_pair(
            baseline,
            observed,
            operation_id=descriptor["operation_id"],
            descriptor_sha256=sha256_file(descriptor_path),
        )
        path = self._bridge_external_database_transition_path(
            descriptor["operation_id"]
        )
        self._ensure_private_operation_directory(path.parent)
        if path.exists() or path.is_symlink():
            existing = validate_external_database_bridge_pair(
                load_private_json(path),
                before_binding=baseline,
            )
            if existing != pair:
                # A retry obtains fresh timestamps.  The durable pair owns
                # those timestamps; only its semantic post-state must match.
                if (
                    existing["descriptor_sha256"]
                    != pair["descriptor_sha256"]
                    or existing["before_state_sha256"]
                    != pair["before_state_sha256"]
                    or existing["after_binding"]["state_sha256"]
                    != pair["after_binding"]["state_sha256"]
                ):
                    raise PullDeployError(
                        "bridge external database transition changed on retry"
                    )
                pair = existing
        else:
            atomic_json(path, pair)
        reference = external_database_transition_reference(
            pair,
            path=path,
        )
        return pair, reference, {
            "alias": validate_external_database_transition_reference(
                alias_reference,
                expected_kind="alias-0005-reconciliation",
            ),
            "bridge": reference,
        }

    def _load_bridge_external_database_transition(
        self,
        descriptor: dict[str, Any],
        reference: object,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        reference = validate_external_database_transition_reference(
            reference,
            expected_kind="bridge-expand-to-0011",
        )
        baseline, alias_pair, alias_reference = (
            self._completed_alias_external_database_baseline(descriptor)
        )
        path = self._bridge_external_database_transition_path(
            descriptor["operation_id"]
        )
        if (
            Path(reference["path"]) != path
            or reference["sha256"] != sha256_file(path)
        ):
            raise PullDeployError(
                "bridge external database transition path differs"
            )
        pair = validate_external_database_bridge_pair(
            load_private_json(path),
            before_binding=baseline,
        )
        expected_reference = external_database_transition_reference(
            pair,
            path=path,
        )
        if reference != expected_reference:
            raise PullDeployError(
                "bridge external database transition reference differs"
            )
        return pair, alias_pair, alias_reference

    def _final_external_database_transition_path(
        self,
        operation_id: str,
    ) -> Path:
        normal, _external = self._operation_directories(operation_id)
        return normal / "external-database-final-transition.json"

    def _capture_final_external_database_transition(
        self,
        descriptor: dict[str, Any],
        descriptor_digest: str,
    ) -> dict[str, Any]:
        previous = descriptor.get("previous_deployment")
        if not isinstance(previous, dict):
            raise PullDeployError(
                "0013 external database transition lacks previous B state"
            )
        base = validate_external_database_audit_binding(
            previous.get("external_database_audit")
        )
        contract_pair = previous.get(
            "contract_external_database_audit"
        )
        if contract_pair is None:
            raise PullDeployError(
                "0013 external database transition lacks 0012 evidence"
            )
        before = external_database_endpoint(
            base,
            contract_pair=contract_pair,
        )
        policy = {
            **_bridge_core.EXTERNAL_DATABASE_AUDIT_POLICY,
            "media_authority_rules_sha256": base[
                "authority_rules"
            ]["sha256"],
            "audit_role_sql_sha256": base["role_sql"]["sha256"],
        }
        observed = self.external_database_audit_evidence(
            policy,
            lightweight_revalidation=True,
            role_sql_authority=base["role_sql"],
            helper_control_authority=base["helper_control"],
        )
        pair = build_external_database_final_pair(
            before,
            observed,
            operation_id=descriptor["operation_id"],
            descriptor_sha256=descriptor_digest,
        )
        path = self._final_external_database_transition_path(
            descriptor["operation_id"]
        )
        self._ensure_private_operation_directory(path.parent)
        if path.exists() or path.is_symlink():
            existing = validate_external_database_final_pair(
                load_private_json(path),
                before_binding=before,
            )
            if (
                existing["operation_id"] != pair["operation_id"]
                or existing["kind"] != "expand-to-0013"
                or existing["descriptor_sha256"]
                != pair["descriptor_sha256"]
                or existing["before_identity_sha256"]
                != pair["before_identity_sha256"]
                or existing["before_state_sha256"]
                != pair["before_state_sha256"]
                or existing["after_binding"]["state_sha256"]
                != pair["after_binding"]["state_sha256"]
                or existing["after_binding"]["helper"]
                != pair["after_binding"]["helper"]
                or existing["after_binding"]["role_sql"]
                != pair["after_binding"]["role_sql"]
                or existing["after_binding"]["role_provisioning"]
                != pair["after_binding"]["role_provisioning"]
                or existing["after_binding"]["registry"]
                != pair["after_binding"]["registry"]
                or existing["after_binding"]["expected_users"]
                != pair["after_binding"]["expected_users"]
            ):
                raise PullDeployError(
                    "0013 external database transition changed on retry"
                )
            pair = existing
        else:
            atomic_json(path, pair)
        return pair

    def _load_final_external_database_transition(
        self,
        state: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Open the exact 0013 pair and the READY descriptor that created it."""

        raw_final = state.get("final_external_database_audit")
        if raw_final is None:
            return None
        active = validate_external_database_audit_binding(
            state.get("external_database_audit")
        )
        before = external_database_endpoint(
            active,
            contract_pair=state.get("contract_external_database_audit"),
        )
        final = validate_external_database_final_pair(
            raw_final,
            before_binding=before,
        )
        operation_id = final["operation_id"]
        path = self._final_external_database_transition_path(operation_id)
        normal_audit, external_audit = self._operation_directories(operation_id)
        if (
            path.parent != normal_audit
            or external_audit.exists()
            or external_audit.is_symlink()
        ):
            raise PullDeployError(
                "0013 external transition is in an ambiguous audit root"
            )
        ensure_private_directory(normal_audit)
        persisted = validate_external_database_final_pair(
            load_private_json(path),
            before_binding=before,
        )
        if persisted != final:
            raise PullDeployError(
                "0013 external transition file differs from durable state"
            )
        operation_root, descriptor_path, ready_path = self._operation_paths(
            operation_id
        )
        if (
            descriptor_path.parent != operation_root
            or not descriptor_path.is_file()
            or descriptor_path.is_symlink()
            or not ready_path.is_file()
            or ready_path.is_symlink()
            or sha256_file(descriptor_path) != final["descriptor_sha256"]
        ):
            raise PullDeployError(
                "0013 external transition descriptor provenance is unavailable"
            )
        origin_descriptor = validate_descriptor(
            load_private_json(descriptor_path)
        )
        ready = load_private_json(ready_path)
        self._validate_ready(ready, origin_descriptor, descriptor_path)
        origin_previous = origin_descriptor.get("previous_deployment")
        if not isinstance(origin_previous, dict):
            raise PullDeployError(
                "0013 external transition descriptor lacks its prior B state"
            )
        previous_active = validate_external_database_audit_binding(
            origin_previous.get("external_database_audit")
        )
        origin_before = external_database_endpoint(
            previous_active,
            contract_pair=origin_previous.get(
                "contract_external_database_audit"
            ),
            final_pair=origin_previous.get(
                "final_external_database_audit"
            ),
        )
        if (
            origin_descriptor.get("operation_id") != operation_id
            or ready.get("descriptor_sha256") != final["descriptor_sha256"]
            or origin_before != before
            or final["before_state_sha256"] != before["state_sha256"]
            or _bridge_core.FINAL_MIGRATION_RECORD
            not in origin_descriptor["migrations"]["records"]
        ):
            raise PullDeployError(
                "0013 external transition origin descriptor identity differs"
            )
        return final

    def _is_exact_direct_contract_state(
        self,
        state: Mapping[str, Any],
        *,
        descriptor: Mapping[str, Any],
        descriptor_digest: str,
    ) -> bool:
        """Allow governance growth only from one exact successful 0012 journal."""

        contract_operation = state.get("last_contract_operation")
        if not isinstance(contract_operation, str):
            return False
        contract_operation = require_operation_id(contract_operation)
        deployment_operation = require_operation_id(
            str(state.get("operation_id", ""))
        )
        matches = [
            journal
            for _path, journal in self._successful_contract_journals()
            if (
                journal.get("operation_id") == contract_operation
                and journal.get("deployment_operation_id")
                == deployment_operation
                and journal.get("pull_descriptor_sha256")
                == descriptor_digest
            )
        ]
        if not matches:
            return False
        if len(matches) != 1:
            raise PullDeployError(
                "deployment governance has ambiguous 0012 journals"
            )
        journal = matches[0]
        state_digest = sha256_bytes(
            canonical_json_bytes(dict(state)) + b"\n"
        )
        if journal.get("post_state_sha256") != state_digest:
            raise PullDeployError(
                "deployment governance differs from its 0012 journal"
            )
        pre_state_digest = require_digest(
            journal.get("pre_state_sha256"),
            "0012 governance pre-state digest",
        )
        pre_terminal = self._deployment_terminal_audit_binding(
            operation_id=deployment_operation,
            descriptor_sha256=descriptor_digest,
            state_sha256=pre_state_digest,
            source_sha=descriptor["repository"]["target_sha"],
            source_tree=descriptor["repository"]["target_tree"],
        )
        approval = journal.get("approval")
        mutable_pair = validate_mutable_data_pair(
            journal.get("contract_mutable_data_audit")
        )
        external_pair: dict[str, Any] | None = None
        if journal.get("contract_external_database_audit") is not None:
            external_pair = validate_external_database_contract_pair(
                journal["contract_external_database_audit"],
                before_binding=pre_terminal["state"].get(
                    "external_database_audit"
                ),
            )
        if not isinstance(approval, dict):
            raise PullDeployError(
                "0012 governance journal approval is invalid"
            )
        projected = self._project_contract_terminal_state(
            pre_terminal["state"],
            descriptor=descriptor,
            operation_id=contract_operation,
            approval=approval,
            mutable_pair=mutable_pair,
            external_pair=external_pair,
        )
        if projected != dict(state):
            raise PullDeployError(
                "0012 governance journal does not project the exact state"
            )
        return True

    def _validate_state_source_descriptor(
        self,
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Bind every candidate/durable state to its exact prepared source."""

        operation_id = require_operation_id(str(state.get("operation_id", "")))
        operation_root, descriptor_path, ready_path = self._operation_paths(
            operation_id
        )
        descriptor_digest = require_digest(
            state.get("descriptor_sha256"),
            "deployment state source descriptor digest",
        )
        if (
            descriptor_path.parent != operation_root
            or not descriptor_path.is_file()
            or descriptor_path.is_symlink()
            or not ready_path.is_file()
            or ready_path.is_symlink()
            or sha256_file(descriptor_path) != descriptor_digest
        ):
            raise PullDeployError(
                "deployment state source descriptor provenance is unavailable"
            )
        descriptor = validate_descriptor(load_private_json(descriptor_path))
        ready = load_private_json(ready_path)
        self._validate_ready(ready, descriptor, descriptor_path)
        repository = descriptor["repository"]
        release = descriptor["release_input"]
        monomer = descriptor["monomer_md"]
        unit = monomer["systemd_unit"]
        expected_unit = {
            "target_path": unit["target_path"],
            "sha256": unit["sha256"],
            "control_release_id": unit["control_release_id"],
            "launcher_sha256": unit["launcher_sha256"],
        }
        active = state["active_monomer_md_slot"]
        slot = monomer["slot_record"]
        expected_active_slot = {
            "slot": slot["slot"],
            "source_sha": slot["source_sha"],
            "source_tree": slot["source_tree"],
            "worker_lock_sha256": slot["worker_lock_sha256"],
            "slot_record_sha256": monomer["slot_record_sha256"],
            "operation_id": operation_id,
        }
        active_control = state["active_control"]
        executor = descriptor["controller"]["executor_control"]
        if (
            descriptor.get("operation_id") != operation_id
            or ready.get("descriptor_sha256") != descriptor_digest
            or state.get("source_sha") != repository["target_sha"]
            or state.get("source_tree") != repository["target_tree"]
            or state.get("previous_release") != repository["previous_sha"]
            or state.get("images") != descriptor["images"]
            or state.get("asset_manifest_digest")
            != release["asset_manifest_digest"]
            or state.get("asset_identity") != release["asset"]
            or state.get("byteff2_commit")
            != release["asset"]["byteff2_commit"]
            or state.get("production_config")
            != descriptor["production_config"]
            or state.get("control_helpers")
            != descriptor["controller"]["helpers"]
            or state.get("monomer_md_worker_env") != monomer["worker_env"]
            or state.get("monomer_md_systemd_unit") != expected_unit
            or any(
                active.get(field) != value
                for field, value in expected_active_slot.items()
            )
            or not self._active_matches_candidate(
                active_control,
                executor,
            )
        ):
            raise PullDeployError(
                "deployment state differs from its exact source descriptor"
            )

        previous_state = descriptor.get("previous_deployment")
        governance_fields = (
            "approved_contracts",
            "migration_epoch_barrier",
            "schema_compatibility_floor",
            "last_contract_operation",
            "contract_mutable_data_audit",
            "contract_external_database_audit",
        )
        if descriptor["schema_version"] == BRIDGE_DESCRIPTOR_SCHEMA_VERSION:
            if (
                state.get("external_database_audit") is None
                or state.get("external_database_transition_chain") is None
            ):
                raise PullDeployError(
                    "bridge deployment state lacks its external database authority"
                )
        elif isinstance(previous_state, dict):
            for field in (
                "external_database_audit",
                "external_database_transition_chain",
            ):
                inherited = previous_state.get(field)
                if state.get(field) != inherited:
                    raise PullDeployError(
                        "deployment state did not inherit its external database authority"
                    )
            changed_governance = [
                field
                for field in governance_fields
                if state.get(field) != previous_state.get(field)
            ]
            if changed_governance and not self._is_exact_direct_contract_state(
                state,
                descriptor=descriptor,
                descriptor_digest=descriptor_digest,
            ):
                raise PullDeployError(
                    "deployment state did not inherit governance field "
                    + changed_governance[0]
                )
            previous_final_mutable = previous_state.get(
                "final_mutable_data_audit"
            )
            previous_final_external = previous_state.get(
                "final_external_database_audit"
            )
            candidate_final_mutable = state.get(
                "final_mutable_data_audit"
            )
            candidate_final_external = state.get(
                "final_external_database_audit"
            )
            if (
                candidate_final_mutable != previous_final_mutable
                or candidate_final_external != previous_final_external
            ):
                if state.get("rollback_provenance") is not None:
                    # Explicit rollback is not an ordinary successor.  Its
                    # retained-0013 pairs are proven against the exact F
                    # terminal audit by _validate_rollback_state_provenance.
                    # Governance and contract fields above still inherit
                    # directly from the sealed B state.
                    pass
                else:
                    # Only the exact deployment that applies 0013 may
                    # introduce the two final transition pairs.  All later
                    # successors must inherit both byte-for-byte.
                    mutable_pair = (
                        validate_mutable_data_pair(candidate_final_mutable)
                        if candidate_final_mutable is not None
                        else None
                    )
                    external_pair = (
                        validate_external_database_final_pair(
                            candidate_final_external,
                            before_binding=external_database_endpoint(
                                validate_external_database_audit_binding(
                                    previous_state.get(
                                        "external_database_audit"
                                    )
                                ),
                                contract_pair=previous_state.get(
                                    "contract_external_database_audit"
                                ),
                            ),
                        )
                        if candidate_final_external is not None
                        else None
                    )
                    if (
                        previous_final_mutable is not None
                        or previous_final_external is not None
                        or mutable_pair is None
                        or external_pair is None
                        or mutable_pair["transition"]["kind"]
                        != "expand-0013"
                        or mutable_pair["transition"]["operation_id"]
                        != operation_id
                        or external_pair["operation_id"] != operation_id
                        or external_pair["descriptor_sha256"]
                        != descriptor_digest
                        or state.get("migrations")
                        != [
                            *previous_state.get("migrations", []),
                            _bridge_core.FINAL_MIGRATION_RECORD,
                        ]
                    ):
                        raise PullDeployError(
                            "deployment state changed final 0013 authority outside its exact transition"
                        )
        else:
            baseline_governance = {
                "approved_contracts": [],
                "migration_epoch_barrier": None,
                "schema_compatibility_floor": None,
                "last_contract_operation": None,
                "contract_mutable_data_audit": None,
                "contract_external_database_audit": None,
            }
            changed_governance = [
                field
                for field in governance_fields
                if state.get(field) != baseline_governance[field]
            ]
            if changed_governance and not self._is_exact_direct_contract_state(
                state,
                descriptor=descriptor,
                descriptor_digest=descriptor_digest,
            ):
                raise PullDeployError(
                    "bootstrap deployment state has unjournaled governance authority"
                )
        if descriptor["schema_version"] == BRIDGE_DESCRIPTOR_SCHEMA_VERSION:
            bridge_governance = (
                {
                    field: previous_state.get(field)
                    for field in governance_fields
                }
                if isinstance(previous_state, dict)
                else {
                    "approved_contracts": [],
                    "migration_epoch_barrier": None,
                    "schema_compatibility_floor": None,
                    "last_contract_operation": None,
                    "contract_mutable_data_audit": None,
                    "contract_external_database_audit": None,
                }
            )
            changed_governance = [
                field
                for field in governance_fields
                if state.get(field) != bridge_governance[field]
            ]
            if changed_governance and not self._is_exact_direct_contract_state(
                state,
                descriptor=descriptor,
                descriptor_digest=descriptor_digest,
            ):
                raise PullDeployError(
                    "bridge deployment state has unjournaled governance authority"
                )
        if (
            not isinstance(previous_state, dict)
            and state.get("rollback_provenance") is None
            and (
                state.get("final_mutable_data_audit") is not None
                or state.get("final_external_database_audit") is not None
            )
        ):
            raise PullDeployError(
                "bootstrap deployment state has unproven final 0013 authority"
            )

        descriptor_records = descriptor["migrations"].get("records")
        if not isinstance(descriptor_records, list) or not descriptor_records:
            raise PullDeployError(
                "deployment state source migration manifest is invalid"
            )
        compatibility = state.get("migration_compatibility")
        if descriptor["schema_version"] == BRIDGE_DESCRIPTOR_SCHEMA_VERSION:
            authority = descriptor["bridge"]["policy"]
        else:
            authority = (
                previous_state.get("migration_compatibility")
                if isinstance(previous_state, dict)
                else None
            )
        if (compatibility is None) != (authority is None):
            raise PullDeployError(
                "deployment compatibility presence differs from source authority"
            )
        if compatibility is None:
            allowed_ledgers = [descriptor_records]
            contract_indexes = [
                index
                for index, record in enumerate(descriptor_records)
                if isinstance(record, dict)
                and record.get("kind") == "contract"
            ]
            if (
                contract_indexes
                and contract_indexes[-1] == len(descriptor_records) - 1
            ):
                allowed_ledgers.append(
                    descriptor_records[: contract_indexes[-1]]
                )
            if state.get("migrations") not in allowed_ledgers:
                raise PullDeployError(
                    "deployment state migration ledger differs from its source manifest"
                )
        else:
            compatibility = validate_migration_compatibility_state(
                compatibility,
                migrations=state.get("migrations"),
            )
            if (
                compatibility is None
                or compatibility["code_manifest_sha256"]
                != descriptor["migrations"]["sha256"]
            ):
                raise PullDeployError(
                    "deployment compatibility code manifest differs from source"
                )
            expected_compatibility = build_migration_compatibility_state(
                authority,
                code_manifest_sha256=descriptor["migrations"]["sha256"],
                migrations=state.get("migrations"),
            )
            if compatibility != expected_compatibility:
                raise PullDeployError(
                    "deployment compatibility registry differs from source authority"
                )
        return descriptor

    def _validate_rollback_state_provenance(
        self,
        state: Mapping[str, Any],
    ) -> None:
        """Open the exact F success audit and sealed previous state for rollback."""

        raw = state.get("rollback_provenance")
        if raw is None:
            return
        provenance = dict(raw)
        operation_id = provenance["from_operation_id"]
        operation_root, descriptor_path, ready_path = self._operation_paths(
            operation_id
        )
        if (
            descriptor_path.parent != operation_root
            or not descriptor_path.is_file()
            or descriptor_path.is_symlink()
            or not ready_path.is_file()
            or ready_path.is_symlink()
            or sha256_file(descriptor_path)
            != provenance["from_descriptor_sha256"]
        ):
            raise PullDeployError(
                "rollback source descriptor provenance is unavailable"
            )
        source_descriptor = validate_descriptor(
            load_private_json(descriptor_path)
        )
        ready = load_private_json(ready_path)
        self._validate_ready(ready, source_descriptor, descriptor_path)
        sealed_previous = source_descriptor.get("previous_deployment")
        if not isinstance(sealed_previous, dict):
            raise PullDeployError(
                "rollback source descriptor lacks a governed previous state"
            )
        if (
            source_descriptor.get("operation_id") != operation_id
            or source_descriptor["repository"]["target_sha"]
            != provenance["from_source_sha"]
            or source_descriptor["repository"]["target_tree"]
            != provenance["from_source_tree"]
            or ready.get("descriptor_sha256")
            != provenance["from_descriptor_sha256"]
            or source_descriptor.get("previous_deployment_sha256")
            != provenance["sealed_previous_state_sha256"]
            or sealed_previous.get("operation_id")
            != provenance["to_operation_id"]
            or sealed_previous.get("source_sha")
            != provenance["to_source_sha"]
            or sealed_previous.get("source_tree")
            != provenance["to_source_tree"]
            or sealed_previous.get("descriptor_sha256")
            != provenance["to_descriptor_sha256"]
        ):
            raise PullDeployError(
                "rollback source descriptor authority differs"
            )
        allowed_differences = {
            "migrations",
            "migration_compatibility",
            "database_backup",
            "mutable_data_audit",
            "final_mutable_data_audit",
            "final_external_database_audit",
            "deployed_at",
            "rollback_provenance",
        }
        projected_state = {
            key: value
            for key, value in state.items()
            if key not in allowed_differences
        }
        projected_previous = {
            key: value
            for key, value in sealed_previous.items()
            if key not in allowed_differences
        }
        if projected_state != projected_previous:
            raise PullDeployError(
                "rollback state changes fields outside the sealed rollback projection"
            )
        source_terminal = self._deployment_terminal_audit_binding(
            operation_id=operation_id,
            descriptor_sha256=provenance["from_descriptor_sha256"],
            state_sha256=provenance["from_state_sha256"],
            source_sha=provenance["from_source_sha"],
            source_tree=provenance["from_source_tree"],
            expected_terminal_sha256=provenance[
                "from_terminal_audit_sha256"
            ],
        )
        source_state = source_terminal["state"]
        expected_semantics = self._explicit_rollback_state(
            source_state,
            sealed_previous,
        )
        for field in (
            "migrations",
            "migration_compatibility",
            "final_mutable_data_audit",
            "final_external_database_audit",
        ):
            if state.get(field) != expected_semantics.get(field):
                raise PullDeployError(
                    f"rollback state {field} differs from the sealed F-to-B projection"
                )
        source_final_mutable_digest = (
            canonical_json_digest(
                source_state["final_mutable_data_audit"]
            )
            if source_state.get("final_mutable_data_audit") is not None
            else None
        )
        source_final_external_digest = (
            canonical_json_digest(
                source_state["final_external_database_audit"]
            )
            if source_state.get("final_external_database_audit") is not None
            else None
        )
        if (
            source_final_mutable_digest
            != provenance["final_mutable_data_audit_sha256"]
            or source_final_external_digest
            != provenance["final_external_database_audit_sha256"]
        ):
            raise PullDeployError(
                "rollback source terminal audit lacks inherited final evidence"
            )

    def _validate_external_database_state_provenance(
        self,
        state: object,
    ) -> dict[str, Any]:
        """Open and verify every content-addressed external DB chain edge."""

        validated = validate_current_deployment_state(state)
        self._validate_state_source_descriptor(validated)
        raw_active = validated.get("external_database_audit")
        if raw_active is None:
            self._validate_rollback_state_provenance(validated)
            return validated
        active = validate_external_database_audit_binding(raw_active)
        chain = validate_external_database_transition_chain(
            validated.get("external_database_transition_chain"),
            active_binding=active,
        )
        bridge_reference = validate_external_database_transition_reference(
            chain["bridge"],
            expected_kind="bridge-expand-to-0011",
        )
        bridge_operation = bridge_reference["operation_id"]
        operation_root, descriptor_path, ready_path = (
            self._operation_paths(bridge_operation)
        )
        if (
            descriptor_path.parent != operation_root
            or not descriptor_path.is_file()
            or descriptor_path.is_symlink()
            or not ready_path.is_file()
            or ready_path.is_symlink()
            or sha256_file(descriptor_path)
            != bridge_reference["descriptor_sha256"]
        ):
            raise PullDeployError(
                "external database bridge descriptor provenance is unavailable"
            )
        bridge_descriptor = validate_descriptor(
            load_private_json(descriptor_path)
        )
        ready = load_private_json(ready_path)
        self._validate_ready(
            ready,
            bridge_descriptor,
            descriptor_path,
        )
        if (
            bridge_descriptor.get("schema_version")
            != BRIDGE_DESCRIPTOR_SCHEMA_VERSION
            or bridge_descriptor.get("operation_id")
            != bridge_operation
            or ready.get("descriptor_sha256")
            != bridge_reference["descriptor_sha256"]
        ):
            raise PullDeployError(
                "external database bridge descriptor identity differs"
            )
        try:
            token = _bridge_core.load_token_authority(self.state_dir)
            alias_marker = (
                _control_runtime.load_production_0005_alias_gate(
                    self.runtime_root,
                    require_completed=True,
                )
            )
            alias_identity = alias_marker.get("identity")
            alias_authority = (
                alias_identity.get("bridge_authority")
                if isinstance(alias_identity, dict)
                else None
            )
            validate_alias_bridge_authority(
                alias_authority,
                descriptor=bridge_descriptor,
                descriptor_path=descriptor_path,
                ready_path=ready_path,
                state_root=self.state_dir,
                current_token=token,
            )
        except Exception as exc:
            raise PullDeployError(
                "external database bridge token provenance differs"
            ) from exc
        bridge_pair, _alias_pair, alias_reference = (
            self._load_bridge_external_database_transition(
                bridge_descriptor,
                bridge_reference,
            )
        )
        if (
            chain["alias"] != alias_reference
            or bridge_pair["after_binding"] != active
        ):
            raise PullDeployError(
                "external database transition chain content differs"
            )
        raw_contract = validated.get(
            "contract_external_database_audit"
        )
        raw_final = validated.get("final_external_database_audit")
        external_database_endpoint(
            active,
            contract_pair=raw_contract,
            final_pair=raw_final,
        )
        self._load_final_external_database_transition(validated)
        self._validate_rollback_state_provenance(validated)
        return validated

    def mutable_data_contract(self) -> dict[str, Any]:
        path = self.config_dir / MUTABLE_DATA_AUDIT_HELPER
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise PullDeployError(
                "mutable-data audit helper is unavailable"
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise PullDeployError("mutable-data audit helper is unsafe")
        dependencies: dict[str, dict[str, str]] = {}
        dependency_payloads: dict[str, bytes] = {}
        for key, filename in (
            ("pg_service", MUTABLE_DATA_SERVICE_CONFIG),
            ("pgpass", MUTABLE_DATA_PGPASS),
        ):
            dependency = self.config_dir / filename
            try:
                dependency_metadata = dependency.lstat()
            except OSError as exc:
                raise PullDeployError(
                    f"mutable-data helper dependency is unavailable: {filename}"
                ) from exc
            if (
                not stat.S_ISREG(dependency_metadata.st_mode)
                or dependency.is_symlink()
                or dependency_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(dependency_metadata.st_mode) != 0o600
            ):
                raise PullDeployError(
                    f"mutable-data helper dependency is unsafe: {filename}"
                )
            payload = dependency.read_bytes()
            if not payload or len(payload) > 64 * 1024:
                raise PullDeployError(
                    f"mutable-data helper dependency is malformed: {filename}"
                )
            dependency_payloads[key] = payload
            dependencies[key] = {
                "path": str(dependency),
                "sha256": sha256_file(dependency),
                "mode": "0600",
            }
        connection = validate_mutable_data_connection_inputs(
            dependency_payloads["pg_service"],
            dependency_payloads["pgpass"],
            expected_passfile=self.config_dir / MUTABLE_DATA_PGPASS,
        )
        return validate_mutable_data_contract(
            {
                "schema_version": 6,
                "helper_path": str(path),
                "helper_sha256": sha256_file(path),
                "dependencies": dependencies,
                "connection": connection,
                "business_tables": list(MUTABLE_DATA_BUSINESS_TABLES),
                "governed_controls": list(
                    MUTABLE_DATA_GOVERNED_CONTROLS
                ),
                "static_tables": list(MUTABLE_DATA_STATIC_TABLES),
                "migration_exception": MUTABLE_DATA_EXCEPTION,
                "migration_exception_archive_evidence": (
                    "generation.polytao_jobs:canonical-archive-v2"
                ),
                "sequences": list(MUTABLE_DATA_SEQUENCES),
                "bridge_projection": (
                    "md.monomer_md_jobs:pre-0009-row-json-v1"
                ),
                "evidence_schema_version": 6,
            }
        )

    def _capture_mutable_data(
        self,
        descriptor: dict[str, Any],
    ) -> dict[str, Any]:
        if (
            self.production_config_evidence(check_free_space=False)
            != descriptor["production_config"]
        ):
            raise PullDeployError(
                "mutable-data helper configuration changed after prepare"
            )
        contract = self.mutable_data_contract()
        if contract != descriptor["mutable_data"]:
            raise PullDeployError("mutable-data helper contract changed after prepare")
        marker = load_private_json(self.marker_path)
        sealed = validate_postgres_runtime_fence(
            marker.get("postgres_runtime_fence")
        )
        capture = getattr(self.lifecycle, "postgres_runtime_identity", None)
        if not callable(capture):
            raise PullDeployError(
                "mutable-data audit cannot revalidate PostgreSQL runtime"
            )
        before = validate_postgres_runtime_fence(capture(self, descriptor))
        if postgres_runtime_fence_identity(before) != postgres_runtime_fence_identity(
            sealed
        ):
            raise PullDeployError(
                "mutable-data audit PostgreSQL runtime differs from stop fence"
            )
        runtime_identity = postgres_runtime_fence_identity(before)
        if runtime_identity["host_endpoint"] != {
            "host": contract["connection"]["host"],
            "port": contract["connection"]["port"],
            "container_port": 5432,
            "protocol": "tcp",
        }:
            raise PullDeployError(
                "mutable-data audit endpoint differs from PostgreSQL container"
            )
        environment = self.control_environment()
        environment["NEXPOLY_MUTABLE_AUDIT_RUNTIME_JSON"] = json.dumps(
            runtime_identity,
            sort_keys=True,
            separators=(",", ":"),
        )
        environment["NEXPOLY_MUTABLE_AUDIT_OPERATION_ID"] = descriptor[
            "operation_id"
        ]
        result = self.runner.run(
            [contract["helper_path"]],
            cwd=self.production_root,
            env=environment,
            text=True,
        )
        evidence = validate_mutable_data_evidence(
            parse_command_json(result.stdout, "mutable-data audit")
        )
        after = validate_postgres_runtime_fence(capture(self, descriptor))
        if (
            postgres_runtime_fence_identity(after) != runtime_identity
            or evidence["postgres_runtime"] != runtime_identity
            or evidence["connection"] != contract["connection"]
        ):
            raise PullDeployError(
                "mutable-data audit did not bind the exact PostgreSQL container"
            )
        return evidence

    def _bind_mutable_data_before(
        self,
        marker: dict[str, Any],
        descriptor: dict[str, Any],
    ) -> dict[str, Any]:
        existing = marker.get("mutable_data_before")
        before = (
            validate_mutable_data_evidence(existing)
            if existing is not None
            else self._capture_mutable_data(descriptor)
        )
        postgres = marker.get("postgres_runtime_fence")
        if (
            isinstance(postgres, dict)
            and before["database_system_identifier"]
            != postgres.get("system_identifier")
        ):
            raise PullDeployError(
                "mutable-data audit selected another PostgreSQL cluster"
            )
        if existing is None:
            self._advance(
                marker,
                str(marker["phase"]),
                mutable_data_before=before,
            )
        return before

    def _bind_mutable_data_after(
        self,
        marker: dict[str, Any],
        descriptor: dict[str, Any],
    ) -> dict[str, Any]:
        before = self._bind_mutable_data_before(marker, descriptor)
        after = self._capture_mutable_data(descriptor)
        pair = self._build_mutable_data_pair_for_descriptor(
            descriptor,
            before,
            after,
        )
        postgres = marker.get("postgres_runtime_fence")
        if (
            isinstance(postgres, dict)
            and after["database_system_identifier"]
            != postgres.get("system_identifier")
        ):
            raise PullDeployError(
                "post-deploy mutable-data audit selected another PostgreSQL cluster"
            )
        self._advance(
            marker,
            str(marker["phase"]),
            mutable_data_after=after,
        )
        return pair

    def _build_mutable_data_pair_for_descriptor(
        self,
        descriptor: dict[str, Any],
        before: object,
        after: object,
        *,
        descriptor_digest: str | None = None,
    ) -> dict[str, Any]:
        before_evidence = validate_mutable_data_evidence(before)
        after_evidence = validate_mutable_data_evidence(after)
        is_bridge_expansion = (
            descriptor.get("schema_version")
            == BRIDGE_DESCRIPTOR_SCHEMA_VERSION
            and len(before_evidence["migration_ledger"]) == 8
            and len(after_evidence["migration_ledger"]) == 11
        )
        if not is_bridge_expansion:
            return build_mutable_data_pair(
                before_evidence,
                after_evidence,
            )
        if descriptor_digest is None:
            descriptor_path = (
                self.prepared_root
                / descriptor["operation_id"]
                / "descriptor.json"
            )
            descriptor_digest = sha256_file(descriptor_path)
        return build_bridge_mutable_data_pair(
            before_evidence,
            after_evidence,
            descriptor_sha256=descriptor_digest,
        )

    def _git(
        self, *arguments: str, check: bool = True
    ) -> subprocess.CompletedProcess[Any]:
        preflight = self._git_trust_preflight()
        command = (
            _git_source_trust.safe_git_command(
                self.production_root,
                *arguments,
            )
            if preflight is not None
            else [
                "git",
                "-c",
                "credential.helper=",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-c",
                f"core.worktree={self.production_root}",
                *arguments,
            ]
        )
        return self.runner.run(
            command,
            cwd=self.production_root,
            env=self._clean_environment(),
            check=check,
            umask=0o077,
        )

    def repository_identity(
        self, *, require_ssh_origin: bool = False
    ) -> dict[str, Any]:
        preflight = self._git_trust_preflight()
        branch = str(self._git("symbolic-ref", "--short", "HEAD").stdout).strip()
        if branch != "main":
            raise PullDeployError("production checkout must be on local main")
        status = str(
            self._git("status", "--porcelain=v1", "--untracked-files=all").stdout
        )
        if status:
            raise PullDeployError(
                "production checkout contains tracked or non-ignored untracked changes"
            )
        tracked = self._git("ls-files", "-z", "--cached").stdout
        if not isinstance(tracked, str):
            raise PullDeployError("cannot enumerate tracked production files")
        tracked_paths = {value for value in tracked.split("\0") if value}
        for value in tracked_paths:
            if not value:
                continue
            relative = Path(value)
            if relative.is_absolute() or ".." in relative.parts:
                raise PullDeployError("tracked production path escapes the checkout")
            path = self.production_root / relative
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise PullDeployError(
                    f"tracked production file is missing: {relative}"
                ) from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or path.is_symlink()
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o022
            ):
                raise PullDeployError(f"tracked production file is unsafe: {relative}")
            current = path.parent
            while current != self.production_root:
                parent_metadata = current.lstat()
                if (
                    not stat.S_ISDIR(parent_metadata.st_mode)
                    or current.is_symlink()
                    or parent_metadata.st_uid != os.geteuid()
                    or parent_metadata.st_mode & 0o022
                ):
                    raise PullDeployError(
                        f"tracked production parent is unsafe: {current}"
                    )
                current = current.parent
        for relative in FORBIDDEN_IN_TREE_RUNTIME_PATHS:
            path = self.production_root / relative
            if not path.exists() and not path.is_symlink():
                continue
            tracked_under = {
                value
                for value in tracked_paths
                if value == relative or value.startswith(relative.rstrip("/") + "/")
            }
            unsafe = not tracked_under
            if path.is_dir() and not path.is_symlink() and not unsafe:
                for directory, names, files in os.walk(path, followlinks=False):
                    current = Path(directory)
                    for name in (*names, *files):
                        child = current / name
                        child_relative = child.relative_to(
                            self.production_root
                        ).as_posix()
                        if child.is_symlink() or (
                            child.is_file() and child_relative not in tracked_paths
                        ):
                            unsafe = True
                            break
                    if unsafe:
                        break
            elif path.is_file() and relative in tracked_paths:
                unsafe = False
            if unsafe:
                raise PullDeployError(
                    f"runtime state must be moved outside the Git checkout before deployment: {relative}"
                )
        origin = str(self._git("remote", "get-url", "origin").stdout).strip()
        if origin not in {REPOSITORY_SSH_URL, REPOSITORY_HTTPS_URL}:
            raise PullDeployError("production origin is not the canonical repository")
        if require_ssh_origin and origin != REPOSITORY_SSH_URL:
            raise PullDeployError("production apply requires the deploy-key SSH origin")
        sha = require_sha(
            str(self._git("rev-parse", "HEAD").stdout).strip(), "current source SHA"
        )
        tree = require_sha(
            str(self._git("rev-parse", "HEAD^{tree}").stdout).strip(),
            "current source tree",
        )
        identity: dict[str, Any] = {
            "sha": sha,
            "tree": tree,
            "origin": origin,
        }
        if preflight is not None:
            environment = self._clean_environment()
            try:
                trust = _git_source_trust.repository_trust_evidence(
                    self.production_root,
                    source_sha=sha,
                    source_tree=tree,
                    branch="refs/heads/main",
                    origin=origin,
                    ambient=os.environ,
                    home=str(DEPLOY_USER_HOME),
                    ssh_command=environment["GIT_SSH_COMMAND"],
                )
                _git_source_trust.require_stable_trust_surface(
                    preflight,
                    trust,
                )
            except Exception as exc:
                raise PullDeployError(
                    "production Git trust evidence changed"
                ) from exc
            identity["trust"] = trust
            permission_takeover = self._git_permission_takeover()
            if permission_takeover is not None:
                identity["permission_takeover"] = permission_takeover
        return identity

    def ignored_runtime_entries(self) -> list[str]:
        result = self._git(
            "ls-files",
            "-z",
            "--others",
            "--ignored",
            "--exclude-standard",
        ).stdout
        if not isinstance(result, str):
            raise PullDeployError("cannot enumerate ignored production runtime entries")
        entries = sorted({value for value in result.split("\0") if value})
        if len(entries) > 10000 or any(
            Path(value).is_absolute() or ".." in Path(value).parts for value in entries
        ):
            raise PullDeployError("ignored production runtime inventory is unsafe")
        return entries

    def _assert_no_ignored_runtime(self) -> None:
        entries = self.ignored_runtime_entries()
        if entries:
            preview = ", ".join(entries[:10])
            raise PullDeployError(
                "ignored runtime/cache/secrets remain inside the production checkout: "
                + preview
            )

    def remote_main(self) -> str:
        result = self._git(
            "ls-remote", "--exit-code", REPOSITORY_SSH_URL, "refs/heads/main"
        )
        lines = [
            line.split() for line in str(result.stdout).splitlines() if line.strip()
        ]
        if len(lines) != 1 or len(lines[0]) != 2 or lines[0][1] != "refs/heads/main":
            raise PullDeployError("remote main probe returned malformed evidence")
        return require_sha(lines[0][0], "remote main SHA")

    def fetch_target(self, target_sha: str, operation_id: str) -> str:
        self._require_deploy_lock_for_staging()
        self._git(
            "fetch",
            "--no-tags",
            "--prune",
            REPOSITORY_SSH_URL,
            "+refs/heads/main:refs/remotes/nexpoly-deploy/main",
        )
        fetched = require_sha(
            str(
                self._git("rev-parse", "refs/remotes/nexpoly-deploy/main").stdout
            ).strip(),
            "fetched main SHA",
        )
        if fetched != target_sha or self.remote_main() != target_sha:
            raise PullDeployError("target SHA is no longer current remote main")
        object_type = str(self._git("cat-file", "-t", target_sha).stdout).strip()
        if object_type != "commit":
            raise PullDeployError("target Git object is not a commit")
        target_tree = require_sha(
            str(self._git("rev-parse", f"{target_sha}^{{tree}}").stdout).strip(),
            "target source tree",
        )
        ancestor = self._git(
            "merge-base", "--is-ancestor", "HEAD", target_sha, check=False
        )
        if ancestor.returncode != 0:
            raise PullDeployError(
                "target main is not a fast-forward of production HEAD"
            )
        self._git(
            "update-ref",
            f"refs/nexpoly/prepared/{operation_id}",
            target_sha,
        )
        return target_tree

    def bridge_policy_relation(
        self,
        authority_sha: str,
        *,
        create_target_ref: bool,
        fetch_authority: bool,
    ) -> dict[str, Any]:
        """Re-fetch F and derive the only deployable historical B from policy.

        No caller supplies the target.  The exact SHA, tree, private ref,
        images, asset and migration compatibility all come from the policy
        stored in the current protected remote main object.
        """

        authority_sha = require_sha(authority_sha, "bridge authority SHA")
        if fetch_authority or create_target_ref:
            self._require_deploy_lock_for_staging()
        if self.remote_main() != authority_sha:
            raise PullDeployError("bridge authority is no longer current remote main")
        if fetch_authority:
            self._git(
                "fetch",
                "--no-tags",
                "--prune",
                REPOSITORY_SSH_URL,
                "+refs/heads/main:refs/remotes/nexpoly-deploy/main",
            )
        fetched = require_sha(
            str(
                self._git("rev-parse", "refs/remotes/nexpoly-deploy/main").stdout
            ).strip(),
            "fetched bridge authority SHA",
        )
        if fetched != authority_sha:
            raise PullDeployError("fetched bridge authority differs from remote main")
        authority_tree = require_sha(
            str(self._git("rev-parse", f"{authority_sha}^{{tree}}").stdout).strip(),
            "bridge authority tree",
        )
        try:
            policy = _bridge_core.parse_policy(
                self._git_show(authority_sha, _bridge_core.POLICY_RELATIVE_PATH)
            )
        except Exception as exc:
            raise PullDeployError("bridge authority policy is invalid") from exc
        self._verify_bridge_media_authority_rules(authority_sha, policy)
        target_sha = policy["target_sha"]
        if str(self._git("cat-file", "-t", target_sha).stdout).strip() != "commit":
            raise PullDeployError("bridge target object is not a commit")
        target_tree = require_sha(
            str(self._git("rev-parse", f"{target_sha}^{{tree}}").stdout).strip(),
            "bridge target tree",
        )
        target_is_ancestor = (
            self._git(
                "merge-base",
                "--is-ancestor",
                target_sha,
                authority_sha,
                check=False,
            ).returncode
            == 0
        )
        production_can_advance = (
            self._git(
                "merge-base",
                "--is-ancestor",
                "HEAD",
                target_sha,
                check=False,
            ).returncode
            == 0
        )
        if not production_can_advance:
            raise PullDeployError(
                "bridge target is not a fast-forward of the production source"
            )
        target_ref = policy["target_ref"]
        existing = self._git(
            "show-ref",
            "--verify",
            "--hash",
            target_ref,
            check=False,
        )
        if existing.returncode not in {0, 1}:
            raise PullDeployError("cannot inspect the exact bridge target ref")
        if existing.returncode == 0:
            if require_sha(
                str(existing.stdout).strip(), "existing bridge target ref"
            ) != target_sha:
                raise PullDeployError("exact bridge target ref was repointed")
        elif create_target_ref:
            self._git(
                "update-ref",
                target_ref,
                target_sha,
                "0" * 40,
            )
            if require_sha(
                str(
                    self._git("show-ref", "--verify", "--hash", target_ref).stdout
                ).strip(),
                "created bridge target ref",
            ) != target_sha:
                raise PullDeployError("exact bridge target ref did not publish")
        if self.remote_main() != authority_sha:
            raise PullDeployError("bridge authority changed during verification")
        try:
            target_manifest_payload = self._git_show(
                target_sha, "backend/migrations/postgres/manifest.json"
            )
            authority_manifest_payload = self._git_show(
                authority_sha, "backend/migrations/postgres/manifest.json"
            )
            target_manifest = json.loads(target_manifest_payload)
            authority_manifest = json.loads(authority_manifest_payload)
            if (
                not isinstance(target_manifest, dict)
                or set(target_manifest) != {"schema_version", "migrations"}
                or target_manifest.get("schema_version") != 2
                or not isinstance(authority_manifest, dict)
                or set(authority_manifest) != {"schema_version", "migrations"}
                or authority_manifest.get("schema_version") != 2
            ):
                raise PullDeployError(
                    "bridge migration manifests are not canonical schema V2"
                )
            migration_registry = _bridge_core.validate_migration_registry(
                policy,
                target_manifest_sha256=sha256_bytes(target_manifest_payload),
                target_records=target_manifest["migrations"],
                authority_manifest_sha256=sha256_bytes(authority_manifest_payload),
                authority_records=authority_manifest["migrations"],
            )
            relation = _bridge_core.validate_relation(
                policy,
                authority_sha=authority_sha,
                authority_tree=authority_tree,
                remote_main=authority_sha,
                target_sha=target_sha,
                target_tree=target_tree,
                target_ref=target_ref,
                is_ancestor=target_is_ancestor,
            )
        except Exception as exc:
            raise PullDeployError(
                "bridge authority/target relation differs from policy"
            ) from exc
        return {
            "policy": policy,
            "policy_sha256": _bridge_core.canonical_json_digest(policy),
            "relation": relation,
            "migration_registry": migration_registry,
        }

    def materialize_prefetched_bridge_relation(
        self,
        ready: dict[str, Any],
        *,
        create_target_ref: bool,
    ) -> dict[str, Any]:
        """Import and validate exact F/B objects from the sealed local bundle."""

        self._require_deploy_lock_for_staging()
        try:
            ready = _prefetch_evidence.validate_ready_evidence(
                ready,
                runtime_root=self.runtime_root,
            )
        except Exception as exc:
            raise PullDeployError(
                "maintenance prefetch evidence cannot materialize F/B"
            ) from exc
        operation_id = ready["operation_id"]
        authority = ready["source"]["authority"]
        target = ready["source"]["target"]
        bundle = Path(ready["git_bundle"]["path"])
        authority_ref = f"refs/nexpoly/prefetch/{operation_id}/authority"
        self._git(
            "fetch",
            "--no-tags",
            str(bundle),
            f"+refs/heads/main:{authority_ref}",
        )
        imported = require_sha(
            str(self._git("rev-parse", authority_ref).stdout).strip(),
            "prefetched authority ref",
        )
        if imported != authority["sha"]:
            raise PullDeployError(
                "prefetched bundle materialized another F authority"
            )
        authority_tree = require_sha(
            str(
                self._git(
                    "rev-parse", f"{authority['sha']}^{{tree}}"
                ).stdout
            ).strip(),
            "prefetched authority tree",
        )
        target_tree = require_sha(
            str(
                self._git("rev-parse", f"{target['sha']}^{{tree}}").stdout
            ).strip(),
            "prefetched bridge target tree",
        )
        if (
            authority_tree != authority["tree"]
            or target_tree != target["tree"]
            or str(
                self._git("cat-file", "-t", target["sha"]).stdout
            ).strip()
            != "commit"
            or self._git(
                "merge-base",
                "--is-ancestor",
                target["sha"],
                authority["sha"],
                check=False,
            ).returncode
            != 0
            or self._git(
                "merge-base",
                "--is-ancestor",
                "HEAD",
                target["sha"],
                check=False,
            ).returncode
            != 0
        ):
            raise PullDeployError(
                "prefetched F/B Git ancestry or tree differs"
            )
        try:
            policy = _bridge_core.parse_policy(
                self._git_show(
                    authority["sha"],
                    _bridge_core.POLICY_RELATIVE_PATH,
                )
            )
        except Exception as exc:
            raise PullDeployError(
                "prefetched F policy is invalid"
            ) from exc
        self._verify_bridge_media_authority_rules(
            authority["sha"],
            policy,
        )
        if (
            policy != ready["policy"]
            or canonical_json_digest(policy) != ready["policy_sha256"]
        ):
            raise PullDeployError(
                "prefetched F policy differs from Git authority"
            )
        target_ref = policy["target_ref"]
        existing = self._git(
            "show-ref",
            "--verify",
            "--hash",
            target_ref,
            check=False,
        )
        if existing.returncode not in {0, 1}:
            raise PullDeployError(
                "cannot inspect exact prefetched bridge target ref"
            )
        if existing.returncode == 0:
            if require_sha(
                str(existing.stdout).strip(),
                "prefetched bridge target ref",
            ) != target["sha"]:
                raise PullDeployError(
                    "exact prefetched bridge target ref was repointed"
                )
        elif create_target_ref:
            self._git(
                "update-ref",
                target_ref,
                target["sha"],
                "0" * 40,
            )
        try:
            target_manifest_payload = self._git_show(
                target["sha"],
                "backend/migrations/postgres/manifest.json",
            )
            authority_manifest_payload = self._git_show(
                authority["sha"],
                "backend/migrations/postgres/manifest.json",
            )
            target_manifest = json.loads(target_manifest_payload)
            authority_manifest = json.loads(authority_manifest_payload)
            if (
                not isinstance(target_manifest, dict)
                or set(target_manifest)
                != {"schema_version", "migrations"}
                or target_manifest.get("schema_version") != 2
                or not isinstance(authority_manifest, dict)
                or set(authority_manifest)
                != {"schema_version", "migrations"}
                or authority_manifest.get("schema_version") != 2
            ):
                raise PullDeployError(
                    "prefetched migration manifests are not schema V2"
                )
            migration_registry = _bridge_core.validate_migration_registry(
                policy,
                target_manifest_sha256=sha256_bytes(
                    target_manifest_payload
                ),
                target_records=target_manifest["migrations"],
                authority_manifest_sha256=sha256_bytes(
                    authority_manifest_payload
                ),
                authority_records=authority_manifest["migrations"],
            )
            relation = _bridge_core.validate_relation(
                policy,
                authority_sha=authority["sha"],
                authority_tree=authority["tree"],
                remote_main=authority["sha"],
                target_sha=target["sha"],
                target_tree=target["tree"],
                target_ref=target_ref,
                is_ancestor=True,
            )
        except Exception as exc:
            raise PullDeployError(
                "prefetched F/B policy relation differs"
            ) from exc
        return {
            "policy": policy,
            "policy_sha256": canonical_json_digest(policy),
            "relation": relation,
            "migration_registry": migration_registry,
        }

    def _git_show(self, target_sha: str, relative: str) -> bytes:
        result = self._git("show", f"{target_sha}:{relative}")
        return str(result.stdout).encode("utf-8")

    def _verify_bridge_media_authority_rules(
        self,
        authority_sha: str,
        policy: Mapping[str, Any],
    ) -> str:
        """Bind media rules and role provisioning SQL to exact F."""

        authority_sha = require_sha(
            authority_sha,
            "bridge media authority rules source",
        )
        external_policy = policy.get("external_database_audit")
        if not isinstance(external_policy, dict):
            raise PullDeployError(
                "bridge media authority rules policy is unavailable"
            )
        expected = require_digest(
            external_policy.get("media_authority_rules_sha256"),
            "bridge media authority rules",
        )
        try:
            payload = self._git_show(
                authority_sha,
                _bridge_core.MEDIA_AUTHORITY_RULES_RELATIVE_PATH,
            )
        except Exception as exc:
            raise PullDeployError(
                "bridge media authority rules are unavailable from exact F"
            ) from exc
        if not payload or len(payload) > 4 * 1024 * 1024:
            raise PullDeployError(
                "bridge media authority rules payload is invalid"
            )
        observed = sha256_bytes(payload)
        if observed != expected:
            raise PullDeployError(
                "bridge media authority rules differ from F policy"
            )
        expected_role_sql = require_digest(
            external_policy.get("audit_role_sql_sha256"),
            "bridge media audit-role SQL",
        )
        try:
            role_sql = self._git_show(
                authority_sha,
                _bridge_core.MEDIA_AUDIT_ROLE_SQL_RELATIVE_PATH,
            )
        except Exception as exc:
            raise PullDeployError(
                "bridge media audit-role SQL is unavailable from exact F"
            ) from exc
        if (
            not role_sql
            or len(role_sql) > 4 * 1024 * 1024
            or sha256_bytes(role_sql) != expected_role_sql
        ):
            raise PullDeployError(
                "bridge media audit-role SQL differs from F policy"
            )
        return observed

    def _github_token(self) -> str:
        path = self.config_dir / "github-api-token"
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise PullDeployError("GitHub API token must be an owner-only regular file")
        token = path.read_text(encoding="utf-8").strip()
        if not token or any(character.isspace() for character in token):
            raise PullDeployError("GitHub API token is empty or malformed")
        return token

    def ci_evidence(
        self,
        target_sha: str,
        *,
        required_jobs: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        token = self._github_token()
        runs = self.runner.request_json(
            f"{REPOSITORY_API_ROOT}/actions/runs?branch=main&head_sha={target_sha}&event=push&per_page=20",
            token,
        )
        values = runs.get("workflow_runs")
        if not isinstance(values, list):
            raise PullDeployError("GitHub workflow evidence has no run list")
        candidates = [
            run
            for run in values
            if isinstance(run, dict)
            and run.get("head_sha") == target_sha
            and run.get("head_branch") == "main"
            and run.get("event") == "push"
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
            and run.get("path") == ".github/workflows/ci.yml"
            and isinstance(run.get("id"), int)
            and not isinstance(run.get("id"), bool)
        ]
        if not candidates:
            raise PullDeployError(
                "target main has no successful completed CI workflow run"
            )
        run = max(
            candidates, key=lambda value: (value.get("run_attempt", 0), value["id"])
        )
        jobs = self.runner.request_json(
            f"{REPOSITORY_API_ROOT}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100",
            token,
        ).get("jobs")
        if not isinstance(jobs, list):
            raise PullDeployError("GitHub workflow evidence has no job list")
        successful = {
            job.get("name")
            for job in jobs
            if isinstance(job, dict) and job.get("conclusion") == "success"
        }
        required = (
            set(_bridge_core.REQUIRED_CI_JOBS)
            if required_jobs is None
            else set(required_jobs)
        )
        if (
            not required
            or len(required) > 32
            or any(not isinstance(name, str) or not name for name in required)
        ):
            raise PullDeployError("required CI job policy is invalid")
        if not required.issubset(successful):
            raise PullDeployError(
                "target CI lacks a successful gate or immutable image publication job"
            )
        return {
            "workflow_run_id": run["id"],
            "run_attempt": run.get("run_attempt", 1),
            "head_sha": target_sha,
            "head_branch": "main",
            "event": "push",
            "path": ".github/workflows/ci.yml",
            "conclusion": "success",
            "required_jobs": sorted(required),
        }

    def image_evidence(self, role: str, target_sha: str) -> dict[str, str]:
        root = BACKEND_TAG_ROOT if role == "backend" else WEB_TAG_ROOT
        tag = f"{root}:sha-{target_sha}"
        self.runner.run(["docker", "pull", tag], env=self.control_environment())
        result = self.runner.run(
            ["docker", "image", "inspect", tag], env=self.control_environment()
        )
        try:
            values = json.loads(str(result.stdout))
            image = values[0]
            labels = image["Config"]["Labels"]
            repo_digests = image["RepoDigests"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise PullDeployError(
                f"{role} image inspection evidence is malformed"
            ) from exc
        matches = [
            value
            for value in repo_digests
            if isinstance(value, str)
            and value.startswith(root + "@")
            and DIGEST_RE.fullmatch(value.split("@", 1)[1])
        ]
        if len(set(matches)) != 1:
            raise PullDeployError(
                f"{role} image does not resolve to one immutable repository digest"
            )
        digest_ref = matches[0]
        expected_labels = {
            "org.opencontainers.image.revision": target_sha,
            "org.opencontainers.image.source": SOURCE_URL,
            "org.opencontainers.image.version": f"sha-{target_sha}",
        }
        if not isinstance(labels, dict) or any(
            labels.get(key) != value for key, value in expected_labels.items()
        ):
            raise PullDeployError(f"{role} image OCI identity differs from target main")
        image_id = str(image.get("Id", ""))
        require_digest(image_id, f"{role} image ID")
        self.runner.run(["docker", "pull", digest_ref], env=self.control_environment())
        digest_inspection = self.runner.run(
            ["docker", "image", "inspect", digest_ref],
            env=self.control_environment(),
        )
        try:
            digest_values = json.loads(str(digest_inspection.stdout))
            digest_image = digest_values[0]
            digest_labels = digest_image["Config"]["Labels"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise PullDeployError(
                f"{role} immutable digest inspection is malformed"
            ) from exc
        if (
            digest_image.get("Id") != image_id
            or digest_ref not in digest_image.get("RepoDigests", [])
            or not isinstance(digest_labels, dict)
            or any(
                digest_labels.get(key) != value
                for key, value in expected_labels.items()
            )
        ):
            raise PullDeployError(
                f"{role} tag and immutable digest resolve to different images"
            )
        return {
            "tag": tag,
            "digest_ref": digest_ref,
            "image_id": image_id,
            "revision": target_sha,
            "source": SOURCE_URL,
            "version": f"sha-{target_sha}",
        }

    def _revalidate_materialized_images(
        self,
        images: object,
        *,
        source_sha: str,
        pull: bool,
    ) -> None:
        records = validate_image_records(images, source_sha=source_sha)
        for role in ("backend", "web"):
            record = records[role]
            if pull:
                self.runner.run(
                    ["docker", "pull", record["digest_ref"]],
                    env=self.control_environment(),
                )
            inspected = self.runner.run(
                ["docker", "image", "inspect", record["digest_ref"]],
                env=self.control_environment(),
            )
            try:
                values = json.loads(str(inspected.stdout))
                image = values[0]
                labels = image["Config"]["Labels"]
            except (json.JSONDecodeError, IndexError, KeyError, TypeError) as exc:
                raise PullDeployError(
                    f"previous {role} image evidence is malformed"
                ) from exc
            if (
                len(values) != 1
                or image.get("Id") != record["image_id"]
                or record["digest_ref"] not in image.get("RepoDigests", [])
                or labels.get("org.opencontainers.image.revision") != record["revision"]
                or labels.get("org.opencontainers.image.source") != record["source"]
                or labels.get("org.opencontainers.image.version") != record["version"]
            ):
                raise PullDeployError(
                    f"previous {role} image material differs from sealed rollback evidence"
                )

    def postgres_restore_image_evidence(self) -> dict[str, str]:
        self.runner.run(
            ["docker", "pull", POSTGRES16_IMAGE], env=self.control_environment()
        )
        result = self.runner.run(
            ["docker", "image", "inspect", POSTGRES16_IMAGE],
            env=self.control_environment(),
        )
        try:
            values = json.loads(str(result.stdout))
            image = values[0]
        except (json.JSONDecodeError, IndexError, TypeError) as exc:
            raise PullDeployError(
                "PostgreSQL restore image inspection is malformed"
            ) from exc
        image_id = require_digest(
            image.get("Id") if isinstance(image, dict) else None,
            "PostgreSQL restore image ID",
        )
        repo_digests = image.get("RepoDigests") if isinstance(image, dict) else None
        named, digest = POSTGRES16_IMAGE.rsplit("@", 1)
        last_slash = named.rfind("/")
        last_colon = named.rfind(":")
        repository = named[:last_colon] if last_colon > last_slash else named
        canonical_repo_digest = f"{repository}@{digest}"
        if (
            not isinstance(repo_digests, list)
            or canonical_repo_digest not in repo_digests
        ):
            raise PullDeployError(
                "PostgreSQL restore image does not contain the pinned digest"
            )
        return {"digest_ref": POSTGRES16_IMAGE, "image_id": image_id}

    def controller_digest(self) -> str:
        return sha256_file(Path(__file__).resolve())

    def stable_helper_evidence(self) -> dict[str, str]:
        """Return immutable selector identities; versioned controls live elsewhere."""

        evidence: dict[str, str] = {}
        for name in STABLE_HELPER_FILES:
            path = self.bin_dir / name
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise PullDeployError(
                    f"stable control helper is missing: {name}"
                ) from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or path.is_symlink()
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise PullDeployError(f"stable control helper is unsafe: {name}")
            evidence[name] = sha256_file(path)
        return evidence

    def validate_installed_controls_against_target(self, target_sha: str) -> None:
        """The tiny router ABI is not upgraded by an ordinary pull deploy."""

        target_sha = require_sha(target_sha, "control source SHA")
        for name, relative in CONTROL_SOURCE_PATHS.items():
            installed = self.bin_dir / name
            if installed.read_bytes() != self._git_show(target_sha, relative):
                raise PullDeployError(
                    f"immutable selector differs from target Git object: {name}"
                )

    def active_control_evidence(self) -> dict[str, Any]:
        try:
            active, _manifest, _root = _control_runtime.load_active_control(
                self.runtime_root
            )
        except Exception as exc:
            raise PullDeployError("active control authority is unavailable") from exc
        return dict(active)

    def prepare_control_release(
        self,
        *,
        operation_id: str,
        target_sha: str,
        target_tree: str,
    ) -> dict[str, Any]:
        """Build or reuse one deterministic, immutable target control release."""

        self._require_deploy_lock_for_staging()
        operation_id = require_operation_id(operation_id)
        target_sha = require_sha(target_sha, "control release source SHA")
        target_tree = require_sha(target_tree, "control release source tree")
        try:
            source_manifest = _control_runtime.parse_source_manifest(
                self._git_show(target_sha, CONTROL_SOURCE_MANIFEST)
            )
        except Exception as exc:
            raise PullDeployError("target control release manifest is invalid") from exc
        compatibility = source_manifest["compatibility"]
        required_versions = {
            "handoff_protocol_versions": _control_runtime.PROTOCOL_VERSION,
            "descriptor_schema_versions": DESCRIPTOR_SCHEMA_VERSION,
            "current_state_schema_versions": 2,
            "marker_schema_versions": 2,
            "worker_slot_schema_versions": SLOT_RECORD_SCHEMA_VERSION,
            "prepare_abort_abi_versions": 1,
        }
        for field, required in required_versions.items():
            if required not in compatibility[field]:
                raise PullDeployError(
                    f"target controls do not support live {field}: {required}"
                )
        payloads: dict[str, bytes] = {}
        identities: dict[str, dict[str, Any]] = {}
        for source in source_manifest["files"]:
            payload = self._git_show(target_sha, source["source"])
            payloads[source["name"]] = payload
            identities[source["name"]] = {
                "sha256": sha256_bytes(payload),
                "size": len(payload),
                "mode": source["mode"],
            }
        identity = {
            "schema_version": _control_runtime.CONTROL_MANIFEST_SCHEMA_VERSION,
            "protocol_version": _control_runtime.PROTOCOL_VERSION,
            "source_sha": target_sha,
            "source_tree": target_tree,
            "compatibility": compatibility,
            "entrypoints": source_manifest["entrypoints"],
            "files": identities,
        }
        release_id = _control_runtime.release_identity(identity)
        manifest = {**identity, "release_id": release_id}
        try:
            _control_runtime.validate_control_manifest(manifest)
        except Exception as exc:
            raise PullDeployError(
                "generated control release identity is invalid"
            ) from exc
        final = self.control_releases_dir / release_id
        manifest_payload = canonical_json_bytes(manifest) + b"\n"
        if final.exists() or final.is_symlink():
            try:
                existing, existing_root = _control_runtime.load_control_release(
                    self.runtime_root, release_id
                )
            except Exception as exc:
                raise PullDeployError(
                    "existing content-addressed control release is invalid"
                ) from exc
            if existing != manifest or existing_root != final:
                raise PullDeployError("existing control release differs from target")
        else:
            staging = self.control_releases_dir / (
                ".prepare-" + operation_id + "-" + secrets.token_hex(8)
            )
            staging.mkdir(mode=0o700)
            try:
                for name, payload in payloads.items():
                    atomic_bytes(staging / name, payload, mode=0o700)
                atomic_bytes(
                    staging / _control_runtime.CONTROL_MANIFEST_NAME,
                    manifest_payload,
                    mode=0o600,
                )
                fsync_directory(staging)
                try:
                    os.rename(staging, final)
                    fsync_directory(self.control_releases_dir)
                except FileExistsError:
                    # A same-content concurrent prepare may have won.  The
                    # deploy lock normally prevents this, but exact validation
                    # keeps an unknown rename outcome safe.
                    shutil.rmtree(staging)
                existing, existing_root = _control_runtime.load_control_release(
                    self.runtime_root, release_id
                )
                if existing != manifest or existing_root != final:
                    raise PullDeployError(
                        "sealed control release differs after install"
                    )
            except BaseException:
                if staging.exists() and not staging.is_symlink():
                    shutil.rmtree(staging)
                raise
        candidate = {
            "schema_version": _control_runtime.CONTROL_CANDIDATE_SCHEMA_VERSION,
            "protocol_version": _control_runtime.PROTOCOL_VERSION,
            "component": "deployment-controls",
            "release_id": release_id,
            "source_sha": target_sha,
            "source_tree": target_tree,
            "manifest_sha256": sha256_bytes(manifest_payload),
            "operation_id": operation_id,
            "prepared_at": utc_now(),
        }
        try:
            _control_runtime.load_candidate_control(self.runtime_root, candidate)
        except Exception as exc:
            raise PullDeployError(
                "candidate control release failed validation"
            ) from exc
        return candidate

    def asset_evidence(self, expected_digest: str) -> dict[str, Any]:
        expected_digest = require_digest(expected_digest, "target asset manifest")
        target = ASSET_RELEASES_ROOT / expected_digest.split(":", 1)[1]
        target_evidence = inspect_asset_release(target, expected_digest)
        pointer = self.state_dir / "current-assets"
        previous: dict[str, Any] | None = None
        if pointer.exists() or pointer.is_symlink():
            metadata = pointer.lstat()
            if not stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise PullDeployError(
                    "external asset pointer is not a deploy-user-owned symlink"
                )
            raw_target = os.readlink(pointer)
            if not Path(raw_target).is_absolute():
                raise PullDeployError("external asset pointer target must be absolute")
            resolved = pointer.resolve(strict=True)
            manifest = resolved / "ASSET-MANIFEST.json"
            previous_digest = sha256_file(manifest)
            previous = inspect_asset_release(resolved, previous_digest)
        return {
            "pointer_path": str(pointer),
            **target_evidence,
            "previous": previous,
        }

    def _asset_pointer_target(self, descriptor: dict[str, Any]) -> Path | None:
        pointer = Path(descriptor["release_input"]["asset"]["pointer_path"])
        if not pointer.exists() and not pointer.is_symlink():
            return None
        metadata = pointer.lstat()
        if not stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise PullDeployError("external asset pointer is unsafe")
        raw = os.readlink(pointer)
        if not Path(raw).is_absolute():
            raise PullDeployError("external asset pointer target is not absolute")
        return pointer.resolve(strict=True)

    def _switch_asset_pointer(self, descriptor: dict[str, Any]) -> None:
        asset = descriptor["release_input"]["asset"]
        current = self._asset_pointer_target(descriptor)
        expected_previous = (
            Path(asset["previous"]["root"]) if asset["previous"] is not None else None
        )
        target = Path(asset["root"])
        if current == target:
            return
        if current != expected_previous:
            raise PullDeployError("external asset pointer changed after prepare")
        atomic_symlink(Path(asset["pointer_path"]), str(target))
        if self._asset_pointer_target(descriptor) != target:
            raise PullDeployError("external asset pointer switch did not commit")

    def _restore_previous_asset_pointer(self, descriptor: dict[str, Any]) -> None:
        asset = descriptor["release_input"]["asset"]
        pointer = Path(asset["pointer_path"])
        current = self._asset_pointer_target(descriptor)
        target = Path(asset["root"])
        previous = (
            Path(asset["previous"]["root"]) if asset["previous"] is not None else None
        )
        if current == previous:
            return
        if current != target:
            raise PullDeployError(
                "external asset pointer is neither previous nor candidate"
            )
        if previous is None:
            pointer.unlink()
            fsync_directory(pointer.parent)
        else:
            atomic_symlink(pointer, str(previous))

    def _validate_worker_env(self, control_root: Path) -> dict[str, str]:
        path = self.config_dir / "worker.env"
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise PullDeployError(
                "external production Worker environment is missing"
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise PullDeployError("external production Worker environment is unsafe")
        helper = control_root / "monomer_worker_env.py"
        helper_metadata = helper.lstat()
        if (
            not stat.S_ISREG(helper_metadata.st_mode)
            or helper.is_symlink()
            or helper_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(helper_metadata.st_mode) != 0o700
        ):
            raise PullDeployError("stable Worker environment validator is unsafe")
        self.runner.run(
            ["/usr/bin/python3", "-I", "-B", str(helper), "validate", str(path)],
            env=self.control_environment(),
        )
        values = parse_literal_env(path)
        deploy_values = self.production_deploy_values(check_free_space=False)
        byteff2_python = values.get("BYTEFF2_PYTHON")
        openmm_dir = values.get("BYTEFF2_OPENMM_DIR")
        if (
            byteff2_python != deploy_values.get("NEXPOLY_WORKER_BASE_PYTHON")
            or not byteff2_python
            or not openmm_dir
            or values.get("MONOMER_MD_WORKER_MODE") != "real"
            or deploy_values.get("MONOMER_MD_REQUIRE_TRANSPORT_READY") != "true"
            or values.get("MONOMER_MD_TRANSPORT_CUDA_SMOKE_ENABLED") != "true"
            or values.get("MONOMER_MD_PYTHON")
        ):
            raise PullDeployError(
                "external Worker environment lost its production runtime contract"
            )
        asset_root = self.state_dir / "current-assets/byteff2"
        expected_worker_values = {
            "BYTEFF2_ROOT": str(asset_root),
            "BYTEFF2_PYTHON": byteff2_python,
            "BYTEFF2_OPENMM_DIR": openmm_dir,
            "PYTHONPATH": (
                f"{self.production_root}:{asset_root}:"
                f"{asset_root / 'submodules/bytemol'}"
            ),
            "MONOMER_MD_JOB_ROOT": str(self.state_dir / "monomer-md-worker-runs"),
            "MONOMER_MD_WORKER_UDS": str(
                self.state_dir / "monomer-md-worker-socket/worker.sock"
            ),
            "MONOMER_MD_WORKER_ID": "monomer-md-production-worker",
            "MONOMER_MD_WORKER_MODE": "real",
            "MONOMER_MD_MAX_ACTIVE_JOBS": "1",
            "MONOMER_MD_MAX_CONCURRENT_JOBS": "1",
            "MONOMER_MD_DEFAULT_STEPS": "300",
            "MONOMER_MD_MAX_STEPS": "300",
            "MONOMER_MD_TRANSPORT_CUDA_SMOKE_ENABLED": "true",
            "NEXPOLY_GPU_DEVICE": "2",
            "MONOMER_MD_GPU_BROKER_ENABLED": "false",
            "MONOMER_MD_GPU_BROKER_ENVIRONMENT": "prod",
            "MONOMER_MD_GPU_BROKER_SOCKET_PATH": str(
                self.state_dir / "gpu-resource/broker.sock"
            ),
            "MONOMER_MD_GPU_MPS_PIPE_ROOT": str(self.state_dir / "gpu-resource"),
        }
        for key, expected in expected_worker_values.items():
            if values.get(key) != expected:
                raise PullDeployError(
                    f"external Worker production setting {key} is not pinned"
                )
        if values.get("MONOMER_MD_CUDA_VISIBLE_DEVICES") not in {None, "2"}:
            raise PullDeployError(
                "external Worker production setting "
                "MONOMER_MD_CUDA_VISIBLE_DEVICES is not pinned"
            )
        python_path = Path(byteff2_python)
        openmm_path = Path(openmm_dir)
        gmx = python_path.parent / "gmx"
        for executable in (python_path, gmx):
            resolved = executable.resolve(strict=True)
            metadata = resolved.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o022
                or not os.access(resolved, os.X_OK)
            ):
                raise PullDeployError("external Worker executable identity is unsafe")
        openmm_metadata = openmm_path.lstat()
        if not stat.S_ISDIR(openmm_metadata.st_mode) or openmm_path.is_symlink():
            raise PullDeployError("external ByteFF2 OpenMM directory is unsafe")
        worker_dsn = values.get("APP_POSTGRES_DSN")
        try:
            _load_governance_core().validate_postgres_dsn(
                worker_dsn,
                "worker.env APP_POSTGRES_DSN",
                expected_user=deploy_values["NEXPOLY_POSTGRES_USER"],
                expected_password=deploy_values["NEXPOLY_POSTGRES_PASSWORD"],
                expected_host="127.0.0.1",
                expected_port=55432,
                expected_database="nexpoly",
            )
        except Exception as exc:
            raise PullDeployError(
                "external Worker PostgreSQL DSN identity is invalid"
            ) from exc
        return {
            "path": str(path),
            "sha256": sha256_file(path),
            "byteff2_python": byteff2_python,
            "byteff2_openmm_dir": openmm_dir,
            "gmx_sha256": sha256_file(gmx.resolve(strict=True)),
        }

    def _validate_candidate_unit_payload(self, payload: bytes) -> None:
        try:
            text = payload.decode("utf-8")
        except UnicodeError as exc:
            raise PullDeployError("candidate Worker systemd unit is not UTF-8") from exc
        required = {
            "WorkingDirectory=/data/lzq/gith/nexpoly",
            "ExecStart=/usr/bin/python3 -I -B /data/lzq/gith/nexpoly-runtime/bin/control_runtime_selector.py run monomer-md",
            "UMask=0077",
            "NoNewPrivileges=true",
        }
        if not required.issubset(set(text.splitlines())):
            raise PullDeployError(
                "candidate Worker unit does not use the stable A/B launcher contract"
            )
        if any(
            token in text
            for token in (
                "EnvironmentFile=",
                "run_host_worker.sh",
                ".env.monomer-md-worker",
            )
        ):
            raise PullDeployError(
                "candidate Worker unit retains a legacy live-checkout launcher"
            )

    def prepare_worker_controls(
        self,
        *,
        operation_id: str,
        target_sha: str,
        executor_control: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            _candidate, manifest, control_root = (
                _control_runtime.load_candidate_control(
                    self.runtime_root, executor_control
                )
            )
        except Exception as exc:
            raise PullDeployError("candidate Worker controls are unavailable") from exc
        role = manifest["entrypoints"].get("monomer-md")
        if not isinstance(role, dict) or role.get("kind") != "worker":
            raise PullDeployError("candidate controls lack the monomer-md role")
        launcher_name = role["launcher"]
        worker_env = self._validate_worker_env(control_root)
        operation, _descriptor, _ready = self._operation_paths(operation_id)
        payload = self._git_show(target_sha, MONOMER_MD_UNIT_SOURCE)
        self._validate_candidate_unit_payload(payload)
        candidate = operation / MONOMER_MD_UNIT_NAME
        atomic_bytes(candidate, payload)
        try:
            home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
        except KeyError as exc:
            raise PullDeployError("deploy user has no passwd identity") from exc
        if not self.test_root_mode and home != DEPLOY_USER_HOME:
            raise PullDeployError(
                "deploy user home differs from the fixed production identity"
            )
        unit_parent = home / ".config/systemd/user"
        parent_metadata = unit_parent.lstat()
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or unit_parent.is_symlink()
            or parent_metadata.st_uid != os.geteuid()
            or parent_metadata.st_mode & 0o022
        ):
            raise PullDeployError("deploy-user systemd unit directory is unsafe")
        target = unit_parent / MONOMER_MD_UNIT_NAME
        previous_present = target.exists() or target.is_symlink()
        previous_sha: str | None = None
        previous_backup: str | None = None
        if previous_present:
            metadata = target.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or target.is_symlink()
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o022
            ):
                raise PullDeployError("installed production Worker unit is unsafe")
            previous_payload = target.read_bytes()
            previous_sha = sha256_bytes(previous_payload)
            backup = operation / f"previous-{MONOMER_MD_UNIT_NAME}"
            if backup.exists():
                if sha256_file(backup) != previous_sha:
                    raise PullDeployError(
                        "previous Worker unit backup changed during retry"
                    )
            else:
                atomic_bytes(backup, previous_payload)
            previous_backup = str(backup)
        previous_unit_state = self._worker_unit_state(target)
        expected_previous_state = (
            {
                "LoadState": "loaded",
                "FragmentPath": str(target),
                "DropInPaths": "",
                "NeedDaemonReload": "no",
                "UnitFileState": "enabled",
            }
            if previous_present
            else {
                "LoadState": "not-found",
                "FragmentPath": "",
                "DropInPaths": "",
                "NeedDaemonReload": "no",
                "UnitFileState": "",
            }
        )
        if previous_unit_state != expected_previous_state:
            raise PullDeployError(
                "installed Worker systemd state is not the governed enabled baseline"
            )
        return {
            "worker_env": worker_env,
            "systemd_unit": {
                "source_path": MONOMER_MD_UNIT_SOURCE,
                "candidate_path": str(candidate),
                "target_path": str(target),
                "sha256": sha256_bytes(payload),
                "previous_present": previous_present,
                "previous_sha256": previous_sha,
                "previous_backup_path": previous_backup,
                "previous_unit_state": previous_unit_state,
                "control_release_id": executor_control["release_id"],
                "launcher_sha256": manifest["files"][launcher_name]["sha256"],
            },
        }

    def _revalidate_worker_controls(self, descriptor: dict[str, Any]) -> None:
        controls = descriptor["monomer_md"]
        executor = descriptor["controller"]["executor_control"]
        try:
            _candidate, manifest, control_root = (
                _control_runtime.load_candidate_control(self.runtime_root, executor)
            )
        except Exception as exc:
            raise PullDeployError("candidate Worker controls changed") from exc
        if self._validate_worker_env(control_root) != controls["worker_env"]:
            raise PullDeployError("external Worker environment changed after prepare")
        unit = controls["systemd_unit"]
        role = manifest["entrypoints"].get("monomer-md")
        if (
            not isinstance(role, dict)
            or role.get("kind") != "worker"
            or unit["control_release_id"] != executor["release_id"]
            or unit["launcher_sha256"] != manifest["files"][role["launcher"]]["sha256"]
        ):
            raise PullDeployError("candidate Worker launcher authority changed")
        candidate = Path(unit["candidate_path"])
        if sha256_file(candidate) != unit["sha256"]:
            raise PullDeployError("candidate Worker unit changed after prepare")
        payload = self._git_show(
            descriptor["repository"]["target_sha"], MONOMER_MD_UNIT_SOURCE
        )
        if sha256_bytes(payload) != unit["sha256"] or payload != candidate.read_bytes():
            raise PullDeployError(
                "candidate Worker unit differs from target Git object"
            )
        self._validate_candidate_unit_payload(payload)
        target = Path(unit["target_path"])
        if self._worker_unit_state(target) != unit["previous_unit_state"]:
            raise PullDeployError(
                "installed Worker systemd state changed after prepare"
            )
        if unit["previous_present"]:
            if (
                not target.is_file()
                or target.is_symlink()
                or sha256_file(target) != unit["previous_sha256"]
            ):
                raise PullDeployError(
                    "installed previous Worker unit changed after prepare"
                )
        elif target.exists() or target.is_symlink():
            raise PullDeployError(
                "installed Worker unit appeared after absent-unit prepare"
            )

    def _worker_unit_state(self, target: Path) -> dict[str, str]:
        shown = self.runner.run(
            [
                "systemctl",
                "--user",
                "show",
                MONOMER_MD_UNIT_NAME,
                "--property=LoadState",
                "--property=FragmentPath",
                "--property=DropInPaths",
                "--property=NeedDaemonReload",
                "--property=UnitFileState",
            ],
            env=self.control_environment(),
        )
        fields = dict(
            line.split("=", 1) for line in str(shown.stdout).splitlines() if "=" in line
        )
        expected_fields = {
            "LoadState",
            "FragmentPath",
            "DropInPaths",
            "NeedDaemonReload",
            "UnitFileState",
        }
        if set(fields) != expected_fields:
            raise PullDeployError("systemd Worker unit evidence has an invalid shape")
        return fields

    def _reload_and_verify_worker_unit(
        self,
        *,
        target: Path,
        expected_digest: str | None,
    ) -> None:
        self.runner.run(
            ["systemctl", "--user", "daemon-reload"],
            env=self.control_environment(),
        )
        if expected_digest is not None:
            if sha256_file(target) != expected_digest:
                raise PullDeployError(
                    "installed Worker unit digest differs after reload"
                )
            fields = self._worker_unit_state(target)
            if fields != {
                "LoadState": "loaded",
                "FragmentPath": str(target),
                "DropInPaths": "",
                "NeedDaemonReload": "no",
                "UnitFileState": "enabled",
            }:
                raise PullDeployError(
                    "systemd did not load the enabled sealed Worker unit without drop-ins"
                )
        elif target.exists() or target.is_symlink():
            raise PullDeployError("removed Worker unit remains on disk after reload")
        else:
            fields = self._worker_unit_state(target)
            if fields != {
                "LoadState": "not-found",
                "FragmentPath": "",
                "DropInPaths": "",
                "NeedDaemonReload": "no",
                "UnitFileState": "",
            }:
                raise PullDeployError("removed Worker unit remains loaded or enabled")

    def _install_candidate_worker_unit(self, descriptor: dict[str, Any]) -> None:
        self._revalidate_worker_controls(descriptor)
        unit = descriptor["monomer_md"]["systemd_unit"]
        target = Path(unit["target_path"])
        current_present = target.exists() or target.is_symlink()
        if current_present != unit["previous_present"]:
            raise PullDeployError(
                "installed Worker unit presence changed after prepare"
            )
        if current_present and sha256_file(target) != unit["previous_sha256"]:
            raise PullDeployError("installed Worker unit changed after prepare")
        atomic_control_file(
            target, Path(unit["candidate_path"]).read_bytes(), mode=0o600
        )
        if not unit["previous_present"]:
            self.runner.run(
                ["systemctl", "--user", "enable", MONOMER_MD_UNIT_NAME],
                env=self.control_environment(),
            )
        self._reload_and_verify_worker_unit(
            target=target, expected_digest=unit["sha256"]
        )

    def _restore_previous_worker_unit(self, descriptor: dict[str, Any]) -> None:
        unit = descriptor["monomer_md"]["systemd_unit"]
        target = Path(unit["target_path"])
        current_digest: str | None = None
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file():
                raise PullDeployError("installed Worker unit became unsafe")
            current_digest = sha256_file(target)
        allowed = {unit["sha256"]}
        if unit["previous_present"]:
            allowed.add(unit["previous_sha256"])
        if current_digest is not None and current_digest not in allowed:
            raise PullDeployError(
                "installed Worker unit is neither previous nor candidate"
            )
        if unit["previous_present"]:
            backup = Path(unit["previous_backup_path"])
            if sha256_file(backup) != unit["previous_sha256"]:
                raise PullDeployError("previous Worker unit backup is unavailable")
            if current_digest != unit["previous_sha256"]:
                atomic_control_file(target, backup.read_bytes(), mode=0o600)
        elif target.exists() or target.is_symlink():
            if (
                target.is_symlink()
                or not target.is_file()
                or sha256_file(target) != unit["sha256"]
            ):
                raise PullDeployError("refusing to remove an unrecognized Worker unit")
            self.runner.run(
                ["systemctl", "--user", "disable", MONOMER_MD_UNIT_NAME],
                env=self.control_environment(),
            )
            target.unlink()
            fsync_directory(target.parent)
        self._reload_and_verify_worker_unit(
            target=target,
            expected_digest=(
                unit["previous_sha256"] if unit["previous_present"] else None
            ),
        )

    def _active_slot(self) -> dict[str, Any] | None:
        if (
            not self.active_slot_path.exists()
            and not self.active_slot_path.is_symlink()
        ):
            return None
        active = validate_active_slot_record(load_private_json(self.active_slot_path))
        record_path = self.slots_state_dir / f"md-{active['slot']}.json"
        record = validate_slot_record(load_private_json(record_path), active["slot"])
        if worker_record_digest(record) != active["slot_record_sha256"]:
            raise PullDeployError("active slot record digest differs from its pointer")
        if any(
            active[key] != record[key]
            for key in ("source_sha", "source_tree", "worker_lock_sha256")
        ):
            raise PullDeployError("active slot and slot record identities differ")
        return active

    def choose_inactive_slot(self) -> str:
        active = self._active_slot()
        if active is None:
            return "a"
        return "b" if active["slot"] == "a" else "a"

    def _base_python_identity(self, base_python: Path) -> dict[str, Any]:
        try:
            return shared_inspect_base_python_identity(
                base_python,
                environment={"PATH": SAFE_PATH, "HOME": os.environ.get("HOME", "")},
            )
        except WorkerSlotError as exc:
            raise PullDeployError(
                f"Worker base Python identity is invalid: {exc}"
            ) from exc

    def _assert_slot_not_running(self, slot_root: Path) -> None:
        if not slot_root.exists():
            return
        resolved = slot_root.resolve()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                process_metadata = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise PullDeployError(
                    f"cannot inspect process {entry.name} before recycling Worker slot"
                ) from exc
            if process_metadata.st_uid != os.geteuid():
                continue

            def start_time() -> str:
                try:
                    raw = (entry / "stat").read_text(encoding="utf-8")
                except FileNotFoundError:
                    raise
                except OSError as exc:
                    raise PullDeployError(
                        f"cannot read process {entry.name} identity"
                    ) from exc
                close = raw.rfind(")")
                fields = raw[close + 2 :].split() if close >= 0 else []
                if len(fields) <= 19:
                    raise PullDeployError(f"process {entry.name} identity is malformed")
                return fields[19]

            try:
                before = start_time()
                with (entry / "cmdline").open("rb") as stream:
                    command = stream.read(64 * 1024 + 1)
                if len(command) > 64 * 1024:
                    raise PullDeployError(
                        f"process {entry.name} command line is oversized"
                    )
                try:
                    executable = (entry / "exe").resolve(strict=True)
                except FileNotFoundError:
                    executable = None
                except PermissionError:
                    # A same-UID nondumpable session helper (for example
                    # sd-pam) may expose a stable cmdline but deny exe. The
                    # bounded, start-time-fenced argv remains authoritative
                    # for venv launchers; exe is only additional evidence.
                    executable = None
                except (OSError, RuntimeError) as exc:
                    raise PullDeployError(
                        f"cannot inspect process {entry.name} executable"
                    ) from exc
                after = start_time()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise PullDeployError(
                    f"cannot inspect process {entry.name} command line"
                ) from exc
            if before != after:
                # PID reuse raced the snapshot. Fail closed; an operator retry
                # will rescan the replacement process from a stable identity.
                raise PullDeployError(
                    f"process {entry.name} changed while checking inactive slot"
                )
            arguments = [
                Path(os.fsdecode(value))
                for value in command.split(b"\0")
                if value and os.path.isabs(os.fsdecode(value))
            ]
            command_uses_slot = any(
                argument == resolved or resolved in argument.parents
                for argument in arguments
            )
            executable_uses_slot = executable is not None and (
                executable == resolved or resolved in executable.parents
            )
            if command_uses_slot or executable_uses_slot:
                raise PullDeployError(
                    f"inactive slot is still used by process {entry.name}"
                )

    def _require_deploy_lock_for_staging(self) -> None:
        if self._held_deploy_lock_fd is None:
            raise PullDeployError(
                "private deployment staging lacks deploy.lock ownership"
            )

    def _discard_private_staging(self, staging: Path, *, label: str) -> None:
        """Quarantine then remove one already-classified same-operation tree."""

        self._require_deploy_lock_for_staging()
        ensure_private_directory(staging)
        parent = staging.parent
        ensure_private_directory(parent)
        tombstone = parent / f"{staging.name}.discard"
        try:
            rename_directory_noreplace(staging, tombstone)
        except FileExistsError as exc:
            raise PullDeployError(f"{label} quarantine path already exists") from exc
        shutil.rmtree(tombstone)
        fsync_directory(parent)

    def _clear_private_staging_tombstone(
        self,
        staging: Path,
        *,
        owner_name: str,
        expected_owner: Mapping[str, Any],
        label: str,
        identity_fields: Iterable[str] | None = None,
    ) -> None:
        """Finish a same-operation quarantine interrupted before deletion."""

        tombstone = staging.parent / f"{staging.name}.discard"
        if not (tombstone.exists() or tombstone.is_symlink()):
            return
        self._require_deploy_lock_for_staging()
        ensure_private_directory(tombstone)
        owner_path = tombstone / owner_name
        if owner_path.exists() or owner_path.is_symlink():
            owner = load_private_json(owner_path)
            differs = (
                owner != dict(expected_owner)
                if identity_fields is None
                else any(
                    owner.get(field) != expected_owner.get(field)
                    for field in identity_fields
                )
            )
            if differs:
                raise PullDeployError(
                    f"{label} quarantine belongs to another operation"
                )
        # An ownerless tombstone can only be the exact mkdir-before-owner
        # residue already isolated under this operation-specific name.
        shutil.rmtree(tombstone)
        fsync_directory(tombstone.parent)

    @staticmethod
    def _wheel_cache_owner(
        *,
        operation_id: str,
        wheel_cache_key: str,
        worker_lock_sha256: str,
        base_python_identity_sha256: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "operation_id": operation_id,
            "wheel_cache_key": wheel_cache_key,
            "worker_lock_sha256": worker_lock_sha256,
            "base_python_identity_sha256": base_python_identity_sha256,
        }

    def _validate_wheel_cache(
        self,
        cache: Path,
        *,
        wheel_cache_key: str,
        worker_lock_sha256: str,
        base_python_identity_sha256: str,
        staging_operation_id: str | None = None,
    ) -> str:
        ensure_private_directory(cache)
        owner = load_private_json(cache / ".owner.json")
        if (
            set(owner)
            != {
                "schema_version",
                "operation_id",
                "wheel_cache_key",
                "worker_lock_sha256",
                "base_python_identity_sha256",
            }
            or owner.get("schema_version") != 1
            or require_operation_id(str(owner.get("operation_id", "")))
            != owner.get("operation_id")
            or (
                staging_operation_id is not None
                and owner.get("operation_id") != staging_operation_id
            )
            or owner.get("wheel_cache_key") != wheel_cache_key
            or owner.get("worker_lock_sha256") != worker_lock_sha256
            or owner.get("base_python_identity_sha256")
            != base_python_identity_sha256
        ):
            raise PullDeployError("Worker wheel cache ownership is invalid")
        ready = load_private_json(cache / "READY.json")
        payload = wheel_payload_inventory(cache)
        if (
            set(ready)
            != {
                "schema_version",
                "status",
                "operation_id",
                "wheel_cache_key",
                "worker_lock_sha256",
                "base_python_identity_sha256",
                "payload_file_count",
                "payload_inventory_sha256",
                "ready_at",
            }
            or ready.get("schema_version") != 1
            or ready.get("status") != "ready"
            or ready.get("operation_id") != owner["operation_id"]
            or ready.get("wheel_cache_key") != wheel_cache_key
            or ready.get("worker_lock_sha256") != worker_lock_sha256
            or ready.get("base_python_identity_sha256")
            != base_python_identity_sha256
            or ready.get("payload_file_count") != len(payload["files"])
            or ready.get("payload_inventory_sha256")
            != payload["inventory_sha256"]
            or not isinstance(ready.get("ready_at"), str)
            or not ready["ready_at"]
        ):
            raise PullDeployError("Worker wheel cache READY evidence is invalid")
        return directory_inventory_digest(cache)

    def _remove_owned_slot(self, slot: str, operation_id: str) -> None:
        root = self.venv_root / f"md-{slot}"
        owner = root / ".preparing.json"
        ready_record = self.slots_state_dir / f"md-{slot}.json"
        tombstone = self.venv_root / f".{slot}.discard-{operation_id}"
        if tombstone.exists() or tombstone.is_symlink():
            ensure_private_directory(tombstone)
            tombstone_owner = tombstone / ".preparing.json"
            if tombstone_owner.exists() or tombstone_owner.is_symlink():
                owner_doc = load_private_json(tombstone_owner)
                if (
                    owner_doc.get("operation_id") != operation_id
                    or owner_doc.get("slot") != slot
                ):
                    raise PullDeployError(
                        "inactive Worker slot tombstone belongs to another operation"
                    )
            elif ready_record.exists() or ready_record.is_symlink():
                validate_slot_record(
                    load_private_json(ready_record), slot
                )
            else:
                # rmtree may already have removed the in-tree owner before a
                # crash.  The deterministic operation-specific quarantine
                # name, deploy.lock and owner-private parent make this an
                # interrupted deletion, not an unclassified live slot.
                pass
            shutil.rmtree(tombstone)
            fsync_directory(self.venv_root)
            if ready_record.exists() or ready_record.is_symlink():
                ready_record.unlink()
                fsync_directory(ready_record.parent)
        if not root.exists() and not root.is_symlink():
            return
        if (
            not root.is_dir()
            or root.is_symlink()
            or root.resolve().parent != self.venv_root.resolve()
        ):
            raise PullDeployError("inactive Worker slot path is unsafe")
        self._assert_slot_not_running(root)
        if owner.exists():
            owner_doc = load_private_json(owner)
            if (
                owner_doc.get("operation_id") != operation_id
                or owner_doc.get("slot") != slot
            ):
                raise PullDeployError(
                    "partial inactive slot belongs to another operation"
                )
        elif ready_record.exists():
            # A sealed, validated inactive slot is intentionally recyclable on
            # the third and later A/B deployment. Partial slots remain bound
            # to their originating operation by the branch above.
            validate_slot_record(load_private_json(ready_record), slot)
        else:
            raise PullDeployError("refusing to remove an unowned inactive Worker slot")
        if tombstone.exists() or tombstone.is_symlink():
            raise PullDeployError("inactive Worker slot tombstone already exists")
        try:
            rename_directory_noreplace(root, tombstone)
        except FileExistsError as exc:
            raise PullDeployError(
                "inactive Worker slot tombstone already exists"
            ) from exc
        shutil.rmtree(tombstone)
        fsync_directory(self.venv_root)
        if ready_record.exists():
            ready_record.unlink()
            fsync_directory(ready_record.parent)

    def prepare_md_slot(
        self,
        *,
        operation_id: str,
        target_sha: str,
        target_tree: str,
        lock_payload: bytes,
    ) -> dict[str, Any]:
        values = parse_literal_env(self.config_dir / "deploy.env")
        base_python_value = values.get("NEXPOLY_WORKER_BASE_PYTHON")
        if not base_python_value:
            raise PullDeployError("deploy.env does not pin NEXPOLY_WORKER_BASE_PYTHON")
        base_identity = self._base_python_identity(Path(base_python_value))
        expected_base_identity = values.get(
            "NEXPOLY_WORKER_BASE_PYTHON_IDENTITY_SHA256"
        )
        if (
            expected_base_identity is None
            or require_digest(expected_base_identity, "Worker base Python identity pin")
            != base_identity["identity_sha256"]
        ):
            raise PullDeployError(
                "Worker base Python identity differs from deploy.env pin"
            )
        worker_lock_sha = sha256_bytes(lock_payload)
        normalized = lock_payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        requirements_sha = sha256_bytes(normalized)
        wheel_cache_key = sha256_bytes(
            canonical_json_bytes(
                {
                    "worker_lock_sha256": worker_lock_sha,
                    "base_python_identity_sha256": base_identity["identity_sha256"],
                    "platform": sys.platform,
                }
            )
        )
        cache = self.wheel_cache_dir / wheel_cache_key
        operation_dir = self.prepared_root / operation_id
        lock_path = operation_dir / "monomer-md-requirements.lock"
        atomic_bytes(lock_path, lock_payload)
        expected_owner = self._wheel_cache_owner(
            operation_id=operation_id,
            wheel_cache_key=wheel_cache_key,
            worker_lock_sha256=worker_lock_sha,
            base_python_identity_sha256=base_identity["identity_sha256"],
        )
        staging = (
            self.wheel_cache_dir / f".{wheel_cache_key}.staging-{operation_id}"
        )
        self._clear_private_staging_tombstone(
            staging,
            owner_name=".owner.json",
            expected_owner=expected_owner,
            label="Worker wheel cache staging",
        )
        if (
            (cache.exists() or cache.is_symlink())
            and (staging.exists() or staging.is_symlink())
        ):
            if not staging.is_dir() or staging.is_symlink():
                raise PullDeployError("Worker wheel cache staging is unsafe")
            owner_path = staging / ".owner.json"
            if (
                (owner_path.exists() or owner_path.is_symlink())
                and load_private_json(owner_path) != expected_owner
            ):
                raise PullDeployError(
                    "Worker wheel cache staging belongs to another operation"
                )
            self._validate_wheel_cache(
                cache,
                wheel_cache_key=wheel_cache_key,
                worker_lock_sha256=worker_lock_sha,
                base_python_identity_sha256=base_identity[
                    "identity_sha256"
                ],
            )
            self._discard_private_staging(
                staging, label="Worker wheel cache staging"
            )
        if not cache.exists() and not cache.is_symlink():
            if staging.exists() or staging.is_symlink():
                if not staging.is_dir() or staging.is_symlink():
                    raise PullDeployError("Worker wheel cache staging is unsafe")
                owner_path = staging / ".owner.json"
                if not owner_path.exists() and not owner_path.is_symlink():
                    # This exact staging name is serialized by deploy.lock.
                    # An ownerless mkdir is an interrupted same-operation
                    # creation and can be quarantined rather than becoming an
                    # unrecoverable foreign directory.
                    self._discard_private_staging(
                        staging, label="Worker wheel cache staging"
                    )
                elif load_private_json(owner_path) != expected_owner:
                    raise PullDeployError(
                        "Worker wheel cache staging belongs to another operation"
                    )
                elif (staging / "READY.json").exists() or (
                    staging / "READY.json"
                ).is_symlink():
                    self._validate_wheel_cache(
                        staging,
                        wheel_cache_key=wheel_cache_key,
                        worker_lock_sha256=worker_lock_sha,
                        base_python_identity_sha256=base_identity[
                            "identity_sha256"
                        ],
                        staging_operation_id=operation_id,
                    )
                    fsync_private_tree(staging)
                    try:
                        rename_directory_noreplace(staging, cache)
                    except FileExistsError:
                        # A previous no-clobber publication may have committed
                        # while its response was lost.  Accept only the exact
                        # content-addressed final tree.
                        self._validate_wheel_cache(
                            cache,
                            wheel_cache_key=wheel_cache_key,
                            worker_lock_sha256=worker_lock_sha,
                            base_python_identity_sha256=base_identity[
                                "identity_sha256"
                            ],
                        )
                        self._discard_private_staging(
                            staging, label="Worker wheel cache staging"
                        )
                else:
                    self._discard_private_staging(
                        staging, label="Worker wheel cache staging"
                    )
            if not cache.exists() and not cache.is_symlink():
                staging.mkdir(mode=0o700)
                atomic_json(staging / ".owner.json", expected_owner)
                fsync_directory(staging)
                self.runner.run(
                    [
                        base_identity["resolved_path"],
                        "-I",
                        "-m",
                        "pip",
                        "download",
                        "--require-hashes",
                        "--only-binary=:all:",
                        "--dest",
                        str(staging),
                        "-r",
                        str(lock_path),
                    ],
                    env={
                        "PATH": SAFE_PATH,
                        "PIP_CONFIG_FILE": os.devnull,
                        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                        "PIP_NO_INPUT": "1",
                        "PYTHONNOUSERSITE": "1",
                    },
                )
                payload_inventory = wheel_payload_inventory(staging)
                atomic_json(
                    staging / "READY.json",
                    {
                        "schema_version": 1,
                        "status": "ready",
                        "operation_id": operation_id,
                        "wheel_cache_key": wheel_cache_key,
                        "worker_lock_sha256": worker_lock_sha,
                        "base_python_identity_sha256": base_identity[
                            "identity_sha256"
                        ],
                        "payload_file_count": len(payload_inventory["files"]),
                        "payload_inventory_sha256": payload_inventory[
                            "inventory_sha256"
                        ],
                        "ready_at": utc_now(),
                    },
                )
                fsync_private_tree(staging)
                try:
                    rename_directory_noreplace(staging, cache)
                except FileExistsError:
                    self._validate_wheel_cache(
                        cache,
                        wheel_cache_key=wheel_cache_key,
                        worker_lock_sha256=worker_lock_sha,
                        base_python_identity_sha256=base_identity[
                            "identity_sha256"
                        ],
                    )
                    self._discard_private_staging(
                        staging, label="Worker wheel cache staging"
                    )
                except BaseException:
                    # renameat2 may have committed while the caller lost its
                    # response.  The final READY+inventory is the only safe
                    # successful interpretation.
                    if cache.exists() or cache.is_symlink():
                        self._validate_wheel_cache(
                            cache,
                            wheel_cache_key=wheel_cache_key,
                            worker_lock_sha256=worker_lock_sha,
                            base_python_identity_sha256=base_identity[
                                "identity_sha256"
                            ],
                        )
                        if staging.exists() or staging.is_symlink():
                            if (
                                not staging.is_dir()
                                or staging.is_symlink()
                                or load_private_json(
                                    staging / ".owner.json"
                                )
                                != expected_owner
                            ):
                                raise PullDeployError(
                                    "Worker wheel cache staging changed during publication"
                                )
                            self._discard_private_staging(
                                staging,
                                label="Worker wheel cache staging",
                            )
                    else:
                        raise
        wheel_inventory = self._validate_wheel_cache(
            cache,
            wheel_cache_key=wheel_cache_key,
            worker_lock_sha256=worker_lock_sha,
            base_python_identity_sha256=base_identity["identity_sha256"],
        )
        slot = self.choose_inactive_slot()
        slot_root = self.venv_root / f"md-{slot}"
        self._remove_owned_slot(slot, operation_id)
        slot_owner = {
            "schema_version": 1,
            "operation_id": operation_id,
            "slot": slot,
        }
        slot_staging = (
            self.venv_root / f".md-{slot}.preparing-{operation_id}"
        )
        self._clear_private_staging_tombstone(
            slot_staging,
            owner_name=".preparing.json",
            expected_owner=slot_owner,
            label="Worker slot staging",
        )
        if slot_staging.exists() or slot_staging.is_symlink():
            if not slot_staging.is_dir() or slot_staging.is_symlink():
                raise PullDeployError("Worker slot staging is unsafe")
            owner_path = slot_staging / ".preparing.json"
            if not owner_path.exists() and not owner_path.is_symlink():
                self._discard_private_staging(
                    slot_staging, label="Worker slot staging"
                )
            elif load_private_json(owner_path) != slot_owner:
                raise PullDeployError(
                    "Worker slot staging belongs to another operation"
                )
            else:
                self._discard_private_staging(
                    slot_staging, label="Worker slot staging"
                )
        slot_staging.mkdir(mode=0o700)
        atomic_json(slot_staging / ".preparing.json", slot_owner)
        fsync_private_tree(slot_staging)
        try:
            rename_directory_noreplace(slot_staging, slot_root)
        except FileExistsError:
            owner = load_private_json(slot_root / ".preparing.json")
            if owner != slot_owner:
                raise PullDeployError(
                    "published Worker slot belongs to another operation"
                )
            self._discard_private_staging(
                slot_staging, label="Worker slot staging"
            )
        except BaseException:
            if (
                slot_root.exists()
                and not slot_root.is_symlink()
                and load_private_json(slot_root / ".preparing.json")
                == slot_owner
            ):
                if slot_staging.exists() or slot_staging.is_symlink():
                    if (
                        not slot_staging.is_dir()
                        or slot_staging.is_symlink()
                        or load_private_json(
                            slot_staging / ".preparing.json"
                        )
                        != slot_owner
                    ):
                        raise PullDeployError(
                            "Worker slot staging changed during publication"
                        )
                    self._discard_private_staging(
                        slot_staging, label="Worker slot staging"
                    )
            else:
                raise
        venv = slot_root / "venv"
        try:
            self.runner.run(
                [
                    base_identity["resolved_path"],
                    "-I",
                    "-m",
                    "venv",
                    "--system-site-packages",
                    str(venv),
                ],
                env=self.control_environment(),
            )
            self.runner.run(
                [
                    base_identity["resolved_path"],
                    "-I",
                    "-m",
                    "pip",
                    "--isolated",
                    "--python",
                    str(venv / "bin" / "python"),
                    "install",
                    "--no-index",
                    "--require-hashes",
                    "--ignore-installed",
                    "--no-cache-dir",
                    "--only-binary=:all:",
                    "--find-links",
                    str(cache),
                    "-r",
                    str(lock_path),
                ],
                env=self.control_environment(),
            )
            self.runner.run(
                [str(venv / "bin" / "python"), "-I", "-m", "pip", "check"],
                env=self.control_environment(),
            )
            try:
                shared_inspect_base_python_identity(
                    Path(base_python_value),
                    expected_identity=base_identity["identity_sha256"],
                    environment={"PATH": SAFE_PATH, "HOME": os.environ.get("HOME", "")},
                )
            except WorkerSlotError as exc:
                raise PullDeployError(
                    f"Worker base Python changed during slot preparation: {exc}"
                ) from exc
            if not (venv / "bin" / "python").is_file():
                raise PullDeployError("prepared Worker venv has no Python executable")
            record = {
                "schema_version": SLOT_RECORD_SCHEMA_VERSION,
                "component": "monomer-md",
                "status": "ready",
                "slot": slot,
                "source_sha": target_sha,
                "source_tree": target_tree,
                "worker_lock_sha256": worker_lock_sha,
                "requirements_sha256": requirements_sha,
                "wheel_cache_key": wheel_cache_key,
                "wheel_inventory_sha256": wheel_inventory,
                "venv_prefix": str(venv.resolve(strict=True)),
                "venv_inventory_sha256": worker_directory_inventory_digest(venv),
                "base_python_configured_path": base_python_value,
                "base_python_identity_sha256": base_identity["identity_sha256"],
                "prepared_operation_id": operation_id,
                "prepared_at": utc_now(),
            }
            validate_slot_record(record, slot)
            record_path = self.slots_state_dir / f"md-{slot}.json"
            atomic_json(record_path, record)
            (slot_root / ".preparing.json").unlink()
            fsync_directory(slot_root)
            return record
        except BaseException:
            # The owner record deliberately remains for same-operation retry.
            raise

    def _source_evidence(
        self, target_sha: str
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], bytes]:
        release_input_payload = self._git_show(target_sha, "release-input.json")
        migration_payload = self._git_show(
            target_sha, "backend/migrations/postgres/manifest.json"
        )
        lock_payload = self._git_show(
            target_sha, "workers/monomer_md_worker/requirements.lock"
        )
        try:
            release_input = json.loads(release_input_payload)
            migrations = json.loads(migration_payload)
        except json.JSONDecodeError as exc:
            raise PullDeployError(
                "target release input or migration manifest is invalid JSON"
            ) from exc
        if not isinstance(migrations, dict):
            raise PullDeployError(
                "target release input or migration manifest is invalid"
            )
        release_input = validate_release_input(release_input)
        compose_paths = ("docker-compose.yml", "docker-compose.prod.yml")
        compose_files = {
            path: sha256_bytes(self._git_show(target_sha, path))
            for path in compose_paths
        }
        return (
            {
                "sha256": sha256_bytes(release_input_payload),
                **release_input,
            },
            {
                "sha256": sha256_bytes(migration_payload),
                "schema_version": migrations.get("schema_version"),
                "records": migrations.get("migrations"),
            },
            {"sha256": canonical_json_digest(compose_files), "files": compose_files},
            lock_payload,
        )

    def plan(self, *, target_sha: str, operation_id: str) -> dict[str, Any]:
        self.ensure_roots(mutating=False)
        target_sha = require_sha(target_sha, "target SHA")
        operation_id = require_operation_id(operation_id)
        self._require_no_contract_maintenance(require_alias_completed=False)
        if self.marker_path.exists() or self.marker_path.is_symlink():
            raise PullDeployError(
                "an interrupted deployment must be recovered before planning"
            )
        self._assert_operation_not_terminal(operation_id, action="plan")
        production_config = self.production_config_evidence(check_free_space=True)
        # Read-only credential gates: plan proves that the dedicated deploy
        # identity, pinned host key, GHCR credential and GitHub status token
        # are provisioned without exposing their contents.
        self._github_token()
        helpers = self.stable_helper_evidence()
        active_control = self.active_control_evidence()
        current_state: dict[str, Any] | None = None
        if self.current_state_path.exists() or self.current_state_path.is_symlink():
            current_state = self._validate_steady_deployment_state(
                load_private_json(self.current_state_path)
            )
        active = self._active_slot()
        if (
            current_state is not None
            and active != current_state["active_monomer_md_slot"]
        ):
            raise PullDeployError(
                "active Worker slot differs from current deployment state"
            )
        if current_state is None and active is not None:
            raise PullDeployError(
                "active Worker slot exists without a current deployment state"
            )
        if (
            current_state is not None
            and active_control != current_state["active_control"]
        ):
            raise PullDeployError(
                "active control authority differs from current deployment state"
            )
        current = self.repository_identity()
        remote = self.remote_main()
        if remote != target_sha:
            raise PullDeployError("requested target is not current remote main")
        return {
            "action": "plan",
            "apply": False,
            "operation_id": operation_id,
            "production_root": str(self.production_root),
            "runtime_root": str(self.runtime_root),
            "previous_sha": current["sha"],
            "previous_tree": current["tree"],
            "source_trust": current.get("trust"),
            "target_sha": target_sha,
            "remote_main": remote,
            "deploy_origin_ready": current["origin"] == REPOSITORY_SSH_URL,
            "production_config": production_config,
            "stable_helpers": helpers,
            "active_control": active_control,
            "service_mutation": False,
            "ignored_runtime_entries": self.ignored_runtime_entries(),
        }

    def completed_legacy_takeover_evidence(
        self,
        *,
        authority_sha: str,
        authority_tree: str,
        expected_repository: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Re-prove the installed exact takeover and Bootstrap binding."""

        try:
            bootstrap = _control_runtime._validate_bootstrap_authority(
                self.runtime_root
            )
        except Exception as exc:
            raise PullDeployError(
                "completed Bootstrap authority is unavailable"
            ) from exc
        sealed = validate_legacy_takeover_binding(
            bootstrap.get("legacy_takeover")
        )
        if (
            bootstrap.get("source_sha") != authority_sha
            or bootstrap.get("source_tree") != authority_tree
            or sealed["authority_sha"] != authority_sha
            or sealed["authority_tree"] != authority_tree
        ):
            raise PullDeployError(
                "Bootstrap and legacy takeover are not the exact F authority"
            )
        if expected_repository is not None and (
            sealed["git_identity"]["head_sha"]
            != expected_repository.get("sha")
            or sealed["git_identity"]["head_tree"]
            != expected_repository.get("tree")
        ):
            raise PullDeployError(
                "legacy takeover belongs to another production Git identity"
            )
        try:
            current = _legacy_takeover_evidence.validate_completed(
                self.runtime_root,
                sealed["operation_id"],
                authority_sha,
                authority_tree,
                expected_git_identity=sealed["git_identity"],
            )
        except Exception as exc:
            raise PullDeployError(
                "legacy takeover installation or completed evidence changed"
            ) from exc
        validated = validate_legacy_takeover_binding(current)
        if validated != sealed:
            raise PullDeployError(
                "legacy takeover differs from Bootstrap authority"
            )
        return validated

    def maintenance_prefetch_evidence(
        self,
        operation_id: str,
        *,
        authority_sha: str,
        authority_tree: str | None,
        target_sha: str | None = None,
        target_tree: str | None = None,
        policy_sha256: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Validate and compact one complete local-only prefetch authority."""

        operation_id = str(operation_id)
        ready_path = (
            self.runtime_root / "prefetch" / operation_id / "ready.json"
        )
        try:
            ready = _prefetch_evidence.validate_ready_evidence(
                load_private_json(ready_path),
                runtime_root=self.runtime_root,
            )
        except Exception as exc:
            raise PullDeployError(
                "complete maintenance prefetch evidence is required"
            ) from exc
        source = ready["source"]
        if (
            source["authority"]["sha"] != authority_sha
            or authority_tree is not None
            and source["authority"]["tree"] != authority_tree
            or target_sha is not None
            and source["target"]["sha"] != target_sha
            or target_tree is not None
            and source["target"]["tree"] != target_tree
            or policy_sha256 is not None
            and ready["policy_sha256"] != policy_sha256
        ):
            raise PullDeployError(
                "maintenance prefetch belongs to another F/B policy"
            )
        binding: dict[str, Any] = {
            "schema_version": 2,
            "operation_id": operation_id,
            "ready_path": str(ready_path),
            "ready_sha256": sha256_file(ready_path),
            "identity_sha256": ready["identity_sha256"],
            "source": source,
            "source_readiness_sha256": ready[
                "source_readiness_sha256"
            ],
            "controller_sha256": canonical_json_digest(
                ready["controller"]
            ),
            "policy_sha256": ready["policy_sha256"],
            "docker_config_path": ready["docker_config"]["path"],
            "git_bundle_sha256": ready["git_bundle"]["sha256"],
            "images_sha256": canonical_json_digest(ready["images"]),
            "wheel_caches_sha256": canonical_json_digest(
                ready["wheel_caches"]
            ),
            "asset_manifest_sha256": ready["asset"]["manifest_sha256"],
            "asset_inventory_sha256": ready["asset"]["inventory_sha256"],
            "asset_contract_sha256": ready["asset"]["identity_sha256"],
            "asset_builder_proof_sha256": ready["asset"][
                "builder_proof"
            ]["proof_sha256"],
            "asset_predecessor_inventory_sha256": ready["asset"][
                "predecessor_inventory_sha256"
            ],
            "live_asset_pointer_sha256": canonical_json_digest(
                ready["asset"]["live_pointer_end"]
            ),
            "recovery_tools_sha256": canonical_json_digest(
                ready["recovery_tools"]
            ),
            "created_at": ready["created_at"],
        }
        binding["binding_sha256"] = canonical_json_digest(binding)
        return ready, validate_prefetch_binding(binding)

    def _revalidate_bridge_external_authorities(
        self,
        descriptor: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        bridge = descriptor["bridge"]
        takeover = self.completed_legacy_takeover_evidence(
            authority_sha=bridge["authority"]["sha"],
            authority_tree=bridge["authority"]["tree"],
            expected_repository={
                "sha": descriptor["repository"]["previous_sha"],
                "tree": descriptor["repository"]["previous_tree"],
            },
        )
        ready, prefetch = self.maintenance_prefetch_evidence(
            descriptor["prefetch"]["operation_id"],
            authority_sha=bridge["authority"]["sha"],
            authority_tree=bridge["authority"]["tree"],
            target_sha=bridge["target"]["sha"],
            target_tree=bridge["target"]["tree"],
            policy_sha256=bridge["policy_sha256"],
        )
        if (
            takeover != descriptor["legacy_takeover"]
            or prefetch != descriptor["prefetch"]
        ):
            raise PullDeployError(
                "takeover or maintenance prefetch authority changed"
            )
        return ready, takeover

    def bootstrap_ci_evidence(
        self,
        *,
        authority_sha: str,
        required_jobs: Iterable[str],
    ) -> dict[str, Any]:
        """Consume CI evidence sealed by Bootstrap before legacy takeover."""

        try:
            bootstrap = _control_runtime._validate_bootstrap_authority(
                self.runtime_root
            )
        except Exception as exc:
            raise PullDeployError(
                "Bootstrap CI authority is unavailable"
            ) from exc
        delivery = bootstrap.get("delivery_gate")
        ci = delivery.get("ci") if isinstance(delivery, dict) else None
        required = set(required_jobs)
        if (
            not isinstance(ci, dict)
            or ci.get("head_sha") != authority_sha
            or ci.get("head_branch") != "main"
            or ci.get("event") != "push"
            or ci.get("path") != ".github/workflows/ci.yml"
            or ci.get("conclusion") != "success"
            or set(ci.get("required_jobs", [])) != required
        ):
            raise PullDeployError(
                "Bootstrap did not seal the exact required F CI jobs"
            )
        return dict(ci)

    def prefetched_application_images(
        self,
        ready: dict[str, Any],
        *,
        target_sha: str,
    ) -> dict[str, dict[str, str]]:
        """Project local prefetch records into Pull image evidence."""

        records: dict[str, dict[str, str]] = {}
        for role, root in (
            ("backend", BACKEND_TAG_ROOT),
            ("web", WEB_TAG_ROOT),
        ):
            prefetched = ready["images"]["target"][role]
            records[role] = {
                "tag": f"{root}:sha-{target_sha}",
                "digest_ref": prefetched["digest_ref"],
                "image_id": prefetched["local_image_id"],
                "revision": target_sha,
                "source": SOURCE_URL,
                "version": f"sha-{target_sha}",
            }
        validate_image_records(records, source_sha=target_sha)
        self._revalidate_materialized_images(
            records,
            source_sha=target_sha,
            pull=False,
        )
        return records

    def prefetched_postgres_restore_image(
        self,
        ready: dict[str, Any],
    ) -> dict[str, str]:
        images = ready.get("images")
        if not isinstance(images, dict):
            raise PullDeployError(
                "prefetched PostgreSQL image authority is malformed"
            )
        postgres_audit = images.get("postgres_audit")
        postgres_restore = images.get("postgres_restore")
        expected_audit = _prefetch_evidence.POSTGRES_AUDIT_IMAGES
        if (
            not isinstance(postgres_audit, dict)
            or set(postgres_audit) != set(expected_audit)
        ):
            raise PullDeployError(
                "prefetched PostgreSQL audit image set is incomplete"
            )
        try:
            validated_audit = {
                major: _prefetch_evidence.validate_image_evidence(
                    postgres_audit[major],
                    expected_reference=reference,
                    expected_revision=None,
                    enforce_revision=False,
                )
                for major, reference in sorted(expected_audit.items())
            }
            record = _prefetch_evidence.validate_image_evidence(
                postgres_restore,
                expected_reference=POSTGRES16_IMAGE,
                expected_revision=None,
                enforce_revision=False,
            )
        except Exception as exc:
            raise PullDeployError(
                "prefetched PostgreSQL image authority differs"
            ) from exc
        if (
            record != validated_audit["16"]
            or record["digest_ref"] != POSTGRES16_IMAGE
        ):
            raise PullDeployError(
                "prefetched PostgreSQL restore image is not the exact "
                "PostgreSQL 16 audit image"
            )
        result = self.runner.run(
            ["docker", "image", "inspect", POSTGRES16_IMAGE],
            env=self.control_environment(),
        )
        try:
            values = json.loads(str(result.stdout))
            image = values[0]
            labels = image.get("Config", {}).get("Labels") or {}
            current = {
                "digest_ref": POSTGRES16_IMAGE,
                "oci_reference_digest": POSTGRES16_IMAGE.split("@", 1)[1],
                "local_image_id": image.get("Id"),
                "repo_digests": sorted(
                    set(image.get("RepoDigests") or [])
                ),
                "revision": labels.get(
                    "org.opencontainers.image.revision"
                ),
                "source": labels.get("org.opencontainers.image.source"),
                "version": labels.get("org.opencontainers.image.version"),
            }
            current = _prefetch_evidence.validate_image_evidence(
                current,
                expected_reference=POSTGRES16_IMAGE,
                expected_revision=None,
                enforce_revision=False,
            )
        except Exception as exc:
            raise PullDeployError(
                "prefetched PostgreSQL image is unavailable"
            ) from exc
        if len(values) != 1 or current != record:
            raise PullDeployError(
                "prefetched PostgreSQL image material changed"
            )
        return {
            "digest_ref": POSTGRES16_IMAGE,
            "image_id": record["local_image_id"],
        }

    def bridge_plan(
        self,
        *,
        authority_sha: str,
        operation_id: str,
        prefetch_operation_id: str,
    ) -> dict[str, Any]:
        """Read-only plan for F-authorized deployment of the exact ancestor B."""

        self.ensure_roots(mutating=False)
        authority_sha = require_sha(authority_sha, "bridge authority SHA")
        operation_id = require_operation_id(operation_id)
        if self.remote_main() != authority_sha:
            raise PullDeployError(
                "bridge authority is no longer current remote main"
            )
        self._require_no_contract_maintenance(require_alias_completed=False)
        if self.marker_path.exists() or self.marker_path.is_symlink():
            raise PullDeployError(
                "an interrupted deployment must be recovered before planning"
            )
        self._assert_operation_not_terminal(operation_id, action="bridge-plan")
        if self.current_state_path.exists() or self.current_state_path.is_symlink():
            raise PullDeployError(
                "historical bridge is restricted to the first governed takeover"
            )
        if self._active_slot() is not None:
            raise PullDeployError(
                "active Worker slot exists before first governed takeover"
            )
        production_config = self.production_config_evidence(check_free_space=True)
        helpers = self.stable_helper_evidence()
        active_control = self.active_control_evidence()
        ready, prefetch = self.maintenance_prefetch_evidence(
            prefetch_operation_id,
            authority_sha=authority_sha,
            authority_tree=None,
        )
        authority_tree = ready["source"]["authority"]["tree"]
        target_sha = ready["source"]["target"]["sha"]
        target_tree = ready["source"]["target"]["tree"]
        policy = ready["policy"]
        if (
            policy["target_sha"] != target_sha
            or policy["target_tree"] != target_tree
        ):
            raise PullDeployError(
                "maintenance prefetch target differs from F policy"
            )
        current = self.repository_identity(require_ssh_origin=True)
        takeover = self.completed_legacy_takeover_evidence(
            authority_sha=authority_sha,
            authority_tree=authority_tree,
            expected_repository=current,
        )
        if (
            active_control["source_sha"] != authority_sha
            or active_control["source_tree"] != authority_tree
        ):
            raise PullDeployError(
                "active bootstrap controls are not the bridge authority"
            )
        if self.remote_main() != authority_sha:
            raise PullDeployError(
                "bridge authority changed while planning"
            )
        return {
            "action": "bridge-plan",
            "apply": False,
            "operation_id": operation_id,
            "production_root": str(self.production_root),
            "runtime_root": str(self.runtime_root),
            "authority_sha": authority_sha,
            "authority_tree": authority_tree,
            "target_sha": target_sha,
            "target_tree": target_tree,
            "target_ref": policy["target_ref"],
            "policy_id": policy["policy_id"],
            "prefetch": prefetch,
            "legacy_takeover": takeover,
            "previous_sha": current["sha"],
            "previous_tree": current["tree"],
            "source_trust": current.get("trust"),
            "deploy_origin_ready": current["origin"] == REPOSITORY_SSH_URL,
            "production_config": production_config,
            "stable_helpers": helpers,
            "active_control": active_control,
            "service_mutation": False,
            "ignored_runtime_entries": self.ignored_runtime_entries(),
        }

    def _reserve_current_bridge_token(
        self,
        *,
        authority_sha: str,
        operation_id: str,
        policy_id: str,
    ) -> dict[str, Any]:
        """Reserve a token only while exact F is still protected main."""

        self._require_deploy_lock_for_staging()
        authority_sha = require_sha(
            authority_sha, "bridge authority SHA"
        )
        if self.remote_main() != authority_sha:
            raise PullDeployError(
                "bridge authority changed before descriptor publication"
            )
        predecessor_retirement = self._bridge_token_successor_authority()
        try:
            return _bridge_core.reserve_token(
                self.state_dir,
                operation_id=operation_id,
                policy_id=policy_id,
                predecessor_retirement=predecessor_retirement,
            )
        except Exception as exc:
            raise PullDeployError(
                "cannot reserve exact bridge takeover authority"
            ) from exc

    def _plan_current_bridge_token(
        self,
        *,
        authority_sha: str,
        operation_id: str,
        policy_id: str,
    ) -> dict[str, Any]:
        """Plan random token identity without publishing an unbound record."""

        self._require_deploy_lock_for_staging()
        authority_sha = require_sha(
            authority_sha, "bridge authority SHA"
        )
        if self.bridge_token_path.exists() or self.bridge_token_path.is_symlink():
            try:
                existing = _bridge_core.load_token_authority(
                    self.state_dir
                )
            except Exception as exc:
                raise PullDeployError(
                    "bridge token authority is invalid"
                ) from exc
            if (
                existing["operation_id"] == operation_id
                and existing["policy_id"] == policy_id
                and existing["status"] == "reserved"
            ):
                # Compatibility with a response-lost reservation produced by
                # an older controller.  The descriptor-first path will bind it
                # exactly and never creates a new reserved generation.
                return {
                    "token_id": existing["token_id"],
                    "token_sha256": existing["token_sha256"],
                    "prepared_at": existing["prepared_at"],
                }
            if existing["status"] != "retired-precommit":
                raise PullDeployError(
                    "bridge token exists without a recoverable descriptor"
                )
            self._bridge_token_successor_authority()
        if self.remote_main() != authority_sha:
            raise PullDeployError(
                "bridge authority changed before descriptor publication"
            )
        try:
            identity = _bridge_core.token_identity(os.urandom(32))
        except Exception as exc:
            raise PullDeployError(
                "cannot generate bridge token identity"
            ) from exc
        return {
            **identity,
            "prepared_at": _bridge_core.utc_now(),
        }

    def _bind_bridge_descriptor_token(
        self,
        descriptor: Mapping[str, Any],
        descriptor_path: Path,
    ) -> dict[str, Any]:
        """Bind or re-prove the token before accepting bridge READY."""

        self._require_deploy_lock_for_staging()
        if descriptor.get("schema_version") != (
            BRIDGE_DESCRIPTOR_SCHEMA_VERSION
        ):
            raise PullDeployError(
                "ordinary descriptor cannot bind bridge authority"
            )
        bridge = descriptor.get("bridge")
        if not isinstance(bridge, dict):
            raise PullDeployError(
                "bridge descriptor lacks token authority"
            )
        existing_token: dict[str, Any] | None = None
        if (
            self.bridge_token_path.exists()
            or self.bridge_token_path.is_symlink()
        ):
            try:
                existing_token = _bridge_core.load_token_authority(
                    self.state_dir
                )
            except Exception as exc:
                raise PullDeployError(
                    "bridge token authority is invalid"
                ) from exc
        replaying_existing = (
            existing_token is not None
            and existing_token["operation_id"] == descriptor["operation_id"]
            and existing_token["policy_id"]
            == bridge["policy"]["policy_id"]
            and existing_token["status"] in {"reserved", "prepared"}
            and existing_token["token_id"]
            == bridge["token"]["token_id"]
            and existing_token["token_sha256"]
            == bridge["token"]["token_sha256"]
        )
        if not replaying_existing and (
            self.remote_main() != bridge["authority"]["sha"]
        ):
            raise PullDeployError(
                "bridge authority changed before prepared token publication"
            )
        try:
            bound_token = _bridge_core.publish_prepared_token(
                self.state_dir,
                operation_id=descriptor["operation_id"],
                policy_id=bridge["policy"]["policy_id"],
                descriptor_sha256=sha256_file(descriptor_path),
                token_id=bridge["token"]["token_id"],
                token_sha256=bridge["token"]["token_sha256"],
                prepared_at=descriptor["prepared_at"],
                predecessor_retirement=(
                    self._bridge_token_successor_authority()
                ),
            )
        except Exception as exc:
            raise PullDeployError(
                "bridge token could not bind descriptor"
            ) from exc
        if (
            bound_token["token_id"] != bridge["token"]["token_id"]
            or bound_token["token_sha256"]
            != bridge["token"]["token_sha256"]
        ):
            raise PullDeployError(
                "bridge descriptor token identity changed"
            )
        return bound_token

    def _operation_paths(self, operation_id: str) -> tuple[Path, Path, Path]:
        operation = self.prepared_root / require_operation_id(operation_id)
        return operation, operation / "descriptor.json", operation / "ready.json"

    def _assert_existing_prepare_command_mode(
        self,
        *,
        operation_id: str,
        bridge_requested: bool,
    ) -> None:
        """Reject an existing opposite-mode descriptor before any mutation.

        This fence deliberately reads the raw schema before a handoff fetch,
        target-ref materialization, control-release publication, token change,
        or prepare-attempt write.  The normal descriptor validators repeat the
        check after those read-only decisions to catch a same-UID filesystem
        race.
        """

        _operation, descriptor_path, _ready_path = self._operation_paths(
            operation_id
        )
        if not (descriptor_path.exists() or descriptor_path.is_symlink()):
            return
        raw_descriptor = load_private_json(descriptor_path)
        schema_version = raw_descriptor.get("schema_version")
        if schema_version not in {
            DESCRIPTOR_SCHEMA_VERSION,
            BRIDGE_DESCRIPTOR_SCHEMA_VERSION,
        }:
            raise PullDeployError(
                "existing prepare descriptor schema is unsupported"
            )
        descriptor_is_bridge = (
            schema_version == BRIDGE_DESCRIPTOR_SCHEMA_VERSION
        )
        if descriptor_is_bridge != bridge_requested:
            raise PullDeployError(
                "prepared operation requires its original ordinary or bridge "
                "command mode"
            )

    def prepared_alias_bridge_authority(
        self,
        *,
        control: Mapping[str, Any],
        legacy_source: Mapping[str, str],
        external_database_transition: object | None = None,
        allow_external_transition_pending: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Re-prove the single READY F -> exact-B bridge before alias CAS."""

        ensure_private_directory(self.state_dir)
        ensure_private_directory(self.prepared_root)
        candidates: list[
            tuple[dict[str, Any], Path, Path]
        ] = []
        for operation in self.prepared_root.iterdir():
            ensure_private_directory(operation)
            ready_path = operation / "ready.json"
            if not (ready_path.exists() or ready_path.is_symlink()):
                continue
            descriptor_path = operation / "descriptor.json"
            descriptor = validate_descriptor(
                load_private_json(descriptor_path)
            )
            if operation.name != descriptor["operation_id"]:
                raise PullDeployError(
                    "prepared bridge directory differs from its operation"
                )
            ready = load_private_json(ready_path)
            self._validate_ready(ready, descriptor, descriptor_path)
            if (
                descriptor["schema_version"]
                == BRIDGE_DESCRIPTOR_SCHEMA_VERSION
            ):
                candidates.append(
                    (descriptor, descriptor_path, ready_path)
                )
        if len(candidates) != 1:
            raise PullDeployError(
                "exactly one READY bridge descriptor is required for alias "
                "maintenance"
            )
        descriptor, descriptor_path, ready_path = candidates[0]
        bridge = descriptor["bridge"]
        previous_control = descriptor["controller"][
            "previous_active_control"
        ]
        if (
            previous_control["release_id"] != control.get("release_id")
            or previous_control["source_sha"] != control.get("source_sha")
            or previous_control["source_tree"] != control.get("source_tree")
            or previous_control["manifest_sha256"]
            != "sha256:" + str(control.get("manifest_sha256"))
            or bridge["authority"]["control_release_id"]
            != control.get("release_id")
            or bridge["authority"]["sha"] != control.get("source_sha")
            or bridge["authority"]["tree"] != control.get("source_tree")
            or descriptor["repository"]["previous_sha"]
            != legacy_source.get("sha")
            or descriptor["repository"]["previous_tree"]
            != legacy_source.get("tree")
        ):
            raise PullDeployError(
                "alias execution does not match prepared F/legacy authority"
            )
        try:
            token = _bridge_core.load_token_authority(self.state_dir)
        except Exception as exc:
            raise PullDeployError(
                "prepared bridge token authority is unavailable"
            ) from exc
        descriptor_sha256 = sha256_file(descriptor_path)
        allowed_token_statuses = (
            {"prepared", "commit-intent", "consumed"}
            if external_database_transition is not None
            else {"prepared"}
        )
        if (
            token["status"] not in allowed_token_statuses
            or token["operation_id"] != descriptor["operation_id"]
            or token["policy_id"] != bridge["policy"]["policy_id"]
            or token["descriptor_sha256"] != descriptor_sha256
            or token["token_id"] != bridge["token"]["token_id"]
            or token["token_sha256"] != bridge["token"]["token_sha256"]
        ):
            raise PullDeployError(
                "alias maintenance requires the exact prepared bridge token"
            )
        self._revalidate_bridge_external_authorities(descriptor)
        if external_database_transition is not None:
            self.revalidate_alias_external_database_transition(
                descriptor,
                external_database_transition,
            )
        elif not allow_external_transition_pending:
            self._revalidate_external_database_audit(descriptor)
        takeover = descriptor["legacy_takeover"]
        try:
            status = _legacy_takeover_evidence.load_status(
                self.runtime_root,
                takeover["operation_id"],
            )
        except Exception as exc:
            raise PullDeployError(
                "legacy takeover stopped-runtime evidence is unavailable"
            ) from exc
        fence = status.get("pre_stopped_fence")
        runtime_fence = (
            fence.get("runtime_fence")
            if isinstance(fence, dict)
            else None
        )
        if (
            status.get("apply_phase") != "complete"
            or status.get("restore_phase") is not None
            or status.get("active") is not False
            or status.get("pre_stopped_fence_sha256")
            != takeover["pre_stopped_fence_sha256"]
            or not isinstance(runtime_fence, dict)
            or runtime_fence.get("readers_stopped") is not True
            or runtime_fence.get("postgres_running_untouched") is not True
        ):
            raise PullDeployError(
                "legacy takeover is not at its exact stopped-reader fence"
            )
        authority = alias_bridge_authority_projection(
            descriptor,
            descriptor_path=descriptor_path,
            ready_path=ready_path,
        )
        return authority, dict(runtime_fence)

    def _validate_database_backup(
        self,
        descriptor: dict[str, Any],
        backup: object,
        *,
        require_operation_backup: bool,
    ) -> dict[str, Any]:
        if (
            not isinstance(backup, dict)
            or set(backup)
            != {
                "path",
                "sha256",
                "restore_verification",
                "mutable_data_before_sha256",
            }
            or not isinstance(backup.get("path"), str)
            or not isinstance(backup.get("restore_verification"), dict)
        ):
            raise PullDeployError("database backup evidence has an invalid shape")
        digest = require_digest(backup.get("sha256"), "database backup digest")
        require_digest(
            backup.get("mutable_data_before_sha256"),
            "database backup mutable-data identity",
        )
        path = Path(backup["path"])
        if not path.is_absolute():
            raise PullDeployError("database backup path must be absolute")
        expected_parent = self.backups_dir / descriptor["operation_id"]
        if require_operation_backup and path.parent != expected_parent:
            raise PullDeployError(
                "database backup does not belong to the deployment operation"
            )
        try:
            metadata = path.lstat()
            parent_metadata = path.parent.lstat()
        except OSError as exc:
            raise PullDeployError("database backup is unavailable") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or not stat.S_ISDIR(parent_metadata.st_mode)
            or path.parent.is_symlink()
            or parent_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(parent_metadata.st_mode) != 0o700
            or sha256_file(path) != digest
        ):
            raise PullDeployError(
                "database backup identity differs from sealed evidence"
            )
        restore = backup["restore_verification"]
        if (
            restore.get("schema_version") != 1
            or restore.get("restored") is not True
            or restore.get("postgres_major") != 16
            or restore.get("image") != POSTGRES16_IMAGE
            or restore.get("dump_sha256") != digest
            or not isinstance(restore.get("ledger"), list)
            or restore.get("source_mutable_data_identity_sha256")
            != backup["mutable_data_before_sha256"]
        ):
            raise PullDeployError("database backup restore evidence is invalid")
        canonical_ledger_history(
            [
                {"version": item.get("version"), "checksum": item.get("checksum")}
                for item in restore["ledger"]
                if isinstance(item, dict)
            ],
            descriptor["migrations"].get("records"),
            accepted_ledgers=descriptor_accepted_ledgers(descriptor),
        )
        return dict(backup)

    def _prepare_abort_journal_path(self, operation_id: str) -> Path:
        return self.prepare_aborts_dir / (
            require_operation_id(operation_id) + ".json"
        )

    @staticmethod
    def _validate_prepare_owner_document(
        document: object,
        operation_id: str,
    ) -> dict[str, Any]:
        if (
            not isinstance(document, dict)
            or set(document) != PREPARE_OWNER_FIELDS
            or document.get("schema_version") != 1
            or document.get("operation_id") != operation_id
            or not isinstance(document.get("created_at"), str)
            or not document["created_at"]
        ):
            raise PullDeployError("prepare operation owner is invalid")
        require_sha(document.get("target_sha"), "prepare owner target SHA")
        require_digest(
            document.get("controller_sha256"),
            "prepare owner controller digest",
        )
        return dict(document)

    def _ensure_prepare_abort_roots(self) -> None:
        ensure_private_directory(self.state_dir)
        for path in (
            self.prepare_aborts_dir,
            self.prepare_abort_archives_dir,
        ):
            existed = path.exists() or path.is_symlink()
            ensure_private_directory(path, create=True)
            if not existed:
                fsync_directory(path.parent)

    def _optional_private_json_sha256(self, path: Path) -> str | None:
        if not (path.exists() or path.is_symlink()):
            return None
        load_private_json(path)
        return sha256_file(path)

    def _prepare_abort_handoff_evidence(
        self,
        *,
        operation_id: str,
        owner: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Bind a target-controller owner to its active-controller handoff."""

        handoff_path = self.control_handoffs_dir / f"{operation_id}.json"
        if not (handoff_path.exists() or handoff_path.is_symlink()):
            if (
                not self.test_root_mode
                or owner["controller_sha256"] != self.controller_digest()
            ):
                raise PullDeployError(
                    "prepare owner lacks its selector-sealed control handoff"
                )
            return {
                "control_handoff_sha256": None,
                "control_handoff_schema_version": None,
                "executor_control_sha256": None,
                "target_tree": None,
            }
        record = load_private_json(handoff_path)
        schema_version = record.get("schema_version")
        ordinary_fields = {
            "schema_version",
            "protocol_version",
            "operation_id",
            "target_sha",
            "target_tree",
            "previous_active_control",
            "previous_active_control_sha256",
            "executor_control",
            "executor_control_sha256",
            "created_at",
        }
        bridge_fields = ordinary_fields | {
            "authority_sha",
            "authority_tree",
            "target_ref",
            "policy_id",
            "policy_sha256",
            "prefetch_operation_id",
            "prefetch",
            "legacy_takeover",
        }
        expected_fields = (
            ordinary_fields
            if schema_version == 1
            else bridge_fields
            if schema_version == 2
            else None
        )
        if (
            expected_fields is None
            or set(record) != expected_fields
            or record.get("protocol_version")
            != _control_runtime.PROTOCOL_VERSION
            or record.get("operation_id") != operation_id
            or record.get("target_sha") != owner["target_sha"]
            or not isinstance(record.get("created_at"), str)
            or not record["created_at"]
            or canonical_json_digest(record.get("executor_control"))
            != record.get("executor_control_sha256")
            or canonical_json_digest(record.get("previous_active_control"))
            != record.get("previous_active_control_sha256")
        ):
            raise PullDeployError("prepare control handoff identity is invalid")
        target_tree = require_sha(
            record.get("target_tree"), "prepare handoff target tree"
        )
        if schema_version == 2:
            require_sha(record.get("authority_sha"), "bridge handoff authority SHA")
            require_sha(
                record.get("authority_tree"),
                "bridge handoff authority tree",
            )
            require_digest(record.get("policy_id"), "bridge handoff policy ID")
            require_digest(
                record.get("policy_sha256"),
                "bridge handoff policy digest",
            )
            require_operation_id(str(record.get("prefetch_operation_id", "")))
            validate_prefetch_binding(record.get("prefetch"))
            validate_legacy_takeover_binding(record.get("legacy_takeover"))
        try:
            candidate, manifest, release_root = (
                _control_runtime.load_candidate_control(
                    self.runtime_root,
                    record["executor_control"],
                )
            )
        except Exception as exc:
            raise PullDeployError(
                "prepare control handoff candidate is unavailable"
            ) from exc
        deploy_entrypoint = manifest["entrypoints"].get("deploy")
        if (
            candidate.get("operation_id") != operation_id
            or candidate.get("source_sha") != owner["target_sha"]
            or candidate.get("source_tree") != target_tree
            or not isinstance(deploy_entrypoint, dict)
            or deploy_entrypoint.get("kind") != "python"
            or not isinstance(deploy_entrypoint.get("file"), str)
        ):
            raise PullDeployError(
                "prepare handoff candidate differs from its operation"
            )
        controller_name = deploy_entrypoint["file"]
        controller_record = manifest["files"].get(controller_name)
        controller_path = release_root / controller_name
        if (
            not isinstance(controller_record, dict)
            or controller_record.get("sha256")
            != owner["controller_sha256"]
            or sha256_file(controller_path) != owner["controller_sha256"]
        ):
            raise PullDeployError(
                "prepare owner controller differs from sealed handoff controls"
            )
        if self.active_control_evidence() != record["previous_active_control"]:
            raise PullDeployError(
                "active controls changed from the prepare handoff authority"
            )
        return {
            "control_handoff_sha256": sha256_file(handoff_path),
            "control_handoff_schema_version": schema_version,
            "executor_control_sha256": record["executor_control_sha256"],
            "target_tree": target_tree,
        }

    def _capture_prepare_abort_fences(self) -> dict[str, Any]:
        active_slot = self._active_slot()
        active_slot_sha256 = (
            sha256_file(self.active_slot_path)
            if active_slot is not None
            else None
        )
        active_control = self.active_control_evidence()
        token: dict[str, Any] | None = None
        token_sha256: str | None = None
        if self.bridge_token_path.exists() or self.bridge_token_path.is_symlink():
            try:
                token = _bridge_core.load_token_authority(self.state_dir)
            except Exception as exc:
                raise PullDeployError(
                    "bridge token authority is invalid during prepare abort"
                ) from exc
            token_sha256 = sha256_file(self.bridge_token_path)
        return {
            "current_state_sha256": self._optional_private_json_sha256(
                self.current_state_path
            ),
            "active_control_sha256": sha256_file(self.active_control_path),
            "active_slot_sha256": active_slot_sha256,
            "active_slot": active_slot,
            "bridge_token_sha256": token_sha256,
            "bridge_token_operation_id": (
                token["operation_id"] if token is not None else None
            ),
            "bridge_token_status": (
                token["status"] if token is not None else None
            ),
        }

    def _assert_prepare_abort_fences(
        self,
        journal: Mapping[str, Any],
    ) -> None:
        observed = self._capture_prepare_abort_fences()
        for field in (
            "current_state_sha256",
            "active_control_sha256",
            "active_slot_sha256",
            "active_slot",
            "bridge_token_sha256",
            "bridge_token_operation_id",
            "bridge_token_status",
        ):
            if observed[field] != journal[field]:
                raise PullDeployError(
                    f"prepare abort {field.replace('_', ' ')} CAS changed"
                )
        handoff_path = (
            self.control_handoffs_dir / f"{journal['operation_id']}.json"
        )
        archived_handoff_path = (
            Path(journal["archive_path"]) / "control-handoff.json"
        )
        observed_live_handoff = (
            sha256_file(handoff_path)
            if handoff_path.exists() or handoff_path.is_symlink()
            else None
        )
        observed_archived_handoff = (
            sha256_file(archived_handoff_path)
            if archived_handoff_path.exists()
            or archived_handoff_path.is_symlink()
            else None
        )
        expected_handoff = journal["control_handoff_sha256"]
        if expected_handoff is None:
            if (
                observed_live_handoff is not None
                or observed_archived_handoff is not None
            ):
                raise PullDeployError(
                    "unrecorded prepare abort control handoff appeared"
                )
        elif (
            (
                observed_live_handoff,
                observed_archived_handoff,
            ).count(expected_handoff)
            != 1
            or any(
                value not in {None, expected_handoff}
                for value in (
                    observed_live_handoff,
                    observed_archived_handoff,
                )
            )
        ):
            raise PullDeployError("prepare abort control handoff CAS changed")
        prepared_ref = journal["prepared_ref"]
        observed_ref = self._observe_prepare_abort_prepared_ref(
            prepared_ref["name"]
        )
        expected_ref = prepared_ref["target_sha"]
        ref_evidence_path = (
            Path(journal["archive_path"]) / "prepared-ref.json"
        )
        archived_ref = (
            load_private_json(ref_evidence_path)
            if ref_evidence_path.exists() or ref_evidence_path.is_symlink()
            else None
        )
        expected_ref_evidence = {
            "schema_version": 1,
            "operation_id": journal["operation_id"],
            "ref": prepared_ref["name"],
            "target_sha": expected_ref,
        }
        if expected_ref is None:
            if observed_ref is not None or archived_ref is not None:
                raise PullDeployError(
                    "unrecorded prepared Git ref appeared during abort"
                )
        elif (
            observed_ref not in {None, expected_ref}
            or (
                archived_ref is not None
                and archived_ref != expected_ref_evidence
            )
            or (observed_ref is None and archived_ref is None)
        ):
            raise PullDeployError("prepare abort prepared Git ref CAS changed")

    @staticmethod
    def _validate_prepare_abort_slot_owner(
        document: object,
        *,
        operation_id: str,
        slot: str,
    ) -> dict[str, Any]:
        expected = {
            "schema_version": 1,
            "operation_id": operation_id,
            "slot": slot,
        }
        if document != expected:
            raise PullDeployError(
                "prepare-abort Worker slot belongs to another operation"
            )
        return expected

    def _capture_prepare_abort_staging(
        self,
        *,
        operation_id: str,
        owner: Mapping[str, Any],
    ) -> dict[str, Any]:
        staging = self.prepared_root / f".{operation_id}.preparing"
        tombstone = staging.parent / f"{staging.name}.discard"
        live_present = staging.exists() or staging.is_symlink()
        tombstone_present = tombstone.exists() or tombstone.is_symlink()
        if live_present and tombstone_present:
            raise PullDeployError(
                "prepare operation staging and quarantine both exist"
            )

        def validate_tree(path: Path, *, allow_ownerless: bool) -> str:
            ensure_private_directory(path)
            owner_path = path / "prepare-owner.json"
            if owner_path.exists() or owner_path.is_symlink():
                observed = self._validate_prepare_owner_document(
                    load_private_json(owner_path),
                    operation_id,
                )
                if any(
                    observed[field] != owner[field]
                    for field in (
                        "schema_version",
                        "operation_id",
                        "target_sha",
                        "controller_sha256",
                    )
                ):
                    raise PullDeployError(
                        "prepare operation staging has different ownership"
                    )
            elif not allow_ownerless or any(path.iterdir()):
                raise PullDeployError(
                    "prepare operation staging lacks exact ownership"
                )
            return directory_inventory_digest(path)

        return {
            "live_inventory_sha256": (
                validate_tree(staging, allow_ownerless=True)
                if live_present
                else None
            ),
            "tombstone_inventory_sha256": (
                validate_tree(tombstone, allow_ownerless=True)
                if tombstone_present
                else None
            ),
        }

    def _capture_prepare_abort_wheel_staging(
        self,
        *,
        operation_id: str,
    ) -> list[dict[str, Any]]:
        """Seal only wheel-cache staging paths named for this operation."""

        if not (self.wheel_cache_dir.exists() or self.wheel_cache_dir.is_symlink()):
            return []
        ensure_private_directory(self.wheel_cache_dir)
        suffix = re.escape(operation_id)
        pattern = re.compile(
            rf"^\.(sha256:[0-9a-f]{{64}})\.staging-{suffix}(\.discard)?$"
        )
        grouped: dict[str, dict[str, Path | None]] = {}
        for path in sorted(self.wheel_cache_dir.iterdir(), key=lambda item: item.name):
            match = pattern.fullmatch(path.name)
            if match is None:
                continue
            key = require_digest(match.group(1), "Worker wheel cache key")
            kind = "tombstone" if match.group(2) else "live"
            record = grouped.setdefault(key, {"live": None, "tombstone": None})
            if record[kind] is not None:
                raise PullDeployError(
                    "duplicate prepare-abort Worker wheel staging authority"
                )
            record[kind] = path

        result: list[dict[str, Any]] = []
        expected_owner_fields = {
            "schema_version",
            "operation_id",
            "wheel_cache_key",
            "worker_lock_sha256",
            "base_python_identity_sha256",
        }

        def inventory(path: Path, key: str) -> str:
            ensure_private_directory(path)
            owner_path = path / ".owner.json"
            if owner_path.exists() or owner_path.is_symlink():
                owner = load_private_json(owner_path)
                if (
                    set(owner) != expected_owner_fields
                    or owner.get("schema_version") != 1
                    or owner.get("operation_id") != operation_id
                    or owner.get("wheel_cache_key") != key
                ):
                    raise PullDeployError(
                        "prepare-abort Worker wheel staging has different ownership"
                    )
                require_digest(
                    owner.get("worker_lock_sha256"),
                    "Worker wheel staging lock",
                )
                require_digest(
                    owner.get("base_python_identity_sha256"),
                    "Worker wheel staging Python identity",
                )
            elif any(path.iterdir()):
                raise PullDeployError(
                    "prepare-abort Worker wheel staging lacks exact ownership"
                )
            return directory_inventory_digest(path)

        for key in sorted(grouped):
            live = grouped[key]["live"]
            tombstone = grouped[key]["tombstone"]
            if live is not None and tombstone is not None:
                raise PullDeployError(
                    "Worker wheel staging and quarantine both exist"
                )
            result.append(
                {
                    "wheel_cache_key": key,
                    "live_inventory_sha256": (
                        inventory(live, key) if live is not None else None
                    ),
                    "tombstone_inventory_sha256": (
                        inventory(tombstone, key)
                        if tombstone is not None
                        else None
                    ),
                }
            )
        return result

    def _observe_prepare_abort_prepared_ref(self, ref: str) -> str | None:
        observed: str | None = None
        if not (self.test_root_mode and not self._has_complete_test_git_layout()):
            result = self._git(
                "show-ref",
                "--verify",
                "--hash",
                ref,
                check=False,
            )
            if result.returncode == 0:
                lines = [
                    line.strip()
                    for line in str(result.stdout).splitlines()
                    if line.strip()
                ]
                if len(lines) != 1:
                    raise PullDeployError(
                        "prepared Git ref returned malformed evidence"
                    )
                observed = require_sha(lines[0], "prepared Git ref")
            elif result.returncode != 1:
                raise PullDeployError("prepared Git ref cannot be inspected")
        return observed

    def _capture_prepare_abort_prepared_ref(
        self,
        *,
        operation_id: str,
        target_sha: str,
    ) -> dict[str, Any]:
        ref = f"refs/nexpoly/prepared/{operation_id}"
        observed = self._observe_prepare_abort_prepared_ref(ref)
        if observed is not None and observed != target_sha:
            raise PullDeployError(
                "prepared Git ref belongs to another target"
            )
        return {"name": ref, "target_sha": observed}

    def _capture_prepare_abort_slots(
        self,
        *,
        operation_id: str,
        target_sha: str,
        expected_target_tree: str | None,
        active_slot: Mapping[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        records: list[dict[str, Any]] = []
        target_tree = expected_target_tree
        for slot in ("a", "b"):
            root = self.venv_root / f"md-{slot}"
            record_path = self.slots_state_dir / f"md-{slot}.json"
            staging = (
                self.venv_root / f".md-{slot}.preparing-{operation_id}"
            )
            staging_tombstone = staging.parent / f"{staging.name}.discard"
            slot_tombstone = (
                self.venv_root / f".{slot}.discard-{operation_id}"
            )
            root_present = root.exists() or root.is_symlink()
            record_present = record_path.exists() or record_path.is_symlink()
            staging_present = staging.exists() or staging.is_symlink()
            staging_tombstone_present = (
                staging_tombstone.exists() or staging_tombstone.is_symlink()
            )
            slot_tombstone_present = (
                slot_tombstone.exists() or slot_tombstone.is_symlink()
            )
            if staging_present and staging_tombstone_present:
                raise PullDeployError(
                    "Worker slot staging and quarantine both exist"
                )
            root_owner: dict[str, Any] | None = None
            if root_present:
                ensure_private_directory(root)
                owner_path = root / ".preparing.json"
                if owner_path.exists() or owner_path.is_symlink():
                    raw_owner = load_private_json(owner_path)
                    root_owner = (
                        self._validate_prepare_abort_slot_owner(
                            raw_owner,
                            operation_id=operation_id,
                            slot=slot,
                        )
                    )
            slot_record: dict[str, Any] | None = None
            record_owned = False
            if record_present:
                slot_record = validate_slot_record(
                    load_private_json(record_path),
                    slot,
                )
                record_owned = (
                    slot_record["prepared_operation_id"] == operation_id
                )
                if record_owned:
                    if slot_record["source_sha"] != target_sha:
                        raise PullDeployError(
                            "prepare-abort Worker slot target SHA differs"
                        )
                    if (
                        target_tree is not None
                        and slot_record["source_tree"] != target_tree
                    ):
                        raise PullDeployError(
                            "prepare-abort Worker slot target tree differs"
                        )
                    target_tree = slot_record["source_tree"]
            root_owned = root_owner is not None or record_owned
            if root_owner is not None and slot_record is not None and not record_owned:
                raise PullDeployError(
                    "partial Worker slot conflicts with another operation record"
                )
            if record_owned and not root_present and not slot_tombstone_present:
                raise PullDeployError(
                    "prepare-abort Worker slot record has no owned slot tree"
                )
            if root_owned and active_slot is not None and active_slot["slot"] == slot:
                raise PullDeployError(
                    "prepare abort cannot remove the active Worker slot"
                )
            if root_present and root_owned:
                # The active-slot pointer is necessary but not sufficient: a
                # stale unit or same-UID process may still execute from an
                # otherwise inactive slot.  Seal the absence of such readers
                # before publishing destructive abort intent.
                self._assert_slot_not_running(root)
            if root_present and not root_owned and slot_tombstone_present:
                raise PullDeployError(
                    "prepare-abort slot quarantine conflicts with a live foreign slot"
                )
            slot_tombstone_inventory: str | None = None
            if slot_tombstone_present:
                ensure_private_directory(slot_tombstone)
                tombstone_owner = slot_tombstone / ".preparing.json"
                if tombstone_owner.exists() or tombstone_owner.is_symlink():
                    self._validate_prepare_abort_slot_owner(
                        load_private_json(tombstone_owner),
                        operation_id=operation_id,
                        slot=slot,
                    )
                elif record_present and not record_owned:
                    raise PullDeployError(
                        "ownerless Worker slot quarantine conflicts with "
                        "another operation record"
                    )
                elif root_present and not root_owned:
                    raise PullDeployError(
                        "ownerless Worker slot quarantine conflicts with "
                        "a foreign slot tree"
                    )
                slot_tombstone_inventory = directory_inventory_digest(
                    slot_tombstone
                )
                root_owned = True
            staging_inventory: str | None = None
            if staging_present:
                ensure_private_directory(staging)
                staging_owner = staging / ".preparing.json"
                if staging_owner.exists() or staging_owner.is_symlink():
                    self._validate_prepare_abort_slot_owner(
                        load_private_json(staging_owner),
                        operation_id=operation_id,
                        slot=slot,
                    )
                elif any(staging.iterdir()):
                    raise PullDeployError(
                        "prepare-abort Worker staging lacks exact ownership"
                    )
                staging_inventory = directory_inventory_digest(staging)
            staging_tombstone_inventory: str | None = None
            if staging_tombstone_present:
                ensure_private_directory(staging_tombstone)
                tombstone_owner = staging_tombstone / ".preparing.json"
                if tombstone_owner.exists() or tombstone_owner.is_symlink():
                    self._validate_prepare_abort_slot_owner(
                        load_private_json(tombstone_owner),
                        operation_id=operation_id,
                        slot=slot,
                    )
                staging_tombstone_inventory = directory_inventory_digest(
                    staging_tombstone
                )
            if (
                root_owned
                or staging_inventory is not None
                or staging_tombstone_inventory is not None
            ):
                records.append(
                    {
                        "slot": slot,
                        "root_inventory_sha256": (
                            directory_inventory_digest(root)
                            if root_present and root_owned
                            else None
                        ),
                        "record_sha256": (
                            sha256_file(record_path) if record_owned else None
                        ),
                        "slot_tombstone_inventory_sha256": (
                            slot_tombstone_inventory
                        ),
                        "staging_inventory_sha256": staging_inventory,
                        "staging_tombstone_inventory_sha256": (
                            staging_tombstone_inventory
                        ),
                    }
                )
        return records, target_tree

    def _validate_prepare_abort_journal(
        self,
        document: object,
        operation_id: str,
    ) -> dict[str, Any]:
        if (
            not isinstance(document, dict)
            or set(document) != PREPARE_ABORT_FIELDS
            or document.get("schema_version") != 1
            or document.get("operation_id") != operation_id
            or document.get("phase") not in PREPARE_ABORT_PHASES
        ):
            raise PullDeployError("prepare-abort journal has an invalid shape")
        owner = self._validate_prepare_owner_document(
            document.get("prepare_owner"),
            operation_id,
        )
        if (
            document.get("prepare_owner_sha256")
            != canonical_json_digest(owner)
            or document.get("target_sha") != owner["target_sha"]
        ):
            raise PullDeployError("prepare-abort owner identity differs")
        target_tree = document.get("target_tree")
        if target_tree is not None:
            require_sha(target_tree, "prepare-abort target tree")
        require_digest(
            document.get("operation_inventory_sha256"),
            "prepare-abort operation inventory",
        )
        descriptor_sha256 = document.get("descriptor_sha256")
        if descriptor_sha256 is not None:
            require_digest(descriptor_sha256, "prepare-abort descriptor")
        handoff_sha256 = document.get("control_handoff_sha256")
        handoff_schema = document.get("control_handoff_schema_version")
        executor_sha256 = document.get("executor_control_sha256")
        if handoff_sha256 is None:
            if handoff_schema is not None or executor_sha256 is not None:
                raise PullDeployError(
                    "prepare-abort handoff evidence is incomplete"
                )
        else:
            require_digest(handoff_sha256, "prepare-abort control handoff")
            require_digest(
                executor_sha256,
                "prepare-abort executor control",
            )
            if handoff_schema not in {1, 2} or target_tree is None:
                raise PullDeployError(
                    "prepare-abort control handoff schema is invalid"
                )
        staging = document.get("prepare_staging")
        if not isinstance(staging, dict) or set(staging) != {
            "live_inventory_sha256",
            "tombstone_inventory_sha256",
        }:
            raise PullDeployError("prepare-abort staging evidence is invalid")
        for value in staging.values():
            if value is not None:
                require_digest(value, "prepare-abort staging inventory")
        if all(value is not None for value in staging.values()):
            raise PullDeployError(
                "prepare-abort staging has two simultaneous authorities"
            )
        wheel_staging = document.get("wheel_staging")
        if not isinstance(wheel_staging, list):
            raise PullDeployError(
                "prepare-abort Worker wheel staging evidence is invalid"
            )
        seen_wheel_keys: list[str] = []
        expected_wheel_fields = {
            "wheel_cache_key",
            "live_inventory_sha256",
            "tombstone_inventory_sha256",
        }
        for record in wheel_staging:
            if (
                not isinstance(record, dict)
                or set(record) != expected_wheel_fields
            ):
                raise PullDeployError(
                    "prepare-abort Worker wheel staging record is invalid"
                )
            key = require_digest(
                record.get("wheel_cache_key"),
                "prepare-abort Worker wheel cache key",
            )
            if key in seen_wheel_keys:
                raise PullDeployError(
                    "prepare-abort Worker wheel staging is duplicated"
                )
            seen_wheel_keys.append(key)
            live = record.get("live_inventory_sha256")
            tombstone = record.get("tombstone_inventory_sha256")
            if (live is None) == (tombstone is None):
                raise PullDeployError(
                    "prepare-abort Worker wheel staging authority is ambiguous"
                )
            require_digest(
                live if live is not None else tombstone,
                "prepare-abort Worker wheel staging inventory",
            )
        if seen_wheel_keys != sorted(seen_wheel_keys):
            raise PullDeployError(
                "prepare-abort Worker wheel staging is not canonical"
            )
        slots = document.get("owned_slots")
        if not isinstance(slots, list) or len(slots) > 2:
            raise PullDeployError("prepare-abort Worker slot evidence is invalid")
        expected_slot_fields = {
            "slot",
            "root_inventory_sha256",
            "record_sha256",
            "slot_tombstone_inventory_sha256",
            "staging_inventory_sha256",
            "staging_tombstone_inventory_sha256",
        }
        seen_slots: list[str] = []
        for record in slots:
            if (
                not isinstance(record, dict)
                or set(record) != expected_slot_fields
                or record.get("slot") not in {"a", "b"}
                or record["slot"] in seen_slots
            ):
                raise PullDeployError(
                    "prepare-abort Worker slot record is invalid"
                )
            seen_slots.append(record["slot"])
            identities = [
                record[field]
                for field in expected_slot_fields - {"slot"}
            ]
            if all(value is None for value in identities):
                raise PullDeployError(
                    "prepare-abort Worker slot record is empty"
                )
            for value in identities:
                if value is not None:
                    require_digest(
                        value,
                        "prepare-abort Worker slot inventory",
                    )
        if seen_slots != sorted(seen_slots):
            raise PullDeployError(
                "prepare-abort Worker slots are not canonical"
            )
        prepared_ref = document.get("prepared_ref")
        expected_ref_name = f"refs/nexpoly/prepared/{operation_id}"
        if (
            not isinstance(prepared_ref, dict)
            or set(prepared_ref) != {"name", "target_sha"}
            or prepared_ref.get("name") != expected_ref_name
        ):
            raise PullDeployError("prepare-abort prepared Git ref is invalid")
        prepared_ref_target = prepared_ref.get("target_sha")
        if prepared_ref_target is not None:
            require_sha(prepared_ref_target, "prepare-abort prepared Git ref")
            if prepared_ref_target != document["target_sha"]:
                raise PullDeployError(
                    "prepare-abort prepared Git ref target differs"
                )
        current_state_sha256 = document.get("current_state_sha256")
        if current_state_sha256 is not None:
            require_digest(
                current_state_sha256,
                "prepare-abort current deployment",
            )
        require_digest(
            document.get("active_control_sha256"),
            "prepare-abort active controls",
        )
        active_slot = document.get("active_slot")
        active_slot_sha256 = document.get("active_slot_sha256")
        if active_slot is None:
            if active_slot_sha256 is not None:
                raise PullDeployError(
                    "prepare-abort active slot digest lacks a record"
                )
        else:
            validate_active_slot_record(active_slot)
            require_digest(
                active_slot_sha256,
                "prepare-abort active slot",
            )
        token_sha256 = document.get("bridge_token_sha256")
        token_operation_id = document.get("bridge_token_operation_id")
        token_status = document.get("bridge_token_status")
        if token_sha256 is None:
            if token_operation_id is not None or token_status is not None:
                raise PullDeployError(
                    "prepare-abort bridge token evidence is incomplete"
                )
        else:
            require_digest(token_sha256, "prepare-abort bridge token")
            require_operation_id(str(token_operation_id or ""))
            if token_status not in _bridge_core.TOKEN_STATUSES:
                raise PullDeployError(
                    "prepare-abort bridge token status is invalid"
                )
            if token_operation_id == operation_id:
                raise PullDeployError(
                    "bridge token operation requires first-bridge recovery, "
                    "not prepare abort"
                )
        expected_archive = (
            self.prepare_abort_archives_dir / operation_id
        )
        if document.get("archive_path") != str(expected_archive):
            raise PullDeployError("prepare-abort archive path differs")
        phase = document["phase"]
        if phase == "completed":
            if (
                document.get("status") != "aborted"
                or not isinstance(document.get("completed_at"), str)
                or not document["completed_at"]
            ):
                raise PullDeployError(
                    "completed prepare-abort journal is incomplete"
                )
            require_digest(
                document.get("archive_inventory_sha256"),
                "completed prepare-abort archive inventory",
            )
        elif (
            document.get("status") != "aborting"
            or document.get("archive_inventory_sha256") is not None
            or document.get("completed_at") is not None
        ):
            raise PullDeployError(
                "in-progress prepare-abort journal is inconsistent"
            )
        if (
            not isinstance(document.get("created_at"), str)
            or not document["created_at"]
        ):
            raise PullDeployError("prepare-abort timestamp is invalid")
        return dict(document)

    def _load_prepare_abort_journal(
        self,
        operation_id: str,
    ) -> dict[str, Any] | None:
        path = self._prepare_abort_journal_path(operation_id)
        if not (path.exists() or path.is_symlink()):
            return None
        ensure_private_directory(self.prepare_aborts_dir)
        return self._validate_prepare_abort_journal(
            load_private_json(path),
            operation_id,
        )

    def _advance_prepare_abort(
        self,
        journal: dict[str, Any],
        phase: str,
        **updates: Any,
    ) -> dict[str, Any]:
        phases = (
            "intent",
            "slot-cleanup-intent",
            "slots-cleaned",
            "operation-archive-intent",
            "completed",
        )
        current = phases.index(journal["phase"])
        requested = phases.index(phase)
        if requested not in {current, current + 1}:
            raise PullDeployError("prepare-abort phase transition is invalid")
        candidate = {**journal, **updates, "phase": phase}
        if phase == "completed":
            candidate["status"] = "aborted"
        candidate = self._validate_prepare_abort_journal(
            candidate,
            journal["operation_id"],
        )
        atomic_json(
            self._prepare_abort_journal_path(journal["operation_id"]),
            candidate,
        )
        journal.clear()
        journal.update(candidate)
        return journal

    def _ensure_prepare_abort_archive(
        self,
        journal: Mapping[str, Any],
    ) -> Path:
        archive = Path(journal["archive_path"])
        if archive.parent != self.prepare_abort_archives_dir:
            raise PullDeployError("prepare-abort archive escapes its authority")
        if archive.exists() or archive.is_symlink():
            ensure_private_directory(archive)
        else:
            archive.mkdir(mode=0o700)
            fsync_directory(archive.parent)
        owner_path = archive / "ARCHIVE-OWNER.json"
        expected_owner = {
            "schema_version": 1,
            "operation_id": journal["operation_id"],
            "prepare_owner_sha256": journal["prepare_owner_sha256"],
            "created_at": journal["created_at"],
        }
        if owner_path.exists() or owner_path.is_symlink():
            if load_private_json(owner_path) != expected_owner:
                raise PullDeployError(
                    "prepare-abort archive has different ownership"
                )
        else:
            if any(archive.iterdir()):
                raise PullDeployError(
                    "prepare-abort archive lacks its exact owner"
                )
            atomic_json(owner_path, expected_owner)
        return archive

    @staticmethod
    def _ensure_prepare_abort_archive_parent(
        archive: Path,
        *components: str,
    ) -> Path:
        current = archive
        for component in components:
            if (
                not component
                or component in {".", ".."}
                or "/" in component
                or "\0" in component
            ):
                raise PullDeployError(
                    "prepare-abort archive component is unsafe"
                )
            target = current / component
            existed = target.exists() or target.is_symlink()
            ensure_private_directory(target, create=True)
            if not existed:
                fsync_directory(current)
            current = target
        return current

    @staticmethod
    def _validate_prepare_abort_archive_file(
        path: Path,
        expected_sha256: str,
    ) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise PullDeployError(
                f"prepare-abort archive file is unavailable: {path}"
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or sha256_file(path) != expected_sha256
        ):
            raise PullDeployError(
                "prepare-abort archive file differs from sealed evidence"
            )

    def _archive_prepare_abort_directory(
        self,
        *,
        source: Path,
        target: Path,
        expected_inventory_sha256: str | None,
        label: str,
    ) -> None:
        source_present = source.exists() or source.is_symlink()
        target_present = target.exists() or target.is_symlink()
        if expected_inventory_sha256 is None:
            if source_present or target_present:
                raise PullDeployError(f"unrecorded {label} appeared")
            return
        require_digest(
            expected_inventory_sha256,
            f"prepare-abort {label} inventory",
        )
        if source_present and target_present:
            raise PullDeployError(
                f"{label} and its abort archive both exist"
            )
        if source_present:
            ensure_private_directory(source)
            if directory_inventory_digest(source) != expected_inventory_sha256:
                raise PullDeployError(
                    f"{label} changed after prepare-abort intent"
                )
            try:
                quarantine_directory_noreplace(source, target)
            except BaseException:
                if (
                    not (source.exists() or source.is_symlink())
                    and (target.exists() or target.is_symlink())
                ):
                    ensure_private_directory(target)
                    if (
                        directory_inventory_digest(target)
                        == expected_inventory_sha256
                    ):
                        return
                raise
        if not (target.exists() or target.is_symlink()):
            raise PullDeployError(f"{label} abort archive is unavailable")
        ensure_private_directory(target)
        if directory_inventory_digest(target) != expected_inventory_sha256:
            raise PullDeployError(
                f"{label} abort archive differs from sealed evidence"
            )

    def _archive_prepare_abort_file(
        self,
        *,
        source: Path,
        target: Path,
        expected_sha256: str | None,
        label: str,
    ) -> None:
        source_present = source.exists() or source.is_symlink()
        target_present = target.exists() or target.is_symlink()
        if expected_sha256 is None:
            if source_present or target_present:
                raise PullDeployError(f"unrecorded {label} appeared")
            return
        require_digest(expected_sha256, f"prepare-abort {label}")
        if source_present and target_present:
            raise PullDeployError(
                f"{label} and its abort archive both exist"
            )
        if source_present:
            self._validate_prepare_abort_archive_file(
                source,
                expected_sha256,
            )
            try:
                quarantine_regular_file_noreplace(source, target)
            except BaseException:
                if (
                    not (source.exists() or source.is_symlink())
                    and (target.exists() or target.is_symlink())
                ):
                    self._validate_prepare_abort_archive_file(
                        target,
                        expected_sha256,
                    )
                    return
                raise
        if not (target.exists() or target.is_symlink()):
            raise PullDeployError(f"{label} abort archive is unavailable")
        self._validate_prepare_abort_archive_file(target, expected_sha256)

    def _reconcile_prepare_abort_staging(
        self,
        journal: Mapping[str, Any],
    ) -> None:
        operation_id = journal["operation_id"]
        evidence = journal["prepare_staging"]
        archive = self._ensure_prepare_abort_archive(journal)
        staging = self.prepared_root / f".{operation_id}.preparing"
        tombstone = staging.parent / f"{staging.name}.discard"
        self._archive_prepare_abort_directory(
            source=staging,
            target=archive / "prepared-staging",
            expected_inventory_sha256=evidence["live_inventory_sha256"],
            label="prepare operation staging",
        )
        self._archive_prepare_abort_directory(
            source=tombstone,
            target=archive / "prepared-staging-tombstone",
            expected_inventory_sha256=(
                evidence["tombstone_inventory_sha256"]
            ),
            label="prepare operation staging quarantine",
        )

    def _reconcile_prepare_abort_wheel_staging(
        self,
        journal: Mapping[str, Any],
    ) -> None:
        operation_id = journal["operation_id"]
        archive = self._ensure_prepare_abort_archive(journal)
        live_parent = self._ensure_prepare_abort_archive_parent(
            archive,
            "wheel-staging",
        )
        tombstone_parent = self._ensure_prepare_abort_archive_parent(
            archive,
            "wheel-staging-tombstones",
        )
        for evidence in journal["wheel_staging"]:
            key = evidence["wheel_cache_key"]
            component = key.removeprefix("sha256:")
            staging = (
                self.wheel_cache_dir
                / f".{key}.staging-{operation_id}"
            )
            tombstone = staging.parent / f"{staging.name}.discard"
            self._archive_prepare_abort_directory(
                source=staging,
                target=live_parent / component,
                expected_inventory_sha256=(
                    evidence["live_inventory_sha256"]
                ),
                label=f"Worker wheel staging {key}",
            )
            self._archive_prepare_abort_directory(
                source=tombstone,
                target=tombstone_parent / component,
                expected_inventory_sha256=(
                    evidence["tombstone_inventory_sha256"]
                ),
                label=f"Worker wheel staging quarantine {key}",
            )

    def _reconcile_prepare_abort_slot(
        self,
        journal: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> None:
        operation_id = journal["operation_id"]
        slot = evidence["slot"]
        archive = self._ensure_prepare_abort_archive(journal)
        root = self.venv_root / f"md-{slot}"
        record_path = self.slots_state_dir / f"md-{slot}.json"
        staging = self.venv_root / f".md-{slot}.preparing-{operation_id}"
        staging_tombstone = staging.parent / f"{staging.name}.discard"
        slot_tombstone = self.venv_root / f".{slot}.discard-{operation_id}"
        worker_slots = self._ensure_prepare_abort_archive_parent(
            archive,
            "worker-slots",
        )
        worker_slot_records = self._ensure_prepare_abort_archive_parent(
            archive,
            "worker-slot-records",
        )
        worker_slot_tombstones = self._ensure_prepare_abort_archive_parent(
            archive,
            "worker-slot-tombstones",
        )
        worker_staging = self._ensure_prepare_abort_archive_parent(
            archive,
            "worker-staging",
        )
        worker_staging_tombstones = (
            self._ensure_prepare_abort_archive_parent(
                archive,
                "worker-staging-tombstones",
            )
        )

        self._archive_prepare_abort_directory(
            source=staging,
            target=worker_staging / f"md-{slot}",
            expected_inventory_sha256=evidence[
                "staging_inventory_sha256"
            ],
            label=f"Worker slot {slot} staging",
        )
        self._archive_prepare_abort_directory(
            source=staging_tombstone,
            target=worker_staging_tombstones / f"md-{slot}",
            expected_inventory_sha256=evidence[
                "staging_tombstone_inventory_sha256"
            ],
            label=f"Worker slot {slot} staging quarantine",
        )
        if evidence["root_inventory_sha256"] is not None:
            if root.exists() or root.is_symlink():
                # Recheck after a crash/retry and immediately before the live
                # slot is renamed out of service.  The durable inventory alone
                # cannot prove that no reader started after abort intent.
                self._assert_slot_not_running(root)
            self._archive_prepare_abort_directory(
                source=root,
                target=worker_slots / f"md-{slot}",
                expected_inventory_sha256=evidence[
                    "root_inventory_sha256"
                ],
                label=f"Worker slot {slot}",
            )
        if evidence["record_sha256"] is not None:
            self._archive_prepare_abort_file(
                source=record_path,
                target=worker_slot_records / f"md-{slot}.json",
                expected_sha256=evidence["record_sha256"],
                label=f"Worker slot {slot} record",
            )
        self._archive_prepare_abort_directory(
            source=slot_tombstone,
            target=worker_slot_tombstones / f"md-{slot}",
            expected_inventory_sha256=evidence[
                "slot_tombstone_inventory_sha256"
            ],
            label=f"Worker slot {slot} quarantine",
        )

        if record_path.exists() or record_path.is_symlink():
            remaining_record = validate_slot_record(
                load_private_json(record_path),
                slot,
            )
            if remaining_record["prepared_operation_id"] == operation_id:
                raise PullDeployError(
                    "prepare-abort Worker slot record remains owned"
                )
        if root.exists() or root.is_symlink():
            ensure_private_directory(root)
            owner_path = root / ".preparing.json"
            if owner_path.exists() or owner_path.is_symlink():
                owner = load_private_json(owner_path)
                if owner.get("operation_id") == operation_id:
                    raise PullDeployError(
                        "prepare-abort Worker slot remains owned"
                    )

    def _reconcile_prepare_abort_archive(
        self,
        journal: Mapping[str, Any],
    ) -> str:
        operation_id = journal["operation_id"]
        operation, _descriptor_path, ready_path = self._operation_paths(
            operation_id
        )
        archive = self._ensure_prepare_abort_archive(journal)
        handoff_path = self.control_handoffs_dir / f"{operation_id}.json"
        self._archive_prepare_abort_file(
            source=handoff_path,
            target=archive / "control-handoff.json",
            expected_sha256=journal["control_handoff_sha256"],
            label="prepare control handoff",
        )

        prepared_ref = journal["prepared_ref"]
        ref_name = prepared_ref["name"]
        expected_ref = prepared_ref["target_sha"]
        ref_evidence_path = archive / "prepared-ref.json"
        if expected_ref is None:
            if self._observe_prepare_abort_prepared_ref(ref_name) is not None:
                raise PullDeployError(
                    "unrecorded prepared Git ref appeared during abort"
                )
            if ref_evidence_path.exists() or ref_evidence_path.is_symlink():
                raise PullDeployError(
                    "unrecorded prepared Git ref archive appeared"
                )
        else:
            ref_evidence = {
                "schema_version": 1,
                "operation_id": operation_id,
                "ref": ref_name,
                "target_sha": expected_ref,
            }
            if ref_evidence_path.exists() or ref_evidence_path.is_symlink():
                if load_private_json(ref_evidence_path) != ref_evidence:
                    raise PullDeployError(
                        "prepared Git ref archive differs from sealed evidence"
                    )
            else:
                observed_ref = self._observe_prepare_abort_prepared_ref(
                    ref_name
                )
                if observed_ref != expected_ref:
                    raise PullDeployError(
                        "prepared Git ref disappeared before evidence was archived"
                    )
                atomic_json(ref_evidence_path, ref_evidence)
            observed_ref = self._observe_prepare_abort_prepared_ref(ref_name)
            if observed_ref == expected_ref:
                try:
                    self._git(
                        "update-ref",
                        "-d",
                        ref_name,
                        expected_ref,
                    )
                except BaseException:
                    if (
                        self._observe_prepare_abort_prepared_ref(ref_name)
                        is None
                    ):
                        pass
                    else:
                        raise
            elif observed_ref is not None:
                raise PullDeployError(
                    "prepared Git ref changed before CAS deletion"
                )
            if self._observe_prepare_abort_prepared_ref(ref_name) is not None:
                raise PullDeployError(
                    "prepared Git ref remains after CAS deletion"
                )

        if ready_path.exists() or ready_path.is_symlink():
            raise PullDeployError(
                "READY prepare cannot be archived by prepare-abort"
            )
        self._archive_prepare_abort_directory(
            source=operation,
            target=archive / "operation",
            expected_inventory_sha256=journal[
                "operation_inventory_sha256"
            ],
            label="prepare operation",
        )
        return directory_inventory_digest(archive)

    def _assert_no_deployment_terminal_records(
        self,
        operation_id: str,
    ) -> None:
        state = self._load_operation_state(operation_id)
        if state is not None:
            raise PullDeployError(
                f"operation ID is terminal ({state['outcome']})"
            )
        for audit in self._operation_directories(operation_id):
            if not (audit.exists() or audit.is_symlink()):
                continue
            ensure_private_directory(audit)
            terminal_files = sorted(
                path.name
                for path in audit.glob("*.json")
                if path.name != "operation-state.json"
            )
            if terminal_files:
                raise PullDeployError(
                    "operation has terminal audit evidence without a valid state record"
                )

    def abort_prepare(self, *, operation_id: str) -> dict[str, Any]:
        """Crash-safely retire one unsealed, token-free prepare operation."""

        self.ensure_roots(mutating=True)
        operation_id = require_operation_id(operation_id)
        if not self.apply_enabled:
            raise PullDeployError("prepare-abort requires mutation mode")
        with self.deployment_lock():
            self._require_no_contract_maintenance(
                require_alias_completed=False
            )
            if self.marker_path.exists() or self.marker_path.is_symlink():
                raise PullDeployError(
                    "deployment recovery must finish before prepare-abort"
                )
            operation, descriptor_path, ready_path = self._operation_paths(
                operation_id
            )
            journal = self._load_prepare_abort_journal(operation_id)
            if journal is not None and journal["phase"] == "completed":
                archive = Path(journal["archive_path"])
                if operation.exists() or operation.is_symlink():
                    raise PullDeployError(
                        "completed prepare-abort still has a live operation"
                    )
                if (
                    not (archive.exists() or archive.is_symlink())
                    or directory_inventory_digest(archive)
                    != journal["archive_inventory_sha256"]
                ):
                    raise PullDeployError(
                        "completed prepare-abort archive is unavailable"
                    )
                handoff_path = (
                    self.control_handoffs_dir / f"{operation_id}.json"
                )
                if handoff_path.exists() or handoff_path.is_symlink():
                    raise PullDeployError(
                        "completed prepare-abort still has a live handoff"
                    )
                archived_handoff = archive / "control-handoff.json"
                expected_handoff = journal["control_handoff_sha256"]
                if expected_handoff is None:
                    if (
                        archived_handoff.exists()
                        or archived_handoff.is_symlink()
                    ):
                        raise PullDeployError(
                            "completed prepare-abort has an unrecorded handoff"
                        )
                else:
                    self._validate_prepare_abort_archive_file(
                        archived_handoff,
                        expected_handoff,
                    )
                prepared_ref = journal["prepared_ref"]
                if (
                    self._observe_prepare_abort_prepared_ref(
                        prepared_ref["name"]
                    )
                    is not None
                ):
                    raise PullDeployError(
                        "completed prepare-abort still has a prepared Git ref"
                    )
                ref_evidence_path = archive / "prepared-ref.json"
                if prepared_ref["target_sha"] is None:
                    if (
                        ref_evidence_path.exists()
                        or ref_evidence_path.is_symlink()
                    ):
                        raise PullDeployError(
                            "completed prepare-abort has unrecorded Git evidence"
                        )
                elif load_private_json(ref_evidence_path) != {
                    "schema_version": 1,
                    "operation_id": operation_id,
                    "ref": prepared_ref["name"],
                    "target_sha": prepared_ref["target_sha"],
                }:
                    raise PullDeployError(
                        "completed prepare-abort Git provenance changed"
                    )
                archived_operation = archive / "operation"
                ensure_private_directory(archived_operation)
                if (
                    directory_inventory_digest(archived_operation)
                    != journal["operation_inventory_sha256"]
                ):
                    raise PullDeployError(
                        "completed prepare-abort operation provenance changed"
                    )
                return {
                    "action": "prepare-abort",
                    "status": "already-aborted",
                    "operation_id": operation_id,
                    "archive_path": str(archive),
                    "archive_inventory_sha256": (
                        journal["archive_inventory_sha256"]
                    ),
                }
            if journal is None:
                self._assert_no_deployment_terminal_records(operation_id)
                if not (operation.exists() or operation.is_symlink()):
                    raise PullDeployError(
                        "prepare operation is unavailable for abort"
                    )
                ensure_private_directory(operation)
                if ready_path.exists() or ready_path.is_symlink():
                    raise PullDeployError(
                        "READY prepare must use apply/rollback, not prepare-abort"
                    )
                owner = self._validate_prepare_owner_document(
                    load_private_json(operation / "prepare-owner.json"),
                    operation_id,
                )
                operation_inventory_sha256 = directory_inventory_digest(
                    operation
                )
                descriptor_sha256: str | None = None
                descriptor_target_tree: str | None = None
                if descriptor_path.exists() or descriptor_path.is_symlink():
                    descriptor = validate_descriptor(
                        load_private_json(descriptor_path)
                    )
                    if (
                        descriptor["operation_id"] != operation_id
                        or descriptor["repository"]["target_sha"]
                        != owner["target_sha"]
                    ):
                        raise PullDeployError(
                            "prepare-abort descriptor ownership differs"
                        )
                    descriptor_sha256 = sha256_file(descriptor_path)
                    descriptor_target_tree = descriptor["repository"][
                        "target_tree"
                    ]
                handoff = self._prepare_abort_handoff_evidence(
                    operation_id=operation_id,
                    owner=owner,
                )
                target_tree = handoff["target_tree"]
                if (
                    target_tree is not None
                    and descriptor_target_tree is not None
                    and target_tree != descriptor_target_tree
                ):
                    raise PullDeployError(
                        "prepare-abort descriptor and handoff trees differ"
                    )
                target_tree = target_tree or descriptor_target_tree
                fences = self._capture_prepare_abort_fences()
                if fences["bridge_token_operation_id"] == operation_id:
                    raise PullDeployError(
                        "bridge token operation requires first-bridge recovery, "
                        "not prepare-abort"
                    )
                prepare_staging = self._capture_prepare_abort_staging(
                    operation_id=operation_id,
                    owner=owner,
                )
                wheel_staging = (
                    self._capture_prepare_abort_wheel_staging(
                        operation_id=operation_id,
                    )
                )
                owned_slots, slot_target_tree = (
                    self._capture_prepare_abort_slots(
                        operation_id=operation_id,
                        target_sha=owner["target_sha"],
                        expected_target_tree=target_tree,
                        active_slot=fences["active_slot"],
                    )
                )
                if (
                    target_tree is not None
                    and slot_target_tree is not None
                    and target_tree != slot_target_tree
                ):
                    raise PullDeployError(
                        "prepare-abort slot and source target trees differ"
                    )
                target_tree = target_tree or slot_target_tree
                archive = self.prepare_abort_archives_dir / operation_id
                if archive.exists() or archive.is_symlink():
                    raise PullDeployError(
                        "prepare-abort archive already exists without a journal"
                    )
                self._ensure_prepare_abort_roots()
                journal = {
                    "schema_version": 1,
                    "operation_id": operation_id,
                    "status": "aborting",
                    "phase": "intent",
                    "prepare_owner": owner,
                    "prepare_owner_sha256": canonical_json_digest(owner),
                    "target_sha": owner["target_sha"],
                    "target_tree": target_tree,
                    **handoff,
                    "operation_inventory_sha256": (
                        operation_inventory_sha256
                    ),
                    "descriptor_sha256": descriptor_sha256,
                    "prepare_staging": prepare_staging,
                    "wheel_staging": wheel_staging,
                    "owned_slots": owned_slots,
                    "prepared_ref": (
                        self._capture_prepare_abort_prepared_ref(
                            operation_id=operation_id,
                            target_sha=owner["target_sha"],
                        )
                    ),
                    **fences,
                    "archive_path": str(archive),
                    "archive_inventory_sha256": None,
                    "created_at": utc_now(),
                    "completed_at": None,
                }
                journal = self._validate_prepare_abort_journal(
                    journal,
                    operation_id,
                )
                atomic_json(
                    self._prepare_abort_journal_path(operation_id),
                    journal,
                )
            else:
                self._assert_no_deployment_terminal_records(operation_id)
                if ready_path.exists() or ready_path.is_symlink():
                    raise PullDeployError(
                        "READY appeared during prepare-abort"
                    )
            self._assert_prepare_abort_fences(journal)
            if journal["phase"] == "intent":
                self._advance_prepare_abort(
                    journal,
                    "slot-cleanup-intent",
                )
            if journal["phase"] == "slot-cleanup-intent":
                self._reconcile_prepare_abort_staging(journal)
                self._reconcile_prepare_abort_wheel_staging(journal)
                for slot_evidence in journal["owned_slots"]:
                    self._reconcile_prepare_abort_slot(
                        journal,
                        slot_evidence,
                    )
                self._advance_prepare_abort(journal, "slots-cleaned")
            if journal["phase"] == "slots-cleaned":
                if not (operation.exists() or operation.is_symlink()):
                    raise PullDeployError(
                        "prepare operation disappeared before archive intent"
                    )
                ensure_private_directory(operation)
                if (
                    directory_inventory_digest(operation)
                    != journal["operation_inventory_sha256"]
                ):
                    raise PullDeployError(
                        "prepare operation changed before archive intent"
                    )
                self._advance_prepare_abort(
                    journal,
                    "operation-archive-intent",
                )
            if journal["phase"] == "operation-archive-intent":
                archive_digest = self._reconcile_prepare_abort_archive(
                    journal
                )
                self._advance_prepare_abort(
                    journal,
                    "completed",
                    archive_inventory_sha256=archive_digest,
                    completed_at=utc_now(),
                )
            return {
                "action": "prepare-abort",
                "status": "aborted",
                "operation_id": operation_id,
                "archive_path": journal["archive_path"],
                "archive_inventory_sha256": (
                    journal["archive_inventory_sha256"]
                ),
            }

    def _operation_directories(self, operation_id: str) -> tuple[Path, Path]:
        operation_id = require_operation_id(operation_id)
        return (
            self.audit_dir / operation_id,
            self.runtime_root
            / "legacy-takeover"
            / "runtime"
            / "pull-terminal"
            / operation_id,
        )

    def _operation_state_path(
        self,
        operation_id: str,
        *,
        restored_takeover: bool = False,
    ) -> Path:
        normal, external = self._operation_directories(operation_id)
        return (
            external if restored_takeover else normal
        ) / "operation-state.json"

    def _ensure_private_operation_directory(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.runtime_root)
        except ValueError as exc:
            raise PullDeployError(
                "terminal operation audit escapes the runtime root"
            ) from exc
        ensure_private_directory(self.runtime_root)
        current = self.runtime_root
        for component in relative.parts:
            current = current / component
            ensure_private_directory(current, create=True)

    def _use_restored_takeover_audit(self, operation_id: str) -> bool:
        _normal, external = self._operation_directories(operation_id)
        if external.exists() or external.is_symlink():
            ensure_private_directory(external)
            return True
        if not (self.marker_path.exists() or self.marker_path.is_symlink()):
            return False
        marker = load_private_json(self.marker_path)
        if marker.get("operation_id") != operation_id:
            return False
        terminal = marker.get("takeover_restored_terminal_sha256")
        if terminal is None:
            return False
        require_digest(terminal, "legacy takeover restored terminal digest")
        return True

    def _load_operation_state(self, operation_id: str) -> dict[str, Any] | None:
        paths = [
            directory / "operation-state.json"
            for directory in self._operation_directories(operation_id)
        ]
        present = [
            path for path in paths if path.exists() or path.is_symlink()
        ]
        if not present:
            return None
        if len(present) != 1:
            raise PullDeployError(
                "terminal deployment operation exists in multiple audit roots"
            )
        path = present[0]
        document = load_private_json(path)
        if (
            set(document) != OPERATION_STATE_FIELDS
            or document.get("schema_version") != 1
            or document.get("operation_id") != operation_id
            or document.get("outcome") not in TERMINAL_OPERATION_OUTCOMES
            or not isinstance(document.get("recorded_at"), str)
            or not document["recorded_at"]
        ):
            raise PullDeployError("terminal deployment operation record is invalid")
        require_digest(
            document.get("descriptor_sha256"),
            "terminal operation descriptor digest",
        )
        return document

    def _assert_operation_not_terminal(self, operation_id: str, *, action: str) -> None:
        self._assert_no_deployment_terminal_records(operation_id)
        abort = self._load_prepare_abort_journal(operation_id)
        if abort is not None:
            raise PullDeployError(
                "operation ID has a durable prepare-abort "
                f"({abort['phase']}) and cannot {action}"
            )

    def _record_operation_outcome(
        self,
        *,
        operation_id: str,
        descriptor_sha256: str,
        outcome: str,
    ) -> dict[str, Any]:
        if outcome not in TERMINAL_OPERATION_OUTCOMES:
            raise PullDeployError("terminal deployment outcome is invalid")
        descriptor_sha256 = require_digest(
            descriptor_sha256, "terminal operation descriptor digest"
        )
        existing = self._load_operation_state(operation_id)
        if existing is not None:
            if (
                existing["descriptor_sha256"] == descriptor_sha256
                and existing["outcome"] == outcome
            ):
                return existing
            if not (
                existing["descriptor_sha256"] == descriptor_sha256
                and existing["outcome"] == "deployed"
                and outcome == "rolled-back"
            ):
                raise PullDeployError(
                    "terminal deployment operation transition is invalid"
                )
        normal, external = self._operation_directories(operation_id)
        operation_dir = (
            external
            if self._use_restored_takeover_audit(operation_id)
            else normal
        )
        self._ensure_private_operation_directory(operation_dir)
        document = {
            "schema_version": 1,
            "operation_id": operation_id,
            "descriptor_sha256": descriptor_sha256,
            "outcome": outcome,
            "recorded_at": utc_now(),
        }
        atomic_json(operation_dir / "operation-state.json", document)
        return document

    def _open_prepare_operation(
        self,
        operation: Path,
        *,
        operation_id: str,
        target_sha: str,
    ) -> int:
        """Create or safely resume one unsealed prepare owned by this controller."""

        self._require_deploy_lock_for_staging()
        owner_path = operation / "prepare-owner.json"
        staging = operation.parent / f".{operation.name}.preparing"
        owner = {
            "schema_version": 1,
            "operation_id": operation_id,
            "target_sha": target_sha,
            "controller_sha256": self.controller_digest(),
            "created_at": utc_now(),
        }
        self._clear_private_staging_tombstone(
            staging,
            owner_name="prepare-owner.json",
            expected_owner=owner,
            label="prepare operation staging",
            identity_fields=(
                "schema_version",
                "operation_id",
                "target_sha",
                "controller_sha256",
            ),
        )
        if not operation.exists() and not operation.is_symlink():
            if staging.exists() or staging.is_symlink():
                if not staging.is_dir() or staging.is_symlink():
                    raise PullDeployError(
                        "prepare operation staging is unsafe"
                    )
                staging_owner_path = staging / "prepare-owner.json"
                if (
                    not staging_owner_path.exists()
                    and not staging_owner_path.is_symlink()
                ):
                    self._discard_private_staging(
                        staging, label="prepare operation staging"
                    )
                else:
                    staging_owner = load_private_json(staging_owner_path)
                    if (
                        set(staging_owner) != PREPARE_OWNER_FIELDS
                        or staging_owner.get("schema_version") != 1
                        or staging_owner.get("operation_id") != operation_id
                        or staging_owner.get("target_sha") != target_sha
                        or staging_owner.get("controller_sha256")
                        != self.controller_digest()
                    ):
                        raise PullDeployError(
                            "prepare operation staging belongs to another operation"
                        )
                    self._discard_private_staging(
                        staging, label="prepare operation staging"
                    )
            staging.mkdir(mode=0o700)
            atomic_json(staging / "prepare-owner.json", owner)
            fsync_private_tree(staging)
            try:
                rename_directory_noreplace(staging, operation)
            except FileExistsError:
                # A prior publication can have completed while its response
                # was lost.  Accept only the exact target/controller owner.
                ensure_private_directory(operation)
                observed = load_private_json(owner_path)
                if (
                    set(observed) != PREPARE_OWNER_FIELDS
                    or observed.get("schema_version") != 1
                    or observed.get("operation_id") != operation_id
                    or observed.get("target_sha") != target_sha
                    or observed.get("controller_sha256")
                    != self.controller_digest()
                ):
                    raise PullDeployError(
                        "unfinished prepare directory has different ownership"
                    )
                self._discard_private_staging(
                    staging, label="prepare operation staging"
                )
            except BaseException:
                if operation.exists() and not operation.is_symlink():
                    observed = load_private_json(owner_path)
                    if (
                        set(observed) == PREPARE_OWNER_FIELDS
                        and observed.get("schema_version") == 1
                        and observed.get("operation_id") == operation_id
                        and observed.get("target_sha") == target_sha
                        and observed.get("controller_sha256")
                        == self.controller_digest()
                    ):
                        if staging.exists() or staging.is_symlink():
                            staging_observed = load_private_json(
                                staging / "prepare-owner.json"
                            )
                            if (
                                set(staging_observed)
                                != PREPARE_OWNER_FIELDS
                                or staging_observed.get("schema_version")
                                != 1
                                or staging_observed.get("operation_id")
                                != operation_id
                                or staging_observed.get("target_sha")
                                != target_sha
                                or staging_observed.get(
                                    "controller_sha256"
                                )
                                != self.controller_digest()
                            ):
                                raise PullDeployError(
                                    "prepare operation staging changed during publication"
                                )
                            self._discard_private_staging(
                                staging,
                                label="prepare operation staging",
                            )
                    else:
                        raise
                else:
                    raise
            attempt = 1
        else:
            ensure_private_directory(operation)
            owner = load_private_json(owner_path)
            if (
                set(owner) != PREPARE_OWNER_FIELDS
                or owner.get("schema_version") != 1
                or owner.get("operation_id") != operation_id
                or owner.get("target_sha") != target_sha
                or owner.get("controller_sha256") != self.controller_digest()
            ):
                raise PullDeployError(
                    "unfinished prepare directory has different ownership"
                )
            attempt_path = operation / "prepare-attempt.json"
            if attempt_path.exists():
                previous = load_private_json(attempt_path)
                value = previous.get("attempt")
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    raise PullDeployError("prepare attempt record is invalid")
                attempt = value + 1
            else:
                attempt = 1
        atomic_json(
            operation / "prepare-attempt.json",
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "target_sha": target_sha,
                "attempt": attempt,
                "status": "running",
                "started_at": utc_now(),
            },
        )
        return attempt

    def _handoff_prepare_to_target_controller(
        self, *, target_sha: str, operation_id: str
    ) -> None:
        """Stage target controls with A, then replace A with candidate B.

        The active controller performs only the compatibility/installation
        handoff.  The target controller performs every operation-specific
        prepare step and owns the descriptor, allowing a compatible future
        DFT/GPU control release to introduce its own sealed evidence.
        """

        with self.deployment_lock():
            self._require_no_contract_maintenance(require_alias_completed=False)
            if self.marker_path.exists() or self.marker_path.is_symlink():
                raise PullDeployError(
                    "an interrupted deployment must be recovered before prepare"
                )
            self._assert_operation_not_terminal(operation_id, action="prepare")
            self._assert_existing_prepare_command_mode(
                operation_id=operation_id,
                bridge_requested=False,
            )
            current = self.repository_identity(require_ssh_origin=True)
            if self.remote_main() != target_sha:
                raise PullDeployError(
                    "requested target is no longer current remote main"
                )
            previous_active = self.active_control_evidence()
            if self.current_state_path.exists() or self.current_state_path.is_symlink():
                governed = self._validate_steady_deployment_state(
                    load_private_json(self.current_state_path)
                )
                if (
                    governed["source_sha"] != current["sha"]
                    or governed["source_tree"] != current["tree"]
                    or governed["active_control"] != previous_active
                ):
                    raise PullDeployError(
                        "governed source/control state changed before prepare handoff"
                    )
            target_tree = self.fetch_target(target_sha, operation_id)
            self.validate_installed_controls_against_target(target_sha)
            handoff_path = self.control_handoffs_dir / f"{operation_id}.json"
            if handoff_path.exists() or handoff_path.is_symlink():
                record = load_private_json(handoff_path)
                if (
                    record.get("operation_id") != operation_id
                    or record.get("target_sha") != target_sha
                    or record.get("target_tree") != target_tree
                    or record.get("previous_active_control") != previous_active
                    or canonical_json_digest(record.get("previous_active_control"))
                    != record.get("previous_active_control_sha256")
                    or canonical_json_digest(record.get("executor_control"))
                    != record.get("executor_control_sha256")
                ):
                    raise PullDeployError(
                        "prepare handoff record has different ownership"
                    )
                candidate = record["executor_control"]
            else:
                candidate = self.prepare_control_release(
                    operation_id=operation_id,
                    target_sha=target_sha,
                    target_tree=target_tree,
                )
                record = {
                    "schema_version": 1,
                    "protocol_version": _control_runtime.PROTOCOL_VERSION,
                    "operation_id": operation_id,
                    "target_sha": target_sha,
                    "target_tree": target_tree,
                    "previous_active_control": previous_active,
                    "previous_active_control_sha256": canonical_json_digest(
                        previous_active
                    ),
                    "executor_control": candidate,
                    "executor_control_sha256": canonical_json_digest(candidate),
                    "created_at": utc_now(),
                }
                atomic_json(handoff_path, record)
            try:
                _record, manifest, release_root = (
                    _control_runtime.load_candidate_control(
                        self.runtime_root, candidate
                    )
                )
            except Exception as exc:
                raise PullDeployError(
                    "target controller cannot be loaded for handoff"
                ) from exc
            controller_path = release_root / manifest["entrypoints"]["deploy"]["file"]
            if manifest["entrypoints"]["deploy"]["kind"] != "python":
                raise PullDeployError("target deploy entrypoint is not Python")
            handoff_digest = sha256_file(handoff_path)
        environment = self.control_environment()
        environment.update(
            {
                "NEXPOLY_ACTIVE_CONTROL_ROOT": str(release_root),
                "NEXPOLY_ACTIVE_CONTROL_RELEASE_ID": candidate["release_id"],
                "NEXPOLY_PREPARE_HANDOFF_OPERATION": operation_id,
                "NEXPOLY_PREPARE_HANDOFF_SHA256": handoff_digest,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        os.execve(
            "/usr/bin/python3",
            [
                "/usr/bin/python3",
                "-I",
                "-B",
                str(controller_path),
                "prepare",
                "--sha",
                target_sha,
                "--operation-id",
                operation_id,
            ],
            environment,
        )

    def _handoff_prepare_to_bridge_controller(
        self,
        *,
        authority_sha: str,
        operation_id: str,
        prefetch_operation_id: str,
    ) -> None:
        """Let active F derive B, install B controls, then exec B to prepare."""

        with self.deployment_lock():
            self._require_no_contract_maintenance(require_alias_completed=False)
            if self.marker_path.exists() or self.marker_path.is_symlink():
                raise PullDeployError(
                    "an interrupted deployment must be recovered before prepare"
                )
            self._assert_operation_not_terminal(
                operation_id, action="bridge-prepare"
            )
            self._assert_existing_prepare_command_mode(
                operation_id=operation_id,
                bridge_requested=True,
            )
            if (
                self.current_state_path.exists()
                or self.current_state_path.is_symlink()
            ):
                raise PullDeployError(
                    "historical bridge is restricted to first governed takeover"
                )
            handoff_path = (
                self.control_handoffs_dir / f"{operation_id}.json"
            )
            handoff_exists = (
                handoff_path.exists() or handoff_path.is_symlink()
            )
            if not handoff_exists and self.remote_main() != authority_sha:
                raise PullDeployError(
                    "bridge authority is no longer current remote main"
                )
            current = self.repository_identity(require_ssh_origin=True)
            prefetch_ready, prefetch_binding = (
                self.maintenance_prefetch_evidence(
                    prefetch_operation_id,
                    authority_sha=authority_sha,
                    authority_tree=None,
                )
            )
            relation = self.materialize_prefetched_bridge_relation(
                prefetch_ready,
                create_target_ref=True,
            )
            target_sha = relation["policy"]["target_sha"]
            target_tree = relation["policy"]["target_tree"]
            takeover = self.completed_legacy_takeover_evidence(
                authority_sha=authority_sha,
                authority_tree=relation["relation"]["authority_tree"],
                expected_repository=current,
            )
            previous_active = self.active_control_evidence()
            if (
                previous_active["source_sha"] != authority_sha
                or previous_active["source_tree"]
                != relation["relation"]["authority_tree"]
            ):
                raise PullDeployError(
                    "active controls are not the exact bridge authority"
                )
            self.validate_installed_controls_against_target(authority_sha)
            if (
                self._git(
                    "merge-base",
                    "--is-ancestor",
                    current["sha"],
                    target_sha,
                    check=False,
                ).returncode
                != 0
            ):
                raise PullDeployError(
                    "bridge target is not a fast-forward of production HEAD"
                )
            if handoff_exists:
                record = load_private_json(handoff_path)
                if (
                    record.get("schema_version") != 2
                    or record.get("operation_id") != operation_id
                    or record.get("authority_sha") != authority_sha
                    or record.get("authority_tree")
                    != relation["relation"]["authority_tree"]
                    or record.get("target_sha") != target_sha
                    or record.get("target_tree") != target_tree
                    or record.get("policy_id") != relation["policy"]["policy_id"]
                    or record.get("policy_sha256")
                    != relation["policy_sha256"]
                    or record.get("prefetch_operation_id")
                    != prefetch_operation_id
                    or record.get("prefetch") != prefetch_binding
                    or record.get("legacy_takeover") != takeover
                    or record.get("previous_active_control") != previous_active
                    or canonical_json_digest(record.get("previous_active_control"))
                    != record.get("previous_active_control_sha256")
                    or canonical_json_digest(record.get("executor_control"))
                    != record.get("executor_control_sha256")
                ):
                    raise PullDeployError(
                        "bridge prepare handoff has different ownership"
                    )
                candidate = record["executor_control"]
            else:
                if self.remote_main() != authority_sha:
                    raise PullDeployError(
                        "bridge authority changed before control handoff"
                    )
                candidate = self.prepare_control_release(
                    operation_id=operation_id,
                    target_sha=target_sha,
                    target_tree=target_tree,
                )
                record = {
                    "schema_version": 2,
                    "protocol_version": _control_runtime.PROTOCOL_VERSION,
                    "operation_id": operation_id,
                    "authority_sha": authority_sha,
                    "authority_tree": relation["relation"]["authority_tree"],
                    "target_sha": target_sha,
                    "target_tree": target_tree,
                    "target_ref": relation["policy"]["target_ref"],
                    "policy_id": relation["policy"]["policy_id"],
                    "policy_sha256": relation["policy_sha256"],
                    "prefetch_operation_id": prefetch_operation_id,
                    "prefetch": prefetch_binding,
                    "legacy_takeover": takeover,
                    "previous_active_control": previous_active,
                    "previous_active_control_sha256": canonical_json_digest(
                        previous_active
                    ),
                    "executor_control": candidate,
                    "executor_control_sha256": canonical_json_digest(candidate),
                    "created_at": utc_now(),
                }
                atomic_json(handoff_path, record)
            try:
                _record, manifest, release_root = (
                    _control_runtime.load_candidate_control(
                        self.runtime_root, candidate
                    )
                )
            except Exception as exc:
                raise PullDeployError(
                    "bridge target controller cannot be loaded"
                ) from exc
            if BRIDGE_DESCRIPTOR_SCHEMA_VERSION not in manifest["compatibility"][
                "descriptor_schema_versions"
            ]:
                raise PullDeployError(
                    "bridge target controls do not support descriptor schema v3"
                )
            controller_path = release_root / manifest["entrypoints"]["deploy"]["file"]
            if manifest["entrypoints"]["deploy"]["kind"] != "python":
                raise PullDeployError("bridge deploy entrypoint is not Python")
            handoff_digest = sha256_file(handoff_path)
        environment = self.control_environment()
        environment.update(
            {
                "NEXPOLY_ACTIVE_CONTROL_ROOT": str(release_root),
                "NEXPOLY_ACTIVE_CONTROL_RELEASE_ID": candidate["release_id"],
                "NEXPOLY_PREPARE_HANDOFF_OPERATION": operation_id,
                "NEXPOLY_PREPARE_HANDOFF_SHA256": handoff_digest,
                "NEXPOLY_BRIDGE_AUTHORITY_SHA": authority_sha,
                "NEXPOLY_PREFETCH_OPERATION_ID": prefetch_operation_id,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        os.execve(
            "/usr/bin/python3",
            [
                "/usr/bin/python3",
                "-I",
                "-B",
                str(controller_path),
                "bridge-prepare",
                "--authority-sha",
                authority_sha,
                "--operation-id",
                operation_id,
                "--prefetch-operation-id",
                prefetch_operation_id,
            ],
            environment,
        )

    def _validate_bridge_prepare_handoff(
        self,
        *,
        authority_sha: str,
        operation_id: str,
        prefetch_operation_id: str,
        materialize: bool,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Validate the handoff, importing its bundle only while locked."""

        handoff_operation = os.environ.get("NEXPOLY_PREPARE_HANDOFF_OPERATION")
        handoff_digest = os.environ.get("NEXPOLY_PREPARE_HANDOFF_SHA256")
        environment_authority = os.environ.get("NEXPOLY_BRIDGE_AUTHORITY_SHA")
        environment_prefetch = os.environ.get(
            "NEXPOLY_PREFETCH_OPERATION_ID"
        )
        if (
            handoff_operation != operation_id
            or environment_authority != authority_sha
            or environment_prefetch != prefetch_operation_id
            or not isinstance(handoff_digest, str)
        ):
            raise PullDeployError("bridge prepare lacks a selector-sealed handoff")
        handoff_path = self.control_handoffs_dir / f"{operation_id}.json"
        if sha256_file(handoff_path) != require_digest(
            handoff_digest, "bridge prepare handoff digest"
        ):
            raise PullDeployError("bridge prepare handoff record changed")
        record = load_private_json(handoff_path)
        required = {
            "schema_version",
            "protocol_version",
            "operation_id",
            "authority_sha",
            "authority_tree",
            "target_sha",
            "target_tree",
            "target_ref",
            "policy_id",
            "policy_sha256",
            "prefetch_operation_id",
            "prefetch",
            "legacy_takeover",
            "previous_active_control",
            "previous_active_control_sha256",
            "executor_control",
            "executor_control_sha256",
            "created_at",
        }
        if (
            set(record) != required
            or record.get("schema_version") != 2
            or record.get("protocol_version") != _control_runtime.PROTOCOL_VERSION
            or record.get("operation_id") != operation_id
            or record.get("authority_sha") != authority_sha
            or record.get("prefetch_operation_id")
            != prefetch_operation_id
            or canonical_json_digest(record.get("executor_control"))
            != record.get("executor_control_sha256")
            or canonical_json_digest(record.get("previous_active_control"))
            != record.get("previous_active_control_sha256")
        ):
            raise PullDeployError("bridge prepare handoff identity is invalid")
        ready, prefetch = self.maintenance_prefetch_evidence(
            prefetch_operation_id,
            authority_sha=authority_sha,
            authority_tree=record["authority_tree"],
            target_sha=record["target_sha"],
            target_tree=record["target_tree"],
            policy_sha256=record["policy_sha256"],
        )
        relation = None
        if materialize:
            self._require_deploy_lock_for_staging()
            relation = self.materialize_prefetched_bridge_relation(
                ready,
                create_target_ref=True,
            )
        current = self.repository_identity(require_ssh_origin=True)
        takeover = self.completed_legacy_takeover_evidence(
            authority_sha=authority_sha,
            authority_tree=record["authority_tree"],
            expected_repository=current,
        )
        if record["prefetch"] != prefetch or record[
            "legacy_takeover"
        ] != takeover:
            raise PullDeployError(
                "bridge policy changed across the target-controller handoff"
            )
        if relation is not None and (
            record["authority_tree"]
            != relation["relation"]["authority_tree"]
            or record["target_sha"] != relation["policy"]["target_sha"]
            or record["target_tree"] != relation["policy"]["target_tree"]
            or record["target_ref"] != relation["policy"]["target_ref"]
            or record["policy_id"] != relation["policy"]["policy_id"]
            or record["policy_sha256"] != relation["policy_sha256"]
        ):
            raise PullDeployError(
                "bridge policy changed across the target-controller handoff"
            )
        try:
            candidate, manifest, release_root = (
                _control_runtime.load_candidate_control(
                    self.runtime_root, record["executor_control"]
                )
            )
        except Exception as exc:
            raise PullDeployError(
                "bridge candidate control release is invalid"
            ) from exc
        if (
            candidate["operation_id"] != operation_id
            or candidate["source_sha"] != record["target_sha"]
            or candidate["source_tree"] != record["target_tree"]
            or Path(__file__).resolve().parent != release_root.resolve()
            or self.active_control_evidence() != record["previous_active_control"]
            or record["previous_active_control"]["source_sha"] != authority_sha
            or record["previous_active_control"]["source_tree"]
            != record["authority_tree"]
            or BRIDGE_DESCRIPTOR_SCHEMA_VERSION
            not in manifest["compatibility"]["descriptor_schema_versions"]
        ):
            raise PullDeployError(
                "bridge candidate prepare is not executing sealed B controls"
            )
        return record, relation

    def _validate_prepare_handoff(
        self, *, target_sha: str, operation_id: str
    ) -> dict[str, Any]:
        handoff_operation = os.environ.get("NEXPOLY_PREPARE_HANDOFF_OPERATION")
        handoff_digest = os.environ.get("NEXPOLY_PREPARE_HANDOFF_SHA256")
        if handoff_operation != operation_id or not isinstance(handoff_digest, str):
            raise PullDeployError("candidate prepare lacks a selector-sealed handoff")
        handoff_path = self.control_handoffs_dir / f"{operation_id}.json"
        if sha256_file(handoff_path) != require_digest(
            handoff_digest, "prepare handoff digest"
        ):
            raise PullDeployError("candidate prepare handoff record changed")
        record = load_private_json(handoff_path)
        required = {
            "schema_version",
            "protocol_version",
            "operation_id",
            "target_sha",
            "target_tree",
            "previous_active_control",
            "previous_active_control_sha256",
            "executor_control",
            "executor_control_sha256",
            "created_at",
        }
        if (
            set(record) != required
            or record.get("schema_version") != 1
            or record.get("protocol_version") != _control_runtime.PROTOCOL_VERSION
            or record.get("operation_id") != operation_id
            or record.get("target_sha") != target_sha
            or canonical_json_digest(record.get("executor_control"))
            != record.get("executor_control_sha256")
            or canonical_json_digest(record.get("previous_active_control"))
            != record.get("previous_active_control_sha256")
        ):
            raise PullDeployError("candidate prepare handoff identity is invalid")
        try:
            candidate, _manifest, release_root = (
                _control_runtime.load_candidate_control(
                    self.runtime_root, record["executor_control"]
                )
            )
        except Exception as exc:
            raise PullDeployError(
                "candidate prepare control release is invalid"
            ) from exc
        if (
            candidate["operation_id"] != operation_id
            or candidate["source_sha"] != target_sha
            or candidate["source_tree"] != record["target_tree"]
            or Path(__file__).resolve().parent != release_root.resolve()
            or self.active_control_evidence() != record["previous_active_control"]
        ):
            raise PullDeployError(
                "candidate prepare is not executing the sealed handoff"
            )
        return record

    def prepare(
        self,
        *,
        target_sha: str | None,
        operation_id: str,
        bridge_authority_sha: str | None = None,
        prefetch_operation_id: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_roots(mutating=True)
        operation_id = require_operation_id(operation_id)
        bridge_relation: dict[str, Any] | None = None
        bridge_handoff: dict[str, Any] | None = None
        bridge_prefetch_ready: dict[str, Any] | None = None
        bridge_prefetch_binding: dict[str, Any] | None = None
        bridge_takeover_binding: dict[str, Any] | None = None
        bridge_requested = bridge_authority_sha is not None
        if bridge_requested:
            authority_sha = require_sha(
                bridge_authority_sha, "bridge authority SHA"
            )
            if target_sha is not None:
                raise PullDeployError(
                    "bridge target is derived from policy, not caller input"
                )
            if prefetch_operation_id is None:
                raise PullDeployError(
                    "bridge prepare requires exact maintenance prefetch evidence"
                )
            if not self.apply_enabled:
                return self.bridge_plan(
                    authority_sha=authority_sha,
                    operation_id=operation_id,
                    prefetch_operation_id=prefetch_operation_id,
                )
            if (
                not self.test_root_mode
                and os.environ.get("NEXPOLY_PREPARE_HANDOFF_OPERATION") is None
            ):
                self._handoff_prepare_to_bridge_controller(
                    authority_sha=authority_sha,
                    operation_id=operation_id,
                    prefetch_operation_id=prefetch_operation_id,
                )
                raise PullDeployError(
                    "bridge target controller prepare handoff returned unexpectedly"
                )
            if not self.test_root_mode:
                bridge_handoff, bridge_relation = (
                    self._validate_bridge_prepare_handoff(
                        authority_sha=authority_sha,
                        operation_id=operation_id,
                        prefetch_operation_id=prefetch_operation_id,
                        materialize=False,
                    )
                )
                bridge_prefetch_binding = validate_prefetch_binding(
                    bridge_handoff["prefetch"]
                )
                bridge_takeover_binding = (
                    validate_legacy_takeover_binding(
                        bridge_handoff["legacy_takeover"]
                    )
                )
            else:
                bridge_prefetch_ready, bridge_prefetch_binding = (
                    self.maintenance_prefetch_evidence(
                        prefetch_operation_id,
                        authority_sha=authority_sha,
                        authority_tree=None,
                    )
                )
            target_sha = require_sha(
                (
                    bridge_relation["policy"]["target_sha"]
                    if bridge_relation is not None
                    else bridge_handoff["target_sha"]
                    if bridge_handoff is not None
                    else bridge_prefetch_ready["source"]["target"]["sha"]
                ),
                "bridge target SHA",
            )
        else:
            target_sha = require_sha(target_sha, "target SHA")
        if not self.apply_enabled:
            return {
                **self.plan(target_sha=target_sha, operation_id=operation_id),
                "action": "prepare",
            }
        handoff_record: dict[str, Any] | None = None
        if (
            not self.test_root_mode
            and bridge_relation is None
            and bridge_handoff is None
        ):
            if os.environ.get("NEXPOLY_PREPARE_HANDOFF_OPERATION") is None:
                self._handoff_prepare_to_target_controller(
                    target_sha=target_sha, operation_id=operation_id
                )
                raise PullDeployError(
                    "target controller prepare handoff returned unexpectedly"
                )
            handoff_record = self._validate_prepare_handoff(
                target_sha=target_sha, operation_id=operation_id
            )
        with self.deployment_lock():
            self._require_no_contract_maintenance(require_alias_completed=False)
            operation, descriptor_path, ready_path = self._operation_paths(
                operation_id
            )
            self._assert_existing_prepare_command_mode(
                operation_id=operation_id,
                bridge_requested=bridge_requested,
            )
            if bridge_handoff is not None:
                bridge_handoff, bridge_relation = (
                    self._validate_bridge_prepare_handoff(
                        authority_sha=authority_sha,
                        operation_id=operation_id,
                        prefetch_operation_id=prefetch_operation_id,
                        materialize=True,
                    )
                )
                bridge_prefetch_binding = validate_prefetch_binding(
                    bridge_handoff["prefetch"]
                )
                bridge_takeover_binding = (
                    validate_legacy_takeover_binding(
                        bridge_handoff["legacy_takeover"]
                    )
                )
            elif bridge_prefetch_ready is not None:
                bridge_relation = self.materialize_prefetched_bridge_relation(
                    bridge_prefetch_ready,
                    create_target_ref=True,
                )
            elif handoff_record is not None:
                # Revalidate after acquiring the lock; pre-lock validation is
                # intentionally not authority for descriptor preparation.
                handoff_record = self._validate_prepare_handoff(
                    target_sha=target_sha, operation_id=operation_id
                )
            if bridge_relation is not None:
                materialized_target = require_sha(
                    bridge_relation["policy"]["target_sha"],
                    "materialized bridge target SHA",
                )
                if materialized_target != target_sha:
                    raise PullDeployError(
                        "bridge target changed while acquiring deploy.lock"
                    )
            if self.marker_path.exists() or self.marker_path.is_symlink():
                raise PullDeployError(
                    "an interrupted deployment must be recovered before prepare"
                )
            self._assert_operation_not_terminal(operation_id, action="prepare")
            if ready_path.exists() or ready_path.is_symlink():
                raw_descriptor = load_private_json(descriptor_path)
                descriptor_is_bridge = (
                    raw_descriptor.get("schema_version")
                    == BRIDGE_DESCRIPTOR_SCHEMA_VERSION
                )
                if descriptor_is_bridge != (
                    bridge_relation is not None
                ):
                    raise PullDeployError(
                        "prepared operation requires its original "
                        "ordinary or bridge command mode"
                    )
                descriptor = validate_descriptor(raw_descriptor)
                ready = load_private_json(ready_path)
                self._validate_ready(ready, descriptor, descriptor_path)
                if descriptor["repository"]["target_sha"] != target_sha:
                    raise PullDeployError(
                        "operation ID is already prepared for another target"
                    )
                if bridge_relation is not None and (
                    descriptor.get("schema_version")
                    != BRIDGE_DESCRIPTOR_SCHEMA_VERSION
                    or descriptor["bridge"]["authority"]["sha"] != authority_sha
                    or descriptor["bridge"]["policy"]
                    != bridge_relation["policy"]
                ):
                    raise PullDeployError(
                        "operation ID is already prepared for another bridge authority"
                    )
                if bridge_relation is not None:
                    if (
                        descriptor["prefetch"]["operation_id"]
                        != prefetch_operation_id
                    ):
                        raise PullDeployError(
                            "operation is prepared for another prefetch authority"
                        )
                    self._revalidate_bridge_external_authorities(
                        descriptor
                    )
                    try:
                        alias_marker = (
                            _control_runtime.load_production_0005_alias_gate(
                                self.runtime_root,
                                require_completed=False,
                            )
                        )
                    except Exception as exc:
                        raise PullDeployError(
                            "prepared bridge alias gate is invalid"
                        ) from exc
                    if (
                        isinstance(alias_marker, dict)
                        and alias_marker.get("phase") == "completed"
                    ):
                        baseline, _pair, _reference = (
                            self._completed_alias_external_database_baseline(
                                descriptor
                            )
                        )
                        self._revalidate_external_database_binding(
                            baseline,
                            policy=descriptor["bridge"]["policy"][
                                "external_database_audit"
                            ],
                        )
                    else:
                        self._revalidate_external_database_audit(
                            descriptor
                        )
                    self._bind_bridge_descriptor_token(
                        descriptor,
                        descriptor_path,
                    )
                return {"action": "prepare", "status": "already-ready", **ready}
            if descriptor_path.exists() or descriptor_path.is_symlink():
                attempt = self._open_prepare_operation(
                    operation,
                    operation_id=operation_id,
                    target_sha=target_sha,
                )
                try:
                    ready = self._resume_descriptor_without_ready(
                        descriptor_path=descriptor_path,
                        ready_path=ready_path,
                        operation_id=operation_id,
                        target_sha=target_sha,
                        bridge_relation=bridge_relation,
                        authority_sha=(
                            authority_sha
                            if bridge_relation is not None
                            else None
                        ),
                        prefetch_operation_id=prefetch_operation_id,
                    )
                    atomic_json(
                        operation / "prepare-attempt.json",
                        {
                            "schema_version": 1,
                            "operation_id": operation_id,
                            "target_sha": target_sha,
                            "attempt": attempt,
                            "status": "ready",
                            "started_at": load_private_json(
                                operation / "prepare-attempt.json"
                            )["started_at"],
                            "completed_at": utc_now(),
                        },
                    )
                    return {
                        "action": "prepare",
                        "status": "resumed-descriptor",
                        **ready,
                    }
                except BaseException as exc:
                    atomic_json(
                        operation / "prepare-attempt.json",
                        {
                            "schema_version": 1,
                            "operation_id": operation_id,
                            "target_sha": target_sha,
                            "attempt": attempt,
                            "status": "failed",
                            "failed_at": utc_now(),
                            "error": str(exc)[:500],
                        },
                    )
                    raise
            attempt = self._open_prepare_operation(
                operation,
                operation_id=operation_id,
                target_sha=target_sha,
            )
            try:
                production_config = self.production_config_evidence(
                    check_free_space=True
                )
                previous: dict[str, Any] | None = None
                previous_digest: str | None = None
                if (
                    self.current_state_path.exists()
                    or self.current_state_path.is_symlink()
                ):
                    previous_digest = sha256_file(self.current_state_path)
                    previous = self._validate_steady_deployment_state(
                        load_private_json(self.current_state_path)
                    )
                if bridge_relation is not None and previous is not None:
                    raise PullDeployError(
                        "historical bridge is restricted to first governed takeover"
                    )
                anchor = {
                    "previous_deployment": previous,
                    "previous_deployment_sha256": previous_digest,
                }
                self._revalidate_previous_deployment_state(anchor)
                if previous is not None:
                    self._revalidate_materialized_images(
                        previous["images"],
                        source_sha=previous["source_sha"],
                        pull=True,
                    )
                # Configuration and credential digests are a per-operation
                # compare-and-swap fence, not permanent runtime identity.
                # A later deployment must be able to seal an intentional
                # password/token/known-hosts rotation.  The previous state
                # retains its old evidence for audit and rollback analysis;
                # _revalidate_pre_switch() prevents drift after this prepare.
                self._assert_no_ignored_runtime()
                current = self.repository_identity(require_ssh_origin=True)
                previous_active_control = self.active_control_evidence()
                if (
                    handoff_record is not None
                    and previous_active_control
                    != handoff_record["previous_active_control"]
                ):
                    raise PullDeployError(
                        "active controls changed after target prepare handoff"
                    )
                if (
                    bridge_handoff is not None
                    and previous_active_control
                    != bridge_handoff["previous_active_control"]
                ):
                    raise PullDeployError(
                        "active controls changed after bridge prepare handoff"
                    )
                if previous is not None and (
                    previous_active_control["source_sha"] != current["sha"]
                    or previous_active_control["source_tree"] != current["tree"]
                ):
                    raise PullDeployError(
                        "active controls differ from the production source identity"
                    )
                if bridge_relation is not None:
                    (
                        bridge_prefetch_ready,
                        current_prefetch_binding,
                    ) = self.maintenance_prefetch_evidence(
                        prefetch_operation_id,
                        authority_sha=authority_sha,
                        authority_tree=bridge_relation["relation"][
                            "authority_tree"
                        ],
                        target_sha=target_sha,
                        target_tree=bridge_relation["policy"][
                            "target_tree"
                        ],
                        policy_sha256=bridge_relation["policy_sha256"],
                    )
                    bridge_relation = (
                        self.materialize_prefetched_bridge_relation(
                            bridge_prefetch_ready,
                            create_target_ref=True,
                        )
                    )
                    current_takeover_binding = (
                        self.completed_legacy_takeover_evidence(
                            authority_sha=authority_sha,
                            authority_tree=bridge_relation["relation"][
                                "authority_tree"
                            ],
                            expected_repository=current,
                        )
                    )
                    if (
                        bridge_prefetch_binding is not None
                        and bridge_prefetch_binding
                        != current_prefetch_binding
                    ) or (
                        bridge_takeover_binding is not None
                        and bridge_takeover_binding
                        != current_takeover_binding
                    ):
                        raise PullDeployError(
                            "takeover or prefetch changed during bridge prepare"
                        )
                    bridge_prefetch_binding = current_prefetch_binding
                    bridge_takeover_binding = current_takeover_binding
                    if target_sha != bridge_relation["policy"]["target_sha"]:
                        raise PullDeployError(
                            "bridge target changed during preparation"
                        )
                    target_tree = bridge_relation["policy"]["target_tree"]
                    if (
                        previous_active_control["source_sha"] != authority_sha
                        or previous_active_control["source_tree"]
                        != bridge_relation["relation"]["authority_tree"]
                    ):
                        raise PullDeployError(
                            "active bootstrap controls changed from authority F"
                        )
                else:
                    if self.remote_main() != target_sha:
                        raise PullDeployError(
                            "requested target is no longer current remote main"
                        )
                    target_tree = self.fetch_target(target_sha, operation_id)
                    if (
                        handoff_record is not None
                        and target_tree != handoff_record["target_tree"]
                    ):
                        raise PullDeployError(
                            "target tree changed after target prepare handoff"
                        )
                self.validate_installed_controls_against_target(target_sha)
                executor_control = (
                    bridge_handoff["executor_control"]
                    if bridge_handoff is not None
                    else handoff_record["executor_control"]
                    if handoff_record is not None
                    else self.prepare_control_release(
                        operation_id=operation_id,
                        target_sha=target_sha,
                        target_tree=target_tree,
                    )
                )
                try:
                    _candidate, candidate_manifest, _candidate_root = (
                        _control_runtime.load_candidate_control(
                            self.runtime_root, executor_control
                        )
                    )
                except Exception as exc:
                    raise PullDeployError(
                        "prepared candidate controls are unavailable"
                    ) from exc
                if (
                    bridge_relation is not None
                    and BRIDGE_DESCRIPTOR_SCHEMA_VERSION
                    not in candidate_manifest["compatibility"][
                        "descriptor_schema_versions"
                    ]
                ):
                    raise PullDeployError(
                        "prepared B controls do not support bridge descriptor v3"
                    )
                release_input, migrations, compose, lock_payload = (
                    self._source_evidence(target_sha)
                )
                release_input["asset"] = self.asset_evidence(
                    release_input["asset_manifest_digest"]
                )
                if bridge_relation is not None:
                    if (
                        bridge_prefetch_ready is None
                        or bridge_prefetch_binding is None
                        or bridge_takeover_binding is None
                    ):
                        raise PullDeployError(
                            "bridge prepare lost takeover or prefetch authority"
                        )
                    ci = self.bootstrap_ci_evidence(
                        authority_sha=authority_sha,
                        required_jobs=bridge_relation["policy"][
                            "required_ci_jobs"
                        ],
                    )
                    images = self.prefetched_application_images(
                        bridge_prefetch_ready,
                        target_sha=target_sha,
                    )
                    postgres_restore_image = (
                        self.prefetched_postgres_restore_image(
                            bridge_prefetch_ready
                        )
                    )
                    target_wheels = [
                        record
                        for record in bridge_prefetch_ready[
                            "wheel_caches"
                        ]
                        if record["source_sha"] == target_sha
                    ]
                    if (
                        len(target_wheels) != 1
                        or target_wheels[0]["source_tree"] != target_tree
                        or target_wheels[0]["worker_lock_sha256"]
                        != sha256_bytes(lock_payload)
                    ):
                        raise PullDeployError(
                            "prefetched B wheel cache differs from source lock"
                        )
                else:
                    ci = self.ci_evidence(target_sha)
                    images = {
                        "backend": self.image_evidence(
                            "backend", target_sha
                        ),
                        "web": self.image_evidence("web", target_sha),
                    }
                    postgres_restore_image = (
                        self.postgres_restore_image_evidence()
                    )
                worker_controls = self.prepare_worker_controls(
                    operation_id=operation_id,
                    target_sha=target_sha,
                    executor_control=executor_control,
                )
                # Recheck the active pointer/state immediately before the
                # only operation that may recycle an inactive A/B slot.
                self._revalidate_previous_deployment_state(anchor)
                slot_record = self.prepare_md_slot(
                    operation_id=operation_id,
                    target_sha=target_sha,
                    target_tree=target_tree,
                    lock_payload=lock_payload,
                )
                self._revalidate_previous_deployment_state(anchor)
                token_record: dict[str, Any] | None = None
                bridge_descriptor: dict[str, Any] | None = None
                external_database_audit: dict[str, Any] | None = None
                if bridge_relation is not None:
                    target_digest_refs = {
                        role: images[role]["digest_ref"]
                        for role in ("backend", "web")
                    }
                    if (
                        target_digest_refs
                        != bridge_relation["policy"]["target_images"]
                        or release_input["asset_manifest_digest"]
                        != bridge_relation["policy"]["asset_manifest_digest"]
                        or release_input["datasets_on_asset_change"]
                        != bridge_relation["policy"]["datasets_on_asset_change"]
                    ):
                        raise PullDeployError(
                            "materialized B images or asset differ from F policy"
                        )
                    external_database_audit = (
                        self.external_database_audit_evidence(
                            bridge_relation["policy"][
                                "external_database_audit"
                            ]
                        )
                    )
                    try:
                        token_record = self._plan_current_bridge_token(
                            authority_sha=authority_sha,
                            operation_id=operation_id,
                            policy_id=bridge_relation["policy"]["policy_id"],
                        )
                        bridge_descriptor = _bridge_core.build_bridge_descriptor(
                            operation_id=operation_id,
                            authority_sha=authority_sha,
                            authority_tree=bridge_relation["relation"][
                                "authority_tree"
                            ],
                            authority_control_release_id=previous_active_control[
                                "release_id"
                            ],
                            ci_evidence=ci,
                            target_control_release_id=executor_control["release_id"],
                            policy=bridge_relation["policy"],
                            token_id=token_record["token_id"],
                            token_sha256=token_record["token_sha256"],
                        )
                    except Exception as exc:
                        if isinstance(exc, PullDeployError):
                            raise
                        raise PullDeployError(
                            "cannot reserve exact bridge takeover authority"
                        ) from exc
                descriptor = {
                    "schema_version": (
                        BRIDGE_DESCRIPTOR_SCHEMA_VERSION
                        if bridge_descriptor is not None
                        else DESCRIPTOR_SCHEMA_VERSION
                    ),
                    "operation_id": operation_id,
                    "controller": {
                        "schema_version": CONTROLLER_SCHEMA_VERSION,
                        "sha256": candidate_manifest["files"][
                            "pull_deploy_controller.py"
                        ]["sha256"],
                        "helpers": self.stable_helper_evidence(),
                        "executor_control": executor_control,
                        "executor_control_sha256": canonical_json_digest(
                            executor_control
                        ),
                        "previous_active_control": previous_active_control,
                        "previous_active_control_sha256": canonical_json_digest(
                            previous_active_control
                        ),
                    },
                    "repository": {
                        "path": str(self.production_root),
                        "remote": REPOSITORY_SSH_URL,
                        "previous_sha": current["sha"],
                        "previous_tree": current["tree"],
                        "target_sha": target_sha,
                        "target_tree": target_tree,
                    },
                    "ci": ci,
                    "images": images,
                    "postgres_restore_image": postgres_restore_image,
                    "release_input": release_input,
                    "migrations": migrations,
                    "compose": compose,
                    "production_config": production_config,
                    "mutable_data": self.mutable_data_contract(),
                    "monomer_md": {
                        "slot": slot_record["slot"],
                        "slot_record": slot_record,
                        "slot_record_sha256": worker_record_digest(slot_record),
                        **worker_controls,
                    },
                    "previous_deployment": previous,
                    "previous_deployment_sha256": previous_digest,
                    "prepared_at": (
                        token_record["prepared_at"]
                        if token_record is not None
                        else utc_now()
                    ),
                }
                if bridge_descriptor is not None:
                    descriptor["bridge"] = bridge_descriptor
                    descriptor["legacy_takeover"] = (
                        bridge_takeover_binding
                    )
                    descriptor["prefetch"] = bridge_prefetch_binding
                    descriptor["external_database_audit"] = (
                        external_database_audit
                    )
                validate_descriptor(descriptor)
                if descriptor_path.exists() or descriptor_path.is_symlink():
                    existing_descriptor = validate_descriptor(
                        load_private_json(descriptor_path)
                    )
                    if existing_descriptor != descriptor:
                        raise PullDeployError(
                            "interrupted bridge descriptor differs on retry"
                        )
                else:
                    atomic_json(descriptor_path, descriptor)
                if bridge_descriptor is not None:
                    self._bind_bridge_descriptor_token(
                        descriptor,
                        descriptor_path,
                    )
                ready = {
                    "schema_version": 1,
                    "status": "ready",
                    "operation_id": operation_id,
                    "source_sha": target_sha,
                    "descriptor_sha256": sha256_file(descriptor_path),
                    "slot_record_sha256": descriptor["monomer_md"][
                        "slot_record_sha256"
                    ],
                    "executor_control": executor_control,
                    "executor_control_sha256": canonical_json_digest(executor_control),
                    "prepared_at": utc_now(),
                }
                atomic_json(ready_path, ready)
                self._validate_ready(ready, descriptor, descriptor_path)
                atomic_json(
                    operation / "prepare-attempt.json",
                    {
                        "schema_version": 1,
                        "operation_id": operation_id,
                        "target_sha": target_sha,
                        "attempt": attempt,
                        "status": "ready",
                        "started_at": load_private_json(
                            operation / "prepare-attempt.json"
                        )["started_at"],
                        "completed_at": utc_now(),
                    },
                )
                return {"action": "prepare", **ready}
            except BaseException as exc:
                atomic_json(
                    operation / "prepare-attempt.json",
                    {
                        "schema_version": 1,
                        "operation_id": operation_id,
                        "target_sha": target_sha,
                        "attempt": attempt,
                        "status": "failed",
                        "failed_at": utc_now(),
                        "error": str(exc)[:500],
                    },
                )
                # Prepared venv ownership records are retained for safe retry;
                # an unsealed operation directory is never accepted by apply.
                raise

    def _validate_ready(
        self,
        ready: dict[str, Any],
        descriptor: dict[str, Any],
        descriptor_path: Path,
    ) -> None:
        if (
            set(ready) != READY_FIELDS
            or ready.get("schema_version") != 1
            or ready.get("status") != "ready"
        ):
            raise PullDeployError(
                "prepared deployment READY record has an invalid shape"
            )
        if (
            ready.get("operation_id") != descriptor["operation_id"]
            or ready.get("source_sha") != descriptor["repository"]["target_sha"]
            or ready.get("descriptor_sha256") != sha256_file(descriptor_path)
            or ready.get("slot_record_sha256")
            != descriptor["monomer_md"]["slot_record_sha256"]
            or ready.get("executor_control")
            != descriptor["controller"]["executor_control"]
            or ready.get("executor_control_sha256")
            != descriptor["controller"]["executor_control_sha256"]
        ):
            raise PullDeployError(
                "prepared deployment READY record differs from descriptor"
            )

    def _resume_descriptor_without_ready(
        self,
        *,
        descriptor_path: Path,
        ready_path: Path,
        operation_id: str,
        target_sha: str,
        bridge_relation: dict[str, Any] | None,
        authority_sha: str | None,
        prefetch_operation_id: str | None,
    ) -> dict[str, Any]:
        """Finish descriptor -> token -> READY after an interrupted rename."""

        if ready_path.exists() or ready_path.is_symlink():
            raise PullDeployError(
                "descriptor-only prepare recovery received an existing READY"
            )
        raw_descriptor = load_private_json(descriptor_path)
        descriptor_is_bridge = (
            raw_descriptor.get("schema_version")
            == BRIDGE_DESCRIPTOR_SCHEMA_VERSION
        )
        if descriptor_is_bridge != (bridge_relation is not None):
            raise PullDeployError(
                "interrupted descriptor requires its original ordinary or "
                "bridge command mode"
            )
        descriptor = validate_descriptor(raw_descriptor)
        if (
            descriptor["operation_id"] != operation_id
            or descriptor["repository"]["target_sha"] != target_sha
            or descriptor_path.parent.name != operation_id
        ):
            raise PullDeployError(
                "interrupted descriptor belongs to another prepare"
            )
        if (
            descriptor["controller"]["sha256"] != self.controller_digest()
            or descriptor["controller"]["helpers"]
            != self.stable_helper_evidence()
        ):
            raise PullDeployError(
                "interrupted descriptor controller authority changed"
            )
        self._revalidate_worker_controls(descriptor)
        if (
            self.asset_evidence(
                descriptor["release_input"]["asset_manifest_digest"]
            )
            != descriptor["release_input"]["asset"]
        ):
            raise PullDeployError(
                "interrupted descriptor asset authority changed"
            )
        if bridge_relation is not None:
            bridge = descriptor.get("bridge")
            if (
                descriptor.get("schema_version")
                != BRIDGE_DESCRIPTOR_SCHEMA_VERSION
                or not isinstance(bridge, dict)
                or bridge["authority"]["sha"] != authority_sha
                or bridge["policy"] != bridge_relation["policy"]
                or descriptor["prefetch"]["operation_id"]
                != prefetch_operation_id
            ):
                raise PullDeployError(
                    "interrupted descriptor differs from bridge handoff"
                )
            self._revalidate_bridge_external_authorities(descriptor)
            try:
                alias_marker = (
                    _control_runtime.load_production_0005_alias_gate(
                        self.runtime_root,
                        require_completed=False,
                    )
                )
            except Exception as exc:
                raise PullDeployError(
                    "interrupted alias gate is invalid"
                ) from exc
            if (
                isinstance(alias_marker, dict)
                and alias_marker.get("phase") == "completed"
            ):
                baseline, _pair, _reference = (
                    self._completed_alias_external_database_baseline(
                        descriptor
                    )
                )
                self._revalidate_external_database_binding(
                    baseline,
                    policy=bridge["policy"][
                        "external_database_audit"
                    ],
                )
            else:
                self._revalidate_external_database_audit(descriptor)
            self._bind_bridge_descriptor_token(
                descriptor,
                descriptor_path,
            )
        ready = {
            "schema_version": 1,
            "status": "ready",
            "operation_id": operation_id,
            "source_sha": target_sha,
            "descriptor_sha256": sha256_file(descriptor_path),
            "slot_record_sha256": descriptor["monomer_md"][
                "slot_record_sha256"
            ],
            "executor_control": descriptor["controller"][
                "executor_control"
            ],
            "executor_control_sha256": descriptor["controller"][
                "executor_control_sha256"
            ],
            "prepared_at": utc_now(),
        }
        atomic_json(ready_path, ready)
        self._validate_ready(ready, descriptor, descriptor_path)
        return ready

    @staticmethod
    def _legacy_contract_state_projection(
        state: dict[str, Any],
    ) -> dict[str, Any]:
        projected = json.loads(json.dumps(state))
        history = projected.get("migrations")
        if not isinstance(history, list) or any(
            not isinstance(record, dict)
            or not isinstance(record.get("version"), str)
            for record in history
        ):
            raise PullDeployError(
                "contract recovery previous state has invalid migrations"
            )
        projected["migrations"] = [
            record["version"] for record in history
        ]
        return projected

    def _contract_recovery_external_database_endpoint(
        self,
        *,
        descriptor: dict[str, Any],
        descriptor_digest: str,
        bridge_baseline: dict[str, Any],
        current_state: dict[str, Any],
        recovery_operation_id: str | None,
    ) -> tuple[dict[str, Any], bool]:
        """Select the live endpoint for one exact in-progress 0012 operation.

        The boolean result is true only for an unknown database commit/restore
        window.  Callers may use that window to enter the already-fenced,
        idempotent recovery state machine; it is never authority to commit a
        deployment state or reopen admission.
        """

        if recovery_operation_id is None:
            return bridge_baseline, False
        recovery_operation_id = require_operation_id(
            recovery_operation_id
        )
        if not (
            self.contract_marker_path.exists()
            or self.contract_marker_path.is_symlink()
        ):
            return bridge_baseline, False
        marker = load_private_json(self.contract_marker_path)
        authority = (
            marker.get("pull_maintenance_authority")
            if isinstance(marker, dict)
            else None
        )
        expected_baseline = {
            "identity_sha256": bridge_baseline["identity_sha256"],
            "state_sha256": bridge_baseline["state_sha256"],
            "helper_sha256": bridge_baseline["helper"]["sha256"],
            "helper_control_sha256": canonical_json_digest(
                bridge_baseline["helper_control"]
            ),
            "authority_rules_sha256": bridge_baseline[
                "authority_rules"
            ]["sha256"],
            "role_sql_sha256": bridge_baseline["role_sql"]["sha256"],
            "role_sql_authority_sha256": canonical_json_digest(
                bridge_baseline["role_sql"]
            ),
            "role_provisioning_evidence_sha256": bridge_baseline[
                "role_provisioning"
            ]["evidence_sha256"],
            "registry_sha256": bridge_baseline["registry"]["sha256"],
        }
        expected_previous = self._legacy_contract_state_projection(
            current_state
        )
        if (
            not isinstance(marker, dict)
            or marker.get("schema_version") != 1
            or marker.get("operation_id") != recovery_operation_id
            or marker.get("deployment_operation_id")
            != descriptor["operation_id"]
            or marker.get("source_sha")
            != descriptor["repository"]["target_sha"]
            or marker.get("source_tree")
            != descriptor["repository"]["target_tree"]
            or marker.get("pull_descriptor_sha256")
            != descriptor_digest
            or marker.get("previous_state") != expected_previous
            or marker.get("status")
            not in {"running", "failed", "resume-pending"}
            or not isinstance(marker.get("drain_attempted"), bool)
            or not isinstance(
                marker.get("worker_drain_attempted"), bool
            )
            or not isinstance(authority, dict)
            or marker.get("pull_maintenance_authority_sha256")
            != canonical_json_digest(authority)
            or authority.get("schema_version") != 3
            or authority.get("source_sha")
            != descriptor["repository"]["target_sha"]
            or authority.get("source_tree")
            != descriptor["repository"]["target_tree"]
            or authority.get("pull_descriptor_sha256")
            != descriptor_digest
            or authority.get("active_control")
            != current_state.get("active_control")
            or authority.get("active_control_sha256")
            != canonical_json_digest(current_state.get("active_control"))
            or authority.get("production_config")
            != current_state.get("production_config")
            or authority.get("external_database_bridge_baseline")
            != expected_baseline
        ):
            raise PullDeployError(
                "0012 recovery marker differs from bridge authority"
            )
        runtime_verification = marker.get(
            "runtime_recovery_verification"
        )
        runtime_verification_digest = marker.get(
            "runtime_recovery_verification_sha256"
        )
        if (
            (runtime_verification is None)
            != (runtime_verification_digest is None)
            or runtime_verification is not None
            and canonical_json_digest(runtime_verification)
            != runtime_verification_digest
        ):
            raise PullDeployError(
                "0012 recovery runtime fence is invalid"
            )

        phase = marker.get("phase")
        database_started = marker.get("database_change_started")
        restore_started = marker.get("database_restore_started")
        restored = marker.get("database_restored")
        if not isinstance(database_started, bool):
            raise PullDeployError(
                "0012 recovery database-change flag is invalid"
            )
        if database_started is False:
            if (
                restore_started is not None
                or restored is not None
                or phase not in {"prepared", "drained", "backed-up"}
                or phase in {"drained", "backed-up"}
                and (
                    marker.get("drain_attempted") is not True
                    or marker.get("worker_drain_attempted") is not True
                )
            ):
                raise PullDeployError(
                    "0012 pre-change recovery phase is inconsistent"
                )
            return bridge_baseline, False
        if (
            marker.get("drain_attempted") is not True
            or marker.get("worker_drain_attempted") is not True
        ):
            raise PullDeployError(
                "0012 database recovery lacks its completed drain fence"
            )
        if phase == "database-restored":
            if (
                marker.get("status") != "failed"
                or restore_started is not True
                or restored is not True
            ):
                raise PullDeployError(
                    "0012 restored marker is inconsistent"
                )
            snapshot = marker.get("external_database_restored")
            try:
                validated_snapshot = (
                    _site_helper_contracts.validate_external_database_audit(
                        snapshot,
                        expected_users=bridge_baseline["expected_users"],
                        expected_media_authority_rules_digest=bridge_baseline[
                            "authority_rules"
                        ]["sha256"],
                        expected_runtime_registry_digest=bridge_baseline[
                            "registry"
                        ]["sha256"],
                    )
                )
            except Exception as exc:
                raise PullDeployError(
                    "0012 restored external database evidence is invalid"
                ) from exc
            restored_state = canonical_json_digest(
                external_database_audit_state(validated_snapshot)
            )
            if (
                marker.get("external_database_restored_state_sha256")
                != restored_state
                or restored_state != bridge_baseline["state_sha256"]
            ):
                raise PullDeployError(
                    "0012 restored external database differs from bridge"
                )
            return bridge_baseline, False
        if phase == "database-restore-started":
            if (
                marker.get("status") != "failed"
                or restore_started is not True
                or restored not in {None, False}
            ):
                raise PullDeployError(
                    "0012 database restore intent is inconsistent"
                )
            return bridge_baseline, True
        if restore_started is not None or restored is not None:
            raise PullDeployError(
                "0012 database restore flags lack an exact phase"
            )

        raw_pair = marker.get("contract_external_database_audit")
        if raw_pair is not None:
            pair = validate_external_database_contract_pair(
                raw_pair,
                before_binding=bridge_baseline,
            )
            if (
                pair["operation_id"] != recovery_operation_id
                or phase
                not in {
                    "database-change-started",
                    "contract-applied",
                    "verifying",
                }
            ):
                raise PullDeployError(
                    "0012 transition pair has an invalid recovery phase"
                )
            return (
                external_database_contract_after_binding(
                    bridge_baseline,
                    pair,
                ),
                False,
            )
        if (
            phase == "database-change-started"
            and marker.get("status") in {"running", "failed"}
        ):
            return bridge_baseline, True
        raise PullDeployError(
            "0012 database change lacks exact transition evidence"
        )

    def _load_prepared(
        self,
        operation_id: str,
        target_sha: str | None = None,
        *,
        bridge_token_statuses: frozenset[str] | None = None,
        contract_recovery_operation_id: str | None = None,
        allow_deployment_database_recovery: bool = False,
    ) -> tuple[dict[str, Any], str]:
        _operation, descriptor_path, ready_path = self._operation_paths(operation_id)
        descriptor = validate_descriptor(load_private_json(descriptor_path))
        previous_state = descriptor.get("previous_deployment")
        if previous_state is not None:
            if (
                self._validate_external_database_state_provenance(
                    previous_state
                )
                != previous_state
            ):
                raise PullDeployError(
                    "prepared previous deployment provenance differs"
                )
        ready = load_private_json(ready_path)
        self._validate_ready(ready, descriptor, descriptor_path)
        if descriptor["schema_version"] == BRIDGE_DESCRIPTOR_SCHEMA_VERSION:
            try:
                token = _bridge_core.load_token_authority(self.state_dir)
            except Exception as exc:
                raise PullDeployError(
                    "prepared bridge token authority is unavailable"
                ) from exc
            bridge = descriptor["bridge"]
            allowed_statuses = (
                bridge_token_statuses
                if bridge_token_statuses is not None
                else frozenset({"prepared", "commit-intent", "consumed"})
            )
            if (
                token["status"] not in allowed_statuses
                or token["operation_id"] != descriptor["operation_id"]
                or token["policy_id"] != bridge["policy"]["policy_id"]
                or token["descriptor_sha256"] != ready["descriptor_sha256"]
                or token["token_id"] != bridge["token"]["token_id"]
                or token["token_sha256"] != bridge["token"]["token_sha256"]
            ):
                raise PullDeployError(
                    "prepared bridge token differs from descriptor authority"
                )
            try:
                alias_marker = (
                    _control_runtime.load_production_0005_alias_gate(
                        self.runtime_root,
                        require_completed=True,
                    )
                )
                alias_identity = alias_marker.get("identity")
                alias_authority = (
                    alias_identity.get("bridge_authority")
                    if isinstance(alias_identity, dict)
                    else None
                )
                if not self.test_root_mode or alias_authority is not None:
                    validate_alias_bridge_authority(
                        alias_authority,
                        descriptor=descriptor,
                        descriptor_path=descriptor_path,
                        ready_path=ready_path,
                        state_root=self.state_dir,
                        current_token=token,
                    )
                    pre_expand_baseline, _pair, _reference = (
                        self._completed_alias_external_database_baseline(
                            descriptor
                        )
                    )
                    expected_live = pre_expand_baseline
                    pending_database_recovery = False
                    current_state: dict[str, Any] | None = None
                    bridge_endpoint: dict[str, Any] | None = None
                    if (
                        self.current_state_path.exists()
                        or self.current_state_path.is_symlink()
                    ):
                        raw_current = validate_current_deployment_state(
                            load_private_json(self.current_state_path)
                        )
                        if (
                            raw_current.get("operation_id")
                            == descriptor["operation_id"]
                            and raw_current.get("descriptor_sha256")
                            == ready["descriptor_sha256"]
                        ):
                            active_external = (
                                validate_external_database_audit_binding(
                                    raw_current.get(
                                        "external_database_audit"
                                    )
                                )
                            )
                            chain = (
                                validate_external_database_transition_chain(
                                    raw_current.get(
                                        "external_database_transition_chain"
                                    ),
                                    active_binding=active_external,
                                )
                            )
                            bridge_pair, _alias, alias_reference = (
                                self._load_bridge_external_database_transition(
                                    descriptor,
                                    chain["bridge"],
                                )
                            )
                            if (
                                chain["alias"] != alias_reference
                                or bridge_pair["after_binding"]
                                != active_external
                            ):
                                raise PullDeployError(
                                    "current bridge external database "
                                    "endpoint differs"
                                )
                            current_state = raw_current
                            bridge_endpoint = active_external
                            expected_live = active_external
                            raw_contract_pair = raw_current.get(
                                "contract_external_database_audit"
                            )
                            if raw_contract_pair is not None:
                                contract_pair = (
                                    validate_external_database_contract_pair(
                                        raw_contract_pair,
                                        before_binding=active_external,
                                    )
                                )
                                expected_live = (
                                    external_database_contract_after_binding(
                                        active_external,
                                        contract_pair,
                                    )
                                )
                    if self.marker_path.exists() or self.marker_path.is_symlink():
                        raw_marker = validate_recovery_marker(
                            load_private_json(self.marker_path),
                            descriptor=descriptor,
                            descriptor_digest=ready[
                                "descriptor_sha256"
                            ],
                        )
                        if (
                            raw_marker.get("database_change_started")
                            is True
                        ):
                            if raw_marker.get("runtime_stopped") is not True:
                                raise PullDeployError(
                                    "database recovery lacks stopped readers"
                                )
                            self._validate_database_backup(
                                descriptor,
                                raw_marker.get("database_backup"),
                                require_operation_backup=True,
                            )
                            postgres_fence = raw_marker.get(
                                "postgres_runtime_fence"
                            )
                            if postgres_fence is not None:
                                validate_postgres_runtime_fence(
                                    postgres_fence
                                )
                            elif not self._is_first_bridge(descriptor):
                                raise PullDeployError(
                                    "database recovery lacks PostgreSQL fence"
                                )
                        bridge_reference = raw_marker.get(
                            "bridge_external_database_audit"
                        )
                        if (
                            raw_marker.get("database_restore_started")
                            is True
                            and raw_marker.get("database_restored")
                            is not True
                        ):
                            pending_database_recovery = True
                        elif raw_marker.get("database_restored") is True:
                            expected_live = pre_expand_baseline
                        elif bridge_reference is not None:
                            bridge_pair, _alias, _alias_ref = (
                                self._load_bridge_external_database_transition(
                                    descriptor,
                                    bridge_reference,
                                )
                            )
                            expected_live = bridge_pair["after_binding"]
                        elif (
                            raw_marker.get("database_change_started")
                            is True
                            and (
                                raw_marker.get("phase")
                                == "migrations-started"
                                or raw_marker.get("phase") == "failed"
                                and raw_marker.get("failed_phase")
                                == "migrations-started"
                            )
                        ):
                            pending_database_recovery = True
                    if (
                        current_state is not None
                        and bridge_endpoint is not None
                        and current_state.get(
                            "contract_external_database_audit"
                        )
                        is None
                        and not (
                            self.marker_path.exists()
                            or self.marker_path.is_symlink()
                        )
                    ):
                        (
                            expected_live,
                            contract_pending,
                        ) = self._contract_recovery_external_database_endpoint(
                            descriptor=descriptor,
                            descriptor_digest=ready[
                                "descriptor_sha256"
                            ],
                            bridge_baseline=bridge_endpoint,
                            current_state=current_state,
                            recovery_operation_id=(
                                contract_recovery_operation_id
                            ),
                        )
                        pending_database_recovery = (
                            pending_database_recovery
                            or contract_pending
                        )
                    elif (
                        contract_recovery_operation_id is not None
                        and current_state is None
                    ):
                        raise PullDeployError(
                            "0012 recovery lacks its committed bridge state"
                        )
                    if (
                        pending_database_recovery
                        and not allow_deployment_database_recovery
                        and contract_recovery_operation_id is None
                    ):
                        raise PullDeployError(
                            "database recovery pending is not load authority"
                        )
                    if not pending_database_recovery:
                        self._revalidate_external_database_binding(
                            expected_live,
                            policy=descriptor["bridge"]["policy"][
                                "external_database_audit"
                            ],
                        )
            except Exception as exc:
                raise PullDeployError(
                    "completed production alias is not bound to this bridge"
                ) from exc
        if (
            target_sha is not None
            and descriptor["repository"]["target_sha"] != target_sha
        ):
            raise PullDeployError("prepared target SHA differs from requested target")
        if descriptor["controller"]["sha256"] != self.controller_digest():
            raise PullDeployError(
                "prepared operation must execute with the sealed target controller"
            )
        if descriptor["controller"]["helpers"] != self.stable_helper_evidence():
            raise PullDeployError("prepared stable control helpers changed")
        try:
            _candidate, manifest, candidate_root = (
                _control_runtime.load_candidate_control(
                    self.runtime_root, descriptor["controller"]["executor_control"]
                )
            )
        except Exception as exc:
            raise PullDeployError("prepared target controls changed") from exc
        if (
            manifest["files"]["pull_deploy_controller.py"]["sha256"]
            != descriptor["controller"]["sha256"]
            or canonical_json_digest(descriptor["controller"]["executor_control"])
            != descriptor["controller"]["executor_control_sha256"]
            or Path(__file__).resolve().parent != candidate_root.resolve()
            and not self.test_root_mode
        ):
            raise PullDeployError(
                "running controller differs from sealed target controls"
            )
        if descriptor["release_input"]["asset"]["pointer_path"] != str(
            self.state_dir / "current-assets"
        ):
            raise PullDeployError(
                "prepared descriptor uses another external asset root"
            )
        record_path = (
            self.slots_state_dir / f"md-{descriptor['monomer_md']['slot']}.json"
        )
        record = validate_slot_record(
            load_private_json(record_path), descriptor["monomer_md"]["slot"]
        )
        if (
            worker_record_digest(record)
            != descriptor["monomer_md"]["slot_record_sha256"]
        ):
            raise PullDeployError("prepared monomer MD slot record changed")
        self._revalidate_worker_controls(descriptor)
        return descriptor, ready["descriptor_sha256"]

    def _write_marker(self, marker: dict[str, Any]) -> None:
        atomic_json(self.marker_path, marker)

    @staticmethod
    def _sealed_runtime_verification(verification: object) -> dict[str, Any]:
        """Return an immutable JSON copy containing recovery-fence authority."""

        if (
            not isinstance(verification, dict)
            or not isinstance(verification.get("recovery_fence"), dict)
            or not verification["recovery_fence"]
        ):
            raise PullDeployError("runtime verification lacks recovery fence evidence")
        try:
            sealed = json.loads(canonical_json_bytes(verification))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PullDeployError("runtime verification is not canonical JSON") from exc
        if not isinstance(sealed, dict):
            raise PullDeployError("runtime verification is invalid")
        return sealed

    def _persist_runtime_verification(
        self,
        marker: dict[str, Any],
        verification: object,
        *,
        phase: str | None = None,
    ) -> dict[str, Any]:
        """Atomically fence a runtime in the crash marker before admission opens."""

        sealed = self._sealed_runtime_verification(verification)
        marker["verification"] = sealed
        marker.pop("runtime_start_intent", None)
        if phase is not None:
            marker["phase"] = phase
        marker["updated_at"] = utc_now()
        self._write_marker(marker)
        return sealed

    def _record_runtime_start_intent(
        self,
        marker: dict[str, Any],
        descriptor: dict[str, Any],
    ) -> None:
        """Authorize recovery of a same-descriptor start with unknown commit."""

        marker.pop("verification", None)
        marker["runtime_start_intent"] = {
            "target_sha": descriptor["repository"]["target_sha"],
            "recorded_at": utc_now(),
        }
        marker["updated_at"] = utc_now()
        self._write_marker(marker)

    def _marker_runtime_verification(self, marker: dict[str, Any]) -> dict[str, Any]:
        """Require durable fence evidence before finalising an open runtime."""

        return self._sealed_runtime_verification(marker.get("verification"))

    def _prepare_runtime_recovery(
        self,
        marker: dict[str, Any],
        descriptor: dict[str, Any],
        *,
        allow_unfenced: bool,
    ) -> dict[str, Any]:
        """Persist an ingress-isolated live/stopped recovery phase.

        The lifecycle performs the first side effect (ingress isolation),
        classifies all source readers, and re-drains the exact live instance.
        This controller then durably records that phase before any stop,
        restart, database restore, or admission resume can follow.
        """

        expected = marker.get("verification")
        if expected is not None and not isinstance(expected, dict):
            raise PullDeployError("runtime recovery verification evidence is invalid")
        start_intent = marker.get("runtime_start_intent")
        start_intent_authorized = bool(
            isinstance(start_intent, dict)
            and start_intent.get("target_sha") == descriptor["repository"]["target_sha"]
        )
        recovery = self.lifecycle.prepare_recovery_runtime(
            self,
            descriptor,
            expected,
            allow_unfenced=allow_unfenced or start_intent_authorized,
        )
        if not isinstance(recovery, dict) or recovery.get("runtime_state") not in {
            "drained",
            "stopped",
        }:
            raise PullDeployError("runtime recovery returned invalid lifecycle state")
        marker["drain"] = recovery
        marker["updated_at"] = utc_now()
        self._write_marker(marker)
        verification = recovery.get("verification")
        if recovery["runtime_state"] == "drained":
            if not isinstance(verification, dict):
                raise PullDeployError("drained recovery lacks runtime verification")
            self._persist_runtime_verification(marker, verification)
        elif verification is not None:
            raise PullDeployError(
                "stopped recovery unexpectedly contains live verification"
            )
        return recovery

    def _recover_runtime_and_resume(
        self,
        marker: dict[str, Any],
        descriptor: dict[str, Any],
        *,
        allow_unfenced: bool,
        bind_mutable_after: bool = True,
    ) -> dict[str, Any]:
        """Reconstruct one runtime from an explicit isolated recovery phase."""

        recovery = self._prepare_runtime_recovery(
            marker,
            descriptor,
            allow_unfenced=allow_unfenced,
        )
        if recovery["runtime_state"] == "stopped":
            self._persist_stopped_postgres_runtime_fence(marker, descriptor)
            self._record_runtime_start_intent(marker, descriptor)
            self.lifecycle.start(self, descriptor)
        verification = self._persist_runtime_verification(
            marker, self.lifecycle.verify(self, descriptor)
        )
        if bind_mutable_after and marker.get("mutable_data_before") is not None:
            self._bind_mutable_data_after(marker, descriptor)
        self.lifecycle.resume(self, descriptor, verification)
        return verification

    def _persist_stopped_postgres_runtime_fence(
        self,
        marker: dict[str, Any],
        descriptor: dict[str, Any],
    ) -> dict[str, Any]:
        """Seal PostgreSQL identity before any stopped-runtime restart.

        PostgreSQL remains live while application readers are stopped.  A
        recovery that starts the application without first sealing that exact
        container, image, volume, endpoint and system identifier could attach
        the candidate to a replacement database after a crash.  Persist the
        observation before recording start intent so a lost start response is
        still fenced by durable evidence.
        """

        capture = getattr(self.lifecycle, "postgres_runtime_identity", None)
        if not callable(capture):
            raise PullDeployError(
                "stopped runtime recovery cannot fence PostgreSQL identity"
            )
        observed = validate_postgres_runtime_fence(capture(self, descriptor))
        existing = marker.get("postgres_runtime_fence")
        if existing is not None and postgres_runtime_fence_identity(
            existing
        ) != postgres_runtime_fence_identity(observed):
            raise PullDeployError(
                "PostgreSQL identity changed before stopped runtime restart"
            )
        marker["postgres_runtime_fence"] = observed
        marker["updated_at"] = utc_now()
        self._write_marker(marker)
        return observed

    def _recover_unchanged_and_resume(
        self,
        marker: dict[str, Any],
        descriptor: dict[str, Any],
        *,
        allow_unfenced: bool,
    ) -> None:
        """Safely abort a pre-stop operation without restarting source readers."""

        recovery = self._prepare_runtime_recovery(
            marker,
            descriptor,
            allow_unfenced=allow_unfenced,
        )
        if recovery["runtime_state"] != "drained":
            raise PullDeployError("pre-stop unchanged runtime is no longer live")
        self.lifecycle.resume_unchanged(
            self,
            descriptor,
            lambda verification: self._persist_runtime_verification(
                marker, verification
            ),
            self._marker_runtime_verification(marker),
        )

    def _advance(self, marker: dict[str, Any], phase: str, **values: Any) -> None:
        marker.update(values)
        marker["phase"] = phase
        marker["updated_at"] = utc_now()
        self._write_marker(marker)

    def _stop_runtime(
        self,
        marker: dict[str, Any],
        descriptor: dict[str, Any],
    ) -> None:
        """Stop source readers and durably preserve the PostgreSQL identity."""

        existing = marker.get("postgres_runtime_fence")
        capture = getattr(self.lifecycle, "postgres_runtime_identity", None)
        if existing is None and callable(capture):
            before = capture(self, descriptor)
            if before is not None:
                marker["postgres_runtime_fence"] = validate_postgres_runtime_fence(
                    before
                )
                marker["updated_at"] = utc_now()
                self._write_marker(marker)
                existing = marker["postgres_runtime_fence"]
        if existing is None and not self.test_root_mode:
            raise PullDeployError(
                "runtime stop lacks a pre-stop PostgreSQL identity fence"
            )
        observed = self.lifecycle.stop(self, descriptor)
        if observed is None:
            if not self.test_root_mode:
                raise PullDeployError(
                    "runtime stop did not return a PostgreSQL identity fence"
                )
            return
        fence = validate_postgres_runtime_fence(observed)
        if existing is not None:
            if postgres_runtime_fence_identity(
                existing
            ) != postgres_runtime_fence_identity(fence):
                raise PullDeployError(
                    "PostgreSQL identity changed across runtime stop recovery"
                )
        else:
            marker["postgres_runtime_fence"] = fence
        self._write_marker(marker)

    def _adopt_legacy_takeover_stop(
        self,
        marker: dict[str, Any],
        descriptor: dict[str, Any],
    ) -> None:
        """Consume takeover's stop fence without draining or stopping again."""

        self._revalidate_bridge_external_authorities(descriptor)
        takeover = descriptor["legacy_takeover"]
        try:
            status = _legacy_takeover_evidence.load_status(
                self.runtime_root,
                takeover["operation_id"],
            )
        except Exception as exc:
            raise PullDeployError(
                "legacy takeover pre-stopped status is unavailable"
            ) from exc
        fence = status.get("pre_stopped_fence")
        runtime = (
            fence.get("runtime_fence")
            if isinstance(fence, dict)
            else None
        )
        if (
            not isinstance(runtime, dict)
            or status.get("apply_phase") != "complete"
            or status.get("restore_phase") is not None
            or status.get("active") is not False
            or status.get("pre_stopped_fence_sha256")
            != takeover["pre_stopped_fence_sha256"]
            or runtime.get("readers_stopped") is not True
            or runtime.get("postgres_running_untouched") is not True
        ):
            raise PullDeployError(
                "legacy takeover did not leave an exact stopped reader fence"
            )
        capture = getattr(self.lifecycle, "postgres_runtime_identity", None)
        if not callable(capture):
            raise PullDeployError(
                "takeover stop cannot capture PostgreSQL identity"
            )
        postgres = validate_postgres_runtime_fence(
            capture(self, descriptor)
        )
        if (
            runtime.get("postgres_container_id")
            != postgres["container_id"]
            or runtime.get("postgres_image_id") != postgres["image_id"]
            or runtime.get("postgres_data_volume")
            != postgres["data_volume"]["name"]
            or runtime.get("postgres_system_identifier")
            != postgres["system_identifier"]
        ):
            raise PullDeployError(
                "PostgreSQL identity differs from legacy takeover stop fence"
            )
        marker["drain"] = {
            "runtime_state": "stopped",
            "ingress_isolated": True,
            "source": "legacy-takeover",
            "pre_stopped_fence_sha256": takeover[
                "pre_stopped_fence_sha256"
            ],
        }
        marker["postgres_runtime_fence"] = postgres
        marker["takeover_pre_stopped_fence_sha256"] = takeover[
            "pre_stopped_fence_sha256"
        ]
        marker["runtime_stopped"] = True
        marker["phase"] = "runtime-stopped"
        marker["updated_at"] = utc_now()
        self._write_marker(marker)

    def _revalidate_previous_deployment_state(self, descriptor: dict[str, Any]) -> None:
        expected = descriptor.get("previous_deployment")
        expected_digest = descriptor.get("previous_deployment_sha256")
        exists = (
            self.current_state_path.exists() or self.current_state_path.is_symlink()
        )
        if expected is None:
            if exists or expected_digest is not None:
                raise PullDeployError(
                    "current deployment state appeared after bootstrap prepare"
                )
            if self._active_slot() is not None:
                raise PullDeployError(
                    "active Worker slot exists without a governed deployment"
                )
            # Bootstrap installs the target content-addressed controller
            # before legacy runtime takeover.  It is executor authority, not
            # evidence that the old checkout was governed.
            self.active_control_evidence()
            return
        if not exists or expected_digest is None:
            raise PullDeployError("sealed current deployment state is missing")
        if sha256_file(self.current_state_path) != expected_digest:
            raise PullDeployError("current deployment state changed after prepare")
        actual = self._validate_steady_deployment_state(
            load_private_json(self.current_state_path)
        )
        if actual != expected:
            raise PullDeployError(
                "current deployment state differs from sealed evidence"
            )
        if self._active_slot() != expected["active_monomer_md_slot"]:
            raise PullDeployError(
                "active Worker slot differs from sealed current deployment"
            )
        if self.active_control_evidence() != expected["active_control"]:
            raise PullDeployError(
                "active controls differ from sealed current deployment"
            )

    def _commit_current_state_cas(
        self,
        candidate: dict[str, Any],
        *,
        candidate_sha256: str,
        expected_pre_state: dict[str, Any] | None,
        expected_pre_state_sha256: str | None,
    ) -> str:
        """Replace current state only from the exact sealed pre-state.

        The final reload is intentionally adjacent to ``atomic_json``.  It
        closes the long drain/backup/migrate transaction window while still
        treating an exact candidate already on disk as a lost successful
        write response.
        """

        candidate = validate_current_deployment_state(candidate)
        candidate_sha256 = require_digest(
            candidate_sha256, "current-state candidate digest"
        )
        if (
            sha256_bytes(canonical_json_bytes(candidate) + b"\n")
            != candidate_sha256
        ):
            raise PullDeployError("current-state candidate digest differs")
        if (expected_pre_state is None) != (
            expected_pre_state_sha256 is None
        ):
            raise PullDeployError("current-state precondition is incomplete")
        if expected_pre_state_sha256 is not None:
            expected_pre_state_sha256 = require_digest(
                expected_pre_state_sha256,
                "current-state precondition digest",
            )
            expected_pre_state = validate_current_deployment_state(
                expected_pre_state
            )
            # The descriptor seals the actual bytes of
            # current-deployment.json.  Contract 0012 may rewrite the same
            # logical object using a different safe JSON serialization, so
            # never substitute a re-serialized logical digest for that file
            # CAS.  Object equality and the sealed on-disk digest are
            # independent preconditions below.

        exists = (
            self.current_state_path.exists()
            or self.current_state_path.is_symlink()
        )
        observed = (
            load_private_json(self.current_state_path) if exists else None
        )
        observed_digest = (
            sha256_file(self.current_state_path) if observed is not None else None
        )
        if observed == candidate and observed_digest == candidate_sha256:
            return "already-committed"
        if expected_pre_state is None:
            if observed is not None:
                raise PullDeployError(
                    "current deployment state appeared before commit"
                )
        elif (
            observed != expected_pre_state
            or observed_digest != expected_pre_state_sha256
        ):
            raise PullDeployError(
                "current deployment state changed before commit"
            )
        atomic_json(self.current_state_path, candidate)
        if (
            sha256_file(self.current_state_path) != candidate_sha256
            or load_private_json(self.current_state_path) != candidate
        ):
            raise PullDeployError(
                "current deployment state did not commit exactly"
            )
        return "committed"

    def _revalidate_pre_switch(self, descriptor: dict[str, Any]) -> None:
        if (
            descriptor.get("schema_version")
            == BRIDGE_DESCRIPTOR_SCHEMA_VERSION
        ):
            with self.offline_bridge_revalidation():
                self._revalidate_pre_switch_inner(descriptor)
            return
        self._revalidate_pre_switch_inner(descriptor)

    def _revalidate_pre_switch_inner(
        self,
        descriptor: dict[str, Any],
    ) -> None:
        self._assert_no_ignored_runtime()
        if self.production_config_evidence(check_free_space=True) != descriptor.get(
            "production_config"
        ):
            raise PullDeployError("production configuration changed after prepare")
        if self.mutable_data_contract() != descriptor.get("mutable_data"):
            raise PullDeployError("mutable-data audit contract changed after prepare")
        self._revalidate_previous_deployment_state(descriptor)
        bridge = (
            descriptor["bridge"]
            if descriptor["schema_version"] == BRIDGE_DESCRIPTOR_SCHEMA_VERSION
            else None
        )
        if (
            bridge is not None
            and self.active_control_evidence()
            != descriptor["controller"]["previous_active_control"]
        ):
            raise PullDeployError(
                "active F control authority changed after bridge prepare"
            )
        previous = descriptor.get("previous_deployment")
        if isinstance(previous, dict):
            self._previous_runtime_descriptor(descriptor)
            self._revalidate_materialized_images(
                previous["images"],
                source_sha=previous["source_sha"],
                pull=False,
            )
            previous_asset = descriptor["release_input"]["asset"].get("previous")
            if not isinstance(previous_asset, dict) or self._asset_pointer_target(
                descriptor
            ) != Path(previous_asset["root"]):
                raise PullDeployError(
                    "previous external asset pointer changed after prepare"
                )
        self.validate_installed_controls_against_target(
            descriptor["repository"]["target_sha"]
        )
        if (
            self.asset_evidence(descriptor["release_input"]["asset_manifest_digest"])
            != descriptor["release_input"]["asset"]
        ):
            raise PullDeployError(
                "external production asset identity changed after prepare"
            )
        self._revalidate_worker_controls(descriptor)
        repository = self.repository_identity(require_ssh_origin=True)
        expected = descriptor["repository"]
        if (
            repository["sha"] != expected["previous_sha"]
            or repository["tree"] != expected["previous_tree"]
        ):
            raise PullDeployError("production source changed after prepare")
        if bridge is not None:
            baseline, _pair, _reference = (
                self._completed_alias_external_database_baseline(
                    descriptor
                )
            )
            self._revalidate_external_database_binding(
                baseline,
                policy=bridge["policy"]["external_database_audit"],
            )
            prefetched, _takeover = (
                self._revalidate_bridge_external_authorities(descriptor)
            )
            relation = self.materialize_prefetched_bridge_relation(
                prefetched,
                create_target_ref=True,
            )
            if (
                relation["policy"] != bridge["policy"]
                or relation["policy_sha256"] != bridge["policy_sha256"]
                or relation["relation"]["authority_sha"]
                != bridge["authority"]["sha"]
                or relation["relation"]["authority_tree"]
                != bridge["authority"]["tree"]
                or relation["relation"]["target_sha"] != expected["target_sha"]
                or relation["relation"]["target_tree"] != expected["target_tree"]
                or relation["relation"]["target_ref"]
                != bridge["target"]["exact_ref"]
            ):
                raise PullDeployError(
                    "bridge F policy or exact B relation changed after prepare"
                )
            authority_ci = self.bootstrap_ci_evidence(
                authority_sha=bridge["authority"]["sha"],
                required_jobs=bridge["policy"]["required_ci_jobs"],
            )
            if (
                authority_ci != descriptor["ci"]
                or _bridge_core.canonical_json_digest(authority_ci)
                != bridge["authority"]["ci_evidence_sha256"]
            ):
                raise PullDeployError(
                    "bridge authority CI evidence changed after prepare"
                )
            try:
                token = _bridge_core.load_token_authority(self.state_dir)
            except Exception as exc:
                raise PullDeployError(
                    "bridge token authority changed after prepare"
                ) from exc
            if (
                token["status"] not in {"prepared", "commit-intent"}
                or token["operation_id"] != descriptor["operation_id"]
                or token["policy_id"] != bridge["policy"]["policy_id"]
                or token["token_id"] != bridge["token"]["token_id"]
                or token["token_sha256"] != bridge["token"]["token_sha256"]
            ):
                raise PullDeployError(
                    "bridge token is not available before source switch"
                )
        else:
            if self.remote_main() != expected["target_sha"]:
                raise PullDeployError("prepared target has been superseded on main")
            fetched_tree = self.fetch_target(
                expected["target_sha"], descriptor["operation_id"]
            )
            if fetched_tree != expected["target_tree"]:
                raise PullDeployError("prepared target tree changed")
            if self.ci_evidence(expected["target_sha"]) != descriptor["ci"]:
                raise PullDeployError("target CI evidence changed after prepare")
        if bridge is not None:
            if (
                self.prefetched_application_images(
                    prefetched,
                    target_sha=expected["target_sha"],
                )
                != descriptor["images"]
                or self.prefetched_postgres_restore_image(prefetched)
                != descriptor["postgres_restore_image"]
            ):
                raise PullDeployError(
                    "sealed local bridge image material changed after prepare"
                )
        else:
            for role in ("backend", "web"):
                if (
                    self.image_evidence(role, expected["target_sha"])
                    != descriptor["images"][role]
                ):
                    raise PullDeployError(
                        f"sealed {role} image identity changed after prepare"
                    )
            if (
                self.postgres_restore_image_evidence()
                != descriptor["postgres_restore_image"]
            ):
                raise PullDeployError(
                    "sealed PostgreSQL restore image changed after prepare"
                )

    def _switch_source(self, descriptor: dict[str, Any]) -> None:
        repository = descriptor["repository"]
        existing = self._git(
            "show-ref",
            "--verify",
            "--hash",
            "refs/nexpoly/previous",
            check=False,
        )
        if existing.returncode not in {0, 1}:
            raise PullDeployError("cannot inspect the previous-source recovery ref")
        expected_previous_ref = (
            require_sha(str(existing.stdout).strip(), "existing previous-source ref")
            if existing.returncode == 0
            else "0" * 40
        )
        self._git(
            "update-ref",
            "refs/nexpoly/previous",
            repository["previous_sha"],
            expected_previous_ref,
        )
        source_ref = (
            descriptor["bridge"]["target"]["exact_ref"]
            if descriptor["schema_version"] == BRIDGE_DESCRIPTOR_SCHEMA_VERSION
            else "refs/remotes/nexpoly-deploy/main"
        )
        self._git("merge", "--ff-only", source_ref)
        current = self.repository_identity(require_ssh_origin=True)
        if (
            current["sha"] != repository["target_sha"]
            or current["tree"] != repository["target_tree"]
        ):
            raise PullDeployError("production checkout differs after fast-forward")

    def _restore_source(self, descriptor: dict[str, Any]) -> None:
        previous = descriptor["repository"]["previous_sha"]
        self._git("reset", "--hard", previous)
        current = self.repository_identity()
        if (
            current["sha"] != previous
            or current["tree"] != descriptor["repository"]["previous_tree"]
        ):
            raise PullDeployError(
                "production source rollback did not restore previous identity"
            )

    @staticmethod
    def _active_matches_candidate(
        active: dict[str, Any], candidate: dict[str, Any]
    ) -> bool:
        return all(
            active.get(key) == candidate.get(key)
            for key in ("release_id", "source_sha", "source_tree", "manifest_sha256")
        ) and active.get("operation_id") == candidate.get("operation_id")

    def _activate_control(self, descriptor: dict[str, Any]) -> dict[str, Any]:
        candidate = descriptor["controller"]["executor_control"]
        previous = descriptor["controller"]["previous_active_control"]
        try:
            _record, _manifest, _root = _control_runtime.load_candidate_control(
                self.runtime_root, candidate
            )
            current = self.active_control_evidence()
        except Exception as exc:
            raise PullDeployError("control handoff evidence is unavailable") from exc
        if self._active_matches_candidate(current, candidate):
            return current
        if current != previous:
            raise PullDeployError(
                "active control authority is neither sealed previous nor candidate"
            )
        active = {
            "schema_version": _control_runtime.ACTIVE_CONTROL_SCHEMA_VERSION,
            "protocol_version": _control_runtime.PROTOCOL_VERSION,
            "component": "deployment-controls",
            "generation": previous["generation"] + 1,
            "release_id": candidate["release_id"],
            "source_sha": candidate["source_sha"],
            "source_tree": candidate["source_tree"],
            "manifest_sha256": candidate["manifest_sha256"],
            "operation_id": descriptor["operation_id"],
            "previous_release_id": previous["release_id"],
            "activated_at": utc_now(),
        }
        try:
            _control_runtime.validate_active_control_record(active)
        except Exception as exc:
            raise PullDeployError("candidate active control record is invalid") from exc
        atomic_json(self.active_control_path, active)
        if self.active_control_evidence() != active:
            raise PullDeployError("candidate control authority did not switch exactly")
        return active

    def _restore_previous_control(self, descriptor: dict[str, Any]) -> None:
        previous = descriptor["controller"]["previous_active_control"]
        candidate = descriptor["controller"]["executor_control"]
        current = self.active_control_evidence()
        if current == previous:
            return
        if not self._active_matches_candidate(current, candidate):
            raise PullDeployError(
                "control rollback authority is neither sealed previous nor candidate"
            )
        try:
            _control_runtime.load_control_release(
                self.runtime_root, previous["release_id"]
            )
        except Exception as exc:
            raise PullDeployError("previous control release is unavailable") from exc
        atomic_json(self.active_control_path, previous)
        if self.active_control_evidence() != previous:
            raise PullDeployError("previous control authority did not restore exactly")

    def _activate_slot(self, descriptor: dict[str, Any]) -> dict[str, Any]:
        slot = descriptor["monomer_md"]["slot"]
        record_path = self.slots_state_dir / f"md-{slot}.json"
        record = validate_slot_record(load_private_json(record_path), slot)
        record_digest = worker_record_digest(record)
        if (
            worker_record_digest(record)
            != descriptor["monomer_md"]["slot_record_sha256"]
        ):
            raise PullDeployError(
                "candidate slot differs immediately before activation"
            )
        active = {
            "schema_version": ACTIVE_SLOT_SCHEMA_VERSION,
            "component": "monomer-md",
            "slot": slot,
            "source_sha": record["source_sha"],
            "source_tree": record["source_tree"],
            "worker_lock_sha256": record["worker_lock_sha256"],
            "slot_record_sha256": record_digest,
            "operation_id": descriptor["operation_id"],
            "activated_at": utc_now(),
        }
        validate_active_slot_record(active)
        atomic_json(self.active_slot_path, active)
        return active

    def _restore_previous_slot(self, descriptor: dict[str, Any]) -> None:
        previous = descriptor.get("previous_deployment")
        current: dict[str, Any] | None = None
        if self.active_slot_path.exists() or self.active_slot_path.is_symlink():
            current = validate_active_slot_record(
                load_private_json(self.active_slot_path)
            )
        candidate = descriptor["monomer_md"]["slot_record"]
        candidate_digest = descriptor["monomer_md"]["slot_record_sha256"]

        def is_candidate(value: dict[str, Any] | None) -> bool:
            return value is not None and all(
                value.get(key) == expected
                for key, expected in {
                    "slot": candidate["slot"],
                    "source_sha": candidate["source_sha"],
                    "source_tree": candidate["source_tree"],
                    "worker_lock_sha256": candidate["worker_lock_sha256"],
                    "slot_record_sha256": candidate_digest,
                    "operation_id": descriptor["operation_id"],
                }.items()
            )

        if not isinstance(previous, dict):
            if current is None:
                return
            if not is_candidate(current):
                raise PullDeployError(
                    "bootstrap active Worker slot is not the sealed candidate"
                )
            self.active_slot_path.unlink()
            fsync_directory(self.active_slot_path.parent)
            return
        active = previous.get("active_monomer_md_slot")
        if not isinstance(active, dict):
            raise PullDeployError("previous deployment has no active Worker slot")
        validate_active_slot_record(active)
        if current != active and not is_candidate(current):
            raise PullDeployError(
                "active Worker slot is neither sealed previous nor candidate"
            )
        record_path = self.slots_state_dir / f"md-{active['slot']}.json"
        record = validate_slot_record(load_private_json(record_path), active["slot"])
        if worker_record_digest(record) != active["slot_record_sha256"]:
            raise PullDeployError("previous monomer MD slot record is unavailable")
        atomic_json(self.active_slot_path, active)

    def _previous_runtime_descriptor(
        self, descriptor: dict[str, Any]
    ) -> dict[str, Any]:
        """Project the prior governed state into lifecycle image/source inputs."""

        previous = descriptor.get("previous_deployment")
        if not isinstance(previous, dict):
            raise PullDeployError(
                "previous governed deployment evidence is unavailable"
            )
        if previous.get("control_helpers") != descriptor["controller"]["helpers"]:
            raise PullDeployError(
                "stable control upgrade closed the previous runtime rollback window"
            )
        images = previous.get("images")
        previous_sha = previous.get("source_sha")
        previous_tree = previous.get("source_tree")
        if not isinstance(images, dict) or set(images) != {"backend", "web"}:
            raise PullDeployError("previous deployment image evidence is invalid")
        require_sha(previous_sha, "previous governed source SHA")
        require_sha(previous_tree, "previous governed source tree")
        active = previous.get("active_monomer_md_slot")
        unit = previous.get("monomer_md_systemd_unit")
        worker_env = previous.get("monomer_md_worker_env")
        asset = previous.get("asset_identity")
        if (
            not isinstance(active, dict)
            or not isinstance(unit, dict)
            or not isinstance(worker_env, dict)
        ):
            raise PullDeployError("previous Worker runtime evidence is incomplete")
        validate_active_slot_record(active)
        slot_record = validate_slot_record(
            load_private_json(self.slots_state_dir / f"md-{active['slot']}.json"),
            active["slot"],
        )
        if worker_record_digest(slot_record) != active["slot_record_sha256"]:
            raise PullDeployError("previous Worker slot evidence changed")
        if (
            set(unit)
            != {"target_path", "sha256", "control_release_id", "launcher_sha256"}
            or not isinstance(unit["target_path"], str)
            or not Path(unit["target_path"]).is_absolute()
        ):
            raise PullDeployError("previous Worker systemd evidence is incomplete")
        require_digest(unit["sha256"], "previous Worker unit digest")
        if unit["control_release_id"] != previous["active_control"]["release_id"]:
            raise PullDeployError(
                "previous Worker control release differs from authority"
            )
        require_digest(unit["launcher_sha256"], "previous Worker launcher digest")
        if not isinstance(asset, dict):
            raise PullDeployError("previous external asset evidence is incomplete")
        projected = json.loads(json.dumps(descriptor))
        projected["_drain_authority_sha"] = descriptor["repository"]["target_sha"]
        projected["images"] = images
        projected["repository"]["target_sha"] = previous_sha
        projected["repository"]["target_tree"] = previous_tree
        projected["release_input"]["asset_manifest_digest"] = previous.get(
            "asset_manifest_digest"
        )
        projected["release_input"]["asset"] = asset
        compatibility = previous.get("migration_compatibility")
        if isinstance(compatibility, dict):
            projected["_migration_compatibility"] = compatibility
        projected["monomer_md"] = {
            "slot": active["slot"],
            "slot_record": slot_record,
            "slot_record_sha256": active["slot_record_sha256"],
            "worker_env": worker_env,
            "systemd_unit": {
                **descriptor["monomer_md"]["systemd_unit"],
                "target_path": unit["target_path"],
                "sha256": unit["sha256"],
                "control_release_id": unit["control_release_id"],
                "launcher_sha256": unit["launcher_sha256"],
            },
        }
        projected["_runtime_active_control"] = previous["active_control"]
        return projected

    def _current_state(
        self, descriptor: dict[str, Any], descriptor_digest: str, marker: dict[str, Any]
    ) -> dict[str, Any]:
        previous = descriptor.get("previous_deployment")
        migrations: list[dict[str, Any]] = []
        approvals: list[dict[str, Any]] = []
        barrier = None
        floor = None
        last_contract_operation = None
        contract_mutable_data_audit = None
        final_mutable_data_audit = None
        contract_external_database_audit = None
        final_external_database_audit = None
        if isinstance(previous, dict):
            approvals = list(previous.get("approved_contracts", []))
            barrier = previous.get("migration_epoch_barrier")
            floor = previous.get("schema_compatibility_floor")
            last_contract_operation = previous.get("last_contract_operation")
            contract_mutable_data_audit = previous.get(
                "contract_mutable_data_audit"
            )
            final_mutable_data_audit = previous.get(
                "final_mutable_data_audit"
            )
            contract_external_database_audit = previous.get(
                "contract_external_database_audit"
            )
            final_external_database_audit = previous.get(
                "final_external_database_audit"
            )
        history = marker.get("migration_history")
        if not isinstance(history, list) or not history:
            raise PullDeployError(
                "deployment marker has no canonical migration history"
            )
        migrations = [dict(record) for record in history if isinstance(record, dict)]
        if len(migrations) != len(history):
            raise PullDeployError("deployment marker migration history is invalid")
        migration_compatibility: dict[str, Any] | None = None
        compatibility_authority: dict[str, Any] | None = None
        if descriptor["schema_version"] == BRIDGE_DESCRIPTOR_SCHEMA_VERSION:
            compatibility_authority = descriptor["bridge"]["policy"]
        elif isinstance(previous, dict) and isinstance(
            previous.get("migration_compatibility"), dict
        ):
            compatibility_authority = previous["migration_compatibility"]
        if compatibility_authority is not None:
            migration_compatibility = build_migration_compatibility_state(
                compatibility_authority,
                code_manifest_sha256=descriptor["migrations"]["sha256"],
                migrations=migrations,
            )
        active = validate_active_slot_record(load_private_json(self.active_slot_path))
        active_control = self.active_control_evidence()
        if not self._active_matches_candidate(
            active_control, descriptor["controller"]["executor_control"]
        ):
            raise PullDeployError(
                "candidate control authority is not active at state commit"
            )
        mutable_before = marker.get("mutable_data_before")
        mutable_after = marker.get("mutable_data_after")
        mutable_pair = self._build_mutable_data_pair_for_descriptor(
            descriptor,
            mutable_before,
            mutable_after,
            descriptor_digest=descriptor_digest,
        )
        if mutable_pair["transition"]["kind"] == "expand-0013":
            final_mutable_data_audit = mutable_pair
        state = {
            "schema_version": 2,
            "status": "success",
            "operation_id": descriptor["operation_id"],
            "source_sha": descriptor["repository"]["target_sha"],
            "source_tree": descriptor["repository"]["target_tree"],
            "previous_release": descriptor["repository"]["previous_sha"],
            "descriptor_sha256": descriptor_digest,
            "images": descriptor["images"],
            "asset_manifest_digest": descriptor["release_input"][
                "asset_manifest_digest"
            ],
            "asset_identity": descriptor["release_input"]["asset"],
            "byteff2_commit": descriptor["release_input"]["asset"]["byteff2_commit"],
            "migrations": migrations,
            "approved_contracts": approvals,
            "migration_epoch_barrier": barrier,
            "schema_compatibility_floor": floor,
            "last_contract_operation": last_contract_operation,
            "contract_mutable_data_audit": contract_mutable_data_audit,
            "migration_compatibility": migration_compatibility,
            "active_monomer_md_slot": active,
            "monomer_md_worker_env": descriptor["monomer_md"]["worker_env"],
            "monomer_md_systemd_unit": {
                "target_path": descriptor["monomer_md"]["systemd_unit"]["target_path"],
                "sha256": descriptor["monomer_md"]["systemd_unit"]["sha256"],
                "control_release_id": descriptor["monomer_md"]["systemd_unit"][
                    "control_release_id"
                ],
                "launcher_sha256": descriptor["monomer_md"]["systemd_unit"][
                    "launcher_sha256"
                ],
            },
            "control_helpers": descriptor["controller"]["helpers"],
            "active_control": active_control,
            "production_config": descriptor["production_config"],
            "database_backup": marker.get("database_backup"),
            "mutable_data_audit": mutable_pair,
            "deployed_at": utc_now(),
        }
        external_database_transition_chain = None
        if (
            descriptor["schema_version"]
            == BRIDGE_DESCRIPTOR_SCHEMA_VERSION
        ):
            (
                bridge_pair,
                _alias_pair,
                alias_reference,
            ) = self._load_bridge_external_database_transition(
                descriptor,
                marker.get("bridge_external_database_audit"),
            )
            external_database_audit = bridge_pair["after_binding"]
            external_database_transition_chain = (
                build_external_database_transition_chain(
                    alias_reference=alias_reference,
                    bridge_reference=marker[
                        "bridge_external_database_audit"
                    ],
                    active_binding=external_database_audit,
                )
            )
        else:
            external_database_audit = (
                previous.get("external_database_audit")
                if isinstance(previous, dict)
                else None
            )
            external_database_transition_chain = (
                previous.get("external_database_transition_chain")
                if isinstance(previous, dict)
                else None
            )
        if external_database_audit is not None:
            state["external_database_audit"] = (
                validate_external_database_audit_binding(
                    external_database_audit
                )
            )
        if external_database_transition_chain is not None:
            state["external_database_transition_chain"] = (
                validate_external_database_transition_chain(
                    external_database_transition_chain,
                    active_binding=external_database_audit,
                )
            )
        if contract_external_database_audit is not None:
            state["contract_external_database_audit"] = (
                contract_external_database_audit
            )
        captured_final_external = marker.get(
            "final_external_database_audit"
        )
        if captured_final_external is not None:
            final_external_database_audit = captured_final_external
        if final_external_database_audit is not None:
            state["final_external_database_audit"] = (
                final_external_database_audit
            )
        if final_mutable_data_audit is not None:
            state["final_mutable_data_audit"] = final_mutable_data_audit
        return state

    def _audit_attempt(self, marker: dict[str, Any], status: str) -> Path:
        operation_id = require_operation_id(marker["operation_id"])
        normal, external = self._operation_directories(operation_id)
        if marker.get("takeover_restored_terminal_sha256") is not None:
            require_digest(
                marker["takeover_restored_terminal_sha256"],
                "legacy takeover restored terminal digest",
            )
            operation_dir = external
        else:
            operation_dir = normal
        self._ensure_private_operation_directory(operation_dir)
        path = operation_dir / f"{status}.json"
        expected = {**marker, "status": status}
        if path.exists() or path.is_symlink():
            existing = load_private_json(path)
            recorded_at = existing.pop("recorded_at", None)
            if (
                not isinstance(recorded_at, str)
                or not recorded_at
                or existing != expected
            ):
                raise PullDeployError(
                    "terminal deployment audit changed across recovery"
                )
            return path
        atomic_json(
            path,
            {**expected, "recorded_at": utc_now()},
        )
        return path

    def _audit_nonterminal_attempt(
        self,
        marker: dict[str, Any],
        status: str,
    ) -> Path:
        """Append one attempt-addressed audit without terminalising an operation."""

        if status != "recovered-explicit-rollback-aborted":
            raise PullDeployError("nonterminal deployment audit status is invalid")
        operation_id = require_operation_id(marker["operation_id"])
        normal, external = self._operation_directories(operation_id)
        if external.exists() or external.is_symlink():
            raise PullDeployError(
                "nonterminal rollback audit has an ambiguous operation root"
            )
        self._ensure_private_operation_directory(normal)
        expected = {**marker, "status": status}
        attempt_id = require_operation_id(
            str(marker.get("rollback_attempt_id", ""))
        )
        if not attempt_id.startswith("rollback-attempt-"):
            raise PullDeployError(
                "nonterminal rollback audit lacks an attempt identity"
            )
        path = normal / f"{status}-{attempt_id}.json"
        if path.exists() or path.is_symlink():
            existing = load_private_json(path)
            recorded_at = existing.pop("recorded_at", None)
            if (
                not isinstance(recorded_at, str)
                or not recorded_at
                or existing != expected
            ):
                raise PullDeployError(
                    "nonterminal rollback audit changed across recovery"
                )
            return path
        atomic_json(path, {**expected, "recorded_at": utc_now()})
        return path

    def _revalidate_bridge_candidate_database_state(
        self,
        descriptor: dict[str, Any],
        candidate: object,
        *,
        include_mutable: bool,
    ) -> None:
        """Freshly fence the database immediately around the bridge commit."""

        state = self._validate_external_database_state_provenance(candidate)
        active = validate_external_database_audit_binding(
            state.get("external_database_audit")
        )
        chain = validate_external_database_transition_chain(
            state.get("external_database_transition_chain"),
            active_binding=active,
        )
        bridge_pair, _alias_pair, alias_reference = (
            self._load_bridge_external_database_transition(
                descriptor,
                chain["bridge"],
            )
        )
        if (
            chain["alias"] != alias_reference
            or bridge_pair["after_binding"] != active
        ):
            raise PullDeployError(
                "bridge candidate external database chain differs"
            )
        expected = external_database_endpoint(
            active,
            contract_pair=state.get(
                "contract_external_database_audit"
            ),
            final_pair=state.get("final_external_database_audit"),
        )
        self._revalidate_external_database_binding(
            expected,
            policy=descriptor["bridge"]["policy"][
                "external_database_audit"
            ],
        )
        if include_mutable:
            pair = validate_mutable_data_pair(
                state.get("mutable_data_audit")
            )
            observed = self._capture_mutable_data(descriptor)
            if mutable_data_identity(observed) != mutable_data_identity(
                pair["after"]
            ):
                raise PullDeployError(
                    "bridge candidate mutable data changed before commit"
                )

    def _revalidate_candidate_database_state(
        self,
        descriptor: dict[str, Any],
        candidate: object,
        *,
        include_mutable: bool,
    ) -> None:
        """Freshly fence one ordinary candidate around its state commit."""

        if descriptor["schema_version"] == BRIDGE_DESCRIPTOR_SCHEMA_VERSION:
            self._revalidate_bridge_candidate_database_state(
                descriptor,
                candidate,
                include_mutable=include_mutable,
            )
            return
        state = self._validate_external_database_state_provenance(candidate)
        raw_active = state.get("external_database_audit")
        if raw_active is not None:
            active = validate_external_database_audit_binding(raw_active)
            expected = external_database_endpoint(
                active,
                contract_pair=state.get(
                    "contract_external_database_audit"
                ),
                final_pair=state.get(
                    "final_external_database_audit"
                ),
            )
            self._revalidate_external_database_binding(
                expected,
                policy={
                    **_bridge_core.EXTERNAL_DATABASE_AUDIT_POLICY,
                    "media_authority_rules_sha256": active[
                        "authority_rules"
                    ]["sha256"],
                    "audit_role_sql_sha256": active["role_sql"][
                        "sha256"
                    ],
                },
            )
        if include_mutable:
            pair = validate_mutable_data_pair(
                state.get("mutable_data_audit")
            )
            observed = self._capture_mutable_data(descriptor)
            if mutable_data_identity(observed) != mutable_data_identity(
                pair["after"]
            ):
                raise PullDeployError(
                    "candidate mutable data changed before commit"
                )

    def _load_bridge_token_for_candidate(
        self,
        descriptor: dict[str, Any],
        descriptor_digest: str,
    ) -> dict[str, Any]:
        try:
            token = _bridge_core.load_token_authority(self.state_dir)
        except Exception as exc:
            raise PullDeployError(
                "bridge token recovery authority differs"
            ) from exc
        bridge = descriptor["bridge"]
        if (
            token["operation_id"] != descriptor["operation_id"]
            or token["descriptor_sha256"] != descriptor_digest
            or token["policy_id"] != bridge["policy"]["policy_id"]
            or token["token_id"] != bridge["token"]["token_id"]
            or token["token_sha256"] != bridge["token"]["token_sha256"]
            or token["status"]
            not in {"prepared", "commit-intent", "consumed"}
        ):
            raise PullDeployError(
                "bridge token identity differs during candidate recovery"
            )
        return token

    def _candidate_current_state(
        self,
        descriptor: dict[str, Any],
        descriptor_digest: str,
        marker: dict[str, Any],
    ) -> dict[str, Any] | None:
        candidate = marker.get("candidate_state")
        candidate_digest = marker.get("candidate_state_sha256")
        expected_digest: str | None = None
        effective_phase = (
            marker.get("failed_phase")
            if marker.get("phase") == "failed"
            else marker.get("phase")
        )
        if candidate is not None or candidate_digest is not None:
            if not isinstance(candidate, dict):
                raise PullDeployError("deployment marker candidate state is invalid")
            validate_current_deployment_state(candidate)
            expected_digest = require_digest(
                candidate_digest, "deployment marker candidate-state digest"
            )
            if (
                candidate.get("source_sha") != descriptor["repository"]["target_sha"]
                or candidate.get("source_tree")
                != descriptor["repository"]["target_tree"]
                or candidate.get("descriptor_sha256") != descriptor_digest
                or sha256_bytes(canonical_json_bytes(candidate) + b"\n")
                != expected_digest
            ):
                raise PullDeployError(
                    "deployment marker candidate state differs from descriptor"
                )
            if effective_phase not in {
                "state-commit-started",
                "state-committed",
                "admission-resumed",
            }:
                raise PullDeployError(
                    "deployment candidate state has an invalid commit phase"
                )
        elif effective_phase in {
            "state-commit-started",
            "state-committed",
            "admission-resumed",
        }:
            raise PullDeployError(
                "deployment commit phase lost its candidate state"
            )
        exists = (
            self.current_state_path.exists()
            or self.current_state_path.is_symlink()
        )
        current = (
            self._validate_external_database_state_provenance(
                load_private_json(self.current_state_path)
            )
            if exists
            else None
        )
        current_digest = (
            sha256_file(self.current_state_path) if current is not None else None
        )
        current_is_candidate = bool(
            candidate is not None
            and current is not None
            and current == candidate
            and current_digest == expected_digest
        )
        previous = descriptor.get("previous_deployment")
        previous_digest = descriptor.get("previous_deployment_sha256")
        current_is_previous = bool(
            isinstance(previous, dict)
            and isinstance(previous_digest, str)
            and current is not None
            and current_digest == previous_digest
            and current == previous
        )
        current_is_bootstrap_absent = (
            previous is None and previous_digest is None and current is None
        )
        current_is_precommit = (
            current_is_previous or current_is_bootstrap_absent
        )
        is_bridge = (
            descriptor["schema_version"]
            == BRIDGE_DESCRIPTOR_SCHEMA_VERSION
        )

        if is_bridge:
            token = self._load_bridge_token_for_candidate(
                descriptor,
                descriptor_digest,
            )
            token_status = token["status"]
            token_candidate = token.get("candidate_state_sha256")
            if candidate is None:
                if token_status != "prepared":
                    raise PullDeployError(
                        "bridge commit authority lost its candidate state"
                    )
                if current_is_precommit:
                    return None
            else:
                assert expected_digest is not None
                if token_status in {"commit-intent", "consumed"} and (
                    token_candidate != expected_digest
                ):
                    raise PullDeployError(
                        "bridge token names another candidate state"
                    )
                if token_status in {"prepared", "commit-intent"} and (
                    effective_phase != "state-commit-started"
                ):
                    raise PullDeployError(
                        "bridge token status differs from marker commit phase"
                    )
                if current_is_candidate:
                    if token_status == "prepared":
                        raise PullDeployError(
                            "bridge current state appeared before token commit intent"
                        )
                    include_mutable = (
                        token_status == "commit-intent"
                        or effective_phase == "state-commit-started"
                    )
                    self._revalidate_bridge_candidate_database_state(
                        descriptor,
                        candidate,
                        include_mutable=include_mutable,
                    )
                    if token_status == "commit-intent":
                        self._consume_bridge_token(
                            descriptor,
                            descriptor_digest,
                            expected_digest,
                        )
                    return current
                if current_is_precommit:
                    if token_status == "prepared":
                        return None
                    if token_status == "consumed":
                        raise PullDeployError(
                            "consumed bridge token lost its candidate current state"
                        )
                    self._revalidate_bridge_candidate_database_state(
                        descriptor,
                        candidate,
                        include_mutable=True,
                    )
                    # A globally durable commit intent makes rollback
                    # ambiguous. Finish the exact replace only after both
                    # database evidence classes have been freshly fenced.
                    self._commit_current_state_cas(
                        candidate,
                        candidate_sha256=expected_digest,
                        expected_pre_state=previous,
                        expected_pre_state_sha256=marker.get(
                            "current_state_precondition_sha256",
                            previous_digest,
                        ),
                    )
                    self._consume_bridge_token(
                        descriptor,
                        descriptor_digest,
                        expected_digest,
                    )
                    return candidate
        elif candidate is not None and current_is_candidate:
            self._revalidate_candidate_database_state(
                descriptor,
                candidate,
                include_mutable=effective_phase == "state-commit-started",
            )
            return current

        if current_is_precommit:
            if effective_phase in {
                "state-committed",
                "admission-resumed",
            }:
                raise PullDeployError(
                    "deployment commit phase lost its candidate current state"
                )
            return None
        if candidate is not None and current is not None and (
            current.get("source_sha")
            == descriptor["repository"]["target_sha"]
            or current.get("descriptor_sha256") == descriptor_digest
        ):
            raise PullDeployError(
                "durable candidate state differs from its exact commit intent"
            )
        if candidate is None and current is not None and (
            current.get("source_sha")
            == descriptor["repository"]["target_sha"]
            or current.get("descriptor_sha256") == descriptor_digest
        ):
            raise PullDeployError(
                "candidate current state exists without a sealed commit intent"
            )
        raise PullDeployError(
            "current deployment state is neither sealed previous nor candidate"
        )

    def _reconcile_bridge_token(
        self,
        descriptor: dict[str, Any],
        descriptor_digest: str,
        *,
        observed_current_state_sha256: str | None,
    ) -> dict[str, Any]:
        bridge = descriptor["bridge"]
        try:
            token = _bridge_core.reconcile_token(
                self.state_dir,
                operation_id=descriptor["operation_id"],
                descriptor_sha256=descriptor_digest,
                observed_current_state_sha256=observed_current_state_sha256,
            )
        except Exception as exc:
            raise PullDeployError("bridge token recovery authority differs") from exc
        if (
            token["policy_id"] != bridge["policy"]["policy_id"]
            or token["token_id"] != bridge["token"]["token_id"]
            or token["token_sha256"] != bridge["token"]["token_sha256"]
        ):
            raise PullDeployError("bridge token identity differs during recovery")
        return token

    def _begin_bridge_state_commit(
        self,
        descriptor: dict[str, Any],
        descriptor_digest: str,
        candidate_state_digest: str,
    ) -> None:
        try:
            token = _bridge_core.begin_state_commit(
                self.state_dir,
                operation_id=descriptor["operation_id"],
                descriptor_sha256=descriptor_digest,
                candidate_state_sha256=candidate_state_digest,
            )
        except Exception as exc:
            raise PullDeployError(
                "bridge token could not authorize current-state commit"
            ) from exc
        bridge = descriptor["bridge"]
        if (
            token["status"] not in {"commit-intent", "consumed"}
            or token["policy_id"] != bridge["policy"]["policy_id"]
            or token["token_id"] != bridge["token"]["token_id"]
            or token["token_sha256"] != bridge["token"]["token_sha256"]
        ):
            raise PullDeployError("bridge state commit token identity differs")

    def _consume_bridge_token(
        self,
        descriptor: dict[str, Any],
        descriptor_digest: str,
        candidate_state_digest: str,
    ) -> None:
        try:
            token = _bridge_core.consume_token(
                self.state_dir,
                operation_id=descriptor["operation_id"],
                descriptor_sha256=descriptor_digest,
                candidate_state_sha256=candidate_state_digest,
            )
        except Exception as exc:
            raise PullDeployError("bridge token consumption failed") from exc
        if token["status"] != "consumed":
            raise PullDeployError("bridge token did not become permanently consumed")

    @staticmethod
    def _bridge_recovery_capsule_binding(
        metadata: dict[str, Any],
    ) -> dict[str, str]:
        return {
            "capsule_sha256": metadata["capsule_sha256"],
            "descriptor_sha256": metadata["descriptor_sha256"],
            "control_release_id": metadata["control_release_id"],
            "recovery_entry_sha256": metadata["files"][
                "bridge_recovery_capsule.py"
            ]["sha256"],
        }

    def _load_bridge_recovery_capsule(
        self,
        capsule_sha256: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        capsule_sha256 = require_digest(
            capsule_sha256, "bridge recovery capsule digest"
        )
        base = (
            self.runtime_root / BRIDGE_RECOVERY_CAPSULE_ROOT_RELATIVE
        )
        current = self.runtime_root
        ensure_private_directory(current)
        for component in BRIDGE_RECOVERY_CAPSULE_ROOT_RELATIVE.parts:
            current = current / component
            ensure_private_directory(current)
        if current != base:
            raise PullDeployError("bridge recovery capsule root is inconsistent")
        root = base / capsule_sha256.removeprefix("sha256:")
        ensure_private_directory(root)
        metadata = load_private_json(root / "capsule.json")
        fields = {
            "schema_version",
            "capsule_sha256",
            "operation_id",
            "authority_sha",
            "target_sha",
            "descriptor_sha256",
            "control_release_id",
            "takeover_operation_id",
            "files",
        }
        if (
            set(metadata) != fields
            or metadata.get("schema_version") != 1
            or metadata.get("capsule_sha256") != capsule_sha256
            or canonical_json_digest(
                {
                    key: value
                    for key, value in metadata.items()
                    if key != "capsule_sha256"
                }
            )
            != capsule_sha256
            or not isinstance(metadata.get("control_release_id"), str)
            or re.fullmatch(
                r"^[0-9a-f]{64}$", metadata["control_release_id"]
            )
            is None
        ):
            raise PullDeployError("bridge recovery capsule identity is invalid")
        require_operation_id(metadata.get("operation_id"))
        require_sha(metadata.get("authority_sha"), "capsule authority SHA")
        require_sha(metadata.get("target_sha"), "capsule target SHA")
        require_digest(
            metadata.get("descriptor_sha256"),
            "capsule descriptor digest",
        )
        if (
            not isinstance(metadata.get("takeover_operation_id"), str)
            or not metadata["takeover_operation_id"].startswith("takeover-")
        ):
            raise PullDeployError(
                "bridge recovery capsule takeover identity is invalid"
            )
        files = metadata.get("files")
        if (
            not isinstance(files, dict)
            or set(files) != set(BRIDGE_RECOVERY_CAPSULE_FILES)
        ):
            raise PullDeployError(
                "bridge recovery capsule file inventory is invalid"
            )
        control = root / "control"
        ensure_private_directory(control)
        if {path.name for path in control.iterdir()} != set(
            BRIDGE_RECOVERY_CAPSULE_FILES
        ):
            raise PullDeployError(
                "bridge recovery capsule contains extra control files"
            )
        for name in BRIDGE_RECOVERY_CAPSULE_FILES:
            record = files.get(name)
            path = control / name
            try:
                file_metadata = path.lstat()
            except OSError as exc:
                raise PullDeployError(
                    f"bridge recovery capsule file is unavailable: {name}"
                ) from exc
            if (
                not isinstance(record, dict)
                or set(record) != {"sha256", "mode"}
                or record.get("mode") != "0700"
                or not stat.S_ISREG(file_metadata.st_mode)
                or path.is_symlink()
                or file_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(file_metadata.st_mode) != 0o700
                or sha256_file(path)
                != require_digest(
                    record.get("sha256"),
                    f"bridge recovery capsule {name}",
                )
            ):
                raise PullDeployError(
                    f"bridge recovery capsule file changed: {name}"
                )
        descriptor_path = root / "descriptor.json"
        descriptor = validate_descriptor(load_private_json(descriptor_path))
        if (
            sha256_file(descriptor_path)
            != metadata["descriptor_sha256"]
            or descriptor.get("operation_id") != metadata["operation_id"]
            or descriptor["repository"]["target_sha"]
            != metadata["target_sha"]
            or descriptor["bridge"]["authority"]["sha"]
            != metadata["authority_sha"]
            or descriptor["legacy_takeover"]["operation_id"]
            != metadata["takeover_operation_id"]
            or descriptor["controller"]["executor_control"]["release_id"]
            != metadata["control_release_id"]
        ):
            raise PullDeployError(
                "bridge recovery capsule descriptor authority differs"
            )
        return metadata, descriptor

    def _prepare_bridge_recovery_capsule(
        self,
        descriptor: dict[str, Any],
        descriptor_digest: str,
    ) -> dict[str, Any]:
        """Publish the minimum exact B controls outside the restored layout."""

        if (
            not self._is_first_bridge(descriptor)
            or self._held_deploy_lock_fd is None
        ):
            raise PullDeployError(
                "bridge recovery capsule requires locked first-bridge authority"
            )
        descriptor_digest = require_digest(
            descriptor_digest, "bridge recovery descriptor digest"
        )
        executor = descriptor["controller"]["executor_control"]
        try:
            _candidate, manifest, candidate_root = (
                _control_runtime.load_candidate_control(
                    self.runtime_root, executor
                )
            )
        except Exception as exc:
            raise PullDeployError(
                "B recovery control release is unavailable"
            ) from exc
        files: dict[str, dict[str, str]] = {}
        payloads: dict[str, bytes] = {}
        for name in BRIDGE_RECOVERY_CAPSULE_FILES:
            record = manifest["files"].get(name)
            path = candidate_root / name
            if (
                not isinstance(record, dict)
                or record.get("mode") != 0o700
                or not path.is_file()
                or path.is_symlink()
            ):
                raise PullDeployError(
                    f"B recovery control is unavailable: {name}"
                )
            payload = path.read_bytes()
            digest = sha256_bytes(payload)
            if digest != record.get("sha256"):
                raise PullDeployError(
                    f"B recovery control digest differs: {name}"
                )
            payloads[name] = payload
            files[name] = {"sha256": digest, "mode": "0700"}
        descriptor_payload = canonical_json_bytes(descriptor) + b"\n"
        if sha256_bytes(descriptor_payload) != descriptor_digest:
            raise PullDeployError(
                "bridge recovery descriptor changed before capsule"
            )
        identity = {
            "schema_version": 1,
            "operation_id": descriptor["operation_id"],
            "authority_sha": descriptor["bridge"]["authority"]["sha"],
            "target_sha": descriptor["repository"]["target_sha"],
            "descriptor_sha256": descriptor_digest,
            "control_release_id": executor["release_id"],
            "takeover_operation_id": descriptor["legacy_takeover"][
                "operation_id"
            ],
            "files": files,
        }
        capsule_sha256 = canonical_json_digest(identity)
        metadata = {**identity, "capsule_sha256": capsule_sha256}
        base = (
            self.runtime_root / BRIDGE_RECOVERY_CAPSULE_ROOT_RELATIVE
        )
        self._ensure_private_operation_directory(base)
        final = base / capsule_sha256.removeprefix("sha256:")
        if final.exists() or final.is_symlink():
            observed, observed_descriptor = (
                self._load_bridge_recovery_capsule(capsule_sha256)
            )
            if observed != metadata or observed_descriptor != descriptor:
                raise PullDeployError(
                    "existing bridge recovery capsule conflicts"
                )
            return observed
        staging = base / (
            f".{capsule_sha256.removeprefix('sha256:')}.staging"
        )
        if staging.exists() or staging.is_symlink():
            ensure_private_directory(staging)
            shutil.rmtree(staging)
            fsync_directory(base)
        staging.mkdir(mode=0o700)
        control = staging / "control"
        control.mkdir(mode=0o700)
        try:
            for name, payload in payloads.items():
                atomic_bytes(control / name, payload, mode=0o700)
            atomic_bytes(
                staging / "descriptor.json",
                descriptor_payload,
                mode=0o600,
            )
            atomic_json(staging / "capsule.json", metadata)
            fsync_directory(control)
            fsync_directory(staging)
            os.rename(staging, final)
            fsync_directory(base)
        except BaseException:
            with contextlib.suppress(OSError):
                shutil.rmtree(staging)
                fsync_directory(base)
            raise
        observed, observed_descriptor = self._load_bridge_recovery_capsule(
            capsule_sha256
        )
        if observed != metadata or observed_descriptor != descriptor:
            raise PullDeployError(
                "published bridge recovery capsule differs"
            )
        return observed

    def _retire_failed_first_bridge_token(
        self,
        descriptor: dict[str, Any],
        descriptor_digest: str,
        marker: dict[str, Any],
        audit_path: Path,
    ) -> dict[str, Any]:
        """Retire only a prepared token bound to a terminal failed restore."""

        if self._held_deploy_lock_fd is None:
            raise PullDeployError("bridge token retirement lacks deploy.lock ownership")
        if not self._is_first_bridge(descriptor):
            raise PullDeployError("only the first bridge token can retire precommit")
        if self.current_state_path.exists() or self.current_state_path.is_symlink():
            raise PullDeployError(
                "bridge token cannot retire after current state committed"
            )
        restored_terminal = require_digest(
            marker.get("takeover_restored_terminal_sha256"),
            "legacy takeover restored terminal digest",
        )
        capsule_binding = marker.get("bridge_recovery_capsule")
        if not isinstance(capsule_binding, dict):
            raise PullDeployError(
                "bridge token retirement lacks recovery capsule authority"
            )
        capsule_sha256 = require_digest(
            capsule_binding.get("capsule_sha256"),
            "bridge token recovery capsule",
        )
        capsule, capsule_descriptor = self._load_bridge_recovery_capsule(
            capsule_sha256
        )
        if (
            self._bridge_recovery_capsule_binding(capsule)
            != capsule_binding
            or capsule.get("descriptor_sha256") != descriptor_digest
            or capsule_descriptor != descriptor
        ):
            raise PullDeployError(
                "bridge token recovery capsule authority differs"
            )
        operation_id = descriptor["operation_id"]
        operation_state = self._load_operation_state(operation_id)
        if (
            operation_state is None
            or operation_state.get("outcome") != "failed"
            or operation_state.get("descriptor_sha256") != descriptor_digest
        ):
            raise PullDeployError(
                "bridge token retirement lacks terminal failed operation state"
            )
        normal, external = self._operation_directories(operation_id)
        operation_state_path = external / "operation-state.json"
        if (
            normal.exists()
            or normal.is_symlink()
            or not operation_state_path.exists()
        ):
            raise PullDeployError(
                "bridge token retirement operation state is in the wrong audit root"
            )
        if audit_path.parent != external or audit_path.name == "operation-state.json":
            raise PullDeployError(
                "bridge token retirement audit is in the wrong audit root"
            )
        try:
            token = _bridge_core.retire_precommit_token(
                self.state_dir,
                operation_id=operation_id,
                descriptor_sha256=descriptor_digest,
                operation_state_sha256=sha256_file(operation_state_path),
                terminal_audit_sha256=sha256_file(audit_path),
                restored_terminal_sha256=restored_terminal,
                recovery_capsule_sha256=capsule_sha256,
            )
        except Exception as exc:
            raise PullDeployError(
                "failed bridge token could not retire exactly"
            ) from exc
        if (
            token["status"] != "retired-precommit"
            or token["operation_id"] != operation_id
            or token["descriptor_sha256"] != descriptor_digest
            or token["retirement"]["restored_terminal_sha256"]
            != restored_terminal
        ):
            raise PullDeployError("retired bridge token identity differs")
        return token

    def _complete_failed_first_bridge_recovery(
        self,
        descriptor: dict[str, Any],
        descriptor_digest: str,
        marker: dict[str, Any],
        *,
        audit_status: str,
    ) -> None:
        """Commit a failed F -> B recovery in one strictly ordered sequence."""

        if self._held_deploy_lock_fd is None:
            raise PullDeployError(
                "failed bridge recovery lacks deploy.lock ownership"
            )
        restored = self._probe_restored_legacy_takeover(descriptor)
        if restored is None:
            raise PullDeployError(
                "failed bridge recovery lacks exact terminal legacy restore"
            )
        # Ordering is security-sensitive: marker truth and terminal evidence
        # must be durable before the global one-time token can be retired.
        self._finalize_restored_legacy_takeover(
            descriptor,
            marker,
            restored,
        )
        audit_path = self._audit_attempt(marker, audit_status)
        self._record_operation_outcome(
            operation_id=descriptor["operation_id"],
            descriptor_sha256=descriptor_digest,
            outcome="failed",
        )
        self._retire_failed_first_bridge_token(
            descriptor,
            descriptor_digest,
            marker,
            audit_path,
        )
        self.marker_path.unlink()
        fsync_directory(self.marker_path.parent)

    def _bridge_token_successor_authority(
        self,
    ) -> dict[str, Any] | None:
        """Validate the complete failed/restore chain before allocating a generation."""

        if not (
            self.bridge_token_path.exists()
            or self.bridge_token_path.is_symlink()
        ):
            return None
        try:
            token = _bridge_core.load_token_authority(self.state_dir)
        except Exception as exc:
            raise PullDeployError("bridge token authority is invalid") from exc
        if token["status"] != "retired-precommit":
            return None
        try:
            authority = _bridge_core.retirement_reuse_authority(token)
        except Exception as exc:
            raise PullDeployError(
                "retired bridge token successor authority is invalid"
            ) from exc
        operation_id = token["operation_id"]
        capsule, descriptor = self._load_bridge_recovery_capsule(
            authority["recovery_capsule_sha256"]
        )
        descriptor_digest = capsule["descriptor_sha256"]
        if (
            not self._is_first_bridge(descriptor)
            or descriptor.get("operation_id") != operation_id
            or descriptor_digest != token["descriptor_sha256"]
        ):
            raise PullDeployError(
                "retired bridge token descriptor authority differs"
            )
        state = self._load_operation_state(operation_id)
        normal, external = self._operation_directories(operation_id)
        state_path = external / "operation-state.json"
        if (
            normal.exists()
            or normal.is_symlink()
            or state is None
            or state.get("outcome") != "failed"
            or state.get("descriptor_sha256") != descriptor_digest
            or not state_path.exists()
            or sha256_file(state_path)
            != authority["operation_state_sha256"]
        ):
            raise PullDeployError(
                "retired bridge token failed operation authority differs"
            )
        matching_audits: list[Path] = []
        for path in external.glob("*.json"):
            if path.name == "operation-state.json":
                continue
            load_private_json(path)
            if sha256_file(path) == authority["terminal_audit_sha256"]:
                matching_audits.append(path)
        if len(matching_audits) != 1:
            raise PullDeployError(
                "retired bridge token terminal audit authority differs"
            )
        audit = load_private_json(matching_audits[0])
        if (
            audit.get("operation_id") != operation_id
            or audit.get("descriptor_sha256") != descriptor_digest
            or audit.get("takeover_restored_terminal_sha256")
            != authority["restored_terminal_sha256"]
            or not isinstance(audit.get("bridge_recovery_capsule"), dict)
            or audit["bridge_recovery_capsule"].get("capsule_sha256")
            != authority["recovery_capsule_sha256"]
            or audit.get("status")
            not in {
                "failed",
                "recovered-takeover-restore",
                "recovered-partial-takeover-restore",
            }
        ):
            raise PullDeployError(
                "retired bridge token terminal audit content differs"
            )
        restored = self._probe_restored_legacy_takeover(descriptor)
        if (
            restored is None
            or restored.get("restored_terminal_sha256")
            != authority["restored_terminal_sha256"]
            or self.current_state_path.exists()
            or self.current_state_path.is_symlink()
        ):
            raise PullDeployError(
                "retired bridge token restored identity differs"
            )
        return authority

    def _bridge_precommit_is_retryable(
        self,
        descriptor: dict[str, Any],
        descriptor_digest: str,
    ) -> bool:
        """Keep the same prepared operation reusable after a full rollback."""

        if descriptor["schema_version"] != BRIDGE_DESCRIPTOR_SCHEMA_VERSION:
            return False
        if (
            self._is_first_bridge(descriptor)
            and self._probe_restored_legacy_takeover(descriptor)
            is not None
        ):
            # The legacy origin/files/runtime are active again.  Reusing this
            # prepared operation would bypass a new sealed takeover.
            return False
        try:
            token = _bridge_core.load_token_authority(self.state_dir)
        except Exception as exc:
            raise PullDeployError(
                "bridge rollback cannot classify token authority"
            ) from exc
        bridge = descriptor["bridge"]
        return bool(
            token["status"] == "prepared"
            and token["operation_id"] == descriptor["operation_id"]
            and token["descriptor_sha256"] == descriptor_digest
            and token["policy_id"] == bridge["policy"]["policy_id"]
            and token["token_id"] == bridge["token"]["token_id"]
            and token["token_sha256"] == bridge["token"]["token_sha256"]
        )

    def _restore_database_after_failed_apply(
        self,
        descriptor: dict[str, Any],
        marker: dict[str, Any],
    ) -> None:
        if marker.get("database_change_started") is not True:
            return
        backup = marker.get("database_backup")
        backup = self._validate_database_backup(
            descriptor, backup, require_operation_backup=True
        )
        policy = (
            descriptor["bridge"]["policy"]["external_database_audit"]
            if descriptor.get("schema_version")
            == BRIDGE_DESCRIPTOR_SCHEMA_VERSION
            else None
        )
        restore_baseline = None
        if policy is not None:
            restore_baseline, _alias_pair, _alias_reference = (
                self._completed_alias_external_database_baseline(
                    descriptor
                )
            )
        if marker.get("database_restored") is True:
            if marker.get("database_restore_started") is not True:
                raise PullDeployError(
                    "database restored marker lacks restore intent"
                )
            restored = marker.get("database_restore")
            restored_mutable = validate_mutable_data_pair(
                marker.get("mutable_data_restored")
            )
            if (
                not isinstance(restored, dict)
                or restored.get("restored") is not True
                or restored.get("dump_sha256") != backup["sha256"]
                or restored.get("ledger")
                != backup["restore_verification"]["ledger"]
                or restored_mutable
                != self._build_mutable_data_pair_for_descriptor(
                    descriptor,
                    marker.get("mutable_data_before"),
                    marker.get("mutable_data_after"),
                )
            ):
                raise PullDeployError(
                    "durable database restore evidence differs"
                )
            if restore_baseline is not None and policy is not None:
                self._revalidate_external_database_binding(
                    restore_baseline,
                    policy=policy,
                )
            return
        self._advance(
            marker,
            "database-restore-started",
            database_restore_started=True,
        )
        restored = self.lifecycle.restore_database(self, descriptor, backup)
        if (
            not isinstance(restored, dict)
            or restored.get("restored") is not True
            or restored.get("dump_sha256") != backup["sha256"]
            or restored.get("ledger")
            != backup["restore_verification"]["ledger"]
        ):
            raise PullDeployError("production database restore evidence is invalid")
        if restore_baseline is not None and policy is not None:
            self._revalidate_external_database_binding(
                restore_baseline,
                policy=policy,
            )
        restored_mutable = self._bind_mutable_data_after(marker, descriptor)
        self._advance(
            marker,
            "database-restored",
            database_restored=True,
            database_restore=restored,
            mutable_data_restored=restored_mutable,
        )

    def _reconcile_effect_commit_windows(
        self, descriptor: dict[str, Any], marker: dict[str, Any]
    ) -> None:
        """Fence old/exact-new states after a crash between effect and marker."""

        repository = self.repository_identity(require_ssh_origin=True)
        expected = descriptor["repository"]
        if (repository["sha"], repository["tree"]) == (
            expected["target_sha"],
            expected["target_tree"],
        ):
            marker["source_switched"] = True
        elif (repository["sha"], repository["tree"]) == (
            expected["previous_sha"],
            expected["previous_tree"],
        ):
            marker["source_switched"] = False
        else:
            raise PullDeployError(
                "live source is neither sealed previous nor candidate"
            )

        asset = descriptor["release_input"]["asset"]
        asset_current = self._asset_pointer_target(descriptor)
        asset_target = Path(asset["root"])
        asset_previous = (
            Path(asset["previous"]["root"]) if asset["previous"] is not None else None
        )
        if asset_current == asset_target:
            marker["asset_switched"] = True
        elif asset_current == asset_previous:
            marker["asset_switched"] = False
        else:
            raise PullDeployError(
                "asset pointer is neither sealed previous nor candidate"
            )

        unit = descriptor["monomer_md"]["systemd_unit"]
        unit_path = Path(unit["target_path"])
        unit_digest = None
        if unit_path.exists() or unit_path.is_symlink():
            if unit_path.is_symlink() or not unit_path.is_file():
                raise PullDeployError("installed Worker unit became unsafe")
            unit_digest = sha256_file(unit_path)
        if unit_digest == unit["sha256"]:
            marker["unit_switched"] = True
        elif (unit["previous_present"] and unit_digest == unit["previous_sha256"]) or (
            not unit["previous_present"] and unit_digest is None
        ):
            marker["unit_switched"] = False
        else:
            raise PullDeployError(
                "Worker unit is neither sealed previous nor candidate"
            )

        active: dict[str, Any] | None = None
        if self.active_slot_path.exists() or self.active_slot_path.is_symlink():
            active = validate_active_slot_record(
                load_private_json(self.active_slot_path)
            )
        candidate_slot = descriptor["monomer_md"]["slot_record"]
        candidate_digest = descriptor["monomer_md"]["slot_record_sha256"]
        if active is not None and (
            active["slot"] == candidate_slot["slot"]
            and active["source_sha"] == candidate_slot["source_sha"]
            and active["source_tree"] == candidate_slot["source_tree"]
            and active["worker_lock_sha256"] == candidate_slot["worker_lock_sha256"]
            and active["slot_record_sha256"] == candidate_digest
        ):
            marker["slot_switched"] = True
        else:
            previous = descriptor.get("previous_deployment")
            previous_active = (
                previous.get("active_monomer_md_slot")
                if isinstance(previous, dict)
                else None
            )
            if active == previous_active or (
                active is None and previous_active is None
            ):
                marker["slot_switched"] = False
            else:
                raise PullDeployError(
                    "active Worker slot is neither sealed previous nor candidate"
                )

        live_control = self.active_control_evidence()
        candidate_control = descriptor["controller"]["executor_control"]
        previous_control = descriptor["controller"]["previous_active_control"]
        if self._active_matches_candidate(live_control, candidate_control):
            marker["control_switched"] = True
        elif live_control == previous_control:
            marker["control_switched"] = False
        else:
            raise PullDeployError(
                "active controls are neither sealed previous nor candidate"
            )
        marker["reconciled_at"] = utc_now()
        self._write_marker(marker)

    def _record_restored_effect(
        self,
        marker: dict[str, Any],
        field: str,
    ) -> None:
        """Durably fence one successfully restored deployment effect.

        Previous and candidate releases may legitimately share source bytes,
        unit bytes, or an asset pointer.  Re-inspection alone cannot choose a
        side in that case, so the successful idempotent restore call is the
        commit authority for recording the effect as previous.
        """

        if field not in {
            "source_switched",
            "slot_switched",
            "unit_switched",
            "control_switched",
            "asset_switched",
        }:
            raise PullDeployError("unknown deployment effect restoration field")
        marker[field] = False
        marker["updated_at"] = utc_now()
        self._write_marker(marker)

    @staticmethod
    def _is_first_bridge(descriptor: dict[str, Any]) -> bool:
        return bool(
            descriptor.get("schema_version")
            == BRIDGE_DESCRIPTOR_SCHEMA_VERSION
            and descriptor.get("previous_deployment") is None
        )

    def _probe_restored_legacy_takeover(
        self,
        descriptor: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Recognize an exact completed restore without touching live Git."""

        if not self._is_first_bridge(descriptor):
            return None
        sealed = validate_legacy_takeover_binding(
            descriptor["legacy_takeover"]
        )
        try:
            status = _legacy_takeover_evidence.load_status(
                self.runtime_root,
                sealed["operation_id"],
            )
        except Exception as exc:
            raise PullDeployError(
                "legacy takeover restore status is unavailable"
            ) from exc
        if status.get("restore_phase") != "restored":
            return None
        try:
            restored = _legacy_takeover_evidence.validate_restored(
                self.runtime_root,
                sealed["operation_id"],
                sealed["authority_sha"],
                sealed["authority_tree"],
                expected_git_identity=sealed["git_identity"],
                status_document=status,
            )
        except Exception as exc:
            raise PullDeployError(
                "legacy takeover terminal restore is invalid"
            ) from exc
        for name in (
            "operation_id",
            "authority_sha",
            "authority_tree",
            "install_manifest_sha256",
            "classification_sha256",
            "runtime_identity_sha256",
            "git_identity",
            "pre_stopped_fence_sha256",
            "control_layout_sha256",
            "checkout_permissions_sha256",
            "applied_record_sha256",
        ):
            if restored.get(name) != sealed.get(name):
                raise PullDeployError(
                    "legacy takeover terminal restore belongs to another authority"
                )
        return restored

    def _finalize_restored_legacy_takeover(
        self,
        descriptor: dict[str, Any],
        marker: dict[str, Any],
        restored: dict[str, Any],
    ) -> None:
        """Commit marker truth after takeover already reopened legacy."""

        if (
            self.current_state_path.exists()
            or self.current_state_path.is_symlink()
        ):
            raise PullDeployError(
                "legacy takeover restored after candidate state committed"
            )
        if marker.get("database_change_started") is True and (
            marker.get("database_restored") is not True
            or marker.get("mutable_data_restored") is None
        ):
            raise PullDeployError(
                "legacy takeover restored before database rollback committed"
            )
        if marker.get("asset_switched") is not False:
            raise PullDeployError(
                "legacy takeover restored before asset pointer rollback committed"
            )
        if marker.get("slot_switched") is not False:
            raise PullDeployError(
                "legacy takeover restored before Worker slot rollback committed"
            )
        if self._active_slot() is not None:
            raise PullDeployError(
                "legacy takeover restored with a governed Worker slot active"
            )
        terminal = require_digest(
            restored.get("restored_terminal_sha256"),
            "legacy takeover restored terminal digest",
        )
        existing = marker.get("takeover_restored_terminal_sha256")
        if existing is not None and existing != terminal:
            raise PullDeployError(
                "legacy takeover terminal restore digest changed"
            )
        if existing == terminal:
            if (
                any(
                    marker.get(field) is not False
                    for field in (
                        "source_switched",
                        "slot_switched",
                        "control_switched",
                        "unit_switched",
                        "asset_switched",
                        "runtime_stopped",
                    )
                )
                or marker.get("runtime_start_intent") is not None
                or marker.get("verification") is not None
            ):
                raise PullDeployError(
                    "legacy takeover terminal marker is internally inconsistent"
                )
            return
        marker["source_switched"] = False
        marker["slot_switched"] = False
        marker["control_switched"] = False
        marker["unit_switched"] = False
        marker["asset_switched"] = False
        marker["runtime_stopped"] = False
        marker["takeover_restored_terminal_sha256"] = terminal
        marker.pop("runtime_start_intent", None)
        marker.pop("verification", None)
        marker["updated_at"] = utc_now()
        self._write_marker(marker)

    def _run_legacy_takeover_restore(
        self,
        command: list[str],
        *,
        deploy_lock_fd: int,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                "HOME": str(DEPLOY_USER_HOME),
                "USER": "devuser",
                "LOGNAME": "devuser",
                "PATH": SAFE_PATH,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            pass_fds=(deploy_lock_fd,),
            timeout=1800,
        )

    def _restore_legacy_takeover(
        self,
        descriptor: dict[str, Any],
        marker: dict[str, Any],
    ) -> dict[str, Any]:
        """Restore exact legacy state under Pull's already-held deploy lock."""

        if not self._is_first_bridge(descriptor):
            raise PullDeployError(
                "legacy takeover restore is restricted to the first bridge"
            )
        if self._held_deploy_lock_fd is None:
            raise PullDeployError(
                "legacy takeover restore lacks inherited deploy.lock ownership"
            )
        already = self._probe_restored_legacy_takeover(descriptor)
        if already is not None:
            self._finalize_restored_legacy_takeover(
                descriptor,
                marker,
                already,
            )
            return already
        sealed = validate_legacy_takeover_binding(
            descriptor["legacy_takeover"]
        )
        try:
            _legacy_takeover_evidence.validate_install_manifest(
                self.runtime_root,
                sealed["authority_sha"],
                sealed["authority_tree"],
            )
            status = _legacy_takeover_evidence.load_status(
                self.runtime_root,
                sealed["operation_id"],
            )
        except Exception as exc:
            raise PullDeployError(
                "legacy takeover restore authority is unavailable"
            ) from exc
        if (
            status.get("apply_phase") != "complete"
            or status.get("active") is not False
            or status.get("classification_sha256")
            != sealed["classification_sha256"]
            or status.get("runtime_identity_sha256")
            != sealed["runtime_identity_sha256"]
            or status.get("git_identity") != sealed["git_identity"]
            or status.get("pre_stopped_fence_sha256")
            != sealed["pre_stopped_fence_sha256"]
            or status.get("control_layout_sha256")
            != sealed["control_layout_sha256"]
            or status.get("checkout_permissions_sha256")
            != sealed["checkout_permissions_sha256"]
            or status.get("applied_record_sha256")
            != sealed["applied_record_sha256"]
        ):
            raise PullDeployError(
                "legacy takeover changed before exact restore"
            )
        intent = marker.get("takeover_restore_started")
        if intent is None:
            if status.get("restore_phase") is not None:
                raise PullDeployError(
                    "legacy takeover restore advanced without durable Pull intent"
                )
            try:
                control = (
                    _legacy_takeover_evidence.snapshot_current_control_layout(
                        self.runtime_root
                    )
                )
                permissions = (
                    _legacy_takeover_evidence.snapshot_current_checkout_permissions(
                        self.runtime_root,
                        sealed["operation_id"],
                    )
                )
            except Exception as exc:
                raise PullDeployError(
                    "cannot seal bootstrap replacement state for restore"
                ) from exc
            unit = Path(
                descriptor["monomer_md"]["systemd_unit"]["target_path"]
            )
            try:
                metadata = unit.lstat()
            except OSError as exc:
                raise PullDeployError(
                    "Worker unit replacement is unavailable for restore"
                ) from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or unit.is_symlink()
                or metadata.st_uid != os.geteuid()
            ):
                raise PullDeployError(
                    "Worker unit replacement is unsafe for restore"
                )
            worker_digest = sha256_file(unit)
            legacy_worker_digest = status["pre_stopped_fence"].get(
                "worker_unit_seal_sha256"
            )
            if worker_digest not in {
                descriptor["monomer_md"]["systemd_unit"]["sha256"],
                legacy_worker_digest,
            }:
                raise PullDeployError(
                    "Worker unit is neither sealed legacy nor candidate"
                )
            intent = {
                "operation_id": sealed["operation_id"],
                "worker_unit_sha256": worker_digest,
                "control_layout_sha256": control["sha256"],
                "checkout_permissions_sha256": permissions["sha256"],
                "started_at": utc_now(),
            }
            marker["takeover_restore_started"] = intent
            marker["updated_at"] = utc_now()
            self._write_marker(marker)
        capsule = self._prepare_bridge_recovery_capsule(
            descriptor,
            require_digest(
                marker.get("descriptor_sha256"),
                "bridge recovery descriptor digest",
            ),
        )
        capsule_binding = self._bridge_recovery_capsule_binding(capsule)
        existing_capsule = marker.get("bridge_recovery_capsule")
        if existing_capsule is None:
            marker["bridge_recovery_capsule"] = capsule_binding
            marker["updated_at"] = utc_now()
            self._write_marker(marker)
        elif existing_capsule != capsule_binding:
            raise PullDeployError(
                "bridge recovery capsule changed before legacy restore"
            )
        launcher = (
            self.runtime_root
            / "legacy-takeover/bin/nexpoly-legacy-takeover"
        )
        command = [
            str(launcher),
            "restore",
            "--operation-id",
            sealed["operation_id"],
            "--expected-worker-unit-sha256",
            intent["worker_unit_sha256"],
            "--expected-control-layout-sha256",
            intent["control_layout_sha256"],
            "--expected-checkout-permissions-sha256",
            intent["checkout_permissions_sha256"],
            "--parent-deploy-lock-fd",
            str(self._held_deploy_lock_fd),
        ]
        try:
            completed = self._run_legacy_takeover_restore(
                command,
                deploy_lock_fd=self._held_deploy_lock_fd,
            )
            response = json.loads(completed.stdout)
            _legacy_takeover_evidence.validate_status_document(
                response,
                sealed["operation_id"],
            )
        except Exception as exc:
            raise PullDeployError(
                "exact legacy takeover restore did not complete"
            ) from exc
        restored = self._probe_restored_legacy_takeover(descriptor)
        if restored is None:
            raise PullDeployError(
                "legacy takeover restore lacks terminal evidence"
            )
        self._finalize_restored_legacy_takeover(
            descriptor,
            marker,
            restored,
        )
        return restored

    def _is_pre_stop_abort_marker(
        self,
        descriptor: dict[str, Any],
        marker: dict[str, Any],
    ) -> bool:
        if (
            descriptor.get("schema_version")
            == BRIDGE_DESCRIPTOR_SCHEMA_VERSION
            and descriptor.get("previous_deployment") is None
        ):
            # The exact legacy runtime was already stopped by takeover before
            # Pull began.  Even a failure in Pull's "prepared" phase must use
            # the takeover restore state machine, never an in-place bootstrap
            # resume path.
            return False
        failed_phase = marker.get("failed_phase", marker.get("phase"))
        if not (
            marker.get("action") == "deploy"
            and failed_phase in {"prepared", "drain-started", "drained"}
            and marker.get("runtime_stopped") is False
            and marker.get("database_change_started") is False
            and all(
                marker.get(field) is False
                for field in (
                    "source_switched",
                    "slot_switched",
                    "unit_switched",
                    "control_switched",
                    "asset_switched",
                )
            )
        ):
            return False
        previous = descriptor.get("previous_deployment")
        previous_digest = descriptor.get("previous_deployment_sha256")
        current_exists = (
            self.current_state_path.exists() or self.current_state_path.is_symlink()
        )
        if previous is None:
            return not current_exists and previous_digest is None
        if not isinstance(previous, dict) or not isinstance(previous_digest, str):
            return False
        return bool(
            current_exists
            and sha256_file(self.current_state_path) == previous_digest
            and load_private_json(self.current_state_path) == previous
        )

    def _rollback_first_bridge(
        self,
        descriptor: dict[str, Any],
        marker: dict[str, Any],
    ) -> None:
        """Roll the failed F -> B attempt back through exact takeover state."""

        restored = self._probe_restored_legacy_takeover(descriptor)
        if restored is not None:
            self._finalize_restored_legacy_takeover(
                descriptor,
                marker,
                restored,
            )
            return
        restore_intent = marker.get("takeover_restore_started")
        if restore_intent is not None:
            # Database, asset and slot rollback commit before the external
            # takeover restore is ever invoked.  A partial restore can already
            # have returned Git to HTTPS and restored ignored files, so it
            # must resume directly without generic SSH/clean-tree inspection.
            if (
                marker.get("asset_switched") is not False
                or marker.get("slot_switched") is not False
                or marker.get("database_change_started") is True
                and marker.get("database_restored") is not True
            ):
                raise PullDeployError(
                    "partial takeover restore lacks prerequisite rollback commits"
                )
            self._restore_legacy_takeover(descriptor, marker)
            return

        self._reconcile_effect_commit_windows(descriptor, marker)
        candidate_may_be_live = bool(
            marker.get("runtime_start_intent") is not None
            or marker.get("verification") is not None
        )
        if candidate_may_be_live:
            # This is the governed candidate runtime, not the pre-stopped
            # legacy runtime.  Isolate and drain only this exact instance.
            recovery = self._prepare_runtime_recovery(
                marker,
                descriptor,
                allow_unfenced=True,
            )
            if recovery["runtime_state"] == "drained":
                self._stop_runtime(marker, descriptor)
            marker["runtime_stopped"] = True
            marker["updated_at"] = utc_now()
            self._write_marker(marker)
        self._restore_database_after_failed_apply(descriptor, marker)
        if marker.get("slot_switched") is True:
            self._restore_previous_slot(descriptor)
        self._record_restored_effect(marker, "slot_switched")
        self._restore_previous_asset_pointer(descriptor)
        self._record_restored_effect(marker, "asset_switched")
        self._restore_legacy_takeover(descriptor, marker)

    def _rollback_failed_attempt(
        self, descriptor: dict[str, Any], marker: dict[str, Any]
    ) -> None:
        failed_phase = marker.get("failed_phase", marker.get("phase"))
        abort_before_stop = self._is_pre_stop_abort_marker(descriptor, marker)
        if abort_before_stop:
            # Drain may have timed out with an accepted job still running.  No
            # stop or switch intent was committed, so never restart/restore a
            # process here.  Re-open the unchanged runtime in place; failure
            # leaves the marker and partial drain fail-closed for an operator.
            if descriptor.get("previous_deployment") is None:
                self.lifecycle.resume_bootstrap_unchanged(self, descriptor)
            else:
                previous = descriptor["previous_deployment"]
                self._validate_steady_deployment_state(previous)
                previous_runtime = self._previous_runtime_descriptor(descriptor)
                self._recover_unchanged_and_resume(
                    marker,
                    previous_runtime,
                    allow_unfenced=marker.get("verification") is None,
                )
            marker["pre_stop_abort"] = True
            marker["updated_at"] = utc_now()
            self._write_marker(marker)
            return

        if self._is_first_bridge(descriptor):
            self._rollback_first_bridge(descriptor, marker)
            return

        self._reconcile_effect_commit_windows(descriptor, marker)
        bootstrap_effects_restored = descriptor.get(
            "previous_deployment"
        ) is None and all(
            marker.get(field) is False
            for field in (
                "source_switched",
                "slot_switched",
                "unit_switched",
                "control_switched",
                "asset_switched",
            )
        )
        if bootstrap_effects_restored and self.lifecycle.bootstrap_can_resume_unchanged(
            self, descriptor
        ):
            # A full legacy restore may have committed and reopened admission
            # before its response/marker update was lost.  Detect it before
            # any generic stop path: active work may already have been
            # accepted by the restored runtime.
            self.lifecycle.resume_bootstrap_unchanged(self, descriptor)
            return
        stop_required = (
            marker.get("runtime_stopped") is True
            or failed_phase in STOP_INTENT_PHASES
            or any(
                marker.get(field) is True
                for field in (
                    "source_switched",
                    "slot_switched",
                    "control_switched",
                )
            )
        )
        if stop_required:
            # Stop intent is an unknown-commit boundary.  A process may have
            # stopped all or only some source readers before crashing; an
            # idempotent successful stop is mandatory before any Git/config
            # restoration.  Failure leaves the marker and ingress isolated.
            runtime_descriptor = descriptor
            if (
                all(
                    marker.get(field) is False
                    for field in (
                        "source_switched",
                        "slot_switched",
                        "control_switched",
                        "unit_switched",
                        "asset_switched",
                    )
                )
                and descriptor.get("previous_deployment") is not None
                and isinstance(descriptor.get("controller"), dict)
            ):
                runtime_descriptor = self._previous_runtime_descriptor(descriptor)
            recovery = self._prepare_runtime_recovery(
                marker,
                runtime_descriptor,
                allow_unfenced=marker.get("verification") is None,
            )
            if marker.get("phase") not in {
                "runtime-stop-started",
                "runtime-stopped",
            }:
                self._advance(
                    marker,
                    "runtime-stop-started",
                    drain=recovery,
                )
            if recovery["runtime_state"] == "drained":
                self._stop_runtime(marker, runtime_descriptor)
            self._advance(marker, "runtime-stopped", runtime_stopped=True)
        self._restore_database_after_failed_apply(descriptor, marker)
        if marker.get("source_switched") is True:
            self._restore_source(descriptor)
            self._record_restored_effect(marker, "source_switched")
        if marker.get("slot_switched") is True:
            self._restore_previous_slot(descriptor)
            self._record_restored_effect(marker, "slot_switched")
        self._restore_previous_worker_unit(descriptor)
        self._record_restored_effect(marker, "unit_switched")
        self._restore_previous_asset_pointer(descriptor)
        self._record_restored_effect(marker, "asset_switched")
        if marker.get("control_switched") is True:
            self._restore_previous_control(descriptor)
            self._record_restored_effect(marker, "control_switched")
        if descriptor.get("previous_deployment") is None:
            # A previous restore may have committed and reopened legacy
            # admission before its stdout/marker write was lost.  Probe first:
            # restarting that exact open runtime could kill newly accepted
            # work.  The unchanged path is ingress-only and process-fenced.
            hook = self.config_dir / "bootstrap-rollback"
            if not hook.exists():
                raise PullDeployError(
                    "failed bootstrap has no audited legacy rollback hook"
                )
            SystemLifecycle()._run_bootstrap_hook(
                self,
                "bootstrap-rollback",
                descriptor["production_config"],
            )
            return
        if any(
            marker.get(field) is not False
            for field in (
                "source_switched",
                "slot_switched",
                "unit_switched",
                "control_switched",
                "asset_switched",
            )
        ):
            raise PullDeployError(
                "failed deployment rollback did not restore every governed effect"
            )
        previous = descriptor["previous_deployment"]
        self._validate_steady_deployment_state(previous)
        previous_descriptor = self._previous_runtime_descriptor(descriptor)
        self._recover_runtime_and_resume(
            marker,
            previous_descriptor,
            allow_unfenced=marker.get("verification") is None,
        )

    def _deployment_terminal_audit_binding(
        self,
        *,
        operation_id: str,
        descriptor_sha256: str,
        state_sha256: str,
        source_sha: str,
        source_tree: str,
        expected_terminal_sha256: str | None = None,
        expected_state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Open the unique immutable success audit that committed one state."""

        operation_id = require_operation_id(operation_id)
        descriptor_sha256 = require_digest(
            descriptor_sha256,
            "deployment terminal descriptor digest",
        )
        state_sha256 = require_digest(
            state_sha256,
            "deployment terminal state digest",
        )
        require_sha(source_sha, "deployment terminal source SHA")
        require_sha(source_tree, "deployment terminal source tree")
        if expected_terminal_sha256 is not None:
            expected_terminal_sha256 = require_digest(
                expected_terminal_sha256,
                "deployment terminal audit digest",
            )
        matches: list[dict[str, Any]] = []
        for directory in self._operation_directories(operation_id):
            if not (directory.exists() or directory.is_symlink()):
                continue
            ensure_private_directory(directory)
            for status in ("success", "recovered-success"):
                path = directory / f"{status}.json"
                if not (path.exists() or path.is_symlink()):
                    continue
                audit = load_private_json(path)
                audit_digest = sha256_file(path)
                candidate = audit.get("candidate_state")
                if not isinstance(candidate, dict):
                    continue
                candidate = validate_current_deployment_state(candidate)
                candidate_digest = sha256_bytes(
                    canonical_json_bytes(candidate) + b"\n"
                )
                if (
                    audit.get("status") != status
                    or audit.get("operation_id") != operation_id
                    or audit.get("descriptor_sha256") != descriptor_sha256
                    or audit.get("candidate_state_sha256") != state_sha256
                    or candidate_digest != state_sha256
                    or candidate.get("operation_id") != operation_id
                    or candidate.get("descriptor_sha256")
                    != descriptor_sha256
                    or candidate.get("source_sha") != source_sha
                    or candidate.get("source_tree") != source_tree
                    or (
                        expected_terminal_sha256 is not None
                        and audit_digest != expected_terminal_sha256
                    )
                    or (
                        expected_state is not None
                        and candidate != dict(expected_state)
                    )
                ):
                    continue
                matches.append(
                    {
                        "path": str(path),
                        "sha256": audit_digest,
                        "state": candidate,
                    }
                )
        if not matches or (
            expected_terminal_sha256 is not None and len(matches) != 1
        ):
            raise PullDeployError(
                "deployment source state lacks one exact immutable success audit"
            )
        # A crash after success.json but before operation-state.json can cause
        # recovery to add recovered-success.json for the same exact candidate.
        # During preflight choose success.json deterministically; once its
        # digest is sealed in the rollback marker/provenance, require exactly
        # that one immutable audit.
        matches.sort(
            key=lambda match: (
                0 if Path(match["path"]).name == "success.json" else 1,
                match["path"],
            )
        )
        return matches[0]

    def _load_exact_prepared_descriptor(
        self,
        operation_id: str,
        descriptor_sha256: str,
    ) -> dict[str, Any]:
        operation_id = require_operation_id(operation_id)
        descriptor_sha256 = require_digest(
            descriptor_sha256,
            "prepared descriptor identity",
        )
        operation_root, descriptor_path, ready_path = self._operation_paths(
            operation_id
        )
        if (
            descriptor_path.parent != operation_root
            or not descriptor_path.is_file()
            or descriptor_path.is_symlink()
            or not ready_path.is_file()
            or ready_path.is_symlink()
            or sha256_file(descriptor_path) != descriptor_sha256
        ):
            raise PullDeployError(
                "terminal state prepared descriptor is unavailable"
            )
        descriptor = validate_descriptor(
            load_private_json(descriptor_path)
        )
        ready = load_private_json(ready_path)
        self._validate_ready(ready, descriptor, descriptor_path)
        if descriptor.get("operation_id") != operation_id:
            raise PullDeployError(
                "terminal state prepared descriptor operation differs"
            )
        return descriptor

    def _successful_contract_journals(
        self,
    ) -> list[tuple[Path, dict[str, Any]]]:
        root = self.runtime_root / "state" / "contract-operations"
        if not (root.exists() or root.is_symlink()):
            return []
        ensure_private_directory(root)
        result: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(root.glob("*.json")):
            document = load_private_json(path)
            if document.get("status") != "success":
                continue
            if (
                document.get("schema_version") != 2
                or not isinstance(
                    document.get("deployment_operation_id"), str
                )
                or not isinstance(
                    document.get("pull_descriptor_sha256"), str
                )
            ):
                raise PullDeployError(
                    "successful 0012 journal lacks terminal schema V2 authority"
                )
            operation_id = require_operation_id(
                str(document.get("operation_id", ""))
            )
            require_operation_id(
                document["deployment_operation_id"]
            )
            require_digest(
                document["pull_descriptor_sha256"],
                "successful 0012 deployment descriptor",
            )
            if path.name != f"{operation_id}.json":
                raise PullDeployError(
                    "successful 0012 journal path differs from its operation"
                )
            result.append((path, document))
        return result

    def _reject_unrecorded_contract_success(
        self,
        state: Mapping[str, Any],
    ) -> None:
        for _path, journal in self._successful_contract_journals():
            if (
                journal.get("deployment_operation_id")
                == state.get("operation_id")
                and journal.get("pull_descriptor_sha256")
                == state.get("descriptor_sha256")
            ):
                raise PullDeployError(
                    "deployment state omits an already successful 0012 operation"
                )

    def _contract_anchor_state(
        self,
        state: Mapping[str, Any],
        *,
        contract_operation_id: str,
        deployment_operation_id: str,
        descriptor_sha256: str,
        seen: set[str] | None = None,
    ) -> dict[str, Any]:
        candidate = validate_current_deployment_state(dict(state))
        digest = sha256_bytes(canonical_json_bytes(candidate) + b"\n")
        visited = set() if seen is None else seen
        if digest in visited or len(visited) >= 32:
            raise PullDeployError(
                "0012 deployment ancestry contains a cycle"
            )
        visited.add(digest)
        if candidate.get("last_contract_operation") != contract_operation_id:
            raise PullDeployError(
                "0012 deployment ancestry lost its contract operation"
            )
        provenance = candidate.get("rollback_provenance")
        if isinstance(provenance, dict):
            source_descriptor = self._load_exact_prepared_descriptor(
                provenance["from_operation_id"],
                provenance["from_descriptor_sha256"],
            )
            sealed = source_descriptor.get("previous_deployment")
            if not isinstance(sealed, dict):
                raise PullDeployError(
                    "rollback ancestry lacks its sealed pre-0012 deployment"
                )
            return self._contract_anchor_state(
                sealed,
                contract_operation_id=contract_operation_id,
                deployment_operation_id=deployment_operation_id,
                descriptor_sha256=descriptor_sha256,
                seen=visited,
            )
        if (
            candidate.get("operation_id") == deployment_operation_id
            and candidate.get("descriptor_sha256") == descriptor_sha256
        ):
            return candidate
        descriptor = self._validate_state_source_descriptor(candidate)
        previous = descriptor.get("previous_deployment")
        if not isinstance(previous, dict):
            raise PullDeployError(
                "0012 deployment ancestry does not reach its deployment"
            )
        return self._contract_anchor_state(
            previous,
            contract_operation_id=contract_operation_id,
            deployment_operation_id=deployment_operation_id,
            descriptor_sha256=descriptor_sha256,
            seen=visited,
        )

    def _project_contract_terminal_state(
        self,
        pre_state: Mapping[str, Any],
        *,
        descriptor: Mapping[str, Any],
        operation_id: str,
        approval: Mapping[str, Any],
        mutable_pair: Mapping[str, Any],
        external_pair: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        before = validate_current_deployment_state(dict(pre_state))
        records = descriptor["migrations"].get("records")
        if not isinstance(records, list):
            raise PullDeployError(
                "0012 terminal descriptor migration manifest is invalid"
            )
        contract_indexes = [
            index
            for index, record in enumerate(records)
            if isinstance(record, dict)
            and record.get("version") == "0012_drop_polytao_jobs"
        ]
        if contract_indexes != [len(records) - 1]:
            raise PullDeployError(
                "0012 terminal descriptor contract position differs"
            )
        contract_index = contract_indexes[0]
        if before["migrations"] != records[:contract_index]:
            raise PullDeployError(
                "0012 terminal pre-state is not the canonical manifest prefix"
            )
        if (
            before.get("last_contract_operation") is not None
            or before.get("contract_mutable_data_audit") is not None
            or before.get("contract_external_database_audit") is not None
        ):
            raise PullDeployError(
                "0012 terminal pre-state already contains contract state"
            )
        projected = json.loads(json.dumps(before))
        projected["migrations"] = [
            *json.loads(json.dumps(before["migrations"])),
            dict(records[contract_index]),
        ]
        approvals = projected.get("approved_contracts")
        if not isinstance(approvals, list):
            raise PullDeployError(
                "0012 terminal pre-state approvals are invalid"
            )
        projected["approved_contracts"] = [
            *approvals,
            dict(approval),
        ]
        projected["migration_epoch_barrier"] = {
            "epoch": 1,
            "contract": {
                "version": "0012_drop_polytao_jobs",
                "checksum": records[contract_index]["checksum"],
            },
            "operation_id": operation_id,
            "approved_at": approval["approved_at"],
        }
        projected["schema_compatibility_floor"] = {
            "version": "0012_drop_polytao_jobs",
            "checksum": records[contract_index]["checksum"],
        }
        projected["last_contract_operation"] = operation_id
        projected["contract_mutable_data_audit"] = dict(mutable_pair)
        if external_pair is not None:
            projected["contract_external_database_audit"] = dict(
                external_pair
            )
        compatibility = before.get("migration_compatibility")
        if compatibility is not None:
            projected["migration_compatibility"] = (
                build_migration_compatibility_state(
                    compatibility,
                    code_manifest_sha256=descriptor["migrations"]["sha256"],
                    migrations=projected["migrations"],
                )
            )
        return validate_current_deployment_state(projected)

    def _validate_contract_audit_manifest(
        self,
        journal: Mapping[str, Any],
        *,
        operation_id: str,
        require_external: bool,
    ) -> None:
        audit_dir = (
            self.runtime_root
            / "audit"
            / "contracts"
            / "0012"
            / operation_id
        )
        ensure_private_directory(audit_dir)
        manifest_path = audit_dir / "AUDIT-MANIFEST.json"
        manifest = load_private_json(manifest_path)
        if (
            manifest != journal.get("audit_manifest")
            or sha256_file(manifest_path)
            != journal.get("audit_manifest_sha256")
            or set(manifest) != {
                "schema_version",
                "operation_id",
                "contract_version",
                "contract_checksum",
                "files",
            }
            or manifest.get("schema_version") != 1
            or manifest.get("operation_id") != operation_id
            or manifest.get("contract_version")
            != "0012_drop_polytao_jobs"
            or manifest.get("contract_checksum")
            != _site_helper_contracts.CANONICAL_MIGRATION_LEDGER[11][1]
            or not isinstance(manifest.get("files"), list)
        ):
            raise PullDeployError(
                "0012 terminal audit manifest identity differs"
            )
        expected_names = {
            "mutable-data.transition.json",
            "mutable-data.before.json",
            "mutable-data.after.json",
            "pull-state.before.json",
            "pull-state.after.json",
        }
        if require_external:
            expected_names.update(
                {
                    "external-database.transition.json",
                    "external-database.after.json",
                }
            )
        seen: set[str] = set()
        for record in manifest["files"]:
            if (
                not isinstance(record, dict)
                or set(record) != {"name", "size", "sha256"}
                or not isinstance(record.get("name"), str)
                or Path(record["name"]).name != record["name"]
                or record["name"] in {"", ".", "..", "AUDIT-MANIFEST.json"}
                or record["name"] in seen
                or not isinstance(record.get("size"), int)
                or isinstance(record.get("size"), bool)
                or record["size"] < 0
            ):
                raise PullDeployError(
                    "0012 terminal audit manifest file record is invalid"
                )
            require_digest(
                record.get("sha256"),
                "0012 terminal audit file digest",
            )
            seen.add(record["name"])
            path = audit_dir / record["name"]
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise PullDeployError(
                    "0012 terminal audit file is unavailable"
                ) from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or path.is_symlink()
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size != record["size"]
                or sha256_file(path) != record["sha256"]
            ):
                raise PullDeployError(
                    "0012 terminal audit file differs from its manifest"
                )
        if not expected_names.issubset(seen):
            raise PullDeployError(
                "0012 terminal audit manifest omits transition evidence"
            )

    def _validate_contract_steady_terminal(
        self,
        state: Mapping[str, Any],
        *,
        state_sha256: str,
    ) -> str:
        operation_id = require_operation_id(
            str(state.get("last_contract_operation", ""))
        )
        all_journals = self._successful_contract_journals()
        operation_matches = [
            (path, journal)
            for path, journal in all_journals
            if journal.get("operation_id") == operation_id
        ]
        if len(operation_matches) != 1:
            raise PullDeployError(
                "post-0012 state lacks one unique successful operation journal"
            )
        direct_journal = operation_matches[0]
        path, journal = direct_journal
        if path.name != f"{operation_id}.json":
            raise PullDeployError(
                "0012 success journal path differs from its operation"
            )
        deployment_operation_id = require_operation_id(
            str(journal.get("deployment_operation_id", ""))
        )
        descriptor_sha256 = require_digest(
            journal.get("pull_descriptor_sha256"),
            "0012 deployment descriptor digest",
        )
        lineage_matches = [
            (candidate_path, candidate)
            for candidate_path, candidate in all_journals
            if (
                candidate.get("deployment_operation_id")
                == deployment_operation_id
                and candidate.get("pull_descriptor_sha256")
                == descriptor_sha256
            )
        ]
        if lineage_matches != [direct_journal]:
            raise PullDeployError(
                "0012 deployment has ambiguous success journals"
            )
        anchor = self._contract_anchor_state(
            state,
            contract_operation_id=operation_id,
            deployment_operation_id=deployment_operation_id,
            descriptor_sha256=descriptor_sha256,
        )
        anchor_sha256 = sha256_bytes(
            canonical_json_bytes(anchor) + b"\n"
        )
        if state.get("operation_id") == deployment_operation_id:
            if state_sha256 != anchor_sha256:
                raise PullDeployError(
                    "direct post-0012 state differs from its contract anchor"
                )
        descriptor = self._load_exact_prepared_descriptor(
            deployment_operation_id,
            descriptor_sha256,
        )
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
            "contract_mutable_data_audit",
            "deployment_operation_id",
            "pull_descriptor_sha256",
            "pre_state_sha256",
            "post_state_sha256",
            "contract_mutable_data_audit_sha256",
        }
        has_external = anchor.get(
            "contract_external_database_audit"
        ) is not None
        if has_external:
            expected_fields.update(
                {
                    "contract_external_database_audit",
                    "contract_external_database_audit_sha256",
                }
            )
        if (
            set(journal) != expected_fields
            or journal.get("schema_version") != 2
            or journal.get("status") != "success"
            or journal.get("operation_id") != operation_id
            or journal.get("deployment_operation_id")
            != deployment_operation_id
            or journal.get("pull_descriptor_sha256")
            != descriptor_sha256
            or journal.get("source_sha")
            != descriptor["repository"]["target_sha"]
            or journal.get("post_state_sha256") != anchor_sha256
            or journal.get("verification")
            != {"schema_version": 1, "verified": True}
            or not isinstance(
                journal.get("ingress_isolated_canary"), dict
            )
            or journal["ingress_isolated_canary"].get("status")
            != "passed"
            or journal["ingress_isolated_canary"].get(
                "ingress_isolated"
            )
            is not True
            or canonical_json_digest(
                journal["ingress_isolated_canary"]
            )
            != journal.get("ingress_isolated_canary_sha256")
        ):
            raise PullDeployError(
                "0012 success journal terminal identity differs"
            )
        pre_state_sha256 = require_digest(
            journal.get("pre_state_sha256"),
            "0012 pre-state digest",
        )
        pre_terminal = self._deployment_terminal_audit_binding(
            operation_id=deployment_operation_id,
            descriptor_sha256=descriptor_sha256,
            state_sha256=pre_state_sha256,
            source_sha=descriptor["repository"]["target_sha"],
            source_tree=descriptor["repository"]["target_tree"],
        )
        if (
            pre_terminal["state"].get("last_contract_operation")
            is not None
            or pre_terminal["state"].get("contract_mutable_data_audit")
            is not None
            or pre_terminal["state"].get(
                "contract_external_database_audit"
            )
            is not None
        ):
            raise PullDeployError(
                "0012 journal pre-state already records a contract"
            )
        approval = journal.get("approval")
        approvals = anchor.get("approved_contracts")
        if (
            not isinstance(approval, dict)
            or not isinstance(approvals, list)
            or [record for record in approvals if record == approval]
            != [approval]
            or approval.get("operation_id") != operation_id
            or approval.get("version") != "0012_drop_polytao_jobs"
            or approval.get("checksum")
            != _site_helper_contracts.CANONICAL_MIGRATION_LEDGER[11][1]
            or journal.get("completed_at") != approval.get("approved_at")
        ):
            raise PullDeployError(
                "0012 success journal approval differs from current state"
            )
        mutable_pair = validate_mutable_data_pair(
            journal.get("contract_mutable_data_audit")
        )
        if (
            mutable_pair != anchor.get("contract_mutable_data_audit")
            or canonical_json_digest(mutable_pair)
            != journal.get("contract_mutable_data_audit_sha256")
        ):
            raise PullDeployError(
                "0012 success journal mutable transition differs"
            )
        external_pair: dict[str, Any] | None = None
        if has_external:
            external_pair = validate_external_database_contract_pair(
                journal.get("contract_external_database_audit"),
                before_binding=anchor.get("external_database_audit"),
            )
            if (
                external_pair
                != anchor.get("contract_external_database_audit")
                or canonical_json_digest(external_pair)
                != journal.get(
                    "contract_external_database_audit_sha256"
                )
            ):
                raise PullDeployError(
                    "0012 success journal external transition differs"
                )
        expected_anchor = self._project_contract_terminal_state(
            pre_terminal["state"],
            descriptor=descriptor,
            operation_id=operation_id,
            approval=approval,
            mutable_pair=mutable_pair,
            external_pair=external_pair,
        )
        if expected_anchor != anchor:
            raise PullDeployError(
                "0012 journal does not reconstruct its exact post-state"
            )
        audit_dir = (
            self.runtime_root
            / "audit"
            / "contracts"
            / "0012"
            / operation_id
        )
        if (
            load_private_json(audit_dir / "pull-state.before.json")
            != pre_terminal["state"]
            or load_private_json(audit_dir / "pull-state.after.json")
            != anchor
            or load_private_json(
                audit_dir / "mutable-data.transition.json"
            )
            != mutable_pair
            or load_private_json(audit_dir / "mutable-data.before.json")
            != mutable_pair["before"]
            or load_private_json(audit_dir / "mutable-data.after.json")
            != mutable_pair["after"]
        ):
            raise PullDeployError(
                "0012 terminal pull-state evidence differs"
            )
        if has_external and (
            load_private_json(
                audit_dir / "external-database.transition.json"
            )
            != external_pair
            or load_private_json(
                audit_dir / "external-database.after.json"
            )
            != external_pair["after_snapshot"]
        ):
            raise PullDeployError(
                "0012 terminal external audit evidence differs"
            )
        backup = journal.get("database_backup")
        backup_sha256 = require_digest(
            journal.get("database_backup_sha256"),
            "0012 database backup digest",
        )
        backup_path = Path(str(backup))
        backup_root = (
            self.runtime_root / "backups" / "contracts" / "0012"
        )
        try:
            metadata = backup_path.lstat()
            backup_path.relative_to(backup_root)
        except (OSError, ValueError) as exc:
            raise PullDeployError(
                "0012 terminal database backup is unavailable"
            ) from exc
        if (
            not backup_path.is_absolute()
            or not stat.S_ISREG(metadata.st_mode)
            or backup_path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or sha256_file(backup_path) != backup_sha256
        ):
            raise PullDeployError(
                "0012 terminal database backup identity differs"
            )
        self._validate_contract_audit_manifest(
            journal,
            operation_id=operation_id,
            require_external=has_external,
        )
        operation_state = self._load_operation_state(
            deployment_operation_id
        )
        if (
            operation_state is None
            or operation_state.get("outcome") != "deployed"
            or operation_state.get("descriptor_sha256")
            != descriptor_sha256
        ):
            raise PullDeployError(
                "0012 source deployment lacks its deployed outcome"
            )
        return deployment_operation_id

    def _validate_steady_rollback_terminal(
        self,
        state: Mapping[str, Any],
        *,
        state_sha256: str,
    ) -> dict[str, Any]:
        provenance = validate_rollback_provenance(
            state.get("rollback_provenance"),
            state=state,
            mutable_pair=validate_mutable_data_pair(
                state.get("mutable_data_audit")
            ),
            final_mutable_pair=(
                validate_mutable_data_pair(
                    state.get("final_mutable_data_audit")
                )
                if state.get("final_mutable_data_audit") is not None
                else None
            ),
            final_external_pair=(
                validate_external_database_final_pair(
                    state.get("final_external_database_audit"),
                    before_binding=external_database_endpoint(
                        validate_external_database_audit_binding(
                            state.get("external_database_audit")
                        ),
                        contract_pair=state.get(
                            "contract_external_database_audit"
                        ),
                    ),
                )
                if state.get("final_external_database_audit") is not None
                else None
            ),
            history_ledger=[
                {
                    "version": record["version"],
                    "checksum": record["checksum"],
                }
                for record in state["migrations"]
            ],
        )
        source_operation = provenance["from_operation_id"]
        source_descriptor_digest = provenance["from_descriptor_sha256"]
        operation_state = self._load_operation_state(source_operation)
        if (
            operation_state is None
            or operation_state.get("outcome") != "rolled-back"
            or operation_state.get("descriptor_sha256")
            != source_descriptor_digest
        ):
            raise PullDeployError(
                "rollback source operation lacks its rolled-back terminal outcome"
            )
        source_descriptor, observed_digest = self._load_prepared(
            source_operation,
            bridge_token_statuses=frozenset({"consumed"}),
        )
        if observed_digest != source_descriptor_digest:
            raise PullDeployError(
                "rollback source terminal descriptor identity differs"
            )

        matches: list[dict[str, Any]] = []
        for directory in self._operation_directories(source_operation):
            if not (directory.exists() or directory.is_symlink()):
                continue
            ensure_private_directory(directory)
            for status in (
                "explicit-rollback",
                "recovered-explicit-rollback",
            ):
                path = directory / f"{status}.json"
                if not (path.exists() or path.is_symlink()):
                    continue
                audit = load_private_json(path)
                recorded_at = audit.pop("recorded_at", None)
                observed_status = audit.pop("status", None)
                if (
                    not isinstance(recorded_at, str)
                    or not recorded_at
                    or observed_status != status
                ):
                    continue
                try:
                    marker = validate_recovery_marker(
                        audit,
                        descriptor=source_descriptor,
                        descriptor_digest=source_descriptor_digest,
                    )
                except PullDeployError:
                    continue
                candidate = marker.get("rollback_candidate_state")
                if (
                    marker.get("action") != "explicit-rollback"
                    or marker.get("phase") != "explicit-rollback-complete"
                    or marker.get("rollback_candidate_state_sha256")
                    != state_sha256
                    or candidate != dict(state)
                    or marker.get("rollback_current_state_sha256")
                    != provenance["from_state_sha256"]
                    or marker.get(
                        "rollback_source_terminal_audit_sha256"
                    )
                    != provenance["from_terminal_audit_sha256"]
                    or marker.get("runtime_stopped") is not True
                    or any(
                        marker.get(field) is not False
                        for field in (
                            "source_switched",
                            "slot_switched",
                            "control_switched",
                            "unit_switched",
                            "asset_switched",
                        )
                    )
                ):
                    continue
                matches.append(
                    {
                        "path": str(path),
                        "sha256": sha256_file(path),
                    }
                )
        if not matches:
            raise PullDeployError(
                "rollback state lacks one exact immutable rollback terminal audit"
            )
        matches.sort(
            key=lambda match: (
                0
                if Path(match["path"]).name == "explicit-rollback.json"
                else 1,
                match["path"],
            )
        )
        return matches[0]

    def _validate_steady_deployment_state(
        self,
        state: object,
        *,
        revalidate_live: bool = True,
    ) -> dict[str, Any]:
        """Validate a marker-free state against its immutable terminal chain."""

        validated = self._validate_external_database_state_provenance(state)
        state_sha256 = sha256_bytes(
            canonical_json_bytes(validated) + b"\n"
        )
        target_operation = self._load_operation_state(
            validated["operation_id"]
        )
        if (
            target_operation is None
            or target_operation.get("outcome") != "deployed"
            or target_operation.get("descriptor_sha256")
            != validated["descriptor_sha256"]
        ):
            raise PullDeployError(
                "current deployment lacks its deployed terminal outcome"
            )
        contract_deployment_operation: str | None = None
        if validated.get("last_contract_operation") is not None:
            contract_deployment_operation = (
                self._validate_contract_steady_terminal(
                    validated,
                    state_sha256=state_sha256,
                )
            )
        if validated.get("rollback_provenance") is not None:
            self._validate_steady_rollback_terminal(
                validated,
                state_sha256=state_sha256,
            )
        elif (
            contract_deployment_operation is None
            or validated["operation_id"]
            != contract_deployment_operation
        ):
            self._deployment_terminal_audit_binding(
                operation_id=validated["operation_id"],
                descriptor_sha256=validated["descriptor_sha256"],
                state_sha256=state_sha256,
                source_sha=validated["source_sha"],
                source_tree=validated["source_tree"],
                expected_state=validated,
            )
        if contract_deployment_operation is None:
            self._reject_unrecorded_contract_success(validated)
        if revalidate_live:
            raw_active = validated.get("external_database_audit")
            if raw_active is not None:
                active = validate_external_database_audit_binding(
                    raw_active
                )
                expected = external_database_endpoint(
                    active,
                    contract_pair=validated.get(
                        "contract_external_database_audit"
                    ),
                    final_pair=validated.get(
                        "final_external_database_audit"
                    ),
                )
                self._revalidate_external_database_binding(
                    expected,
                    policy={
                        **_bridge_core.EXTERNAL_DATABASE_AUDIT_POLICY,
                        "media_authority_rules_sha256": active[
                            "authority_rules"
                        ]["sha256"],
                        "audit_role_sql_sha256": active["role_sql"][
                            "sha256"
                        ],
                    },
                )
        return validated

    def _build_rollback_provenance(
        self,
        current: dict[str, Any],
        previous: dict[str, Any],
        rollback_state: dict[str, Any],
        *,
        descriptor_digest: str,
        current_state_digest: str,
        previous_state_digest: str,
        source_terminal_audit_digest: str,
    ) -> dict[str, Any]:
        compatibility = validate_migration_compatibility_state(
            rollback_state.get("migration_compatibility"),
            migrations=rollback_state.get("migrations"),
        )
        retained_0013 = bool(
            compatibility is not None
            and compatibility["code_manifest_sha256"]
            == compatibility["target_manifest_sha256"]
            and compatibility["ledger_state"]["name"] == "post-0013"
        )
        source_terminal = self._deployment_terminal_audit_binding(
            operation_id=current["operation_id"],
            descriptor_sha256=descriptor_digest,
            state_sha256=current_state_digest,
            source_sha=current["source_sha"],
            source_tree=current["source_tree"],
            expected_terminal_sha256=source_terminal_audit_digest,
            expected_state=current,
        )
        final_mutable = rollback_state.get("final_mutable_data_audit")
        final_external = rollback_state.get(
            "final_external_database_audit"
        )
        provenance = {
            "schema_version": 1,
            "kind": (
                "explicit-f-to-b-retain-0013"
                if retained_0013
                else "explicit-code-rollback"
            ),
            "rollback_operation_id": current["operation_id"],
            "from_operation_id": current["operation_id"],
            "from_source_sha": current["source_sha"],
            "from_source_tree": current["source_tree"],
            "from_descriptor_sha256": descriptor_digest,
            "from_state_sha256": require_digest(
                current_state_digest,
                "explicit rollback source state",
            ),
            "from_terminal_audit_sha256": source_terminal["sha256"],
            "to_operation_id": previous["operation_id"],
            "to_source_sha": previous["source_sha"],
            "to_source_tree": previous["source_tree"],
            "to_descriptor_sha256": previous["descriptor_sha256"],
            "sealed_previous_state_sha256": require_digest(
                previous_state_digest,
                "explicit rollback target state",
            ),
            "retained_ledger_sha256": canonical_json_digest(
                [
                    {
                        "version": record["version"],
                        "checksum": record["checksum"],
                    }
                    for record in rollback_state["migrations"]
                ]
            ),
            "final_mutable_data_audit_sha256": (
                canonical_json_digest(final_mutable)
                if final_mutable is not None
                else None
            ),
            "final_external_database_audit_sha256": (
                canonical_json_digest(final_external)
                if final_external is not None
                else None
            ),
            "created_at": utc_now(),
        }
        return provenance

    @staticmethod
    def _explicit_rollback_state(
        current: dict[str, Any],
        previous: dict[str, Any],
    ) -> dict[str, Any]:
        """Project B truthfully when the database retains exact F/0013."""

        rollback_state = json.loads(json.dumps(previous))
        previous_compatibility = previous.get("migration_compatibility")
        current_compatibility = current.get("migration_compatibility")
        if (previous_compatibility is None) != (current_compatibility is None):
            raise PullDeployError(
                "explicit rollback migration compatibility authority differs"
            )
        if previous_compatibility is not None:
            if (
                not isinstance(previous_compatibility, dict)
                or not isinstance(current_compatibility, dict)
                or any(
                    previous_compatibility.get(field)
                    != current_compatibility.get(field)
                    for field in (
                        "policy_id",
                        "target_manifest_sha256",
                        "authority_manifest_sha256",
                        "accepted_migration_ledgers",
                    )
                )
            ):
                raise PullDeployError(
                    "explicit rollback migration compatibility registry differs"
                )
        if current.get("migrations") == previous.get("migrations"):
            return rollback_state
        if (
            not isinstance(previous_compatibility, dict)
            or not isinstance(current_compatibility, dict)
            or previous_compatibility.get("code_manifest_sha256")
            != previous_compatibility.get("target_manifest_sha256")
            or previous_compatibility.get("ledger_state", {}).get("name")
            != "post-0012"
            or current_compatibility.get("code_manifest_sha256")
            != current_compatibility.get("authority_manifest_sha256")
            or current.get("migrations")
            != [
                *previous.get("migrations", []),
                _bridge_core.FINAL_MIGRATION_RECORD,
            ]
            or current_compatibility.get("ledger_state", {}).get("name")
            != "post-0013"
        ):
            raise PullDeployError(
                "explicit rollback crosses migration histories without the exact 0013 compatibility registry"
            )
        rollback_state["migrations"] = json.loads(json.dumps(current["migrations"]))
        rollback_state["migration_compatibility"] = (
            build_migration_compatibility_state(
                previous_compatibility,
                code_manifest_sha256=previous_compatibility[
                    "code_manifest_sha256"
                ],
                migrations=rollback_state["migrations"],
            )
        )
        for field in (
            "final_mutable_data_audit",
            "final_external_database_audit",
        ):
            if current.get(field) is None:
                raise PullDeployError(
                    f"explicit rollback lacks retained 0013 {field} evidence"
                )
            rollback_state[field] = json.loads(
                json.dumps(current[field])
            )
        return rollback_state

    def _recover_explicit_rollback(
        self,
        descriptor: dict[str, Any],
        descriptor_digest: str,
        marker: dict[str, Any],
    ) -> dict[str, Any]:
        previous = descriptor.get("previous_deployment")
        if not isinstance(previous, dict):
            raise PullDeployError("explicit rollback has no previous deployment")
        current = self._validate_external_database_state_provenance(
            load_private_json(self.current_state_path)
        )
        candidate_digest = require_digest(
            marker.get("rollback_current_state_sha256"),
            "explicit rollback source-state digest",
        )
        file_digest = sha256_file(self.current_state_path)
        current_is_source = (
            file_digest == candidate_digest
            and current.get("operation_id") == descriptor["operation_id"]
            and current.get("source_sha") == descriptor["repository"]["target_sha"]
            and current.get("source_tree")
            == descriptor["repository"]["target_tree"]
            and current.get("descriptor_sha256") == descriptor_digest
        )
        self._deployment_terminal_audit_binding(
            operation_id=descriptor["operation_id"],
            descriptor_sha256=descriptor_digest,
            state_sha256=candidate_digest,
            source_sha=descriptor["repository"]["target_sha"],
            source_tree=descriptor["repository"]["target_tree"],
            expected_terminal_sha256=require_digest(
                marker.get("rollback_source_terminal_audit_sha256"),
                "explicit rollback source terminal audit digest",
            ),
            expected_state=current if current_is_source else None,
        )
        sealed_rollback_state = marker.get("rollback_candidate_state")
        sealed_rollback_digest = marker.get("rollback_candidate_state_sha256")
        if (sealed_rollback_state is None) != (sealed_rollback_digest is None):
            raise PullDeployError(
                "explicit rollback marker has incomplete sealed candidate state"
            )
        phase = str(marker.get("phase", ""))
        previous_descriptor = self._previous_runtime_descriptor(descriptor)

        if sealed_rollback_state is not None:
            rollback_state = self._validate_external_database_state_provenance(
                sealed_rollback_state
            )
            rollback_state_digest = require_digest(
                sealed_rollback_digest,
                "explicit rollback candidate-state digest",
            )
            if (
                sha256_bytes(canonical_json_bytes(rollback_state) + b"\n")
                != rollback_state_digest
            ):
                raise PullDeployError(
                    "explicit rollback sealed candidate-state digest differs"
                )
            current_is_rollback = (
                file_digest == rollback_state_digest
                and current == rollback_state
            )
            if not current_is_source and not current_is_rollback:
                raise PullDeployError(
                    "explicit rollback current state is neither exact source nor sealed rollback candidate"
                )
            if any(
                marker.get(field) is not False
                for field in (
                    "source_switched",
                    "slot_switched",
                    "unit_switched",
                    "control_switched",
                    "asset_switched",
                )
            ):
                raise PullDeployError(
                    "sealed explicit rollback candidate precedes complete effect restoration"
                )
            if marker.get("runtime_stopped") is not True:
                raise PullDeployError(
                    "sealed explicit rollback candidate lacks the stopped-runtime boundary"
                )
            if phase not in {
                "explicit-rollback-state-commit-started",
                "explicit-rollback-state-committed",
                "explicit-rollback-admission-resumed",
                "explicit-rollback-recovered",
                "explicit-rollback-complete",
            }:
                raise PullDeployError(
                    "sealed explicit rollback candidate has an invalid recovery phase"
                )
            self._revalidate_candidate_database_state(
                previous_descriptor,
                rollback_state,
                include_mutable=(
                    phase == "explicit-rollback-state-commit-started"
                ),
            )
            if current_is_source:
                if phase != "explicit-rollback-state-commit-started":
                    raise PullDeployError(
                        "explicit rollback source state survived a committed phase"
                    )
                self._commit_current_state_cas(
                    rollback_state,
                    candidate_sha256=rollback_state_digest,
                    expected_pre_state=current,
                    expected_pre_state_sha256=marker[
                        "current_state_precondition_sha256"
                    ],
                )
                current_is_rollback = True
                self._advance(
                    marker,
                    "explicit-rollback-state-committed",
                )
                phase = "explicit-rollback-state-committed"
            elif phase == "explicit-rollback-state-commit-started":
                # The atomic state replacement committed but its phase update
                # was lost.  Re-fence mutable data before acknowledging that
                # commit window; admission has not yet been authorised.
                self._advance(
                    marker,
                    "explicit-rollback-state-committed",
                )
                phase = "explicit-rollback-state-committed"

            if phase == "explicit-rollback-state-committed":
                # Resume may have committed while its response was lost.  The
                # recovery primitive isolates and drains that exact runtime.
                # Do not compare the historical mutable snapshot here: writes
                # accepted after an unknown resume commit are legitimate.
                self._recover_runtime_and_resume(
                    marker,
                    previous_descriptor,
                    allow_unfenced=marker.get("verification") is None,
                    bind_mutable_after=False,
                )
                self._advance(
                    marker,
                    "explicit-rollback-admission-resumed",
                )
                phase = "explicit-rollback-admission-resumed"
            else:
                verification = self._marker_runtime_verification(marker)
                self.lifecycle.verify_open_runtime(
                    self,
                    previous_descriptor,
                    verification,
                )
            if phase != "explicit-rollback-complete":
                self._advance(marker, "explicit-rollback-complete")
            self._audit_attempt(marker, "recovered-explicit-rollback")
            self._record_operation_outcome(
                operation_id=descriptor["operation_id"],
                descriptor_sha256=descriptor_digest,
                outcome="rolled-back",
            )
            self._validate_steady_deployment_state(rollback_state)
            self.marker_path.unlink()
            fsync_directory(self.marker_path.parent)
            return rollback_state

        if phase in {
            "explicit-rollback-state-commit-started",
            "explicit-rollback-state-committed",
            "explicit-rollback-admission-resumed",
            "explicit-rollback-recovered",
            "explicit-rollback-complete",
        }:
            raise PullDeployError(
                "explicit rollback commit phase lacks a sealed candidate state"
            )
        if not current_is_source:
            raise PullDeployError(
                "unsealed explicit rollback current state is not the exact source"
            )
        rollback_state = self._explicit_rollback_state(current, previous)
        self._reconcile_effect_commit_windows(descriptor, marker)
        candidate_effects = all(
            marker.get(name) is True
            for name in (
                "source_switched",
                "slot_switched",
                "control_switched",
                "unit_switched",
                "asset_switched",
            )
        )
        if (
            marker.get("runtime_stopped") is not True
            and candidate_effects
        ):
            if phase not in {
                "explicit-rollback-started",
                "explicit-rollback-drained",
                "explicit-rollback-stop-started",
            }:
                raise PullDeployError(
                    "explicit rollback has an invalid pre-stop recovery phase"
                )
            if phase != "explicit-rollback-stop-started":
                # No stop intent was durable.  Drain may have timed out with a
                # live job, so resume the unchanged candidate in place and
                # abort this rollback without restarting any process.
                self._recover_unchanged_and_resume(
                    marker,
                    descriptor,
                    allow_unfenced=marker.get("verification") is None,
                )
                self._audit_nonterminal_attempt(
                    marker,
                    "recovered-explicit-rollback-aborted",
                )
                self.marker_path.unlink()
                fsync_directory(self.marker_path.parent)
                return current
            if not isinstance(marker.get("drain"), dict):
                raise PullDeployError(
                    "explicit rollback stop intent lacks durable drain evidence"
                )
            # A durable drain response does not prove that the same Worker is
            # still running.  Isolate and re-drain the exact live instance, or
            # prove all source readers are already stopped, before retrying.
            recovery = self._prepare_runtime_recovery(
                marker,
                descriptor,
                allow_unfenced=marker.get("verification") is None,
            )
            if recovery["runtime_state"] == "drained":
                self._stop_runtime(marker, descriptor)
            self._advance(
                marker,
                "explicit-rollback-runtime-stopped",
                runtime_stopped=True,
            )

        if (
            marker.get("runtime_stopped") is not True
            and candidate_effects
        ):
            # The branch above either aborted safely or persisted the stop.
            raise PullDeployError("explicit rollback stop recovery did not commit")
        if marker.get("runtime_stopped") is not True:
            raise PullDeployError(
                "explicit rollback effects changed before the stopped-runtime boundary"
            )

        # At least one rollback effect committed, or stop was durably marked.
        # Re-prove the stop boundary before touching any source/config effect;
        # a replacement Worker may otherwise have accepted work while Backend
        # persistent admission remained closed.
        effects_are_previous = all(
            marker.get(field) is False
            for field in (
                "source_switched",
                "slot_switched",
                "unit_switched",
                "control_switched",
                "asset_switched",
            )
        )
        recovery_descriptor = (
            previous_descriptor
            if effects_are_previous
            else descriptor
        )
        recovery = self._prepare_runtime_recovery(
            marker,
            recovery_descriptor,
            allow_unfenced=marker.get("verification") is None,
        )
        if recovery["runtime_state"] == "drained":
            self._stop_runtime(marker, recovery_descriptor)
        marker["runtime_stopped"] = True
        marker["updated_at"] = utc_now()
        self._write_marker(marker)
        mutable_before = self._bind_mutable_data_before(marker, descriptor)
        rollback_backup = marker.get("rollback_backup")
        if rollback_backup is None:
            rollback_backup = self.lifecycle.backup_rollback(
                self,
                descriptor,
                marker["rollback_backup_operation_id"],
            )
            rollback_backup["mutable_data_before_sha256"] = (
                canonical_json_digest(mutable_data_identity(mutable_before))
            )
            marker["rollback_backup"] = rollback_backup
            self._write_marker(marker)
        backup_descriptor = dict(descriptor)
        backup_descriptor["operation_id"] = marker["rollback_backup_operation_id"]
        self._validate_database_backup(
            backup_descriptor,
            rollback_backup,
            require_operation_backup=True,
        )
        if marker.get("source_switched") is True:
            self._restore_source(descriptor)
            self._record_restored_effect(marker, "source_switched")
        self._restore_previous_slot(descriptor)
        self._record_restored_effect(marker, "slot_switched")
        self._restore_previous_worker_unit(descriptor)
        self._record_restored_effect(marker, "unit_switched")
        self._restore_previous_asset_pointer(descriptor)
        self._record_restored_effect(marker, "asset_switched")
        self._restore_previous_control(descriptor)
        self._record_restored_effect(marker, "control_switched")
        if any(
            marker.get(field) is not False
            for field in (
                "source_switched",
                "slot_switched",
                "unit_switched",
                "control_switched",
                "asset_switched",
            )
        ):
            raise PullDeployError(
                "explicit rollback recovery did not restore every governed effect"
            )
        recovery = self._prepare_runtime_recovery(
            marker,
            previous_descriptor,
            allow_unfenced=marker.get("verification") is None,
        )
        if recovery["runtime_state"] == "stopped":
            self._persist_stopped_postgres_runtime_fence(
                marker, previous_descriptor
            )
            self._record_runtime_start_intent(marker, previous_descriptor)
            self.lifecycle.start(self, previous_descriptor)
        verification = self._persist_runtime_verification(
            marker,
            self.lifecycle.verify(self, previous_descriptor),
        )
        rollback_mutable = self._bind_mutable_data_after(
            marker, previous_descriptor
        )
        rollback_state["database_backup"] = rollback_backup
        rollback_state["mutable_data_audit"] = rollback_mutable
        rollback_state["deployed_at"] = utc_now()
        rollback_state["rollback_provenance"] = (
            self._build_rollback_provenance(
                current,
                previous,
                rollback_state,
                descriptor_digest=descriptor_digest,
                current_state_digest=candidate_digest,
                previous_state_digest=descriptor[
                    "previous_deployment_sha256"
                ],
                source_terminal_audit_digest=marker[
                    "rollback_source_terminal_audit_sha256"
                ],
            )
        )
        validate_current_deployment_state(rollback_state)
        rollback_state_digest = sha256_bytes(
            canonical_json_bytes(rollback_state) + b"\n"
        )
        self._advance(
            marker,
            "explicit-rollback-state-commit-started",
            rollback_candidate_state=rollback_state,
            rollback_candidate_state_sha256=rollback_state_digest,
        )
        self._revalidate_candidate_database_state(
            previous_descriptor,
            rollback_state,
            include_mutable=True,
        )
        self._commit_current_state_cas(
            rollback_state,
            candidate_sha256=rollback_state_digest,
            expected_pre_state=current,
            expected_pre_state_sha256=marker[
                "current_state_precondition_sha256"
            ],
        )
        self._advance(
            marker,
            "explicit-rollback-state-committed",
        )
        self.lifecycle.resume(self, previous_descriptor, verification)
        self._advance(marker, "explicit-rollback-admission-resumed")
        self._advance(marker, "explicit-rollback-complete")
        self._audit_attempt(marker, "recovered-explicit-rollback")
        self._record_operation_outcome(
            operation_id=descriptor["operation_id"],
            descriptor_sha256=descriptor_digest,
            outcome="rolled-back",
        )
        self._validate_steady_deployment_state(rollback_state)
        self.marker_path.unlink()
        fsync_directory(self.marker_path.parent)
        return rollback_state

    def recover_interrupted(self) -> dict[str, Any] | None:
        if not self.marker_path.exists():
            return None
        marker = load_private_json(self.marker_path)
        operation_id = require_operation_id(str(marker.get("operation_id", "")))
        descriptor, descriptor_digest = self._load_prepared(
            operation_id,
            allow_deployment_database_recovery=True,
        )
        validate_recovery_marker(
            marker,
            descriptor=descriptor,
            descriptor_digest=descriptor_digest,
        )
        if marker["action"] == "explicit-rollback":
            return self._recover_explicit_rollback(
                descriptor, descriptor_digest, marker
            )
        if self._is_first_bridge(descriptor):
            restored_takeover = (
                self._probe_restored_legacy_takeover(descriptor)
            )
            if restored_takeover is not None:
                # This must precede repository/effect reconciliation: exact
                # restore intentionally brought back HTTPS origin, ignored
                # legacy content and the old open runtime.
                self._complete_failed_first_bridge_recovery(
                    descriptor,
                    descriptor_digest,
                    marker,
                    audit_status="recovered-takeover-restore",
                )
                return None
            if marker.get("takeover_restore_started") is not None:
                self._rollback_failed_attempt(descriptor, marker)
                self._complete_failed_first_bridge_recovery(
                    descriptor,
                    descriptor_digest,
                    marker,
                    audit_status="recovered-partial-takeover-restore",
                )
                return None
        if self._is_pre_stop_abort_marker(descriptor, marker):
            # This path must precede effect reconciliation.  Candidate and
            # previous releases may share unit or asset bytes, and inspecting
            # those equal bytes cannot prove that a switch happened.
            self._rollback_failed_attempt(descriptor, marker)
            self._audit_attempt(marker, "recovered-pre-stop-abort")
            if not self._bridge_precommit_is_retryable(
                descriptor, descriptor_digest
            ):
                self._record_operation_outcome(
                    operation_id=operation_id,
                    descriptor_sha256=descriptor_digest,
                    outcome="failed",
                )
            self.marker_path.unlink()
            fsync_directory(self.marker_path.parent)
            return None
        self._reconcile_effect_commit_windows(descriptor, marker)
        current = self._candidate_current_state(descriptor, descriptor_digest, marker)
        if current is not None:
            # State commit is the deploy commit point.  Never roll the runtime
            # back while durable state names the candidate.  Reconstruct the
            # candidate idempotently, then reopen admission only after full
            # verification.
            effective_phase = (
                marker.get("failed_phase")
                if marker.get("phase") == "failed"
                else marker.get("phase")
            )
            if effective_phase == "state-commit-started":
                self._advance(marker, "state-committed")
                effective_phase = "state-committed"
            if effective_phase == "state-committed":
                self._recover_runtime_and_resume(
                    marker,
                    descriptor,
                    allow_unfenced=marker.get("verification") is None,
                    bind_mutable_after=False,
                )
                self._advance(marker, "admission-resumed")
            elif effective_phase == "admission-resumed":
                self.lifecycle.verify_open_runtime(
                    self,
                    descriptor,
                    self._marker_runtime_verification(marker),
                )
            else:
                raise PullDeployError(
                    "committed deployment candidate has an invalid recovery phase"
                )
            self._audit_attempt(marker, "recovered-success")
            self._record_operation_outcome(
                operation_id=operation_id,
                descriptor_sha256=descriptor_digest,
                outcome="deployed",
            )
            self._validate_steady_deployment_state(current)
            self.marker_path.unlink()
            fsync_directory(self.marker_path.parent)
            return current
        previous = descriptor.get("previous_deployment")
        restored = all(
            marker.get(field) is False
            for field in (
                "source_switched",
                "slot_switched",
                "unit_switched",
                "control_switched",
                "asset_switched",
            )
        )
        if isinstance(previous, dict) and restored:
            previous_runtime = self._previous_runtime_descriptor(descriptor)
            self._validate_steady_deployment_state(previous)
            self._recover_runtime_and_resume(
                marker,
                previous_runtime,
                allow_unfenced=marker.get("verification") is None,
            )
            self._audit_attempt(marker, "recovered-failed-open-admission")
            self._record_operation_outcome(
                operation_id=operation_id,
                descriptor_sha256=descriptor_digest,
                outcome="failed",
            )
            self.marker_path.unlink()
            fsync_directory(self.marker_path.parent)
            return None
        self._rollback_failed_attempt(descriptor, marker)
        if self._is_first_bridge(descriptor):
            self._complete_failed_first_bridge_recovery(
                descriptor,
                descriptor_digest,
                marker,
                audit_status="recovered-takeover-restore",
            )
            return None
        self._audit_attempt(marker, "recovered-rollback")
        if not self._bridge_precommit_is_retryable(
            descriptor, descriptor_digest
        ):
            self._record_operation_outcome(
                operation_id=operation_id,
                descriptor_sha256=descriptor_digest,
                outcome="failed",
            )
        self.marker_path.unlink()
        fsync_directory(self.marker_path.parent)
        return None

    def apply(
        self,
        *,
        target_sha: str,
        operation_id: str,
        bridge_authority_sha: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_roots(mutating=True)
        target_sha = require_sha(target_sha, "target SHA")
        operation_id = require_operation_id(operation_id)
        if not self.apply_enabled:
            return {
                "action": "apply",
                "apply": False,
                "operation_id": operation_id,
                "target_sha": target_sha,
            }
        with self.deployment_lock():
            self._require_no_contract_maintenance()
            descriptor, descriptor_digest = self._load_prepared(
                operation_id,
                target_sha,
                allow_deployment_database_recovery=(
                    self.marker_path.exists()
                    or self.marker_path.is_symlink()
                ),
            )
            if descriptor["schema_version"] == BRIDGE_DESCRIPTOR_SCHEMA_VERSION:
                if (
                    bridge_authority_sha is None
                    or require_sha(
                        bridge_authority_sha, "bridge authority SHA"
                    )
                    != descriptor["bridge"]["authority"]["sha"]
                ):
                    raise PullDeployError(
                        "bridge descriptor requires its exact authority command"
                    )
            elif bridge_authority_sha is not None:
                raise PullDeployError(
                    "ordinary deployment cannot use bridge authority"
                )
            recovered = self.recover_interrupted()
            if recovered is not None and recovered.get("source_sha") == target_sha:
                return recovered
            self._assert_operation_not_terminal(operation_id, action="apply")
            descriptor, descriptor_digest = self._load_prepared(
                operation_id, target_sha
            )
            self._revalidate_pre_switch(descriptor)
            marker = {
                "schema_version": 2,
                "action": "deploy",
                "operation_id": operation_id,
                "source_sha": target_sha,
                "descriptor_sha256": descriptor_digest,
                "executor_control": descriptor["controller"]["executor_control"],
                "executor_control_sha256": descriptor["controller"][
                    "executor_control_sha256"
                ],
                "current_state_precondition_sha256": descriptor.get(
                    "previous_deployment_sha256"
                ),
                "phase": "prepared",
                "started_at": utc_now(),
                "updated_at": utc_now(),
                "runtime_stopped": False,
                "source_switched": False,
                "slot_switched": False,
                "control_switched": False,
                "unit_switched": False,
                "asset_switched": False,
                "database_change_started": False,
            }
            self._write_marker(marker)
            try:
                first_bridge = bool(
                    descriptor["schema_version"]
                    == BRIDGE_DESCRIPTOR_SCHEMA_VERSION
                    and descriptor.get("previous_deployment") is None
                )
                if first_bridge:
                    self._adopt_legacy_takeover_stop(marker, descriptor)
                else:
                    self._advance(marker, "drain-started")
                    drain = self.lifecycle.drain(self, descriptor)
                    self._advance(marker, "drained", drain=drain)
                    stop_recovery = self._prepare_runtime_recovery(
                        marker,
                        descriptor,
                        allow_unfenced=True,
                    )
                    self._advance(
                        marker,
                        "runtime-stop-started",
                        drain=stop_recovery,
                    )
                    if stop_recovery["runtime_state"] == "drained":
                        self._stop_runtime(marker, descriptor)
                    self._advance(
                        marker,
                        "runtime-stopped",
                        runtime_stopped=True,
                    )
                mutable_before = self._bind_mutable_data_before(
                    marker, descriptor
                )
                self._advance(marker, "backup-started")
                backup = self.lifecycle.backup(self, descriptor)
                backup["mutable_data_before_sha256"] = canonical_json_digest(
                    mutable_data_identity(mutable_before)
                )
                backup = self._validate_database_backup(
                    descriptor,
                    backup,
                    require_operation_backup=True,
                )
                self._advance(marker, "backup-verified", database_backup=backup)
                self._revalidate_pre_switch(descriptor)
                self._advance(marker, "asset-switch-started")
                self._switch_asset_pointer(descriptor)
                self._advance(marker, "asset-switched", asset_switched=True)
                self._advance(marker, "source-switch-started")
                self._switch_source(descriptor)
                self._advance(marker, "source-switched", source_switched=True)
                self._advance(marker, "worker-unit-install-started")
                self._install_candidate_worker_unit(descriptor)
                self._advance(marker, "worker-unit-installed", unit_switched=True)
                self._advance(
                    marker, "migrations-started", database_change_started=True
                )
                migration = self.lifecycle.migrate(self, descriptor)
                if (
                    not isinstance(migration, dict)
                    or set(migration) != {"newly_applied", "ledger"}
                    or not isinstance(migration["newly_applied"], list)
                    or not isinstance(migration["ledger"], list)
                ):
                    raise PullDeployError(
                        "migration lifecycle returned invalid evidence"
                    )
                bridge_external_reference = None
                final_external_pair = None
                if (
                    descriptor["schema_version"]
                    == BRIDGE_DESCRIPTOR_SCHEMA_VERSION
                ):
                    (
                        _bridge_external_pair,
                        bridge_external_reference,
                        _transition_chain,
                    ) = self._capture_bridge_external_database_transition(
                        descriptor
                    )
                elif "0013_monomer_dft_jobs" in migration["newly_applied"]:
                    final_external_pair = (
                        self._capture_final_external_database_transition(
                            descriptor,
                            descriptor_digest,
                        )
                    )
                self._advance(
                    marker,
                    "migrations-complete",
                    applied_migrations=migration["newly_applied"],
                    migration_history=migration["ledger"],
                    **(
                        {
                            "bridge_external_database_audit": (
                                bridge_external_reference
                            )
                        }
                        if bridge_external_reference is not None
                        else {}
                    ),
                    **(
                        {
                            "final_external_database_audit": (
                                final_external_pair
                            )
                        }
                        if final_external_pair is not None
                        else {}
                    ),
                )
                self._advance(marker, "slot-switch-started")
                active_slot = self._activate_slot(descriptor)
                self._advance(
                    marker, "slot-switched", slot_switched=True, active_slot=active_slot
                )
                self._advance(marker, "control-switch-started")
                active_control = self._activate_control(descriptor)
                self._advance(
                    marker,
                    "control-switched",
                    control_switched=True,
                    active_control=active_control,
                )
                self._advance(marker, "runtime-start-started")
                self._record_runtime_start_intent(marker, descriptor)
                self.lifecycle.start(self, descriptor)
                self._advance(marker, "runtime-started")
                self._advance(marker, "verifying")
                verification = self._persist_runtime_verification(
                    marker,
                    self.lifecycle.verify(self, descriptor),
                    phase="verified",
                )
                self._bind_mutable_data_after(marker, descriptor)
                state = self._current_state(descriptor, descriptor_digest, marker)
                state_digest = sha256_bytes(canonical_json_bytes(state) + b"\n")
                self._advance(
                    marker,
                    "state-commit-started",
                    candidate_state=state,
                    candidate_state_sha256=state_digest,
                )
                if (
                    descriptor["schema_version"]
                    == BRIDGE_DESCRIPTOR_SCHEMA_VERSION
                ):
                    self._revalidate_bridge_candidate_database_state(
                        descriptor,
                        state,
                        include_mutable=True,
                    )
                    self._begin_bridge_state_commit(
                        descriptor,
                        descriptor_digest,
                        state_digest,
                    )
                else:
                    self._revalidate_candidate_database_state(
                        descriptor,
                        state,
                        include_mutable=True,
                    )
                self._commit_current_state_cas(
                    state,
                    candidate_sha256=state_digest,
                    expected_pre_state=descriptor.get(
                        "previous_deployment"
                    ),
                    expected_pre_state_sha256=marker[
                        "current_state_precondition_sha256"
                    ],
                )
                if (
                    descriptor["schema_version"]
                    == BRIDGE_DESCRIPTOR_SCHEMA_VERSION
                ):
                    self._consume_bridge_token(
                        descriptor,
                        descriptor_digest,
                        state_digest,
                    )
                self._advance(marker, "state-committed")
                self.lifecycle.resume(self, descriptor, verification)
                self._advance(marker, "admission-resumed")
                self._audit_attempt(marker, "success")
                self._record_operation_outcome(
                    operation_id=operation_id,
                    descriptor_sha256=descriptor_digest,
                    outcome="deployed",
                )
                self._validate_steady_deployment_state(state)
                self.marker_path.unlink()
                fsync_directory(self.marker_path.parent)
                return state
            except BaseException as exc:
                failed_phase = marker.get("phase")
                marker["error"] = str(exc)[:500]
                marker["failed_at"] = utc_now()
                marker["failed_phase"] = failed_phase
                self._advance(marker, "failed")
                try:
                    committed = self._candidate_current_state(
                        descriptor, descriptor_digest, marker
                    )
                except BaseException as state_exc:
                    marker["forward_recovery_error"] = str(state_exc)[:500]
                    self._write_marker(marker)
                    self._audit_attempt(marker, "failed-forward-recovery")
                    raise PullDeployError(
                        "deployment state commit is ambiguous; ingress remains isolated"
                    ) from state_exc
                if committed is not None:
                    try:
                        effective_phase = marker.get("failed_phase")
                        if effective_phase == "state-commit-started":
                            self._advance(marker, "state-committed")
                            effective_phase = "state-committed"
                        if effective_phase == "state-committed":
                            self._recover_runtime_and_resume(
                                marker,
                                descriptor,
                                allow_unfenced=(
                                    marker.get("verification") is None
                                ),
                                bind_mutable_after=False,
                            )
                            self._advance(marker, "admission-resumed")
                        elif effective_phase == "admission-resumed":
                            self.lifecycle.verify_open_runtime(
                                self,
                                descriptor,
                                self._marker_runtime_verification(marker),
                            )
                        else:
                            raise PullDeployError(
                                "committed candidate has an invalid failed phase"
                            )
                    except BaseException as forward_exc:
                        marker["forward_recovery_error"] = str(forward_exc)[:500]
                        self._write_marker(marker)
                        self._audit_attempt(marker, "failed-forward-recovery")
                        raise PullDeployError(
                            "deployment committed but candidate admission recovery failed"
                        ) from forward_exc
                    self._audit_attempt(marker, "recovered-success")
                    self._record_operation_outcome(
                        operation_id=operation_id,
                        descriptor_sha256=descriptor_digest,
                        outcome="deployed",
                    )
                    self._validate_steady_deployment_state(committed)
                    self.marker_path.unlink()
                    fsync_directory(self.marker_path.parent)
                    return committed
                try:
                    self._rollback_failed_attempt(descriptor, marker)
                except BaseException as rollback_exc:
                    marker["rollback"] = "failed"
                    marker["rollback_error"] = str(rollback_exc)[:500]
                    self._write_marker(marker)
                    self._audit_attempt(marker, "failed-rollback")
                    raise PullDeployError(
                        "deployment and rollback failed; admission remains isolated"
                    ) from rollback_exc
                marker["rollback"] = "success"
                if self._is_first_bridge(descriptor):
                    self._complete_failed_first_bridge_recovery(
                        descriptor,
                        descriptor_digest,
                        marker,
                        audit_status="failed",
                    )
                    raise
                self._audit_attempt(marker, "failed")
                if not self._bridge_precommit_is_retryable(
                    descriptor, descriptor_digest
                ):
                    self._record_operation_outcome(
                        operation_id=operation_id,
                        descriptor_sha256=descriptor_digest,
                        outcome="failed",
                    )
                self.marker_path.unlink()
                fsync_directory(self.marker_path.parent)
                raise

    def apply_bridge(
        self, *, authority_sha: str, operation_id: str
    ) -> dict[str, Any]:
        """Apply only the B target already sealed by F in a v3 descriptor."""

        self.ensure_roots(mutating=True)
        authority_sha = require_sha(authority_sha, "bridge authority SHA")
        operation_id = require_operation_id(operation_id)
        descriptor, _digest = self._load_prepared(
            operation_id,
            allow_deployment_database_recovery=(
                self.marker_path.exists()
                or self.marker_path.is_symlink()
            ),
        )
        if (
            descriptor["schema_version"] != BRIDGE_DESCRIPTOR_SCHEMA_VERSION
            or descriptor["bridge"]["authority"]["sha"] != authority_sha
        ):
            raise PullDeployError(
                "prepared operation is not owned by this bridge authority"
            )
        return self.apply(
            target_sha=descriptor["repository"]["target_sha"],
            operation_id=operation_id,
            bridge_authority_sha=authority_sha,
        )

    def recover_restored_first_bridge(
        self,
        *,
        authority_sha: str,
        target_sha: str,
        operation_id: str,
        capsule_sha256: str,
        descriptor_sha256: str,
        restored_terminal_sha256: str,
    ) -> dict[str, Any]:
        """Finalize only an exact restored first bridge without trusting live Git."""

        # Exact legacy restore intentionally reinstates the old HTTPS origin,
        # ignored files and checkout permissions.  Validate only the sealed B
        # control/runtime roots here; every ordinary command still calls
        # ensure_roots() and therefore rejects that checkout.
        self._ensure_control_runtime_roots(mutating=True)
        authority_sha = require_sha(authority_sha, "bridge authority SHA")
        target_sha = require_sha(target_sha, "bridge target SHA")
        operation_id = require_operation_id(operation_id)
        capsule_sha256 = require_digest(
            capsule_sha256, "bridge recovery capsule digest"
        )
        descriptor_sha256 = require_digest(
            descriptor_sha256, "bridge descriptor digest"
        )
        restored_terminal_sha256 = require_digest(
            restored_terminal_sha256,
            "legacy takeover restored terminal digest",
        )
        if not self.apply_enabled:
            return {
                "action": "bridge-recover-restored",
                "apply": False,
                "operation_id": operation_id,
                "capsule_sha256": capsule_sha256,
                "authority_sha": authority_sha,
                "target_sha": target_sha,
                "descriptor_sha256": descriptor_sha256,
                "restored_terminal_sha256": restored_terminal_sha256,
            }
        with self.deployment_lock():
            self._require_no_contract_maintenance()
            if not (
                self.marker_path.exists()
                or self.marker_path.is_symlink()
            ):
                raise PullDeployError(
                    "restored bridge recovery marker is unavailable"
                )
            marker = load_private_json(self.marker_path)
            capsule, descriptor = self._load_bridge_recovery_capsule(
                capsule_sha256
            )
            observed_descriptor_sha256 = capsule["descriptor_sha256"]
            if (
                observed_descriptor_sha256 != descriptor_sha256
                or capsule["operation_id"] != operation_id
                or not self._is_first_bridge(descriptor)
                or descriptor["bridge"]["authority"]["sha"] != authority_sha
                or descriptor["repository"]["target_sha"] != target_sha
            ):
                raise PullDeployError(
                    "restored bridge recovery content authority differs"
                )
            validate_recovery_marker(
                marker,
                descriptor=descriptor,
                descriptor_digest=descriptor_sha256,
            )
            restored = self._probe_restored_legacy_takeover(descriptor)
            if (
                restored is None
                or restored.get("restored_terminal_sha256")
                != restored_terminal_sha256
                or marker.get("bridge_recovery_capsule")
                != self._bridge_recovery_capsule_binding(capsule)
                or self.current_state_path.exists()
                or self.current_state_path.is_symlink()
            ):
                raise PullDeployError(
                    "restored bridge recovery terminal identity differs"
                )
            self._complete_failed_first_bridge_recovery(
                descriptor,
                descriptor_sha256,
                marker,
                audit_status="recovered-takeover-restore",
            )
            return {
                "action": "bridge-recover-restored",
                "apply": True,
                "operation_id": operation_id,
                "authority_sha": authority_sha,
                "target_sha": target_sha,
                "descriptor_sha256": descriptor_sha256,
                "capsule_sha256": capsule_sha256,
                "restored_terminal_sha256": restored_terminal_sha256,
                "token_status": "retired-precommit",
            }

    def rollback(self, *, operation_id: str) -> dict[str, Any]:
        self.ensure_roots(mutating=True)
        operation_id = require_operation_id(operation_id)
        if not self.apply_enabled:
            return {"action": "rollback", "apply": False, "operation_id": operation_id}
        with self.deployment_lock():
            self._require_no_contract_maintenance()
            if self.marker_path.exists() or self.marker_path.is_symlink():
                interrupted = load_private_json(self.marker_path)
                if (
                    interrupted.get("action") != "explicit-rollback"
                    or interrupted.get("operation_id") != operation_id
                ):
                    raise PullDeployError(
                        "another interrupted deployment must be recovered before explicit rollback"
                    )
                recovered = self.recover_interrupted()
                if recovered is None:
                    raise PullDeployError(
                        "explicit rollback recovery produced no governed state"
                    )
                return recovered
            descriptor, descriptor_digest = self._load_prepared(operation_id)
            operation_state = self._load_operation_state(operation_id)
            if (
                operation_state is None
                or operation_state.get("outcome") != "deployed"
                or operation_state.get("descriptor_sha256") != descriptor_digest
            ):
                raise PullDeployError(
                    "explicit rollback requires the terminal deployed operation"
                )
            current_state_digest = sha256_file(self.current_state_path)
            current = self._validate_steady_deployment_state(
                load_private_json(self.current_state_path)
            )
            if (
                sha256_bytes(canonical_json_bytes(current) + b"\n")
                != current_state_digest
            ):
                raise PullDeployError(
                    "explicit rollback current state is not canonically sealed"
                )
            if (
                current.get("operation_id") != operation_id
                or current.get("source_sha") != descriptor["repository"]["target_sha"]
                or current.get("descriptor_sha256") != descriptor_digest
            ):
                raise PullDeployError(
                    "explicit rollback target is not the current deployment"
                )
            previous = descriptor.get("previous_deployment")
            if not isinstance(previous, dict):
                raise PullDeployError(
                    "bootstrap rollback requires its dedicated maintenance hook"
                )
            rollback_state = self._explicit_rollback_state(current, previous)
            source_terminal = self._deployment_terminal_audit_binding(
                operation_id=operation_id,
                descriptor_sha256=descriptor_digest,
                state_sha256=current_state_digest,
                source_sha=current["source_sha"],
                source_tree=current["source_tree"],
                expected_state=current,
            )
            # Validate the complete old runtime projection before drain.  In
            # particular, a stable-control CAS upgrade deliberately closes
            # rollback to a state bound to older executable helpers.
            self._previous_runtime_descriptor(descriptor)
            marker = {
                "schema_version": 2,
                "action": "explicit-rollback",
                "operation_id": operation_id,
                "source_sha": current["source_sha"],
                "descriptor_sha256": descriptor_digest,
                "executor_control": descriptor["controller"]["executor_control"],
                "executor_control_sha256": descriptor["controller"][
                    "executor_control_sha256"
                ],
                "current_state_precondition_sha256": current_state_digest,
                "rollback_current_state_sha256": current_state_digest,
                "rollback_source_terminal_audit_sha256": source_terminal[
                    "sha256"
                ],
                "rollback_attempt_id": (
                    "rollback-attempt-" + secrets.token_hex(20)
                ),
                "rollback_backup_operation_id": (
                    "rollback-"
                    + canonical_json_digest(
                        {
                            "operation_id": operation_id,
                            "current_state_sha256": current_state_digest,
                        }
                    ).removeprefix("sha256:")[:40]
                ),
                "phase": "explicit-rollback-started",
                "started_at": utc_now(),
                "updated_at": utc_now(),
                "runtime_stopped": False,
                "source_switched": True,
                "slot_switched": True,
                "control_switched": True,
                "unit_switched": True,
                "asset_switched": True,
                "database_change_started": False,
            }
            self._write_marker(marker)
            drain = self.lifecycle.drain(self, descriptor)
            self._advance(marker, "explicit-rollback-drained", drain=drain)
            stop_recovery = self._prepare_runtime_recovery(
                marker,
                descriptor,
                allow_unfenced=True,
            )
            self._advance(
                marker,
                "explicit-rollback-stop-started",
                drain=stop_recovery,
            )
            if stop_recovery["runtime_state"] == "drained":
                self._stop_runtime(marker, descriptor)
            self._advance(
                marker, "explicit-rollback-runtime-stopped", runtime_stopped=True
            )
            mutable_before = self._bind_mutable_data_before(marker, descriptor)
            # A fresh backup protects writes made after the original deploy.
            # It is evidence only: explicit rollback never restores the old
            # pre-deploy dump and therefore never discards post-deploy writes.
            marker["rollback_backup"] = self.lifecycle.backup_rollback(
                self,
                descriptor,
                marker["rollback_backup_operation_id"],
            )
            marker["rollback_backup"][
                "mutable_data_before_sha256"
            ] = canonical_json_digest(mutable_data_identity(mutable_before))
            self._write_marker(marker)
            self._restore_source(descriptor)
            self._advance(
                marker, "explicit-rollback-source-restored", source_switched=False
            )
            self._restore_previous_slot(descriptor)
            self._advance(
                marker, "explicit-rollback-slot-restored", slot_switched=False
            )
            self._restore_previous_worker_unit(descriptor)
            self._advance(
                marker, "explicit-rollback-unit-restored", unit_switched=False
            )
            self._restore_previous_asset_pointer(descriptor)
            self._advance(
                marker, "explicit-rollback-asset-restored", asset_switched=False
            )
            self._restore_previous_control(descriptor)
            self._advance(
                marker,
                "explicit-rollback-control-restored",
                control_switched=False,
            )
            previous_descriptor = self._previous_runtime_descriptor(descriptor)
            self._record_runtime_start_intent(marker, previous_descriptor)
            self.lifecycle.start(self, previous_descriptor)
            verification = self._persist_runtime_verification(
                marker, self.lifecycle.verify(self, previous_descriptor)
            )
            rollback_mutable = self._bind_mutable_data_after(
                marker, previous_descriptor
            )
            rollback_state["database_backup"] = marker["rollback_backup"]
            rollback_state["mutable_data_audit"] = rollback_mutable
            rollback_state["deployed_at"] = utc_now()
            rollback_state["rollback_provenance"] = (
                self._build_rollback_provenance(
                    current,
                    previous,
                    rollback_state,
                    descriptor_digest=descriptor_digest,
                    current_state_digest=marker[
                        "rollback_current_state_sha256"
                    ],
                    previous_state_digest=descriptor[
                        "previous_deployment_sha256"
                    ],
                    source_terminal_audit_digest=marker[
                        "rollback_source_terminal_audit_sha256"
                    ],
                )
            )
            validate_current_deployment_state(rollback_state)
            rollback_state_digest = sha256_bytes(
                canonical_json_bytes(rollback_state) + b"\n"
            )
            self._advance(
                marker,
                "explicit-rollback-state-commit-started",
                rollback_candidate_state=rollback_state,
                rollback_candidate_state_sha256=rollback_state_digest,
            )
            self._revalidate_candidate_database_state(
                previous_descriptor,
                rollback_state,
                include_mutable=True,
            )
            self._commit_current_state_cas(
                rollback_state,
                candidate_sha256=rollback_state_digest,
                expected_pre_state=current,
                expected_pre_state_sha256=marker[
                    "current_state_precondition_sha256"
                ],
            )
            self._advance(
                marker,
                "explicit-rollback-state-committed",
            )
            self.lifecycle.resume(self, previous_descriptor, verification)
            self._advance(
                marker,
                "explicit-rollback-admission-resumed",
            )
            self._advance(marker, "explicit-rollback-complete")
            self._audit_attempt(marker, "explicit-rollback")
            self._record_operation_outcome(
                operation_id=operation_id,
                descriptor_sha256=descriptor_digest,
                outcome="rolled-back",
            )
            self._validate_steady_deployment_state(rollback_state)
            self.marker_path.unlink()
            fsync_directory(self.marker_path.parent)
            return rollback_state


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("plan", "prepare", "apply"):
        command = commands.add_parser(name)
        command.add_argument("--sha", required=True)
        command.add_argument("--operation-id", required=True)
    for name in ("bridge-plan", "bridge-prepare"):
        command = commands.add_parser(name)
        command.add_argument("--authority-sha", required=True)
        command.add_argument("--operation-id", required=True)
        command.add_argument("--prefetch-operation-id", required=True)
    bridge_apply = commands.add_parser("bridge-apply")
    bridge_apply.add_argument("--authority-sha", required=True)
    bridge_apply.add_argument("--operation-id", required=True)
    bridge_recover = commands.add_parser("bridge-recover-restored")
    bridge_recover.add_argument("--authority-sha", required=True)
    bridge_recover.add_argument("--target-sha", required=True)
    bridge_recover.add_argument("--operation-id", required=True)
    bridge_recover.add_argument("--capsule-sha256", required=True)
    bridge_recover.add_argument("--descriptor-sha256", required=True)
    bridge_recover.add_argument(
        "--restored-terminal-sha256", required=True
    )
    prepare_abort = commands.add_parser("prepare-abort")
    prepare_abort.add_argument("--operation-id", required=True)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--operation-id", required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = parser().parse_args(argv)
    controller = PullDeployController(
        PRODUCTION_ROOT,
        RUNTIME_ROOT,
        apply=args.command not in {"plan", "bridge-plan"},
    )
    try:
        if args.command == "plan":
            document = controller.plan(
                target_sha=args.sha, operation_id=args.operation_id
            )
        elif args.command == "bridge-plan":
            document = controller.bridge_plan(
                authority_sha=args.authority_sha,
                operation_id=args.operation_id,
                prefetch_operation_id=args.prefetch_operation_id,
            )
        elif args.command == "bridge-prepare":
            document = controller.prepare(
                target_sha=None,
                operation_id=args.operation_id,
                bridge_authority_sha=args.authority_sha,
                prefetch_operation_id=args.prefetch_operation_id,
            )
        elif args.command == "bridge-apply":
            document = controller.apply_bridge(
                authority_sha=args.authority_sha,
                operation_id=args.operation_id,
            )
        elif args.command == "bridge-recover-restored":
            document = controller.recover_restored_first_bridge(
                authority_sha=args.authority_sha,
                target_sha=args.target_sha,
                operation_id=args.operation_id,
                capsule_sha256=args.capsule_sha256,
                descriptor_sha256=args.descriptor_sha256,
                restored_terminal_sha256=args.restored_terminal_sha256,
            )
        elif args.command == "prepare":
            document = controller.prepare(
                target_sha=args.sha, operation_id=args.operation_id
            )
        elif args.command == "apply":
            document = controller.apply(
                target_sha=args.sha, operation_id=args.operation_id
            )
        elif args.command == "prepare-abort":
            document = controller.abort_prepare(
                operation_id=args.operation_id
            )
        else:
            document = controller.rollback(operation_id=args.operation_id)
    except (PullDeployError, OSError, subprocess.SubprocessError) as exc:
        print(f"pull-deploy: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

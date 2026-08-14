#!/usr/bin/env python3
"""Validate the immutable, production-safe monomer DFT release boundary."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


MIGRATION_VERSION = "0013_monomer_dft_jobs"
MIGRATION_CHECKSUM = (
    "ab633a6253887dad45103c288d54a0d02d4d69ce1f9a14c1271338d448f9acbc"
)
CATALOG_FINGERPRINT = (
    "6dc2e6ca7e1bb052836afec2bbdd46c6aa0928e97efdbbc6669b9b220f9bf6f8"
)
CHECKOUT_SHA = "de0fac2e4500dabe0009e67214ff5f5447ce83dd"
EXPECTED_RUNNER = "ubuntu-24.04"
EXPECTED_AIMNET_COMMIT = "9a6c56440349bccbb7ac0630a0622f9c584f894e"
EXPECTED_AIMNET_TREE = "fd28c0f8bf2d0e513aad24032228927140d6783c"
PRODUCTION_REPO_ROOT = Path("/data/lzq/gith/nexpoly")

REQUIRED_PATHS = (
    ".env.monomer-dft.dev.example",
    ".gitignore",
    ".github/workflows/ci.yml",
    "backend/app/main.py",
    "backend/app/postgres_preflight.py",
    "backend/app/routers/monomer_dft.py",
    "backend/app/services/deployment_control.py",
    "backend/app/services/monomer_dft_download_proxy.py",
    "backend/app/services/monomer_dft_repository.py",
    "backend/app/services/monomer_dft_schema.py",
    "backend/app/services/monomer_dft_worker_client.py",
    "backend/migrations/postgres/0013_monomer_dft_jobs.sql",
    "backend/migrations/postgres/manifest.json",
    "contracts/monomer_dft_api_contract_v1.json",
    "docker-compose.monomer-dft-dev.yml",
    "docker-compose.prod.yml",
    "gpu_resource/authority.py",
    "gpu_resource/client.py",
    "ops/gpu_broker/server.py",
    "ops/systemd/nexpoly-gpu2-guard.service",
    "ops/systemd/nexpoly-gpu2-guard.timer",
    "ops/systemd/nexpoly-monomer-dft-worker.service",
    "scripts/ci/validate_workflows.py",
    "scripts/gpu_mps_control.sh",
    "scripts/gpu2_guard.py",
    "scripts/monomer_dft_gpu_acceptance.py",
    "scripts/monomer_dft_acceptance_env.py",
    "scripts/monomer_dft_runtime_contract.py",
    "scripts/monomer_dft_dev_stack.sh",
    "scripts/monomer_dft_worker_ctl.sh",
    "scripts/preflight_monomer_dft_prod.py",
    "scripts/preflight_monomer_dft_env.py",
    "scripts/run_monomer_dft_gpu_acceptance.py",
    "scripts/release_controller.py",
    "scripts/setup_monomer_dft_env.sh",
    "scripts/setup_monomer_dft_prod_runtime.sh",
    "scripts/smoke_monomer_dft_env.py",
    "scripts/validate_monomer_dft_release_contract.py",
    "workers/monomer_dft_worker/aimnet-source.lock.json",
    "workers/monomer_dft_worker/app/config.py",
    "workers/monomer_dft_worker/app/gpu_broker_client.py",
    "workers/monomer_dft_worker/build-requirements.lock",
    "workers/monomer_dft_worker/run_host_worker.sh",
)


def _read_text(root: Path, relative: str, failures: list[str]) -> str:
    path = root / relative
    try:
        if path.is_symlink() or not path.is_file():
            failures.append(f"{relative} must be a regular non-symlink")
            return ""
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        failures.append(f"cannot read {relative}: {exc}")
        return ""


def _load_json(root: Path, relative: str, failures: list[str]) -> dict[str, Any]:
    text = _read_text(root, relative, failures)
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        failures.append(f"{relative} is invalid JSON: {exc}")
        return {}
    if not isinstance(payload, dict):
        failures.append(f"{relative} must contain a JSON object")
        return {}
    return payload


def _canonical_checksum(path: Path) -> str:
    normalized = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(normalized).hexdigest()


def validate_migration_and_api_contract(root: Path, failures: list[str]) -> None:
    manifest = _load_json(
        root, "backend/migrations/postgres/manifest.json", failures
    )
    entries = manifest.get("migrations")
    if manifest.get("schema_version") != 2 or not isinstance(entries, list):
        failures.append("migration manifest must use schema version 2")
        entries = []
    matching = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("version") == MIGRATION_VERSION
    ]
    if len(matching) != 1:
        failures.append(f"migration manifest must contain exactly one {MIGRATION_VERSION}")
    else:
        entry = matching[0]
        if (
            entry.get("kind") != "expand"
            or entry.get("epoch") != 2
            or entry.get("checksum") != MIGRATION_CHECKSUM
        ):
            failures.append(
                f"{MIGRATION_VERSION} must be the fixed epoch-2 expand migration"
            )
        requirements = entry.get("requires_contracts")
        if (
            not isinstance(requirements, list)
            or len(requirements) != 1
            or not isinstance(requirements[0], dict)
            or requirements[0].get("version") != "0012_drop_polytao_jobs"
        ):
            failures.append(f"{MIGRATION_VERSION} must require exact migration 0012")

    migration_path = (
        root / "backend/migrations/postgres/0013_monomer_dft_jobs.sql"
    )
    try:
        actual_checksum = _canonical_checksum(migration_path)
    except OSError as exc:
        failures.append(f"cannot hash {migration_path.relative_to(root)}: {exc}")
    else:
        if actual_checksum != MIGRATION_CHECKSUM:
            failures.append(
                f"{MIGRATION_VERSION} checksum changed: {actual_checksum}"
            )

    contract = _load_json(root, "contracts/monomer_dft_api_contract_v1.json", failures)
    expected_gate = {
        "migration_version": MIGRATION_VERSION,
        "migration_checksum_sha256": MIGRATION_CHECKSUM,
        "readiness_field": "schema_ready",
        "safe_without_schema": ["/status", "/capabilities"],
        "guarded_resource_prefixes": ["/jobs"],
        "not_ready_error": {
            "http_status": 503,
            "code": "schema_not_ready",
            "retry_after_seconds": 5,
        },
    }
    if contract.get("database_schema_gate") != expected_gate:
        failures.append("public API contract does not pin the exact 0013 schema gate")
    stable_codes = contract.get("stable_error_codes")
    required_stable_codes = {
        "schema_not_ready",
        "worker_socket_not_configured",
        "worker_unavailable",
    }
    if (
        not isinstance(stable_codes, list)
        or not required_stable_codes.issubset(stable_codes)
    ):
        failures.append(
            "public API contract must publish stable schema and Worker errors"
        )


def _parse_env(text: str, failures: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            failures.append(f"development environment line {line_number} is invalid")
            continue
        key, value = line.split("=", 1)
        if not key or key in values:
            failures.append(
                f"development environment line {line_number} has an empty or duplicate key"
            )
            continue
        values[key] = value
    return values


def validate_development_gpu_contract(text: str, failures: list[str]) -> None:
    values = _parse_env(text, failures)
    expected = {
        "MONOMER_DFT_DEPLOYMENT": "dev",
        "NEXPOLY_DFT_GPU_DEVICE": "1",
        "NEXPOLY_DFT_OVERFLOW_GPU_DEVICES": "3",
        "MONOMER_DFT_GPU_BROKER_UDS": ".runtime/gpu-resource/broker.sock",
        "MONOMER_DFT_GPU_MPS_PIPE_ROOT": ".runtime/gpu-resource",
        "MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS": (
            ".runtime/gpu-resource/external-reservations.json"
        ),
        "MONOMER_DFT_DOWNLOAD_SPOOL_ROOT": (
            "/app/.runtime/monomer-dft-download-spool"
        ),
        "AIMNET_SOURCE_CLONE": ".runtime/aimnet-source-clone",
        "AIMNET_SOURCE_DIR": ".runtime/aimnet-source-archive",
    }
    for key, expected_value in expected.items():
        if values.get(key) != expected_value:
            failures.append(f"{key} must equal {expected_value!r}")
    production_root = str(PRODUCTION_REPO_ROOT)
    if any(
        value == production_root or value.startswith(f"{production_root}/")
        for value in values.values()
    ):
        failures.append("development DFT configuration references production state")
    selected_devices = {
        values.get("NEXPOLY_DFT_GPU_DEVICE", ""),
        *filter(
            None,
            values.get("NEXPOLY_DFT_OVERFLOW_GPU_DEVICES", "").split(","),
        ),
    }
    forbidden_devices = selected_devices & {"0", "2"}
    if forbidden_devices:
        failures.append(
            "development DFT configuration must never select physical GPU "
            + ", ".join(sorted(forbidden_devices))
        )


def validate_development_compose(text: str, failures: list[str]) -> None:
    for marker in (
        "MONOMER_DFT_DOWNLOAD_SPOOL_ROOT: /app/.runtime/monomer-dft-download-spool",
        "source: ./.runtime/monomer-dft-download-spool",
        "target: /app/.runtime/monomer-dft-download-spool",
        "source: ./.runtime/monomer-dft-worker-socket",
        "create_host_path: false",
        'SOURCE_REVISION: "${NEXPOLY_DFT_AUTHORITY_SHA:-unknown}"',
        "${NEXPOLY_DFT_BACKEND_IMAGE_REF:-nexpoly-dft-dev-backend:latest}",
        "${NEXPOLY_DFT_WEB_IMAGE_REF:-nexpoly-dft-dev-frontend:latest}",
    ):
        if marker not in text:
            failures.append(
                f"development Compose is missing its worktree runtime fence: {marker}"
            )
    for marker in (
        '"127.0.0.1:${NEXPOLY_DFT_POSTGRES_PORT:-25532}:5432"',
        '"127.0.0.1:${NEXPOLY_DFT_BACKEND_PORT:-28000}:8000"',
        '"127.0.0.1:${NEXPOLY_DFT_FRONTEND_PORT:-25173}:80"',
    ):
        if marker not in text:
            failures.append(f"development Compose is missing loopback binding: {marker}")
    if re.search(r"(?m)^name:\s*", text):
        failures.append(
            "development Compose must receive its validated project name from the controller"
        )
    if "/data/lzq/gith/nexpoly/" in text:
        failures.append("development Compose references production runtime state")
    for forbidden in (
        "/proc/",
        "NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY",
        "NEXPOLY_DFT_GPU_AUTHORITY_PID",
    ):
        if forbidden in text:
            failures.append(
                "descriptor authority must remain on the host Worker boundary: "
                + forbidden
            )


def validate_development_delivery(root: Path, failures: list[str]) -> None:
    paths = (
        "gpu_resource/authority.py",
        "gpu_resource/client.py",
        "ops/gpu_broker/server.py",
        "scripts/gpu_mps_control.sh",
        "scripts/monomer_dft_dev_stack.sh",
        "scripts/monomer_dft_acceptance_env.py",
        "scripts/monomer_dft_worker_ctl.sh",
        "scripts/preflight_monomer_dft_env.py",
        "scripts/run_monomer_dft_gpu_acceptance.py",
        "scripts/setup_monomer_dft_env.sh",
        "scripts/smoke_monomer_dft_env.py",
        "workers/monomer_dft_worker/app/config.py",
        "workers/monomer_dft_worker/app/gpu_broker_client.py",
        "workers/monomer_dft_worker/run_host_worker.sh",
    )
    texts = {relative: _read_text(root, relative, failures) for relative in paths}
    combined = "\n".join(texts.values())
    for forbidden in (
        "remote_release.sh",
        "systemctl ",
        "authorized_keys",
        "\nssh ",
        "\nscp ",
        "/data/lzq/nexpoly-assets/",
        "/data/lzq/gith/nexpoly/ops/",
    ):
        if forbidden in combined:
            failures.append(
                f"development DFT delivery contains a production execution path: {forbidden}"
            )

    worker = texts["scripts/monomer_dft_worker_ctl.sh"]
    for marker in (
        "MONOMER_DFT_DEPLOYMENT must be exactly dev",
        "NEXPOLY_DFT_GPU_DEVICE=1",
        "NEXPOLY_DFT_OVERFLOW_GPU_DEVICES=3",
        'GPU_RUNTIME_ROOT="$RUNTIME_ROOT/gpu-resource"',
        'HOME="$PRIVATE_HOME"',
        'TMPDIR="$PRIVATE_TMPDIR"',
        'FORMAL_ACCEPTANCE="${NEXPOLY_DFT_FORMAL_ACCEPTANCE:-0}"',
        "load_formal_env",
        "reject_formal_control_environment",
        "compgen -e",
        "coproc FORMAL_ENV_COPROC",
        'wait "$parser_pid"',
        "FORMAL_ENV_KEY_COUNT=46",
        "FORMAL_ENV_KEYSET_SHA256=",
        "configure_formal_gpu_authority",
        '--expected-root "$GPU_RUNTIME_ROOT"',
        "NEXPOLY_DFT_GPU1_MPS_PIPE_AUTHORITY",
    ):
        if marker not in worker:
            failures.append(f"development Worker fence is missing: {marker}")
    for forbidden in ('== "prod"', ':-2}', "GPU_DEVICE=2"):
        if forbidden in worker:
            failures.append(
                f"development Worker retains a production/GPU2 branch: {forbidden}"
            )

    stack = texts["scripts/monomer_dft_dev_stack.sh"]
    for marker in (
        "NEXPOLY_DFT_ACCEPTANCE_PROJECT_NAME",
        "^nexpoly_dft_fresh_",
        "fresh acceptance requires an exact NEXPOLY_DFT_AUTHORITY_SHA",
        "fresh acceptance must use the fixed local Docker socket",
        "fresh acceptance environment contains forbidden",
        "coproc FORMAL_ENV_COPROC",
        'wait "$parser_pid"',
        "FORMAL_ENV_KEY_COUNT=46",
        "FORMAL_ENV_KEYSET_SHA256=",
        "COMPOSE_ENV_FILE=/dev/null",
        '--env-file "$COMPOSE_ENV_FILE"',
        "NEXPOLY_DFT_FORMAL_ACCEPTANCE=1",
        "compgen -e",
        "final-main immutable image pull failed",
        "image_arguments=(--no-build)",
        "configure_formal_gpu_authority",
        '--expected-root "$REPO_ROOT/.runtime/gpu-resource"',
    ):
        if marker not in stack:
            failures.append(
                f"fresh acceptance Compose fence is missing: {marker}"
            )
    for forbidden in ("FORMAL_ENV_TEMP", "FORMAL_COMPOSE_ENV_TEMP"):
        if forbidden in stack or forbidden in worker:
            failures.append(
                "formal acceptance must not materialize parsed secrets in "
                f"a temporary file: {forbidden}"
            )

    formal_env = texts["scripts/monomer_dft_acceptance_env.py"]
    for marker in (
        "ALLOWED_KEYS = frozenset(",
        "EXPANSION_MARKERS",
        "def parse_dotenv(",
        "os.open(path, flags)",
        "os.O_NOFOLLOW",
        "os.fstat(descriptor)",
        "MAX_ENV_BYTES = 64 * 1024",
        "_file_snapshot(after) != expected",
        "stat.S_IMODE(metadata.st_mode) != 0o600",
        "duplicate dotenv key is forbidden",
        "formal acceptance dotenv is incomplete",
        "def encode_nul_pairs(",
    ):
        if marker not in formal_env:
            failures.append(
                f"formal acceptance dotenv data parser is missing: {marker}"
            )

    preflight = texts["scripts/preflight_monomer_dft_env.py"]
    for marker in (
        "def effective_environment(",
        "load_formal_gpu_authority(",
        'expected_root=repo_root / ".runtime/gpu-resource"',
        "formal_gpu_authority.root",
        "formal_gpu_authority.reservations",
    ):
        if marker not in preflight:
            failures.append(
                f"formal preflight descriptor authority is missing: {marker}"
            )
    for forbidden in (
        "PRODUCTION_BROKER_SOCKET",
        "PRODUCTION_MPS_PIPE_ROOT",
        '"2": "GPU-',
    ):
        if forbidden in preflight:
            failures.append(
                f"development preflight retains a production/GPU2 runtime: {forbidden}"
            )

    setup = texts["scripts/setup_monomer_dft_env.sh"]
    for marker in (
        'DEFAULT_AIMNET_CLONE="$RUNTIME_ROOT/aimnet-source-clone"',
        'rev-parse HEAD',
        ')" == "$AIMNET_COMMIT"',
        "GIT_NO_REPLACE_OBJECTS=1 GIT_NO_LAZY_FETCH=1",
        'git -C "$AIMNET_CLONE" archive',
    ):
        if marker not in setup:
            failures.append(f"AIMNet clean source fence is missing: {marker}")
    if "/data/lzq/gith/aimnetcentral" in setup:
        failures.append("AIMNet setup must not use the adjacent dirty source clone")

    acceptance = texts["scripts/run_monomer_dft_gpu_acceptance.py"]
    for marker in (
        "def snapshot_gpu2(",
        "class Gpu2AuditMonitor:",
        "class FreshAcceptanceControl:",
        "def run_leased_direct(",
        "def run_backend_e2e(",
        "def _validate_scientific_result(",
        "def _validate_journal(",
        "def _safe_command_environment(",
        "def _production_repo_snapshot(",
        "def _production_worktree_inventory(",
        "def _production_git_authority_inventory(",
        "PRODUCTION_BASELINE_SHA",
        "PRODUCTION_BASELINE_TREE",
        "PRODUCTION_BASELINE_ORIGIN",
        "PRODUCTION_BASELINE_RAW_GIT_AUTHORITY",
        "PRODUCTION_BASELINE_SNAPSHOT",
        '"--ignored=matching"',
        '"GIT_OPTIONAL_LOCKS": "0"',
        '"GIT_CONFIG_NOSYSTEM": "1"',
        '"GIT_CONFIG_GLOBAL": "/dev/null"',
        '"core.fsmonitor=false"',
        '"core.hooksPath=/dev/null"',
        'b"info"',
        '"for-each-ref"',
        "production tracked worktree status differs from the fixed baseline",
        "PRODUCTION_CAS_MAX_TOTAL_BYTES",
        '"ignored_path_count"',
        '"ignored_content_bytes"',
        '"inventory_sha256"',
        '"git_authority_sha256"',
        '"git_config_sha256"',
        '"git_refs_sha256"',
        "def _validate_authority_images_input(",
        "def _validate_published_platform_manifest(",
        "def validate_bridge_authority(",
        "def _broker_rejection_status_projection(",
        "def _finalize_gpu3_rejection(",
        "before_status_sha256",
        "after_status_sha256",
        "claim_sha256",
        "def _candidate_image_tags(",
        "def _docker_image_tag_snapshot(",
        "ORDINARY_DEV_IMAGE_TAGS",
        "candidate image tag survived cleanup",
        "ordinary development image tags changed during acceptance",
        "def _open_absolute_directory_chain(",
        "def _require_private_gpu_root(",
        "acceptance GPU runtime root is not owner-private",
        "def _formal_gpu_authority_environment(",
        'f"/proc/{self.authority_process_id}/fd/"',
        "NEXPOLY_DFT_GPU_AUTHORITY_START_TICKS",
        "NEXPOLY_DFT_GPU1_MPS_PIPE_AUTHORITY",
        "def _verify_model_descriptor(",
        "def _prepare_stable_model_copy(",
        "def _run_calculations_from_stable_model_copy(",
        "os.O_NOFOLLOW",
        "os.pread(",
        'f"/proc/self/fd/{descriptor}"',
        "pass_fds=(",
        "model_copy.model_descriptor,",
        "mps_pipe_fd,",
        "broker_root_fd,",
        "smoke_runtime.prepare_runtime(REPO_ROOT)",
        "def _prepare_formal_smoke_runtime(",
        'result["formal_gpu_authority"] is True',
        "smoke_runtime.run_calculations(",
        "transient_scope_command(",
        "wait_for_scope_membership(",
        "NEXPOLY_GPU_EXEC_GATE_FD",
        "direct acceptance is permitted only for GPU3 overflow",
        "BACKEND_BASE_URL = \"http://127.0.0.1:28000/api/v1/monomer-dft\"",
        "urllib.request.ProxyHandler({})",
        "zipfile.ZipFile(",
        "np.load(",
        '"gpu_capacity_unavailable"',
        '"externally_fenced"',
        '"energy", "forces", "hessian"',
        '"broker_science"',
        '"candidate-tree", "final-main"',
        '"published_exact"',
        "SAFE_COMMAND_PATH",
        '"buildx",',
        '"imagetools",',
        '"parent_lease_id"',
        "_bind_gpu3_claim_cas(",
        "controller.cleanup()",
        "completed_journal_sha256",
        "cancelled_journal_sha256",
        "hessian_artifact_sha256",
        "bundle_manifest_sha256",
        "bundle_sha256",
        "acceptance_contract.validate_gpu3_direct_result(report)",
        "acceptance_contract.validate_gpu3_actual_lease(lease_evidence)",
        "acceptance_contract.canonical_json_file_digest(report)",
        '"result": gpu3_direct',
        '"lease": gpu3_lease',
        "canonical_json_digest(science)",
    ):
        if marker not in acceptance:
            failures.append(f"real GPU acceptance control is missing: {marker}")
    for forbidden in (
        'gpu_index="1"',
        '"direct_science"',
        'choices=("manage", "existing")',
    ):
        if forbidden in acceptance:
            failures.append(
                f"real GPU acceptance retains an unsafe legacy path: {forbidden}"
            )

    authority = texts["gpu_resource/authority.py"]
    for marker in (
        "def load_formal_gpu_authority(",
        "def materialize_formal_gpu_authority(",
        "expected_root: Path | None = None",
        "GPU root authority differs from the exact development root",
        "/data/lzq/gith/nexpoly-runtime",
        'root / "external-reservations.json"',
        'root / f"mps-{index}" / "pipe"',
        "_process_start_ticks(process_id) != process_start_ticks",
        "process-local GPU descriptor authority environment changed",
    ):
        if marker not in authority:
            failures.append(
                f"formal GPU descriptor authority is missing: {marker}"
            )

    mps_control = texts["scripts/gpu_mps_control.sh"]
    for marker in (
        'expected_development_gpu_root="$REPO_ROOT/.runtime/gpu-resource"',
        "formal development descriptor authority forbids production GPU2",
        "formal development descriptor authority forbids the production repository",
        "NEXPOLY_GPU_MPS_AUTHORITY_START_TICKS",
        "MPS descriptor authority hierarchy changed",
        "MPS reservation descriptor authority escaped its root",
    ):
        if marker not in mps_control:
            failures.append(
                f"MPS descriptor authority fence is missing: {marker}"
            )

    host_runner = texts[
        "workers/monomer_dft_worker/run_host_worker.sh"
    ]
    for marker in (
        "NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY",
        '--expected-root "$RUNTIME_ROOT/gpu-resource"',
        "GPU descriptor authority validation failed",
    ):
        if marker not in host_runner:
            failures.append(
                f"host Worker descriptor authority is missing: {marker}"
            )

    worker_config = texts["workers/monomer_dft_worker/app/config.py"]
    for marker in (
        "load_formal_gpu_authority(",
        "materialize_formal_gpu_authority(",
        "mps_pipe_directories:",
        "differs from formal GPU descriptor authority",
    ):
        if marker not in worker_config:
            failures.append(
                f"Worker Python descriptor authority is missing: {marker}"
            )

    gpu_client = texts["gpu_resource/client.py"]
    broker_adapter = texts[
        "workers/monomer_dft_worker/app/gpu_broker_client.py"
    ]
    for marker in (
        "pipe_directories:",
        "descriptor-bound MPS pipe authority",
        r"/proc/[1-9][0-9]*/fd/[0-9]+",
    ):
        if marker not in gpu_client:
            failures.append(
                f"GPU client descriptor authority is missing: {marker}"
            )
    for marker in (
        "mps_pipe_directories:",
        'mps_arguments["pipe_directories"]',
        "expected_pipe_directory",
    ):
        if marker not in broker_adapter:
            failures.append(
                f"Worker Broker adapter authority is missing: {marker}"
            )

    broker_server = texts["ops/gpu_broker/server.py"]
    for marker in (
        "def process_stable_descriptor_path(",
        "def _open_external_reservations(",
        'prefix = "/proc/self/fd/"',
        "_LOCAL_INHERITED_FD_RE.fullmatch(raw)",
        "return os.dup(descriptor)",
        "raw_payload = os.pread(",
        "args.socket = process_stable_descriptor_path(args.socket)",
        "args.external_reservations = process_stable_descriptor_path(",
        "args.mps_state_root = process_stable_descriptor_path(",
    ):
        if marker not in broker_server:
            failures.append(
                f"GPU Broker process-stable descriptor authority is missing: {marker}"
            )

    acceptance_contract_path = root / "scripts/monomer_dft_gpu_acceptance.py"
    acceptance_contract_text = _read_text(
        root,
        "scripts/monomer_dft_gpu_acceptance.py",
        failures,
    )
    for marker in (
        "def validate_gpu3_direct_result(",
        "def validate_gpu3_actual_lease(",
        "def canonical_json_file_digest(",
        "PRODUCTION_BASELINE_SHA",
        "PRODUCTION_BASELINE_TREE",
        "PRODUCTION_BASELINE_ORIGIN_SHA256",
        "PRODUCTION_BASELINE_HEAD_REF_SHA256",
        'gpus["1"]["evidence_sha256"] != canonical_json_digest(science)',
        '"GPU3 external-fence digest differs from its evidence"',
        '"GPU3 actual science is not bound to its Broker lease"',
        '"candidate_image_tags"',
        '"candidate_images_absent_before"',
        '"ordinary_dev_images_unchanged"',
        '"model_copy_sha256"',
        '"model_copy_path_sha256"',
        '"model_copy_removed"',
        '"before_status_sha256"',
        '"after_status_sha256"',
        '"claim_sha256"',
    ):
        if marker not in acceptance_contract_text:
            failures.append(
                f"GPU acceptance evidence binding is missing: {marker}"
            )
    reservations_path = root / "ops/config/gpu-external-reservations.json"
    try:
        spec = importlib.util.spec_from_file_location(
            "nexpoly_release_gpu_acceptance_contract",
            acceptance_contract_path,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("module specification is unavailable")
        acceptance_contract_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(acceptance_contract_module)
        reservations_sha256 = (
            "sha256:" + hashlib.sha256(reservations_path.read_bytes()).hexdigest()
        )
    except (OSError, RuntimeError, AttributeError) as exc:
        failures.append(f"cannot load fixed GPU acceptance contract: {exc}")
    else:
        if (
            acceptance_contract_module.EXTERNAL_RESERVATIONS_SHA256
            != reservations_sha256
        ):
            failures.append(
                "GPU3 external-fence contract differs from the governed reservations"
            )

    download_proxy = _read_text(
        root,
        "backend/app/services/monomer_dft_download_proxy.py",
        failures,
    )
    for marker in (
        'f"process-{self._process_identity[0]}-{self._process_identity[1]}"',
        "def _ensure_private_directory(",
        "os.chmod(path, 0o700)",
        "def _remove_empty_process_spool(",
    ):
        if marker not in download_proxy:
            failures.append(
                f"download spool is missing its process-private 0700 lease fence: {marker}"
            )


def validate_production_activation(root: Path, failures: list[str]) -> None:
    compose = _read_text(root, "docker-compose.prod.yml", failures)
    for marker in (
        'MONOMER_DFT_SUBMIT_ENABLED: "true"',
        'MONOMER_DFT_WORKER_UDS: "/app/monomer-dft-worker/worker.sock"',
        "state/monomer-dft-worker-socket",
        "state/monomer-dft-download-spool",
        "create_host_path: false",
    ):
        if marker not in compose:
            failures.append(f"production Compose is missing DFT activation marker: {marker}")

    main_text = _read_text(root, "backend/app/main.py", failures)
    for marker in (
        "app.state.monomer_dft_runtime_enabled = bool(",
        "if app_settings.monomer_dft_worker_uds",
        "app.state.monomer_dft_reconciler = None",
        "if app.state.monomer_dft_runtime_enabled:",
    ):
        if marker not in main_text:
            failures.append(f"backend hard-off construction guard is missing: {marker}")

    worker_client = _read_text(
        root, "backend/app/services/monomer_dft_worker_client.py", failures
    )
    if "if client is None and uds_path:" not in worker_client:
        failures.append("DFT Worker client must create transports only for an explicit UDS")
    if "httpx.AsyncClient()" in worker_client:
        failures.append("DFT Worker client contains an implicit network fallback")

    worker_unit = _read_text(
        root, "ops/systemd/nexpoly-monomer-dft-worker.service", failures
    )
    for marker in (
        "MONOMER_DFT_DEPLOYMENT=prod",
        "NEXPOLY_DFT_GPU_DEVICE=2",
        "NEXPOLY_DFT_GPU_GUARD_MODE=observe",
        "NEXPOLY_DFT_OVERFLOW_GPU_DEVICES=",
        "MONOMER_DFT_GPU_BROKER_ENABLED=0",
        "Restart=always",
        "scripts/gpu2_guard.py",
        (
            "/data/lzq/gith/nexpoly-runtime/bin/"
            "control_runtime_selector.py run monomer-dft"
        ),
        "preflight_monomer_dft_prod.py",
    ):
        if marker not in worker_unit:
            failures.append(f"production DFT unit is missing policy marker: {marker}")
    if "gpu2_guard.py --require-ready" in worker_unit:
        failures.append("production DFT unit must observe rather than enforce GPU2 guard")
    if "ExecStart=/data/lzq/gith/nexpoly/workers/monomer_dft_worker/run_host_worker.sh" in worker_unit:
        failures.append("production DFT unit must not execute the live checkout launcher")

    guard = _read_text(root, "scripts/gpu2_guard.py", failures)
    for marker in (
        'GPU_INDEX = "2"',
        'GPU_UUID = "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe"',
        '"status": "ready" if not unknown else "quarantined"',
    ):
        if marker not in guard:
            failures.append(f"GPU2 guard is missing policy marker: {marker}")


def validate_database_schema_state_contract(
    root: Path,
    failures: list[str],
) -> None:
    schema_probe = _read_text(
        root,
        "backend/app/services/monomer_dft_schema.py",
        failures,
    )
    for marker in (
        CATALOG_FINGERPRINT,
        'ABSENT = "absent"',
        'READY = "ready"',
        'INVALID = "invalid"',
        "owner_is_current_role",
        "owner_matches_schema",
        "access_control",
        "relation_options",
        "tablespace",
        "security_labels",
    ):
        if marker not in schema_probe:
            failures.append(
                f"exact PG16 monomer DFT schema probe is missing: {marker}"
            )

    deployment_control = _read_text(
        root,
        "backend/app/services/deployment_control.py",
        failures,
    )
    for marker in (
        '"pending",\n    "queued",\n    "running",\n    "cancel_requested",',
        "probe_monomer_dft_schema(connection)",
        "active_jobs_schema_version=2",
        "active_jobs_schema_version=1",
    ):
        if marker not in deployment_control:
            failures.append(
                f"versioned deployment job snapshot contract is missing: {marker}"
            )

    preflight = _read_text(root, "backend/app/postgres_preflight.py", failures)
    for marker in (
        'SCHEMA_TARGET_STARTUP = "startup-through-0012"',
        'SCHEMA_TARGET_FINAL = "final-0013"',
        "STARTUP_REQUIRED_MIGRATIONS",
        "probe_monomer_dft_schema(connection)",
    ):
        if marker not in preflight:
            failures.append(
                f"Postgres startup/final schema profile is missing: {marker}"
            )

    main_text = _read_text(root, "backend/app/main.py", failures)
    if "schema_target=SCHEMA_TARGET_STARTUP" not in main_text:
        failures.append(
            "backend startup must use the through-0012 compatibility profile"
        )

    release_controller = _read_text(
        root,
        "scripts/release_controller.py",
        failures,
    )
    for marker in (
        "PERSISTENT_ACTIVE_JOB_CATEGORIES_V1",
        "PERSISTENT_ACTIVE_JOB_CATEGORIES_V2",
        "validated_persistent_active_total(",
    ):
        if marker not in release_controller:
            failures.append(
                f"bootstrap persistent snapshot validation is missing: {marker}"
            )


def validate_aimnet_build_contract(root: Path, failures: list[str]) -> None:
    lock = _load_json(
        root, "workers/monomer_dft_worker/aimnet-source.lock.json", failures
    )
    source = lock.get("source")
    if not isinstance(source, dict):
        failures.append("AIMNet source lock is missing source metadata")
        return
    expected = {
        "commit": EXPECTED_AIMNET_COMMIT,
        "tree": EXPECTED_AIMNET_TREE,
        "archive_inventory_sha256": (
            "abf724d01f2dabab12ee29381d53e4646f0b4a04c8f435c03f21b3d3ab19936d"
        ),
        "package_name": "aimnet",
        "wheel_install_mode": "non-editable",
        "python_minor": "3.12",
        "uv_version": "0.11.21",
        "source_date_epoch": 1782945961,
    }
    for key, expected_value in expected.items():
        if source.get(key) != expected_value:
            failures.append(f"AIMNet source lock {key} must equal {expected_value!r}")
    expected_wheel = {
        "filename": "aimnet-0.2.0.post1.dev41+g9a6c56440-py3-none-any.whl",
        "sha256": "9cb53c47230f3746872a34948480b1228f98258026d88b338111cf90f8d28557",
        "file_count": 47,
        "inventory_sha256": (
            "54ad7842d215f0430c9d376c6c8d550925f2ede9b880e8969dadd72b5b2471ce"
        ),
        "record_path": (
            "aimnet-0.2.0.post1.dev41+g9a6c56440.dist-info/RECORD"
        ),
        "record_sha256": (
            "54b23e6ff673423e19865c702ab174910f39259a8fcdbfd670e19303d6909d61"
        ),
    }
    if lock.get("wheel") != expected_wheel:
        failures.append("AIMNet wheel filename, inventory, RECORD, or digest changed")
    build_lock = root / "workers/monomer_dft_worker/build-requirements.lock"
    try:
        build_lock_checksum = _canonical_checksum(build_lock)
    except OSError as exc:
        failures.append(f"cannot hash AIMNet build lock: {exc}")
    else:
        if source.get("build_requirements_sha256") != build_lock_checksum:
            failures.append("AIMNet build-requirements checksum does not match source lock")

    runtime_contract_path = root / "scripts/monomer_dft_runtime_contract.py"
    try:
        spec = importlib.util.spec_from_file_location(
            "nexpoly_release_runtime_contract",
            runtime_contract_path,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("module specification is unavailable")
        runtime_contract_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime_contract_module)
        runtime_contract = runtime_contract_module.RUNTIME_CONTRACT
    except (OSError, RuntimeError, AttributeError) as exc:
        failures.append(f"cannot load fixed native runtime contract: {exc}")
    else:
        models = lock.get("models")
        expected_runtime_contract = {
            "schema_version": 1,
            "python_minor": source.get("python_minor"),
            "uv_version": source.get("uv_version"),
            "build_lock_sha256": (
                f"sha256:{source.get('build_requirements_sha256')}"
            ),
            "source": {
                key: (
                    f"sha256:{source.get(key)}"
                    if key == "archive_inventory_sha256"
                    else source.get(key)
                )
                for key in (
                    "repository_url",
                    "commit",
                    "tree",
                    "archive_inventory_sha256",
                    "package_name",
                    "package_version",
                    "source_date_epoch",
                )
            },
            "wheel": {
                key: (
                    f"sha256:{expected_wheel[key]}"
                    if key in {"sha256", "inventory_sha256", "record_sha256"}
                    else expected_wheel[key]
                )
                for key in expected_wheel
            },
            "registry_sha256": f"sha256:{lock.get('registry', {}).get('sha256')}",
            "models_sha256": (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        models,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    ).encode("utf-8")
                ).hexdigest()
            ),
        }
        if runtime_contract != expected_runtime_contract:
            failures.append(
                "production native runtime contract differs from the AIMNet source lock"
            )

    setup = _read_text(root, "scripts/setup_monomer_dft_env.sh", failures)
    for marker in (
        'git -C "$AIMNET_CLONE" archive',
        '"${AIMNET_COMMIT}^{tree}"',
        'rev-parse HEAD',
        'DEFAULT_AIMNET_CLONE="$RUNTIME_ROOT/aimnet-source-clone"',
        "aimnet-wheel-manifest.json",
        "SETUPTOOLS_SCM_PRETEND_VERSION",
        'SOURCE_DATE_EPOCH="$AIMNET_SOURCE_DATE_EPOCH"',
    ):
        if marker not in setup:
            failures.append(f"AIMNet clean-archive build control is missing: {marker}")


def validate_ci_contract(root: Path, failures: list[str]) -> None:
    ci_text = _read_text(root, ".github/workflows/ci.yml", failures)
    runners = re.findall(r"^\s*runs-on:\s*([^\s#]+)", ci_text, flags=re.MULTILINE)
    if not runners or any(runner != EXPECTED_RUNNER for runner in runners):
        failures.append(f"every canonical CI job must run on {EXPECTED_RUNNER}")
    checkout_refs = re.findall(
        r"^\s*uses:\s*actions/checkout@([0-9a-f]{40})",
        ci_text,
        flags=re.MULTILINE,
    )
    if not checkout_refs or any(ref != CHECKOUT_SHA for ref in checkout_refs):
        failures.append("every checkout must use actions/checkout v6.0.2 exact SHA")
    for marker in (
        "backend-tests:",
        "dft-worker-tests:",
        "python -m pytest workers/monomer_dft_worker/tests",
        "python3 scripts/validate_monomer_dft_release_contract.py --require-committed",
    ):
        if marker not in ci_text:
            failures.append(f"canonical CI is missing DFT release control: {marker}")
    if (root / ".github/workflows/monomer-dft-ci.yml").exists():
        failures.append("temporary monomer DFT workflow must not exist")


def validate_committed_inputs(root: Path, failures: list[str]) -> None:
    for relative in REQUIRED_PATHS:
        tracked = subprocess.run(
            ("git", "-C", str(root), "ls-files", "--error-unmatch", "--", relative),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if tracked.returncode != 0:
            failures.append(f"release-contract input is not tracked: {relative}")
            continue
        clean = subprocess.run(
            ("git", "-C", str(root), "diff", "--quiet", "HEAD", "--", relative),
            check=False,
        )
        if clean.returncode != 0:
            failures.append(f"release-contract input is not committed: {relative}")


def validate(root: Path, *, require_committed: bool = False) -> list[str]:
    failures: list[str] = []
    lexical = Path(os.path.abspath(os.path.normpath(root)))
    if lexical == PRODUCTION_REPO_ROOT:
        return [
            "development DFT release validation is forbidden in the production repository"
        ]
    resolved = lexical.resolve()
    for relative in REQUIRED_PATHS:
        _read_text(resolved, relative, failures)
    validate_migration_and_api_contract(resolved, failures)
    validate_development_gpu_contract(
        _read_text(resolved, ".env.monomer-dft.dev.example", failures),
        failures,
    )
    validate_development_compose(
        _read_text(resolved, "docker-compose.monomer-dft-dev.yml", failures),
        failures,
    )
    validate_development_delivery(resolved, failures)
    validate_production_activation(resolved, failures)
    validate_database_schema_state_contract(resolved, failures)
    validate_aimnet_build_contract(resolved, failures)
    validate_ci_contract(resolved, failures)
    if require_committed:
        validate_committed_inputs(resolved, failures)
    return failures


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to the script's repository)",
    )
    parser.add_argument(
        "--require-committed",
        action="store_true",
        help="also require every governed input to be tracked and equal to HEAD",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    failures = validate(args.root, require_committed=args.require_committed)
    if failures:
        for failure in failures:
            print(f"monomer DFT release contract: {failure}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "migration_version": MIGRATION_VERSION,
                "migration_checksum": MIGRATION_CHECKSUM,
                "catalog_fingerprint": CATALOG_FINGERPRINT,
                "checkout_sha": CHECKOUT_SHA,
                "runner": EXPECTED_RUNNER,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

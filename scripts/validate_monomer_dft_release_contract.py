#!/usr/bin/env python3
"""Validate the immutable, production-safe monomer DFT release boundary."""

from __future__ import annotations

import argparse
import hashlib
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
    "scripts/ci/validate_workflows.py",
    "scripts/monomer_dft_dev_stack.sh",
    "scripts/monomer_dft_worker_ctl.sh",
    "scripts/preflight_monomer_dft_env.py",
    "scripts/release_controller.py",
    "scripts/setup_monomer_dft_env.sh",
    "scripts/smoke_monomer_dft_env.py",
    "scripts/validate_monomer_dft_release_contract.py",
    "workers/monomer_dft_worker/aimnet-source.lock.json",
    "workers/monomer_dft_worker/app/config.py",
    "workers/monomer_dft_worker/build-requirements.lock",
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


def validate_development_delivery(root: Path, failures: list[str]) -> None:
    paths = (
        "scripts/monomer_dft_dev_stack.sh",
        "scripts/monomer_dft_worker_ctl.sh",
        "scripts/preflight_monomer_dft_env.py",
        "scripts/setup_monomer_dft_env.sh",
        "scripts/smoke_monomer_dft_env.py",
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
    ):
        if marker not in worker:
            failures.append(f"development Worker fence is missing: {marker}")
    for forbidden in ('== "prod"', ':-2}', "GPU_DEVICE=2"):
        if forbidden in worker:
            failures.append(
                f"development Worker retains a production/GPU2 branch: {forbidden}"
            )

    preflight = texts["scripts/preflight_monomer_dft_env.py"]
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


def validate_production_hard_off(root: Path, failures: list[str]) -> None:
    compose = _read_text(root, "docker-compose.prod.yml", failures)
    for marker in (
        'MONOMER_DFT_SUBMIT_ENABLED: "false"',
        'MONOMER_DFT_WORKER_UDS: ""',
    ):
        if marker not in compose:
            failures.append(f"production Compose is missing hard-off marker: {marker}")
    for forbidden in (
        "MONOMER_DFT_WORKER_BASE_URL:",
        "monomer-dft-worker-socket",
        "/app/monomer-dft-worker",
    ):
        if forbidden in compose:
            failures.append(
                f"production Compose must not configure DFT transport or mounts: {forbidden}"
            )

    main_text = _read_text(root, "backend/app/main.py", failures)
    for marker in (
        "app.state.monomer_dft_runtime_enabled = bool(",
        "app.state.monomer_dft_worker_client = None",
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
    validate_production_hard_off(resolved, failures)
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

#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

set -a
# shellcheck disable=SC1091
source .env.dev
set +a

# This workflow has one immutable physical-device contract.  Reject an
# inherited or dotenv override before computing state paths or invoking
# Docker/the controller.
[[ "${NEXPOLY_DEV_GPU_DEVICE:-1}" == "1" ]] || {
  echo "NEXPOLY_DEV_GPU_DEVICE must be exactly physical GPU 1." >&2
  exit 2
}
export NEXPOLY_DEV_GPU_DEVICE=1

: "${NEXPOLY_ASSET_ROOT:?Set NEXPOLY_ASSET_ROOT to a pinned immutable asset release}"
CURRENT_SOURCE_REVISION="$(git rev-parse --verify HEAD)"
CURRENT_SOURCE_TREE="$(git rev-parse --verify 'HEAD^{tree}')"
BACKEND_DEPENDENCY_LOCK_SHA256="sha256:$(
  sha256sum \
    backend/requirements.lock \
    backend/requirements-system.lock \
    backend/requirements-legacy.lock \
    backend/requirements-ci.lock |
    sha256sum | awk '{print $1}'
)"
BACKEND_BUILD_CONFIG_SHA256="sha256:$(
  sha256sum \
    Dockerfile \
    docker-compose.yml \
    docker-compose.dev.yml \
    docker-compose.gpu-governed.yml \
    docker-compose.dev-gpu-session.yml |
    sha256sum | awk '{print $1}'
)"
NEXPOLY_BUILD_REVISION="${NEXPOLY_BUILD_REVISION:-$CURRENT_SOURCE_REVISION}"
[[ "$NEXPOLY_BUILD_REVISION" =~ ^[0-9a-f]{40}$ ]] || {
  echo "NEXPOLY_BUILD_REVISION must be a full lowercase Git SHA." >&2
  exit 2
}
[[ "$NEXPOLY_BUILD_REVISION" == "$CURRENT_SOURCE_REVISION" ]] || {
  echo "NEXPOLY_BUILD_REVISION must match the currently checked-out Git HEAD." >&2
  exit 2
}
export NEXPOLY_BUILD_REVISION
export NEXPOLY_BUILD_SOURCE_TREE="$CURRENT_SOURCE_TREE"
export NEXPOLY_BACKEND_DEPENDENCY_LOCK_SHA256="$BACKEND_DEPENDENCY_LOCK_SHA256"
export NEXPOLY_BACKEND_BUILD_CONFIG_SHA256="$BACKEND_BUILD_CONFIG_SHA256"

COMPOSE=(docker compose -p nexpoly_dev -f docker-compose.yml -f docker-compose.dev.yml --env-file .env.dev)
GPU_COMPOSE=(
  docker compose -p nexpoly_dev
  -f docker-compose.yml
  -f docker-compose.dev.yml
  -f docker-compose.dev-gpu-session.yml
  --env-file .env.dev
)
GPU_SESSION_CONTROLLER="$ROOT_DIR/scripts/dev_gpu_session.py"
GPU_SESSION_PYTHON="/usr/bin/python3"
[[ -x "$GPU_SESSION_PYTHON" ]] && "$GPU_SESSION_PYTHON" -I -c \
  'import os, signal; assert callable(os.pidfd_open); assert callable(signal.pidfd_send_signal)' \
  >/dev/null 2>&1 || {
    echo "The fixed GPU session controller Python lacks required pidfd APIs: $GPU_SESSION_PYTHON" >&2
    exit 2
  }
export NEXPOLY_GPU_STATE_ROOT="$ROOT_DIR/.runtime/gpu-resource"
DEV_BACKEND_IMAGE="nexpoly-dev-backend:latest"
DEV_PYPI_INDEX_URL="${NEXPOLY_DEV_PYPI_INDEX_URL:-https://pypi.org/simple}"
DEV_PYPI_MIRROR_URL="${NEXPOLY_DEV_PYPI_MIRROR_URL:-https://mirrors.ustc.edu.cn/pypi/simple}"
FRONTEND_URL="http://127.0.0.1:${NEXPOLY_DEV_FRONTEND_PORT:-15173}"
BACKEND_URL="http://127.0.0.1:${NEXPOLY_DEV_BACKEND_PORT:-18000}"
WORKER_ENABLED="${MONOMER_MD_DEV_WORKER_ENABLED:-true}"
WORKER_SOCKET_DIR="${MONOMER_MD_DEV_WORKER_SOCKET_DIR:-$ROOT_DIR/.runtime/monomer-md-worker-socket}"
WORKER_JOB_ROOT="${MONOMER_MD_DEV_WORKER_JOB_ROOT:-$ROOT_DIR/.runtime/monomer-md-worker-runs}"
CANARY_STATE_DIR="${MONOMER_MD_DEV_CANARY_STATE_DIR:-$ROOT_DIR/.runtime/monomer-md-canaries}"
WORKER_PYTHON="${MONOMER_MD_DEV_WORKER_PYTHON:-$ROOT_DIR/.venv-monomer-md-worker/bin/python}"
WORKER_VENV_ROOT="$ROOT_DIR/.venv-monomer-md-worker"
WORKER_LOCK="$ROOT_DIR/workers/monomer_md_worker/requirements.lock"
WORKER_BASE_PYTHON="${MONOMER_MD_DEV_WORKER_BASE_PYTHON:-}"
WORKER_BASE_IDENTITY="${MONOMER_MD_DEV_WORKER_BASE_PYTHON_IDENTITY_SHA256:-}"
WORKER_WHEELHOUSE="${MONOMER_MD_DEV_WORKER_WHEELHOUSE:-}"
WORKER_OPENMM_DIR="${MONOMER_MD_DEV_BYTEFF2_OPENMM_DIR:-${BYTEFF2_OPENMM_DIR:-}}"
WORKER_PROCESS_HELPER="$ROOT_DIR/scripts/dev_worker_process.py"
DFT_WORKER_SOCKET_DIR="${MONOMER_DFT_DEV_WORKER_SOCKET_DIR:-$ROOT_DIR/.runtime/monomer-dft-worker-socket}"
DFT_DOWNLOAD_SPOOL_DIR="${MONOMER_DFT_DEV_DOWNLOAD_SPOOL_DIR:-$ROOT_DIR/.runtime/monomer-dft-download-spool}"
DFT_WORKER_SESSION_RECORD="$ROOT_DIR/.runtime/monomer-dft-worker.session.json"
DFT_WORKER_LOCK_SHA256="sha256:$(sha256sum workers/monomer_dft_worker/requirements.lock | awk '{print $1}')"
DFT_WORKER_VERSION="dev:${CURRENT_SOURCE_REVISION}:${CURRENT_SOURCE_TREE}:${DFT_WORKER_LOCK_SHA256}"
[[ "$WORKER_SOCKET_DIR" == /* ]] || WORKER_SOCKET_DIR="$ROOT_DIR/${WORKER_SOCKET_DIR#./}"
[[ "$WORKER_JOB_ROOT" == /* ]] || WORKER_JOB_ROOT="$ROOT_DIR/${WORKER_JOB_ROOT#./}"
[[ "$CANARY_STATE_DIR" == /* ]] || CANARY_STATE_DIR="$ROOT_DIR/${CANARY_STATE_DIR#./}"
[[ "$WORKER_PYTHON" == /* ]] || WORKER_PYTHON="$ROOT_DIR/${WORKER_PYTHON#./}"
[[ "$DFT_WORKER_SOCKET_DIR" == /* ]] || DFT_WORKER_SOCKET_DIR="$ROOT_DIR/${DFT_WORKER_SOCKET_DIR#./}"
[[ "$DFT_DOWNLOAD_SPOOL_DIR" == /* ]] || DFT_DOWNLOAD_SPOOL_DIR="$ROOT_DIR/${DFT_DOWNLOAD_SPOOL_DIR#./}"
WORKER_SOCKET="$WORKER_SOCKET_DIR/worker.sock"
WORKER_PID_FILE="$WORKER_JOB_ROOT/worker.pid"
WORKER_LOG_FILE="$WORKER_JOB_ROOT/worker.log"
WORKER_LOCK_SHA256="sha256:$(sha256sum "$WORKER_LOCK" | awk '{print $1}')"
: "${BYTEFF2_ROOT:?Set BYTEFF2_ROOT to the byteff2 tree in the pinned asset release}"

assert_dev_runtime_path() {
  local name="$1" path="$2" normalized
  normalized="$(realpath -ms -- "$path")"
  [[ "$normalized" == "$path" && "$path" == "$ROOT_DIR/.runtime/"* ]] || {
    echo "$name must resolve below $ROOT_DIR/.runtime: $path" >&2
    return 1
  }
  case "$path/" in
    /data/lzq/gith/nexpoly-runtime/*)
      echo "$name must not use the production runtime root." >&2
      return 1
      ;;
  esac
}

prepare_worker_runtime_directories() {
  assert_dev_runtime_path MONOMER_MD_DEV_WORKER_SOCKET_DIR "$WORKER_SOCKET_DIR"
  assert_dev_runtime_path MONOMER_MD_DEV_WORKER_JOB_ROOT "$WORKER_JOB_ROOT"
  [[ ! -L "$ROOT_DIR/.runtime" ]] || {
    echo "Development runtime root must not be a symlink." >&2
    return 1
  }
  mkdir -p "$ROOT_DIR/.runtime" "$WORKER_SOCKET_DIR" "$WORKER_JOB_ROOT"
  local directory
  for directory in "$ROOT_DIR/.runtime" "$WORKER_SOCKET_DIR" "$WORKER_JOB_ROOT"; do
    [[ -d "$directory" && ! -L "$directory" && "$(stat -c '%u' "$directory")" == "$(id -u)" ]] || {
      echo "Development Worker directory is unsafe: $directory" >&2
      return 1
    }
    chmod 700 "$directory"
  done
  [[ ! -L "$WORKER_PID_FILE" && ! -L "$WORKER_LOG_FILE" && ! -L "$WORKER_SOCKET" ]] || {
    echo "Development Worker metadata path must not be a symlink." >&2
    return 1
  }
}

prepare_dft_runtime_directories() {
  assert_dev_runtime_path MONOMER_DFT_DEV_WORKER_SOCKET_DIR "$DFT_WORKER_SOCKET_DIR"
  assert_dev_runtime_path MONOMER_DFT_DEV_DOWNLOAD_SPOOL_DIR "$DFT_DOWNLOAD_SPOOL_DIR"
  [[ "$DFT_WORKER_SOCKET_DIR" == "$ROOT_DIR/.runtime/monomer-dft-worker-socket" ]] || {
    echo "Main dev DFT Worker socket must use the fixed worktree-private path." >&2
    return 1
  }
  [[ "$DFT_DOWNLOAD_SPOOL_DIR" == "$ROOT_DIR/.runtime/monomer-dft-download-spool" ]] || {
    echo "Main dev DFT download spool must use the fixed worktree-private path." >&2
    return 1
  }
  mkdir -p "$ROOT_DIR/.runtime" "$DFT_WORKER_SOCKET_DIR" "$DFT_DOWNLOAD_SPOOL_DIR"
  local directory
  for directory in "$DFT_WORKER_SOCKET_DIR" "$DFT_DOWNLOAD_SPOOL_DIR"; do
    [[ -d "$directory" && ! -L "$directory" && "$(stat -c '%u' "$directory")" == "$(id -u)" ]] || {
      echo "Development DFT directory is unsafe: $directory" >&2
      return 1
    }
    chmod 700 "$directory"
  done
}

validate_dft_session_prerequisites() {
  local env_file="$ROOT_DIR/.env.monomer-dft.dev"
  local python_path="$ROOT_DIR/.runtime/venvs/monomer-dft-worker/bin/python"
  [[ -f "$env_file" && ! -L "$env_file" && "$(stat -c '%u:%a' "$env_file")" == "$(id -u):600" ]] || {
    echo "GPU session requires an owner-private .env.monomer-dft.dev before controller startup." >&2
    return 1
  }
  [[ -f "$python_path" && -x "$python_path" ]] || {
    echo "GPU session requires the isolated DFT Worker Python before controller startup." >&2
    return 1
  }
  local path
  for path in \
    "$DFT_WORKER_SESSION_RECORD" \
    "$ROOT_DIR/.runtime/monomer-dft-worker.pid" \
    "$DFT_WORKER_SOCKET_DIR/worker.sock"; do
    [[ ! -e "$path" && ! -L "$path" ]] || {
      echo "Preexisting DFT Worker state is not owned by the new session: $path" >&2
      return 1
    }
  done
}

validate_worker_transport_runtime() {
  [[ "$WORKER_OPENMM_DIR" == /* && -d "$WORKER_OPENMM_DIR" && ! -L "$WORKER_OPENMM_DIR" ]] || {
    echo "Set MONOMER_MD_DEV_BYTEFF2_OPENMM_DIR to the absolute OpenMM runtime directory required by Transport." >&2
    return 1
  }
  local relative
  for relative in \
    lib/libOpenMM.so \
    lib/plugins/libOpenMMCUDA.so \
    lib/libOpenMMVelocityVerlet.so \
    lib/plugins/libVelocityVerletPluginCUDA.so; do
    [[ -f "$WORKER_OPENMM_DIR/$relative" && ! -L "$WORKER_OPENMM_DIR/$relative" ]] || {
      echo "Transport runtime is missing a pinned native asset: $relative" >&2
      return 1
    }
  done
}

prepare_canary_state_directory() {
  case "$CANARY_STATE_DIR/" in
    /data/lzq/gith/nexpoly-runtime/*)
      echo "Development canary state must not use the production runtime root." >&2
      return 1
      ;;
  esac
  [[ ! -L "$CANARY_STATE_DIR" ]] || {
    echo "Development canary state directory must not be a symlink." >&2
    return 1
  }
  mkdir -p "$CANARY_STATE_DIR"
  [[ -d "$CANARY_STATE_DIR" && ! -L "$CANARY_STATE_DIR" ]] || {
    echo "Development canary state path is not a directory." >&2
    return 1
  }
  chmod 700 "$CANARY_STATE_DIR"
}

validate_asset_release() {
  [[ "${ASSET_RELEASE_VALIDATED:-false}" == "true" ]] && return 0
  [[ -d "$NEXPOLY_ASSET_ROOT" && ! -L "$NEXPOLY_ASSET_ROOT" ]] || {
    echo "NEXPOLY_ASSET_ROOT is not a release directory: $NEXPOLY_ASSET_ROOT" >&2
    return 1
  }
  local manifest="$NEXPOLY_ASSET_ROOT/ASSET-MANIFEST.json"
  [[ -f "$manifest" && ! -L "$manifest" ]] || {
    echo "Asset release is missing ASSET-MANIFEST.json: $NEXPOLY_ASSET_ROOT" >&2
    return 1
  }
  local expected_digest actual_digest
  expected_digest="$(basename "$NEXPOLY_ASSET_ROOT")"
  actual_digest="$(sha256sum "$manifest" | awk '{print $1}')"
  [[ "$expected_digest" =~ ^[0-9a-f]{64}$ && "$actual_digest" == "$expected_digest" ]] || {
    echo "Asset release digest does not match its immutable directory name." >&2
    return 1
  }
  for directory in model database backend-data byteff2; do
    [[ -d "$NEXPOLY_ASSET_ROOT/$directory" && ! -L "$NEXPOLY_ASSET_ROOT/$directory" ]] || {
      echo "Asset release is missing $directory/." >&2
      return 1
    }
  done
  local configured_byteff2_root pinned_byteff2_root
  configured_byteff2_root="$(realpath -e -- "$BYTEFF2_ROOT")"
  pinned_byteff2_root="$(realpath -e -- "$NEXPOLY_ASSET_ROOT/byteff2")"
  [[ "$configured_byteff2_root" == "$pinned_byteff2_root" ]] || {
    echo "BYTEFF2_ROOT must be the byteff2 tree from NEXPOLY_ASSET_ROOT." >&2
    return 1
  }
  while IFS= read -r relative_path; do
    [[ -e "$NEXPOLY_ASSET_ROOT/$relative_path" && ! -L "$NEXPOLY_ASSET_ROOT/$relative_path" ]] || {
      echo "Asset release is missing required model asset: $relative_path" >&2
      return 1
    }
  done < <(python3 backend/app/model_asset_manifest.py --profile release --format paths)

  python3 - "$NEXPOLY_ASSET_ROOT" "$manifest" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys


release_root = Path(sys.argv[1])
manifest_path = Path(sys.argv[2])
required_roots = {"model", "database", "backend-data", "byteff2"}


def fail(message: str) -> None:
    raise SystemExit(f"Asset release integrity check failed: {message}")


try:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
except Exception as exc:
    fail(f"invalid ASSET-MANIFEST.json: {exc}")
if isinstance(manifest, dict) and manifest.get("schema_version") == 2:
    try:
        from scripts.asset_release_contract import (
            AssetContractError,
            validate_schema_v2_release,
        )

        validate_schema_v2_release(
            release_root,
            expected_digest=f"sha256:{release_root.name}",
            releases_root=release_root.parent,
        )
    except (AssetContractError, OSError, ValueError) as exc:
        fail(f"schema-v2 contract validation failed: {exc}")
    raise SystemExit(0)
if not isinstance(manifest, dict) or set(manifest) != {
    "schema_version", "byteff2_commit", "byteff2_submodules", "assets"
} or manifest.get("schema_version") != 1:
    fail("ASSET-MANIFEST.json must use supported schema_version 1 or 2")
byteff2_commit = manifest.get("byteff2_commit")
byteff2_submodules = manifest.get("byteff2_submodules")
if not isinstance(byteff2_commit, str) or re.fullmatch(r"[0-9a-f]{40}", byteff2_commit) is None:
    fail("manifest byteff2_commit must be a full lowercase commit SHA")
if not isinstance(byteff2_submodules, dict) or any(
    not isinstance(name, str)
    or not name
    or not isinstance(commit, str)
    or re.fullmatch(r"[0-9a-f]{40}", commit) is None
    for name, commit in byteff2_submodules.items()
):
    fail("manifest byteff2_submodules is invalid")
assets = manifest.get("assets")
if not isinstance(assets, dict) or set(assets) != required_roots:
    fail("manifest assets must contain exactly model, database, backend-data, and byteff2")

for asset_root_name, records in sorted(assets.items()):
    if (
        not isinstance(asset_root_name, str)
        or not asset_root_name
        or "/" in asset_root_name
        or "\\" in asset_root_name
        or asset_root_name in {".", ".."}
    ):
        fail(f"unsafe asset root name: {asset_root_name!r}")
    if not isinstance(records, list):
        fail(f"manifest entry for {asset_root_name!r} must be a list")
    asset_root = release_root / asset_root_name
    if not asset_root.is_dir() or asset_root.is_symlink():
        fail(f"asset root is missing or is a symlink: {asset_root_name}")

    expected_paths: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "size"}:
            fail(f"invalid record {index} in asset root {asset_root_name}")
        relative_path = record["path"]
        expected_size = record["size"]
        expected_sha256 = record["sha256"]
        if not isinstance(relative_path, str) or not relative_path:
            fail(f"record {index} in {asset_root_name} has an invalid path")
        path_parts = relative_path.split("/")
        if (
            relative_path.startswith("/")
            or "\\" in relative_path
            or any(part in {"", ".", ".."} for part in path_parts)
        ):
            fail(f"unsafe manifest path: {asset_root_name}/{relative_path}")
        if relative_path in expected_paths:
            fail(f"duplicate manifest path: {asset_root_name}/{relative_path}")
        expected_paths.add(relative_path)
        if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0:
            fail(f"invalid size for {asset_root_name}/{relative_path}")
        if not isinstance(expected_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
            fail(f"invalid sha256 for {asset_root_name}/{relative_path}")

        candidate = asset_root.joinpath(*path_parts)
        cursor = release_root
        for part in (asset_root_name, *path_parts):
            cursor /= part
            if cursor.is_symlink():
                fail(f"symlink is not allowed: {asset_root_name}/{relative_path}")
        if not candidate.is_file():
            fail(f"manifest asset is missing or is not a regular file: {asset_root_name}/{relative_path}")
        actual_size = candidate.stat().st_size
        if actual_size != expected_size:
            fail(
                f"size mismatch for {asset_root_name}/{relative_path}: "
                f"expected {expected_size}, found {actual_size}"
            )
        digest = hashlib.sha256()
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        actual_sha256 = digest.hexdigest()
        if actual_sha256 != expected_sha256:
            fail(f"sha256 mismatch for {asset_root_name}/{relative_path}")

    actual_paths: set[str] = set()
    for directory, directory_names, file_names in os.walk(asset_root, followlinks=False):
        directory_path = Path(directory)
        for name in directory_names:
            path = directory_path / name
            if path.is_symlink():
                fail(f"symlink directory is not allowed: {path.relative_to(release_root)}")
        for name in file_names:
            path = directory_path / name
            if path.is_symlink() or not path.is_file():
                fail(f"non-regular asset is not allowed: {path.relative_to(release_root)}")
            actual_paths.add(path.relative_to(asset_root).as_posix())
    unlisted = sorted(actual_paths - expected_paths)
    if unlisted:
        fail(f"unlisted asset file: {asset_root_name}/{unlisted[0]}")
    missing = sorted(expected_paths - actual_paths)
    if missing:
        fail(f"manifest asset is missing: {asset_root_name}/{missing[0]}")

commit_marker = release_root / "byteff2" / "BYTEFF2-COMMIT"
if not commit_marker.is_file() or commit_marker.is_symlink():
    fail("byteff2/BYTEFF2-COMMIT is missing or unsafe")
if commit_marker.read_text(encoding="ascii").strip() != byteff2_commit:
    fail("byteff2/BYTEFF2-COMMIT differs from the manifest")
PY
  ASSET_RELEASE_VALIDATED=true
}

build_backend_image() {
  assert_clean_candidate
  assert_default_builder
  "${COMPOSE[@]}" build \
    --builder default \
    --build-arg SOURCE_REVISION="$NEXPOLY_BUILD_REVISION" \
    --build-arg SOURCE_TREE="$CURRENT_SOURCE_TREE" \
    --build-arg DEPENDENCY_LOCK_SHA256="$BACKEND_DEPENDENCY_LOCK_SHA256" \
    --build-arg BUILD_CONFIG_SHA256="$BACKEND_BUILD_CONFIG_SHA256" \
    --build-arg PYPI_INDEX_URL="$DEV_PYPI_INDEX_URL" \
    --build-arg PYPI_MIRROR_URL="$DEV_PYPI_MIRROR_URL" \
    backend
  docker image inspect "$DEV_BACKEND_IMAGE" >/dev/null
}

assert_clean_candidate() {
  git diff --quiet --ignore-submodules -- || {
    echo "Development runtime images must be built from a clean tracked worktree." >&2
    return 1
  }
  git diff --cached --quiet --ignore-submodules -- || {
    echo "Development runtime images must be built after staged changes are committed." >&2
    return 1
  }
  [[ -z "$(git ls-files --others --exclude-standard)" ]] || {
    echo "Development runtime images must not omit untracked source files." >&2
    return 1
  }
}

assert_default_builder() {
  local driver
  driver="$(docker buildx inspect default 2>/dev/null | awk -F: '/^Driver:/ {gsub(/[[:space:]]/, "", $2); print $2; exit}')"
  [[ "$driver" == "docker" ]] || {
    echo "Docker's default builder must use the docker driver; found ${driver:-unknown}." >&2
    return 1
  }
}

cleanup_legacy_builder() {
  if docker buildx inspect nexpoly-dev-safe >/dev/null 2>&1; then
    docker buildx rm nexpoly-dev-safe
  fi
  if [[ -n "$(docker ps -aq --filter name='^/buildx_buildkit_nexpoly-dev-safe0$')" ]]; then
    echo "Legacy nexpoly-dev-safe BuildKit helper still exists after cleanup." >&2
    return 1
  fi
}

run_dev_migrations() {
  local ledger_exists migration_count contract_applied mode
  ledger_exists="$(
    "${COMPOSE[@]}" exec -T lab-postgres \
      psql -X -A -t -v ON_ERROR_STOP=1 -U nexpoly_dev -d nexpoly_dev \
      -c "SELECT to_regclass('governance.schema_migrations') IS NOT NULL"
  )"
  mode=bootstrap
  if [[ "$ledger_exists" == "t" ]]; then
    migration_count="$(
      "${COMPOSE[@]}" exec -T lab-postgres \
        psql -X -A -t -v ON_ERROR_STOP=1 -U nexpoly_dev -d nexpoly_dev \
        -c "SELECT COUNT(*) FROM governance.schema_migrations"
    )"
    if [[ ! "$migration_count" =~ ^[0-9]+$ ]]; then
      echo "Development migration ledger returned an invalid count." >&2
      return 1
    fi
    if (( migration_count > 0 )); then
      contract_applied="$(
        "${COMPOSE[@]}" exec -T lab-postgres \
          psql -X -A -t -v ON_ERROR_STOP=1 -U nexpoly_dev -d nexpoly_dev \
          -c "SELECT EXISTS (SELECT 1 FROM governance.schema_migrations WHERE version = '0012_drop_polytao_jobs')"
      )"
      if [[ "$contract_applied" != "t" ]]; then
        echo "Destructive migration 0012 is pending; ordinary up will not apply it." >&2
        echo "Review the contract, then run: ./scripts/dev_server_gpu.sh contract-migrate" >&2
        return 1
      fi
      mode=expand
    fi
  fi
  echo "Applying development PostgreSQL migrations in explicit $mode mode."
  "${COMPOSE[@]}" run --rm postgres-init \
    python -m app.postgres_migrations --mode "$mode"
}

run_dev_contract_migration() {
  local source_version row_count restored_row_count stamp archive_dir full_dump table_dump
  local verify_database archive_verified
  "${COMPOSE[@]}" up -d lab-postgres
  source_version="$(
    "${COMPOSE[@]}" exec -T lab-postgres \
      psql -X -A -t -v ON_ERROR_STOP=1 -U nexpoly_dev -d nexpoly_dev \
      -c "SELECT MAX(version) FROM governance.schema_migrations"
  )"
  [[ "$source_version" == "0011_monomer_md_demo_steps" ]] || {
    echo "Contract migration requires schema version 0011; found ${source_version:-none}." >&2
    return 1
  }
  row_count="$(
    "${COMPOSE[@]}" exec -T lab-postgres \
      psql -X -A -t -v ON_ERROR_STOP=1 -U nexpoly_dev -d nexpoly_dev \
      -c "SELECT COUNT(*) FROM generation.polytao_jobs"
  )"
  [[ "$row_count" =~ ^[0-9]+$ ]] || {
    echo "PolyTAO contract archive returned an invalid row count." >&2
    return 1
  }

  "${COMPOSE[@]}" stop frontend-dev backend
  stamp="$(date -u +%Y%m%dT%H%M%SZ)-$(date +%s%N)"
  archive_dir="$ROOT_DIR/.runtime/contract-archives/0012/$stamp"
  mkdir -p "$archive_dir"
  chmod 700 "$ROOT_DIR/.runtime/contract-archives" \
    "$ROOT_DIR/.runtime/contract-archives/0012" "$archive_dir"
  full_dump="$archive_dir/nexpoly_dev.full.dump"
  table_dump="$archive_dir/polytao_jobs.dump"
  "${COMPOSE[@]}" exec -T lab-postgres \
    pg_dump -U nexpoly_dev -d nexpoly_dev -Fc --no-owner --no-privileges >"$full_dump"
  "${COMPOSE[@]}" exec -T lab-postgres \
    pg_dump -U nexpoly_dev -d nexpoly_dev -Fc --no-owner --no-privileges \
      --table=generation.polytao_jobs >"$table_dump"
  "${COMPOSE[@]}" exec -T lab-postgres pg_restore --list <"$full_dump" >/dev/null
  "${COMPOSE[@]}" exec -T lab-postgres pg_restore --list <"$table_dump" >/dev/null
  chmod 600 "$full_dump" "$table_dump"
  sha256sum "$full_dump" "$table_dump" >"$archive_dir/SHA256SUMS"
  chmod 600 "$archive_dir/SHA256SUMS"
  verify_database="nexpoly_c0012_$(date +%s%N)"
  "${COMPOSE[@]}" exec -T lab-postgres createdb -U nexpoly_dev "$verify_database"
  archive_verified=false
  if "${COMPOSE[@]}" exec -T lab-postgres \
      psql -X -v ON_ERROR_STOP=1 -U nexpoly_dev -d "$verify_database" \
        -c "CREATE SCHEMA generation" \
    && "${COMPOSE[@]}" exec -T lab-postgres \
      pg_restore --exit-on-error --no-owner --no-privileges \
        -U nexpoly_dev -d "$verify_database" <"$table_dump" \
    && restored_row_count="$(
      "${COMPOSE[@]}" exec -T lab-postgres \
        psql -X -A -t -v ON_ERROR_STOP=1 -U nexpoly_dev -d "$verify_database" \
          -c "SELECT COUNT(*) FROM generation.polytao_jobs"
    )" \
    && [[ "$restored_row_count" == "$row_count" ]]; then
    archive_verified=true
  fi
  "${COMPOSE[@]}" exec -T lab-postgres \
    dropdb --if-exists -U nexpoly_dev "$verify_database"
  [[ "$archive_verified" == "true" ]] || {
    echo "Isolated PolyTAO archive restore or row-count verification failed." >&2
    return 1
  }
  python3 - "$archive_dir/archive.json" "$source_version" "$row_count" \
    "$restored_row_count" "$full_dump" "$table_dump" <<'PY'
import json
from pathlib import Path
import sys

output, source_version, row_count, restored_row_count, full_dump, table_dump = sys.argv[1:]
Path(output).write_text(
    json.dumps(
        {
            "schema_version": 1,
            "source_schema_migration_version": source_version,
            "schema_compatibility_floor": {
                "version": "0012_drop_polytao_jobs",
                "checksum": "c59b6f1efe9f926ad135379bd1a7141a7920730fa93c0e802646b1b913511728",
            },
            "table": "generation.polytao_jobs",
            "row_count": int(row_count),
            "restored_row_count": int(restored_row_count),
            "full_dump": full_dump,
            "table_dump": table_dump,
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY
  chmod 600 "$archive_dir/archive.json"
  "${COMPOSE[@]}" run --rm postgres-init \
    python -m app.postgres_migrations --mode contract-0012
  "${COMPOSE[@]}" exec -T lab-postgres \
    psql -X -A -t -v ON_ERROR_STOP=1 -U nexpoly_dev -d nexpoly_dev \
      -c "SELECT version FROM governance.schema_migrations WHERE version = '0012_drop_polytao_jobs'" \
    | grep -Fxq 0012_drop_polytao_jobs
  echo "Development contract migration completed. Audit archive: $archive_dir"
  echo "Run ./scripts/dev_server_gpu.sh up to rebuild and start the application."
}

compute_backend_config_hash() {
  "${COMPOSE[@]}" config --format json | python3 -c '
import hashlib
import json
import sys

config = json.load(sys.stdin)
service = config["services"]["backend"]
labels = service.get("labels") or {}
labels.pop("com.nexpoly.dev.config-hash", None)
payload = json.dumps(service, sort_keys=True, separators=(",", ":")).encode()
print(hashlib.sha256(payload).hexdigest())
'
}

compute_gpu_backend_config_hash() {
  "${GPU_COMPOSE[@]}" config --format json | python3 -c '
import hashlib
import json
import sys

config = json.load(sys.stdin)
service = config["services"]["backend"]
labels = service.get("labels") or {}
labels.pop("com.nexpoly.dev.config-hash", None)
payload = json.dumps(service, sort_keys=True, separators=(",", ":")).encode()
print(hashlib.sha256(payload).hexdigest())
'
}

wait_backend_configured() {
  local container_id health
  for _ in $(seq 1 180); do
    container_id="$("${COMPOSE[@]}" ps -q backend)"
    if [[ -n "$container_id" ]]; then
      health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$container_id")"
      if [[ "$health" == "healthy" ]]; then
        return 0
      fi
      if [[ "$(docker inspect -f '{{.State.Status}}' "$container_id")" == "exited" ]]; then
        "${COMPOSE[@]}" logs --tail=120 backend >&2
        return 1
      fi
    fi
    sleep 1
  done
  echo "Timed out waiting for the CPU-only development backend preflight." >&2
  "${COMPOSE[@]}" logs --tail=120 backend >&2 || true
  return 1
}

verify_backend_drift() {
  local container_id expected_image actual_image desired_hash actual_hash image_revision runtime_revision
  local image_tree image_lock image_build_config runtime_identity
  assert_clean_candidate
  assert_default_builder
  container_id="$("${COMPOSE[@]}" ps -q backend)"
  [[ -n "$container_id" ]] || { echo "Development backend container is missing." >&2; return 1; }
  expected_image="$(docker image inspect -f '{{.Id}}' "$DEV_BACKEND_IMAGE")"
  actual_image="$(docker inspect -f '{{.Image}}' "$container_id")"
  [[ "$actual_image" == "$expected_image" ]] || {
    echo "Development backend is running a stale image ID." >&2
    return 1
  }
  desired_hash="$(compute_backend_config_hash)"
  actual_hash="$(docker inspect -f '{{index .Config.Labels "com.nexpoly.dev.config-hash"}}' "$container_id")"
  [[ -n "$desired_hash" && "$actual_hash" == "$desired_hash" ]] || {
    echo "Development backend Compose configuration has drifted." >&2
    return 1
  }
  image_revision="$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$DEV_BACKEND_IMAGE")"
  runtime_revision="$(docker exec "$container_id" python -c 'import os; print(os.environ.get("BUILD_REVISION", ""))')"
  image_tree="$(docker image inspect -f '{{index .Config.Labels "com.nexpoly.source.tree"}}' "$DEV_BACKEND_IMAGE")"
  image_lock="$(docker image inspect -f '{{index .Config.Labels "com.nexpoly.backend.dependency-lock"}}' "$DEV_BACKEND_IMAGE")"
  image_build_config="$(docker image inspect -f '{{index .Config.Labels "com.nexpoly.backend.build-config"}}' "$DEV_BACKEND_IMAGE")"
  [[ "$image_revision" == "$NEXPOLY_BUILD_REVISION" ]] || {
    echo "Development backend image revision does not match the requested source revision." >&2
    return 1
  }
  [[ "$runtime_revision" == "$NEXPOLY_BUILD_REVISION" ]] || {
    echo "Development backend runtime revision does not match the requested source revision." >&2
    return 1
  }
  [[ "$image_tree" == "$CURRENT_SOURCE_TREE" &&
    "$image_lock" == "$BACKEND_DEPENDENCY_LOCK_SHA256" &&
    "$image_build_config" == "$BACKEND_BUILD_CONFIG_SHA256" ]] || {
    echo "Development backend image source-tree/dependency/config identity has drifted." >&2
    return 1
  }
  runtime_identity="$(docker exec "$container_id" python -c \
    'import json, os; print(json.dumps([os.getenv("BUILD_SOURCE_TREE"), os.getenv("BUILD_DEPENDENCY_LOCK_SHA256"), os.getenv("BUILD_CONFIG_SHA256")], separators=(",", ":")))')"
  [[ "$runtime_identity" == "[\"$CURRENT_SOURCE_TREE\",\"$BACKEND_DEPENDENCY_LOCK_SHA256\",\"$BACKEND_BUILD_CONFIG_SHA256\"]" ]] || {
    echo "Development backend runtime source-tree/dependency/config identity has drifted." >&2
    return 1
  }
  docker exec "$container_id" python -c \
    "import os; expected={'WEB_CONCURRENCY':'1','NVIDIA_VISIBLE_DEVICES':'none','GPU_PRELOAD_MODE':'lazy','GPU_MAX_CONCURRENT_INFERENCES':'1','GPU_MAX_WAITING_INFERENCES':'8','GPU_SYNC_QUEUE_TIMEOUT_SECONDS':'30','GPU_ASYNC_QUEUE_TIMEOUT_SECONDS':'600','MODEL_ENABLED':'false','OCSR_ENABLED':'false','OCSR_DEVICE':'cpu','GEN_MODEL_ENABLED':'false','GEN_DEVICE':'cpu','GEN_JOB_WORKERS':'1','GEN_MAX_ACTIVE_JOBS':'8','RETRO_MODEL_ENABLED':'false','RETRO_DEVICE':'cpu','POLYTAO_ENABLED':'false','POLYTAO_DEVICE':'cpu','POLYTAO_JOB_THREADS':'1','POLYTAO_MAX_ACTIVE_JOBS':'1','MONOMER_MD_SUBMIT_ENABLED':'false','MONOMER_DFT_SUBMIT_ENABLED':'false'}; actual={key:os.getenv(key) for key in expected}; assert actual == expected, actual"
  docker inspect "$container_id" | python3 -c '
import json, sys
container = json.load(sys.stdin)[0]
if container["HostConfig"].get("DeviceRequests"):
    raise SystemExit("CPU-only development backend must not have a GPU DeviceRequest")
'
}

require_worker_venv_config() {
  local expected_python="$WORKER_VENV_ROOT/bin/python"
  [[ "$WORKER_PYTHON" == "$expected_python" ]] || {
    echo "MONOMER_MD_DEV_WORKER_PYTHON must be the isolated dev venv: $expected_python" >&2
    return 1
  }
  [[ "$WORKER_BASE_PYTHON" == /* && -x "$WORKER_BASE_PYTHON" ]] || {
    echo "MONOMER_MD_DEV_WORKER_BASE_PYTHON must be an absolute executable path." >&2
    return 1
  }
  [[ "$WORKER_BASE_IDENTITY" =~ ^sha256:[0-9a-f]{64}$ ]] || {
    echo "MONOMER_MD_DEV_WORKER_BASE_PYTHON_IDENTITY_SHA256 must pin the frozen base Python." >&2
    return 1
  }
}

worker_base_identity() {
  [[ "$WORKER_BASE_PYTHON" == /* && -x "$WORKER_BASE_PYTHON" ]] || {
    echo "Set MONOMER_MD_DEV_WORKER_BASE_PYTHON to an absolute executable path." >&2
    return 1
  }
  python3 scripts/prepare_dev_worker_venv.py identity \
    --base-python "$WORKER_BASE_PYTHON"
}

worker_prepare_venv() {
  validate_asset_release
  require_worker_venv_config
  local -a command=(
    python3 scripts/prepare_dev_worker_venv.py prepare
    --repository-root "$ROOT_DIR"
    --target "$WORKER_VENV_ROOT"
    --lock "$WORKER_LOCK"
    --pid-file "$WORKER_PID_FILE"
    --socket "$WORKER_SOCKET"
    --base-python "$WORKER_BASE_PYTHON"
    --expected-base-identity "$WORKER_BASE_IDENTITY"
  )
  if [[ -n "$WORKER_WHEELHOUSE" ]]; then
    command+=(--wheelhouse "$WORKER_WHEELHOUSE")
  fi
  "${command[@]}"
}

worker_verify_venv() {
  require_worker_venv_config
  python3 scripts/prepare_dev_worker_venv.py verify \
    --repository-root "$ROOT_DIR" \
    --target "$WORKER_VENV_ROOT" \
    --lock "$WORKER_LOCK" \
    --base-python "$WORKER_BASE_PYTHON" \
    --expected-base-identity "$WORKER_BASE_IDENTITY" \
    >/dev/null
}

worker_process_record() {
  local command="$1"
  shift
  [[ "${NEXPOLY_DEV_GPU_SESSION_ID:-}" =~ ^[0-9a-f]{32}$ ]] || {
    echo "MD Worker process operations require the exact controller session identity." >&2
    return 1
  }
  "$GPU_SESSION_PYTHON" -I "$WORKER_PROCESS_HELPER" "$command" \
    --record "$WORKER_PID_FILE" \
    --python "$WORKER_PYTHON" \
    --socket "$WORKER_SOCKET" \
    --source-sha "$CURRENT_SOURCE_REVISION" \
    --source-tree "$CURRENT_SOURCE_TREE" \
    --worker-lock-sha256 "$WORKER_LOCK_SHA256" \
    --session-id "$NEXPOLY_DEV_GPU_SESSION_ID" \
    "$@"
}

worker_is_running() {
  [[ -f "$WORKER_PID_FILE" ]] || return 1
  worker_process_record verify --require-instance >/dev/null 2>&1
}

worker_launch_is_running() {
  [[ -f "$WORKER_PID_FILE" ]] || return 1
  worker_process_record verify >/dev/null 2>&1
}

worker_assert_process_identity() {
  worker_process_record verify --require-instance >/dev/null || {
    echo "Dev monomer MD worker process identity cannot be verified." >&2
    return 1
  }
}

worker_health_payload() {
  [[ -S "$WORKER_SOCKET" && ! -L "$WORKER_SOCKET" ]] || return 1
  [[ "$(stat -c '%u:%a' "$WORKER_SOCKET")" == "$(id -u):600" ]] || return 1
  curl --max-time 30 --unix-socket "$WORKER_SOCKET" -fsS http://localhost/health
}

worker_secure_socket() {
  [[ -S "$WORKER_SOCKET" && ! -L "$WORKER_SOCKET" ]] || return 1
  [[ "$(stat -c '%u' "$WORKER_SOCKET")" == "$(id -u)" ]] || return 1
  chmod 600 -- "$WORKER_SOCKET" || return 1
  [[ -S "$WORKER_SOCKET" && ! -L "$WORKER_SOCKET" ]] || return 1
  [[ "$(stat -c '%u:%a' "$WORKER_SOCKET")" == "$(id -u):600" ]]
}

worker_health_validate() {
  local mode="$1" expected_instance="" payload
  payload="$(worker_health_payload)"
  if [[ "$mode" == "strict" ]]; then
    expected_instance="$(
      worker_process_record verify --require-instance |
        python3 -c 'import json, sys; print(json.load(sys.stdin)["worker_instance_id"])'
    )"
  fi
  printf '%s' "$payload" | "$WORKER_PYTHON" -c '
import json
import os
import sys

payload = json.load(sys.stdin)
mode, expected_instance, source_sha, source_tree, source_root, venv_prefix, lock_sha, python_executable, byteff2_root = sys.argv[1:]
required_protocols = {"Density", "Transport", "HVap", "Dielectric", "Compressibility"}
protocols = payload.get("protocols")
instance = payload.get("worker_instance_id")
valid = (
    payload.get("status") == "ok"
    and payload.get("mode") == "real"
    and payload.get("runtime_ready") is True
    and payload.get("db_configured") is True
    and payload.get("source_sha") == source_sha
    and payload.get("source_tree") == source_tree
    and payload.get("source_root") == source_root
    and payload.get("venv_prefix") == venv_prefix
    and payload.get("worker_lock_sha256") == lock_sha
    and payload.get("python_executable") == python_executable
    and os.path.realpath(payload.get("byteff2_root", "")) == os.path.realpath(byteff2_root)
    and isinstance(instance, str)
    and bool(instance)
    and isinstance(protocols, dict)
    and required_protocols <= set(protocols)
    and all(
        isinstance(protocols[name], dict)
        and protocols[name].get("supported") is True
        and protocols[name].get("runtime_ready") is True
        for name in required_protocols
    )
    and (mode == "prebind" or instance == expected_instance)
)
if not valid:
    raise SystemExit(2)
print(instance)
' "$mode" "$expected_instance" "$CURRENT_SOURCE_REVISION" "$CURRENT_SOURCE_TREE" \
    "$ROOT_DIR" "$WORKER_VENV_ROOT" "$WORKER_LOCK_SHA256" \
    "$(realpath -e -- "$WORKER_PYTHON")" "$BYTEFF2_ROOT"
}

worker_health() {
  worker_health_validate strict >/dev/null
}

worker_active_jobs() {
  local payload expected_instance
  worker_health
  payload="$(worker_health_payload)"
  expected_instance="$(
    worker_process_record verify --require-instance |
      python3 -c 'import json, sys; print(json.load(sys.stdin)["worker_instance_id"])'
  )"
  printf '%s' "$payload" | "$WORKER_PYTHON" -c \
    'import json, sys; payload = json.load(sys.stdin); value = payload.get("active_jobs"); instance = payload.get("worker_instance_id"); sys.exit(2) if instance != sys.argv[1] or isinstance(value, bool) or not isinstance(value, int) or value < 0 else None; print(value)' \
    "$expected_instance"
}

worker_cleanup_failed_launch() {
  local spawn_pid="$1" collected=false
  if worker_process_record verify >/dev/null 2>&1; then
    worker_process_record terminate >/dev/null || return 1
  fi
  for _ in $(seq 1 20); do
    if worker_process_record collect-dead >/dev/null 2>&1; then
      collected=true
      break
    fi
    sleep 0.1
  done
  if [[ "$collected" != "true" ]]; then
    echo "Refusing to collect the failed MD Worker launch without exact dead-process evidence." >&2
    return 1
  fi
  wait "$spawn_pid" 2>/dev/null || true
  if [[ -e "$WORKER_SOCKET" || -L "$WORKER_SOCKET" ]]; then
    [[ -S "$WORKER_SOCKET" && ! -L "$WORKER_SOCKET" ]] || {
      echo "Refusing to collect an unsafe failed-launch Worker socket." >&2
      return 1
    }
    rm -f -- "$WORKER_SOCKET"
  fi
}

worker_up() {
  if [[ "$WORKER_ENABLED" != "true" ]]; then
    echo "Dev monomer MD worker is disabled by MONOMER_MD_DEV_WORKER_ENABLED=$WORKER_ENABLED"
    return 0
  fi
  [[ "${NEXPOLY_DEV_GPU_SESSION_ACTIVE:-false}" == "true" ]] || {
    echo "Dev monomer MD worker may start only inside gpu-session-up." >&2
    return 1
  }
  validate_asset_release
  prepare_worker_runtime_directories
  worker_verify_venv
  validate_worker_transport_runtime
  if [[ -f "$WORKER_PID_FILE" ]]; then
    if worker_is_running && worker_health; then
      echo "Dev monomer MD worker is already healthy."
      return 0
    fi
    echo "Dev monomer MD worker record exists but its exact process/health identity is invalid; refusing to replace it." >&2
    return 1
  fi
  if [[ -e "$WORKER_SOCKET" || -L "$WORKER_SOCKET" ]]; then
    echo "Dev monomer MD socket path exists without a managed process record; inspect it before restarting." >&2
    return 1
  fi
  if [[ ! -x "$WORKER_PYTHON" ]]; then
    echo "Dev monomer MD Python is not executable: $WORKER_PYTHON" >&2
    return 1
  fi
  if [[ ! -d "$BYTEFF2_ROOT" ]]; then
    echo "ByteFF2 root does not exist: $BYTEFF2_ROOT" >&2
    return 1
  fi
  if [[ -e "$WORKER_LOG_FILE" ]]; then
    [[ -f "$WORKER_LOG_FILE" && ! -L "$WORKER_LOG_FILE" && "$(stat -c '%u' "$WORKER_LOG_FILE")" == "$(id -u)" ]] || {
      echo "Dev monomer MD log file is unsafe: $WORKER_LOG_FILE" >&2
      return 1
    }
    chmod 600 "$WORKER_LOG_FILE"
  fi

  (
    cd "$ROOT_DIR/workers/monomer_md_worker"
    export APP_POSTGRES_DSN="postgresql://nexpoly_dev:nexpoly_dev@127.0.0.1:${NEXPOLY_DEV_POSTGRES_PORT:-15532}/nexpoly_dev"
    export BYTEFF2_PYTHON="$WORKER_PYTHON"
    export BYTEFF2_OPENMM_DIR="$WORKER_OPENMM_DIR"
    export BYTEFF2_ROOT
    export MONOMER_MD_CUDA_VISIBLE_DEVICES="1"
    export MONOMER_MD_DEFAULT_STEPS=300
    export MONOMER_MD_HEALTH_PROBE_TIMEOUT_SECONDS=30
    export MONOMER_MD_JOB_ROOT="$WORKER_JOB_ROOT"
    export MONOMER_MD_MAX_ACTIVE_JOBS=1
    export MONOMER_MD_MAX_CONCURRENT_JOBS=1
    export MONOMER_MD_MAX_STEPS=300
    export MONOMER_MD_PYTHON="$WORKER_PYTHON"
    export MONOMER_MD_REPORT_INTERVAL=10
    export MONOMER_MD_WORKER_ID="monomer-md-dev-$NEXPOLY_DEV_GPU_SESSION_ID"
    export NEXPOLY_DEV_GPU_SESSION_ID
    export MONOMER_MD_WORKER_MODE=real
    export MONOMER_MD_WORKER_UDS="$WORKER_SOCKET"
    if [[ "${NEXPOLY_DEV_GPU_SESSION_ACTIVE:-false}" == "true" ]]; then
      export MONOMER_MD_GPU_BROKER_ENABLED=true
      export MONOMER_MD_GPU_BROKER_ENVIRONMENT=dev
      export MONOMER_MD_GPU_BROKER_SOCKET_PATH="$ROOT_DIR/.runtime/gpu-resource/broker.sock"
      export MONOMER_MD_GPU_MPS_PIPE_ROOT="$ROOT_DIR/.runtime/gpu-resource"
      export MONOMER_MD_GPU_BROKER_WAIT_TIMEOUT_SECONDS=45
      export MONOMER_MD_GPU_SCOPE_LAUNCHER=systemd-user-scope
    else
      export MONOMER_MD_GPU_BROKER_ENABLED=false
    fi
    export NEXPOLY_GPU_DEVICE="1"
    export PATH="$(dirname "$WORKER_PYTHON"):$(dirname "$WORKER_BASE_PYTHON"):$PATH"
    export PYTHONPATH="$ROOT_DIR:$BYTEFF2_ROOT:$BYTEFF2_ROOT/submodules/bytemol${PYTHONPATH:+:$PYTHONPATH}"
    exec nohup "$WORKER_PYTHON" -m uvicorn app.main:app --uds "$WORKER_SOCKET"
  ) >>"$WORKER_LOG_FILE" 2>&1 < /dev/null &
  local spawn_pid="$!" record_created=false worker_instance=""
  for _ in $(seq 1 50); do
    if worker_process_record create \
      --pid "$spawn_pid" \
      --expected-argv "$WORKER_PYTHON" -m uvicorn app.main:app --uds "$WORKER_SOCKET" \
      >/dev/null 2>&1; then
      record_created=true
      break
    fi
    kill -0 "$spawn_pid" 2>/dev/null || break
    sleep 0.02
  done
  if [[ "$record_created" != "true" ]]; then
    kill "$spawn_pid" 2>/dev/null || true
    wait "$spawn_pid" 2>/dev/null || true
    echo "Dev monomer MD worker launch identity could not be recorded." >&2
    return 1
  fi

  for _ in $(seq 1 45); do
    if [[ -e "$WORKER_SOCKET" || -L "$WORKER_SOCKET" ]]; then
      if ! worker_secure_socket; then
        echo "Dev monomer MD worker created an unsafe socket." >&2
        worker_cleanup_failed_launch "$spawn_pid" || true
        return 1
      fi
    fi
    if worker_instance="$(worker_health_validate prebind 2>/dev/null)"; then
      if ! worker_process_record bind-instance --instance-id "$worker_instance" >/dev/null \
        || ! worker_assert_process_identity \
        || ! worker_health; then
        worker_cleanup_failed_launch "$spawn_pid" || true
        return 1
      fi
      echo "Dev monomer MD worker is healthy on $WORKER_SOCKET"
      return 0
    fi
    if ! worker_launch_is_running; then
      echo "Dev monomer MD worker exited during startup." >&2
      tail -n 40 "$WORKER_LOG_FILE" >&2 || true
      worker_cleanup_failed_launch "$spawn_pid" || true
      return 1
    fi
    sleep 1
  done
  echo "Timed out waiting for the dev monomer MD worker." >&2
  tail -n 40 "$WORKER_LOG_FILE" >&2 || true
  worker_cleanup_failed_launch "$spawn_pid" || true
  return 1
}

worker_stop() {
  if [[ ! -f "$WORKER_PID_FILE" ]]; then
    if [[ -e "$WORKER_SOCKET" || -L "$WORKER_SOCKET" ]]; then
      echo "Refusing to remove the dev worker socket because the managed PID file is missing." >&2
      return 1
    fi
    echo "Dev monomer MD worker is already stopped."
    return 0
  fi

  local record pid
  record="$(worker_process_record verify --require-instance)" || {
    echo "Refusing to stop dev worker because PID/start/command/instance identity is invalid." >&2
    return 1
  }
  pid="$(printf '%s' "$record" | python3 -c 'import json, sys; print(json.load(sys.stdin)["pid"])')"

  local active_jobs
  if ! active_jobs="$(worker_active_jobs)"; then
    echo "Refusing to stop dev worker because active job state could not be verified." >&2
    return 1
  fi
  if (( active_jobs > 0 )); then
    echo "Refusing to stop dev worker while $active_jobs job(s) are active." >&2
    return 1
  fi

  worker_process_record terminate --require-instance >/dev/null
  for _ in $(seq 1 20); do
    worker_process_record verify --require-instance >/dev/null 2>&1 || break
    sleep 0.5
  done
  if worker_process_record verify --require-instance >/dev/null 2>&1; then
    echo "Dev monomer MD worker did not stop cleanly; PID $pid is still running." >&2
    return 1
  fi
  [[ ! -e "$WORKER_SOCKET" || ( -S "$WORKER_SOCKET" && ! -L "$WORKER_SOCKET" ) ]] || {
    echo "Refusing to collect an unsafe dev worker socket residue." >&2
    return 1
  }
  rm -f -- "$WORKER_PID_FILE" "$WORKER_SOCKET"
  echo "Dev monomer MD worker is stopped."
}

worker_drain_stop() {
  if [[ ! -f "$WORKER_PID_FILE" ]]; then
    [[ ! -e "$WORKER_SOCKET" && ! -L "$WORKER_SOCKET" ]] || {
      echo "Refusing MD drain-stop because its socket lacks a process record." >&2
      return 1
    }
    return 0
  fi
  local record expected_instance response pid
  if ! record="$(worker_process_record verify --require-instance 2>/dev/null)"; then
    worker_process_record verify >/dev/null || return 1
    expected_instance="$(worker_health_validate prebind)" || return 1
    worker_process_record bind-instance --instance-id "$expected_instance" >/dev/null || return 1
    record="$(worker_process_record verify --require-instance)" || return 1
  fi
  expected_instance="$(printf '%s' "$record" | python3 -c 'import json, sys; print(json.load(sys.stdin)["worker_instance_id"])')"
  pid="$(printf '%s' "$record" | python3 -c 'import json, sys; print(json.load(sys.stdin)["pid"])')"
  response="$(curl --max-time 10 --unix-socket "$WORKER_SOCKET" -fsS -X POST http://localhost/drain)" || return 1
  printf '%s' "$response" | python3 -c '
import json, sys
value=json.load(sys.stdin)
if value.get("worker_instance_id") != sys.argv[1] or value.get("status") != "draining" or value.get("accepting_jobs") is not False:
    raise SystemExit("MD drain response differs from the fenced Worker instance")
' "$expected_instance"
  # SIGTERM targets only the already verified pidfd. The Worker lifespan owns
  # cancellation/persistence of its own jobs; no external PID is signalled.
  worker_process_record terminate --require-instance >/dev/null
  for _ in $(seq 1 40); do
    worker_process_record verify --require-instance >/dev/null 2>&1 || break
    sleep 0.25
  done
  worker_process_record verify --require-instance >/dev/null 2>&1 && {
    echo "Drained MD Worker PID $pid did not stop cleanly." >&2
    return 1
  }
  [[ ! -e "$WORKER_SOCKET" || ( -S "$WORKER_SOCKET" && ! -L "$WORKER_SOCKET" ) ]] || return 1
  rm -f -- "$WORKER_PID_FILE" "$WORKER_SOCKET"
}

dft_worker_session_record() {
  local action="$1" health="${2:-}"
  [[ "${NEXPOLY_DEV_GPU_SESSION_ID:-}" =~ ^[0-9a-f]{32}$ ]] || {
    echo "DFT Worker fencing requires the exact controller session identity." >&2
    return 1
  }
  "$GPU_SESSION_PYTHON" -I - "$action" \
    "$DFT_WORKER_SESSION_RECORD" "$ROOT_DIR/.runtime/monomer-dft-worker.pid" \
    "$NEXPOLY_DEV_GPU_SESSION_ID" "$CURRENT_SOURCE_REVISION" "$CURRENT_SOURCE_TREE" \
    "$DFT_WORKER_LOCK_SHA256" "$DFT_WORKER_VERSION" "$health" <<'PY'
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

action, raw_record, raw_pid_file, session_id, source_sha, source_tree, lock_sha, version, raw_health = sys.argv[1:]
record_path = Path(raw_record)
pid_file = Path(raw_pid_file)

def process_ticks(pid: int) -> int:
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    close = raw.rfind(")")
    fields = raw[close + 2:].split()
    if len(fields) <= 19 or not fields[19].isdigit():
        raise SystemExit("DFT Worker start identity is invalid")
    return int(fields[19])

def live_identity():
    parts = pid_file.read_text(encoding="ascii").split()
    if len(parts) != 2 or not all(item.isdigit() for item in parts):
        raise SystemExit("DFT Worker PID record is invalid")
    pid, ticks = map(int, parts)
    if process_ticks(pid) != ticks:
        raise SystemExit("DFT Worker PID was reused")
    environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    required = {
        f"NEXPOLY_DEV_GPU_SESSION_ID={session_id}".encode(),
        f"MONOMER_DFT_WORKER_VERSION={version}".encode(),
        b"NEXPOLY_DEV_GPU1_ONLY_SESSION=1",
        b"NEXPOLY_DFT_GPU_DEVICE=1",
        b"NEXPOLY_DFT_OVERFLOW_GPU_DEVICES=",
    }
    if not required <= set(environ):
        raise SystemExit("DFT Worker process environment differs from this session")
    return pid, ticks

def safe_record():
    metadata = record_path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1:
        raise SystemExit("DFT Worker session record is unsafe")
    value = json.loads(record_path.read_text(encoding="utf-8"))
    expected_keys = {"schema_version", "pid", "start_ticks", "session_id", "source_sha", "source_tree", "worker_lock_sha256", "worker_version", "worker_instance_id"}
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise SystemExit("DFT Worker session record schema differs")
    return value

health = json.loads(raw_health)
instance = health.get("worker_instance_id")
if not isinstance(instance, str) or re.fullmatch(r"[0-9a-fA-F]{32}", instance) is None or health.get("worker_version") != version:
    raise SystemExit("DFT Worker health identity differs")
pid, ticks = live_identity()
expected = {
    "schema_version": 1,
    "pid": pid,
    "start_ticks": ticks,
    "session_id": session_id,
    "source_sha": source_sha,
    "source_tree": source_tree,
    "worker_lock_sha256": lock_sha,
    "worker_version": version,
    "worker_instance_id": instance,
}

if action == "bind":
    if record_path.exists() or record_path.is_symlink():
        raise SystemExit("DFT Worker session record already exists")
    fd, temporary = tempfile.mkstemp(prefix=".monomer-dft-worker.session.", dir=record_path.parent)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n").encode())
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, record_path)
    finally:
        if fd >= 0:
            os.close(fd)
        try: os.unlink(temporary)
        except FileNotFoundError: pass
elif action == "verify":
    if safe_record() != expected:
        raise SystemExit("DFT Worker session/process/health identity changed")
else:
    raise SystemExit("unknown DFT Worker session-record action")
print(instance)
PY
}

dft_worker_ctl() {
  env \
    NEXPOLY_DEV_GPU1_ONLY_SESSION=1 \
    NEXPOLY_DEV_GPU_SESSION_ID="$NEXPOLY_DEV_GPU_SESSION_ID" \
    NEXPOLY_DFT_GPU_DEVICE=1 \
    NEXPOLY_DFT_OVERFLOW_GPU_DEVICES= \
    MONOMER_DFT_WORKER_VERSION="$DFT_WORKER_VERSION" \
    scripts/monomer_dft_worker_ctl.sh "$@"
}

dft_worker_drain_stop() {
  local socket="$DFT_WORKER_SOCKET_DIR/worker.sock" health response instance
  if [[ ! -S "$socket" ]]; then
    [[ ! -e "$DFT_WORKER_SESSION_RECORD" && ! -L "$DFT_WORKER_SESSION_RECORD" ]] && return 0
    echo "DFT Worker session record exists without its exact socket; refusing a broad stop." >&2
    return 1
  fi
  health="$(curl --max-time 10 --unix-socket "$socket" -fsS http://localhost/health)" || return 1
  if [[ ! -e "$DFT_WORKER_SESSION_RECORD" && ! -L "$DFT_WORKER_SESSION_RECORD" ]]; then
    # Recover only the narrow post-start/pre-bind window after proving the
    # live PID/start/env/health all belong to this exact controller session.
    dft_worker_session_record bind "$health" >/dev/null || return 1
  fi
  instance="$(dft_worker_session_record verify "$health")" || return 1
  response="$(curl --max-time 10 --unix-socket "$socket" -fsS -X POST http://localhost/drain)" || return 1
  printf '%s' "$response" | python3 -c '
import json, sys
value=json.load(sys.stdin)
if value.get("worker_instance_id") != sys.argv[1] or value.get("status") != "draining" or value.get("accepting_jobs") is not False:
    raise SystemExit("DFT drain response differs from the fenced Worker instance")
' "$instance"
  for _ in $(seq 1 20); do
    if curl --max-time 5 --unix-socket "$socket" -fsS http://localhost/health | python3 -c '
import json, sys
value=json.load(sys.stdin)
active=value.get("active_jobs")
if value.get("worker_instance_id") != sys.argv[1] or value.get("draining") is not True or isinstance(active,bool) or not isinstance(active,int) or active != 0:
    raise SystemExit(1)
' "$instance"; then
      dft_worker_ctl stop-if-drained-instance "$instance"
      rm -f -- "$DFT_WORKER_SESSION_RECORD"
      return 0
    fi
    sleep 0.5
  done
  echo "DFT Worker remains drained while its own calculation exits naturally." >&2
  return 1
}

worker_status() {
  local payload
  worker_verify_venv
  worker_assert_process_identity
  if ! worker_health || ! payload="$(worker_health_payload)"; then
    echo "Dev monomer MD worker is unavailable." >&2
    return 1
  fi
  printf '%s\n' "$payload"
}

wait_gpu_backend_configured() {
  local container_id health
  for _ in $(seq 1 180); do
    container_id="$("${GPU_COMPOSE[@]}" ps -q backend)"
    if [[ -n "$container_id" ]]; then
      health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$container_id")"
      [[ "$health" == "healthy" ]] && return 0
      if [[ "$(docker inspect -f '{{.State.Status}}' "$container_id")" == "exited" ]]; then
        "${GPU_COMPOSE[@]}" logs --tail=120 backend >&2
        return 1
      fi
    fi
    sleep 1
  done
  echo "Timed out waiting for the governed development backend." >&2
  "${GPU_COMPOSE[@]}" logs --tail=120 backend >&2 || true
  return 1
}

verify_gpu_backend_drift() {
  local expected_controller_status="${1:-ready}"
  local container_id expected_image actual_image desired_hash actual_hash
  local image_revision image_tree image_lock image_build_config runtime_identity
  assert_clean_candidate
  assert_default_builder
  container_id="$("${GPU_COMPOSE[@]}" ps -q backend)"
  [[ -n "$container_id" ]] || { echo "Governed development backend is missing." >&2; return 1; }
  expected_image="$(docker image inspect -f '{{.Id}}' "$DEV_BACKEND_IMAGE")"
  actual_image="$(docker inspect -f '{{.Image}}' "$container_id")"
  [[ "$actual_image" == "$expected_image" ]] || {
    echo "Governed development backend is running a stale image ID." >&2
    return 1
  }
  desired_hash="$(compute_gpu_backend_config_hash)"
  actual_hash="$(docker inspect -f '{{index .Config.Labels "com.nexpoly.dev.config-hash"}}' "$container_id")"
  [[ -n "$desired_hash" && "$actual_hash" == "$desired_hash" ]] || {
    echo "Governed development backend Compose configuration has drifted." >&2
    return 1
  }
  image_revision="$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$DEV_BACKEND_IMAGE")"
  image_tree="$(docker image inspect -f '{{index .Config.Labels "com.nexpoly.source.tree"}}' "$DEV_BACKEND_IMAGE")"
  image_lock="$(docker image inspect -f '{{index .Config.Labels "com.nexpoly.backend.dependency-lock"}}' "$DEV_BACKEND_IMAGE")"
  image_build_config="$(docker image inspect -f '{{index .Config.Labels "com.nexpoly.backend.build-config"}}' "$DEV_BACKEND_IMAGE")"
  [[ "$image_revision" == "$NEXPOLY_BUILD_REVISION" &&
    "$image_tree" == "$CURRENT_SOURCE_TREE" &&
    "$image_lock" == "$BACKEND_DEPENDENCY_LOCK_SHA256" &&
    "$image_build_config" == "$BACKEND_BUILD_CONFIG_SHA256" ]] || {
    echo "Governed backend image revision/tree/dependency/config identity has drifted." >&2
    return 1
  }
  runtime_identity="$(docker exec "$container_id" python -c \
    'import json, os; print(json.dumps([os.getenv("BUILD_REVISION"), os.getenv("BUILD_SOURCE_TREE"), os.getenv("BUILD_DEPENDENCY_LOCK_SHA256"), os.getenv("BUILD_CONFIG_SHA256")], separators=(",", ":")))')"
  [[ "$runtime_identity" == "[\"$NEXPOLY_BUILD_REVISION\",\"$CURRENT_SOURCE_TREE\",\"$BACKEND_DEPENDENCY_LOCK_SHA256\",\"$BACKEND_BUILD_CONFIG_SHA256\"]" ]] || {
    echo "Governed backend runtime build identity has drifted." >&2
    return 1
  }
  local inspect_file
  inspect_file="$(mktemp)"
  trap 'rm -f "$inspect_file"' RETURN
  docker inspect "$container_id" >"$inspect_file"
  python3 - "$ROOT_DIR" "$inspect_file" "$NEXPOLY_DEV_GPU_SESSION_ID" <<'PY'
import json
import sys

with open(sys.argv[2], encoding="utf-8") as handle:
    container = json.load(handle)[0]
requests = container["HostConfig"].get("DeviceRequests") or []
if len(requests) != 1:
    raise SystemExit("governed backend must have exactly one DeviceRequest")
request = requests[0]
if request.get("Driver") != "nvidia" or request.get("DeviceIDs") != ["1"]:
    raise SystemExit("governed backend must request physical GPU1 only")
if "gpu" not in {item for group in request.get("Capabilities", []) for item in group}:
    raise SystemExit("governed backend DeviceRequest lacks the GPU capability")
labels = container["Config"].get("Labels") or {}
expected_labels = {
    "com.nexpoly.gpu.registration": "backend-dev",
    "com.nexpoly.gpu.component": "backend",
    "com.nexpoly.gpu.environment": "dev",
    "com.nexpoly.gpu.session-id": sys.argv[3],
}
if any(labels.get(key) != value for key, value in expected_labels.items()):
    raise SystemExit("governed backend GPU registration labels differ")
root = sys.argv[1]
mounts = {item["Destination"]: item for item in container.get("Mounts", [])}
expected = {
    "/app/monomer-dft-worker": (root + "/.runtime/monomer-dft-worker-socket", False),
    "/app/.runtime/monomer-dft-download-spool": (root + "/.runtime/monomer-dft-download-spool", True),
    "/app/.runtime/gpu-resource": (root + "/.runtime/gpu-resource", False),
}
for target, (source, rw) in expected.items():
    mount = mounts.get(target)
    if not mount or mount.get("Source") != source or mount.get("RW") is not rw:
        raise SystemExit(f"governed backend mount differs: {target}")
PY
  rm -f "$inspect_file"
  trap - RETURN
  docker exec "$container_id" python -c \
    "import os; expected={'NVIDIA_VISIBLE_DEVICES':'1','MODEL_ENABLED':'true','OCSR_ENABLED':'true','OCSR_DEVICE':'cuda','GEN_MODEL_ENABLED':'true','GEN_DEVICE':'cuda','RETRO_MODEL_ENABLED':'true','RETRO_DEVICE':'cuda','POLYTAO_ENABLED':'true','POLYTAO_DEVICE':'cuda','MONOMER_MD_SUBMIT_ENABLED':'true','MONOMER_DFT_SUBMIT_ENABLED':'true','GPU_BROKER_ENABLED':'true','GPU_BROKER_SOCKET_PATH':'/app/.runtime/gpu-resource/broker.sock','GPU_MPS_PIPE_ROOT':'/app/.runtime/gpu-resource'}; actual={key:os.getenv(key) for key in expected}; assert actual == expected, actual"
  "$GPU_SESSION_PYTHON" -I "$GPU_SESSION_CONTROLLER" status | python3 -c \
    'import json, sys; value=json.load(sys.stdin); assert value.get("status") == sys.argv[3] and value.get("gpu3_untouched") is True and value.get("contaminated") is False and value.get("source_sha") == sys.argv[1] and value.get("source_tree") == sys.argv[2] and value.get("session_id") == sys.argv[4], value' \
    "$CURRENT_SOURCE_REVISION" "$CURRENT_SOURCE_TREE" "$expected_controller_status" "$NEXPOLY_DEV_GPU_SESSION_ID"
}

verify_backend_image_build_identity() {
  local image_revision image_tree image_lock image_build_config
  image_revision="$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$DEV_BACKEND_IMAGE")"
  image_tree="$(docker image inspect -f '{{index .Config.Labels "com.nexpoly.source.tree"}}' "$DEV_BACKEND_IMAGE")"
  image_lock="$(docker image inspect -f '{{index .Config.Labels "com.nexpoly.backend.dependency-lock"}}' "$DEV_BACKEND_IMAGE")"
  image_build_config="$(docker image inspect -f '{{index .Config.Labels "com.nexpoly.backend.build-config"}}' "$DEV_BACKEND_IMAGE")"
  [[ "$image_revision" == "$NEXPOLY_BUILD_REVISION" &&
    "$image_tree" == "$CURRENT_SOURCE_TREE" &&
    "$image_lock" == "$BACKEND_DEPENDENCY_LOCK_SHA256" &&
    "$image_build_config" == "$BACKEND_BUILD_CONFIG_SHA256" ]] || {
    echo "Backend image provenance differs before GPU session startup." >&2
    return 1
  }
}

write_gpu_session_activation_manifest() {
  local controller_payload run_directory container_id image_id manifest
  controller_payload="$("$GPU_SESSION_PYTHON" -I "$GPU_SESSION_CONTROLLER" status)"
  run_directory="$(printf '%s' "$controller_payload" | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_directory"])')"
  container_id="$("${GPU_COMPOSE[@]}" ps -q backend)"
  [[ "$container_id" =~ ^[0-9a-f]{64}$ ]] || return 1
  image_id="$(docker inspect -f '{{.Image}}' "$container_id")"
  manifest="$run_directory/activation-manifest.json"
  "$GPU_SESSION_PYTHON" -I - \
    "$manifest" "$WORKER_PID_FILE" "$DFT_WORKER_SESSION_RECORD" \
    "$NEXPOLY_DEV_GPU_SESSION_ID" "$CURRENT_SOURCE_REVISION" "$CURRENT_SOURCE_TREE" \
    "$container_id" "$image_id" "$NEXPOLY_DEV_CONFIG_HASH" <<'PY'
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

manifest, md_path, dft_path = map(Path, sys.argv[1:4])
session_id, source_sha, source_tree, container_id, image_id, config_hash = sys.argv[4:]

def private_json(path: Path):
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise SystemExit(f"unsafe session identity record: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("session_id") != session_id:
        raise SystemExit(f"session identity differs: {path}")
    return value

value = {
    "schema_version": 1,
    "session_id": session_id,
    "source_sha": source_sha,
    "source_tree": source_tree,
    "backend_container_id": container_id,
    "backend_image_id": image_id,
    "backend_config_hash": config_hash,
    "md_process": private_json(md_path),
    "dft_process": private_json(dft_path),
}
if manifest.exists() or manifest.is_symlink():
    raise SystemExit("activation manifest already exists")
fd, temporary = tempfile.mkstemp(prefix=".activation-manifest.", dir=manifest.parent)
try:
    os.fchmod(fd, 0o600)
    os.write(fd, (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())
    os.fsync(fd)
    os.close(fd)
    fd = -1
    os.replace(temporary, manifest)
finally:
    if fd >= 0:
        os.close(fd)
    try: os.unlink(temporary)
    except FileNotFoundError: pass
PY
}

GPU_SESSION_ROLLBACK_ARMED=false

gpu_backend_stop_exact_session() {
  local container_id label
  [[ "${NEXPOLY_DEV_GPU_SESSION_ID:-}" =~ ^[0-9a-f]{32}$ ]] || {
    echo "GPU backend stop requires an exact controller session identity." >&2
    return 1
  }
  container_id="$("${GPU_COMPOSE[@]}" ps -q backend)"
  [[ -n "$container_id" ]] || return 0
  label="$(docker inspect -f '{{index .Config.Labels "com.nexpoly.gpu.session-id"}}' "$container_id")" || return 1
  if [[ "$label" != "$NEXPOLY_DEV_GPU_SESSION_ID" ]]; then
    # An idle CPU backend or another authority is never stopped by recovery.
    return 0
  fi
  "${GPU_COMPOSE[@]}" stop backend
}

gpu_session_up_rollback() {
  local original_status=$? payload="" session_id=""
  trap - ERR
  set +e
  if [[ "$GPU_SESSION_ROLLBACK_ARMED" == "true" ]]; then
    payload="$("$GPU_SESSION_PYTHON" -I "$GPU_SESSION_CONTROLLER" status 2>/dev/null)"
    session_id="$(printf '%s' "$payload" | python3 -c 'import json, re, sys; value=json.load(sys.stdin).get("session_id", ""); print(value if isinstance(value,str) and re.fullmatch(r"[0-9a-f]{32}", value) else "")' 2>/dev/null)"
    [[ -z "$session_id" ]] || export NEXPOLY_DEV_GPU_SESSION_ID="$session_id"
    "$GPU_SESSION_PYTHON" -I "$GPU_SESSION_CONTROLLER" drain --execute >/dev/null 2>&1
    if [[ -n "$session_id" ]]; then
      NEXPOLY_DEV_GPU_SESSION_INTERNAL_RECOVERY=1 gpu_session_stop_owned_internal
      NEXPOLY_DEV_GPU_SESSION_INTERNAL_RECOVERY=1 gpu_session_restore_cpu_internal
    fi
    "$GPU_SESSION_PYTHON" -I "$GPU_SESSION_CONTROLLER" down --execute >/dev/null 2>&1
  fi
  set -e
  return "$original_status"
}

gpu_session_up() {
  if [[ "${NEXPOLY_DEV_GPU_SESSION_EXECUTE:-0}" != "1" ]]; then
    "$GPU_SESSION_PYTHON" -I "$GPU_SESSION_CONTROLLER" up --dry-run
    return 0
  fi
  validate_asset_release
  prepare_canary_state_directory
  prepare_worker_runtime_directories
  prepare_dft_runtime_directories
  worker_verify_venv
  validate_worker_transport_runtime
  validate_dft_session_prerequisites
  build_backend_image
  verify_backend_image_build_identity
  # Replace a stale dev GPU DeviceRequest with the verified idle CPU service
  # before the controller's first free audit.
  NEXPOLY_DEV_CONFIG_HASH="$(compute_backend_config_hash)"
  export NEXPOLY_DEV_CONFIG_HASH
  "${COMPOSE[@]}" up -d --no-deps --force-recreate backend
  wait_backend_configured
  verify_backend_drift
  GPU_SESSION_ROLLBACK_ARMED=true
  trap gpu_session_up_rollback ERR
  local controller_payload dft_health
  controller_payload="$("$GPU_SESSION_PYTHON" -I "$GPU_SESSION_CONTROLLER" up --execute)"
  NEXPOLY_DEV_GPU_SESSION_ID="$(printf '%s' "$controller_payload" | python3 -c 'import json, re, sys; value=json.load(sys.stdin); session=value.get("session_id"); assert value.get("status") == "plane-ready" and isinstance(session,str) and re.fullmatch(r"[0-9a-f]{32}", session), value; print(session)')"
  export NEXPOLY_DEV_GPU_SESSION_ID
  export NEXPOLY_DEV_GPU_SESSION_ACTIVE=true
  dft_worker_ctl start
  dft_health="$(curl --max-time 10 --unix-socket "$DFT_WORKER_SOCKET_DIR/worker.sock" -fsS http://localhost/health)"
  dft_worker_session_record bind "$dft_health" >/dev/null
  "$GPU_SESSION_PYTHON" -I "$GPU_SESSION_CONTROLLER" stabilize --execute \
    --session-id "$NEXPOLY_DEV_GPU_SESSION_ID" >/dev/null
  worker_up
  NEXPOLY_DEV_CONFIG_HASH="$(compute_gpu_backend_config_hash)"
  export NEXPOLY_DEV_CONFIG_HASH
  "${GPU_COMPOSE[@]}" up -d --no-deps --force-recreate backend
  wait_gpu_backend_configured
  verify_gpu_backend_drift plane-ready
  write_gpu_session_activation_manifest
  "$GPU_SESSION_PYTHON" -I "$GPU_SESSION_CONTROLLER" activate --execute \
    --session-id "$NEXPOLY_DEV_GPU_SESSION_ID" >/dev/null
  verify_gpu_backend_drift ready
  GPU_SESSION_ROLLBACK_ARMED=false
  trap - ERR
  echo "Development GPU1 session is ready; GPU3 was not modified."
}

verify_gpu_session_stopped_runtime() {
  verify_backend_drift
  local path
  for path in \
    "$ROOT_DIR/.runtime/gpu-session/controller.json" \
    "$ROOT_DIR/.runtime/gpu-resource/broker.sock" \
    "$ROOT_DIR/.runtime/gpu-resource/mps-1" \
    "$WORKER_PID_FILE" \
    "$WORKER_SOCKET" \
    "$DFT_WORKER_SESSION_RECORD" \
    "$ROOT_DIR/.runtime/monomer-dft-worker.pid" \
    "$DFT_WORKER_SOCKET_DIR/worker.sock"; do
    if [[ -e "$path" || -L "$path" ]]; then
      echo "Stopped GPU session retains an owned runtime path: $path" >&2
      return 1
    fi
  done
}

gpu_session_status() {
  local payload state
  payload="$("$GPU_SESSION_PYTHON" -I "$GPU_SESSION_CONTROLLER" status)"
  printf '%s\n' "$payload"
  printf '%s' "$payload" | python3 -c \
    'import json, sys; value=json.load(sys.stdin); status=value.get("status"); assert status == "stopped" or (value.get("source_sha") == sys.argv[1] and value.get("source_tree") == sys.argv[2]), value' \
    "$CURRENT_SOURCE_REVISION" "$CURRENT_SOURCE_TREE"
  state="$(printf '%s' "$payload" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("status", "invalid"))')"
  if [[ "$state" == "ready" ]]; then
    NEXPOLY_DEV_GPU_SESSION_ID="$(printf '%s' "$payload" | python3 -c 'import json, re, sys; value=json.load(sys.stdin).get("session_id"); assert isinstance(value,str) and re.fullmatch(r"[0-9a-f]{32}", value); print(value)')"
    export NEXPOLY_DEV_GPU_SESSION_ID
    verify_gpu_backend_drift
    worker_status >/dev/null
    local dft_health
    dft_health="$(curl --max-time 10 --unix-socket "$DFT_WORKER_SOCKET_DIR/worker.sock" -fsS http://localhost/health)"
    dft_worker_session_record verify "$dft_health" >/dev/null
  elif [[ "$state" == "stopped" ]]; then
    verify_gpu_session_stopped_runtime
  elif [[ "$state" != "stopped" ]]; then
    return 1
  fi
}

gpu_session_down() {
  if [[ "${NEXPOLY_DEV_GPU_SESSION_EXECUTE:-0}" != "1" ]]; then
    "$GPU_SESSION_PYTHON" -I "$GPU_SESSION_CONTROLLER" down --dry-run
    return 0
  fi
  local payload state
  payload="$("$GPU_SESSION_PYTHON" -I "$GPU_SESSION_CONTROLLER" status)"
  state="$(printf '%s' "$payload" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("status", "invalid"))')"
  if [[ "$state" == "stopped" ]]; then
    verify_gpu_session_stopped_runtime
    echo "Development backend is already in CPU-only idle mode."
    return 0
  fi
  NEXPOLY_DEV_GPU_SESSION_ID="$(printf '%s' "$payload" | python3 -c 'import json, re, sys; value=json.load(sys.stdin); session=value.get("session_id"); assert value.get("status") in {"ready","plane-ready","stabilizing","contaminated","audit-failed","isolation-waiting","cleanup-blocked"} and isinstance(session,str) and re.fullmatch(r"[0-9a-f]{32}", session), value; print(session)')"
  export NEXPOLY_DEV_GPU_SESSION_ID
  "$GPU_SESSION_PYTHON" -I "$GPU_SESSION_CONTROLLER" drain --execute >/dev/null
  gpu_backend_stop_exact_session
  worker_drain_stop
  dft_worker_drain_stop
  NEXPOLY_DEV_CONFIG_HASH="$(compute_backend_config_hash)"
  export NEXPOLY_DEV_CONFIG_HASH
  "${COMPOSE[@]}" up -d --no-deps --force-recreate backend
  wait_backend_configured
  verify_backend_drift
  "$GPU_SESSION_PYTHON" -I "$GPU_SESSION_CONTROLLER" down --execute
  echo "Development backend restored to CPU-only idle mode."
}

gpu_session_stop_owned_internal() {
  [[ "${NEXPOLY_DEV_GPU_SESSION_INTERNAL_RECOVERY:-0}" == "1" ]] || {
    echo "Internal GPU recovery command is controller-only." >&2
    return 1
  }
  local failed=0
  gpu_backend_stop_exact_session || failed=1
  worker_drain_stop || failed=1
  dft_worker_drain_stop || failed=1
  return "$failed"
}

gpu_session_restore_cpu_internal() {
  [[ "${NEXPOLY_DEV_GPU_SESSION_INTERNAL_RECOVERY:-0}" == "1" ]] || {
    echo "Internal GPU recovery command is controller-only." >&2
    return 1
  }
  prepare_dft_runtime_directories
  NEXPOLY_DEV_CONFIG_HASH="$(compute_backend_config_hash)"
  export NEXPOLY_DEV_CONFIG_HASH
  "${COMPOSE[@]}" up -d --no-deps --force-recreate backend
  wait_backend_configured
  verify_backend_drift
}

test_backend() (
  cleanup_backend_test_postgres() {
    "${COMPOSE[@]}" --profile test rm -sf backend-test-postgres >/dev/null 2>&1 || true
  }
  trap cleanup_backend_test_postgres EXIT
  "${COMPOSE[@]}" --profile test up -d backend-test-postgres
  "${COMPOSE[@]}" --profile test build --builder default backend-test
  "${COMPOSE[@]}" --profile test run --rm --no-deps backend-test \
    python -m pytest /app/backend/tests
)

smoke_static() {
  local path="$1"
  local expected_type="$2"
  local min_bytes="$3"
  local metrics content_type size
  metrics="$(curl --max-time 60 -fsS -o /dev/null -w '%{content_type} %{size_download}' "$FRONTEND_URL$path")"
  content_type="${metrics%% *}"
  size="${metrics##* }"
  if [[ "$content_type" != "$expected_type"* ]]; then
    echo "Unexpected content type for $path: $content_type" >&2
    return 1
  fi
  if (( ${size%.*} < min_bytes )); then
    echo "Static asset is too small for $path: $size bytes" >&2
    return 1
  fi
}

smoke() {
  local endpoint session_payload session_state gpu_preflight_mode
  for endpoint in \
    /health \
    /api/v1/database-browser/datasets/summary \
    /api/v1/conditional-generation/tg/status \
    /api/v1/conditional-generation/polytao/status \
    /api/v1/monomer-polymerization/status \
    /api/v1/monomer-md/status \
    /api/v1/online-knowledge/default-config; do
    curl --max-time 60 -fsS -o /dev/null "$FRONTEND_URL$endpoint"
  done
  curl --max-time 30 -fsS "$FRONTEND_URL/ketcher/index.html" | grep -Fq '<title>Ketcher v3.7.0</title>'
  smoke_static /ketcher/static/js/main.8617f334.js text/javascript 1000000
  smoke_static /ketcher/static/css/main.748bd42d.css text/css 100000
  smoke_static /vendor/3Dmol-min.js text/javascript 500000
  session_payload="$($GPU_SESSION_PYTHON -I "$GPU_SESSION_CONTROLLER" status)"
  session_state="$(printf '%s' "$session_payload" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("status", "invalid"))')"
  case "$session_state" in
    stopped)
      verify_backend_drift
      gpu_preflight_mode=disabled
      ;;
    ready)
      gpu_session_status >/dev/null
      gpu_preflight_mode=configured
      ;;
    *)
      echo "Development smoke refuses controller state: $session_state" >&2
      return 1
      ;;
  esac
  local container_id
  container_id="$("${COMPOSE[@]}" ps -q backend)"
  docker exec "$container_id" python -m app.gpu_preflight --mode "$gpu_preflight_mode" --verify-serialized-assets >/tmp/nexpoly-dev-gpu-preflight.json

  python3 - "$FRONTEND_URL" <<'PY'
import json
import sys
from urllib.request import Request, urlopen

base = sys.argv[1]


def post(path, payload, timeout=60):
    request = Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


exact = post(
    "/api/v1/query/smiles",
    {
        "smiles": "**C1=C(O)C(=N*)CC=C1*",
        "match_mode": "structure",
        "similarity_threshold": 1,
        "top_k": 1,
    },
)
if exact.get("total") != 1 or exact["results"][0].get("similarity_score") != 1:
    raise SystemExit("exact structure-query smoke did not honor threshold/top_k")

smipoly = post(
    "/api/v1/monomer-polymerization",
    {
        "monomer_a_smiles": "Nc1ccc(N)cc1",
        "monomer_b_smiles": "O=C1OC(=O)c2cc3c(cc21)C(=O)OC3=O",
        "target_class": "polyimide",
        "max_results": 3,
    },
)
if smipoly.get("total", 0) < 1 or not smipoly.get("results"):
    raise SystemExit("SMiPoly default fixture produced no candidates")
PY

  if [[ "$session_state" == "ready" ]]; then
    local ocsr_result
    ocsr_result="$(mktemp)"
    trap 'rm -f "$ocsr_result"' RETURN
    curl --max-time 300 -fsS \
      -F "image=@$ROOT_DIR/docs/assets/demo-upload-structure.png;type=image/png" \
      "$FRONTEND_URL/api/v1/structure/recognize-image" >"$ocsr_result"
    python3 - "$ocsr_result" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    result = json.load(handle)
if not result.get("smiles"):
    raise SystemExit("OCSR smoke completed without a recognized SMILES")
PY
    rm -f "$ocsr_result"
    trap - RETURN

    python3 - "$FRONTEND_URL" "$BACKEND_URL" <<'PY'
import json
import sys
import time
from urllib.request import Request, urlopen

base = sys.argv[1]
backend_base = sys.argv[2]


def post_json(path, payload, timeout=300):
    request = Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def get_json(path, timeout=30):
    request = Request(base + path, headers={"Cache-Control": "no-store"})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


conditional_job = post_json(
    "/api/v1/conditional-generation/tg/jobs",
    {
        "smiles": "*CC*",
        "delta_tg": 30,
        "candidate_count": 1,
        "top_k": 5,
        "temperature": 1.0,
    },
)
for _ in range(300):
    conditional_state = get_json(
        f"/api/v1/conditional-generation/tg/jobs/{conditional_job['job_id']}"
    )
    if conditional_state["status"] == "completed":
        if not conditional_state.get("result"):
            raise SystemExit("Conditional generation completed without a result")
        break
    if conditional_state["status"] in {"failed", "cancelled"}:
        raise SystemExit(
            "Conditional generation smoke failed: "
            + str(conditional_state.get("error") or conditional_state["status"])
        )
    time.sleep(1)
else:
    raise SystemExit("Timed out waiting for conditional generation smoke job")

retro = post_json(
    "/api/v1/monomer-retrosynthesis",
    {
        "smiles": "Nc1ccc(N)cc1",
        "target_role": "auto",
        "num_beams": 1,
        "num_return_sequences": 1,
        "max_new_tokens": 32,
    },
)
if not retro.get("canonical_smiles") or not retro.get("device"):
    raise SystemExit("Retrosynthesis smoke returned an incomplete response")

payload = {
    "descriptors": {
        "MolWt": 264, "HeavyAtomCount": 19, "NHOHCount": 0, "NOCount": 4,
        "NumAliphaticCarbocycles": 1, "NumAliphaticHeterocycles": 0,
        "NumAliphaticRings": 1, "NumAromaticCarbocycles": 0,
        "NumAromaticHeterocycles": 0, "NumAromaticRings": 0,
        "NumHAcceptors": 4, "NumHDonors": 0, "NumHeteroatoms": 6,
        "NumRotatableBonds": 5, "RingCount": 1,
    },
    "input_smiles": None, "candidate_count": 1, "temperature": 1.0,
    "top_k": 100, "top_p": 0.999, "max_length": 300,
}
request = Request(
    base + "/api/v1/conditional-generation/polytao/jobs",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urlopen(request, timeout=30) as response:
    job = json.load(response)
job_id = job["job_id"]
for _ in range(180):
    with urlopen(base + f"/api/v1/conditional-generation/polytao/jobs/{job_id}", timeout=10) as response:
        state = json.load(response)
    if state["status"] == "completed":
        if not state.get("result") or not state["result"].get("results"):
            raise SystemExit("PolyTAO completed without candidates")
        candidate = state["result"]["results"][0]
        svg = candidate.get("structure_svg")
        if (
            not candidate.get("generated_smiles")
            or not isinstance(svg, str)
            or "<svg" not in svg[:512]
            or not svg.rstrip().endswith("</svg>")
        ):
            raise SystemExit("PolyTAO completed without a candidate SMILES/SVG")
        break
    if state["status"] in {"failed", "cancelled"}:
        raise SystemExit(f"PolyTAO smoke failed: {state.get('error_message') or state['status']}")
    time.sleep(1)
else:
    raise SystemExit("Timed out waiting for PolyTAO smoke job")

with urlopen(backend_base + "/internal/gpu/status", timeout=30) as response:
    gpu_status = json.load(response)
not_ready = [
    name
    for name, state in gpu_status.get("models", {}).items()
    if state.get("enabled") and not state.get("ready")
]
if not_ready:
    raise SystemExit("GPU runtimes are not ready after smoke: " + ", ".join(not_ready))
PY
    gpu_session_status >/dev/null
  else
    verify_backend_drift
  fi
  cleanup_legacy_builder
  echo "Dev single-entry API and static-resource smoke checks passed at $FRONTEND_URL"
}

NEXPOLY_DEV_CONFIG_HASH="$(compute_backend_config_hash)"
export NEXPOLY_DEV_CONFIG_HASH

case "${1:-up}" in
  up)
    "$GPU_SESSION_PYTHON" -I "$GPU_SESSION_CONTROLLER" status | python3 -c \
      'import json, sys; value=json.load(sys.stdin); assert value.get("status") == "stopped", "use gpu-session-down before ordinary up"'
    validate_asset_release
    prepare_canary_state_directory
    prepare_worker_runtime_directories
    prepare_dft_runtime_directories
    build_backend_image
    "${COMPOSE[@]}" up -d lab-postgres
    run_dev_migrations
    "${COMPOSE[@]}" up -d --no-deps --force-recreate backend
    wait_backend_configured
    verify_backend_drift
    "${COMPOSE[@]}" up -d --no-deps frontend-dev
    ;;
  stop)
    "$GPU_SESSION_PYTHON" -I "$GPU_SESSION_CONTROLLER" status | python3 -c \
      'import json, sys; value=json.load(sys.stdin); assert value.get("status") == "stopped", "use gpu-session-down for an active GPU session"'
    "${COMPOSE[@]}" stop backend frontend-dev
    worker_stop
    "${COMPOSE[@]}" stop lab-postgres
    ;;
  down)
    "$GPU_SESSION_PYTHON" -I "$GPU_SESSION_CONTROLLER" status | python3 -c \
      'import json, sys; value=json.load(sys.stdin); assert value.get("status") == "stopped", "use gpu-session-down for an active GPU session"'
    "${COMPOSE[@]}" stop backend frontend-dev
    worker_stop
    "${COMPOSE[@]}" down
    ;;
  ps)
    "${COMPOSE[@]}" ps
    ;;
  logs)
    "${COMPOSE[@]}" logs -f "${@:2}"
    ;;
  preflight)
    "${COMPOSE[@]}" exec -T backend python -m app.postgres_preflight --mode runtime --strict
    preflight_session_payload="$($GPU_SESSION_PYTHON -I "$GPU_SESSION_CONTROLLER" status)"
    preflight_session_state="$(printf '%s' "$preflight_session_payload" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("status", "invalid"))')"
    if [[ "$preflight_session_state" == "stopped" ]]; then
      "${COMPOSE[@]}" exec -T backend python -m app.gpu_preflight --mode disabled --verify-serialized-assets
      verify_gpu_session_stopped_runtime
    elif [[ "$preflight_session_state" == "ready" ]]; then
      "${COMPOSE[@]}" exec -T backend python -m app.gpu_preflight --mode configured --verify-serialized-assets
      gpu_session_status >/dev/null
    else
      echo "Development preflight refuses controller state: $preflight_session_state" >&2
      exit 1
    fi
    ;;
  refresh-data)
    validate_asset_release
    "${COMPOSE[@]}" up -d lab-postgres
    run_dev_migrations
    "${COMPOSE[@]}" run --rm postgres-init python -m app.import_postgres --dataset all --refresh-analytics-snapshot
    ;;
  smoke)
    smoke
    ;;
  worker-up)
    worker_up
    ;;
  worker-stop)
    worker_stop
    ;;
  worker-status)
    worker_status
    ;;
  worker-base-identity)
    worker_base_identity
    ;;
  worker-venv)
    worker_prepare_venv
    ;;
  gpu-session-up)
    gpu_session_up
    ;;
  gpu-session-status)
    gpu_session_status
    ;;
  gpu-session-down)
    gpu_session_down
    ;;
  gpu-session-stop-owned-internal)
    gpu_session_stop_owned_internal
    ;;
  gpu-session-restore-cpu-internal)
    gpu_session_restore_cpu_internal
    ;;
  test-backend)
    test_backend
    ;;
  build-frontend)
    "${COMPOSE[@]}" exec -T frontend-dev npm run build
    ;;
  check-frontend)
    "${COMPOSE[@]}" exec -T frontend-dev npm test
    "${COMPOSE[@]}" exec -T frontend-dev npm run build
    ;;
  cleanup-legacy-builder)
    cleanup_legacy_builder
    ;;
  contract-migrate)
    validate_asset_release
    build_backend_image
    run_dev_contract_migration
    ;;
  tunnel)
    : "${NEXPOLY_DEV_SSH_HOST:?Set NEXPOLY_DEV_SSH_HOST for tunnel output}"
    echo "ssh -N -L ${NEXPOLY_DEV_FRONTEND_PORT:-15173}:127.0.0.1:${NEXPOLY_DEV_FRONTEND_PORT:-15173} ${NEXPOLY_DEV_SSH_USER:-$USER}@$NEXPOLY_DEV_SSH_HOST"
    ;;
  *)
    echo "usage: $0 {up|stop|down|ps|logs|preflight|refresh-data|contract-migrate|smoke|worker-base-identity|worker-venv|worker-up|worker-stop|worker-status|gpu-session-up|gpu-session-status|gpu-session-down|test-backend|build-frontend|check-frontend|cleanup-legacy-builder|tunnel}" >&2
    exit 2
    ;;
esac

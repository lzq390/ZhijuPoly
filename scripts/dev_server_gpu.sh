#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

set -a
# shellcheck disable=SC1091
source .env.dev
set +a

: "${NEXPOLY_ASSET_ROOT:?Set NEXPOLY_ASSET_ROOT to a pinned immutable asset release}"
CURRENT_SOURCE_REVISION="$(git rev-parse --verify HEAD)"
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

COMPOSE=(docker compose -p nexpoly_dev -f docker-compose.yml -f docker-compose.dev.yml --env-file .env.dev)
DEV_BACKEND_IMAGE="nexpoly-dev-backend:latest"
DEV_PYPI_INDEX_URL="${NEXPOLY_DEV_PYPI_INDEX_URL:-https://pypi.org/simple}"
DEV_PYPI_MIRROR_URL="${NEXPOLY_DEV_PYPI_MIRROR_URL:-https://mirrors.ustc.edu.cn/pypi/simple}"
FRONTEND_URL="http://127.0.0.1:${NEXPOLY_DEV_FRONTEND_PORT:-15173}"
BACKEND_URL="http://127.0.0.1:${NEXPOLY_DEV_BACKEND_PORT:-18000}"
WORKER_ENABLED="${MONOMER_MD_DEV_WORKER_ENABLED:-true}"
WORKER_SOCKET_DIR="${MONOMER_MD_DEV_WORKER_SOCKET_DIR:-$ROOT_DIR/.runtime/monomer-md-worker-socket}"
WORKER_JOB_ROOT="${MONOMER_MD_DEV_WORKER_JOB_ROOT:-$ROOT_DIR/.runtime/monomer-md-worker-runs}"
WORKER_PYTHON="${MONOMER_MD_DEV_WORKER_PYTHON:-$ROOT_DIR/.venv-monomer-md-worker/bin/python}"
WORKER_VENV_ROOT="$ROOT_DIR/.venv-monomer-md-worker"
WORKER_LOCK="$ROOT_DIR/workers/monomer_md_worker/requirements.lock"
WORKER_BASE_PYTHON="${MONOMER_MD_DEV_WORKER_BASE_PYTHON:-}"
WORKER_BASE_IDENTITY="${MONOMER_MD_DEV_WORKER_BASE_PYTHON_IDENTITY_SHA256:-}"
WORKER_WHEELHOUSE="${MONOMER_MD_DEV_WORKER_WHEELHOUSE:-}"
[[ "$WORKER_SOCKET_DIR" == /* ]] || WORKER_SOCKET_DIR="$ROOT_DIR/${WORKER_SOCKET_DIR#./}"
[[ "$WORKER_JOB_ROOT" == /* ]] || WORKER_JOB_ROOT="$ROOT_DIR/${WORKER_JOB_ROOT#./}"
[[ "$WORKER_PYTHON" == /* ]] || WORKER_PYTHON="$ROOT_DIR/${WORKER_PYTHON#./}"
WORKER_SOCKET="$WORKER_SOCKET_DIR/worker.sock"
WORKER_PID_FILE="$WORKER_JOB_ROOT/worker.pid"
WORKER_LOG_FILE="$WORKER_JOB_ROOT/worker.log"
: "${BYTEFF2_ROOT:?Set BYTEFF2_ROOT to the byteff2 tree in the pinned asset release}"

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
if not isinstance(manifest, dict) or set(manifest) != {
    "schema_version", "byteff2_commit", "byteff2_submodules", "assets"
} or manifest.get("schema_version") != 1:
    fail("ASSET-MANIFEST.json must use schema_version 1")
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
  assert_default_builder
  "${COMPOSE[@]}" build \
    --builder default \
    --build-arg SOURCE_REVISION="$NEXPOLY_BUILD_REVISION" \
    --build-arg PYPI_INDEX_URL="$DEV_PYPI_INDEX_URL" \
    --build-arg PYPI_MIRROR_URL="$DEV_PYPI_MIRROR_URL" \
    backend
  docker image inspect "$DEV_BACKEND_IMAGE" >/dev/null
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
            "schema_compatibility_floor": "0012_drop_polytao_jobs",
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
    python -m app.postgres_migrations --mode contract
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
  echo "Timed out waiting for configured backend GPU preflight." >&2
  "${COMPOSE[@]}" logs --tail=120 backend >&2 || true
  return 1
}

verify_backend_drift() {
  local container_id expected_image actual_image desired_hash actual_hash image_revision runtime_revision
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
  [[ "$image_revision" == "$NEXPOLY_BUILD_REVISION" ]] || {
    echo "Development backend image revision does not match the requested source revision." >&2
    return 1
  }
  [[ "$runtime_revision" == "$NEXPOLY_BUILD_REVISION" ]] || {
    echo "Development backend runtime revision does not match the requested source revision." >&2
    return 1
  }
  docker exec "$container_id" python -c \
    "import os; expected={'WEB_CONCURRENCY':'1','GPU_PRELOAD_MODE':'lazy','GPU_MAX_CONCURRENT_INFERENCES':'1','GPU_MAX_WAITING_INFERENCES':'8','GPU_SYNC_QUEUE_TIMEOUT_SECONDS':'30','GPU_ASYNC_QUEUE_TIMEOUT_SECONDS':'600','OCSR_ENABLED':'true','GEN_MODEL_ENABLED':'true','GEN_JOB_WORKERS':'1','GEN_MAX_ACTIVE_JOBS':'8','RETRO_MODEL_ENABLED':'true','POLYTAO_ENABLED':'true','POLYTAO_JOB_THREADS':'1','POLYTAO_MAX_ACTIVE_JOBS':'1'}; actual={key:os.getenv(key) for key in expected}; assert actual == expected, actual"
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

worker_is_running() {
  [[ -f "$WORKER_PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$WORKER_PID_FILE")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

worker_managed_python() {
  worker_is_running || return 1
  local pid argv0=""
  pid="$(cat "$WORKER_PID_FILE")"
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  IFS= read -r -d '' argv0 < "/proc/$pid/cmdline" || true
  [[ -n "$argv0" ]] || return 1
  printf '%s\n' "$argv0"
}

worker_assert_process_identity() {
  local actual_python
  actual_python="$(worker_managed_python)" || {
    echo "Dev monomer MD worker process identity cannot be verified." >&2
    return 1
  }
  [[ "$actual_python" == "$WORKER_PYTHON" ]] || {
    echo "Dev monomer MD worker uses $actual_python instead of $WORKER_PYTHON; stop it safely before switching." >&2
    return 1
  }
}

worker_health_payload() {
  [[ -S "$WORKER_SOCKET" ]] || return 1
  curl --max-time 30 --unix-socket "$WORKER_SOCKET" -fsS http://localhost/health
}

worker_health() {
  local payload
  payload="$(worker_health_payload)"
  grep -Eq '"status":"ok".*"runtime_ready":true' <<<"$payload"
}

worker_active_jobs() {
  local payload
  payload="$(worker_health_payload)"
  printf '%s' "$payload" | "$WORKER_PYTHON" -c \
    'import json, sys; value = json.load(sys.stdin).get("active_jobs"); sys.exit(2) if isinstance(value, bool) or not isinstance(value, int) or value < 0 else None; print(value)'
}

worker_up() {
  if [[ "$WORKER_ENABLED" != "true" ]]; then
    echo "Dev monomer MD worker is disabled by MONOMER_MD_DEV_WORKER_ENABLED=$WORKER_ENABLED"
    return 0
  fi
  validate_asset_release
  worker_verify_venv
  mkdir -p "$WORKER_SOCKET_DIR" "$WORKER_JOB_ROOT"
  if worker_health; then
    if worker_is_running; then
      worker_assert_process_identity
      echo "Dev monomer MD worker is already healthy."
      return 0
    fi
    echo "Dev monomer MD worker is healthy but has no managed PID; refusing to adopt it." >&2
    return 1
  fi
  if worker_is_running; then
    echo "Dev monomer MD worker is running but unhealthy; inspect $WORKER_LOG_FILE" >&2
    return 1
  fi
  if [[ -S "$WORKER_SOCKET" && ! -f "$WORKER_PID_FILE" ]]; then
    echo "Dev monomer MD socket exists without a managed PID; inspect it before restarting." >&2
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

  rm -f "$WORKER_PID_FILE" "$WORKER_SOCKET"
  (
    cd "$ROOT_DIR/workers/monomer_md_worker"
    export APP_POSTGRES_DSN="postgresql://nexpoly_dev:nexpoly_dev@127.0.0.1:${NEXPOLY_DEV_POSTGRES_PORT:-15532}/nexpoly_dev"
    export BYTEFF2_PYTHON="$WORKER_PYTHON"
    export BYTEFF2_ROOT
    export MONOMER_MD_CUDA_VISIBLE_DEVICES="${NEXPOLY_DEV_GPU_DEVICE:-1}"
    export MONOMER_MD_DEFAULT_STEPS=300
    export MONOMER_MD_HEALTH_PROBE_TIMEOUT_SECONDS=30
    export MONOMER_MD_JOB_ROOT="$WORKER_JOB_ROOT"
    export MONOMER_MD_MAX_ACTIVE_JOBS=1
    export MONOMER_MD_MAX_CONCURRENT_JOBS=1
    export MONOMER_MD_MAX_STEPS=300
    export MONOMER_MD_PYTHON="$WORKER_PYTHON"
    export MONOMER_MD_REPORT_INTERVAL=10
    export MONOMER_MD_WORKER_ID=monomer-md-dev-worker
    export MONOMER_MD_WORKER_MODE=real
    export MONOMER_MD_WORKER_UDS="$WORKER_SOCKET"
    export NEXPOLY_GPU_DEVICE="${NEXPOLY_DEV_GPU_DEVICE:-1}"
    export PATH="$(dirname "$WORKER_PYTHON"):$PATH"
    export PYTHONPATH="$BYTEFF2_ROOT:$BYTEFF2_ROOT/submodules/bytemol${PYTHONPATH:+:$PYTHONPATH}"
    exec nohup "$WORKER_PYTHON" -m uvicorn app.main:app --uds "$WORKER_SOCKET"
  ) >>"$WORKER_LOG_FILE" 2>&1 < /dev/null &
  echo "$!" > "$WORKER_PID_FILE"

  for _ in $(seq 1 45); do
    if worker_health; then
      worker_assert_process_identity
      echo "Dev monomer MD worker is healthy on $WORKER_SOCKET"
      return 0
    fi
    if ! worker_is_running; then
      echo "Dev monomer MD worker exited during startup." >&2
      tail -n 40 "$WORKER_LOG_FILE" >&2 || true
      return 1
    fi
    sleep 1
  done
  echo "Timed out waiting for the dev monomer MD worker." >&2
  tail -n 40 "$WORKER_LOG_FILE" >&2 || true
  return 1
}

worker_stop() {
  if [[ ! -f "$WORKER_PID_FILE" ]]; then
    if [[ -S "$WORKER_SOCKET" ]]; then
      echo "Refusing to remove the dev worker socket because the managed PID file is missing." >&2
      return 1
    fi
    echo "Dev monomer MD worker is already stopped."
    return 0
  fi

  local pid
  pid="$(cat "$WORKER_PID_FILE")"
  if [[ ! "$pid" =~ ^[0-9]+$ ]]; then
    echo "Refusing to stop dev worker because the PID file is invalid." >&2
    return 1
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$WORKER_PID_FILE" "$WORKER_SOCKET"
    echo "Removed stale dev monomer MD worker metadata."
    return 0
  fi

  local command_line
  command_line="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
  if [[ "$command_line" != *"$WORKER_SOCKET"* ]]; then
    echo "Refusing to stop PID $pid because it does not own the dev worker socket." >&2
    return 1
  fi

  local active_jobs
  if ! active_jobs="$(worker_active_jobs)"; then
    echo "Refusing to stop dev worker because active job state could not be verified." >&2
    return 1
  fi
  if (( active_jobs > 0 )); then
    echo "Refusing to stop dev worker while $active_jobs job(s) are active." >&2
    return 1
  fi

  kill "$pid"
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "Dev monomer MD worker did not stop cleanly; PID $pid is still running." >&2
    return 1
  fi
  rm -f "$WORKER_PID_FILE" "$WORKER_SOCKET"
  echo "Dev monomer MD worker is stopped."
}

worker_status() {
  local payload
  worker_verify_venv
  worker_assert_process_identity
  if ! payload="$(worker_health_payload)"; then
    echo "Dev monomer MD worker is unavailable." >&2
    return 1
  fi
  printf '%s\n' "$payload"
}

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
  local endpoint
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
  local container_id
  container_id="$("${COMPOSE[@]}" ps -q backend)"
  docker exec "$container_id" python -m app.gpu_preflight --mode configured --verify-serialized-assets >/tmp/nexpoly-dev-gpu-preflight.json

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
  verify_backend_drift
  cleanup_legacy_builder
  echo "Dev single-entry API and static-resource smoke checks passed at $FRONTEND_URL"
}

NEXPOLY_DEV_CONFIG_HASH="$(compute_backend_config_hash)"
export NEXPOLY_DEV_CONFIG_HASH

case "${1:-up}" in
  up)
    validate_asset_release
    build_backend_image
    "${COMPOSE[@]}" up -d lab-postgres
    run_dev_migrations
    worker_up
    "${COMPOSE[@]}" up -d --no-deps --force-recreate backend
    wait_backend_configured
    verify_backend_drift
    "${COMPOSE[@]}" up -d --no-deps frontend-dev
    ;;
  stop)
    "${COMPOSE[@]}" stop backend frontend-dev
    worker_stop
    "${COMPOSE[@]}" stop lab-postgres
    ;;
  down)
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
    "${COMPOSE[@]}" exec -T backend python -m app.gpu_preflight --mode configured --verify-serialized-assets
    verify_backend_drift
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
  test-backend)
    "${COMPOSE[@]}" exec -T backend python -m pytest \
      tests/test_conditional_generation.py \
      tests/test_monomer_retrosynthesis.py \
      tests/test_monomer_md.py \
      tests/test_polytao.py \
      tests/test_postgres_governance.py \
      tests/test_gpu_runtime_registry.py \
      tests/test_gpu_preflight.py \
      tests/test_deployment_control.py \
      tests/test_job_manager_reliability.py \
      tests/test_in_memory_jobs.py \
      tests/test_migration_policy.py
    ;;
  build-frontend)
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
    echo "usage: $0 {up|stop|down|ps|logs|preflight|refresh-data|contract-migrate|smoke|worker-base-identity|worker-venv|worker-up|worker-stop|worker-status|test-backend|build-frontend|cleanup-legacy-builder|tunnel}" >&2
    exit 2
    ;;
esac

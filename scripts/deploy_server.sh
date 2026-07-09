#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

log() {
  printf '[nexpoly-deploy] %s\n' "$*"
}

die() {
  printf '[nexpoly-deploy] ERROR: %s\n' "$*" >&2
  exit 1
}

trim_value() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

read_dotenv_value() {
  local key="$1"
  local line=""
  local name=""
  local value=""
  local first=""
  local last=""

  [[ -f "$ROOT_DIR/.env" ]] || return 1

  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" == *=* ]] || continue
    name="$(trim_value "${line%%=*}")"
    [[ "$name" == "$key" ]] || continue

    value="$(trim_value "${line#*=}")"
    if [[ ${#value} -ge 2 ]]; then
      first="${value:0:1}"
      last="${value: -1}"
      if [[ "$first" == '"' && "$last" == '"' ]] || [[ "$first" == "'" && "$last" == "'" ]]; then
        value="${value:1:${#value}-2}"
      fi
    fi
    printf '%s\n' "$value"
    return 0
  done < "$ROOT_DIR/.env"

  return 1
}

config_value() {
  local key="$1"
  local default_value="$2"
  local dotenv_value=""

  if [[ -n "${!key:-}" ]]; then
    printf '%s\n' "${!key}"
    return 0
  fi

  dotenv_value="$(read_dotenv_value "$key" || true)"
  if [[ -n "$dotenv_value" ]]; then
    printf '%s\n' "$dotenv_value"
  else
    printf '%s\n' "$default_value"
  fi
}

validate_port() {
  local name="$1"
  local value="$2"

  case "$value" in
    *[!0-9]*|"")
      die "$name must be a numeric TCP port."
      ;;
  esac
  if (( value < 1 || value > 65535 )); then
    die "$name must be between 1 and 65535."
  fi
}

DEPLOY_REF="${NEXPOLY_DEPLOY_REF:-main}"
DEPLOY_BRANCH="${NEXPOLY_DEPLOY_BRANCH:-main}"
DEPLOY_BUNDLE="${NEXPOLY_DEPLOY_BUNDLE:-}"
WEB_PORT="$(config_value NEXPOLY_WEB_PORT 9000)"
POSTGRES_PORT="$(config_value NEXPOLY_POSTGRES_PORT 55432)"
validate_port NEXPOLY_WEB_PORT "$WEB_PORT"
validate_port NEXPOLY_POSTGRES_PORT "$POSTGRES_PORT"
export NEXPOLY_WEB_PORT="$WEB_PORT"
export NEXPOLY_POSTGRES_PORT="$POSTGRES_PORT"

PYTHON_BIN="${NEXPOLY_TEST_PYTHON:-}"
RUN_SERVER_TESTS="${NEXPOLY_RUN_SERVER_TESTS:-false}"
TEST_POSTGRES_DSN="${NEXPOLY_TEST_POSTGRES_DSN:-postgresql://polyprop:polyprop@localhost:${POSTGRES_PORT}/nexpoly}"
BACKUP_DIR="${NEXPOLY_BACKUP_DIR:-$ROOT_DIR/backups}"
MONOMER_MD_WORKER_DEPLOY_MODE="${NEXPOLY_MONOMER_MD_WORKER_MODE:-auto}"
MONOMER_MD_WORKER_ENV_FILE="${NEXPOLY_MONOMER_MD_WORKER_ENV_FILE:-$ROOT_DIR/.env.monomer-md-worker}"
MONOMER_MD_WORKER_PID_FILE="${NEXPOLY_MONOMER_MD_WORKER_PID_FILE:-$ROOT_DIR/ops/state/monomer-md-worker.pid}"
MONOMER_MD_WORKER_LOG_FILE="${NEXPOLY_MONOMER_MD_WORKER_LOG_FILE:-$ROOT_DIR/ops/logs/monomer-md-worker.log}"
MONOMER_MD_WORKER_SYSTEMD_UNIT="${NEXPOLY_MONOMER_MD_WORKER_SYSTEMD_UNIT:-nexpoly-monomer-md-worker.service}"
POLYTAO_BACKEND_ENABLED="$(config_value POLYTAO_ENABLED "$(config_value POLYTAO_SUBMIT_ENABLED true)")"
POLYTAO_REQUIRED_MODEL_FILES=(
  "model/polytao/config.json"
  "model/polytao/pytorch_model.bin"
  "model/polytao/tokenizer.json"
  "model/polytao/spiece.model"
)
TMP_DIR=""
TEST_WORKTREE=""
TARGET_COMMIT=""

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command is missing: $1"
}

dump_compose_state() {
  log "Docker Compose service state:"
  docker compose ps || true
  log "Recent service logs:"
  docker compose logs --tail=120 postgres-init backend nginx lab-postgres || true
}

cleanup() {
  local status=$?
  if [[ -n "$TEST_WORKTREE" ]]; then
    git worktree remove --force "$TEST_WORKTREE" >/dev/null 2>&1 || true
  fi
  if [[ -n "$TMP_DIR" && "$TMP_DIR" == /tmp/nexpoly-deploy.* ]]; then
    rm -rf "$TMP_DIR"
  fi
  if [[ "$status" -ne 0 ]]; then
    dump_compose_state
  fi
}
trap cleanup EXIT

resolve_target_ref() {
  local requested="$1"
  local resolved=""

  if [[ "$requested" == "$DEPLOY_BRANCH" || "$requested" == "refs/heads/$DEPLOY_BRANCH" || "$requested" == "origin/$DEPLOY_BRANCH" ]]; then
    resolved="origin/$DEPLOY_BRANCH"
  elif git rev-parse --verify --quiet "origin/${requested}^{commit}" >/dev/null; then
    resolved="origin/$requested"
  elif git rev-parse --verify --quiet "${requested}^{commit}" >/dev/null; then
    resolved="$requested"
  else
    die "Cannot resolve deploy ref after fetch: $requested"
  fi

  git rev-parse "${resolved}^{commit}"
}

fetch_deploy_refs() {
  if [[ -n "$DEPLOY_BUNDLE" ]]; then
    [[ -f "$DEPLOY_BUNDLE" ]] || die "Deployment bundle is missing: $DEPLOY_BUNDLE"
    log "Fetching refs from deployment bundle $DEPLOY_BUNDLE."
    git bundle verify "$DEPLOY_BUNDLE" >/dev/null
    git fetch --prune "$DEPLOY_BUNDLE" '+refs/heads/*:refs/remotes/origin/*' '+refs/tags/*:refs/tags/*'
    return 0
  fi

  log "Fetching refs from origin."
  git fetch --prune origin '+refs/heads/*:refs/remotes/origin/*' '+refs/tags/*:refs/tags/*'
}

assert_tracked_worktree_clean() {
  git diff --quiet || die "Deployment checkout has unstaged tracked changes."
  git diff --cached --quiet || die "Deployment checkout has staged tracked changes."
}

wait_for_postgres() {
  log "Waiting for lab-postgres to become ready."
  for _ in $(seq 1 60); do
    if docker compose exec -T lab-postgres pg_isready -U polyprop -d nexpoly >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  die "lab-postgres did not become ready."
}

wait_for_service_healthy() {
  local service="$1"
  local container_id=""
  local health=""

  log "Waiting for $service healthcheck."
  for _ in $(seq 1 60); do
    container_id="$(docker compose ps -q "$service" 2>/dev/null || true)"
    if [[ -n "$container_id" ]]; then
      health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$container_id" 2>/dev/null || true)"
      if [[ "$health" == "healthy" || "$health" == "no-healthcheck" ]]; then
        return 0
      fi
      if [[ "$health" == "unhealthy" ]]; then
        die "$service healthcheck reported unhealthy."
      fi
    fi
    sleep 2
  done
  die "$service did not become healthy."
}

check_compose_contract() {
  docker compose config --quiet
  docker compose config --services | grep -qx 'lab-postgres' || die "docker-compose.yml must define lab-postgres."
  docker compose config --services | grep -qx 'postgres-init' || die "docker-compose.yml must define postgres-init."
  docker compose config --services | grep -qx 'backend' || die "docker-compose.yml must define backend."
  docker compose config --services | grep -qx 'nginx' || die "docker-compose.yml must define nginx."
}

check_required_data_sources() {
  local missing=0
  local required_path
  local required_paths=(
    "database/data1.csv"
    "database/data_txt.zip"
    "database/polymer_process_material_filtered_cleaned_office_utf8_bom.csv"
    "database/polymer_property_detail_cleaned_office_utf8_bom.csv"
    "database/PolymerDatabaseV2.0_reliable085_standardized.csv"
    "backend/data/polyprop.db"
    "backend/data/pi_reverse_design.db"
    "backend/data/fumol.db"
  )

  log "Checking deployment data sources."
  for required_path in "${required_paths[@]}"; do
    if [[ ! -e "$ROOT_DIR/$required_path" && ! -L "$ROOT_DIR/$required_path" ]]; then
      printf '[nexpoly-deploy] Missing data source: %s\n' "$required_path" >&2
      missing=1
    fi
  done

  [[ "$missing" -eq 0 ]] || die "Required deployment data sources are missing."
}

check_required_model_assets() {
  local missing=0
  local asset_path

  log "Checking deployment model assets."
  while IFS= read -r asset_path; do
    if [[ ! -s "$ROOT_DIR/$asset_path" ]]; then
      printf '[nexpoly-deploy] Missing model asset: %s\n' "$asset_path" >&2
      missing=1
    fi
  done < <(python3 "$TEST_WORKTREE/backend/app/model_asset_manifest.py" --format paths)

  if [[ ! -d "$ROOT_DIR/model/reactiont5-retrosynthesis" ]]; then
    printf '[nexpoly-deploy] Missing model directory: model/reactiont5-retrosynthesis\n' >&2
    missing=1
  fi
  if polytao_backend_enabled && ! polytao_model_assets_ready; then
    for required_path in "${POLYTAO_REQUIRED_MODEL_FILES[@]}"; do
      if [[ ! -s "$ROOT_DIR/$required_path" ]]; then
        printf '[nexpoly-deploy] Missing PolyTAO model asset: %s\n' "$required_path" >&2
      fi
    done
    missing=1
  fi

  [[ "$missing" -eq 0 ]] || die "Required deployment model assets are missing."
}

run_backend_tests() {
  case "$RUN_SERVER_TESTS" in
    true|1|yes)
      ;;
    false|0|no|"")
      log "Skipping server-side backend pytest; NEXPOLY_RUN_SERVER_TESTS is not true."
      return 0
      ;;
    *)
      die "NEXPOLY_RUN_SERVER_TESTS must be true or false."
      ;;
  esac

  [[ -n "$PYTHON_BIN" ]] || die "NEXPOLY_TEST_PYTHON is required when NEXPOLY_RUN_SERVER_TESTS=true."
  [[ -x "$PYTHON_BIN" ]] || die "screen312 Python is not executable: $PYTHON_BIN"

  log "Running backend pytest in temporary worktree."
  (
    cd "$TEST_WORKTREE/backend"
    APP_POSTGRES_DSN="$TEST_POSTGRES_DSN" \
      PI_POSTGRES_DSN="$TEST_POSTGRES_DSN" \
      LAB_DATA_POSTGRES_DSN="$TEST_POSTGRES_DSN" \
      "$PYTHON_BIN" -m pytest
  )
}

monomer_worker_enabled() {
  case "$MONOMER_MD_WORKER_DEPLOY_MODE" in
    true|1|yes|enabled)
      return 0
      ;;
    false|0|no|disabled)
      return 1
      ;;
    auto|"")
      [[ -f "$MONOMER_MD_WORKER_ENV_FILE" ]]
      return
      ;;
    *)
      die "NEXPOLY_MONOMER_MD_WORKER_MODE must be auto, true, or false."
      ;;
  esac
}

json_field_is() {
  local payload="$1"
  local field="$2"
  local expected="$3"

  JSON_PAYLOAD="$payload" python3 - "$field" "$expected" <<'PY'
import json
import os
import sys

field = sys.argv[1]
expected = sys.argv[2]
try:
    payload = json.loads(os.environ.get("JSON_PAYLOAD", ""))
except Exception:
    sys.exit(1)

actual = payload.get(field) if isinstance(payload, dict) else None
if expected == "true":
    ok = actual is True
elif expected == "false":
    ok = actual is False
else:
    ok = actual == expected
sys.exit(0 if ok else 1)
PY
}

polytao_model_assets_ready() {
  local required_path
  for required_path in "${POLYTAO_REQUIRED_MODEL_FILES[@]}"; do
    [[ -s "$ROOT_DIR/$required_path" ]] || return 1
  done
  return 0
}

polytao_backend_enabled() {
  case "$POLYTAO_BACKEND_ENABLED" in
    true|1|yes|enabled)
      return 0
      ;;
    false|0|no|disabled)
      return 1
      ;;
    auto|"")
      return 0
      ;;
    *)
      die "POLYTAO_ENABLED must be true or false."
      ;;
  esac
}

check_polytao_backend_status() {
  if ! polytao_backend_enabled; then
    return 0
  fi

  log "Checking backend PolyTAO status endpoint."
  local payload
  payload="$(curl --fail --silent --show-error --max-time 10 "http://127.0.0.1:${WEB_PORT}/api/v1/conditional-generation/polytao/status")"
  printf '%s\n' "$payload"
  json_field_is "$payload" available true || die "Backend PolyTAO status is not available."
}

load_monomer_worker_env() {
  [[ -f "$MONOMER_MD_WORKER_ENV_FILE" ]] || die "Monomer MD worker env file is missing: $MONOMER_MD_WORKER_ENV_FILE"
  set -a
  # shellcheck disable=SC1090
  source "$MONOMER_MD_WORKER_ENV_FILE"
  set +a
}

validate_monomer_worker_env() {
  : "${MONOMER_MD_PYTHON:?MONOMER_MD_PYTHON is required in $MONOMER_MD_WORKER_ENV_FILE}"
  : "${APP_POSTGRES_DSN:?APP_POSTGRES_DSN is required in $MONOMER_MD_WORKER_ENV_FILE}"
  : "${BYTEFF2_ROOT:?BYTEFF2_ROOT is required in $MONOMER_MD_WORKER_ENV_FILE}"
  : "${MONOMER_MD_JOB_ROOT:?MONOMER_MD_JOB_ROOT is required in $MONOMER_MD_WORKER_ENV_FILE}"

  [[ -x "$MONOMER_MD_PYTHON" ]] || die "MONOMER_MD_PYTHON is not executable: $MONOMER_MD_PYTHON"
  [[ -d "$BYTEFF2_ROOT" ]] || die "BYTEFF2_ROOT does not exist: $BYTEFF2_ROOT"
}

stop_monomer_worker() {
  if [[ -f "$MONOMER_MD_WORKER_PID_FILE" ]]; then
    local pid
    pid="$(cat "$MONOMER_MD_WORKER_PID_FILE" || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" >/dev/null 2>&1; then
      log "Stopping existing monomer MD worker pid $pid."
      kill "$pid" || true
      for _ in $(seq 1 20); do
        kill -0 "$pid" >/dev/null 2>&1 || break
        sleep 1
      done
      kill -0 "$pid" >/dev/null 2>&1 && kill -9 "$pid" || true
    fi
    rm -f "$MONOMER_MD_WORKER_PID_FILE"
  fi
}

restart_monomer_worker_with_user_systemd() {
  command -v systemctl >/dev/null 2>&1 || return 1

  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  if [[ ! -d "$XDG_RUNTIME_DIR" ]]; then
    log "User systemd runtime directory is unavailable; falling back to pidfile worker restart."
    return 1
  fi

  if ! systemctl --user list-unit-files "$MONOMER_MD_WORKER_SYSTEMD_UNIT" --no-legend 2>/dev/null | grep -q "^$MONOMER_MD_WORKER_SYSTEMD_UNIT"; then
    log "User systemd unit $MONOMER_MD_WORKER_SYSTEMD_UNIT is not installed; falling back to pidfile worker restart."
    return 1
  fi

  log "Restarting monomer MD worker with user systemd unit $MONOMER_MD_WORKER_SYSTEMD_UNIT."
  stop_monomer_worker
  systemctl --user daemon-reload
  systemctl --user restart "$MONOMER_MD_WORKER_SYSTEMD_UNIT"
  return 0
}

wait_for_monomer_worker() {
  local port="${MONOMER_MD_WORKER_PORT:-18010}"
  local health_host="${MONOMER_MD_WORKER_HEALTH_HOST:-${MONOMER_MD_WORKER_HOST:-127.0.0.1}}"
  local max_time="${MONOMER_MD_HEALTH_PROBE_TIMEOUT_SECONDS:-5}"
  local payload=""

  max_time=$((max_time + 5))
  if [[ -n "${MONOMER_MD_WORKER_UDS:-}" ]]; then
    log "Waiting for monomer MD worker health on unix socket $MONOMER_MD_WORKER_UDS."
    for _ in $(seq 1 90); do
      payload="$(curl --silent --show-error --max-time "$max_time" --unix-socket "$MONOMER_MD_WORKER_UDS" "http://monomer-md-worker/health" 2>/dev/null || true)"
      if json_field_is "$payload" status ok && json_field_is "$payload" runtime_ready true; then
        printf '%s\n' "$payload"
        return 0
      fi
      sleep 2
    done

    printf '[nexpoly-deploy] Last monomer MD worker health payload: %s\n' "$payload" >&2
    die "Monomer MD worker did not become healthy."
  fi

  if [[ "$health_host" == "0.0.0.0" || "$health_host" == "::" ]]; then
    health_host="127.0.0.1"
  fi

  log "Waiting for monomer MD worker health on $health_host:$port."
  for _ in $(seq 1 90); do
    payload="$(curl --silent --show-error --max-time "$max_time" "http://${health_host}:${port}/health" 2>/dev/null || true)"
    if json_field_is "$payload" status ok && json_field_is "$payload" runtime_ready true; then
      printf '%s\n' "$payload"
      return 0
    fi
    sleep 2
  done

  printf '[nexpoly-deploy] Last monomer MD worker health payload: %s\n' "$payload" >&2
  die "Monomer MD worker did not become healthy."
}

restart_monomer_worker() {
  if ! monomer_worker_enabled; then
    log "Skipping monomer MD worker restart; $MONOMER_MD_WORKER_ENV_FILE is absent or worker mode is disabled."
    return 0
  fi

  log "Restarting host-side monomer MD worker."
  require_cmd python3
  load_monomer_worker_env
  validate_monomer_worker_env

  mkdir -p "$(dirname "$MONOMER_MD_WORKER_PID_FILE")" "$(dirname "$MONOMER_MD_WORKER_LOG_FILE")" "$MONOMER_MD_JOB_ROOT"
  "$MONOMER_MD_PYTHON" -m pip install -r "$ROOT_DIR/workers/monomer_md_worker/requirements.txt"

  if restart_monomer_worker_with_user_systemd; then
    wait_for_monomer_worker
    return 0
  fi

  stop_monomer_worker
  (
    cd "$ROOT_DIR/workers/monomer_md_worker"
    export MONOMER_MD_WORKER_MODE="${MONOMER_MD_WORKER_MODE:-real}"
    export MONOMER_MD_WORKER_HOST="${MONOMER_MD_WORKER_HOST:-127.0.0.1}"
    export MONOMER_MD_WORKER_PORT="${MONOMER_MD_WORKER_PORT:-18010}"
    setsid ./run_host_worker.sh > "$MONOMER_MD_WORKER_LOG_FILE" 2>&1 < /dev/null &
    printf '%s\n' "$!" > "$MONOMER_MD_WORKER_PID_FILE"
  )

  wait_for_monomer_worker
}

check_monomer_backend_status() {
  if ! monomer_worker_enabled; then
    return 0
  fi

  log "Checking backend monomer MD status endpoint."
  local payload
  payload="$(curl --fail --silent --show-error --max-time 10 "http://localhost:${WEB_PORT}/api/v1/monomer-md/status")"
  printf '%s\n' "$payload"
  json_field_is "$payload" available true || die "Backend monomer MD status is not available."
}

monomer_smoke_enabled() {
  case "${NEXPOLY_MONOMER_MD_SMOKE:-false}" in
    true|1|yes|enabled)
      return 0
      ;;
    false|0|no|disabled|"")
      return 1
      ;;
    *)
      die "NEXPOLY_MONOMER_MD_SMOKE must be true or false."
      ;;
  esac
}

run_monomer_md_smoke() {
  if ! monomer_worker_enabled || ! monomer_smoke_enabled; then
    return 0
  fi

  log "Running monomer MD CCO smoke through backend."
  require_cmd python3
  NEXPOLY_WEB_PORT="$WEB_PORT" python3 <<'PY'
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

port = os.environ["NEXPOLY_WEB_PORT"]
timeout_seconds = int(os.environ.get("NEXPOLY_MONOMER_MD_SMOKE_TIMEOUT_SECONDS", "300"))
base_url = f"http://127.0.0.1:{port}/api/v1/monomer-md"

def request_json(url: str, *, body: bytes | None = None) -> dict:
    headers = {"Content-Type": "application/json"} if body is not None else {}
    request = urllib.request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{url} returned HTTP {exc.code}: {detail}") from exc

created = request_json(f"{base_url}/jobs", body=b'{"smiles":"CCO"}')
job_id = created.get("job_id")
if not isinstance(job_id, str) or not job_id:
    raise RuntimeError(f"monomer MD smoke did not return a job_id: {created}")

deadline = time.time() + timeout_seconds
payload = {}
while time.time() < deadline:
    payload = request_json(f"{base_url}/jobs/{job_id}")
    status = payload.get("status")
    if status == "completed":
        break
    if status in {"failed", "cancelled"}:
        raise RuntimeError(f"monomer MD smoke ended with {status}: {payload.get('error_message')}")
    time.sleep(5)
else:
    raise RuntimeError(f"monomer MD smoke timed out waiting for job {job_id}; last payload: {payload}")

result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
if payload.get("completed_steps") != 1000 or (result.get("summary") or {}).get("n_steps") != 1000:
    raise RuntimeError(f"monomer MD smoke completed with unexpected step counts: {payload.get('completed_steps')}, {result.get('summary')}")
if result.get("not_equilibrated") is not True or result.get("physical_density_estimate") is not False:
    raise RuntimeError("monomer MD smoke result lost non-physical demo markers")
if not result.get("warnings"):
    raise RuntimeError("monomer MD smoke result did not include warnings")

artifact_root = payload.get("artifact_root") or (payload.get("artifacts") or {}).get("artifact_root")
if not artifact_root:
    raise RuntimeError("monomer MD smoke did not return an artifact root")
root = Path(str(artifact_root))
missing = [name for name in ("density_demo_results.json", "npt_state.csv", "npt.dcd") if not (root / name).is_file() or (root / name).stat().st_size <= 0]
if missing:
    raise RuntimeError(f"monomer MD smoke artifacts are missing or empty under {root}: {', '.join(missing)}")

print(json.dumps({"job_id": job_id, "artifact_root": str(root), "status": "completed"}, ensure_ascii=False))
PY
}

create_database_backup() {
  local short_sha="${TARGET_COMMIT:0:12}"
  local backup_file="$BACKUP_DIR/nexpoly-${short_sha}.dump"

  mkdir -p "$BACKUP_DIR"
  if [[ -e "$backup_file" ]]; then
    backup_file="$BACKUP_DIR/nexpoly-${short_sha}-$(date +%Y%m%d-%H%M%S).dump"
  fi

  log "Backing up Postgres database to $backup_file."
  docker compose exec -T lab-postgres pg_dump -U polyprop -d nexpoly -Fc > "$backup_file"
}

checkout_target_ref() {
  log "Updating deployment checkout to $DEPLOY_REF ($TARGET_COMMIT)."
  if [[ "$DEPLOY_REF" == "$DEPLOY_BRANCH" || "$DEPLOY_REF" == "refs/heads/$DEPLOY_BRANCH" || "$DEPLOY_REF" == "origin/$DEPLOY_BRANCH" ]]; then
    if git show-ref --verify --quiet "refs/heads/$DEPLOY_BRANCH"; then
      git checkout "$DEPLOY_BRANCH"
    else
      git checkout -b "$DEPLOY_BRANCH" "$TARGET_COMMIT"
    fi
    git merge --ff-only "$TARGET_COMMIT"
    [[ "$(git rev-parse HEAD)" == "$TARGET_COMMIT" ]] || die "Branch HEAD does not match tested target commit."
  else
    git checkout --detach "$TARGET_COMMIT"
  fi
}

deploy_compose_stack() {
  log "Building Docker images."
  docker compose build

  log "Running Postgres import gate."
  docker compose run --rm postgres-init

  log "Recreating backend."
  docker compose up -d --no-deps --force-recreate backend
  wait_for_service_healthy backend

  log "Recreating nginx."
  docker compose up -d --no-deps --force-recreate nginx

  log "Running runtime Postgres preflight."
  docker compose exec -T backend python -m app.postgres_preflight --mode runtime --strict

  restart_monomer_worker
  check_monomer_backend_status
  run_monomer_md_smoke
  check_polytao_backend_status

  log "Checking public health endpoint on 127.0.0.1:$WEB_PORT."
  curl --fail --silent --show-error --max-time 10 "http://127.0.0.1:${WEB_PORT}/health"
  printf '\n'
}

main() {
  case "$DEPLOY_REF" in
    *[!A-Za-z0-9._/@:-]*)
      die "NEXPOLY_DEPLOY_REF contains unsupported characters: $DEPLOY_REF"
      ;;
  esac

  [[ -d .git ]] || die "$ROOT_DIR is not a Git checkout."
  [[ -f docker-compose.yml ]] || die "docker-compose.yml is missing from $ROOT_DIR."

  require_cmd git
  require_cmd docker
  require_cmd curl
  require_cmd grep
  require_cmd python3

  assert_tracked_worktree_clean
  check_compose_contract

  log "Using web port $WEB_PORT, Postgres host port $POSTGRES_PORT, and PolyTAO backend enabled=$POLYTAO_BACKEND_ENABLED."
  fetch_deploy_refs
  TARGET_COMMIT="$(resolve_target_ref "$DEPLOY_REF")"

  TMP_DIR="$(mktemp -d /tmp/nexpoly-deploy.XXXXXX)"
  TEST_WORKTREE="$TMP_DIR/worktree"
  git worktree add --detach "$TEST_WORKTREE" "$TARGET_COMMIT" >/dev/null

  docker compose up -d lab-postgres
  wait_for_postgres

  run_backend_tests
  check_required_data_sources
  check_required_model_assets
  create_database_backup
  checkout_target_ref
  deploy_compose_stack

  log "Deployment completed at commit $TARGET_COMMIT."
}

main "$@"

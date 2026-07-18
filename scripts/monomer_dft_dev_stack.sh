#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -P "$SCRIPT_DIR/.." && pwd -P)"
PRODUCTION_REPO_ROOT="/data/lzq/gith/nexpoly"
COMPOSE_FILE="$REPO_ROOT/docker-compose.monomer-dft-dev.yml"
ENV_FILE="$REPO_ROOT/.env.monomer-dft.dev"
WORKER_CTL="$SCRIPT_DIR/monomer_dft_worker_ctl.sh"
PROJECT_NAME="${NEXPOLY_DFT_PROJECT_NAME:-nexpoly_dft_dev}"
MIGRATIONS_DIR="$REPO_ROOT/backend/migrations/postgres"
DOWNLOAD_SPOOL_DIR="$REPO_ROOT/.runtime/monomer-dft-download-spool"

log() {
  printf '[monomer-dft-dev] %s\n' "$*"
}

fail() {
  log "$*" >&2
  exit 2
}

[[ "$REPO_ROOT" != "$PRODUCTION_REPO_ROOT" ]] || fail \
  "development DFT control is forbidden in the production repository"

load_env() {
  [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail "missing safe environment file: $ENV_FILE"
  [[ "$(stat -c '%u' "$ENV_FILE")" == "$(id -u)" ]] || fail "environment file must be owned by uid $(id -u)"
  [[ "$(stat -c '%a' "$ENV_FILE")" == "600" ]] || fail "environment file permissions must be 0600"
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  PROJECT_NAME="${NEXPOLY_DFT_PROJECT_NAME:-nexpoly_dft_dev}"
  [[ "$PROJECT_NAME" =~ ^nexpoly_dft_(dev|fresh_[a-z0-9][a-z0-9_-]{0,40})$ ]] || fail \
    "NEXPOLY_DFT_PROJECT_NAME must be the dev project or a fresh dev acceptance project"
  export NEXPOLY_DFT_PROJECT_NAME="$PROJECT_NAME"
  [[ "${MONOMER_DFT_DEPLOYMENT:-dev}" == "dev" ]] || fail \
    "MONOMER_DFT_DEPLOYMENT must be exactly dev; production mode is forbidden"
  [[ "${NEXPOLY_DFT_GPU_DEVICE:-1}" == "1" ]] || fail \
    "development DFT primary GPU must be physical GPU 1; GPUs 0 and 2 are forbidden"
  [[ "${NEXPOLY_DFT_OVERFLOW_GPU_DEVICES:-3}" == "3" ]] || fail \
    "development DFT overflow must be physical GPU 3 only; GPUs 0 and 2 are forbidden"
  export MONOMER_DFT_DEPLOYMENT=dev
  export NEXPOLY_DFT_GPU_DEVICE=1
  export NEXPOLY_DFT_OVERFLOW_GPU_DEVICES=3
  [[ -z "${PYTHONPATH:-}" ]] || fail "PYTHONPATH must be empty"
  [[ "${NEXPOLY_DFT_FRONTEND_PORT:-25173}" == "25173" ]] || fail "NEXPOLY_DFT_FRONTEND_PORT must be fixed to 25173"
  [[ "${NEXPOLY_DFT_BACKEND_PORT:-28000}" == "28000" ]] || fail "NEXPOLY_DFT_BACKEND_PORT must be fixed to 28000"
  [[ "${NEXPOLY_DFT_POSTGRES_PORT:-25532}" == "25532" ]] || fail "NEXPOLY_DFT_POSTGRES_PORT must be fixed to 25532"
  export NEXPOLY_DFT_FRONTEND_PORT=25173 NEXPOLY_DFT_BACKEND_PORT=28000 NEXPOLY_DFT_POSTGRES_PORT=25532

  [[ -d "$REPO_ROOT/.runtime" && ! -L "$REPO_ROOT/.runtime" ]] || fail \
    "runtime root must be a real directory: $REPO_ROOT/.runtime"
  [[ "$(stat -c '%u' "$REPO_ROOT/.runtime")" == "$(id -u)" ]] || fail \
    "runtime root must be owned by uid $(id -u)"
  chmod 700 -- "$REPO_ROOT/.runtime"

  MONOMER_DFT_WORKER_UDS="${MONOMER_DFT_WORKER_UDS:-.runtime/monomer-dft-worker-socket/worker.sock}"
  if [[ "$MONOMER_DFT_WORKER_UDS" != /* ]]; then
    MONOMER_DFT_WORKER_UDS="$REPO_ROOT/$MONOMER_DFT_WORKER_UDS"
  fi
  MONOMER_DFT_WORKER_UDS="$(realpath -ms -- "$MONOMER_DFT_WORKER_UDS")"
  [[ "$MONOMER_DFT_WORKER_UDS" == \
    "$REPO_ROOT/.runtime/monomer-dft-worker-socket/worker.sock" ]] || fail \
    "worker socket must use the current worktree's fixed development path"
  [[ ! -L "$MONOMER_DFT_WORKER_UDS" ]] || fail "worker socket must not be a symlink"
  local socket_dir
  socket_dir="$(dirname -- "$MONOMER_DFT_WORKER_UDS")"
  [[ -d "$socket_dir" && ! -L "$socket_dir" ]] || fail "worker socket directory must be a real directory: $socket_dir"
  if [[ -n "${MONOMER_DFT_WORKER_SOCKET_DIR:-}" ]]; then
    local configured_socket_dir="$MONOMER_DFT_WORKER_SOCKET_DIR"
    [[ "$configured_socket_dir" == /* ]] || configured_socket_dir="$REPO_ROOT/$configured_socket_dir"
    configured_socket_dir="$(realpath -ms -- "$configured_socket_dir")"
    [[ "$configured_socket_dir" == "$socket_dir" ]] || fail \
      "MONOMER_DFT_WORKER_SOCKET_DIR must match the worker socket directory"
  fi
  MONOMER_DFT_WORKER_SOCKET_DIR="$socket_dir"
  [[ "${MONOMER_DFT_DOWNLOAD_SPOOL_ROOT:-/app/.runtime/monomer-dft-download-spool}" == \
    "/app/.runtime/monomer-dft-download-spool" ]] || fail \
    "download spool must use the fixed worktree-backed development mount"
  export MONOMER_DFT_WORKER_UDS MONOMER_DFT_WORKER_SOCKET_DIR
  export MONOMER_DFT_DOWNLOAD_SPOOL_ROOT=/app/.runtime/monomer-dft-download-spool
}

ensure_download_spool() {
  [[ "$DOWNLOAD_SPOOL_DIR" == "$REPO_ROOT/.runtime/"* ]] || fail \
    "download spool escaped the current worktree runtime"
  [[ ! -L "$DOWNLOAD_SPOOL_DIR" ]] || fail \
    "download spool must not be a symlink: $DOWNLOAD_SPOOL_DIR"
  if [[ ! -e "$DOWNLOAD_SPOOL_DIR" ]]; then
    mkdir --mode=700 -- "$DOWNLOAD_SPOOL_DIR"
  fi
  [[ -d "$DOWNLOAD_SPOOL_DIR" && ! -L "$DOWNLOAD_SPOOL_DIR" ]] || fail \
    "download spool must be a real directory: $DOWNLOAD_SPOOL_DIR"
  [[ "$(stat -c '%u' "$DOWNLOAD_SPOOL_DIR")" == "$(id -u)" ]] || fail \
    "download spool must be owned by uid $(id -u)"
  chmod 700 -- "$DOWNLOAD_SPOOL_DIR"
  [[ "$(stat -c '%a' "$DOWNLOAD_SPOOL_DIR")" == "700" ]] || fail \
    "download spool permissions must be 0700"
}

compose() {
  docker compose \
    --project-name "$PROJECT_NAME" \
    --env-file "$ENV_FILE" \
    --file "$COMPOSE_FILE" \
    "$@"
}

worker_request() {
  local method="$1"
  local path="$2"
  curl --silent --show-error --fail --max-time 5 \
    --request "$method" \
    --unix-socket "$MONOMER_DFT_WORKER_UDS" \
    "http://monomer-dft-worker$path"
}

worker_running() {
  "$WORKER_CTL" status >/dev/null 2>&1
}

assert_worker_ready() {
  worker_request GET /health | python3 -c '
import json, sys
payload = json.load(sys.stdin)
if not isinstance(payload, dict):
    raise SystemExit("worker health response must be an object")
expected = {
    "status": "ok",
    "runtime_ready": True,
    "draining": False,
    "recovering": False,
}
if any(payload.get(key) != value for key, value in expected.items()):
    raise SystemExit("worker runtime is not ready")
'
}

assert_worker_draining() {
  python3 -c '
import json, sys
payload = json.load(sys.stdin)
if not isinstance(payload, dict):
    raise SystemExit("worker drain response must be an object")
if payload.get("status") != "draining" or payload.get("accepting_jobs") is not False:
    raise SystemExit("worker did not enter drain mode")
for key in ("active_jobs", "queued_jobs"):
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SystemExit("worker drain response has an invalid job count")
worker_instance_id = payload.get("worker_instance_id")
if not isinstance(worker_instance_id, str) or len(worker_instance_id) != 32:
    raise SystemExit("worker drain response has an invalid instance id")
try:
    int(worker_instance_id, 16)
except ValueError as exc:
    raise SystemExit("worker drain response has an invalid instance id") from exc
print(worker_instance_id)
'
}

drain_worker_instance() {
  worker_request POST /drain | assert_worker_draining
}

worker_instance_id() {
  worker_request GET /health | python3 -c '
import json, sys
payload = json.load(sys.stdin)
if not isinstance(payload, dict):
    raise SystemExit("worker health response must be an object")
worker_instance_id = payload.get("worker_instance_id")
if not isinstance(worker_instance_id, str) or len(worker_instance_id) != 32:
    raise SystemExit("worker health response has an invalid instance id")
try:
    int(worker_instance_id, 16)
except ValueError as exc:
    raise SystemExit("worker health response has an invalid instance id") from exc
print(worker_instance_id)
'
}

worker_instance_is_draining() {
  local expected_worker_instance_id="$1"
  worker_request GET /health | python3 -c '
import json, sys
payload = json.load(sys.stdin)
expected_worker_instance_id = sys.argv[1]
if not isinstance(payload, dict):
    raise SystemExit("worker health response must be an object")
if payload.get("worker_instance_id") != expected_worker_instance_id:
    raise SystemExit("worker instance changed while verifying drain state")
if payload.get("draining") is not True or payload.get("accepting_jobs") is not False:
    raise SystemExit("worker is no longer drained")
' "$expected_worker_instance_id"
}

assert_full_stack_gate() {
  local contract_migration="$MIGRATIONS_DIR/0012_drop_polytao_jobs.sql"
  local dft_migration="$MIGRATIONS_DIR/0013_monomer_dft_jobs.sql"
  local manifest="$MIGRATIONS_DIR/manifest.json"
  local policy_module="$REPO_ROOT/backend/app/migration_policy.py"
  local dft_release_validator="$REPO_ROOT/scripts/validate_monomer_dft_release_contract.py"
  local canonical_ci="$REPO_ROOT/.github/workflows/ci.yml"
  local release_controller="$REPO_ROOT/scripts/release_controller.py"
  local deployment_control="$REPO_ROOT/backend/app/services/deployment_control.py"
  local release_controller_tests="$REPO_ROOT/scripts/tests/test_release_controller.py"
  local deployment_control_tests="$REPO_ROOT/backend/tests/test_deployment_control.py"
  local temporary_ci="$REPO_ROOT/.github/workflows/monomer-dft-ci.yml"
  [[ -f "$contract_migration" && -f "$manifest" ]] || fail \
    "full-stack gate is closed: merge codex/cicd-overhaul into main and rebase this DFT branch first"
  [[ -f "$dft_migration" ]] || fail "DFT migration is missing: $dft_migration"
  [[ -f "$policy_module" ]] || fail "migration policy validator is missing after the rebase"
  local required_path relative_path
  local -a required_paths=(
    "$contract_migration"
    "$dft_migration"
    "$manifest"
    "$policy_module"
    "$dft_release_validator"
    "$canonical_ci"
    "$release_controller"
    "$deployment_control"
    "$release_controller_tests"
    "$deployment_control_tests"
  )
  for required_path in "${required_paths[@]}"; do
    [[ -f "$required_path" && ! -L "$required_path" ]] || fail \
      "full-stack gate asset is missing or unsafe: $required_path"
    relative_path="${required_path#"$REPO_ROOT"/}"
    git -C "$REPO_ROOT" ls-files --error-unmatch -- "$relative_path" >/dev/null 2>&1 || fail \
      "full-stack gate asset must be committed: $relative_path"
    git -C "$REPO_ROOT" diff --quiet HEAD -- "$relative_path" || fail \
      "full-stack gate asset has uncommitted changes: $relative_path"
  done
  [[ ! -e "$temporary_ci" && ! -L "$temporary_ci" ]] || fail \
    "temporary monomer DFT workflow must be removed after integration into the canonical ci-gate"
  git -C "$REPO_ROOT" cat-file -e \
    'HEAD:.github/workflows/monomer-dft-ci.yml' 2>/dev/null && fail \
    "temporary monomer DFT workflow removal must be committed"
  git -C "$REPO_ROOT" diff --quiet HEAD -- \
    .github/workflows/monomer-dft-ci.yml || fail \
    "temporary monomer DFT workflow has an uncommitted index or worktree state"

  git -C "$REPO_ROOT" show-ref --verify --quiet refs/remotes/origin/main || fail \
    "origin/main is unavailable; fetch and rebase before opening the full-stack gate"
  git -C "$REPO_ROOT" show-ref --verify --quiet refs/heads/main || fail \
    "local main is unavailable"
  [[ "$(git -C "$REPO_ROOT" rev-parse refs/heads/main)" == \
    "$(git -C "$REPO_ROOT" rev-parse refs/remotes/origin/main)" ]] || fail \
    "local main must match origin/main before opening the full-stack gate"
  git -C "$REPO_ROOT" merge-base --is-ancestor refs/remotes/origin/main HEAD || fail \
    "DFT branch is not rebased onto origin/main"

  local -a upstream_paths=(
    "backend/migrations/postgres/0012_drop_polytao_jobs.sql"
    "backend/migrations/postgres/manifest.json"
    "backend/app/migration_policy.py"
    ".github/workflows/ci.yml"
    "scripts/release_controller.py"
    "backend/app/services/deployment_control.py"
  )
  for relative_path in "${upstream_paths[@]}"; do
    git -C "$REPO_ROOT" cat-file -e "refs/remotes/origin/main:$relative_path" 2>/dev/null || fail \
      "required CI/CD foundation is not merged into origin/main: $relative_path"
  done
  git -C "$REPO_ROOT" diff --quiet refs/remotes/origin/main -- \
    backend/migrations/postgres/0012_drop_polytao_jobs.sql \
    backend/app/migration_policy.py || fail \
    "contract migration or migration policy diverges from the merged main foundation"

  PYTHONPATH="$REPO_ROOT/backend" python3 -m app.migration_policy \
    --migrations-dir "$MIGRATIONS_DIR" >/dev/null || fail \
    "migration SQL and manifest policy validation failed"
  python3 "$dft_release_validator" >/dev/null || fail \
    "monomer DFT release contract validation failed"
  python3 - "$manifest" <<'PY' || fail \
    "migration manifest must classify 0012 as contract and 0013 as the following expand migration"
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
if payload.get("schema_version") != 2:
    raise SystemExit(1)
entries = payload.get("migrations", [])
versions = [entry.get("version") for entry in entries if isinstance(entry, dict)]
by_version = {
    entry.get("version"): entry
    for entry in entries
    if isinstance(entry, dict)
}
contract = by_version.get("0012_drop_polytao_jobs")
dft = by_version.get("0013_monomer_dft_jobs")
if not isinstance(contract, dict) or not isinstance(dft, dict):
    raise SystemExit(1)
if contract.get("kind") != "contract" or contract.get("epoch") != 1:
    raise SystemExit(1)
if dft.get("kind") != "expand" or dft.get("epoch") != 2:
    raise SystemExit(1)
if dft.get("requires_contracts") != [{
    "version": "0012_drop_polytao_jobs",
    "checksum": contract.get("checksum"),
}]:
    raise SystemExit(1)
if versions.index("0013_monomer_dft_jobs") != versions.index("0012_drop_polytao_jobs") + 1:
    raise SystemExit(1)
PY
}

rollback_resumed_worker() {
  local resumed_worker_instance_id="$1"
  worker_running || return 0
  local observed_worker_instance_id=""
  observed_worker_instance_id="$(worker_instance_id)" || {
    log "cannot identify the Worker while rolling back resume" >&2
    return 1
  }
  if [[ "$observed_worker_instance_id" != "$resumed_worker_instance_id" ]]; then
    log "Worker instance changed after resume; draining the replacement instance"
  fi
  local drained_worker_instance_id=""
  drained_worker_instance_id="$(drain_worker_instance)" || return 1
  worker_instance_is_draining "$drained_worker_instance_id"
}

running_job_count() {
  local expected_worker_instance_id="$1"
  worker_request GET '/jobs?state=active' | python3 -c '
import json, sys
expected_worker_instance_id = sys.argv[1]
payload = json.load(sys.stdin)
if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
    raise SystemExit("worker active-job response has an invalid shape")
jobs = payload["jobs"]
total = payload.get("total")
if isinstance(total, bool) or not isinstance(total, int) or total != len(jobs):
    raise SystemExit("worker active-job count is inconsistent")
if any(not isinstance(job, dict) or job.get("status") not in {"queued", "running", "cancel_requested"} for job in jobs):
    raise SystemExit("worker active-job response contains an invalid job")
if any(job.get("worker_instance_id") != expected_worker_instance_id for job in jobs):
    raise SystemExit("worker instance changed while inspecting active jobs")
print(sum(1 for job in jobs if job.get("status") in {"running", "cancel_requested"}))
' "$expected_worker_instance_id"
}

start_stack() {
  assert_full_stack_gate
  ensure_download_spool
  local started_worker=false
  local resumed_worker_instance_id=""
  if worker_running; then
    log "worker is already running"
    if worker_request GET /health | python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("draining") is True else 1)'; then
      log "resuming drained worker"
      resumed_worker_instance_id="$(worker_instance_id)" || fail "cannot identify the drained Worker"
      if ! worker_request POST /resume >/dev/null; then
        rollback_resumed_worker "$resumed_worker_instance_id" || true
        fail "worker resume request failed"
      fi
      if [[ "$(worker_instance_id)" != "$resumed_worker_instance_id" ]]; then
        rollback_resumed_worker "$resumed_worker_instance_id" || true
        fail "worker instance changed while it was resumed"
      fi
    fi
  else
    "$WORKER_CTL" start
    started_worker=true
  fi

  if ! assert_worker_ready; then
    if [[ "$started_worker" == "true" ]]; then
      "$WORKER_CTL" stop || true
    elif [[ -n "$resumed_worker_instance_id" ]]; then
      rollback_resumed_worker "$resumed_worker_instance_id" || \
        log "failed to restore drain state after readiness failure" >&2
    fi
    fail "worker runtime is not ready"
  fi

  if ! compose up --detach --build --remove-orphans; then
    compose down --remove-orphans || true
    if [[ "$started_worker" == "true" ]]; then
      "$WORKER_CTL" stop || true
    elif [[ -n "$resumed_worker_instance_id" ]]; then
      rollback_resumed_worker "$resumed_worker_instance_id" || \
        log "failed to restore drain state after Compose failure" >&2
    fi
    fail "DFT development stack failed to start"
  fi

  log "frontend: http://127.0.0.1:${NEXPOLY_DFT_FRONTEND_PORT:-25173}/monomer-dft"
  log "backend:  http://127.0.0.1:${NEXPOLY_DFT_BACKEND_PORT:-28000}/api/v1/monomer-dft/status"
  log "compose project: $PROJECT_NAME (volume: ${PROJECT_NAME}_monomer_dft_postgres_data)"
}

QUIESCENT_WORKER_INSTANCE_ID=""

wait_for_worker_quiescence() {
  local expected_worker_instance_id="$1"
  local timeout="$2"
  local deadline=$((SECONDS + timeout))
  local count=-1
  QUIESCENT_WORKER_INSTANCE_ID=""

  if [[ -z "$expected_worker_instance_id" ]]; then
    expected_worker_instance_id="$(drain_worker_instance)" || return 1
  fi
  while (( SECONDS < deadline )); do
    local observed_worker_instance_id=""
    if ! observed_worker_instance_id="$(worker_instance_id)"; then
      log "worker health is temporarily unavailable while draining; retrying"
      sleep 2
      continue
    fi
    if [[ "$observed_worker_instance_id" != "$expected_worker_instance_id" ]]; then
      log "worker instance changed during drain; draining the replacement instance"
      expected_worker_instance_id="$(drain_worker_instance)" || return 1
      deadline=$((SECONDS + timeout))
      continue
    fi
    if ! worker_instance_is_draining "$expected_worker_instance_id"; then
      log "worker drain state was lost; draining the current instance again"
      expected_worker_instance_id="$(drain_worker_instance)" || return 1
      deadline=$((SECONDS + timeout))
      continue
    fi
    if ! count="$(running_job_count "$expected_worker_instance_id")"; then
      log "worker state changed while active jobs were inspected; retrying"
      sleep 2
      continue
    fi
    if [[ "$count" == "0" ]] && worker_instance_is_draining "$expected_worker_instance_id"; then
      QUIESCENT_WORKER_INSTANCE_ID="$expected_worker_instance_id"
      return 0
    fi
    log "waiting for $count running job(s)"
    sleep 2
  done
  return 1
}

stop_worker_fenced() {
  local expected_worker_instance_id="$1"
  local timeout="$2"
  while worker_running; do
    wait_for_worker_quiescence "$expected_worker_instance_id" "$timeout" || return 1
    expected_worker_instance_id="$QUIESCENT_WORKER_INSTANCE_ID"
    if "$WORKER_CTL" stop-if-drained-instance "$expected_worker_instance_id"; then
      return 0
    fi
    log "conditional Worker stop lost its instance fence; retrying with the current instance"
    expected_worker_instance_id=""
  done
}

stop_stack() {
  local had_worker=false
  local expected_worker_instance_id=""
  local timeout="${MONOMER_DFT_DRAIN_TIMEOUT_SECONDS:-1800}"
  [[ "$timeout" =~ ^[0-9]+$ ]] || fail "MONOMER_DFT_DRAIN_TIMEOUT_SECONDS must be an integer"
  (( timeout >= 1 )) || fail "MONOMER_DFT_DRAIN_TIMEOUT_SECONDS must be at least 1"

  if worker_running; then
    had_worker=true
    log "draining worker"
    expected_worker_instance_id="$(drain_worker_instance)" || fail "worker drain request failed"
    log "draining worker instance $expected_worker_instance_id"
    wait_for_worker_quiescence "$expected_worker_instance_id" "$timeout" || fail \
      "worker drain could not be verified within ${timeout}s; stack remains online"
    expected_worker_instance_id="$QUIESCENT_WORKER_INSTANCE_ID"
  fi

  compose down --remove-orphans
  if [[ "$had_worker" == "true" ]] && worker_running; then
    stop_worker_fenced "$expected_worker_instance_id" "$timeout" || fail \
      "Worker changed or resumed during shutdown and could not be stopped safely"
  fi
  log "stack stopped; PostgreSQL volume, journals, models, caches, and artifacts were retained"
}

status_stack() {
  "$WORKER_CTL" status || true
  compose ps --all
}

usage() {
  printf 'Usage: %s {start|stop|restart|status|config|logs}\n' "$0" >&2
  exit 2
}

main() {
  [[ $# -eq 1 ]] || usage
  local command="$1"
  case "$command" in
    start|stop|restart|status|config|logs) ;;
    *) usage ;;
  esac

  command -v docker >/dev/null 2>&1 || fail "docker is required"
  [[ -x "$WORKER_CTL" ]] || fail "worker controller is not executable: $WORKER_CTL"
  [[ -f "$COMPOSE_FILE" ]] || fail "compose file is missing: $COMPOSE_FILE"
  load_env
  cd "$REPO_ROOT"

  case "$command" in
    start) start_stack ;;
    stop) stop_stack ;;
    restart) assert_full_stack_gate; stop_stack; start_stack ;;
    status) status_stack ;;
    config) compose config ;;
    logs) compose logs --follow ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi

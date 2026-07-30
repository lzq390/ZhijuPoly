#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -P "$SCRIPT_DIR/.." && pwd -P)"
PRODUCTION_REPO_ROOT="/data/lzq/gith/nexpoly"
COMPOSE_FILE="$REPO_ROOT/docker-compose.monomer-dft-dev.yml"
ENV_FILE="$REPO_ROOT/.env.monomer-dft.dev"
WORKER_CTL="$SCRIPT_DIR/monomer_dft_worker_ctl.sh"
FORMAL_ENV_PARSER="$SCRIPT_DIR/monomer_dft_acceptance_env.py"
GPU_AUTHORITY_VALIDATOR="$REPO_ROOT/gpu_resource/authority.py"
COMPOSE_ENV_FILE="$ENV_FILE"
PROJECT_NAME="${NEXPOLY_DFT_PROJECT_NAME:-nexpoly_dft_dev}"
ACCEPTANCE_PROJECT_NAME="${NEXPOLY_DFT_ACCEPTANCE_PROJECT_NAME:-}"
ACCEPTANCE_AUTHORITY_SHA="${NEXPOLY_DFT_AUTHORITY_SHA:-}"
ACCEPTANCE_IMAGE_MODE="${NEXPOLY_DFT_ACCEPTANCE_IMAGE_MODE:-}"
ACCEPTANCE_BACKEND_IMAGE_REF="${NEXPOLY_DFT_BACKEND_IMAGE_REF:-}"
ACCEPTANCE_WEB_IMAGE_REF="${NEXPOLY_DFT_WEB_IMAGE_REF:-}"
ACCEPTANCE_JOB_ROOT="${NEXPOLY_DFT_ACCEPTANCE_JOB_ROOT:-}"
MIGRATIONS_DIR="$REPO_ROOT/backend/migrations/postgres"
DOWNLOAD_SPOOL_DIR="$REPO_ROOT/.runtime/monomer-dft-download-spool"
FORMAL_ENV_KEY_COUNT=46
FORMAL_ENV_KEYSET_SHA256="b907c16be79050f75ff2a60ed13dad1452aa095bfd57833cd43a2735032e4389"

log() {
  printf '[monomer-dft-dev] %s\n' "$*"
}

fail() {
  log "$*" >&2
  exit 2
}

trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

[[ "$REPO_ROOT" != "$PRODUCTION_REPO_ROOT" ]] || fail \
  "development DFT control is forbidden in the production repository"

load_formal_env() {
  local key=""
  local value=""
  local pending_key=""
  local token=""
  local token_count=0
  local invalid_output=0
  local parser_pid=""
  local parser_fd=""
  local coproc_read_fd=""
  local parser_status=0
  local keyset_digest=""
  local -a allowed_keys=(
    AIMNET_CACHE_DIR AIMNET_MODEL_SOURCE_DIR AIMNET_SOURCE_CLONE
    AIMNET_SOURCE_DIR AIMNET_SOURCE_LOCK CUDA_DEVICE_ORDER
    MONOMER_DFT_ARTIFACT_RETENTION_DAYS MONOMER_DFT_DEPLOYMENT
    MONOMER_DFT_JOB_RETENTION_DAYS MONOMER_DFT_JOB_RETENTION_ENABLED
    MONOMER_DFT_DOWNLOAD_MAX_CONCURRENT MONOMER_DFT_DOWNLOAD_SPOOL_ROOT
    MONOMER_DFT_DRAIN_TIMEOUT_SECONDS
    MONOMER_DFT_FATAL_RESTART_BACKOFF_SECONDS
    MONOMER_DFT_FATAL_RESTART_MAX_ATTEMPTS
    MONOMER_DFT_FATAL_RESTART_MAX_BACKOFF_SECONDS
    MONOMER_DFT_FATAL_RESTART_RESET_SECONDS
    MONOMER_DFT_GPU_ACTIVE_THREAD_PERCENTAGE
    MONOMER_DFT_GPU_BROKER_ENABLED MONOMER_DFT_GPU_BROKER_UDS
    MONOMER_DFT_GPU_BUDGET_MIB MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS
    MONOMER_DFT_GPU_MPS_PIPE_ROOT MONOMER_DFT_JOB_ROOT
    MONOMER_DFT_MAX_CONCURRENT_JOBS MONOMER_DFT_MAX_QUEUED_JOBS
    MONOMER_DFT_OPTIMIZATION_TIMEOUT_SECONDS MONOMER_DFT_PYTHON
    MONOMER_DFT_RECONCILE_INTERVAL_SECONDS
    MONOMER_DFT_SINGLE_POINT_TIMEOUT_SECONDS
    MONOMER_DFT_STANDALONE_GPU_SMOKE MONOMER_DFT_VALIDATION_CONCURRENCY
    MONOMER_DFT_WORKER_TIMEOUT_SECONDS MONOMER_DFT_WORKER_UDS
    NEXPOLY_DFT_BACKEND_PORT NEXPOLY_DFT_FRONTEND_PORT
    NEXPOLY_DFT_GPU_DEVICE NEXPOLY_DFT_OVERFLOW_GPU_DEVICES
    NEXPOLY_DFT_POSTGRES_PASSWORD NEXPOLY_DFT_POSTGRES_PORT
    NEXPOLY_DFT_PROJECT_NAME PYTHONDONTWRITEBYTECODE PYTHONNOUSERSITE
    PYTHONPATH UV_CACHE_DIR WARP_CACHE_PATH
  )
  local -A allowed=()
  local -A seen=()
  local -A parsed=()
  PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  export PATH
  [[ -f "$FORMAL_ENV_PARSER" && ! -L "$FORMAL_ENV_PARSER" ]] || fail \
    "formal acceptance dotenv parser is missing or unsafe"
  for key in "${allowed_keys[@]}"; do
    allowed["$key"]=1
  done
  coproc FORMAL_ENV_COPROC {
    exec /usr/bin/python3 -I -S "$FORMAL_ENV_PARSER" \
      --env-file "$ENV_FILE"
  }
  parser_pid="$FORMAL_ENV_COPROC_PID"
  coproc_read_fd="${FORMAL_ENV_COPROC[0]}"
  exec {parser_fd}<&"$coproc_read_fd"
  while IFS= read -r -d '' token <&"$parser_fd"; do
    ((token_count += 1))
    if (( token_count % 2 == 1 )); then
      pending_key="$token"
      if [[ ! "$pending_key" =~ ^[A-Z][A-Z0-9_]*$ ]] ||
        [[ -z "${allowed[$pending_key]:-}" ]] ||
        [[ -n "${seen[$pending_key]:-}" ]]; then
        invalid_output=1
      else
        seen["$pending_key"]=1
      fi
    else
      if [[ "$invalid_output" == "0" && -n "$pending_key" ]]; then
        parsed["$pending_key"]="$token"
      elif [[ -n "$pending_key" && -n "${seen[$pending_key]:-}" ]]; then
        parsed["$pending_key"]="$token"
      fi
      pending_key=""
    fi
  done
  exec {parser_fd}<&-
  if wait "$parser_pid"; then
    parser_status=0
  else
    parser_status=$?
  fi
  [[ "$parser_status" == "0" ]] || fail \
    "formal acceptance dotenv validation failed"
  [[ "$invalid_output" == "0" && $((token_count % 2)) == 0 &&
    "$token_count" == "$((FORMAL_ENV_KEY_COUNT * 2))" &&
    "${#seen[@]}" == "$FORMAL_ENV_KEY_COUNT" &&
    "${#parsed[@]}" == "$FORMAL_ENV_KEY_COUNT" ]] || fail \
    "formal acceptance parser emitted an invalid token inventory"
  keyset_digest="$(
    printf '%s\0' "${!seen[@]}" |
      /usr/bin/sort -z |
      /usr/bin/sha256sum |
      /usr/bin/cut -d ' ' -f1
  )"
  [[ "$keyset_digest" == "$FORMAL_ENV_KEYSET_SHA256" ]] || fail \
    "formal acceptance parser emitted the wrong key set"
  for key in "${allowed_keys[@]}"; do
    value="${parsed[$key]}"
    printf -v "$key" '%s' "$value"
    export "$key"
  done
  COMPOSE_ENV_FILE=/dev/null
}

reject_formal_control_environment() {
  local unsafe_name=""
  local exported_name=""
  local -a unsafe_environment_names=(
    BASH_ENV ENV CDPATH GLOBIGNORE
    LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT LD_DEBUG LD_PROFILE
    PYTHONHOME PYTHONSTARTUP PYTHONINSPECT PYTHONWARNINGS
    HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY
    http_proxy https_proxy all_proxy no_proxy
    DOCKER_CONTEXT DOCKER_CONFIG DOCKER_TLS_VERIFY DOCKER_CERT_PATH
  )
  for unsafe_name in "${unsafe_environment_names[@]}"; do
    [[ -z "${!unsafe_name:-}" ]] || fail \
      "fresh acceptance environment contains forbidden $unsafe_name"
  done
  while IFS= read -r exported_name; do
    case "$exported_name" in
      COMPOSE_*|GIT_*)
        [[ -z "${!exported_name:-}" ]] || fail \
          "fresh acceptance environment contains forbidden $exported_name"
        ;;
    esac
  done < <(compgen -e)
}

configure_formal_gpu_authority() {
  local -a authority_names=(
    NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY
    NEXPOLY_DFT_GPU_AUTHORITY_PID
    NEXPOLY_DFT_GPU_AUTHORITY_START_TICKS
    NEXPOLY_DFT_GPU_AUTHORITY_ROOT
    NEXPOLY_DFT_GPU_AUTHORITY_ROOT_IDENTITY
    NEXPOLY_DFT_GPU_RESERVATIONS_AUTHORITY
    NEXPOLY_DFT_GPU_RESERVATIONS_IDENTITY
    NEXPOLY_DFT_GPU_RESERVATIONS_SHA256
    NEXPOLY_DFT_GPU1_MPS_PIPE_AUTHORITY
    NEXPOLY_DFT_GPU1_MPS_PIPE_IDENTITY
    NEXPOLY_DFT_GPU3_MPS_PIPE_AUTHORITY
    NEXPOLY_DFT_GPU3_MPS_PIPE_IDENTITY
  )
  local name=""
  if [[ "${NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY:-}" != "1" ]]; then
    for name in "${authority_names[@]}"; do
      [[ -z "${!name:-}" ]] || fail \
        "partial GPU descriptor authority is forbidden"
    done
    [[ "$PROJECT_NAME" != nexpoly_dft_fresh_* ]] || fail \
      "fresh acceptance requires GPU descriptor authority"
    return 0
  fi
  [[ "$PROJECT_NAME" == nexpoly_dft_fresh_* &&
    "${NEXPOLY_DFT_FORMAL_ACCEPTANCE:-0}" == "1" ]] || fail \
    "GPU descriptor authority is restricted to fresh formal acceptance"
  [[ -f "$GPU_AUTHORITY_VALIDATOR" &&
    ! -L "$GPU_AUTHORITY_VALIDATOR" ]] || fail \
    "GPU descriptor authority validator is unavailable"
  for name in "${authority_names[@]}"; do
    export "$name"
  done
  MONOMER_DFT_GPU_BROKER_UDS="${NEXPOLY_DFT_GPU_AUTHORITY_ROOT}/broker.sock"
  MONOMER_DFT_GPU_MPS_PIPE_ROOT="$NEXPOLY_DFT_GPU_AUTHORITY_ROOT"
  MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS="$NEXPOLY_DFT_GPU_RESERVATIONS_AUTHORITY"
  export MONOMER_DFT_GPU_BROKER_UDS MONOMER_DFT_GPU_MPS_PIPE_ROOT
  export MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS
  /usr/bin/python3 -I -S "$GPU_AUTHORITY_VALIDATOR" \
    --expected-reservations-file \
    "$REPO_ROOT/ops/config/gpu-external-reservations.json" \
    --expected-root "$REPO_ROOT/.runtime/gpu-resource" ||
    fail "GPU descriptor authority validation failed"
}

load_env() {
  local requested_acceptance_project="$ACCEPTANCE_PROJECT_NAME"
  local requested_authority_sha="$ACCEPTANCE_AUTHORITY_SHA"
  local requested_image_mode="$ACCEPTANCE_IMAGE_MODE"
  local requested_backend_image_ref="$ACCEPTANCE_BACKEND_IMAGE_REF"
  local requested_web_image_ref="$ACCEPTANCE_WEB_IMAGE_REF"
  local requested_job_root="$ACCEPTANCE_JOB_ROOT"
  unset NEXPOLY_DFT_ACCEPTANCE_JOB_ROOT
  [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail "missing safe environment file: $ENV_FILE"
  [[ "$(stat -c '%u' "$ENV_FILE")" == "$(id -u)" ]] || fail "environment file must be owned by uid $(id -u)"
  [[ "$(stat -c '%a' "$ENV_FILE")" == "600" ]] || fail "environment file permissions must be 0600"
  if [[ -n "$requested_acceptance_project" ]]; then
    load_formal_env
  else
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
  fi
  # The owner-private environment file normally pins ``nexpoly_dft_dev``.
  # Real acceptance must instead get a one-run Compose namespace chosen by
  # the clean-F harness.  Apply that narrow override *after* sourcing the
  # file, and never permit it to select the ordinary development project.
  if [[ -n "$requested_acceptance_project" ]]; then
    [[ "$requested_acceptance_project" =~ ^nexpoly_dft_fresh_[a-z0-9][a-z0-9_-]{0,40}$ ]] || fail \
      "NEXPOLY_DFT_ACCEPTANCE_PROJECT_NAME must name a fresh acceptance project"
    NEXPOLY_DFT_PROJECT_NAME="$requested_acceptance_project"
  fi
  PROJECT_NAME="${NEXPOLY_DFT_PROJECT_NAME:-nexpoly_dft_dev}"
  if [[ -z "$requested_acceptance_project" && "$PROJECT_NAME" == nexpoly_dft_fresh_* ]]; then
    fail "fresh acceptance project requires the formal non-executing dotenv path"
  fi
  [[ "$PROJECT_NAME" =~ ^nexpoly_dft_(dev|fresh_[a-z0-9][a-z0-9_-]{0,40})$ ]] || fail \
    "NEXPOLY_DFT_PROJECT_NAME must be the dev project or a fresh dev acceptance project"
  export NEXPOLY_DFT_PROJECT_NAME="$PROJECT_NAME"
  if [[ "$PROJECT_NAME" == nexpoly_dft_fresh_* ]]; then
    export NEXPOLY_DFT_FORMAL_ACCEPTANCE=1
    [[ "${DOCKER_HOST:-}" == "unix:///var/run/docker.sock" ]] || fail \
      "fresh acceptance must use the fixed local Docker socket"
    reject_formal_control_environment
    export DOCKER_HOST=unix:///var/run/docker.sock
    [[ "$requested_authority_sha" =~ ^[0-9a-f]{40}$ ]] || fail \
      "fresh acceptance requires an exact NEXPOLY_DFT_AUTHORITY_SHA"
    NEXPOLY_DFT_AUTHORITY_SHA="$requested_authority_sha"
    export NEXPOLY_DFT_AUTHORITY_SHA
    [[ "$requested_image_mode" == "candidate-tree" || \
      "$requested_image_mode" == "final-main" ]] || fail \
      "fresh acceptance requires candidate-tree or final-main image mode"
    NEXPOLY_DFT_ACCEPTANCE_IMAGE_MODE="$requested_image_mode"
    if [[ "$requested_image_mode" == "final-main" ]]; then
      [[ "$requested_backend_image_ref" =~ ^ghcr\.io/lzq390/nexpoly-backend@sha256:[0-9a-f]{64}$ ]] || fail \
        "final-main Backend image must be the exact governed GHCR digest ref"
      [[ "$requested_web_image_ref" =~ ^ghcr\.io/lzq390/nexpoly-web@sha256:[0-9a-f]{64}$ ]] || fail \
        "final-main Web image must be the exact governed GHCR digest ref"
      NEXPOLY_DFT_BACKEND_IMAGE_REF="$requested_backend_image_ref"
      NEXPOLY_DFT_WEB_IMAGE_REF="$requested_web_image_ref"
      export NEXPOLY_DFT_BACKEND_IMAGE_REF NEXPOLY_DFT_WEB_IMAGE_REF
    else
      local expected_candidate_backend_image_ref=""
      local expected_candidate_web_image_ref=""
      expected_candidate_backend_image_ref="nexpoly-dft-acceptance-backend:${PROJECT_NAME}-${requested_authority_sha}"
      expected_candidate_web_image_ref="nexpoly-dft-acceptance-web:${PROJECT_NAME}-${requested_authority_sha}"
      [[ "$requested_backend_image_ref" == "$expected_candidate_backend_image_ref" ]] || fail \
        "candidate-tree Backend image must use its project/authority tag"
      [[ "$requested_web_image_ref" == "$expected_candidate_web_image_ref" ]] || fail \
        "candidate-tree Web image must use its project/authority tag"
      NEXPOLY_DFT_BACKEND_IMAGE_REF="$requested_backend_image_ref"
      NEXPOLY_DFT_WEB_IMAGE_REF="$requested_web_image_ref"
      export NEXPOLY_DFT_BACKEND_IMAGE_REF NEXPOLY_DFT_WEB_IMAGE_REF
    fi
    export NEXPOLY_DFT_ACCEPTANCE_IMAGE_MODE
  fi
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
  if [[ "$PROJECT_NAME" == nexpoly_dft_fresh_* ]]; then
    [[ -n "$requested_job_root" && "$requested_job_root" == /* ]] || fail \
      "fresh acceptance requires an absolute run-scoped Worker job root"
    [[ "$(realpath -e -- "$requested_job_root")" == "$requested_job_root" ]] || fail \
      "fresh acceptance Worker job root resolved unexpectedly"
    local job_run_directory=""
    job_run_directory="$(dirname -- "$requested_job_root")"
    [[ "$(basename -- "$requested_job_root")" == "worker-jobs" &&
      "$(basename -- "$job_run_directory")" =~ ^gpu-acceptance-[0-9]{8}T[0-9]{6}Z-[1-9][0-9]*$ &&
      "$(dirname -- "$job_run_directory")" == "$REPO_ROOT/.runtime/runs" ]] || fail \
      "fresh acceptance Worker job root is outside its exact run directory"
    local private_directory=""
    for private_directory in \
      "$REPO_ROOT/.runtime" \
      "$REPO_ROOT/.runtime/runs" \
      "$job_run_directory" \
      "$requested_job_root"; do
      [[ -d "$private_directory" && ! -L "$private_directory" ]] || fail \
        "fresh acceptance Worker job path must be a real directory: $private_directory"
      [[ "$(stat -c '%u:%a' "$private_directory")" == "$(id -u):700" ]] || fail \
        "fresh acceptance Worker job path must be owner-private: $private_directory"
    done
    MONOMER_DFT_JOB_ROOT="$requested_job_root"
    NEXPOLY_DFT_ACCEPTANCE_JOB_ROOT="$requested_job_root"
    export MONOMER_DFT_JOB_ROOT NEXPOLY_DFT_ACCEPTANCE_JOB_ROOT
  else
    [[ -z "$requested_job_root" ]] || fail \
      "run-scoped Worker job root is restricted to formal acceptance"
  fi

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
  configure_formal_gpu_authority
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
    --env-file "$COMPOSE_ENV_FILE" \
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
  if [[ "${NEXPOLY_DFT_FORMAL_ACCEPTANCE:-0}" == "1" ]]; then
    [[ "${NEXPOLY_DFT_AUTHORITY_SHA:-}" =~ ^[0-9a-f]{40}$ &&
      "$(git -C "$REPO_ROOT" rev-parse HEAD)" == \
      "$NEXPOLY_DFT_AUTHORITY_SHA" ]] || fail \
      "formal acceptance HEAD must match its exact authority SHA"
    if [[ "${NEXPOLY_DFT_ACCEPTANCE_IMAGE_MODE:-}" == "final-main" ]]; then
      [[ "$(git -C "$REPO_ROOT" rev-parse HEAD)" == \
        "$(git -C "$REPO_ROOT" rev-parse refs/remotes/origin/main)" ]] || fail \
        "final-main acceptance must run at exact origin/main"
    fi
  else
    git -C "$REPO_ROOT" show-ref --verify --quiet refs/heads/main || fail \
      "local main is unavailable"
    [[ "$(git -C "$REPO_ROOT" rev-parse refs/heads/main)" == \
      "$(git -C "$REPO_ROOT" rev-parse refs/remotes/origin/main)" ]] || fail \
      "local main must match origin/main before opening the full-stack gate"
  fi
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

  local -a image_arguments=(--no-build)
  if [[ "${NEXPOLY_DFT_ACCEPTANCE_IMAGE_MODE:-}" == "final-main" ]]; then
    # Pull and execute the already-published immutable F images.  A final
    # acceptance must never rebuild them from a mutable checkout.
    compose pull migrate backend frontend || fail \
      "final-main immutable image pull failed"
  elif ! compose build backend frontend; then
    # ``migrate`` and ``backend`` intentionally share one immutable image
    # tag.  Building during ``compose up`` lets BuildKit race both service
    # targets into that tag.  Materialize it once, then make every service
    # consume only the completed images.
    if [[ "$started_worker" == "true" ]]; then
      "$WORKER_CTL" stop || true
    elif [[ -n "$resumed_worker_instance_id" ]]; then
      rollback_resumed_worker "$resumed_worker_instance_id" || \
        log "failed to restore drain state after image build failure" >&2
    fi
    fail "DFT development images failed to build"
  fi
  if ! compose up --detach "${image_arguments[@]}" --remove-orphans; then
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

#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -P "$SCRIPT_DIR/.." && pwd -P)"
PRODUCTION_REPO_ROOT="/data/lzq/gith/nexpoly"
RUNTIME_ROOT="$REPO_ROOT/.runtime"
WORKER_DIR="$REPO_ROOT/workers/monomer_dft_worker"
RUNNER="$WORKER_DIR/run_host_worker.sh"
ENV_FILE="$REPO_ROOT/.env.monomer-dft.dev"
FORMAL_ENV_PARSER="$SCRIPT_DIR/monomer_dft_acceptance_env.py"
FORMAL_ACCEPTANCE="${NEXPOLY_DFT_FORMAL_ACCEPTANCE:-0}"
FORMAL_PROJECT_NAME="${NEXPOLY_DFT_PROJECT_NAME:-}"
FORMAL_AUTHORITY_SHA="${NEXPOLY_DFT_AUTHORITY_SHA:-}"
GPU_AUTHORITY_VALIDATOR="$REPO_ROOT/gpu_resource/authority.py"
PID_FILE="$RUNTIME_ROOT/monomer-dft-worker.pid"
LOG_FILE="$RUNTIME_ROOT/monomer-dft-worker.log"
LOCK_FILE="$RUNTIME_ROOT/monomer-dft-worker.ctl.lock"
PREFLIGHT="$REPO_ROOT/scripts/preflight_monomer_dft_env.py"
GPU_RUNTIME_ROOT="$RUNTIME_ROOT/gpu-resource"
PRIVATE_HOME="$RUNTIME_ROOT/home"
PRIVATE_TMPDIR="$RUNTIME_ROOT/tmp"
PRIVATE_XDG_CACHE="$RUNTIME_ROOT/xdg-cache"
GPU_SCOPE_XDG_RUNTIME_DIR=""
GPU_SCOPE_DBUS_ADDRESS=""
FORMAL_ENV_KEY_COUNT=44
FORMAL_ENV_KEYSET_SHA256="1e27ea88df12273bdaf31f448cc8366e500db644535a555515bf9a815a1cf90a"

REAL_RUNTIME_ROOT=""
START_TIMEOUT=""
SPAWN_PID=""
SPAWN_START_TICKS=""
SPAWN_PID_TMP=""
SPAWN_CLEANUP_ACTIVE=false
SPAWN_CLEANUP_DONE=false

log() {
  printf '[monomer-dft-worker-ctl] %s\n' "$*"
}

fail() {
  log "$*" >&2
  exit 2
}

trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

[[ "$REPO_ROOT" != "$PRODUCTION_REPO_ROOT" ]] || fail \
  "development DFT worker control is forbidden in the production repository"
[[ "$FORMAL_ACCEPTANCE" == "0" || "$FORMAL_ACCEPTANCE" == "1" ]] || fail \
  "NEXPOLY_DFT_FORMAL_ACCEPTANCE must be 0 or 1"

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
      "formal Worker environment contains forbidden $unsafe_name"
  done
  while IFS= read -r exported_name; do
    case "$exported_name" in
      COMPOSE_*|GIT_*)
        [[ -z "${!exported_name:-}" ]] || fail \
          "formal Worker environment contains forbidden $exported_name"
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
    return 0
  fi
  [[ "$FORMAL_ACCEPTANCE" == "1" &&
    "$FORMAL_PROJECT_NAME" == nexpoly_dft_fresh_* &&
    "$FORMAL_AUTHORITY_SHA" =~ ^[0-9a-f]{40}$ ]] || fail \
    "GPU descriptor authority lacks fresh acceptance identity"
  NEXPOLY_DFT_PROJECT_NAME="$FORMAL_PROJECT_NAME"
  NEXPOLY_DFT_AUTHORITY_SHA="$FORMAL_AUTHORITY_SHA"
  export NEXPOLY_DFT_PROJECT_NAME NEXPOLY_DFT_AUTHORITY_SHA
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
    --expected-root "$GPU_RUNTIME_ROOT" ||
    fail "GPU descriptor authority validation failed"
}

absolute_runtime_path() {
  local value="$1"
  if [[ "$value" != /* ]]; then
    value="$REPO_ROOT/$value"
  fi
  realpath -ms -- "$value"
}

assert_runtime_path() {
  local name="$1"
  local value="$2"
  [[ "$value" == "$RUNTIME_ROOT" || "$value" == "$RUNTIME_ROOT"/* ]] || fail "$name must be below $RUNTIME_ROOT"
}

initialize_runtime_root() {
  [[ ! -L "$RUNTIME_ROOT" ]] || fail "runtime root must not be a symlink: $RUNTIME_ROOT"
  if [[ ! -e "$RUNTIME_ROOT" ]]; then
    mkdir -- "$RUNTIME_ROOT"
  fi
  [[ -d "$RUNTIME_ROOT" && ! -L "$RUNTIME_ROOT" ]] || fail "runtime root must be a real directory: $RUNTIME_ROOT"
  [[ "$(stat -c '%u' "$RUNTIME_ROOT")" == "$(id -u)" ]] || fail \
    "runtime root must be owned by uid $(id -u): $RUNTIME_ROOT"
  chmod 700 -- "$RUNTIME_ROOT"
  REAL_RUNTIME_ROOT="$(realpath -e -- "$RUNTIME_ROOT")"
  [[ "$REAL_RUNTIME_ROOT" == "$RUNTIME_ROOT" ]] || fail "runtime root resolves unexpectedly: $REAL_RUNTIME_ROOT"
}

ensure_runtime_directory() {
  local name="$1"
  local value="$2"
  local create_missing="$3"
  assert_runtime_path "$name" "$value"

  if [[ "$value" == "$RUNTIME_ROOT" ]]; then
    [[ -d "$value" && ! -L "$value" && "$(realpath -e -- "$value")" == "$REAL_RUNTIME_ROOT" ]] || fail "$name has an unsafe runtime root"
    return 0
  fi

  local relative="${value#"$RUNTIME_ROOT"/}"
  local current="$RUNTIME_ROOT"
  local component candidate resolved
  IFS='/' read -r -a components <<< "$relative"
  for component in "${components[@]}"; do
    [[ -n "$component" && "$component" != "." && "$component" != ".." ]] || fail "$name contains an unsafe path component: $value"
    candidate="$current/$component"
    [[ ! -L "$candidate" ]] || fail "$name contains a symlink component: $candidate"
    if [[ ! -e "$candidate" ]]; then
      [[ "$create_missing" == "true" ]] || fail "$name directory is missing: $candidate"
      mkdir -- "$candidate"
    fi
    [[ -d "$candidate" && ! -L "$candidate" ]] || fail "$name must contain only real directories: $candidate"
    [[ "$(stat -c '%u' "$candidate")" == "$(id -u)" ]] || fail \
      "$name contains a directory not owned by uid $(id -u): $candidate"
    chmod 700 -- "$candidate"
    resolved="$(realpath -e -- "$candidate")"
    [[ "$resolved" == "$REAL_RUNTIME_ROOT"/* ]] || fail "$name escapes $REAL_RUNTIME_ROOT: $resolved"
    current="$candidate"
  done
}

assert_safe_runtime_file() {
  local name="$1"
  local value="$2"
  ensure_runtime_directory "$name parent" "$(dirname "$value")" false
  [[ ! -L "$value" ]] || fail "$name must not be a symlink: $value"
  [[ ! -e "$value" || -f "$value" ]] || fail "$name must be a regular file: $value"
}

assert_safe_socket_path() {
  ensure_runtime_directory "MONOMER_DFT_WORKER_UDS parent" "$(dirname "$MONOMER_DFT_WORKER_UDS")" false
  [[ ! -L "$MONOMER_DFT_WORKER_UDS" ]] || fail "worker socket must not be a symlink: $MONOMER_DFT_WORKER_UDS"
  [[ ! -e "$MONOMER_DFT_WORKER_UDS" || -S "$MONOMER_DFT_WORKER_UDS" ]] || fail "worker socket path contains a non-socket: $MONOMER_DFT_WORKER_UDS"
}

secure_socket_permissions() {
  local mode=""
  assert_safe_socket_path
  # The runner can remove a stale UDS between the caller's existence check
  # and this hardening step while an executor restart is in progress. Treat
  # only that exact transient absence as retryable; symlinks and non-sockets
  # remain fatal.
  [[ -S "$MONOMER_DFT_WORKER_UDS" ]] || return 1
  if ! chmod 600 -- "$MONOMER_DFT_WORKER_UDS"; then
    [[ ! -e "$MONOMER_DFT_WORKER_UDS" && ! -L "$MONOMER_DFT_WORKER_UDS" ]] &&
      return 1
    fail "worker socket permissions could not be hardened: $MONOMER_DFT_WORKER_UDS"
  fi
  if [[ ! -e "$MONOMER_DFT_WORKER_UDS" && ! -L "$MONOMER_DFT_WORKER_UDS" ]]; then
    return 1
  fi
  assert_safe_socket_path
  if ! mode="$(stat -c '%a' "$MONOMER_DFT_WORKER_UDS")"; then
    [[ ! -e "$MONOMER_DFT_WORKER_UDS" && ! -L "$MONOMER_DFT_WORKER_UDS" ]] &&
      return 1
    fail "worker socket permissions could not be verified: $MONOMER_DFT_WORKER_UDS"
  fi
  [[ "$mode" == "600" ]] ||
    fail "worker socket permissions must be 0600: $MONOMER_DFT_WORKER_UDS"
}

load_env() {
  local required="$1"
  [[ ! -L "$ENV_FILE" ]] || fail "environment file must not be a symlink: $ENV_FILE"
  if [[ ! -f "$ENV_FILE" ]]; then
    [[ "$required" == "false" ]] && return 0
    fail "environment file is missing: $ENV_FILE"
  fi
  [[ "$(realpath -e -- "$(dirname "$ENV_FILE")")" == "$REPO_ROOT" ]] || fail "environment file parent resolves outside the worktree"
  [[ "$(stat -c '%u' "$ENV_FILE")" == "$(id -u)" ]] || fail "environment file must be owned by uid $(id -u): $ENV_FILE"
  [[ "$(stat -c '%a' "$ENV_FILE")" == "600" ]] || fail "environment file permissions must be 0600: $ENV_FILE"
  if [[ "$FORMAL_ACCEPTANCE" == "1" ]]; then
    load_formal_env
    reject_formal_control_environment
  else
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
  fi
}

validate_dev_selection() {
  [[ "${MONOMER_DFT_DEPLOYMENT:-dev}" == "dev" ]] || fail \
    "MONOMER_DFT_DEPLOYMENT must be exactly dev; production mode is forbidden"
  [[ "${NEXPOLY_DFT_GPU_DEVICE:-1}" == "1" ]] || fail \
    "development primary GPU must be physical GPU 1; GPUs 0 and 2 are forbidden"
  [[ "${NEXPOLY_DFT_OVERFLOW_GPU_DEVICES:-3}" == "3" ]] || fail \
    "development overflow GPU must be physical GPU 3 only; GPUs 0 and 2 are forbidden"
  [[ "${MONOMER_DFT_GPU_BROKER_ENABLED:-1}" =~ ^[01]$ ]] || fail \
    "MONOMER_DFT_GPU_BROKER_ENABLED must be 0 or 1"
  [[ "${MONOMER_DFT_STANDALONE_GPU_SMOKE:-0}" =~ ^[01]$ ]] || fail \
    "MONOMER_DFT_STANDALONE_GPU_SMOKE must be 0 or 1"
  if [[ "${MONOMER_DFT_GPU_BROKER_ENABLED:-1}" == "1" ]]; then
    [[ "${MONOMER_DFT_STANDALONE_GPU_SMOKE:-0}" == "0" ]] || fail \
      "Broker-managed development mode cannot enable standalone smoke"
  else
    [[ "${MONOMER_DFT_STANDALONE_GPU_SMOKE:-0}" == "1" ]] || fail \
      "Broker-disabled development mode requires explicit standalone smoke"
  fi
  export MONOMER_DFT_DEPLOYMENT=dev
  export NEXPOLY_DFT_GPU_DEVICE=1
  export NEXPOLY_DFT_OVERFLOW_GPU_DEVICES=3
}

configure_paths() {
  validate_dev_selection
  configure_formal_gpu_authority
  MONOMER_DFT_PYTHON="$(absolute_runtime_path "${MONOMER_DFT_PYTHON:-.runtime/venvs/monomer-dft-worker/bin/python}")"
  MONOMER_DFT_WORKER_UDS="$(absolute_runtime_path "${MONOMER_DFT_WORKER_UDS:-.runtime/monomer-dft-worker-socket/worker.sock}")"
  MONOMER_DFT_JOB_ROOT="$(absolute_runtime_path "${MONOMER_DFT_JOB_ROOT:-.runtime/monomer-dft-worker-runs}")"
  if [[ "${NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY:-}" != "1" ]]; then
    MONOMER_DFT_GPU_BROKER_UDS="$(absolute_runtime_path "${MONOMER_DFT_GPU_BROKER_UDS:-.runtime/gpu-resource/broker.sock}")"
    MONOMER_DFT_GPU_MPS_PIPE_ROOT="$(absolute_runtime_path "${MONOMER_DFT_GPU_MPS_PIPE_ROOT:-.runtime/gpu-resource}")"
    MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS="$(absolute_runtime_path "${MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS:-.runtime/gpu-resource/external-reservations.json}")"
  fi
  AIMNET_CACHE_DIR="$(absolute_runtime_path "${AIMNET_CACHE_DIR:-.runtime/aimnet-cache}")"
  WARP_CACHE_PATH="$(absolute_runtime_path "${WARP_CACHE_PATH:-.runtime/warp-cache}")"
  UV_CACHE_DIR="$(absolute_runtime_path "${UV_CACHE_DIR:-.runtime/uv-cache}")"
  AIMNET_SOURCE_DIR="$(absolute_runtime_path "${AIMNET_SOURCE_DIR:-.runtime/aimnet-source-archive}")"
  assert_runtime_path MONOMER_DFT_PYTHON "$MONOMER_DFT_PYTHON"
  assert_runtime_path MONOMER_DFT_WORKER_UDS "$MONOMER_DFT_WORKER_UDS"
  assert_runtime_path MONOMER_DFT_JOB_ROOT "$MONOMER_DFT_JOB_ROOT"
  if [[ "${NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY:-}" != "1" ]]; then
    assert_runtime_path MONOMER_DFT_GPU_BROKER_UDS "$MONOMER_DFT_GPU_BROKER_UDS"
    assert_runtime_path MONOMER_DFT_GPU_MPS_PIPE_ROOT "$MONOMER_DFT_GPU_MPS_PIPE_ROOT"
    assert_runtime_path MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS "$MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS"
  fi
  assert_runtime_path AIMNET_CACHE_DIR "$AIMNET_CACHE_DIR"
  assert_runtime_path WARP_CACHE_PATH "$WARP_CACHE_PATH"
  assert_runtime_path UV_CACHE_DIR "$UV_CACHE_DIR"
  assert_runtime_path AIMNET_SOURCE_DIR "$AIMNET_SOURCE_DIR"
  [[ "$MONOMER_DFT_JOB_ROOT" == "$RUNTIME_ROOT/monomer-dft-worker-runs" ]] || fail \
    "job root must use the fixed development runtime path"
  if [[ "${NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY:-}" != "1" ]]; then
    [[ "$MONOMER_DFT_GPU_BROKER_UDS" == "$GPU_RUNTIME_ROOT/broker.sock" ]] || fail \
      "GPU Broker socket must use the current development worktree"
    [[ "$MONOMER_DFT_GPU_MPS_PIPE_ROOT" == "$GPU_RUNTIME_ROOT" ]] || fail \
      "MPS root must use the current development worktree"
    [[ "$MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS" == "$GPU_RUNTIME_ROOT/external-reservations.json" ]] || fail \
      "GPU reservations must use the current development worktree"
  fi
  [[ "$AIMNET_CACHE_DIR" == "$RUNTIME_ROOT/aimnet-cache" ]] || fail \
    "AIMNet cache must use the current development worktree"
  [[ "$WARP_CACHE_PATH" == "$RUNTIME_ROOT/warp-cache" ]] || fail \
    "Warp cache must use the current development worktree"
  [[ "$UV_CACHE_DIR" == "$RUNTIME_ROOT/uv-cache" ]] || fail \
    "uv cache must use the current development worktree"
  [[ "$AIMNET_SOURCE_DIR" == "$RUNTIME_ROOT/aimnet-source-archive" ]] || fail \
    "AIMNet archive must use the current development worktree"
  export MONOMER_DFT_PYTHON MONOMER_DFT_WORKER_UDS MONOMER_DFT_JOB_ROOT
  export MONOMER_DFT_GPU_BROKER_UDS MONOMER_DFT_GPU_MPS_PIPE_ROOT
  export MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS
  export AIMNET_CACHE_DIR WARP_CACHE_PATH UV_CACHE_DIR AIMNET_SOURCE_DIR
}

validate_start_configuration() {
  [[ -f "$RUNNER" && ! -L "$RUNNER" && -x "$RUNNER" ]] || fail "worker runner is missing, unsafe, or not executable: $RUNNER"
  [[ -f "$PREFLIGHT" && ! -L "$PREFLIGHT" ]] || fail "preflight script is missing or unsafe: $PREFLIGHT"
  ensure_runtime_directory "MONOMER_DFT_PYTHON parent" "$(dirname "$MONOMER_DFT_PYTHON")" false
  [[ -f "$MONOMER_DFT_PYTHON" && -x "$MONOMER_DFT_PYTHON" ]] || fail "MONOMER_DFT_PYTHON is not executable: $MONOMER_DFT_PYTHON"
  ensure_runtime_directory "MONOMER_DFT_WORKER_UDS parent" "$(dirname "$MONOMER_DFT_WORKER_UDS")" false
  ensure_runtime_directory "private HOME" "$PRIVATE_HOME" true
  ensure_runtime_directory "private TMPDIR" "$PRIVATE_TMPDIR" true
  ensure_runtime_directory "private XDG cache" "$PRIVATE_XDG_CACHE" true
  [[ ! -L "$MONOMER_DFT_WORKER_UDS" ]] || fail "worker socket must not be a symlink: $MONOMER_DFT_WORKER_UDS"
  [[ "${MONOMER_DFT_MAX_CONCURRENT_JOBS:-1}" == "1" ]] || fail "MONOMER_DFT_MAX_CONCURRENT_JOBS must be 1"
  validate_dev_selection
  [[ -z "${PYTHONPATH:-}" ]] || fail "PYTHONPATH must be empty for the isolated worker"
  if [[ "${MONOMER_DFT_GPU_BROKER_ENABLED:-1}" == "1" ]]; then
    GPU_SCOPE_XDG_RUNTIME_DIR="/run/user/$(id -u)"
    GPU_SCOPE_DBUS_ADDRESS="unix:path=$GPU_SCOPE_XDG_RUNTIME_DIR/bus"
    [[ -d "$GPU_SCOPE_XDG_RUNTIME_DIR" && ! -L "$GPU_SCOPE_XDG_RUNTIME_DIR" ]] ||
      fail "systemd user runtime directory is unavailable"
    [[ "$(stat -c '%u:%a' "$GPU_SCOPE_XDG_RUNTIME_DIR")" == "$(id -u):700" ]] ||
      fail "systemd user runtime directory identity is unsafe"
    [[ -S "$GPU_SCOPE_XDG_RUNTIME_DIR/bus" && ! -L "$GPU_SCOPE_XDG_RUNTIME_DIR/bus" ]] ||
      fail "systemd user bus is unavailable"
    [[ "$(stat -c '%u' "$GPU_SCOPE_XDG_RUNTIME_DIR/bus")" == "$(id -u)" ]] ||
      fail "systemd user bus identity is unsafe"
    [[ -x /usr/bin/systemd-run && ! -L /usr/bin/systemd-run ]] ||
      fail "audited systemd-run launcher is unavailable"
    [[ "$(stat -c '%u' /usr/bin/systemd-run)" == "0" ]] ||
      fail "audited systemd-run launcher owner is unsafe"
    [[ "$((8#$(stat -c '%a' /usr/bin/systemd-run) & 8#022))" == "0" ]] ||
      fail "audited systemd-run launcher mode is unsafe"
  fi
  (( ${#MONOMER_DFT_WORKER_UDS} <= 107 )) || fail "MONOMER_DFT_WORKER_UDS exceeds the Linux Unix-socket path limit"
  START_TIMEOUT="${MONOMER_DFT_START_TIMEOUT_SECONDS:-60}"
  [[ "$START_TIMEOUT" =~ ^[0-9]+$ && "$START_TIMEOUT" -ge 1 ]] || fail "MONOMER_DFT_START_TIMEOUT_SECONDS must be a positive integer"
  assert_safe_runtime_file "PID file" "$PID_FILE"
  assert_safe_runtime_file "log file" "$LOG_FILE"
  assert_safe_runtime_file "control lock file" "$LOCK_FILE"
}

read_pid_state() {
  MANAGED_PID=""
  MANAGED_START_TICKS=""
  local extra=""
  assert_safe_runtime_file "PID file" "$PID_FILE"
  [[ -f "$PID_FILE" ]] || return 1
  read -r MANAGED_PID MANAGED_START_TICKS extra < "$PID_FILE" || return 1
  [[ -z "$extra" && "$MANAGED_PID" =~ ^[0-9]+$ && "$MANAGED_START_TICKS" =~ ^[0-9]+$ ]]
}

pid_state_matches() {
  local expected_pid="$1"
  local expected_start_ticks="$2"
  local pid="" start_ticks="" extra=""
  assert_safe_runtime_file "PID file" "$PID_FILE"
  [[ -f "$PID_FILE" ]] || return 1
  read -r pid start_ticks extra < "$PID_FILE" || return 1
  [[ -z "$extra" && "$pid" == "$expected_pid" && "$start_ticks" == "$expected_start_ticks" ]]
}

atomic_write_pid_state() {
  local pid="$1"
  local start_ticks="$2"
  assert_safe_runtime_file "PID file" "$PID_FILE"
  [[ ! -e "$PID_FILE" ]] || fail "refusing to overwrite existing PID file: $PID_FILE"
  SPAWN_PID_TMP="$(mktemp "$RUNTIME_ROOT/.monomer-dft-worker.pid.tmp.XXXXXX")"
  [[ "$(dirname "$SPAWN_PID_TMP")" == "$RUNTIME_ROOT" && -f "$SPAWN_PID_TMP" && ! -L "$SPAWN_PID_TMP" ]] || fail "failed to create a safe PID state temporary file"
  printf '%s %s\n' "$pid" "$start_ticks" > "$SPAWN_PID_TMP"
  chmod 600 "$SPAWN_PID_TMP"
  [[ ! -L "$PID_FILE" && ! -e "$PID_FILE" ]] || fail "PID file appeared during startup: $PID_FILE"
  mv -T -- "$SPAWN_PID_TMP" "$PID_FILE"
  SPAWN_PID_TMP=""
}

process_start_ticks() {
  local pid="$1"
  [[ -r "/proc/$pid/stat" ]] || return 1
  awk '{print $22}' "/proc/$pid/stat"
}

process_is_running() {
  local pid="$1"
  local state
  kill -0 "$pid" >/dev/null 2>&1 || return 1
  [[ -r "/proc/$pid/stat" ]] || return 1
  state="$(awk '{print $3}' "/proc/$pid/stat")" || return 1
  [[ "$state" != "Z" ]]
}

process_has_instance_marker() {
  local pid="$1"
  [[ -r "/proc/$pid/environ" ]] || return 1
  tr '\0' '\n' < "/proc/$pid/environ" | grep -Fqx "MONOMER_DFT_WORKER_INSTANCE=$REPO_ROOT"
}

process_has_worker_command() {
  local pid="$1"
  local -a command_args=()
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  mapfile -d '' -t command_args < "/proc/$pid/cmdline" 2>/dev/null || return 1

  # The controller's short-lived ``python -c`` launcher carries RUNNER as a
  # data argument before execv().  A substring check can therefore accept the
  # launcher, then observe an empty/transitional /proc/cmdline during exec and
  # falsely report that the worker exited.  Require an exact post-exec argv
  # shape so PID state is not recorded until the runner is actually running.
  if (( ${#command_args[@]} >= 2 )) && [[ "${command_args[1]}" == "$RUNNER" ]]; then
    return 0
  fi
  if (( ${#command_args[@]} >= 4 )) \
      && [[ "${command_args[1]}" == "-m" ]] \
      && [[ "${command_args[2]}" == "uvicorn" ]] \
      && [[ "${command_args[3]}" == "workers.monomer_dft_worker.app.main:app" ]]; then
    return 0
  fi
  if (( ${#command_args[@]} >= 3 )) \
      && [[ "${command_args[1]##*/}" == "uvicorn" ]] \
      && [[ "${command_args[2]}" == "workers.monomer_dft_worker.app.main:app" ]]; then
    return 0
  fi
  return 1
}

is_managed_process() {
  local pid="$1"
  local expected_start_ticks="$2"
  local actual_start_ticks
  process_is_running "$pid" || return 1
  actual_start_ticks="$(process_start_ticks "$pid")" || return 1
  [[ "$actual_start_ticks" == "$expected_start_ticks" ]] || return 1
  process_has_instance_marker "$pid" || return 1
  process_has_worker_command "$pid"
}

recover_spawn_start_ticks() {
  [[ -n "$SPAWN_PID" && -z "$SPAWN_START_TICKS" ]] || return 1
  local recovered_ticks=""
  for _ in $(seq 1 40); do
    process_is_running "$SPAWN_PID" || return 1
    recovered_ticks="$(process_start_ticks "$SPAWN_PID" 2>/dev/null || true)"
    if [[ -n "$recovered_ticks" ]] && is_managed_process "$SPAWN_PID" "$recovered_ticks"; then
      SPAWN_START_TICKS="$recovered_ticks"
      log "recovered startup identity for pid $SPAWN_PID during cleanup"
      return 0
    fi
    sleep 0.01
  done
  return 1
}

socket_health() {
  assert_safe_socket_path
  curl --silent --show-error --fail --max-time 2 \
    --unix-socket "$MONOMER_DFT_WORKER_UDS" \
    http://monomer-dft-worker/health
}

socket_is_listening() {
  assert_safe_socket_path
  if command -v ss >/dev/null 2>&1; then
    ss -H -xl | awk -v socket_path="$MONOMER_DFT_WORKER_UDS" '$NF == socket_path { found = 1 } END { exit !found }'
    return
  fi
  socket_health >/dev/null 2>&1
}

remove_stale_socket() {
  assert_safe_socket_path
  [[ -e "$MONOMER_DFT_WORKER_UDS" ]] || return 0
  socket_is_listening && fail "refusing to remove a listening socket without a verified managed PID: $MONOMER_DFT_WORKER_UDS"
  rm -f -- "$MONOMER_DFT_WORKER_UDS"
}

terminate_verified_process() {
  local pid="$1"
  local start_ticks="$2"
  local label="$3"
  process_is_running "$pid" || {
    wait "$pid" 2>/dev/null || true
    return 0
  }
  if ! is_managed_process "$pid" "$start_ticks"; then
    log "$label PID $pid failed identity verification; refusing to signal it" >&2
    return 1
  fi
  kill -TERM "$pid"
  for _ in $(seq 1 80); do
    process_is_running "$pid" || break
    sleep 0.25
  done
  if process_is_running "$pid"; then
    if ! is_managed_process "$pid" "$start_ticks"; then
      log "$label PID identity changed while stopping; refusing SIGKILL" >&2
      return 1
    fi
    kill -KILL "$pid"
    for _ in $(seq 1 40); do
      process_is_running "$pid" || break
      sleep 0.05
    done
  fi
  wait "$pid" 2>/dev/null || true
  process_is_running "$pid" && return 1
  return 0
}

cleanup_startup() {
  [[ "$SPAWN_CLEANUP_ACTIVE" == "true" && "$SPAWN_CLEANUP_DONE" == "false" ]] || return 0
  SPAWN_CLEANUP_DONE=true
  trap - ERR EXIT HUP INT TERM
  log "cleaning up incomplete worker startup"

  local worker_stopped=false
  if [[ -n "$SPAWN_PID" && -z "$SPAWN_START_TICKS" ]] && process_is_running "$SPAWN_PID"; then
    recover_spawn_start_ticks ||
      log "could not recover and verify startup identity for pid $SPAWN_PID" >&2
  fi
  if [[ -n "$SPAWN_PID" && -n "$SPAWN_START_TICKS" ]]; then
    if terminate_verified_process "$SPAWN_PID" "$SPAWN_START_TICKS" "startup"; then
      worker_stopped=true
    fi
  elif [[ -n "$SPAWN_PID" ]] && ! process_is_running "$SPAWN_PID"; then
    wait "$SPAWN_PID" 2>/dev/null || true
    worker_stopped=true
  elif [[ -n "$SPAWN_PID" ]] && process_is_running "$SPAWN_PID"; then
    log "spawned PID $SPAWN_PID has no verified start time; refusing an unsafe signal" >&2
  else
    worker_stopped=true
  fi

  if [[ -n "$SPAWN_PID_TMP" ]]; then
    assert_safe_runtime_file "PID temporary file" "$SPAWN_PID_TMP"
    rm -f -- "$SPAWN_PID_TMP"
    SPAWN_PID_TMP=""
  fi
  if [[ "$worker_stopped" == "true" && -n "$SPAWN_PID" && -n "$SPAWN_START_TICKS" ]] && pid_state_matches "$SPAWN_PID" "$SPAWN_START_TICKS"; then
    rm -f -- "$PID_FILE"
  fi
  if [[ "$worker_stopped" == "true" && -n "${MONOMER_DFT_WORKER_UDS:-}" ]]; then
    assert_safe_socket_path
    if [[ -S "$MONOMER_DFT_WORKER_UDS" ]] && ! socket_is_listening; then
      rm -f -- "$MONOMER_DFT_WORKER_UDS"
    fi
  fi
  return 0
}

arm_startup_cleanup() {
  SPAWN_PID=""
  SPAWN_START_TICKS=""
  SPAWN_PID_TMP=""
  SPAWN_CLEANUP_ACTIVE=true
  SPAWN_CLEANUP_DONE=false
  trap 'cleanup_startup' ERR
  trap 'cleanup_startup' EXIT
  trap 'cleanup_startup; exit 129' HUP
  trap 'cleanup_startup; exit 130' INT
  trap 'cleanup_startup; exit 143' TERM
}

disarm_startup_cleanup() {
  SPAWN_CLEANUP_ACTIVE=false
  trap - ERR EXIT HUP INT TERM
}

run_preflight() {
  log "running isolated provenance preflight (CUDA remains executor-only)"
  if [[ "${MONOMER_DFT_GPU_BROKER_ENABLED:-1}" == "1" ]]; then
    env -u PYTHONPATH -u CUDA_VISIBLE_DEVICES \
      CUDA_DEVICE_ORDER=PCI_BUS_ID \
      "$MONOMER_DFT_PYTHON" "$PREFLIGHT"
  else
    env -u PYTHONPATH \
      CUDA_DEVICE_ORDER=PCI_BUS_ID \
      CUDA_VISIBLE_DEVICES="${NEXPOLY_DFT_GPU_DEVICE:-1}" \
      "$MONOMER_DFT_PYTHON" "$PREFLIGHT"
  fi
}

start_worker() {
  local stale_state=false
  if read_pid_state; then
    if is_managed_process "$MANAGED_PID" "$MANAGED_START_TICKS"; then
      fail "worker is already running with pid $MANAGED_PID"
    fi
    if process_is_running "$MANAGED_PID"; then
      fail "PID file points at an unverified live process; refusing to overwrite it: $PID_FILE"
    fi
    stale_state=true
    rm -f -- "$PID_FILE"
  elif [[ -e "$PID_FILE" ]]; then
    fail "PID file is malformed; refusing to overwrite it: $PID_FILE"
  fi

  assert_safe_socket_path
  if [[ -e "$MONOMER_DFT_WORKER_UDS" ]]; then
    [[ "$stale_state" == "true" ]] || fail "socket exists without a stale managed PID record: $MONOMER_DFT_WORKER_UDS"
    remove_stale_socket
  fi

  assert_safe_runtime_file "log file" "$LOG_FILE"
  : > "$LOG_FILE"
  chmod 600 "$LOG_FILE"

  arm_startup_cleanup
  local launch_dir="$PWD"
  local launcher_code='import os, signal, sys; signal.signal(signal.SIGHUP, signal.SIG_IGN); os.setsid(); os.execv(sys.argv[1], sys.argv[1:])'
  cd "$WORKER_DIR"
  # Ignore HUP across fork/exec, then let the same PID create a new session and
  # exec the runner.  There is no wrapper/fork PID for the pidfile to confuse.
  # Start from an allowlisted environment so the GPU worker cannot inherit any
  # PostgreSQL DSN/password (or unrelated platform secrets) from the controller
  # shell or the shared development env file.
  trap '' HUP
  env -i \
    HOME="$PRIVATE_HOME" \
    USER="${USER:-}" \
    LOGNAME="${LOGNAME:-}" \
    LANG="${LANG:-C.UTF-8}" \
    PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    TMPDIR="$PRIVATE_TMPDIR" \
    XDG_CACHE_HOME="$PRIVATE_XDG_CACHE" \
    XDG_RUNTIME_DIR="$GPU_SCOPE_XDG_RUNTIME_DIR" \
    DBUS_SESSION_BUS_ADDRESS="$GPU_SCOPE_DBUS_ADDRESS" \
    MONOMER_DFT_PYTHON="$MONOMER_DFT_PYTHON" \
    MONOMER_DFT_WORKER_UDS="$MONOMER_DFT_WORKER_UDS" \
    MONOMER_DFT_JOB_ROOT="$MONOMER_DFT_JOB_ROOT" \
    MONOMER_DFT_MAX_CONCURRENT_JOBS="${MONOMER_DFT_MAX_CONCURRENT_JOBS:-1}" \
    MONOMER_DFT_MAX_QUEUED_JOBS="${MONOMER_DFT_MAX_QUEUED_JOBS:-8}" \
    MONOMER_DFT_SINGLE_POINT_TIMEOUT_SECONDS="${MONOMER_DFT_SINGLE_POINT_TIMEOUT_SECONDS:-600}" \
    MONOMER_DFT_OPTIMIZATION_TIMEOUT_SECONDS="${MONOMER_DFT_OPTIMIZATION_TIMEOUT_SECONDS:-1800}" \
    MONOMER_DFT_FATAL_RESTART_MAX_ATTEMPTS="${MONOMER_DFT_FATAL_RESTART_MAX_ATTEMPTS:-3}" \
    MONOMER_DFT_FATAL_RESTART_BACKOFF_SECONDS="${MONOMER_DFT_FATAL_RESTART_BACKOFF_SECONDS:-1}" \
    MONOMER_DFT_FATAL_RESTART_MAX_BACKOFF_SECONDS="${MONOMER_DFT_FATAL_RESTART_MAX_BACKOFF_SECONDS:-8}" \
    MONOMER_DFT_FATAL_RESTART_RESET_SECONDS="${MONOMER_DFT_FATAL_RESTART_RESET_SECONDS:-300}" \
    MONOMER_DFT_WORKER_VERSION="${MONOMER_DFT_WORKER_VERSION:-0.1.0}" \
    MONOMER_DFT_WORKER_INSTANCE="$REPO_ROOT" \
    MONOMER_DFT_DEPLOYMENT=dev \
    NEXPOLY_DFT_GPU_DEVICE=1 \
    NEXPOLY_DFT_OVERFLOW_GPU_DEVICES=3 \
    MONOMER_DFT_GPU_BROKER_ENABLED="${MONOMER_DFT_GPU_BROKER_ENABLED:-1}" \
    MONOMER_DFT_STANDALONE_GPU_SMOKE="${MONOMER_DFT_STANDALONE_GPU_SMOKE:-0}" \
    MONOMER_DFT_GPU_BROKER_UDS="$MONOMER_DFT_GPU_BROKER_UDS" \
    MONOMER_DFT_GPU_MPS_PIPE_ROOT="$MONOMER_DFT_GPU_MPS_PIPE_ROOT" \
    MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS="$MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS" \
    NEXPOLY_DFT_FORMAL_ACCEPTANCE="$FORMAL_ACCEPTANCE" \
    NEXPOLY_DFT_PROJECT_NAME="$FORMAL_PROJECT_NAME" \
    NEXPOLY_DFT_AUTHORITY_SHA="$FORMAL_AUTHORITY_SHA" \
    NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY="${NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY:-}" \
    NEXPOLY_DFT_GPU_AUTHORITY_PID="${NEXPOLY_DFT_GPU_AUTHORITY_PID:-}" \
    NEXPOLY_DFT_GPU_AUTHORITY_START_TICKS="${NEXPOLY_DFT_GPU_AUTHORITY_START_TICKS:-}" \
    NEXPOLY_DFT_GPU_AUTHORITY_ROOT="${NEXPOLY_DFT_GPU_AUTHORITY_ROOT:-}" \
    NEXPOLY_DFT_GPU_AUTHORITY_ROOT_IDENTITY="${NEXPOLY_DFT_GPU_AUTHORITY_ROOT_IDENTITY:-}" \
    NEXPOLY_DFT_GPU_RESERVATIONS_AUTHORITY="${NEXPOLY_DFT_GPU_RESERVATIONS_AUTHORITY:-}" \
    NEXPOLY_DFT_GPU_RESERVATIONS_IDENTITY="${NEXPOLY_DFT_GPU_RESERVATIONS_IDENTITY:-}" \
    NEXPOLY_DFT_GPU_RESERVATIONS_SHA256="${NEXPOLY_DFT_GPU_RESERVATIONS_SHA256:-}" \
    NEXPOLY_DFT_GPU1_MPS_PIPE_AUTHORITY="${NEXPOLY_DFT_GPU1_MPS_PIPE_AUTHORITY:-}" \
    NEXPOLY_DFT_GPU1_MPS_PIPE_IDENTITY="${NEXPOLY_DFT_GPU1_MPS_PIPE_IDENTITY:-}" \
    NEXPOLY_DFT_GPU3_MPS_PIPE_AUTHORITY="${NEXPOLY_DFT_GPU3_MPS_PIPE_AUTHORITY:-}" \
    NEXPOLY_DFT_GPU3_MPS_PIPE_IDENTITY="${NEXPOLY_DFT_GPU3_MPS_PIPE_IDENTITY:-}" \
    MONOMER_DFT_GPU_BUDGET_MIB="${MONOMER_DFT_GPU_BUDGET_MIB:-4096}" \
    MONOMER_DFT_GPU_ACTIVE_THREAD_PERCENTAGE="${MONOMER_DFT_GPU_ACTIVE_THREAD_PERCENTAGE:-50}" \
    AIMNET_CACHE_DIR="$AIMNET_CACHE_DIR" \
    WARP_CACHE_PATH="$WARP_CACHE_PATH" \
    UV_CACHE_DIR="$UV_CACHE_DIR" \
    CUDA_DEVICE_ORDER=PCI_BUS_ID \
    PYTHONNOUSERSITE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    "$MONOMER_DFT_PYTHON" -c "$launcher_code" "$RUNNER" \
    >> "$LOG_FILE" 2>&1 < /dev/null 9>&- &
  SPAWN_PID=$!
  trap 'cleanup_startup; exit 129' HUP
  cd "$launch_dir"

  local identity_ready=false
  local observed_ticks=""
  for _ in $(seq 1 40); do
    process_is_running "$SPAWN_PID" || break
    observed_ticks="$(process_start_ticks "$SPAWN_PID" 2>/dev/null || true)"
    if [[ -n "$observed_ticks" ]]; then
      SPAWN_START_TICKS="$observed_ticks"
      if is_managed_process "$SPAWN_PID" "$SPAWN_START_TICKS"; then
        identity_ready=true
        break
      fi
    fi
    sleep 0.05
  done
  if [[ "$identity_ready" != "true" ]]; then
    tail -n 40 "$LOG_FILE" >&2 || true
    fail "worker exited or failed identity verification before PID state was recorded"
  fi
  atomic_write_pid_state "$SPAWN_PID" "$SPAWN_START_TICKS"

  local deadline=$((SECONDS + START_TIMEOUT))
  local payload=""
  while (( SECONDS < deadline )); do
    if ! is_managed_process "$SPAWN_PID" "$SPAWN_START_TICKS"; then
      tail -n 40 "$LOG_FILE" >&2 || true
      fail "worker exited during startup"
    fi
    if [[ -e "$MONOMER_DFT_WORKER_UDS" || -L "$MONOMER_DFT_WORKER_UDS" ]]; then
      secure_socket_permissions || true
    fi
    if payload="$(socket_health 2>/dev/null)" \
        && [[ "$payload" == *'"status":"ok"'* ]] \
        && secure_socket_permissions; then
      disarm_startup_cleanup
      log "worker started with pid $SPAWN_PID on $MONOMER_DFT_WORKER_UDS"
      printf '%s\n' "$payload"
      return 0
    fi
    sleep 0.5
  done

  tail -n 40 "$LOG_FILE" >&2 || true
  fail "worker did not become healthy within ${START_TIMEOUT}s"
}

assert_expected_drained_instance() {
  local expected_worker_instance_id="$1"
  socket_health | python3 -c '
import json, sys
payload = json.load(sys.stdin)
expected_worker_instance_id = sys.argv[1]
if not isinstance(payload, dict):
    raise SystemExit("worker health response must be an object")
if payload.get("worker_instance_id") != expected_worker_instance_id:
    raise SystemExit("worker instance changed before conditional stop")
if payload.get("draining") is not True or payload.get("accepting_jobs") is not False:
    raise SystemExit("worker must remain drained before conditional stop")
active_jobs = payload.get("active_jobs")
if isinstance(active_jobs, bool) or not isinstance(active_jobs, int) or active_jobs != 0:
    raise SystemExit("worker still has an active calculation")
if payload.get("recovering") is not False:
    raise SystemExit("worker recovery must be complete before conditional stop")
' "$expected_worker_instance_id"
}

stop_worker() {
  local expected_worker_instance_id="${1:-}"
  if ! read_pid_state; then
    if [[ -e "$PID_FILE" ]]; then
      fail "PID file is malformed; refusing to act on it: $PID_FILE"
    fi
    log "worker is not running"
    return 0
  fi

  if ! process_is_running "$MANAGED_PID"; then
    rm -f -- "$PID_FILE"
    remove_stale_socket
    log "removed stale worker state"
    return 0
  fi
  if [[ -n "$expected_worker_instance_id" ]]; then
    assert_expected_drained_instance "$expected_worker_instance_id" || fail \
      "refusing to stop a Worker that does not match the drained instance fence"
  fi
  log "stopping worker pid $MANAGED_PID"
  terminate_verified_process "$MANAGED_PID" "$MANAGED_START_TICKS" "worker" || fail "worker could not be stopped safely"
  pid_state_matches "$MANAGED_PID" "$MANAGED_START_TICKS" && rm -f -- "$PID_FILE"
  remove_stale_socket
  log "worker stopped"
}

status_worker() {
  if ! read_pid_state; then
    [[ ! -e "$PID_FILE" ]] || fail "PID file is malformed: $PID_FILE"
    log "worker is stopped"
    return 1
  fi
  if is_managed_process "$MANAGED_PID" "$MANAGED_START_TICKS"; then
    log "worker is running with pid $MANAGED_PID"
    socket_health || true
    return 0
  fi
  if process_is_running "$MANAGED_PID"; then
    fail "PID file points at an unverified live process: $MANAGED_PID"
  fi
  log "worker has stale PID state"
  return 1
}

usage() {
  printf 'Usage: %s {start|stop|restart|status|health|stop-if-drained-instance INSTANCE_ID}\n' "$0" >&2
  exit 2
}

main() {
  local command="${1:-}"
  case "$command" in
    stop-if-drained-instance)
      [[ $# -eq 2 && "$2" =~ ^[0-9a-fA-F]{32}$ ]] || usage
      ;;
    start|stop|restart|status|health)
      [[ $# -eq 1 ]] || usage
      ;;
    *) usage ;;
  esac

  initialize_runtime_root
  assert_safe_runtime_file "control lock file" "$LOCK_FILE"
  exec 9> "$LOCK_FILE"
  flock -n 9 || fail "another worker control operation is in progress"

  case "$command" in
    start)
      load_env true
      configure_paths
      validate_start_configuration
      run_preflight
      start_worker
      ;;
    stop)
      load_env false
      configure_paths
      stop_worker
      ;;
    stop-if-drained-instance)
      load_env false
      configure_paths
      stop_worker "$2"
      ;;
    restart)
      load_env true
      configure_paths
      validate_start_configuration
      stop_worker
      run_preflight
      start_worker
      ;;
    status)
      load_env false
      configure_paths
      status_worker
      ;;
    health)
      load_env true
      configure_paths
      socket_health
      printf '\n'
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi

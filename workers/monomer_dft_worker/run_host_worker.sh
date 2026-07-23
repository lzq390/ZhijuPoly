#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -P "$SCRIPT_DIR/../.." && pwd -P)"
PRODUCTION_REPO_ROOT="/data/lzq/gith/nexpoly"
DEFAULT_RUNTIME_ROOT="$REPO_ROOT/.runtime"
GPU_AUTHORITY_VALIDATOR="$REPO_ROOT/gpu_resource/authority.py"
RUNTIME_ROOT=""
RUNTIME_ROOT_IS_DEFAULT=false

fail() {
  printf '[monomer-dft-worker] %s\n' "$*" >&2
  exit 2
}

log() {
  printf '[monomer-dft-worker] %s\n' "$*"
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

assert_not_production_path() {
  local name="$1"
  local value="$2"
  [[ "$value" != "$PRODUCTION_REPO_ROOT" && "$value" != "$PRODUCTION_REPO_ROOT"/* ]] || \
    fail "$name must not reference the production repository"
}

configure_runtime_root() {
  local configured=""
  if [[ "${MONOMER_DFT_DEV_RUNTIME_ROOT+x}" == "x" ]]; then
    configured="$MONOMER_DFT_DEV_RUNTIME_ROOT"
  else
    configured="$DEFAULT_RUNTIME_ROOT"
    RUNTIME_ROOT_IS_DEFAULT=true
  fi
  [[ -n "$configured" ]] || fail "MONOMER_DFT_DEV_RUNTIME_ROOT must not be empty"
  if [[ "$configured" != /* ]]; then
    configured="$REPO_ROOT/$configured"
  fi
  RUNTIME_ROOT="$(realpath -ms -- "$configured")"
  assert_not_production_path MONOMER_DFT_DEV_RUNTIME_ROOT "$RUNTIME_ROOT"
  export MONOMER_DFT_DEV_RUNTIME_ROOT="$RUNTIME_ROOT"
}

initialize_runtime_root() {
  [[ ! -L "$RUNTIME_ROOT" ]] || fail "runtime root must not be a symlink: $RUNTIME_ROOT"
  if [[ ! -e "$RUNTIME_ROOT" ]]; then
    [[ "$RUNTIME_ROOT_IS_DEFAULT" == "true" ]] || \
      fail "caller-provided development runtime root must already exist: $RUNTIME_ROOT"
    [[ "$(dirname -- "$RUNTIME_ROOT")" == "$REPO_ROOT" ]] || \
      fail "default runtime root must be a direct child of the development worktree"
    [[ "$(realpath -e -- "$(dirname -- "$RUNTIME_ROOT")")" == "$REPO_ROOT" ]] || \
      fail "default runtime root parent resolves unexpectedly"
    mkdir --mode=0700 -- "$RUNTIME_ROOT"
  fi
  [[ -d "$RUNTIME_ROOT" && ! -L "$RUNTIME_ROOT" ]] || fail "runtime root must be a real directory: $RUNTIME_ROOT"
  REAL_RUNTIME_ROOT="$(realpath -e -- "$RUNTIME_ROOT")"
  [[ "$REAL_RUNTIME_ROOT" == "$RUNTIME_ROOT" ]] || fail "runtime root resolves unexpectedly: $REAL_RUNTIME_ROOT"
  assert_not_production_path MONOMER_DFT_DEV_RUNTIME_ROOT "$REAL_RUNTIME_ROOT"
  local owner mode
  owner="$(stat -Lc '%u' -- "$RUNTIME_ROOT")" || fail "runtime root owner could not be read: $RUNTIME_ROOT"
  mode="$(stat -Lc '%a' -- "$RUNTIME_ROOT")" || fail "runtime root mode could not be read: $RUNTIME_ROOT"
  [[ "$owner" == "$(id -u)" ]] || fail "runtime root must be owned by the current uid: $RUNTIME_ROOT"
  [[ "$mode" == "700" ]] || fail "runtime root must have mode 0700: $RUNTIME_ROOT"
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
    resolved="$(realpath -e -- "$candidate")"
    [[ "$resolved" == "$REAL_RUNTIME_ROOT"/* ]] || fail "$name escapes $REAL_RUNTIME_ROOT: $resolved"
    current="$candidate"
  done
}

assert_runtime_file_slot() {
  local name="$1"
  local value="$2"
  assert_runtime_path "$name" "$value"
  [[ ! -L "$value" ]] || fail "$name must not be a symlink: $value"
  [[ ! -e "$value" || -f "$value" ]] || fail "$name must be a regular file slot: $value"
}

process_is_running() {
  local pid="$1"
  local state=""
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" >/dev/null 2>&1 || return 1
  [[ -r "/proc/$pid/stat" ]] || return 1
  state="$(awk '{print $3}' "/proc/$pid/stat")" || return 1
  [[ "$state" != "Z" ]]
}

process_group_has_live_members() {
  local pgid="$1"
  local process_table=""
  [[ "$pgid" =~ ^[0-9]+$ ]] || return 1
  process_table="$(ps -eo pgid=,stat= 2>/dev/null)" || return 0
  awk -v target="$pgid" '
    $1 == target && $2 !~ /^Z/ { found = 1 }
    END { exit(found ? 0 : 1) }
  ' <<<"$process_table"
}

signal_supervised_group() {
  local signal_name="$1"
  [[ -n "${SUPERVISED_PGID:-}" ]] || return 0
  [[ "$SUPERVISED_PGID" =~ ^[0-9]+$ ]] || return 1
  [[ "$SUPERVISED_PGID" != "$$" ]] || return 1
  process_group_has_live_members "$SUPERVISED_PGID" || return 0
  kill -s "$signal_name" -- "-$SUPERVISED_PGID" 2>/dev/null || true
}

terminate_supervised_group() {
  local initial_signal="${1:-TERM}"
  local pgid="${SUPERVISED_PGID:-}"
  [[ -n "$pgid" ]] || return 0
  [[ "$pgid" =~ ^[0-9]+$ && "$pgid" != "$$" ]] || return 1

  if process_group_has_live_members "$pgid"; then
    kill -s "$initial_signal" -- "-$pgid" 2>/dev/null || true
    for _ in $(seq 1 50); do
      process_group_has_live_members "$pgid" || break
      sleep 0.1
    done
  fi
  if process_group_has_live_members "$pgid"; then
    kill -KILL -- "-$pgid" 2>/dev/null || true
    for _ in $(seq 1 30); do
      process_group_has_live_members "$pgid" || break
      sleep 0.1
    done
  fi
  ! process_group_has_live_members "$pgid"
}

reap_supervised_process() {
  local pid="${SUPERVISED_PID:-}"
  [[ -n "$pid" ]] || return 0
  wait "$pid" 2>/dev/null || true
}

handle_supervisor_signal() {
  local signal_name="$1"
  SHUTDOWN_REQUESTED=true
  SHUTDOWN_SIGNAL="$signal_name"
  if [[ -n "${BACKOFF_PID:-}" ]]; then
    kill -s "$signal_name" "$BACKOFF_PID" 2>/dev/null || true
  fi
  signal_supervised_group "$signal_name" || true
}

supervisor_exit_cleanup() {
  local status="$1"
  trap - EXIT TERM INT
  if [[ -n "${BACKOFF_PID:-}" ]]; then
    kill -TERM "$BACKOFF_PID" 2>/dev/null || true
    wait "$BACKOFF_PID" 2>/dev/null || true
  fi
  if [[ -n "${SUPERVISED_PGID:-}" ]]; then
    terminate_supervised_group TERM || {
      printf '[monomer-dft-worker] supervised process group could not be reaped safely\n' >&2
      status=74
    }
  fi
  reap_supervised_process
  exit "$status"
}

validate_supervisor_configuration() {
  FATAL_RESTART_MAX_ATTEMPTS="${MONOMER_DFT_FATAL_RESTART_MAX_ATTEMPTS:-3}"
  FATAL_RESTART_BACKOFF_SECONDS="${MONOMER_DFT_FATAL_RESTART_BACKOFF_SECONDS:-1}"
  FATAL_RESTART_MAX_BACKOFF_SECONDS="${MONOMER_DFT_FATAL_RESTART_MAX_BACKOFF_SECONDS:-8}"
  FATAL_RESTART_RESET_SECONDS="${MONOMER_DFT_FATAL_RESTART_RESET_SECONDS:-300}"

  [[ "$FATAL_RESTART_MAX_ATTEMPTS" =~ ^[0-9]+$ ]] || fail \
    "MONOMER_DFT_FATAL_RESTART_MAX_ATTEMPTS must be an integer"
  (( FATAL_RESTART_MAX_ATTEMPTS <= 10 )) || fail \
    "MONOMER_DFT_FATAL_RESTART_MAX_ATTEMPTS must be between 0 and 10"
  [[ "$FATAL_RESTART_BACKOFF_SECONDS" =~ ^[0-9]+$ ]] || fail \
    "MONOMER_DFT_FATAL_RESTART_BACKOFF_SECONDS must be an integer"
  (( FATAL_RESTART_BACKOFF_SECONDS >= 1 && FATAL_RESTART_BACKOFF_SECONDS <= 60 )) || fail \
    "MONOMER_DFT_FATAL_RESTART_BACKOFF_SECONDS must be between 1 and 60"
  [[ "$FATAL_RESTART_MAX_BACKOFF_SECONDS" =~ ^[0-9]+$ ]] || fail \
    "MONOMER_DFT_FATAL_RESTART_MAX_BACKOFF_SECONDS must be an integer"
  (( FATAL_RESTART_MAX_BACKOFF_SECONDS >= FATAL_RESTART_BACKOFF_SECONDS \
      && FATAL_RESTART_MAX_BACKOFF_SECONDS <= 300 )) || fail \
    "MONOMER_DFT_FATAL_RESTART_MAX_BACKOFF_SECONDS must be between the base backoff and 300"
  [[ "$FATAL_RESTART_RESET_SECONDS" =~ ^[0-9]+$ ]] || fail \
    "MONOMER_DFT_FATAL_RESTART_RESET_SECONDS must be an integer"
  (( FATAL_RESTART_RESET_SECONDS >= 60 && FATAL_RESTART_RESET_SECONDS <= 86400 )) || fail \
    "MONOMER_DFT_FATAL_RESTART_RESET_SECONDS must be between 60 and 86400"
}

remove_verified_stale_socket() {
  [[ ! -L "$MONOMER_DFT_WORKER_UDS" ]] || fail \
    "worker socket must not be a symlink: $MONOMER_DFT_WORKER_UDS"
  [[ -e "$MONOMER_DFT_WORKER_UDS" ]] || return 0
  [[ -S "$MONOMER_DFT_WORKER_UDS" ]] || fail \
    "worker socket path contains a non-socket: $MONOMER_DFT_WORKER_UDS"

  local socket_identity=""
  local verified_identity=""
  local probe_status=0
  socket_identity="$(stat -Lc '%d:%i' -- "$MONOMER_DFT_WORKER_UDS")" || fail \
    "worker socket identity could not be read: $MONOMER_DFT_WORKER_UDS"
  if "$MONOMER_DFT_PYTHON" - "$MONOMER_DFT_WORKER_UDS" <<'PY'
import socket
import sys

client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.settimeout(0.25)
try:
    client.connect(sys.argv[1])
except (ConnectionRefusedError, FileNotFoundError):
    raise SystemExit(1)
except OSError:
    raise SystemExit(2)
finally:
    client.close()
raise SystemExit(0)
PY
  then
    fail "refusing to remove a listening worker socket: $MONOMER_DFT_WORKER_UDS"
  else
    probe_status=$?
  fi
  [[ "$probe_status" == "1" ]] || fail \
    "worker socket could not be verified as stale: $MONOMER_DFT_WORKER_UDS"
  [[ -S "$MONOMER_DFT_WORKER_UDS" && ! -L "$MONOMER_DFT_WORKER_UDS" ]] || fail \
    "worker socket changed during stale-socket verification"
  verified_identity="$(stat -Lc '%d:%i' -- "$MONOMER_DFT_WORKER_UDS")" || fail \
    "worker socket disappeared during stale-socket verification"
  [[ "$verified_identity" == "$socket_identity" ]] || fail \
    "worker socket identity changed during stale-socket verification"
  rm -f -- "$MONOMER_DFT_WORKER_UDS"
  [[ ! -e "$MONOMER_DFT_WORKER_UDS" && ! -L "$MONOMER_DFT_WORKER_UDS" ]] || fail \
    "worker socket remained after safe stale-socket cleanup"
}

gpu_is_exclusive() {
  local inventory=""
  local compute_processes=""
  local gpu_uuid=""
  local conflict_count=""
  command -v nvidia-smi >/dev/null 2>&1 || return 1
  inventory="$(nvidia-smi --query-gpu=index,uuid --format=csv,noheader,nounits 2>/dev/null)" || return 1
  gpu_uuid="$(
    awk -F',' -v target="$NEXPOLY_DFT_GPU_DEVICE" '
      {
        gpu_index = $1; uuid = $2
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", gpu_index)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", uuid)
        if (gpu_index == target) print uuid
      }
    ' <<<"$inventory"
  )"
  [[ -n "$gpu_uuid" && "$gpu_uuid" != *$'\n'* ]] || return 1

  compute_processes="$(
    nvidia-smi --query-compute-apps=gpu_uuid,pid --format=csv,noheader,nounits 2>/dev/null
  )" || return 1
  conflict_count="$(
    awk -F',' -v target="$gpu_uuid" '
      {
        uuid = $1; pid = $2
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", uuid)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", pid)
        if (uuid == target && pid ~ /^[0-9]+$/) count += 1
      }
      END { print count + 0 }
    ' <<<"$compute_processes"
  )"
  [[ "$conflict_count" == "0" ]]
}

wait_for_exclusive_gpu() {
  for _ in $(seq 1 20); do
    [[ "${SHUTDOWN_REQUESTED:-false}" != "true" ]] || return 2
    gpu_is_exclusive && return 0
    sleep 0.25
  done
  return 1
}

launch_supervised_worker() {
  LAUNCH_ABORTED=false
  SUPERVISED_PID=""
  SUPERVISED_PGID=""
  if [[ "$SHUTDOWN_REQUESTED" == "true" ]]; then
    LAUNCH_ABORTED=true
    return 0
  fi

  local launcher_code='import os, sys; os.setsid(); os.execv(sys.argv[1], sys.argv[1:])'
  "$MONOMER_DFT_PYTHON" -c "$launcher_code" "$MONOMER_DFT_PYTHON" \
    -m uvicorn workers.monomer_dft_worker.app.main:app \
    --uds "$MONOMER_DFT_WORKER_UDS" \
    --no-access-log \
    --workers 1 &
  SUPERVISED_PID=$!
  SUPERVISED_PGID=$SUPERVISED_PID

  local observed_pgid=""
  for _ in $(seq 1 100); do
    if ! process_is_running "$SUPERVISED_PID"; then
      if [[ "$SHUTDOWN_REQUESTED" == "true" ]]; then
        terminate_supervised_group TERM || true
        reap_supervised_process
        SUPERVISED_PID=""
        SUPERVISED_PGID=""
        LAUNCH_ABORTED=true
      fi
      return 0
    fi
    observed_pgid="$(ps -o pgid= -p "$SUPERVISED_PID" 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ "$observed_pgid" == "$SUPERVISED_PID" ]]; then
      if [[ "$SHUTDOWN_REQUESTED" == "true" ]]; then
        terminate_supervised_group "$SHUTDOWN_SIGNAL" || fail \
          "worker child process group did not terminate during an interrupted launch"
        reap_supervised_process
        SUPERVISED_PID=""
        SUPERVISED_PGID=""
        LAUNCH_ABORTED=true
      fi
      return 0
    fi
    sleep 0.01
  done

  # The launcher is still our direct, unreaped child. If it failed to create
  # the promised session/process group, stop that exact PID rather than ever
  # signaling the runner's own process group.
  kill -TERM "$SUPERVISED_PID" 2>/dev/null || true
  for _ in $(seq 1 50); do
    process_is_running "$SUPERVISED_PID" || break
    sleep 0.02
  done
  if process_is_running "$SUPERVISED_PID"; then
    kill -KILL "$SUPERVISED_PID" 2>/dev/null || true
  fi
  reap_supervised_process
  SUPERVISED_PID=""
  SUPERVISED_PGID=""
  fail "worker child did not enter its dedicated process group"
}

fatal_restart_delay() {
  local restart_number="$1"
  local delay="$FATAL_RESTART_BACKOFF_SECONDS"
  local step=1
  while (( step < restart_number && delay < FATAL_RESTART_MAX_BACKOFF_SECONDS )); do
    delay=$((delay * 2))
    (( delay <= FATAL_RESTART_MAX_BACKOFF_SECONDS )) || delay="$FATAL_RESTART_MAX_BACKOFF_SECONDS"
    step=$((step + 1))
  done
  printf '%s\n' "$delay"
}

if [[ -n "${PYTHONPATH:-}" ]]; then
  IFS=':' read -r -a pythonpath_entries <<< "$PYTHONPATH"
  for entry in "${pythonpath_entries[@]}"; do
    [[ -n "$entry" ]] || continue
    resolved_entry="$(realpath -m -- "$entry")"
    case "$resolved_entry" in
      /data/cgy|/data/cgy/*|/data/lzq/gith/aimnetcentral|/data/lzq/gith/aimnetcentral/*)
        fail "PYTHONPATH must not reference the original AIMNet environment or source clone: $resolved_entry"
        ;;
    esac
  done
  fail "PYTHONPATH must be empty for the isolated monomer DFT worker"
fi
unset PYTHONPATH

assert_not_production_path "Worker code root" "$REPO_ROOT"
configure_runtime_root

# The worker is intentionally database-blind.  The controller launches it
# through an environment allowlist; keep this fail-closed check for direct or
# manually scripted runner invocations as well.
for forbidden_database_variable in \
  APP_POSTGRES_DSN \
  PI_POSTGRES_DSN \
  LAB_DATA_POSTGRES_DSN \
  DATABASE_URL \
  PGPASSWORD \
  POSTGRES_PASSWORD \
  NEXPOLY_DFT_POSTGRES_PASSWORD; do
  [[ -z "${!forbidden_database_variable:-}" ]] || \
    fail "$forbidden_database_variable must not be present in the monomer DFT worker environment"
done

MONOMER_DFT_DEPLOYMENT="${MONOMER_DFT_DEPLOYMENT:-dev}"
[[ "$MONOMER_DFT_DEPLOYMENT" == "dev" ]] || \
  fail "MONOMER_DFT_DEPLOYMENT must be dev; production is hard-off"
[[ "${NEXPOLY_DFT_GPU_DEVICE:-1}" == "1" ]] || \
  fail "dev primary DFT executor must use physical GPU 1; GPU 0 and GPU 2 are forbidden"
[[ "${NEXPOLY_DEV_GPU1_ONLY_SESSION:-0}" =~ ^[01]$ ]] || \
  fail "NEXPOLY_DEV_GPU1_ONLY_SESSION must be 0 or 1"
if [[ "${NEXPOLY_DEV_GPU1_ONLY_SESSION:-0}" == "1" ]]; then
  [[ "${NEXPOLY_DEV_GPU_SESSION_ID:-}" =~ ^[0-9a-f]{32}$ ]] || \
    fail "GPU1-only session identity is missing or invalid"
  [[ -z "${NEXPOLY_DFT_OVERFLOW_GPU_DEVICES:-}" ]] || \
    fail "GPU1-only development sessions forbid every overflow GPU"
else
  [[ "${NEXPOLY_DFT_OVERFLOW_GPU_DEVICES:-3}" == "3" ]] || \
    fail "dev overflow order must be physical GPU 3 only; GPU 0 and GPU 2 are forbidden"
fi

MONOMER_DFT_PYTHON="$(absolute_runtime_path "${MONOMER_DFT_PYTHON:-$RUNTIME_ROOT/venvs/monomer-dft-worker/bin/python}")"
MONOMER_DFT_WORKER_UDS="$(absolute_runtime_path "${MONOMER_DFT_WORKER_UDS:-$RUNTIME_ROOT/monomer-dft-worker-socket/worker.sock}")"
MONOMER_DFT_JOB_ROOT="$(absolute_runtime_path "${MONOMER_DFT_JOB_ROOT:-$RUNTIME_ROOT/monomer-dft-worker-runs}")"
AIMNET_CACHE_DIR="$(absolute_runtime_path "${AIMNET_CACHE_DIR:-$RUNTIME_ROOT/aimnet-cache}")"
WARP_CACHE_PATH="$(absolute_runtime_path "${WARP_CACHE_PATH:-$RUNTIME_ROOT/warp-cache}")"
export UV_CACHE_DIR="$RUNTIME_ROOT/uv-cache"
if [[ "${NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY:-}" == "1" ]]; then
  [[ "${NEXPOLY_DFT_FORMAL_ACCEPTANCE:-0}" == "1" ]] || \
    fail "GPU descriptor authority requires formal acceptance"
  MONOMER_DFT_GPU_BROKER_UDS="${NEXPOLY_DFT_GPU_AUTHORITY_ROOT}/broker.sock"
  MONOMER_DFT_GPU_MPS_PIPE_ROOT="$NEXPOLY_DFT_GPU_AUTHORITY_ROOT"
  MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS="$NEXPOLY_DFT_GPU_RESERVATIONS_AUTHORITY"
  "$MONOMER_DFT_PYTHON" -I -S "$GPU_AUTHORITY_VALIDATOR" \
    --expected-reservations-file \
    "$REPO_ROOT/ops/config/gpu-external-reservations.json" \
    --expected-root "$RUNTIME_ROOT/gpu-resource" ||
    fail "GPU descriptor authority validation failed"
else
  MONOMER_DFT_GPU_BROKER_UDS="$(absolute_runtime_path "${MONOMER_DFT_GPU_BROKER_UDS:-$RUNTIME_ROOT/gpu-resource/broker.sock}")"
  MONOMER_DFT_GPU_MPS_PIPE_ROOT="$(absolute_runtime_path "${MONOMER_DFT_GPU_MPS_PIPE_ROOT:-$RUNTIME_ROOT/gpu-resource}")"
  MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS="$(absolute_runtime_path "${MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS:-$RUNTIME_ROOT/gpu-resource/external-reservations.json}")"
fi
MONOMER_DFT_DOWNLOAD_SPOOL_ROOT="$(absolute_runtime_path "${MONOMER_DFT_DOWNLOAD_SPOOL_ROOT:-$RUNTIME_ROOT/monomer-dft-downloads}")"
export MONOMER_DFT_PYTHON MONOMER_DFT_WORKER_UDS MONOMER_DFT_JOB_ROOT
export AIMNET_CACHE_DIR WARP_CACHE_PATH
export MONOMER_DFT_GPU_BROKER_UDS MONOMER_DFT_GPU_MPS_PIPE_ROOT
export MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS MONOMER_DFT_DOWNLOAD_SPOOL_ROOT
export XDG_CACHE_HOME="$RUNTIME_ROOT/xdg-cache"
export TORCH_HOME="$RUNTIME_ROOT/torch-cache"
export HF_HOME="$RUNTIME_ROOT/hf-cache"
export TMPDIR="$RUNTIME_ROOT/tmp"
export HOME="$RUNTIME_ROOT/home"

assert_runtime_path MONOMER_DFT_PYTHON "$MONOMER_DFT_PYTHON"
assert_runtime_path MONOMER_DFT_WORKER_UDS "$MONOMER_DFT_WORKER_UDS"
assert_runtime_path MONOMER_DFT_JOB_ROOT "$MONOMER_DFT_JOB_ROOT"
assert_runtime_path AIMNET_CACHE_DIR "$AIMNET_CACHE_DIR"
assert_runtime_path WARP_CACHE_PATH "$WARP_CACHE_PATH"
assert_runtime_path UV_CACHE_DIR "$UV_CACHE_DIR"
if [[ "${NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY:-}" != "1" ]]; then
  assert_runtime_path MONOMER_DFT_GPU_BROKER_UDS "$MONOMER_DFT_GPU_BROKER_UDS"
  assert_runtime_path MONOMER_DFT_GPU_MPS_PIPE_ROOT "$MONOMER_DFT_GPU_MPS_PIPE_ROOT"
  assert_runtime_path MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS "$MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS"
fi
assert_runtime_path MONOMER_DFT_DOWNLOAD_SPOOL_ROOT "$MONOMER_DFT_DOWNLOAD_SPOOL_ROOT"
assert_runtime_path XDG_CACHE_HOME "$XDG_CACHE_HOME"
assert_runtime_path TORCH_HOME "$TORCH_HOME"
assert_runtime_path HF_HOME "$HF_HOME"
assert_runtime_path TMPDIR "$TMPDIR"
assert_runtime_path HOME "$HOME"

[[ "${MONOMER_DFT_MAX_CONCURRENT_JOBS:-1}" == "1" ]] || fail "MONOMER_DFT_MAX_CONCURRENT_JOBS must be 1"
[[ "${MONOMER_DFT_GPU_BROKER_ENABLED:-1}" == "0" || "${MONOMER_DFT_GPU_BROKER_ENABLED:-1}" == "1" ]] || \
  fail "MONOMER_DFT_GPU_BROKER_ENABLED must be 0 or 1"
validate_supervisor_configuration

export MONOMER_DFT_MAX_CONCURRENT_JOBS=1
export MONOMER_DFT_DEPLOYMENT
export NEXPOLY_DFT_GPU_DEVICE="${NEXPOLY_DFT_GPU_DEVICE:-1}"
if [[ "${NEXPOLY_DEV_GPU1_ONLY_SESSION:-0}" == "1" ]]; then
  export NEXPOLY_DFT_OVERFLOW_GPU_DEVICES=""
else
  export NEXPOLY_DFT_OVERFLOW_GPU_DEVICES="${NEXPOLY_DFT_OVERFLOW_GPU_DEVICES:-3}"
fi
export CUDA_DEVICE_ORDER=PCI_BUS_ID
# The supervisor must remain CUDA-blind. Each leased executor child receives
# exactly one CUDA_VISIBLE_DEVICES value before importing any CUDA library.
unset CUDA_VISIBLE_DEVICES
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
MONOMER_DFT_WORKER_INSTANCE="${MONOMER_DFT_WORKER_INSTANCE:-$REPO_ROOT}"
[[ "$MONOMER_DFT_WORKER_INSTANCE" == "$REPO_ROOT" ]] || \
  fail "MONOMER_DFT_WORKER_INSTANCE must identify this development worktree"
export MONOMER_DFT_WORKER_INSTANCE

initialize_runtime_root
ensure_runtime_directory "MONOMER_DFT_PYTHON parent" "$(dirname "$MONOMER_DFT_PYTHON")" false
ensure_runtime_directory "MONOMER_DFT_WORKER_UDS parent" "$(dirname "$MONOMER_DFT_WORKER_UDS")" true
ensure_runtime_directory MONOMER_DFT_JOB_ROOT "$MONOMER_DFT_JOB_ROOT" true
ensure_runtime_directory AIMNET_CACHE_DIR "$AIMNET_CACHE_DIR" true
ensure_runtime_directory WARP_CACHE_PATH "$WARP_CACHE_PATH" true
ensure_runtime_directory UV_CACHE_DIR "$UV_CACHE_DIR" true
if [[ "${NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY:-}" != "1" ]]; then
  ensure_runtime_directory "MONOMER_DFT_GPU_BROKER_UDS parent" "$(dirname "$MONOMER_DFT_GPU_BROKER_UDS")" true
  ensure_runtime_directory MONOMER_DFT_GPU_MPS_PIPE_ROOT "$MONOMER_DFT_GPU_MPS_PIPE_ROOT" true
  ensure_runtime_directory "MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS parent" "$(dirname "$MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS")" true
  assert_runtime_file_slot MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS "$MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS"
fi
ensure_runtime_directory MONOMER_DFT_DOWNLOAD_SPOOL_ROOT "$MONOMER_DFT_DOWNLOAD_SPOOL_ROOT" true
ensure_runtime_directory XDG_CACHE_HOME "$XDG_CACHE_HOME" true
ensure_runtime_directory TORCH_HOME "$TORCH_HOME" true
ensure_runtime_directory HF_HOME "$HF_HOME" true
ensure_runtime_directory TMPDIR "$TMPDIR" true
ensure_runtime_directory HOME "$HOME" true
[[ -x "$MONOMER_DFT_PYTHON" ]] || fail "MONOMER_DFT_PYTHON is not executable: $MONOMER_DFT_PYTHON"
[[ ! -e "$MONOMER_DFT_WORKER_UDS" && ! -L "$MONOMER_DFT_WORKER_UDS" ]] || fail "worker socket already exists or is a symlink: $MONOMER_DFT_WORKER_UDS"

# Launch from the repository root so release-local shared packages such as
# gpu_resource are importable without weakening the isolated PYTHONPATH policy.
# The fully-qualified application module also prevents an unrelated top-level
# `app` package from shadowing the Worker.
cd "$REPO_ROOT"
SHUTDOWN_REQUESTED=false
SHUTDOWN_SIGNAL=TERM
SUPERVISED_PID=""
SUPERVISED_PGID=""
BACKOFF_PID=""
LAUNCH_ABORTED=false
trap 'handle_supervisor_signal TERM' TERM
trap 'handle_supervisor_signal INT' INT
trap 'supervisor_exit_cleanup $?' EXIT

fatal_restarts=0
while true; do
  [[ "$SHUTDOWN_REQUESTED" != "true" ]] || exit 0
  remove_verified_stale_socket
  child_started_at=$SECONDS
  launch_supervised_worker
  if [[ "$LAUNCH_ABORTED" == "true" ]]; then
    remove_verified_stale_socket
    exit 0
  fi
  if [[ "$SHUTDOWN_REQUESTED" == "true" ]]; then
    terminate_supervised_group "$SHUTDOWN_SIGNAL" || fail \
      "worker child process group did not terminate before wait"
    reap_supervised_process
    SUPERVISED_PID=""
    SUPERVISED_PGID=""
    remove_verified_stale_socket
    exit 0
  fi
  child_status=0
  if wait "$SUPERVISED_PID"; then
    child_status=0
  else
    child_status=$?
  fi

  if [[ "$SHUTDOWN_REQUESTED" == "true" ]]; then
    terminate_supervised_group "$SHUTDOWN_SIGNAL" || fail \
      "worker child process group did not terminate after $SHUTDOWN_SIGNAL"
    reap_supervised_process
    SUPERVISED_PID=""
    SUPERVISED_PGID=""
    remove_verified_stale_socket
    exit 0
  fi

  terminate_supervised_group TERM || fail \
    "worker child process group remained alive after its leader exited"
  reap_supervised_process
  SUPERVISED_PID=""
  SUPERVISED_PGID=""
  remove_verified_stale_socket

  if [[ "$child_status" != "70" ]]; then
    log "worker exited with status $child_status; non-fatal exits are not restarted"
    exit "$child_status"
  fi

  child_runtime=$((SECONDS - child_started_at))
  if (( child_runtime >= FATAL_RESTART_RESET_SECONDS )); then
    fatal_restarts=0
  fi
  if (( fatal_restarts >= FATAL_RESTART_MAX_ATTEMPTS )); then
    log "fatal restart circuit opened after $fatal_restarts restart attempt(s)"
    exit 70
  fi

  fatal_restarts=$((fatal_restarts + 1))
  restart_delay="$(fatal_restart_delay "$fatal_restarts")"
  log "worker exited with fatal status 70; restart $fatal_restarts/$FATAL_RESTART_MAX_ATTEMPTS in ${restart_delay}s"
  sleep "$restart_delay" &
  BACKOFF_PID=$!
  wait "$BACKOFF_PID" 2>/dev/null || true
  BACKOFF_PID=""
  if [[ "$SHUTDOWN_REQUESTED" == "true" ]]; then
    exit 0
  fi
done

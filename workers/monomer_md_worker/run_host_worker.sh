#!/bin/bash
set -euo pipefail
umask 0077

SCRIPT_PATH="${BASH_SOURCE[0]}"
case "$SCRIPT_PATH" in
  */*)
    SCRIPT_PARENT="${SCRIPT_PATH%/*}"
    ;;
  *)
    SCRIPT_PARENT="."
    ;;
esac
SCRIPT_DIR="$(cd "$SCRIPT_PARENT" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd -P)"

# user-systemd services inherit the user manager's environment. Re-read the
# owner-only file through the same literal parser used by deployment and the
# pidfile fallback so omitted Worker keys cannot retain stale manager values.
# The private argv marker is added only by this re-exec path; an inherited
# environment variable can therefore never bypass sanitization.
if [[ "${1:-}" != "--nexpoly-worker-env-applied" ]]; then
  exec /usr/bin/python3 -I "$REPO_ROOT/scripts/monomer_worker_env.py" exec \
    "$REPO_ROOT/.env.monomer-md-worker" -- \
    /bin/bash "$SCRIPT_DIR/run_host_worker.sh" --nexpoly-worker-env-applied
fi
shift

cd "$SCRIPT_DIR"

export MONOMER_MD_WORKER_MODE="${MONOMER_MD_WORKER_MODE:-real}"
export MONOMER_MD_WORKER_HOST="${MONOMER_MD_WORKER_HOST:-127.0.0.1}"
export MONOMER_MD_WORKER_PORT="${MONOMER_MD_WORKER_PORT:-18010}"
export BYTEFF2_ROOT="${BYTEFF2_ROOT:-}"
export NEXPOLY_GPU_DEVICE="${NEXPOLY_GPU_DEVICE:-2}"
export MONOMER_MD_CUDA_VISIBLE_DEVICES="${MONOMER_MD_CUDA_VISIBLE_DEVICES:-$NEXPOLY_GPU_DEVICE}"
export BYTEFF2_PYTHON="${BYTEFF2_PYTHON:-${MONOMER_MD_PYTHON:-python}}"

if [[ -z "${APP_POSTGRES_DSN:-}" ]]; then
  echo "APP_POSTGRES_DSN is required for the monomer MD worker." >&2
  exit 2
fi

if [[ "$MONOMER_MD_WORKER_MODE" == "real" && -z "$BYTEFF2_ROOT" ]]; then
  echo "BYTEFF2_ROOT is required in real worker mode." >&2
  exit 2
fi

if [[ "$MONOMER_MD_WORKER_MODE" == "real" && ! -d "$BYTEFF2_ROOT" ]]; then
  echo "BYTEFF2_ROOT does not exist: $BYTEFF2_ROOT" >&2
  exit 2
fi

if [[ -n "$BYTEFF2_ROOT" ]]; then
  export PYTHONPATH="${PYTHONPATH:-$BYTEFF2_ROOT:$BYTEFF2_ROOT/submodules/bytemol}"
fi

if [[ -n "${MONOMER_MD_WORKER_UDS:-}" ]]; then
  socket_dir="$(dirname "$MONOMER_MD_WORKER_UDS")"
  install -d -m 0700 "$socket_dir"
  chmod 0700 "$socket_dir"
  rm -f "$MONOMER_MD_WORKER_UDS"
  exec "${MONOMER_MD_PYTHON:-python}" -m uvicorn app.main:app --workers 1 --uds "$MONOMER_MD_WORKER_UDS"
fi

exec "${MONOMER_MD_PYTHON:-python}" -m uvicorn app.main:app --workers 1 --host "$MONOMER_MD_WORKER_HOST" --port "$MONOMER_MD_WORKER_PORT"

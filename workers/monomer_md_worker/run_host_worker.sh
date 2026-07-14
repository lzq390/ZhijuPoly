#!/usr/bin/env bash
set -euo pipefail
umask 0077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export MONOMER_MD_WORKER_MODE="${MONOMER_MD_WORKER_MODE:-real}"
export MONOMER_MD_WORKER_HOST="${MONOMER_MD_WORKER_HOST:-127.0.0.1}"
export MONOMER_MD_WORKER_PORT="${MONOMER_MD_WORKER_PORT:-18010}"
export BYTEFF2_ROOT="${BYTEFF2_ROOT:-}"
export NEXPOLY_GPU_DEVICE="${NEXPOLY_GPU_DEVICE:-2}"
export MONOMER_MD_CUDA_VISIBLE_DEVICES="${MONOMER_MD_CUDA_VISIBLE_DEVICES:-$NEXPOLY_GPU_DEVICE}"
export BYTEFF2_PYTHON="${BYTEFF2_PYTHON:-${MONOMER_MD_PYTHON:-python}}"

if [[ "$BYTEFF2_PYTHON" == */* ]]; then
  export PATH="$(dirname "$BYTEFF2_PYTHON"):$PATH"
fi

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

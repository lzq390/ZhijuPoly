#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export MONOMER_MD_WORKER_MODE="${MONOMER_MD_WORKER_MODE:-real}"
export MONOMER_MD_WORKER_HOST="${MONOMER_MD_WORKER_HOST:-127.0.0.1}"
export MONOMER_MD_WORKER_PORT="${MONOMER_MD_WORKER_PORT:-18010}"
export BYTEFF2_ROOT="${BYTEFF2_ROOT:-/data/lzq/gith/byteff2}"
export NEXPOLY_GPU_DEVICE="${NEXPOLY_GPU_DEVICE:-2}"
export MONOMER_MD_CUDA_VISIBLE_DEVICES="${MONOMER_MD_CUDA_VISIBLE_DEVICES:-$NEXPOLY_GPU_DEVICE}"
export BYTEFF2_PYTHON="${BYTEFF2_PYTHON:-${MONOMER_MD_PYTHON:-python}}"

if [[ -z "${APP_POSTGRES_DSN:-}" ]]; then
  echo "APP_POSTGRES_DSN is required for the monomer MD worker." >&2
  exit 2
fi

if [[ "$MONOMER_MD_WORKER_MODE" == "real" && ! -d "$BYTEFF2_ROOT" ]]; then
  echo "BYTEFF2_ROOT does not exist: $BYTEFF2_ROOT" >&2
  exit 2
fi

exec "${MONOMER_MD_PYTHON:-python}" -m uvicorn app.main:app --host "$MONOMER_MD_WORKER_HOST" --port "$MONOMER_MD_WORKER_PORT"
#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
PIP_TOOLS_VERSION="7.5.0"
PYTORCH_INDEX_URL="https://download.pytorch.org/whl/cu124"

command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
  echo "Python is required: $PYTHON_BIN" >&2
  exit 1
}

actual_pip_tools="$("$PYTHON_BIN" -c 'import importlib.metadata as m; print(m.version("pip-tools"))' 2>/dev/null || true)"
[[ "$actual_pip_tools" == "$PIP_TOOLS_VERSION" ]] || {
  echo "pip-tools==$PIP_TOOLS_VERSION is required in $PYTHON_BIN (found: ${actual_pip_tools:-missing})." >&2
  exit 1
}

platform_contract="$("$PYTHON_BIN" -c 'import platform, sys; print(f"{sys.version_info.major}.{sys.version_info.minor}:{sys.platform}:{platform.machine().lower()}")')"
case "$platform_contract" in
  3.11:linux:x86_64|3.11:linux:amd64) ;;
  *)
    echo "Requirement locks must be generated on Python 3.11/Linux x86_64, got $platform_contract." >&2
    exit 1
    ;;
esac

export PIP_DEFAULT_TIMEOUT="${PIP_DEFAULT_TIMEOUT:-120}"
export PIP_RETRIES="${PIP_RETRIES:-10}"

common=(
  -m piptools compile
  --generate-hashes
  --resolver=backtracking
  --strip-extras
  --reuse-hashes
  --no-emit-index-url
  --no-emit-trusted-host
)

# Torch, TorchVision and their CUDA wheels are part of the immutable base image.
# They remain in the resolver graph (Torch is required by Chemprop), but are
# intentionally omitted from the application lock. Docker verifies the separate
# reviewed system lock before installing the application lock.
system_packages=(
  distribute
  pip
  setuptools
  torch
  torchvision
  triton
  nvidia-cublas-cu12
  nvidia-cuda-cupti-cu12
  nvidia-cuda-nvrtc-cu12
  nvidia-cuda-runtime-cu12
  nvidia-cudnn-cu12
  nvidia-cufft-cu12
  nvidia-curand-cu12
  nvidia-cusolver-cu12
  nvidia-cusparse-cu12
  nvidia-cusparselt-cu12
  nvidia-nccl-cu12
  nvidia-nvjitlink-cu12
  nvidia-nvtx-cu12
)
backend_args=(
  "${common[@]}"
  --constraint=backend/requirements-system.in
  --extra-index-url="$PYTORCH_INDEX_URL"
  --no-allow-unsafe
  --pip-args="--only-binary=:all: --timeout=$PIP_DEFAULT_TIMEOUT --retries=$PIP_RETRIES"
)
for package in "${system_packages[@]}"; do
  backend_args+=(--unsafe-package="$package")
done

"$PYTHON_BIN" "${backend_args[@]}" \
  --output-file=backend/requirements.lock \
  backend/requirements-runtime.in

"$PYTHON_BIN" "${common[@]}" --allow-unsafe \
  --output-file=backend/requirements-ci.lock \
  backend/requirements-monomer-md-ci.txt
"$PYTHON_BIN" "${common[@]}" --allow-unsafe \
  --output-file=workers/monomer_md_worker/requirements.lock \
  workers/monomer_md_worker/requirements.txt
"$PYTHON_BIN" "${common[@]}" --allow-unsafe \
  --output-file=workers/monomer_md_worker/requirements-ci.lock \
  workers/monomer_md_worker/requirements-ci.txt

"$PYTHON_BIN" scripts/ci/validate_dependency_locks.py

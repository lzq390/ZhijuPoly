#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

fail() {
  printf 'production DFT runtime: ERROR: %s\n' "$*" >&2
  exit 2
}

readonly REPO_ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly PRODUCTION_REPO_ROOT="/data/lzq/gith/nexpoly"
readonly RUNTIME_ROOT="/data/lzq/gith/nexpoly-runtime"
readonly ARTIFACT_ROOT="${MONOMER_DFT_PROD_ARTIFACT_ROOT:-$RUNTIME_ROOT/bootstrap-input/monomer-dft}"
readonly SOURCE_LOCK="$REPO_ROOT/workers/monomer_dft_worker/aimnet-source.lock.json"
readonly REQUIREMENTS_LOCK="$REPO_ROOT/workers/monomer_dft_worker/requirements.lock"
readonly RUNTIME_ENV="$RUNTIME_ROOT/config/monomer-dft-runtime.env"

[[ "$REPO_ROOT" == "$PRODUCTION_REPO_ROOT" ]] || fail "must run from the production checkout"
[[ "$(git -C "$REPO_ROOT" rev-parse --show-toplevel)" == "$REPO_ROOT" ]] || fail "invalid production checkout"
git -C "$REPO_ROOT" diff --quiet --ignore-submodules -- || fail "production source is dirty"
git -C "$REPO_ROOT" diff --cached --quiet --ignore-submodules -- || fail "production index is dirty"
release_sha="$(git -C "$REPO_ROOT" rev-parse HEAD)"
release_tree="$(git -C "$REPO_ROOT" rev-parse HEAD^{tree})"
if [[ -n "${MONOMER_DFT_EXPECTED_GIT_REF:-}" ]]; then
  expected="$(git -C "$REPO_ROOT" rev-parse --verify "${MONOMER_DFT_EXPECTED_GIT_REF}^{commit}")"
  [[ "$release_sha" == "$expected" ]] || fail "production HEAD differs from expected release"
fi
[[ -d "$RUNTIME_ROOT" && ! -L "$RUNTIME_ROOT" ]] || fail "production runtime root is unavailable"
[[ "$(stat -Lc '%u:%a' "$RUNTIME_ROOT")" == "$(id -u):700" ]] || fail "production runtime root must be owner-only 0700"
readonly RELEASE_ROOT="$RUNTIME_ROOT/worker-venvs/dft/$release_sha"
readonly STAGING_ROOT="$RUNTIME_ROOT/worker-venvs/dft/.$release_sha.build-$$"
[[ ! -e "$RELEASE_ROOT" ]] || fail "release runtime already exists; refusing to overwrite it"
[[ ! -e "$STAGING_ROOT" ]] || fail "release staging runtime already exists"

python312="$(command -v python3.12 || true)"
uv_bin="$(command -v uv || true)"
[[ -x "$python312" ]] || fail "Python 3.12 is required"
[[ -x "$uv_bin" && "$("$uv_bin" --version)" == "uv 0.11.21" ]] || fail "uv 0.11.21 is required"

mapfile -t artifacts < <(
  python3 -I - "$SOURCE_LOCK" <<'PY'
import json, pathlib, sys
data = json.loads(pathlib.Path(sys.argv[1]).read_text())
print(data["wheel"]["filename"] + "\t" + data["wheel"]["sha256"])
for model in data["models"]:
    print(model["file"] + "\t" + model["sha256"])
PY
)
for artifact in "${artifacts[@]}"; do
  IFS=$'\t' read -r filename expected_sha <<<"$artifact"
  path="$ARTIFACT_ROOT/$filename"
  [[ -f "$path" && ! -L "$path" ]] || fail "missing immutable artifact: $path"
  [[ "$(stat -Lc '%u:%a' "$path")" == "$(id -u):600" ]] || fail "artifact must be owner-only mode 0600: $filename"
  [[ "$(sha256sum "$path" | awk '{print $1}')" == "$expected_sha" ]] || fail "artifact SHA mismatch: $filename"
done

mkdir -p "$RUNTIME_ROOT/worker-venvs/dft"
chmod 0700 "$RUNTIME_ROOT/worker-venvs" "$RUNTIME_ROOT/worker-venvs/dft"
mkdir -m 0700 "$STAGING_ROOT"
export UV_CACHE_DIR="$STAGING_ROOT/uv-cache"
"$uv_bin" venv --python "$python312" "$STAGING_ROOT/venv"
"$uv_bin" pip install --python "$STAGING_ROOT/venv/bin/python" \
  --require-hashes --no-deps -r "$REQUIREMENTS_LOCK"
IFS=$'\t' read -r wheel_name _ <<<"${artifacts[0]}"
"$uv_bin" pip install --python "$STAGING_ROOT/venv/bin/python" \
  --no-deps "$ARTIFACT_ROOT/$wheel_name"

mkdir -m 0700 "$STAGING_ROOT/aimnet-cache" "$STAGING_ROOT/warp-cache"
for artifact in "${artifacts[@]:1}"; do
  IFS=$'\t' read -r filename _ <<<"$artifact"
  install -m 0600 "$ARTIFACT_ROOT/$filename" "$STAGING_ROOT/aimnet-cache/$filename"
done
mkdir -p \
  "$RUNTIME_ROOT/state/monomer-dft-worker-socket" \
  "$RUNTIME_ROOT/state/monomer-dft-worker-runs" \
  "$RUNTIME_ROOT/state/monomer-dft-download-spool"
chmod 0700 \
  "$RUNTIME_ROOT/state/monomer-dft-worker-socket" \
  "$RUNTIME_ROOT/state/monomer-dft-worker-runs" \
  "$RUNTIME_ROOT/state/monomer-dft-download-spool"
python3 -I - \
  "$STAGING_ROOT/runtime.json" \
  "$release_sha" \
  "$release_tree" \
  "$(sha256sum "$REQUIREMENTS_LOCK" | awk '{print $1}')" \
  "$(sha256sum "$SOURCE_LOCK" | awk '{print $1}')" <<'PY'
import datetime, json, os, pathlib, sys

path = pathlib.Path(sys.argv[1])
payload = {
    "schema_version": 1,
    "release": sys.argv[2],
    "source_tree": sys.argv[3],
    "created_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "python": "3.12",
    "uv": "0.11.21",
    "requirements_lock_sha256": "sha256:" + sys.argv[4],
    "aimnet_source_lock_sha256": "sha256:" + sys.argv[5],
}
path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
os.chmod(path, 0o600)
PY
runtime_contract_sha256="sha256:$(sha256sum "$STAGING_ROOT/runtime.json" | awk '{print $1}')"
mv -- "$STAGING_ROOT" "$RELEASE_ROOT"
python3 -I - \
  "$RUNTIME_ENV" \
  "$release_sha" \
  "$runtime_contract_sha256" \
  "$RELEASE_ROOT" <<'PY'
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
release, contract, root = sys.argv[2:]
payload = "\n".join(
    (
        f"MONOMER_DFT_RELEASE_SHA={release}",
        f"MONOMER_DFT_RUNTIME_CONTRACT_SHA256={contract}",
        f"MONOMER_DFT_PYTHON={root}/venv/bin/python",
        f"AIMNET_CACHE_DIR={root}/aimnet-cache",
        f"WARP_CACHE_PATH={root}/warp-cache",
        "",
    )
)
descriptor = os.open(
    temporary,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
    0o600,
)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)
finally:
    if temporary.exists():
        temporary.unlink()
PY
printf 'production DFT runtime: ready at %s (%s)\n' \
  "$RELEASE_ROOT" "$runtime_contract_sha256"

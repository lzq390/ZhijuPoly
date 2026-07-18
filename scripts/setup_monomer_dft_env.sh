#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly EXPECTED_PYTHON_MINOR="3.12"
readonly EXPECTED_UV_VERSION="0.11.21"

readonly MODE="${1:-setup}"

fail() {
  printf 'monomer DFT setup: ERROR: %s\n' "$*" >&2
  exit 2
}

log() {
  printf 'monomer DFT setup: %s\n' "$*"
}

[[ "$#" -le 1 ]] || fail "usage: $0 [--check-repository|--check-aimnet-source]"
[[ "$MODE" == "setup" || "$MODE" == "--check-repository" || "$MODE" == "--check-aimnet-source" ]] \
  || fail "usage: $0 [--check-repository|--check-aimnet-source]"

assert_no_symlink_components() {
  local target="$1"
  local cursor="/"
  local component
  local -a components=()
  [[ "$target" == /* ]] || fail "path safety check requires an absolute path: $target"
  IFS='/' read -r -a components <<<"${target#/}"
  for component in "${components[@]}"; do
    [[ -n "$component" ]] || continue
    cursor="${cursor%/}/$component"
    [[ ! -L "$cursor" ]] || fail "symlink path component is forbidden: $cursor"
  done
}

assert_runtime_target() {
  local target
  target="$(realpath -ms -- "$1")"
  [[ "$target" == "$RUNTIME_ROOT" || "$target" == "$RUNTIME_ROOT/"* ]] || fail "runtime target escapes $RUNTIME_ROOT: $target"
  assert_no_symlink_components "$target"
}

sha256_file() {
  sha256sum "$1" | awk '{print $1}'
}

directory_digest() {
  python3 -I - "$1" <<'PY'
import hashlib
import pathlib
import stat
import sys

root = pathlib.Path(sys.argv[1])
digest = hashlib.sha256()
for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
    relative = path.relative_to(root).as_posix()
    metadata = path.lstat()
    if stat.S_ISDIR(metadata.st_mode):
        kind = b"d"
        content = b""
    elif stat.S_ISREG(metadata.st_mode):
        kind = b"f"
        content = path.read_bytes()
    else:
        raise SystemExit(f"unsafe archive entry: {relative}")
    digest.update(kind)
    digest.update(b"\0")
    digest.update(relative.encode("utf-8"))
    digest.update(b"\0")
    digest.update(f"{stat.S_IMODE(metadata.st_mode):04o}".encode("ascii"))
    digest.update(b"\0")
    digest.update(str(len(content)).encode("ascii"))
    digest.update(b"\0")
    digest.update(content)
print(digest.hexdigest())
PY
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
readonly REPO_ROOT
readonly PRODUCTION_REPO_ROOT="/data/lzq/gith/nexpoly"
readonly RUNTIME_ROOT="$REPO_ROOT/.runtime"
readonly DEFAULT_AIMNET_CLONE="$RUNTIME_ROOT/aimnet-source-clone"
[[ "$REPO_ROOT" != "$PRODUCTION_REPO_ROOT" ]] \
  || fail "development DFT setup is forbidden in the production repository"
[[ "${MONOMER_DFT_DEPLOYMENT:-dev}" == "dev" ]] \
  || fail "MONOMER_DFT_DEPLOYMENT must be exactly dev; production mode is forbidden"
[[ "${NEXPOLY_DFT_GPU_DEVICE:-1}" == "1" ]] \
  || fail "development primary GPU must be physical GPU 1; GPUs 0 and 2 are forbidden"
[[ "${NEXPOLY_DFT_OVERFLOW_GPU_DEVICES:-3}" == "3" ]] \
  || fail "development overflow GPU must be physical GPU 3 only; GPUs 0 and 2 are forbidden"
cd "$REPO_ROOT"

[[ -z "${PYTHONPATH:-}" ]] || fail "PYTHONPATH must be unset; source-tree imports are forbidden"
unset PYTHONPATH
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
export CUDA_DEVICE_ORDER=PCI_BUS_ID

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "$REPO_ROOT is not a Git worktree"
GIT_TOPLEVEL="$(git rev-parse --show-toplevel)"
readonly GIT_TOPLEVEL
[[ "$(realpath -e -- "$GIT_TOPLEVEL")" == "$REPO_ROOT" ]] || fail "script directory is not the current Git worktree root"
git diff --quiet --ignore-submodules -- || fail "tracked source changes must be committed before environment setup"
git diff --cached --quiet --ignore-submodules -- || fail "staged source changes must be committed before environment setup"
if [[ -n "${MONOMER_DFT_EXPECTED_GIT_REF:-}" ]]; then
  EXPECTED_GIT_COMMIT="$(git rev-parse --verify "${MONOMER_DFT_EXPECTED_GIT_REF}^{commit}" 2>/dev/null)" \
    || fail "MONOMER_DFT_EXPECTED_GIT_REF cannot be resolved"
  readonly EXPECTED_GIT_COMMIT
  [[ "$(git rev-parse HEAD)" == "$EXPECTED_GIT_COMMIT" ]] || fail "HEAD does not match MONOMER_DFT_EXPECTED_GIT_REF"
fi

git check-ignore -q .runtime/probe || fail ".runtime/ is not ignored; refusing to create local artifacts"
git check-ignore -q .env.monomer-dft.dev || fail ".env.monomer-dft.dev is not ignored; refusing to create local configuration"
[[ -z "$(git ls-files -- .runtime .env.monomer-dft.dev)" ]] || fail "runtime paths must not be tracked"

readonly REQUIREMENTS_LOCK="$REPO_ROOT/workers/monomer_dft_worker/requirements.lock"
readonly BUILD_REQUIREMENTS_LOCK="$REPO_ROOT/workers/monomer_dft_worker/build-requirements.lock"
readonly SOURCE_LOCK="$REPO_ROOT/workers/monomer_dft_worker/aimnet-source.lock.json"
readonly ENV_EXAMPLE="$REPO_ROOT/.env.monomer-dft.dev.example"
readonly MIGRATION_MANIFEST="$REPO_ROOT/backend/migrations/postgres/manifest.json"
readonly GPU_BROKER_CLIENT="$REPO_ROOT/gpu_resource/client.py"
readonly GPU_GOVERNED_COMPOSE="$REPO_ROOT/docker-compose.gpu-governed.yml"
[[ -f "$REQUIREMENTS_LOCK" ]] || fail "missing tracked dependency lock: $REQUIREMENTS_LOCK"
[[ -f "$BUILD_REQUIREMENTS_LOCK" ]] || fail "missing tracked build dependency lock: $BUILD_REQUIREMENTS_LOCK"
[[ -f "$SOURCE_LOCK" ]] || fail "missing tracked AIMNet lock: $SOURCE_LOCK"
[[ -f "$ENV_EXAMPLE" ]] || fail "missing tracked environment example: $ENV_EXAMPLE"
[[ -f "$MIGRATION_MANIFEST" ]] || fail "missing governed migration manifest: $MIGRATION_MANIFEST"
[[ -f "$GPU_BROKER_CLIENT" ]] || fail "missing governed GPU Broker client: $GPU_BROKER_CLIENT"
[[ -f "$GPU_GOVERNED_COMPOSE" ]] || fail "missing governed GPU Compose contract: $GPU_GOVERNED_COMPOSE"
for tracked_asset in \
  "$REQUIREMENTS_LOCK" \
  "$BUILD_REQUIREMENTS_LOCK" \
  "$SOURCE_LOCK" \
  "$ENV_EXAMPLE" \
  "$MIGRATION_MANIFEST" \
  "$GPU_BROKER_CLIENT" \
  "$GPU_GOVERNED_COMPOSE"; do
  git ls-files --error-unmatch -- "${tracked_asset#"$REPO_ROOT/"}" >/dev/null 2>&1 \
    || fail "required environment asset is not tracked: $tracked_asset"
done

python3 -I - "$REPO_ROOT" "$MIGRATION_MANIFEST" <<'PY' \
  || fail "migration epoch V2 and the 0012/0013 contract bridge are invalid"
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
manifest = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
if manifest.get("schema_version") != 2:
    raise SystemExit(1)
entries = {entry["version"]: entry for entry in manifest.get("migrations", [])}
contract = entries.get("0012_drop_polytao_jobs")
dft = entries.get("0013_monomer_dft_jobs")
if not contract or not dft or contract.get("kind") != "contract" or contract.get("epoch") != 1:
    raise SystemExit(1)
if dft.get("kind") != "expand" or dft.get("epoch") != 2:
    raise SystemExit(1)
expected_requirement = {
    "version": contract["version"],
    "checksum": contract["checksum"],
}
if dft.get("requires_contracts") != [expected_requirement]:
    raise SystemExit(1)
for entry in (contract, dft):
    path = root / "backend" / "migrations" / "postgres" / f"{entry['version']}.sql"
    normalized = path.read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(normalized).hexdigest() != entry.get("checksum"):
        raise SystemExit(1)
PY

python3 -I - "$SOURCE_LOCK" <<'PY' \
  || fail "AIMNet source lock contains unsafe model metadata"
import json
import pathlib
import sys

data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
models = data.get("models")
if data.get("schema_version") != 1 or not isinstance(models, list) or len(models) != 6:
    raise SystemExit(1)
for model in models:
    if not isinstance(model, dict):
        raise SystemExit(1)
    model_file = model.get("file")
    if (
        not isinstance(model_file, str)
        or not model_file
        or model_file in {".", ".."}
        or "/" in model_file
        or "\\" in model_file
        or any(ord(character) < 32 or ord(character) == 127 for character in model_file)
    ):
        raise SystemExit(1)
PY

if [[ "$MODE" == "--check-repository" ]]; then
  log "repository governance checks passed for $REPO_ROOT at $(git rev-parse HEAD)"
  exit 0
fi

BOOTSTRAP_PYTHON="$(command -v python3.12 || true)"
readonly BOOTSTRAP_PYTHON
[[ -n "$BOOTSTRAP_PYTHON" ]] || fail "system python3.12 is required"
PYTHON_MINOR="$(
  "$BOOTSTRAP_PYTHON" -I -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
)"
readonly PYTHON_MINOR
[[ "$PYTHON_MINOR" == "$EXPECTED_PYTHON_MINOR" ]] || fail "expected Python $EXPECTED_PYTHON_MINOR, found $PYTHON_MINOR"

UV_BIN="$(command -v uv || true)"
readonly UV_BIN
[[ -n "$UV_BIN" ]] || fail "uv $EXPECTED_UV_VERSION is required"
UV_VERSION="$($UV_BIN --version | awk '{print $2}')"
readonly UV_VERSION
[[ "$UV_VERSION" == "$EXPECTED_UV_VERSION" ]] || fail "expected uv $EXPECTED_UV_VERSION, found $UV_VERSION"

mapfile -t SOURCE_META < <(
  "$BOOTSTRAP_PYTHON" -I - "$SOURCE_LOCK" <<'PY'
import json
import pathlib
import os
import sys

lock_path = pathlib.Path(sys.argv[1])
data = json.loads(lock_path.read_text(encoding="utf-8"))
if data.get("schema_version") != 1:
    raise SystemExit("unsupported AIMNet lock schema")
source = data["source"]
wheel = data["wheel"]
registry = data["registry"]
required = (
    source["repository_url"],
    source["commit"],
    source["tree"],
    source["package_name"],
    source["package_version"],
    source["wheel_install_mode"],
    source["python_minor"],
    source["uv_version"],
    source["source_date_epoch"],
    source["build_requirements_sha256"],
    wheel["filename"],
    wheel["sha256"],
    wheel["file_count"],
    wheel["inventory_sha256"],
    wheel["record_path"],
    wheel["record_sha256"],
    registry["path"],
    registry["sha256"],
)
if any("\n" in str(value) for value in required):
    raise SystemExit("newline in AIMNet lock metadata")
print(*required, sep="\n")
PY
)
[[ "${#SOURCE_META[@]}" -eq 18 ]] || fail "invalid AIMNet source metadata"
readonly AIMNET_REPOSITORY_URL="${SOURCE_META[0]}"
readonly AIMNET_COMMIT="${SOURCE_META[1]}"
readonly AIMNET_TREE="${SOURCE_META[2]}"
readonly AIMNET_PACKAGE_NAME="${SOURCE_META[3]}"
readonly AIMNET_PACKAGE_VERSION="${SOURCE_META[4]}"
readonly AIMNET_WHEEL_MODE="${SOURCE_META[5]}"
readonly AIMNET_PYTHON_MINOR="${SOURCE_META[6]}"
readonly AIMNET_UV_VERSION="${SOURCE_META[7]}"
readonly AIMNET_SOURCE_DATE_EPOCH="${SOURCE_META[8]}"
readonly AIMNET_BUILD_REQUIREMENTS_SHA="${SOURCE_META[9]}"
readonly AIMNET_WHEEL_FILENAME="${SOURCE_META[10]}"
readonly AIMNET_EXPECTED_WHEEL_SHA="${SOURCE_META[11]}"
readonly AIMNET_WHEEL_FILE_COUNT="${SOURCE_META[12]}"
readonly AIMNET_WHEEL_INVENTORY_SHA="${SOURCE_META[13]}"
readonly AIMNET_WHEEL_RECORD_PATH="${SOURCE_META[14]}"
readonly AIMNET_WHEEL_RECORD_SHA="${SOURCE_META[15]}"
readonly AIMNET_REGISTRY_REL="${SOURCE_META[16]}"
readonly AIMNET_REGISTRY_SHA="${SOURCE_META[17]}"
[[ "$AIMNET_PACKAGE_NAME" == "aimnet" ]] || fail "unexpected AIMNet package name: $AIMNET_PACKAGE_NAME"
[[ "$AIMNET_WHEEL_MODE" == "non-editable" ]] || fail "AIMNet lock must require a non-editable wheel"
[[ "$AIMNET_PYTHON_MINOR" == "$EXPECTED_PYTHON_MINOR" ]] || fail "AIMNet Python lock does not match setup"
[[ "$AIMNET_UV_VERSION" == "$EXPECTED_UV_VERSION" ]] || fail "AIMNet uv lock does not match setup"
[[ "$AIMNET_SOURCE_DATE_EPOCH" =~ ^[1-9][0-9]*$ ]] || fail "AIMNet SOURCE_DATE_EPOCH is invalid"
[[ "$AIMNET_WHEEL_FILE_COUNT" =~ ^[1-9][0-9]*$ ]] || fail "AIMNet wheel file count is invalid"
[[ "$(sha256_file "$BUILD_REQUIREMENTS_LOCK")" == "$AIMNET_BUILD_REQUIREMENTS_SHA" ]] \
  || fail "AIMNet build dependency lock checksum does not match the source lock"
[[ "$AIMNET_REGISTRY_REL" != /* && "$AIMNET_REGISTRY_REL" != *".."* ]] || fail "unsafe registry path in AIMNet lock"

AIMNET_CLONE="$(realpath -ms -- "${AIMNET_SOURCE_CLONE:-$DEFAULT_AIMNET_CLONE}")"
readonly AIMNET_CLONE
assert_runtime_target "$AIMNET_CLONE"
[[ -d "$AIMNET_CLONE/.git" ]] || fail "AIMNet source clone is missing: $AIMNET_CLONE"
[[ "$(stat -c '%u' "$AIMNET_CLONE")" == "$(id -u)" ]] \
  || fail "AIMNet source clone must be owned by uid $(id -u)"
AIMNET_GIT_COMMON_DIR="$(
  env GIT_NO_REPLACE_OBJECTS=1 GIT_NO_LAZY_FETCH=1 \
    git -C "$AIMNET_CLONE" rev-parse --path-format=absolute --git-common-dir
)"
readonly AIMNET_GIT_COMMON_DIR
[[ "$AIMNET_GIT_COMMON_DIR" == "$AIMNET_CLONE/.git" ]] \
  || fail "AIMNet source must be a standalone clone without shared Git objects"
[[ ! -e "$AIMNET_GIT_COMMON_DIR/objects/info/alternates" ]] \
  || fail "AIMNet source clone must not use object alternates"
[[ "$(
  env GIT_NO_REPLACE_OBJECTS=1 GIT_NO_LAZY_FETCH=1 \
    git -C "$AIMNET_CLONE" rev-parse --is-shallow-repository
)" == "false" ]] || fail "AIMNet source clone must contain complete history"
[[ -z "$(
  env GIT_NO_REPLACE_OBJECTS=1 GIT_NO_LAZY_FETCH=1 \
    git -C "$AIMNET_CLONE" config --get extensions.partialClone 2>/dev/null || true
)" ]] || fail "AIMNet source clone must not be partial"
AIMNET_SOURCE_STATUS="$(
  env GIT_NO_REPLACE_OBJECTS=1 GIT_NO_LAZY_FETCH=1 \
    git -C "$AIMNET_CLONE" status \
    --porcelain=v1 \
    --untracked-files=all \
    --ignored=matching \
    --ignore-submodules=none
)"
readonly AIMNET_SOURCE_STATUS
[[ -z "$AIMNET_SOURCE_STATUS" ]] \
  || fail "AIMNet source clone is dirty or contains ignored entries; use a fresh clean clone"
[[ "$(
  env GIT_NO_REPLACE_OBJECTS=1 GIT_NO_LAZY_FETCH=1 \
    git -C "$AIMNET_CLONE" remote get-url --all origin
)" == "$AIMNET_REPOSITORY_URL" ]] || fail "AIMNet fetch origin does not match the lock exactly"
[[ "$(
  env GIT_NO_REPLACE_OBJECTS=1 GIT_NO_LAZY_FETCH=1 \
    git -C "$AIMNET_CLONE" remote get-url --push --all origin
)" == "$AIMNET_REPOSITORY_URL" ]] || fail "AIMNet push origin does not match the lock exactly"
env GIT_NO_REPLACE_OBJECTS=1 GIT_NO_LAZY_FETCH=1 \
  git -C "$AIMNET_CLONE" cat-file -e "${AIMNET_COMMIT}^{commit}" \
  || fail "locked AIMNet commit is unavailable"
[[ "$(
  env GIT_NO_REPLACE_OBJECTS=1 GIT_NO_LAZY_FETCH=1 \
    git -C "$AIMNET_CLONE" rev-parse HEAD
)" == "$AIMNET_COMMIT" ]] || fail "clean AIMNet clone HEAD must equal the locked commit"
[[ "$(
  env GIT_NO_REPLACE_OBJECTS=1 GIT_NO_LAZY_FETCH=1 \
    git -C "$AIMNET_CLONE" rev-parse "${AIMNET_COMMIT}^{tree}"
)" == "$AIMNET_TREE" ]] \
  || fail "AIMNet commit tree does not match the source lock"
[[ "$(
  env GIT_NO_REPLACE_OBJECTS=1 GIT_NO_LAZY_FETCH=1 \
    git -C "$AIMNET_CLONE" show -s --format=%ct "$AIMNET_COMMIT"
)" == "$AIMNET_SOURCE_DATE_EPOCH" ]] \
  || fail "AIMNet commit timestamp does not match SOURCE_DATE_EPOCH"
[[ -z "$(
  env GIT_NO_REPLACE_OBJECTS=1 GIT_NO_LAZY_FETCH=1 \
    git -C "$AIMNET_CLONE" ls-tree -r "$AIMNET_COMMIT" \
      | awk '$1 == "160000" {print; exit}'
)" ]] \
  || fail "AIMNet archive contains a submodule that is not recursively locked"
if [[ "$MODE" == "--check-aimnet-source" ]]; then
  log "clean AIMNet source checks passed for $AIMNET_CLONE at $AIMNET_COMMIT"
  exit 0
fi

readonly VENV_ROOT="$RUNTIME_ROOT/venvs/monomer-dft-worker"
readonly VENV_PYTHON="$VENV_ROOT/bin/python"
readonly WHEELHOUSE="$RUNTIME_ROOT/wheelhouse"
readonly AIMNET_ARCHIVE_ROOT="$RUNTIME_ROOT/aimnet-source-archive"
readonly AIMNET_ARCHIVE_EVIDENCE="$RUNTIME_ROOT/aimnet-source-archive.json"
readonly AIMNET_CACHE="$RUNTIME_ROOT/aimnet-cache"
readonly WARP_CACHE="$RUNTIME_ROOT/warp-cache"
readonly UV_CACHE="$RUNTIME_ROOT/uv-cache"
readonly SOCKET_DIR="$RUNTIME_ROOT/monomer-dft-worker-socket"
readonly JOB_ROOT="$RUNTIME_ROOT/monomer-dft-worker-runs"
readonly GPU_RUNTIME_ROOT="$RUNTIME_ROOT/gpu-resource"
readonly SMOKE_RUN_ROOT="$RUNTIME_ROOT/runs"
readonly DOWNLOAD_SPOOL_ROOT="$RUNTIME_ROOT/monomer-dft-download-spool"
readonly CTL_LOCK="$RUNTIME_ROOT/monomer-dft-worker.ctl.lock"
readonly WORKER_PID_FILE="$RUNTIME_ROOT/monomer-dft-worker.pid"
readonly WORKER_UDS="$SOCKET_DIR/worker.sock"
readonly REAL_ENV="$REPO_ROOT/.env.monomer-dft.dev"

assert_no_symlink_components "$REPO_ROOT"
for runtime_target in "$RUNTIME_ROOT" "$RUNTIME_ROOT/venvs" "$VENV_ROOT" "$WHEELHOUSE" \
  "$AIMNET_ARCHIVE_ROOT" "$AIMNET_ARCHIVE_EVIDENCE" "$AIMNET_CACHE" \
  "$WARP_CACHE" "$UV_CACHE" "$SOCKET_DIR" "$JOB_ROOT" "$SMOKE_RUN_ROOT" \
  "$GPU_RUNTIME_ROOT" "$DOWNLOAD_SPOOL_ROOT"; do
  assert_runtime_target "$runtime_target"
done
assert_runtime_target "$CTL_LOCK"
assert_runtime_target "$WORKER_PID_FILE"
assert_runtime_target "$WORKER_UDS"
assert_no_symlink_components "$REAL_ENV"
[[ ! -e "$REAL_ENV" || ( -f "$REAL_ENV" && ! -L "$REAL_ENV" ) ]] || fail "local environment must be a regular non-symlink: $REAL_ENV"

mkdir -p "$RUNTIME_ROOT/venvs" "$WHEELHOUSE" "$AIMNET_CACHE" "$WARP_CACHE" \
  "$UV_CACHE" "$SOCKET_DIR" "$JOB_ROOT" "$SMOKE_RUN_ROOT" "$GPU_RUNTIME_ROOT" \
  "$DOWNLOAD_SPOOL_ROOT"
for private_directory in \
  "$RUNTIME_ROOT" "$RUNTIME_ROOT/venvs" "$WHEELHOUSE" "$AIMNET_CACHE" \
  "$WARP_CACHE" "$UV_CACHE" "$SOCKET_DIR" "$JOB_ROOT" "$SMOKE_RUN_ROOT" \
  "$GPU_RUNTIME_ROOT" "$DOWNLOAD_SPOOL_ROOT"; do
  [[ -d "$private_directory" && ! -L "$private_directory" ]] \
    || fail "runtime directory is unsafe: $private_directory"
  [[ "$(stat -c '%u' "$private_directory")" == "$(id -u)" ]] \
    || fail "runtime directory must be owned by uid $(id -u): $private_directory"
  chmod 0700 "$private_directory"
done
assert_runtime_target "$CTL_LOCK"
if [[ -e "$CTL_LOCK" ]]; then
  [[ -f "$CTL_LOCK" && ! -L "$CTL_LOCK" ]] || fail "worker control lock must be a regular non-symlink: $CTL_LOCK"
else
  : >"$CTL_LOCK"
fi
chmod 0600 "$CTL_LOCK"
exec {SETUP_LOCK_FD}<>"$CTL_LOCK"
readonly SETUP_LOCK_FD
[[ "$(readlink -f -- "/proc/$$/fd/$SETUP_LOCK_FD")" == "$CTL_LOCK" ]] || fail "worker control lock escaped the runtime"
flock -n "$SETUP_LOCK_FD" || fail "worker control lock is busy; stop the worker or concurrent setup first"
assert_runtime_target "$WORKER_PID_FILE"
assert_runtime_target "$WORKER_UDS"
[[ ! -e "$WORKER_PID_FILE" ]] || fail "managed worker PID state exists; stop the worker before setup: $WORKER_PID_FILE"
[[ ! -e "$WORKER_UDS" ]] || fail "worker socket exists; stop or clean managed worker state before setup: $WORKER_UDS"

assert_runtime_target "$AIMNET_ARCHIVE_ROOT"
assert_runtime_target "$AIMNET_ARCHIVE_EVIDENCE"
rm -rf -- "$AIMNET_ARCHIVE_ROOT"
rm -f -- "$AIMNET_ARCHIVE_EVIDENCE"
install -d -m 0700 "$AIMNET_ARCHIVE_ROOT"
env GIT_NO_REPLACE_OBJECTS=1 GIT_NO_LAZY_FETCH=1 \
  git -C "$AIMNET_CLONE" archive --format=tar "$AIMNET_COMMIT" \
  | tar --extract --file=- --directory="$AIMNET_ARCHIVE_ROOT" \
      --no-same-owner --no-same-permissions
UNSAFE_ARCHIVE_ENTRY="$(
  find "$AIMNET_ARCHIVE_ROOT" -mindepth 1 \
    ! -type d ! -type f -print -quit
)"
readonly UNSAFE_ARCHIVE_ENTRY
[[ -z "$UNSAFE_ARCHIVE_ENTRY" ]] \
  || fail "AIMNet clean archive contains an unsafe entry: $UNSAFE_ARCHIVE_ENTRY"
AIMNET_ARCHIVE_DIGEST_BEFORE="$(directory_digest "$AIMNET_ARCHIVE_ROOT")"
readonly AIMNET_ARCHIVE_DIGEST_BEFORE
readonly AIMNET_REGISTRY="$AIMNET_ARCHIVE_ROOT/$AIMNET_REGISTRY_REL"
[[ -f "$AIMNET_REGISTRY" && ! -L "$AIMNET_REGISTRY" ]] \
  || fail "AIMNet registry is missing from the clean archive: $AIMNET_REGISTRY"
[[ "$(sha256_file "$AIMNET_REGISTRY")" == "$AIMNET_REGISTRY_SHA" ]] \
  || fail "AIMNet clean-archive registry checksum does not match the lock"
"$BOOTSTRAP_PYTHON" -I - \
  "$AIMNET_ARCHIVE_EVIDENCE" \
  "$AIMNET_COMMIT" \
  "$AIMNET_TREE" \
  "$AIMNET_SOURCE_DATE_EPOCH" \
  "$AIMNET_ARCHIVE_DIGEST_BEFORE" <<'PY'
import json
import os
import pathlib
import sys

destination = pathlib.Path(sys.argv[1])
payload = {
    "schema_version": 1,
    "commit": sys.argv[2],
    "tree": sys.argv[3],
    "source_date_epoch": int(sys.argv[4]),
    "archive_inventory_sha256": sys.argv[5],
}
temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
with temporary.open("x", encoding="utf-8") as handle:
    json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(temporary, 0o400)
os.replace(temporary, destination)
directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY

assert_runtime_target "$AIMNET_CACHE"
chmod u+rwx "$AIMNET_CACHE"
export UV_CACHE_DIR="$UV_CACHE"
export UV_LINK_MODE=copy
export UV_PYTHON_DOWNLOADS=never

log "building AIMNet $AIMNET_PACKAGE_VERSION from pinned commit $AIMNET_COMMIT"
assert_runtime_target "$WHEELHOUSE"
for stale_wheel_asset in \
  "$WHEELHOUSE"/aimnet-*.whl \
  "$WHEELHOUSE/aimnet-wheel.sha256" \
  "$WHEELHOUSE/aimnet-wheel-manifest.json"; do
  [[ -e "$stale_wheel_asset" || -L "$stale_wheel_asset" ]] || continue
  [[ -f "$stale_wheel_asset" && ! -L "$stale_wheel_asset" ]] \
    || fail "stale wheelhouse asset is unsafe: $stale_wheel_asset"
  [[ "$(stat -c '%u' "$stale_wheel_asset")" == "$(id -u)" ]] \
    || fail "stale wheelhouse asset is not owned by the setup uid: $stale_wheel_asset"
  chmod 0600 "$stale_wheel_asset"
  rm -f -- "$stale_wheel_asset"
done
SETUPTOOLS_SCM_PRETEND_VERSION="$AIMNET_PACKAGE_VERSION" \
SOURCE_DATE_EPOCH="$AIMNET_SOURCE_DATE_EPOCH" \
"$UV_BIN" build \
  --wheel \
  --clear \
  --no-create-gitignore \
  --python "$BOOTSTRAP_PYTHON" \
  --build-constraints "$BUILD_REQUIREMENTS_LOCK" \
  --require-hashes \
  --out-dir "$WHEELHOUSE" \
  "$AIMNET_ARCHIVE_ROOT"

mapfile -t AIMNET_WHEELS < <(find "$WHEELHOUSE" -maxdepth 1 -type f -name 'aimnet-*.whl' -print | LC_ALL=C sort)
[[ "${#AIMNET_WHEELS[@]}" -eq 1 ]] || fail "expected one AIMNet wheel, found ${#AIMNET_WHEELS[@]}"
readonly AIMNET_WHEEL="${AIMNET_WHEELS[0]}"
[[ -f "$AIMNET_WHEEL" && ! -L "$AIMNET_WHEEL" ]] || fail "AIMNet wheel must be a regular non-symlink"
AIMNET_WHEEL_SHA="$(sha256_file "$AIMNET_WHEEL")"
readonly AIMNET_WHEEL_SHA
[[ "$(basename "$AIMNET_WHEEL")" == "$AIMNET_WHEEL_FILENAME" ]] \
  || fail "AIMNet wheel filename does not match the source lock"
[[ "$AIMNET_WHEEL_SHA" == "$AIMNET_EXPECTED_WHEEL_SHA" ]] \
  || fail "AIMNet wheel digest does not match the deterministic source lock"
printf '%s  %s\n' "$AIMNET_WHEEL_SHA" "$(basename "$AIMNET_WHEEL")" >"$WHEELHOUSE/aimnet-wheel.sha256"
AIMNET_ARCHIVE_DIGEST_AFTER="$(directory_digest "$AIMNET_ARCHIVE_ROOT")"
readonly AIMNET_ARCHIVE_DIGEST_AFTER
[[ "$AIMNET_ARCHIVE_DIGEST_AFTER" == "$AIMNET_ARCHIVE_DIGEST_BEFORE" ]] \
  || fail "AIMNet build modified the fixed clean archive"

"$BOOTSTRAP_PYTHON" -I - \
  "$AIMNET_WHEEL" \
  "$WHEELHOUSE/aimnet-wheel-manifest.json" \
  "$AIMNET_COMMIT" \
  "$AIMNET_TREE" \
  "$AIMNET_SOURCE_DATE_EPOCH" \
  "$AIMNET_ARCHIVE_DIGEST_BEFORE" \
  "$AIMNET_WHEEL_FILE_COUNT" \
  "$AIMNET_WHEEL_INVENTORY_SHA" \
  "$AIMNET_WHEEL_RECORD_PATH" \
  "$AIMNET_WHEEL_RECORD_SHA" <<'PY'
import base64
import csv
import hashlib
import io
import json
import os
import pathlib
import sys
import zipfile

wheel = pathlib.Path(sys.argv[1])
destination = pathlib.Path(sys.argv[2])
(
    commit,
    tree,
    source_date_epoch,
    source_inventory,
    expected_file_count,
    expected_inventory_sha256,
    expected_record_path,
    expected_record_sha256,
) = sys.argv[3:]
wheel_sha256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
with zipfile.ZipFile(wheel) as archive:
    infos = [info for info in archive.infolist() if not info.is_dir()]
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise SystemExit("AIMNet wheel contains duplicate entries")
    if any(
        pathlib.PurePosixPath(name).is_absolute()
        or ".." in pathlib.PurePosixPath(name).parts
        or "\\" in name
        for name in names
    ):
        raise SystemExit("AIMNet wheel contains an unsafe path")
    record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
    if len(record_names) != 1:
        raise SystemExit("AIMNet wheel must contain exactly one RECORD")
    record_name = record_names[0]
    record_bytes = archive.read(record_name)
    rows = list(csv.reader(io.StringIO(record_bytes.decode("utf-8"))))
    if len(rows) != len(names) or {row[0] for row in rows} != set(names):
        raise SystemExit("AIMNet wheel RECORD does not enumerate the exact wheel")
    by_name = {info.filename: info for info in infos}
    for name, hash_spec, raw_size in rows:
        content = archive.read(name)
        if name == record_name:
            if hash_spec or raw_size:
                raise SystemExit("AIMNet wheel RECORD self-entry must be unhashed")
            continue
        expected_hash = "sha256=" + base64.urlsafe_b64encode(
            hashlib.sha256(content).digest()
        ).rstrip(b"=").decode("ascii")
        if hash_spec != expected_hash or raw_size != str(len(content)):
            raise SystemExit(f"AIMNet wheel RECORD mismatch: {name}")
        if by_name[name].file_size != len(content):
            raise SystemExit(f"AIMNet wheel size mismatch: {name}")
    files = [
        {
            "path": name,
            "size": by_name[name].file_size,
            "sha256": hashlib.sha256(archive.read(name)).hexdigest(),
        }
        for name in sorted(names)
    ]
inventory_sha256 = hashlib.sha256(
    json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
record_sha256 = hashlib.sha256(record_bytes).hexdigest()
if len(files) != int(expected_file_count):
    raise SystemExit("AIMNet wheel file count does not match the source lock")
if inventory_sha256 != expected_inventory_sha256:
    raise SystemExit("AIMNet wheel inventory does not match the source lock")
if record_name != expected_record_path or record_sha256 != expected_record_sha256:
    raise SystemExit("AIMNet wheel RECORD does not match the source lock")
payload = {
    "schema_version": 1,
    "source_commit": commit,
    "source_tree": tree,
    "source_date_epoch": int(source_date_epoch),
    "source_inventory_sha256": source_inventory,
    "wheel_file": wheel.name,
    "wheel_sha256": wheel_sha256,
    "file_count": len(files),
    "inventory_sha256": inventory_sha256,
    "record_path": record_name,
    "record_sha256": record_sha256,
    "files": files,
}
temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
with temporary.open("x", encoding="utf-8") as handle:
    json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(temporary, 0o444)
os.replace(temporary, destination)
directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
chmod 0444 \
  "$AIMNET_WHEEL" \
  "$WHEELHOUSE/aimnet-wheel.sha256" \
  "$WHEELHOUSE/aimnet-wheel-manifest.json"

log "creating isolated Python $EXPECTED_PYTHON_MINOR environment"
assert_runtime_target "$VENV_ROOT"
"$UV_BIN" venv --clear --python "$BOOTSTRAP_PYTHON" --no-python-downloads "$VENV_ROOT"
"$UV_BIN" pip install \
  --python "$VENV_PYTHON" \
  --no-python-downloads \
  --torch-backend cu128 \
  --require-hashes \
  --no-deps \
  --requirement "$REQUIREMENTS_LOCK"
"$UV_BIN" pip install \
  --python "$VENV_PYTHON" \
  --no-python-downloads \
  --no-deps \
  "$AIMNET_WHEEL"
"$UV_BIN" pip check --python "$VENV_PYTHON"

"$VENV_PYTHON" -I - "$SOURCE_LOCK" "$AIMNET_REGISTRY" <<'PY'
import json
import pathlib
import sys

import yaml

lock = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
registry = yaml.safe_load(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
for model in lock["models"]:
    alias = model["alias"]
    key = model["registry_key"]
    if registry["aliases"].get(alias) != key:
        raise SystemExit(f"registry alias mismatch for {alias}")
    registered = registry["models"].get(key)
    if registered is None:
        raise SystemExit(f"registry key is missing: {key}")
    for field in ("family", "file", "url", "sha256"):
        expected = model["sha256"] if field == "sha256" else model[field]
        if registered.get(field) != expected:
            raise SystemExit(f"registry {field} mismatch for {alias}")
    if model["sha256"] != model["registry_sha256"] or model["sha256"] != model["cache_sha256"]:
        raise SystemExit(f"model hash audit fields disagree for {alias}")
PY

mapfile -t MODEL_ROWS < <(
  "$BOOTSTRAP_PYTHON" -I - "$SOURCE_LOCK" <<'PY'
import json
import os
import pathlib
import sys

data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
models = data.get("models")
if not isinstance(models, list) or len(models) != 6:
    raise SystemExit("AIMNet lock must contain exactly six member-0 models")
seen_aliases = set()
seen_files = set()
for model in models:
    required = (
        "alias",
        "registry_key",
        "family",
        "ensemble_member",
        "file",
        "url",
        "sha256",
        "registry_sha256",
        "cache_sha256",
    )
    if any(key not in model for key in required):
        raise SystemExit("incomplete model entry in AIMNet lock")
    if any(not model[key] for key in required if key != "ensemble_member"):
        raise SystemExit("empty model metadata in AIMNet lock")
    if model["alias"] in seen_aliases or model["file"] in seen_files:
        raise SystemExit("duplicate alias or model file in AIMNet lock")
    seen_aliases.add(model["alias"])
    seen_files.add(model["file"])
    if model["ensemble_member"] != 0:
        raise SystemExit("only member-0 models belong in the isolated cache")
    if model["registry_sha256"].lower() != model["cache_sha256"].lower():
        raise SystemExit("registry/cache checksum disagreement in AIMNet lock")
    model_file = model["file"]
    if (
        not isinstance(model_file, str)
        or not model_file
        or model_file in {".", ".."}
        or "/" in model_file
        or "\\" in model_file
        or any(ord(character) < 32 or ord(character) == 127 for character in model_file)
    ):
        raise SystemExit("model file must be a safe basename")
    fields = (model["alias"], model["registry_key"], model["file"], model["cache_sha256"])
    if any(
        any(ord(character) < 32 or ord(character) == 127 for character in str(value))
        for value in fields
    ):
        raise SystemExit("unsafe model metadata")
    print(*fields, sep="\t")
PY
)
[[ "${#MODEL_ROWS[@]}" -eq 6 ]] || fail "AIMNet lock did not produce exactly six model entries"

SHARED_MODEL_CACHE="$(realpath -ms -- "${AIMNET_MODEL_SOURCE_DIR:-$HOME/.cache/aimnet}")"
readonly SHARED_MODEL_CACHE
[[ "$SHARED_MODEL_CACHE" != "$PRODUCTION_REPO_ROOT" \
  && "$SHARED_MODEL_CACHE" != "$PRODUCTION_REPO_ROOT/"* ]] \
  || fail "AIMNet model input must not reference the production repository"
assert_no_symlink_components "$SHARED_MODEL_CACHE"
assert_runtime_target "$AIMNET_CACHE"
UNEXPECTED_CACHE_ENTRY="$(find "$AIMNET_CACHE" -mindepth 1 -maxdepth 1 ! -type f -print -quit)"
readonly UNEXPECTED_CACHE_ENTRY
[[ -z "$UNEXPECTED_CACHE_ENTRY" ]] || fail "isolated model cache contains an unexpected non-file entry: $UNEXPECTED_CACHE_ENTRY"
find "$AIMNET_CACHE" -mindepth 1 -maxdepth 1 -type f -delete
for row in "${MODEL_ROWS[@]}"; do
  IFS=$'\t' read -r model_alias registry_key model_file expected_sha <<<"$row"
  model_source_path="$(realpath -ms -- "$SHARED_MODEL_CACHE/$model_file")"
  model_destination_path="$(realpath -ms -- "$AIMNET_CACHE/$model_file")"
  [[ "$(dirname -- "$model_source_path")" == "$SHARED_MODEL_CACHE" ]] \
    || fail "model source escapes the shared cache for $model_alias: $model_source_path"
  assert_no_symlink_components "$model_source_path"
  [[ "$(dirname -- "$model_destination_path")" == "$AIMNET_CACHE" ]] \
    || fail "model destination escapes the isolated cache for $model_alias: $model_destination_path"
  assert_runtime_target "$model_destination_path"
  [[ -f "$model_source_path" && ! -L "$model_source_path" ]] || fail "cached model must be a regular non-symlink for $model_alias: $model_source_path"
  [[ "$(sha256_file "$model_source_path")" == "$expected_sha" ]] || fail "shared-cache checksum mismatch for $model_alias"
  install -m 0444 "$model_source_path" "$model_destination_path"
  [[ "$(sha256_file "$model_destination_path")" == "$expected_sha" ]] || fail "copied checksum mismatch for $model_alias"
  log "verified model $model_alias ($registry_key)"
done
chmod 0555 "$AIMNET_CACHE"

if [[ ! -e "$REAL_ENV" ]]; then
  cat >"$REAL_ENV" <<EOF
MONOMER_DFT_PYTHON=$VENV_PYTHON
MONOMER_DFT_WORKER_UDS=$SOCKET_DIR/worker.sock
MONOMER_DFT_JOB_ROOT=$JOB_ROOT
MONOMER_DFT_MAX_CONCURRENT_JOBS=1
MONOMER_DFT_DEPLOYMENT=dev
NEXPOLY_DFT_GPU_DEVICE=1
NEXPOLY_DFT_OVERFLOW_GPU_DEVICES=3
MONOMER_DFT_GPU_BUDGET_MIB=4096
MONOMER_DFT_GPU_ACTIVE_THREAD_PERCENTAGE=50
MONOMER_DFT_GPU_BROKER_ENABLED=1
MONOMER_DFT_STANDALONE_GPU_SMOKE=0
MONOMER_DFT_GPU_BROKER_UDS=$GPU_RUNTIME_ROOT/broker.sock
MONOMER_DFT_GPU_MPS_PIPE_ROOT=$GPU_RUNTIME_ROOT
MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS=$GPU_RUNTIME_ROOT/external-reservations.json
MONOMER_DFT_DOWNLOAD_SPOOL_ROOT=/app/.runtime/monomer-dft-download-spool
AIMNET_CACHE_DIR=$AIMNET_CACHE
WARP_CACHE_PATH=$WARP_CACHE
UV_CACHE_DIR=$UV_CACHE
AIMNET_SOURCE_DIR=$AIMNET_ARCHIVE_ROOT
AIMNET_MODEL_SOURCE_DIR=$SHARED_MODEL_CACHE
AIMNET_SOURCE_LOCK=workers/monomer_dft_worker/aimnet-source.lock.json
PYTHONNOUSERSITE=1
PYTHONDONTWRITEBYTECODE=1
CUDA_DEVICE_ORDER=PCI_BUS_ID
PYTHONPATH=
EOF
  log "created local environment file $REAL_ENV"
else
  log "preserving existing local environment file $REAL_ENV"
fi
assert_no_symlink_components "$REAL_ENV"
chmod 0600 "$REAL_ENV"

"$BOOTSTRAP_PYTHON" -I - "$RUNTIME_ROOT" "$REAL_ENV" <<'PY'
import os
import pathlib
import sys

runtime = pathlib.Path(sys.argv[1]).resolve()
env_file = pathlib.Path(sys.argv[2]).resolve()
for line in env_file.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    if "=" not in line:
        raise SystemExit(f"invalid environment line: {line!r}")
    key, value = line.split("=", 1)
    if key == "PYTHONPATH" and value:
        raise SystemExit("PYTHONPATH is forbidden in the DFT environment")
    if key in {
        "MONOMER_DFT_PYTHON",
        "MONOMER_DFT_WORKER_UDS",
        "MONOMER_DFT_JOB_ROOT",
        "AIMNET_CACHE_DIR",
        "AIMNET_SOURCE_DIR",
        "WARP_CACHE_PATH",
        "UV_CACHE_DIR",
    }:
        resolved = pathlib.Path(value).expanduser()
        if not resolved.is_absolute():
            resolved = env_file.parent / resolved
        if key == "MONOMER_DFT_PYTHON":
            resolved = pathlib.Path(os.path.abspath(os.path.normpath(resolved)))
        else:
            resolved = resolved.resolve(strict=False)
        if not resolved.is_relative_to(runtime):
            raise SystemExit(f"{key} escapes the isolated runtime: {resolved}")
PY

INSTALLED_AIMNET_PATH="$(
  "$VENV_PYTHON" -I -c 'import aimnet; print(aimnet.__file__)'
)"
readonly INSTALLED_AIMNET_PATH
[[ "$INSTALLED_AIMNET_PATH" == "$VENV_ROOT/"*"/site-packages/aimnet/__init__.py" ]] || fail "AIMNet was not imported from the isolated venv: $INSTALLED_AIMNET_PATH"
INSTALLED_AIMNET_VERSION="$(
  "$VENV_PYTHON" -I -c 'from importlib.metadata import version; print(version("aimnet"))'
)"
readonly INSTALLED_AIMNET_VERSION
[[ "$INSTALLED_AIMNET_VERSION" == "$AIMNET_PACKAGE_VERSION" ]] || fail "installed AIMNet version $INSTALLED_AIMNET_VERSION does not match $AIMNET_PACKAGE_VERSION"

log "running fail-closed CPU/provenance preflight"
env -u CUDA_VISIBLE_DEVICES "$VENV_PYTHON" -I "$SCRIPT_DIR/preflight_monomer_dft_env.py"

log "environment ready"
log "wheel SHA-256: $AIMNET_WHEEL_SHA"
log "next: start the Broker-managed Worker, then run env -u PYTHONPATH -u CUDA_VISIBLE_DEVICES $VENV_PYTHON scripts/smoke_monomer_dft_env.py"

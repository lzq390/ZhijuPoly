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
readonly SLOT_FINAL="$RUNTIME_ROOT/worker-venvs/dft-a"
readonly SLOT_ROOT="$RUNTIME_ROOT/worker-venvs/dft-a.build-$$"
readonly ARTIFACT_ROOT="${MONOMER_DFT_PROD_ARTIFACT_ROOT:-$RUNTIME_ROOT/bootstrap-input/monomer-dft}"
readonly SOURCE_LOCK="$REPO_ROOT/workers/monomer_dft_worker/aimnet-source.lock.json"
readonly REQUIREMENTS_LOCK="$REPO_ROOT/workers/monomer_dft_worker/requirements.lock"

[[ "$REPO_ROOT" == "$PRODUCTION_REPO_ROOT" ]] || fail "must run from the production checkout"
[[ "$(git -C "$REPO_ROOT" rev-parse --show-toplevel)" == "$REPO_ROOT" ]] || fail "invalid production checkout"
git -C "$REPO_ROOT" diff --quiet --ignore-submodules -- || fail "production source is dirty"
git -C "$REPO_ROOT" diff --cached --quiet --ignore-submodules -- || fail "production index is dirty"
if [[ -n "${MONOMER_DFT_EXPECTED_GIT_REF:-}" ]]; then
  expected="$(git -C "$REPO_ROOT" rev-parse --verify "${MONOMER_DFT_EXPECTED_GIT_REF}^{commit}")"
  [[ "$(git -C "$REPO_ROOT" rev-parse HEAD)" == "$expected" ]] || fail "production HEAD differs from expected release"
fi
[[ -d "$RUNTIME_ROOT" && ! -L "$RUNTIME_ROOT" ]] || fail "production runtime root is unavailable"
[[ "$(stat -Lc '%u:%a' "$RUNTIME_ROOT")" == "$(id -u):700" ]] || fail "production runtime root must be owner-only 0700"
[[ ! -e "$SLOT_FINAL" ]] || fail "DFT A slot already exists; refusing to overwrite it"
[[ ! -e "$SLOT_ROOT" ]] || fail "DFT A staging slot already exists"

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

mkdir -m 0700 "$SLOT_ROOT"
export UV_CACHE_DIR="$SLOT_ROOT/uv-cache"
"$uv_bin" venv --python "$python312" "$SLOT_ROOT/venv"
"$uv_bin" pip install --python "$SLOT_ROOT/venv/bin/python" \
  --require-hashes --no-deps -r "$REQUIREMENTS_LOCK"
IFS=$'\t' read -r wheel_name _ <<<"${artifacts[0]}"
"$uv_bin" pip install --python "$SLOT_ROOT/venv/bin/python" \
  --no-deps "$ARTIFACT_ROOT/$wheel_name"

mkdir -m 0700 "$SLOT_ROOT/aimnet-cache" "$SLOT_ROOT/warp-cache"
for artifact in "${artifacts[@]:1}"; do
  IFS=$'\t' read -r filename _ <<<"$artifact"
  install -m 0600 "$ARTIFACT_ROOT/$filename" "$SLOT_ROOT/aimnet-cache/$filename"
done
mkdir -p \
  "$RUNTIME_ROOT/state/monomer-dft-worker-socket" \
  "$RUNTIME_ROOT/state/monomer-dft-worker-runs" \
  "$RUNTIME_ROOT/state/monomer-dft-download-spool"
chmod 0700 \
  "$RUNTIME_ROOT/state/monomer-dft-worker-socket" \
  "$RUNTIME_ROOT/state/monomer-dft-worker-runs" \
  "$RUNTIME_ROOT/state/monomer-dft-download-spool"
python3 -I - "$SLOT_ROOT/slot.json" "$REPO_ROOT" <<'PY'
import datetime, json, os, pathlib, subprocess, sys
path, repo = pathlib.Path(sys.argv[1]), sys.argv[2]
payload = {
    "schema_version": 1,
    "release": subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip(),
    "created_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "python": "3.12",
    "uv": "0.11.21",
}
path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
os.chmod(path, 0o600)
PY
mv -- "$SLOT_ROOT" "$SLOT_FINAL"
printf 'production DFT runtime: ready at %s\n' "$SLOT_FINAL"

#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV_ROOT="${NEXPOLY_MONOMER_MD_VENV_ROOT:-/data/lzq/nexpoly-runtime/monomer-md-worker}"
BASE_PYTHON="${BYTEFF2_PYTHON:-/home/devuser/miniconda3/envs/byteff2-repro/bin/python}"
REQUIREMENTS_FILE="$ROOT_DIR/workers/monomer_md_worker/requirements.txt"
RELEASE_ID=""
ACTIVATE=false

usage() {
  printf 'Usage: %s [--release-id ID] [--activate]\n' "$0"
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --release-id)
      [[ "$#" -ge 2 ]] || {
        usage >&2
        exit 2
      }
      RELEASE_ID="$2"
      shift 2
      ;;
    --activate)
      ACTIVATE=true
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

RELEASE_ID="${RELEASE_ID:-$(git -C "$ROOT_DIR" rev-parse HEAD)}"
VENV_DIR="$VENV_ROOT/venvs/$RELEASE_ID"
TEMP_DIR="$VENV_ROOT/venvs/.${RELEASE_ID}.tmp.$$"
TEMP_LINK="$VENV_ROOT/.current.tmp.$$"
MARKER_NAME=".nexpoly-worker-release.json"

if [[ ! "$RELEASE_ID" =~ ^[0-9a-f]{40}$ ]]; then
  printf 'Worker venv release id must be a full lowercase 40-hex Git SHA.\n' >&2
  exit 2
fi

[[ -x "$BASE_PYTHON" ]] || {
  printf 'ByteFF2 base Python is not executable: %s\n' "$BASE_PYTHON" >&2
  exit 3
}
[[ "$VENV_ROOT" == /* && "$VENV_ROOT" != "/" ]] || {
  printf 'NEXPOLY_MONOMER_MD_VENV_ROOT must be an absolute non-root directory.\n' >&2
  exit 3
}
SOURCE_COMMIT="$(git -C "$ROOT_DIR" rev-parse HEAD)"
if [[ "$SOURCE_COMMIT" != "$RELEASE_ID" ]]; then
  printf 'Provisioning checkout HEAD does not match --release-id; use a detached target worktree.\n' >&2
  exit 4
fi
git -C "$ROOT_DIR" diff --quiet -- workers/monomer_md_worker/requirements.txt || {
  printf 'Worker requirements have unstaged changes; refusing to label this venv by commit.\n' >&2
  exit 4
}
git -C "$ROOT_DIR" diff --cached --quiet -- workers/monomer_md_worker/requirements.txt || {
  printf 'Worker requirements have staged changes; refusing to label this venv by commit.\n' >&2
  exit 4
}
[[ -f "$REQUIREMENTS_FILE" && ! -L "$REQUIREMENTS_FILE" ]] || {
  printf 'Worker requirements file is missing or is a symlink.\n' >&2
  exit 4
}

REQUIREMENTS_SHA256="$(sha256sum "$REQUIREMENTS_FILE" | awk '{print $1}')"
BASE_PYTHON_REALPATH="$(readlink -f "$BASE_PYTHON")"
BASE_PREFIX_REALPATH="$(
  "$BASE_PYTHON" -I -c 'import os, sys; print(os.path.realpath(sys.prefix))'
)"
[[ -n "$BASE_PREFIX_REALPATH" && "$BASE_PREFIX_REALPATH" == /* ]] || {
  printf 'Could not determine the ByteFF2 base Python prefix.\n' >&2
  exit 4
}

umask 077
mkdir -p "$VENV_ROOT/venvs"
exec 9>"$VENV_ROOT/.provision.lock"
chmod 600 "$VENV_ROOT/.provision.lock"
flock -x 9

cleanup() {
  rm -f "$TEMP_LINK"
  if [[ -d "$TEMP_DIR" ]]; then
    rm -rf "$TEMP_DIR"
  fi
}
trap cleanup EXIT

write_release_marker() {
  local directory="$1"
  /usr/bin/python3 -I - "$directory/$MARKER_NAME" "$RELEASE_ID" \
    "$REQUIREMENTS_SHA256" "$BASE_PYTHON_REALPATH" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "base_python_realpath": sys.argv[4],
    "release_sha": sys.argv[2],
    "requirements_sha256": sys.argv[3],
}
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
    json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
    stream.write("\n")
PY
}

verify_release_venv() {
  local directory="$1"
  [[ -d "$directory" && ! -L "$directory" ]] || return 1
  /usr/bin/python3 -I - "$directory/$MARKER_NAME" "$RELEASE_ID" \
    "$REQUIREMENTS_SHA256" "$BASE_PYTHON_REALPATH" <<'PY' || return 1
import json
import os
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
try:
    descriptor = os.open(path, flags)
except OSError:
    raise SystemExit(1) from None
try:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(1)
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise SystemExit(1)
    with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as stream:
        payload = json.load(stream)
finally:
    os.close(descriptor)
expected = {
    "base_python_realpath": sys.argv[4],
    "release_sha": sys.argv[2],
    "requirements_sha256": sys.argv[3],
}
raise SystemExit(0 if payload == expected else 1)
PY
  "$directory/bin/python" -I -c \
    'import os, sys; assert sys.prefix != sys.base_prefix; assert os.path.realpath(sys.prefix) == os.path.realpath(sys.argv[1]); assert os.path.realpath(sys.base_prefix) == sys.argv[2]; import fastapi, numpy, pandas, psycopg, pydantic, uvicorn' \
    "$directory" "$BASE_PREFIX_REALPATH" || return 1
}

if [[ -e "$VENV_DIR" || -L "$VENV_DIR" ]]; then
  verify_release_venv "$VENV_DIR" || {
    printf 'Existing Worker venv is incomplete or its release marker is invalid: %s\n' "$VENV_DIR" >&2
    exit 5
  }
else
  "$BASE_PYTHON" -I -m venv --system-site-packages "$TEMP_DIR"
  env -u PYTHONHOME -u PYTHONPATH -u PYTHONUSERBASE \
    -u PIP_PREFIX -u PIP_TARGET -u PIP_USER \
    PIP_CONFIG_FILE=/dev/null \
    "$TEMP_DIR/bin/python" -I -m pip \
    --isolated --require-virtualenv --disable-pip-version-check install \
    -r "$ROOT_DIR/workers/monomer_md_worker/requirements.txt"
  "$TEMP_DIR/bin/python" -I -c \
    'import os, sys; assert sys.prefix != sys.base_prefix; assert os.path.realpath(sys.prefix) == os.path.realpath(sys.argv[1]); assert os.path.realpath(sys.base_prefix) == sys.argv[2]; import fastapi, numpy, pandas, psycopg, pydantic, uvicorn' \
    "$TEMP_DIR" "$BASE_PREFIX_REALPATH"
  write_release_marker "$TEMP_DIR"
  mv -T "$TEMP_DIR" "$VENV_DIR"
  verify_release_venv "$VENV_DIR" || {
    printf 'Provisioned Worker venv failed its release-marker verification.\n' >&2
    exit 5
  }
fi

printf 'Provisioned monomer MD Worker venv %s.\n' "$VENV_DIR"
if [[ "$ACTIVATE" == "true" ]]; then
  ln -s "$VENV_DIR" "$TEMP_LINK"
  mv -Tf "$TEMP_LINK" "$VENV_ROOT/current"
  printf 'Atomically switched %s/current to %s.\n' "$VENV_ROOT" "$VENV_DIR"
else
  printf 'The current symlink was not changed; deploy_server.sh activates the tested candidate.\n'
fi

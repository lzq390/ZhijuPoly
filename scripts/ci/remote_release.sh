#!/usr/bin/env bash
set -euo pipefail
umask 077

operation="${1:-}"
manifest="${2:-}"
release_bundle="${3:-}"

: "${NEXPOLY_RELEASE_SHA:?NEXPOLY_RELEASE_SHA is required}"
: "${NEXPOLY_SSH_HOST:?NEXPOLY_SSH_HOST is required}"
: "${NEXPOLY_SSH_USER:?NEXPOLY_SSH_USER is required}"
: "${NEXPOLY_SSH_PRIVATE_KEY:?NEXPOLY_SSH_PRIVATE_KEY is required}"
: "${NEXPOLY_SSH_KNOWN_HOSTS:?NEXPOLY_SSH_KNOWN_HOSTS is required; host-key discovery is forbidden}"

[[ "$NEXPOLY_RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "release SHA must be exactly 40 lowercase hexadecimal characters" >&2
  exit 2
}
case "$operation" in
  auto|bootstrap) ;;
  *) echo "operation must be auto or bootstrap" >&2; exit 2 ;;
esac

production_root="${NEXPOLY_PRODUCTION_ROOT:-/data/lzq/gith/nexpoly}"
[[ "$production_root" == /data/lzq/gith/nexpoly ]] || {
  echo "production root must be /data/lzq/gith/nexpoly" >&2
  exit 2
}
ssh_port="${NEXPOLY_SSH_PORT:-22}"
if [[ ! "$ssh_port" =~ ^[0-9]+$ ]] || ((ssh_port < 1 || ssh_port > 65535)); then
  echo "SSH port must be between 1 and 65535" >&2
  exit 2
fi
for required_file in "$manifest" "$release_bundle"; do
  [[ -f "$required_file" && ! -L "$required_file" ]] || {
    echo "required release file is missing or is a symlink: $required_file" >&2
    exit 2
  }
done
[[ "$(basename "$manifest")" == release-manifest.json ]] || {
  echo "manifest must be named release-manifest.json" >&2
  exit 2
}
[[ "$(basename "$release_bundle")" == "nexpoly-release-${NEXPOLY_RELEASE_SHA}.tar.gz" ]] || {
  echo "release bundle name does not match the release SHA" >&2
  exit 2
}

# Validate the local pair before any SSH connection.  The same validation is
# repeated remotely after transfer; neither the SSH channel nor a filename is
# treated as artifact identity.
/usr/bin/python3 -I - "$manifest" "$release_bundle" "$NEXPOLY_RELEASE_SHA" <<'PY'
import hashlib
import json
import os
import stat
import sys

manifest_path, bundle_path, expected_sha = sys.argv[1:]
with open(manifest_path, encoding="utf-8") as source:
    document = json.load(source)
if document.get("source_sha") != expected_sha:
    raise SystemExit("release manifest source SHA does not match the requested release")
record = document.get("release_bundle")
if not isinstance(record, dict) or set(record) != {"name", "size", "sha256"}:
    raise SystemExit("release manifest has no valid release_bundle record")
if record["name"] != os.path.basename(bundle_path):
    raise SystemExit("release bundle name differs from manifest")
metadata = os.lstat(bundle_path)
if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != record["size"]:
    raise SystemExit("release bundle is unsafe or has the wrong size")
digest = hashlib.sha256()
with open(bundle_path, "rb") as source:
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(chunk)
if f"sha256:{digest.hexdigest()}" != record["sha256"]:
    raise SystemExit("release bundle digest differs from manifest")
PY

ssh_dir="$(mktemp -d)"
trap 'rm -rf "$ssh_dir"' EXIT
install -m 600 /dev/null "$ssh_dir/key"
printf '%s\n' "$NEXPOLY_SSH_PRIVATE_KEY" >"$ssh_dir/key"
install -m 600 /dev/null "$ssh_dir/known_hosts"
printf '%s\n' "$NEXPOLY_SSH_KNOWN_HOSTS" >"$ssh_dir/known_hosts"

ssh_options=(
  -i "$ssh_dir/key"
  -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=yes
  -o UserKnownHostsFile="$ssh_dir/known_hosts"
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=20
)
remote="${NEXPOLY_SSH_USER}@${NEXPOLY_SSH_HOST}"
upload_id="${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-${operation}"
remote_parent="${production_root}/ops/incoming/${NEXPOLY_RELEASE_SHA}"
remote_stage="${remote_parent}/${upload_id}"

ssh "${ssh_options[@]}" -p "$ssh_port" "$remote" bash -s -- \
  "$production_root" "$remote_parent" "$remote_stage" "$operation" <<'REMOTE_PREPARE'
set -euo pipefail
umask 077
production_root="$1"
remote_parent="$2"
remote_stage="$3"
operation="$4"
[[ "$production_root" == /data/lzq/gith/nexpoly ]]
case "$remote_parent" in "$production_root"/ops/incoming/*) ;; *) exit 2 ;; esac
case "$remote_stage" in "$remote_parent"/*) ;; *) exit 2 ;; esac
# Bootstrap/retry state is decided only by the locked release controller.  A
# verified-resume-pending or interrupted bootstrap may legitimately have both
# release-state.json and ops/current and still need recovery.
install -d -m 700 "$production_root/ops/incoming" "$remote_parent"
mkdir -m 700 "$remote_stage"
REMOTE_PREPARE

scp "${ssh_options[@]}" -P "$ssh_port" \
  "$manifest" "$release_bundle" "$remote:$remote_stage/"

manifest_name="$(basename "$manifest")"
bundle_name="$(basename "$release_bundle")"
ssh "${ssh_options[@]}" -p "$ssh_port" "$remote" bash -s -- \
  "$production_root" "$remote_stage" "$manifest_name" "$bundle_name" \
  "$NEXPOLY_RELEASE_SHA" "$operation" <<'REMOTE_DEPLOY'
set -euo pipefail
umask 077
production_root="$1"
stage="$2"
manifest_name="$3"
bundle_name="$4"
release_sha="$5"
mode="$6"
[[ "$production_root" == /data/lzq/gith/nexpoly ]]
case "$stage" in "$production_root"/ops/incoming/*) ;; *) exit 2 ;; esac
case "$manifest_name:$bundle_name" in
  *"/"*|*".."*) exit 2 ;;
esac
trap 'rm -rf -- "$stage"' EXIT
chmod 600 "$stage/$manifest_name" "$stage/$bundle_name"
controller="$stage/release_controller.py"
worker_env_helper="$stage/monomer_worker_env.py"

/usr/bin/python3 -I - "$stage/$manifest_name" "$stage/$bundle_name" \
  "$controller.tmp" "$worker_env_helper.tmp" "$release_sha" <<'PY'
import hashlib
import json
import os
import stat
import sys
import tarfile

manifest_path, bundle_path, controller_output, helper_output, expected_sha = sys.argv[1:]
with open(manifest_path, encoding="utf-8") as source:
    document = json.load(source)
if document.get("source_sha") != expected_sha:
    raise SystemExit("release manifest source SHA does not match the requested release")
record = document.get("release_bundle")
if not isinstance(record, dict) or set(record) != {"name", "size", "sha256"}:
    raise SystemExit("release manifest has no valid release_bundle record")
if record["name"] != os.path.basename(bundle_path):
    raise SystemExit("release bundle name differs from manifest")
metadata = os.lstat(bundle_path)
if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size != record["size"]:
    raise SystemExit("release bundle is unsafe or has the wrong size")
digest = hashlib.sha256()
with open(bundle_path, "rb") as source:
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(chunk)
if f"sha256:{digest.hexdigest()}" != record["sha256"]:
    raise SystemExit("release bundle digest differs from manifest")
with tarfile.open(bundle_path, "r:gz") as archive:
    members = archive.getmembers()
    required = (
        (
            {"scripts/release_controller.py", "./scripts/release_controller.py"},
            controller_output,
            2 * 1024 * 1024,
            "release controller",
        ),
        (
            {"scripts/monomer_worker_env.py", "./scripts/monomer_worker_env.py"},
            helper_output,
            256 * 1024,
            "Worker environment helper",
        ),
    )
    for accepted_names, output_path, size_limit, label in required:
        matches = [member for member in members if member.name in accepted_names]
        if (
            len(matches) != 1
            or not matches[0].isfile()
            or matches[0].size > size_limit
        ):
            raise SystemExit(f"release bundle has an unsafe {label} entry")
        source = archive.extractfile(matches[0])
        if source is None:
            raise SystemExit(f"cannot read {label} from release bundle")
        payload = source.read(size_limit + 1)
        if len(payload) != matches[0].size or len(payload) > size_limit:
            raise SystemExit(f"release bundle has an unsafe {label} payload")
        descriptor = os.open(
            output_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o700,
        )
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(payload)
PY

chmod 700 "$controller.tmp" "$worker_env_helper.tmp"
mv "$controller.tmp" "$controller"
mv "$worker_env_helper.tmp" "$worker_env_helper"
/usr/bin/python3 -I "$controller" verify-manifest \
  --manifest "$stage/$manifest_name" --sha "$release_sha"
/usr/bin/python3 -I "$controller" provision-release --apply --mode "$mode" \
  --manifest "$stage/$manifest_name" --production-root "$production_root" &&
/usr/bin/python3 -I "$controller" deploy --apply --mode "$mode" \
  --manifest "$stage/$manifest_name" --production-root "$production_root"
REMOTE_DEPLOY

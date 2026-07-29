#!/usr/bin/env bash
set -euo pipefail

image_ref="${1:-}"
expected_revision="${2:-}"
expected_tree="${3:-}"
expected_dependency_lock="${4:-}"
expected_build_config="${5:-}"

if [[ -z "$image_ref" || $# -ne 5 ]]; then
  echo "usage: assert_backend_image_identity.sh IMAGE REVISION TREE DEPENDENCY_LOCK BUILD_CONFIG" >&2
  exit 2
fi
if [[ ! "$expected_revision" =~ ^[0-9a-f]{40}$ ]]; then
  echo "expected Backend revision must be a full lowercase Git SHA" >&2
  exit 2
fi
if [[ ! "$expected_tree" =~ ^[0-9a-f]{40}$ ]]; then
  echo "expected Backend tree must be a full lowercase Git tree" >&2
  exit 2
fi
for digest in "$expected_dependency_lock" "$expected_build_config"; do
  if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "expected Backend identity digests must be lowercase sha256 values" >&2
    exit 2
  fi
done

config_json="$(
  docker image inspect \
    --format '{{json .Config}}' \
    "$image_ref"
)"
[[ -n "$config_json" ]] || {
  echo "Backend image config inspection returned an empty result" >&2
  exit 1
}

BACKEND_IMAGE_CONFIG_JSON="$config_json" \
EXPECTED_BACKEND_REVISION="$expected_revision" \
EXPECTED_BACKEND_TREE="$expected_tree" \
EXPECTED_BACKEND_DEPENDENCY_LOCK="$expected_dependency_lock" \
EXPECTED_BACKEND_BUILD_CONFIG="$expected_build_config" \
python3 - <<'PY'
import json
import os

try:
    config = json.loads(os.environ["BACKEND_IMAGE_CONFIG_JSON"])
except (KeyError, json.JSONDecodeError) as exc:
    raise SystemExit("Backend image config inspection is invalid") from exc
if not isinstance(config, dict):
    raise SystemExit("Backend image config inspection must be an object")

labels = config.get("Labels")
environment = config.get("Env")
if not isinstance(labels, dict) or not isinstance(environment, list):
    raise SystemExit("Backend image config lacks labels or environment")
if any(not isinstance(item, str) or "=" not in item for item in environment):
    raise SystemExit("Backend image environment is invalid")

environment_by_name = {}
for item in environment:
    name, value = item.split("=", 1)
    if not name or name in environment_by_name:
        raise SystemExit("Backend image environment contains duplicate identities")
    environment_by_name[name] = value

revision = os.environ["EXPECTED_BACKEND_REVISION"]
tree = os.environ["EXPECTED_BACKEND_TREE"]
dependency_lock = os.environ["EXPECTED_BACKEND_DEPENDENCY_LOCK"]
build_config = os.environ["EXPECTED_BACKEND_BUILD_CONFIG"]
expected_labels = {
    "org.opencontainers.image.revision": revision,
    "com.nexpoly.source.tree": tree,
    "com.nexpoly.backend.dependency-lock": dependency_lock,
    "com.nexpoly.backend.build-config": build_config,
}
expected_environment = {
    "BUILD_REVISION": revision,
    "BUILD_SOURCE_TREE": tree,
    "BUILD_DEPENDENCY_LOCK_SHA256": dependency_lock,
    "BUILD_CONFIG_SHA256": build_config,
}

for name, expected in expected_labels.items():
    if labels.get(name) != expected:
        raise SystemExit(f"Backend image label {name} differs from reviewed identity")
for name, expected in expected_environment.items():
    if environment_by_name.get(name) != expected:
        raise SystemExit(
            f"Backend image environment {name} differs from reviewed identity"
        )
PY

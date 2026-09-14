#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -P "$SCRIPT_DIR/../.." && pwd -P)"
IMAGE_TAG="${FRONTEND_PERMISSION_IMAGE_TAG:-nexpoly-frontend-permission-smoke:local}"
CONTAINER_NAME="${FRONTEND_PERMISSION_CONTAINER_NAME:-nexpoly-frontend-permission-smoke-$$}"
EXPECTED_WORKSPACE_URL="${FRONTEND_EXPECTED_WORKSPACE_URL:-}"
EDITOR_ENGINE="${FRONTEND_STRUCTURE_EDITOR_ENGINE:-}"
EDITOR_BUILD_ARGS=()
if [[ -n "$EDITOR_ENGINE" ]]; then
  case "$EDITOR_ENGINE" in react|iframe) ;; *) echo "invalid structure editor engine" >&2; exit 2 ;; esac
  EDITOR_BUILD_ARGS+=(--build-arg "VITE_STRUCTURE_EDITOR_ENGINE=$EDITOR_ENGINE")
fi
STAGING="$(mktemp -d)"
INDEX_FILE="$STAGING/index.html"
IMAGE_DIST="$STAGING/image-dist"

if [[ -n "$EXPECTED_WORKSPACE_URL" && "$EXPECTED_WORKSPACE_URL" != "http://114.214.255.154:9011/" ]]; then
  echo "frontend image smoke accepts only the reviewed production OpenScience URL" >&2
  exit 2
fi

cleanup() {
  docker rm --force "$CONTAINER_NAME" >/dev/null 2>&1 || true
  docker image rm --force "$IMAGE_TAG" >/dev/null 2>&1 || true
  rm -rf -- "$STAGING"
}
trap cleanup EXIT

node --test "$SCRIPT_DIR/verify_frontend_image_assets.test.mjs"
git -C "$REPO_ROOT" archive --format=tar HEAD | tar -C "$STAGING" -xf -

# Reproduce the restrictive source checkout that previously made copied static
# assets unreadable to the nginx worker.
find "$STAGING/frontend/public" -type d -exec chmod 0700 {} +
find "$STAGING/frontend/public" -type f -exec chmod 0600 {} +

docker build \
  --file "$STAGING/frontend/Dockerfile" \
  --tag "$IMAGE_TAG" \
  --build-arg "SOURCE_REVISION=$(git -C "$REPO_ROOT" rev-parse HEAD)" \
  --build-arg "VITE_AGENT_WORKSPACE_URL=$EXPECTED_WORKSPACE_URL" \
  "${EDITOR_BUILD_ARGS[@]}" \
  "$STAGING"

docker run --rm --add-host backend:127.0.0.1 "$IMAGE_TAG" nginx -t
docker run --rm --user 101:101 "$IMAGE_TAG" sh -eu -c '
  test -r /etc/nginx/conf.d/default.conf
  test "$(stat -c %a /etc/nginx/conf.d/default.conf)" = 644
  find /usr/share/nginx/html -type d -exec test -x {} \;
  find /usr/share/nginx/html -type f -exec test -r {} \;
'

docker run \
  --detach \
  --name "$CONTAINER_NAME" \
  --add-host backend:127.0.0.1 \
  "$IMAGE_TAG" >/dev/null

for _ in {1..30}; do
  if docker exec "$CONTAINER_NAME" wget -qO- http://127.0.0.1/ >"$INDEX_FILE" 2>/dev/null; then
    break
  fi
  sleep 1
done
test -s "$INDEX_FILE"
docker exec "$CONTAINER_NAME" wget -qO- http://127.0.0.1/structure-editor.json >"$STAGING/structure-editor.json"
mapfile -t EDITOR_ASSETS < <(node --input-type=module - "$STAGING/structure-editor.json" <<'JS'
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
const build = JSON.parse(readFileSync(process.argv[2], 'utf8'));
assert.ok(['iframe', 'react'].includes(build.engine));
if (build.engine === 'iframe') {
  assert.equal(build.nativeAssets.length, 0);
  console.log('ketcher/index.html');
} else {
  assert.ok(build.nativeAssets.length > 0);
  console.log(build.nativeAssets.join('\n'));
}
JS
)
(( ${#EDITOR_ASSETS[@]} > 0 ))
for editor_asset in "${EDITOR_ASSETS[@]}"; do
  docker exec "$CONTAINER_NAME" wget -qO /dev/null "http://127.0.0.1/$editor_asset"
done
docker exec "$CONTAINER_NAME" wget -qO /dev/null http://127.0.0.1/vendor/3Dmol-min.js

# The HTML bootstrap now imports the App and feature pages lazily. Validate
# their actual manifest entries instead of requiring business code in main.js.
mkdir -p "$IMAGE_DIST"
cp "$INDEX_FILE" "$IMAGE_DIST/index.html"
docker cp "$CONTAINER_NAME:/usr/share/nginx/html/.vite" "$IMAGE_DIST/.vite"
docker cp "$CONTAINER_NAME:/usr/share/nginx/html/assets" "$IMAGE_DIST/assets"
node "$SCRIPT_DIR/verify_frontend_image_assets.mjs" "$IMAGE_DIST" "$EXPECTED_WORKSPACE_URL" \
  >"$STAGING/image-assets.json"
mapfile -t HASHED_ASSETS < <(
  node --input-type=module - "$STAGING/image-assets.json" <<'JS'
import { readFileSync } from 'node:fs';
const result = JSON.parse(readFileSync(process.argv[2], 'utf8'));
console.log(result.checkedAssets.map(file => `/${file}`).join('\n'));
JS
)
(( ${#HASHED_ASSETS[@]} > 0 ))
for hashed_asset in "${HASHED_ASSETS[@]}"; do
  docker exec "$CONTAINER_NAME" wget -qO /dev/null \
    "http://127.0.0.1$hashed_asset"
done
docker exec "$CONTAINER_NAME" sh -eu -c '
  worker_count="$(ps -o user,comm | awk '"'"'$1 != "root" && $2 == "nginx" { count += 1 } END { print count + 0 }'"'"')"
  test "$worker_count" -ge 1
'

#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -P "$SCRIPT_DIR/../.." && pwd -P)"
IMAGE_TAG="${FRONTEND_PERMISSION_IMAGE_TAG:-nexpoly-frontend-permission-smoke:local}"
CONTAINER_NAME="${FRONTEND_PERMISSION_CONTAINER_NAME:-nexpoly-frontend-permission-smoke-$$}"
STAGING="$(mktemp -d)"
INDEX_FILE="$STAGING/index.html"
ASSET_FILE="$STAGING/main.js"

cleanup() {
  docker rm --force "$CONTAINER_NAME" >/dev/null 2>&1 || true
  docker image rm --force "$IMAGE_TAG" >/dev/null 2>&1 || true
  rm -rf -- "$STAGING"
}
trap cleanup EXIT

git -C "$REPO_ROOT" archive --format=tar HEAD | tar -C "$STAGING" -xf -

# Reproduce the restrictive source checkout that previously made copied static
# assets unreadable to the nginx worker.
find "$STAGING/frontend/public" -type d -exec chmod 0700 {} +
find "$STAGING/frontend/public" -type f -exec chmod 0600 {} +

docker build \
  --file "$STAGING/frontend/Dockerfile" \
  --tag "$IMAGE_TAG" \
  --build-arg "SOURCE_REVISION=$(git -C "$REPO_ROOT" rev-parse HEAD)" \
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
docker exec "$CONTAINER_NAME" wget -qO /dev/null http://127.0.0.1/ketcher/index.html
docker exec "$CONTAINER_NAME" wget -qO /dev/null http://127.0.0.1/vendor/3Dmol-min.js

ASSET_PATH="$(sed -n 's/.*src="\([^"]*\/assets\/[^"]*\.js\)".*/\1/p' "$INDEX_FILE" | head -n 1)"
[[ "$ASSET_PATH" == /assets/*.js ]]
mapfile -t HASHED_ASSETS < <(
  grep -oE '/assets/[A-Za-z0-9._/-]+' "$INDEX_FILE" | sort -u
)
(( ${#HASHED_ASSETS[@]} > 0 ))
for hashed_asset in "${HASHED_ASSETS[@]}"; do
  docker exec "$CONTAINER_NAME" wget -qO /dev/null \
    "http://127.0.0.1$hashed_asset"
done
docker exec "$CONTAINER_NAME" sh -eu -c \
  "wget -qO- 'http://127.0.0.1$ASSET_PATH'" >"$ASSET_FILE"
grep -a -q "3Dmol" "$ASSET_FILE"
grep -a -q "正在同步" "$ASSET_FILE"
if grep -a -Eq '127\.0\.0\.1:(4454|9011)|localhost:(4454|9011)' "$ASSET_FILE"; then
  echo "unconfigured frontend image contains an active loopback OpenScience URL" >&2
  exit 1
fi
docker exec "$CONTAINER_NAME" sh -eu -c '
  worker_count="$(ps -o user,comm | awk '"'"'$1 != "root" && $2 == "nginx" { count += 1 } END { print count + 0 }'"'"')"
  test "$worker_count" -ge 1
'

#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if (( $# != 2 )); then
  echo "usage: $0 <candidate-image> <expected-source-revision>" >&2
  exit 2
fi

CANDIDATE_IMAGE="$1"
EXPECTED_REVISION="$2"
BASE_IMAGE="ghcr.io/lzq390/nexpoly-web@sha256:e7d25a1b6d515daec641c8de9c98265f275991eee2396dc578ce9c2fcfdeb197"
BASE_MANIFEST="sha256:e7d25a1b6d515daec641c8de9c98265f275991eee2396dc578ce9c2fcfdeb197"
BASE_IMAGE_ID="sha256:e7d25a1b6d515daec641c8de9c98265f275991eee2396dc578ce9c2fcfdeb197"
BASE_INDEX="index.html"
BASE_BUNDLE="assets/index-B2eNxQLj.js"
PATCHED_BUNDLE="assets/index-nexpoly-3e4638285546.js"
PATCHED_INDEX_SHA256="cadf4874a517094efb1b50b02ea30c271c6e540f1eb0bdb1b5cff437c95ff383"
PATCHED_BUNDLE_SHA256="3e46382855462c8064f4c4cea1a417fd15ada4a03de60b5fa7ca0c2fb5962e4a"
PATCHED_STATIC_TREE_SHA256="32f45b16e585ef348b4a83a9763412476568ec1781aecb5be69ebd7d7f3c54fd"
PARENT_POLICY_SHA256="955ae6f5f3d0710dcaacc0906f6326a4ba99321a0e47fc928c198c8967dd0042"
OLD_RESOLVER='function mf(e,t){if(t)return ZM(e)}'
NEW_RESOLVER='function mf(e,t){if(!t)return;const n=ZM(document.referrer);return n&&["http://114.214.255.154:9000","http://114.214.255.154:9001"].includes(n)?n:void 0}'
OLD_CALL='mf("http://114.214.255.154:9001",i!==window)'

[[ "$EXPECTED_REVISION" =~ ^[0-9a-f]{40}$ ]]

STAGING="$(mktemp -d)"
BASE_CONTAINER="nexpoly-openscience-base-verify-$$"
CANDIDATE_CONTAINER="nexpoly-openscience-candidate-verify-$$"

cleanup() {
  docker rm --force "$CANDIDATE_CONTAINER" "$BASE_CONTAINER" >/dev/null 2>&1 || true
  rm -rf -- "$STAGING"
}
trap cleanup EXIT

docker pull "$BASE_IMAGE" >/dev/null
if [[ "$CANDIDATE_IMAGE" == *@sha256:* ]]; then
  docker pull "$CANDIDATE_IMAGE" >/dev/null
fi

test "$(docker image inspect "$CANDIDATE_IMAGE" --format '{{.Config.User}}')" = nginx
test "$(docker image inspect "$CANDIDATE_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$EXPECTED_REVISION"
test "$(docker image inspect "$CANDIDATE_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.source"}}')" = "https://github.com/lzq390/ZhijuPoly"
test "$(docker image inspect "$CANDIDATE_IMAGE" --format '{{index .Config.Labels "com.nexpoly.openscience.base-image-id"}}')" = "$BASE_IMAGE_ID"
test "$(docker image inspect "$CANDIDATE_IMAGE" --format '{{index .Config.Labels "com.nexpoly.openscience.base-manifest"}}')" = "$BASE_MANIFEST"
test "$(docker image inspect "$CANDIDATE_IMAGE" --format '{{index .Config.Labels "com.nexpoly.openscience.parent-origins"}}')" = \
  "http://114.214.255.154:9000,http://114.214.255.154:9001"
test "$(docker image inspect "$CANDIDATE_IMAGE" --format '{{index .Config.Labels "com.nexpoly.openscience.derived-static-tree"}}')" = \
  "sha256:$PATCHED_STATIC_TREE_SHA256"
test "$(docker image inspect "$CANDIDATE_IMAGE" --format '{{index .Config.Labels "com.nexpoly.openscience.parent-policy-sha256"}}')" = \
  "sha256:$PARENT_POLICY_SHA256"

docker create --name "$BASE_CONTAINER" "$BASE_IMAGE" >/dev/null
docker create --name "$CANDIDATE_CONTAINER" "$CANDIDATE_IMAGE" >/dev/null
mkdir -p \
  "$STAGING/base" \
  "$STAGING/candidate" \
  "$STAGING/base-rootfs" \
  "$STAGING/candidate-rootfs"
docker cp "$BASE_CONTAINER:/usr/share/nginx/html/." "$STAGING/base"
docker cp "$CANDIDATE_CONTAINER:/usr/share/nginx/html/." "$STAGING/candidate"

test "$(find "$STAGING/base" -type f | wc -l)" -eq 798
test "$(find "$STAGING/candidate" -type f | wc -l)" -eq 798
test -f "$STAGING/base/$BASE_BUNDLE"
test ! -e "$STAGING/candidate/$BASE_BUNDLE"
test -f "$STAGING/candidate/$PATCHED_BUNDLE"
test "$(sha256sum "$STAGING/candidate/$BASE_INDEX" | awk '{print $1}')" = "$PATCHED_INDEX_SHA256"
test "$(sha256sum "$STAGING/candidate/$PATCHED_BUNDLE" | awk '{print $1}')" = "$PATCHED_BUNDLE_SHA256"
candidate_static_tree_sha256="$(
  cd "$STAGING/candidate"
  find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}'
)"
test "$candidate_static_tree_sha256" = "$PATCHED_STATIC_TREE_SHA256"

find "$STAGING/base" -type f -printf '%P\n' | sort | \
  grep -Fvx "$BASE_INDEX" | grep -Fvx "$BASE_BUNDLE" >"$STAGING/base-files"
find "$STAGING/candidate" -type f -printf '%P\n' | sort | \
  grep -Fvx "$BASE_INDEX" | grep -Fvx "$PATCHED_BUNDLE" >"$STAGING/candidate-files"
diff -u "$STAGING/base-files" "$STAGING/candidate-files"
while IFS= read -r relative_path; do
  cmp "$STAGING/base/$relative_path" "$STAGING/candidate/$relative_path"
done <"$STAGING/base-files"

# Compare the complete effective root filesystems, excluding only the two
# replaced files and the new cache-busted bundle. Image labels live in the
# config rather than the root filesystem and are validated above.
docker export "$BASE_CONTAINER" | tar --no-same-owner -xf - -C "$STAGING/base-rootfs"
docker export "$CANDIDATE_CONTAINER" | tar --no-same-owner -xf - -C "$STAGING/candidate-rootfs"
(
  cd "$STAGING/base-rootfs"
  find . \
    ! -path "./usr/share/nginx/html/$BASE_INDEX" \
    ! -path "./usr/share/nginx/html/$BASE_BUNDLE" \
    -printf '%P|%y|%m|%s|%l\n' | LC_ALL=C sort
) >"$STAGING/base-rootfs-metadata"
(
  cd "$STAGING/candidate-rootfs"
  find . \
    ! -path "./usr/share/nginx/html/$BASE_INDEX" \
    ! -path "./usr/share/nginx/html/$PATCHED_BUNDLE" \
    -printf '%P|%y|%m|%s|%l\n' | LC_ALL=C sort
) >"$STAGING/candidate-rootfs-metadata"
diff -u "$STAGING/base-rootfs-metadata" "$STAGING/candidate-rootfs-metadata"
(
  cd "$STAGING/base-rootfs"
  find . -type f \
    ! -path "./usr/share/nginx/html/$BASE_INDEX" \
    ! -path "./usr/share/nginx/html/$BASE_BUNDLE" \
    -exec sha256sum {} + | LC_ALL=C sort
) >"$STAGING/base-rootfs-sha256"
(
  cd "$STAGING/candidate-rootfs"
  find . -type f \
    ! -path "./usr/share/nginx/html/$BASE_INDEX" \
    ! -path "./usr/share/nginx/html/$PATCHED_BUNDLE" \
    -exec sha256sum {} + | LC_ALL=C sort
) >"$STAGING/candidate-rootfs-sha256"
diff -u "$STAGING/base-rootfs-sha256" "$STAGING/candidate-rootfs-sha256"

test "$(grep -Fao "$OLD_RESOLVER" "$STAGING/candidate/$PATCHED_BUNDLE" | wc -l)" -eq 0
test "$(grep -Fao "$NEW_RESOLVER" "$STAGING/candidate/$PATCHED_BUNDLE" | wc -l)" -eq 1
test "$(grep -Fao "$OLD_CALL" "$STAGING/candidate/$PATCHED_BUNDLE" | wc -l)" -eq 2
IMPORT_MAP='<script type="importmap">{"imports":{"/assets/index-B2eNxQLj.js":"/assets/index-nexpoly-3e4638285546.js"}}</script>'
test "$(grep -Fao "$IMPORT_MAP" "$STAGING/candidate/$BASE_INDEX" | wc -l)" -eq 1
test "$(grep -Fao "/$BASE_BUNDLE" "$STAGING/candidate/$BASE_INDEX" | wc -l)" -eq 1
test "$(grep -Fao "/$PATCHED_BUNDLE" "$STAGING/candidate/$BASE_INDEX" | wc -l)" -eq 2
test "$(grep -R -F -l "index-B2eNxQLj.js" "$STAGING/candidate/assets" | wc -l)" -eq 20

docker start "$CANDIDATE_CONTAINER" >/dev/null
for _ in {1..30}; do
  health="$(docker inspect "$CANDIDATE_CONTAINER" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')"
  if [[ "$health" == healthy ]]; then
    break
  fi
  if [[ "$health" == unhealthy ]]; then
    docker logs "$CANDIDATE_CONTAINER" >&2
    exit 1
  fi
  sleep 1
done
test "$(docker inspect "$CANDIDATE_CONTAINER" --format '{{.State.Health.Status}}')" = healthy
docker exec "$CANDIDATE_CONTAINER" wget -qO- http://127.0.0.1:4454/ | grep -Fq "/$PATCHED_BUNDLE"
docker exec "$CANDIDATE_CONTAINER" sh -eu -c '
  test "$(stat -c %a /usr/share/nginx/html/index.html)" = 644
  test "$(stat -c %a /usr/share/nginx/html/assets/index-nexpoly-3e4638285546.js)" = 644
'

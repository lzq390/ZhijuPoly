#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if (( $# != 1 && $# != 2 )); then
  echo "usage: $0 <candidate-image> | --container <running-container>" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPOSITORY_ROOT="$(cd -P "$SCRIPT_DIR/../.." && pwd -P)"
PLAYWRIGHT_IMAGE="mcr.microsoft.com/playwright@sha256:c091b21d9fae78c76e85cd4356431e9b018402f172a214fc7d7a5e9a7e29d8ac"
CANDIDATE_CONTAINER="nexpoly-openscience-browser-candidate-$$"
REMOVE_CANDIDATE=0

if (( $# == 2 )); then
  [[ "$1" == "--container" && -n "$2" ]] || exit 2
  CANDIDATE_CONTAINER="$2"
else
  CANDIDATE_IMAGE="$1"
  REMOVE_CANDIDATE=1
fi

cleanup() {
  if (( REMOVE_CANDIDATE == 1 )); then
    docker rm --force "$CANDIDATE_CONTAINER" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

test -d "$REPOSITORY_ROOT/ops/openscience-ui-overlay/node_modules/playwright"
docker pull "$PLAYWRIGHT_IMAGE" >/dev/null
if (( REMOVE_CANDIDATE == 1 )); then
  docker create --name "$CANDIDATE_CONTAINER" "$CANDIDATE_IMAGE" >/dev/null
  docker start "$CANDIDATE_CONTAINER" >/dev/null
fi
for _ in {1..30}; do
  health="$(docker inspect "$CANDIDATE_CONTAINER" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')"
  [[ "$health" == "healthy" ]] && break
  [[ "$health" != "unhealthy" ]] || exit 1
  sleep 1
done
test "$(docker inspect "$CANDIDATE_CONTAINER" --format '{{.State.Health.Status}}')" = "healthy"

docker run --rm \
  --network "container:$CANDIDATE_CONTAINER" \
  --shm-size 1g \
  --volume "$REPOSITORY_ROOT:/work:ro" \
  --workdir /work/ops/openscience-ui-overlay \
  "$PLAYWRIGHT_IMAGE" \
  node ./browser_probe.mjs

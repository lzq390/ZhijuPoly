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
BROWSER_CONTAINER="nexpoly-openscience-browser-probe-$$"
REMOVE_CANDIDATE=0
RUNTIME_ATTEMPTED=0
VERIFY_STAGE=browser-image

# The candidate is a local static server. The browser fixture uses its own
# explicit loopback proxy for synthetic parent origins, without external I/O.
# Existing --container targets and image build/pull proxies are not changed.
readonly -a LOCAL_RUNTIME_PROXY_ARGS=(
  --env HTTP_PROXY= --env HTTPS_PROXY= --env ALL_PROXY=
  --env http_proxy= --env https_proxy= --env all_proxy=
)

runtime_diagnostics() {
  printf 'OpenScience browser verification failed at %s (line %s)\n' "$VERIFY_STAGE" "$1" >&2
  [[ "$RUNTIME_ATTEMPTED" == 1 ]] || return 0
  if (( REMOVE_CANDIDATE == 1 )); then
    /usr/bin/timeout --signal=TERM --kill-after=2s 10s \
      docker inspect "$RUNTIME_CONTAINER" --format 'status={{.State.Status}} exit={{.State.ExitCode}} health={{json .State.Health}}' >&2 || true
    /usr/bin/timeout --signal=TERM --kill-after=2s 10s \
      docker logs --tail 50 "$RUNTIME_CONTAINER" >&2 || true
  else
    # An existing target can be a live service; do not dump its logs or env.
    /usr/bin/timeout --signal=TERM --kill-after=2s 10s \
      docker inspect "$RUNTIME_CONTAINER" --format 'status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' >&2 || true
  fi
}

create_local_candidate() {
  VERIFY_STAGE=container-create
  RUNTIME_ATTEMPTED=1
  /usr/bin/timeout --signal=TERM --kill-after=2s 30s \
    docker create --name "$RUNTIME_CONTAINER" "${LOCAL_RUNTIME_PROXY_ARGS[@]}" "$CANDIDATE_IMAGE" >/dev/null
}

start_local_candidate() {
  VERIFY_STAGE=container-start
  /usr/bin/timeout --signal=TERM --kill-after=2s 30s docker start "$RUNTIME_CONTAINER" >/dev/null
}

wait_for_local_candidate() {
  VERIFY_STAGE=container-health
  local health deadline=$((SECONDS + 30))
  for _ in {1..30}; do
    (( SECONDS < deadline )) || break
    health="$(/usr/bin/timeout --signal=TERM --kill-after=2s 5s \
      docker inspect "$RUNTIME_CONTAINER" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')"
    [[ "$health" != healthy ]] || return 0
    [[ "$health" != unhealthy ]] || return 1
    sleep 1
  done
  echo 'Container did not become healthy before the readiness deadline' >&2
  return 1
}

run_local_browser_probe() {
  VERIFY_STAGE=browser-probes
  /usr/bin/timeout --signal=TERM --kill-after=10s 180s \
  docker run --rm \
    --name "$BROWSER_CONTAINER" \
    "${LOCAL_RUNTIME_PROXY_ARGS[@]}" \
    --network "container:$CANDIDATE_CONTAINER" \
    --shm-size 1g \
    --volume "$REPOSITORY_ROOT:/work:ro" \
    --workdir /work/ops/openscience-ui-overlay \
    "$PLAYWRIGHT_IMAGE" \
    sh -eu -c '
      node /work/ops/openscience-static-release/verify-cache.mjs http://127.0.0.1:4454/
      node ./browser_probe.mjs
    '
}

if (( $# == 2 )); then
  [[ "$1" == "--container" && -n "$2" ]] || exit 2
  CANDIDATE_CONTAINER="$2"
else
  CANDIDATE_IMAGE="$1"
  REMOVE_CANDIDATE=1
fi
RUNTIME_CONTAINER="$CANDIDATE_CONTAINER"

cleanup() {
  docker rm --force "$BROWSER_CONTAINER" >/dev/null 2>&1 || true
  if (( REMOVE_CANDIDATE == 1 )); then
    docker rm --force "$CANDIDATE_CONTAINER" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
failure_status=0
trap 'failure_status=$?; if (( BASH_SUBSHELL == 0 )); then runtime_diagnostics "$LINENO"; exit "$failure_status"; fi' ERR

test -d "$REPOSITORY_ROOT/ops/openscience-ui-overlay/node_modules/playwright"
if ! docker image inspect "$PLAYWRIGHT_IMAGE" >/dev/null 2>&1; then
  /usr/bin/timeout --signal=TERM --kill-after=10s 900s \
    docker pull "$PLAYWRIGHT_IMAGE" >/dev/null
fi
if (( REMOVE_CANDIDATE == 1 )); then
  create_local_candidate
  start_local_candidate
fi
RUNTIME_ATTEMPTED=1
wait_for_local_candidate
run_local_browser_probe

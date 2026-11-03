#!/usr/bin/env bash
set -euo pipefail

# Large scientific images need room for compressed layers, unpacked layers,
# and the exported image at once. Only discard unused SDKs on disposable CI VMs.
if [[ "${GITHUB_ACTIONS:-}" != true ||
      "${RUNNER_ENVIRONMENT:-}" != github-hosted ||
      "${RUNNER_OS:-}" != Linux || "${ImageOS:-}" != ubuntu24 ]]; then
  echo "Image disk preparation requires a GitHub-hosted Ubuntu 24.04 runner" >&2
  exit 1
fi
case "${GITHUB_JOB:-}" in
  image-build|exact-b-bridge|release) ;;
  *) echo "Image disk preparation is restricted to the heavy image jobs" >&2; exit 1 ;;
esac

docker_root="$(docker info --format '{{.DockerRootDir}}')"
if [[ "$docker_root" != /var/lib/docker ]]; then
  echo "Unexpected hosted-runner Docker storage root" >&2
  exit 1
fi

df -h "$docker_root"
# These SDKs are unused by the Python/Node/Docker build and bridge jobs. Keep
# the runner tool cache, checked-out source, PostgreSQL, and all Docker data.
sudo --non-interactive rm -rf -- \
  /usr/local/lib/android \
  /usr/share/dotnet \
  /usr/local/.ghcup \
  /usr/share/swift
df -h "$docker_root"

readonly MIN_FREE_KIB=$((32 * 1024 * 1024))
available_kib="$(df -Pk "$docker_root" | awk 'NR == 2 {print $4}')"
if [[ ! "$available_kib" =~ ^[0-9]+$ ]] || ((available_kib < MIN_FREE_KIB)); then
  echo "Scientific image builds require at least 32 GiB free on Docker storage" >&2
  exit 1
fi

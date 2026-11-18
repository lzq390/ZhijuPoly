#!/usr/bin/env bash
set -euo pipefail

# Large scientific images need room for compressed layers, unpacked layers,
# and the exported image at once. Self-hosted VMs only check their capacity.
if [[ "${GITHUB_ACTIONS:-}" != true || "${RUNNER_OS:-}" != Linux ]]; then
  echo "Image disk preparation requires a Linux GitHub Actions runner" >&2
  exit 1
fi
case "${GITHUB_JOB:-}" in
  image-build|exact-b-bridge|release) ;;
  *) echo "Image disk preparation is restricted to the heavy image jobs" >&2; exit 1 ;;
esac

# The profile is provisioned by the VM image, never by the workflow. Checking it
# prevents accidental use on another host; VM disposal is enforced externally.
case "${RUNNER_ENVIRONMENT:-}" in
  github-hosted)
    [[ "${ImageOS:-}" == ubuntu24 ]] || {
      echo "Hosted image disk preparation requires Ubuntu 24.04" >&2
      exit 1
    }
    ;;
  self-hosted)
    [[ "$GITHUB_JOB" == image-build || "$GITHUB_JOB" == exact-b-bridge ]] || {
      echo "Self-hosted disk checks are restricted to non-publishing image jobs" >&2
      exit 1
    }
    for path in /etc/nexpoly-ci-runner /etc/nexpoly-ci-runner/profile; do
      metadata="$(LC_ALL=C stat --format='%F:%u:%a' -- "$path")"
      expected_type=directory
      [[ "$path" != */profile ]] || expected_type='regular file'
      IFS=: read -r file_type owner mode <<<"$metadata"
      if [[ "$file_type" != "$expected_type" || "$owner" != 0 ||
            ! "$mode" =~ ^[0-7]{3,4}$ ]] || (( (8#$mode & 022) != 0 )); then
        echo "Self-hosted runner profile must be root-owned and not group/world writable" >&2
        exit 1
      fi
    done
    [[ "$(cat /etc/nexpoly-ci-runner/profile)" == nexpoly-ci-ephemeral-v1 ]] || {
      echo "Self-hosted runner profile does not match the ephemeral VM image" >&2
      exit 1
    }
    os_identity="$(awk -F= '/^(ID|VERSION_ID)=/ {gsub(/\"/, "", $2); print $1 "=" $2}' /etc/os-release)"
    if ! [[ $'\n'"$os_identity"$'\n' == *$'\nID=ubuntu\n'* &&
            $'\n'"$os_identity"$'\n' == *$'\nVERSION_ID=24.04\n'* ]]; then
      echo "Self-hosted image disk checks require Ubuntu 24.04" >&2
      exit 1
    fi
    ;;
  *) echo "Unapproved runner environment for image disk preparation" >&2; exit 1 ;;
esac

docker_root="$(docker info --format '{{.DockerRootDir}}')"
if [[ "$docker_root" != /var/lib/docker ]]; then
  echo "Unexpected runner Docker storage root" >&2
  exit 1
fi

df -h "$docker_root"
if [[ "$RUNNER_ENVIRONMENT" == github-hosted ]]; then
  # Only discard unused SDKs on disposable GitHub-hosted VMs. Keep the tool
  # cache, checked-out source, PostgreSQL, and all Docker data.
  sudo --non-interactive rm -rf -- \
    /usr/local/lib/android \
    /usr/share/dotnet \
    /usr/local/.ghcup \
    /usr/share/swift
  df -h "$docker_root"
fi

readonly MIN_FREE_KIB=$((32 * 1024 * 1024))
available_kib="$(df -Pk "$docker_root" | awk 'NR == 2 {print $4}')"
if [[ ! "$available_kib" =~ ^[0-9]+$ ]] || ((available_kib < MIN_FREE_KIB)); then
  echo "Scientific image builds require at least 32 GiB free on Docker storage" >&2
  exit 1
fi

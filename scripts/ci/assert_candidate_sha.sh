#!/usr/bin/env bash
set -euo pipefail

expected_sha="${1:-}"
if [[ ! "$expected_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "candidate SHA must be exactly 40 lowercase hexadecimal characters" >&2
  exit 2
fi

actual_sha="$(git rev-parse HEAD)"
if [[ "$actual_sha" != "$expected_sha" ]]; then
  echo "checkout mismatch: expected $expected_sha, got $actual_sha" >&2
  exit 1
fi

echo "verified immutable checkout $actual_sha"

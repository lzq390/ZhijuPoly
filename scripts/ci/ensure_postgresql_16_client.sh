#!/usr/bin/env bash
set -euo pipefail

readonly POSTGRES_BIN=/usr/lib/postgresql/16/bin
readonly POSTGRES_VERSION_MARKER='PostgreSQL) 16.'
readonly POSTGRES_PACKAGE=postgresql-client-16
readonly MAX_INSTALL_ATTEMPTS=3
readonly -a APT_OPTIONS=(
  -o Acquire::Retries=2
  -o Acquire::http::Timeout=15
  -o Acquire::https::Timeout=15
  -o DPkg::Lock::Timeout=30
)

postgres_client_is_ready() {
  local tool
  for tool in psql pg_dump pg_restore; do
    [[ -x "$POSTGRES_BIN/$tool" ]] || return 1
    "$POSTGRES_BIN/$tool" --version 2>/dev/null |
      /usr/bin/grep -Fq "$POSTGRES_VERSION_MARKER" || return 1
  done
}

install_postgres_client() {
  /usr/bin/sudo --non-interactive \
    /usr/bin/timeout --signal=TERM --kill-after=10s 60s \
    /usr/bin/apt-get "${APT_OPTIONS[@]}" update &&
    /usr/bin/sudo --non-interactive \
      /usr/bin/timeout --signal=TERM --kill-after=10s 90s \
      /usr/bin/env DEBIAN_FRONTEND=noninteractive \
      /usr/bin/apt-get "${APT_OPTIONS[@]}" \
        install --yes --no-install-recommends "$POSTGRES_PACKAGE"
}

if ! postgres_client_is_ready; then
  install_succeeded=false
  for ((attempt = 1; attempt <= MAX_INSTALL_ATTEMPTS; attempt += 1)); do
    if install_postgres_client; then
      install_succeeded=true
      break
    fi
    if ((attempt < MAX_INSTALL_ATTEMPTS)); then
      /usr/bin/sleep "$((attempt * 5))"
    fi
  done
  if [[ "$install_succeeded" != true ]]; then
    echo "PostgreSQL 16 client installation exhausted bounded retries" >&2
    exit 1
  fi
fi

if ! postgres_client_is_ready; then
  echo "PostgreSQL 16 client identity verification failed" >&2
  exit 1
fi

"$POSTGRES_BIN/psql" --version
"$POSTGRES_BIN/pg_dump" --version
"$POSTGRES_BIN/pg_restore" --version

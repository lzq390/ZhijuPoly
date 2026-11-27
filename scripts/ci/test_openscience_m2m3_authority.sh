#!/usr/bin/env bash
# Run the OpenScience M2 ticket-authority and M3 delegation integration tests
# against a disposable, freshly bootstrapped dev-m1 authority database.
#
# The authority bootstrap only accepts its private socket at /m2pg, so this
# script needs root or passwordless sudo to create that directory. PostgreSQL
# runs from POSTGRES_IMAGE when Docker is available, otherwise from local
# PostgreSQL 16 server binaries. Every integration case must run: a skip fails.
#
#   M2M3_POSTGRES=auto|docker|host   (default auto: Docker first)
#   M2M3_POSTGRES_IMAGE              (default: $POSTGRES_IMAGE)
#   M2M3_PG_BIN                      (default: /usr/lib/postgresql/16/bin)
#   PYTHON                           (default: python)
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly ROOT
readonly SOCKET_DIR=/m2pg
readonly DATABASE=nexpoly_m2_dev
readonly PYTHON="${PYTHON:-python}"
readonly PG_BIN="${M2M3_PG_BIN:-/usr/lib/postgresql/16/bin}"
readonly IMAGE="${M2M3_POSTGRES_IMAGE:-${POSTGRES_IMAGE:-}}"
MODE="${M2M3_POSTGRES:-auto}"
CONTAINER=""
WORK=""

die() {
  echo "openscience-m2m3: $*" >&2
  exit 1
}

as_root() {
  if [[ $EUID -eq 0 ]]; then
    "$@"
  else
    sudo --non-interactive "$@"
  fi
}

# initdb refuses root, so a root caller runs the local server as postgres.
as_server() {
  if [[ $EUID -eq 0 ]]; then
    runuser -u postgres -- "$@"
  else
    "$@"
  fi
}

case "$MODE" in
  auto)
    if [[ -n $IMAGE ]] && docker info >/dev/null 2>&1; then
      MODE=docker
    elif [[ -x $PG_BIN/initdb ]]; then
      MODE=host
    else
      die "needs Docker with POSTGRES_IMAGE set, or PostgreSQL 16 server binaries in $PG_BIN"
    fi
    ;;
  docker)
    [[ -n $IMAGE ]] || die "M2M3_POSTGRES=docker needs POSTGRES_IMAGE or M2M3_POSTGRES_IMAGE"
    ;;
  host)
    [[ -x $PG_BIN/initdb ]] || die "no PostgreSQL server binaries in $PG_BIN"
    ;;
  *)
    die "M2M3_POSTGRES must be auto, docker or host"
    ;;
esac
readonly MODE

cleanup() {
  local status=$?
  trap - EXIT
  if [[ -n $CONTAINER ]]; then
    if [[ $status -ne 0 ]]; then
      docker logs --tail 50 "$CONTAINER" >&2 2>&1 || :
    fi
    docker rm --force "$CONTAINER" >/dev/null 2>&1 || :
  elif [[ -n $WORK && -f $WORK/pg/data/postmaster.pid ]]; then
    if [[ $status -ne 0 ]]; then
      as_root tail -n 50 "$WORK/pg/server.log" >&2 || :
    fi
    as_server "$PG_BIN/pg_ctl" -D "$WORK/pg/data" -m immediate -w stop >/dev/null 2>&1 || :
  fi
  if [[ -n $WORK && -d $WORK ]]; then
    as_root rm -rf -- "${WORK:?}"
  fi
  exit "$status"
}
trap cleanup EXIT

cd -- "$ROOT"

# The socket directory must be a real, empty directory owned by this run.
if [[ -L $SOCKET_DIR ]] || { [[ -e $SOCKET_DIR ]] && [[ ! -d $SOCKET_DIR ]]; }; then
  die "$SOCKET_DIR must be a real directory"
fi
if [[ -d $SOCKET_DIR ]] && [[ -n "$(as_root find "$SOCKET_DIR" -mindepth 1 -print -quit)" ]]; then
  die "$SOCKET_DIR is not empty; stop the authority database that uses it first"
fi
as_root install -d -m 0755 "$SOCKET_DIR"

WORK="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/openscience-m2m3.XXXXXX")"
chmod 0711 "$WORK"
mkdir -m 0700 "$WORK/cred" "$WORK/home"
"$PYTHON" - "$WORK/cred" <<'PY'
import os
import secrets
import sys

for name in ("admin", "api", "auth", "service", "a", "b", "router", "gateway"):
    path = os.path.join(sys.argv[1], name)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(secrets.token_urlsafe(40) + "\n")
PY

readonly HBA='local all all scram-sha-256'
if [[ $MODE == docker ]]; then
  server_uid="$(docker run --rm --pull=missing --network none --entrypoint id "$IMAGE" -u postgres)"
  server_gid="$(docker run --rm --pull=never --network none --entrypoint id "$IMAGE" -g postgres)"
  as_root chown "$server_uid:$server_gid" "$SOCKET_DIR"
  mkdir -m 0755 "$WORK/docker"
  install -m 0444 "$WORK/cred/admin" "$WORK/docker/admin-pw"
  printf '%s\n' "$HBA" >"$WORK/docker/pg_hba.conf"
  chmod 0444 "$WORK/docker/pg_hba.conf"
  CONTAINER="nexpoly-openscience-m2m3-$$"
  docker run --detach --pull=never --network none --name "$CONTAINER" \
    --mount "type=bind,src=$SOCKET_DIR,dst=$SOCKET_DIR" \
    --mount "type=bind,src=$WORK/docker/admin-pw,dst=/admin-pw,readonly" \
    --mount "type=bind,src=$WORK/docker/pg_hba.conf,dst=/m2_hba.conf,readonly" \
    -e POSTGRES_USER=m2_admin -e POSTGRES_DB="$DATABASE" \
    -e POSTGRES_PASSWORD_FILE=/admin-pw \
    -e POSTGRES_INITDB_ARGS=--auth-local=scram-sha-256 \
    "$IMAGE" postgres -c listen_addresses= \
    -c "unix_socket_directories=/var/run/postgresql,$SOCKET_DIR" \
    -c hba_file=/m2_hba.conf >/dev/null
  # The entrypoint's temporary init server also listens on this socket; wait
  # until PID 1 is the final postmaster.
  ready=false
  for _ in $(seq 1 180); do
    if [[ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER")" != true ]]; then
      die "the PostgreSQL container exited during startup"
    fi
    if [[ "$(docker exec "$CONTAINER" cat /proc/1/comm 2>/dev/null || :)" == postgres ]] &&
      [[ -S $SOCKET_DIR/.s.PGSQL.5432 ]]; then
      ready=true
      break
    fi
    sleep 0.5
  done
  [[ $ready == true ]] || die "PostgreSQL did not become ready within 90 seconds"
else
  mkdir -m 0700 "$WORK/pg"
  install -m 0400 "$WORK/cred/admin" "$WORK/pg/admin-pw"
  printf '%s\n' "$HBA" >"$WORK/pg/pg_hba.conf"
  if [[ $EUID -eq 0 ]]; then
    chown -R postgres: "$WORK/pg"
    chown postgres: "$SOCKET_DIR"
  else
    as_root chown "$(id -u):$(id -g)" "$SOCKET_DIR"
  fi
  # An unset locale would select SQL_ASCII, which returns text as bytes.
  as_server "$PG_BIN/initdb" -D "$WORK/pg/data" -U m2_admin \
    --no-locale --encoding=UTF8 \
    --pwfile="$WORK/pg/admin-pw" --auth-local=scram-sha-256 \
    --auth-host=scram-sha-256 >"$WORK/initdb.log" 2>&1 ||
    { cat "$WORK/initdb.log" >&2; die "initdb failed"; }
  as_server "$PG_BIN/pg_ctl" -D "$WORK/pg/data" -l "$WORK/pg/server.log" -w -t 60 \
    -o "-c listen_addresses= -c unix_socket_directories=$SOCKET_DIR -c hba_file=$WORK/pg/pg_hba.conf" \
    start >/dev/null
fi
echo "openscience-m2m3: disposable PostgreSQL ($MODE) is listening on $SOCKET_DIR"

dsn() {
  printf 'host=%s dbname=%s user=m2_%s password=%s' \
    "$SOCKET_DIR" "${2:-$DATABASE}" "$1" "$(<"$WORK/cred/$1")"
}

# Only the variables below reach the bootstrap and the tests.
authority() {
  env -i PATH="$PATH" HOME="$WORK/home" TMPDIR="$WORK/home" LANG=C.UTF-8 \
    PYTHONPATH="$ROOT/backend:$ROOT" PYTHONDONTWRITEBYTECODE=1 \
    OPENSCIENCE_M2_ENVIRONMENT=dev-m1 \
    APP_POSTGRES_DSN="$(dsn api)" \
    AUTH_POSTGRES_DSN="$(dsn auth)" \
    APP_SERVICE_POSTGRES_DSN="$(dsn service)" \
    OPENSCIENCE_M2_ADMIN_DSN="$(dsn admin)" \
    OPENSCIENCE_M2_PASSWORD_A_FILE="$WORK/cred/a" \
    OPENSCIENCE_M2_PASSWORD_B_FILE="$WORK/cred/b" \
    OPENSCIENCE_M2_ROUTER_TOKEN_FILE="$WORK/cred/router" \
    "$@"
}

authority "$PYTHON" - "$(dsn admin postgres)" "$DATABASE" <<'PY'
import sys
import time

import psycopg
from psycopg import sql

deadline = time.monotonic() + 30
while True:
    try:
        conn = psycopg.connect(sys.argv[1], autocommit=True, connect_timeout=3)
        break
    except psycopg.OperationalError:
        if time.monotonic() > deadline:
            raise
        time.sleep(0.5)
with conn:
    if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (sys.argv[2],)).fetchone():
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(sys.argv[2])))
PY

authority "$PYTHON" ops/openscience-per-user-dev/m2_database.py
authority "$PYTHON" -m app.auth.openscience_seed

# Fails unless every listed case ran and passed, and nothing was skipped.
require_cases() {
  "$PYTHON" - "$@" <<'PY'
import sys
import xml.etree.ElementTree as ET

report, *required = sys.argv[1:]
cases = list(ET.parse(report).getroot().iter("testcase"))
problems = []
for case in cases:
    for outcome in ("skipped", "failure", "error"):
        if case.find(outcome) is not None:
            problems.append(f"{case.get('name')} {outcome}")
names = {case.get("name") for case in cases}
problems += [f"{name} did not run" for name in required if name not in names]
if not cases or problems:
    raise SystemExit("required OpenScience authority coverage is incomplete: " + "; ".join(problems or ["no cases"]))
print(f"{report.rsplit('/', 1)[-1]}: {len(cases)} cases passed, none skipped")
PY
}

run_suite() {
  local report="$WORK/$1.xml"
  shift
  local test_file="$1"
  shift
  authority "$@" "$PYTHON" -m pytest "$test_file" -q -rfEs --assert=plain \
    --tb=short -p no:cacheprovider --junitxml="$report"
}

run_suite m2 backend/tests/test_openscience_m2_authority.py \
  env OPENSCIENCE_M2_INTEGRATION=1
require_cases "$WORK/m2.xml" test_one_time_ticket_and_device_specific_revocation

run_suite m3 backend/tests/test_openscience_m3_delegation.py \
  env OPENSCIENCE_M3_AUTH_TEST=1 OPENSCIENCE_M3_GATEWAY_TOKEN_FILE="$WORK/cred/gateway"
require_cases "$WORK/m3.xml" test_real_login_delegation_scope_replay_expiration_and_logout

echo "openscience-m2m3: M2 ticket authority and M3 delegation integration passed"

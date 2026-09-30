#!/usr/bin/env bash
# Exercise the shipped image only against a newly created, disposable database.
set -euo pipefail

[[ $# == 1 ]]
readonly backend_image="$1"
readonly postgres_image="postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
readonly network="nexpoly-isolation-ci-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-$$"
readonly postgres="${network}-postgres"
readonly backend="${network}-backend"
cleanup() {
  docker rm -fv "$backend" "$postgres" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker network create --internal "$network" >/dev/null
docker run -d --name "$postgres" --network "$network" --network-alias postgres \
  -e POSTGRES_USER=nexpoly_ci -e POSTGRES_PASSWORD=ci-only -e POSTGRES_DB=nexpoly_ci \
  "$postgres_image" >/dev/null
for _ in {1..30}; do
  docker exec "$postgres" pg_isready -h 127.0.0.1 -U nexpoly_ci -d nexpoly_ci >/dev/null && break
  sleep 1
done
docker exec "$postgres" pg_isready -h 127.0.0.1 -U nexpoly_ci -d nexpoly_ci

# A CI-only empty database has no legacy assets to back up. The explicit owner
# and cutover below are intentional; ordinary bootstrap must stop at 0017.
docker run --rm -i --network "$network" "$backend_image" python - <<'PY'
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from app.auth.cli import manage_user
from app.auth.cutover import apply_identity_cutover
from app.auth.service import AuthService
from app.auth.service_privileges import apply_service_auth_least_privilege
from app.auth.settings import AuthSettings
from app.postgres_migrations import apply_postgres_migrations

dsn = 'postgresql://nexpoly_ci:ci-only@postgres:5432/nexpoly_ci'
with psycopg.connect(dsn) as connection:
    assert connection.execute("SELECT to_regclass('governance.schema_migrations')").fetchone()[0] is None
apply_postgres_migrations(dsn, allowed_kinds={'baseline', 'expand'}, allow_contract_on_fresh_database=True)
with psycopg.connect(dsn, row_factory=dict_row) as connection:
    latest = connection.execute('SELECT max(version) version FROM governance.schema_migrations').fetchone()['version']
    assert latest == '0017_user_isolation_prepare', latest
    owner = manage_user(connection, 'create', username='ci-owner', password='ci-only-initial-password')
    for kind in ('api', 'auth', 'service'):
        connection.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD 'ci-only' IN ROLE {}").format(
            sql.Identifier('ci_' + kind), sql.Identifier('nexpoly_' + kind)))
settings = AuthSettings(
    application_dsn='postgresql://ci_api:ci-only@postgres:5432/nexpoly_ci',
    auth_dsn='postgresql://ci_auth:ci-only@postgres:5432/nexpoly_ci',
    service_dsn='postgresql://ci_service:ci-only@postgres:5432/nexpoly_ci',
)
auth = AuthService(settings)
try:
    auth.assert_application_ready()
except RuntimeError as exc:
    assert 'cutover has not completed' in str(exc), exc
except psycopg.errors.InsufficientPrivilege:
    # 0018 also installs runtime grants; an API role before cutover may not
    # yet read the ledger at all. It must still fail closed.
    pass
else:
    raise AssertionError('0017 must not authorize application startup')
apply_identity_cutover(dsn, str(owner['user_id']))
try:
    auth.assert_application_ready()
except RuntimeError:
    pass
else:
    raise AssertionError('0018 must not authorize current application startup')
apply_service_auth_least_privilege(dsn, service_roles=['ci_service'], expected_database='nexpoly_ci')
retry = apply_identity_cutover(dsn, str(owner['user_id']))
assert retry['already_applied'] and retry['current_readiness'] is False
auth.assert_application_ready()
for grant, revoke in (
    ('GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_binary_file(text) TO ci_service',
     'REVOKE EXECUTE ON FUNCTION pg_catalog.pg_read_binary_file(text) FROM ci_service'),
    ('GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_binary_file(text) TO PUBLIC',
     'REVOKE EXECUTE ON FUNCTION pg_catalog.pg_read_binary_file(text) FROM PUBLIC'),
    ('GRANT pg_signal_backend TO ci_service WITH INHERIT TRUE, SET FALSE',
     'REVOKE pg_signal_backend FROM ci_service'),
    ('GRANT pg_signal_backend TO ci_service WITH INHERIT FALSE, SET TRUE',
     'REVOKE pg_signal_backend FROM ci_service'),
    ('GRANT pg_checkpoint TO ci_service', 'REVOKE pg_checkpoint FROM ci_service'),
    ('GRANT ALTER SYSTEM ON PARAMETER log_statement TO ci_service',
     'REVOKE ALTER SYSTEM ON PARAMETER log_statement FROM ci_service'),
):
    with psycopg.connect(dsn) as connection:
        connection.execute(grant)
    try:
        try:
            auth.assert_application_ready()
        except ValueError as exc:
            assert 'service authentication privilege contract' in str(exc), exc
        else:
            raise AssertionError('Excess system authority must reject application readiness')
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute(revoke)
auth.assert_application_ready()
print('0017 rejects startup; explicit 0018 then 0019 and separate runtime roles verified')
print('Six system function/management privilege drifts reject readiness; valid ACL recovers')
PY

runtime_env=(
  -e APP_POSTGRES_DSN=postgresql://ci_api:ci-only@postgres:5432/nexpoly_ci
  -e PI_POSTGRES_DSN=postgresql://ci_api:ci-only@postgres:5432/nexpoly_ci
  -e LAB_DATA_POSTGRES_DSN=postgresql://ci_api:ci-only@postgres:5432/nexpoly_ci
  -e AUTH_POSTGRES_DSN=postgresql://ci_auth:ci-only@postgres:5432/nexpoly_ci
  -e APP_SERVICE_POSTGRES_DSN=postgresql://ci_service:ci-only@postgres:5432/nexpoly_ci
  -e MODEL_ENABLED=false -e OCSR_ENABLED=false -e GEN_MODEL_ENABLED=false
  -e POLYTAO_ENABLED=false -e RETRO_MODEL_ENABLED=false
  -e GPU_BROKER_ENABLED=false -e CUDA_VISIBLE_DEVICES= -e NVIDIA_VISIBLE_DEVICES=void
  -e MONOMER_MD_SUBMIT_ENABLED=false -e MONOMER_DFT_SUBMIT_ENABLED=false
)
docker run --rm --network "$network" "${runtime_env[@]}" "$backend_image" \
  python -m app.postgres_preflight --mode schema --strict --schema-target user-isolation-0019 --service-context
docker run -d --name "$backend" --network "$network" "${runtime_env[@]}" "$backend_image" >/dev/null
for _ in {1..60}; do
  if docker exec "$backend" python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2)' >/dev/null 2>&1; then
    break
  fi
  if [[ "$(docker inspect --format '{{.State.Running}}' "$backend")" != true ]]; then
    docker logs "$backend"
    exit 1
  fi
  sleep 1
done
docker exec -i "$backend" python - <<'PY'
import urllib.error
import urllib.request

assert urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).status == 200
try:
    urllib.request.urlopen('http://127.0.0.1:8000/api/v1/monomer-dft/jobs', timeout=5)
except urllib.error.HTTPError as exc:
    assert exc.code == 401, exc.code
else:
    raise AssertionError('Guest access to a private route must be rejected')
print('Published backend starts on 0019 and rejects unauthenticated private requests')
PY

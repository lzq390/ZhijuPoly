#!/usr/bin/env bash
set -euo pipefail

readonly B_SHA="7df4ebf123982da8392ba00d2ce26205e74734b2"
readonly B_TREE="94d3176fc42ad4753a7a18b68d8a767be53a697d"
readonly B_BACKEND_IMAGE="ghcr.io/lzq390/nexpoly-backend@sha256:9a82b06c4411570699a332df3e54c5cf6f34ca08ecedd49c18f1c62a79fe0c45"
readonly B_WEB_IMAGE="ghcr.io/lzq390/nexpoly-web@sha256:2bac8c62ffc42a50a03ac15c6b04568c47d39685fdbac23ffb9a2b1e2abac2ac"
readonly BRIDGE_DB_ADMIN_DSN="postgresql://nexpoly_bridge:nexpoly_bridge@127.0.0.1:5432/postgres"
readonly B_DATABASE="nexpoly_b_schema"
readonly F_DATABASE="nexpoly_f_schema"

readonly REPOSITORY_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd -- "$REPOSITORY_ROOT"

candidate_sha="$(git rev-parse --verify HEAD)"
candidate_tree="$(git rev-parse --verify 'HEAD^{tree}')"
readonly candidate_sha candidate_tree
[[ "$candidate_sha" =~ ^[0-9a-f]{40}$ ]]
[[ "$candidate_tree" =~ ^[0-9a-f]{40}$ ]]
[[ "$(git rev-parse --verify "${B_SHA}^{tree}")" == "$B_TREE" ]]
git merge-base --is-ancestor "$B_SHA" "$candidate_sha"

readonly F_BACKEND_IMAGE="nexpoly-f-bridge-ci:${candidate_sha}"
readonly CONTAINER_PREFIX="nexpoly-bridge-ci-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"
managed_containers=()

cleanup() {
  if ((${#managed_containers[@]})); then
    docker rm -f "${managed_containers[@]}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

database_dsn() {
  local database="$1"
  printf 'postgresql://nexpoly_bridge:nexpoly_bridge@127.0.0.1:5432/%s' "$database"
}

run_backend_command() {
  local image="$1"
  local database="$2"
  shift 2
  local dsn
  dsn="$(database_dsn "$database")"
  docker run --rm --network host \
    -e "APP_POSTGRES_DSN=$dsn" \
    -e "PI_POSTGRES_DSN=$dsn" \
    -e "LAB_DATA_POSTGRES_DSN=$dsn" \
    -e STRUCTURED_DATA_BACKEND=postgres \
    -e MODEL_ENABLED=false \
    -e OCSR_ENABLED=false \
    -e GEN_MODEL_ENABLED=false \
    -e POLYTAO_ENABLED=false \
    -e RETRO_MODEL_ENABLED=false \
    -e MONOMER_MD_SUBMIT_ENABLED=false \
    -e MONOMER_DFT_SUBMIT_ENABLED=false \
    -e MONOMER_DFT_WORKER_UDS= \
    "$image" "$@"
}

wait_for_backend() {
  local port="$1"
  local attempt
  for attempt in {1..60}; do
    if curl --fail --silent --show-error "http://127.0.0.1:${port}/health" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

start_backend() {
  local image="$1"
  local database="$2"
  local name="$3"
  local port="$4"
  local dsn
  dsn="$(database_dsn "$database")"
  docker run -d --name "$name" --network host \
    -e "APP_POSTGRES_DSN=$dsn" \
    -e "PI_POSTGRES_DSN=$dsn" \
    -e "LAB_DATA_POSTGRES_DSN=$dsn" \
    -e STRUCTURED_DATA_BACKEND=postgres \
    -e MODEL_ENABLED=false \
    -e OCSR_ENABLED=false \
    -e GEN_MODEL_ENABLED=false \
    -e POLYTAO_ENABLED=false \
    -e RETRO_MODEL_ENABLED=false \
    -e MONOMER_MD_SUBMIT_ENABLED=false \
    -e MONOMER_DFT_SUBMIT_ENABLED=false \
    -e MONOMER_DFT_WORKER_UDS= \
    "$image" \
    uvicorn app.main:app --host 127.0.0.1 --port "$port" \
      --workers 1 --timeout-graceful-shutdown 10 >/dev/null
  managed_containers+=("$name")
  wait_for_backend "$port"
}

stop_backend() {
  local name="$1"
  docker rm -f "$name" >/dev/null
  local retained=()
  local value
  for value in "${managed_containers[@]}"; do
    [[ "$value" == "$name" ]] || retained+=("$value")
  done
  managed_containers=("${retained[@]}")
}

assert_dft_state() {
  local port="$1"
  local expected_ready="$2"
  local temporary
  temporary="$(mktemp)"
  curl --fail --silent --show-error \
    "http://127.0.0.1:${port}/api/v1/monomer-dft/status" >"$temporary"
  python3 - "$temporary" "$expected_ready" <<'PY'
import json
import pathlib
import sys

document = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
expected = sys.argv[2] == "true"
assert document["schema_ready"] is expected, document
assert document["available"] is False, document
if expected:
    assert document["worker_status"] in {
        "not_configured",
        "unavailable",
        "disabled",
    }, document
PY
  rm -f -- "$temporary"

  temporary="$(mktemp)"
  curl --fail --silent --show-error \
    "http://127.0.0.1:${port}/api/v1/monomer-dft/capabilities" >"$temporary"
  python3 - "$temporary" "$expected_ready" <<'PY'
import json
import pathlib
import sys

document = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
expected = sys.argv[2] == "true"
assert document["schema_ready"] is expected, document
assert document["available"] is False, document
PY
  rm -f -- "$temporary"

  if [[ "$expected_ready" == "false" ]]; then
    temporary="$(mktemp)"
    http_status="$(
      curl --silent --show-error --output "$temporary" --write-out '%{http_code}' \
        "http://127.0.0.1:${port}/api/v1/monomer-dft/jobs"
    )"
    [[ "$http_status" == "503" ]]
    python3 - "$temporary" <<'PY'
import json
import pathlib
import sys

document = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert document["code"] == "schema_not_ready", document
assert document["retryable"] is True, document
PY
    rm -f -- "$temporary"
  fi
}

business_digest() {
  local database="$1"
  local dsn
  dsn="$(database_dsn "$database")"
  psql -X -v ON_ERROR_STOP=1 -At "$dsn" <<'SQL' | sha256sum | cut -d' ' -f1
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY DEFERRABLE;
SELECT jsonb_build_object(
  'ledger',
    (SELECT jsonb_agg(to_jsonb(row) ORDER BY version)
       FROM (
         SELECT version, checksum
           FROM governance.schema_migrations
          ORDER BY version
       ) AS row),
  'online_jobs',
    (SELECT jsonb_agg(to_jsonb(row) ORDER BY job_id)
       FROM (SELECT * FROM online_knowledge.jobs ORDER BY job_id) AS row),
  'online_history',
    (SELECT jsonb_agg(to_jsonb(row) ORDER BY history_id)
       FROM (SELECT * FROM online_knowledge.history ORDER BY history_id) AS row),
  'md_jobs',
    (SELECT jsonb_agg(to_jsonb(row) ORDER BY job_id)
       FROM (SELECT * FROM md.monomer_md_jobs ORDER BY job_id) AS row),
  'dft_jobs',
    (SELECT jsonb_agg(to_jsonb(row) ORDER BY job_id)
       FROM (SELECT * FROM monomer_dft.jobs ORDER BY job_id) AS row),
  'dft_attempts',
    (SELECT jsonb_agg(to_jsonb(row) ORDER BY job_id, attempt)
       FROM (SELECT * FROM monomer_dft.job_attempts ORDER BY job_id, attempt) AS row),
  'dft_artifacts',
    (SELECT jsonb_agg(to_jsonb(row) ORDER BY job_id, artifact_id)
       FROM (SELECT * FROM monomer_dft.artifacts ORDER BY job_id, artifact_id) AS row)
)::text;
COMMIT;
SQL
}

docker pull "$B_BACKEND_IMAGE" >/dev/null
docker pull "$B_WEB_IMAGE" >/dev/null

[[ "$(
  docker image inspect "$B_BACKEND_IMAGE" \
    --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
)" == "$B_SHA" ]]
[[ "$(
  docker image inspect "$B_WEB_IMAGE" \
    --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
)" == "$B_SHA" ]]

docker build \
  --build-arg "SOURCE_REVISION=$candidate_sha" \
  --build-arg "SOURCE_URL=https://github.com/lzq390/ZhijuPoly" \
  --build-arg "VERSION=sha-$candidate_sha" \
  --tag "$F_BACKEND_IMAGE" \
  --file Dockerfile \
  . >/dev/null

[[ "$(
  docker image inspect "$F_BACKEND_IMAGE" \
    --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
)" == "$candidate_sha" ]]

for database in "$B_DATABASE" "$F_DATABASE"; do
  psql -X -v ON_ERROR_STOP=1 "$BRIDGE_DB_ADMIN_DSN" \
    --command "CREATE DATABASE ${database};" >/dev/null
done

run_backend_command "$B_BACKEND_IMAGE" "$B_DATABASE" \
  python -m app.postgres_migrations --mode bootstrap
run_backend_command "$B_BACKEND_IMAGE" "$B_DATABASE" \
  python -m app.postgres_preflight --mode schema --strict >/dev/null

# F must be safe on B's post-0012 schema: status and capabilities are available
# without querying monomer_dft tables, while all database-backed DFT routes
# fail before SQL with the stable schema_not_ready response.
b_schema_f_name="${CONTAINER_PREFIX}-f-on-b-schema"
start_backend "$F_BACKEND_IMAGE" "$B_DATABASE" "$b_schema_f_name" 18101
assert_dft_state 18101 false
stop_backend "$b_schema_f_name"

run_backend_command "$F_BACKEND_IMAGE" "$F_DATABASE" \
  python -m app.postgres_migrations --mode bootstrap
run_backend_command "$F_BACKEND_IMAGE" "$F_DATABASE" \
  python -m app.postgres_preflight --mode schema --strict >/dev/null

psql -X -v ON_ERROR_STOP=1 "$(database_dsn "$F_DATABASE")" <<'SQL' >/dev/null
BEGIN;
INSERT INTO online_knowledge.jobs (
  job_id, status, material, mode, max_papers, progress_stage,
  progress_message, processed_papers, total_papers, created_at, updated_at,
  result_data
) VALUES (
  'bridge-online-job', 'completed', 'polyimide', 'deep', 3, 'done',
  'complete', 3, 3, '2026-07-18T00:00:00Z', '2026-07-18T00:01:00Z',
  '{"papers": 3}'::jsonb
);
INSERT INTO online_knowledge.history (
  history_id, material, mode, created_at, papers_found,
  reactions_extracted, max_papers, result_data
) VALUES (
  910001, 'polyimide', 'deep', '2026-07-18T00:01:00Z',
  3, 2, 3, '{"source": "bridge-ci"}'::jsonb
);
INSERT INTO md.monomer_md_jobs (
  job_id, status, input_smiles, canonical_smiles, requested_steps,
  completed_steps, progress_percent, progress_stage, progress_message,
  engine, artifacts, result_data, created_at, updated_at, finished_at
) VALUES (
  'bridge-md-job', 'completed', 'CCO', 'CCO', 10, 10, 100,
  'completed', 'complete', 'bridge-ci', '{"trajectory": "kept"}'::jsonb,
  '{"energy": -1.25}'::jsonb, '2026-07-18T00:00:00Z',
  '2026-07-18T00:01:00Z', '2026-07-18T00:01:00Z'
);
INSERT INTO monomer_dft.jobs (
  job_id, idempotency_key, request_sha256, request_json, calculation_type,
  model_name, input_smiles, canonical_smiles, effective_charge, multiplicity,
  status, attempt_token, stage, progress_percent, scientific_status,
  result_json, timings, provenance, created_at, updated_at, submitted_at,
  started_at, finished_at, last_reconciled_at
) VALUES (
  '018f0000-0000-7000-8000-000000000001',
  'bridge-ci-job-0001',
  repeat('1', 64),
  '{"smiles": "O", "calculation_type": "single_point"}'::jsonb,
  'single_point', 'aimnet2', 'O', 'O', 0, 1, 'completed', repeat('2', 64),
  'single_point', 100, 'complete', '{"energy_ev": -7.5}'::jsonb,
  '{"total_seconds": 1.0}'::jsonb,
  '{"source": "bridge-ci"}'::jsonb,
  '2026-07-18T00:00:00Z', '2026-07-18T00:01:00Z',
  '2026-07-18T00:00:10Z', '2026-07-18T00:00:20Z',
  '2026-07-18T00:01:00Z', '2026-07-18T00:01:00Z'
);
INSERT INTO monomer_dft.job_attempts (
  job_id, attempt, attempt_token, request_sha256, status, outcome,
  created_at, submitted_at, started_at, finished_at
) VALUES (
  '018f0000-0000-7000-8000-000000000001', 1, repeat('2', 64),
  repeat('1', 64), 'completed', '{"energy_ev": -7.5}'::jsonb,
  '2026-07-18T00:00:00Z', '2026-07-18T00:00:10Z',
  '2026-07-18T00:00:20Z', '2026-07-18T00:01:00Z'
);
INSERT INTO monomer_dft.artifacts (
  job_id, artifact_id, name, relative_location, media_type, size_bytes,
  sha256, metadata, created_at, updated_at
) VALUES (
  '018f0000-0000-7000-8000-000000000001', 'result-json',
  'result.json', 'artifacts/result.json', 'application/json', 19,
  repeat('3', 64), '{"source": "bridge-ci"}'::jsonb,
  '2026-07-18T00:01:00Z', '2026-07-18T00:01:00Z'
);
COMMIT;
SQL

before_digest="$(business_digest "$F_DATABASE")"
readonly before_digest
[[ "$before_digest" =~ ^[0-9a-f]{64}$ ]]

f_before_name="${CONTAINER_PREFIX}-f-before"
start_backend "$F_BACKEND_IMAGE" "$F_DATABASE" "$f_before_name" 18102
assert_dft_state 18102 true
stop_backend "$f_before_name"
[[ "$(business_digest "$F_DATABASE")" == "$before_digest" ]]

# Exact B must accept the canonical forward 0013 ledger without applying,
# rewriting, or truncating any mutable row.
run_backend_command "$B_BACKEND_IMAGE" "$F_DATABASE" \
  python -m app.postgres_preflight --mode schema --strict >/dev/null
b_forward_name="${CONTAINER_PREFIX}-b-forward"
start_backend "$B_BACKEND_IMAGE" "$F_DATABASE" "$b_forward_name" 18103
stop_backend "$b_forward_name"
[[ "$(business_digest "$F_DATABASE")" == "$before_digest" ]]

# Returning to F must preserve the same ledger and all business rows.
run_backend_command "$F_BACKEND_IMAGE" "$F_DATABASE" \
  python -m app.postgres_preflight --mode schema --strict >/dev/null
f_after_name="${CONTAINER_PREFIX}-f-after"
start_backend "$F_BACKEND_IMAGE" "$F_DATABASE" "$f_after_name" 18104
assert_dft_state 18104 true
stop_backend "$f_after_name"
[[ "$(business_digest "$F_DATABASE")" == "$before_digest" ]]

web_name="${CONTAINER_PREFIX}-b-web"
docker run -d --name "$web_name" -p 127.0.0.1:18105:80 "$B_WEB_IMAGE" >/dev/null
managed_containers+=("$web_name")
for _ in {1..30}; do
  if curl --fail --silent --show-error http://127.0.0.1:18105/ >/dev/null; then
    break
  fi
  sleep 1
done
curl --fail --silent --show-error http://127.0.0.1:18105/ >/dev/null
stop_backend "$web_name"

printf 'exact B/F bridge smoke passed: B=%s B-tree=%s F=%s F-tree=%s data=%s\n' \
  "$B_SHA" "$B_TREE" "$candidate_sha" "$candidate_tree" "$before_digest"

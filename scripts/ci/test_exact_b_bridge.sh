#!/usr/bin/env bash
set -euo pipefail

readonly B_SHA="82a69ddb42bcd5c4666b5bf038d02414bccc6dde"
readonly B_TREE="44e4b4c398b7b84abdeb40bc02b885569aba4d8b"
readonly B_BRIDGE_CORE_BLOB="15b8a1378d4100a5c74666344107bf00661fe34f"
readonly B_BACKEND_IMAGE="ghcr.io/lzq390/nexpoly-backend@sha256:ecd522706ce34b6aa444b30f1dee49e34e9c5ab1e4bca78b6037848facacd8c7"
readonly B_WEB_IMAGE="ghcr.io/lzq390/nexpoly-web@sha256:bc4a472c7eab5fc4b2f1e278567d9fc2551ac70e720ff06053c297c6829c18e0"
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
[[ "$(git rev-parse --verify "${B_SHA}:scripts/bridge_deploy_core.py")" == "$B_BRIDGE_CORE_BLOB" ]]
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
    local job_id="018f0000-0000-4000-8000-000000000001"
    local submit_body
    submit_body="$(
      python3 - <<'PY'
import json

print(json.dumps({
    "input": {
        "smiles": "CCO",
        "net_charge": None,
        "multiplicity": 1,
        "psmiles_mode": None,
    },
    "calculation_type": "single_point",
    "model": "aimnet2",
    "conformer": {"seed": 1, "max_iterations": 500},
    "single_point": {"properties": ["energy", "forces", "charges"]},
}, separators=(",", ":")))
PY
    )"
    assert_schema_not_ready_route "$port" GET "/jobs"
    assert_schema_not_ready_route "$port" POST "/jobs" "$submit_body"
    assert_schema_not_ready_route "$port" GET "/jobs/${job_id}"
    assert_schema_not_ready_route "$port" POST "/jobs/${job_id}/cancel"
    assert_schema_not_ready_route \
      "$port" GET "/jobs/${job_id}/artifacts/scientific_result"
    assert_schema_not_ready_route "$port" GET "/jobs/${job_id}/bundle"
    assert_schema_not_ready_route "$port" DELETE "/jobs/${job_id}/artifacts"
  fi
}

assert_schema_not_ready_route() {
  local port="$1"
  local method="$2"
  local path="$3"
  local body="${4:-}"
  local temporary http_status
  local -a request=(
    --silent
    --show-error
    --output
    ""
    --write-out
    "%{http_code}"
    --request
    "$method"
    --header
    "Idempotency-Key: bridge-schema-gate-0001"
  )
  temporary="$(mktemp)"
  request[3]="$temporary"
  if [[ -n "$body" ]]; then
    request+=(--header "Content-Type: application/json" --data-binary "$body")
  fi
  http_status="$(
    curl "${request[@]}" \
      "http://127.0.0.1:${port}/api/v1/monomer-dft${path}"
  )"
  [[ "$http_status" == "503" ]]
  python3 - "$temporary" "$method" "$path" <<'PY'
import json
import pathlib
import sys

document = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert document == {
    "code": "schema_not_ready",
    "message": "monomer DFT schema is not ready",
    "retryable": True,
    "details": {},
}, (sys.argv[2], sys.argv[3], document)
assert document["retryable"] is True, document
PY
  rm -f -- "$temporary"
}

pre_dft_mutable_digest() {
  local database="$1"
  local dsn
  dsn="$(database_dsn "$database")"
  psql -X -v ON_ERROR_STOP=1 -At "$dsn" <<'SQL' | sha256sum | cut -d' ' -f1
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY DEFERRABLE;
SELECT jsonb_build_object(
  'online_jobs',
    (SELECT jsonb_agg(to_jsonb(row) ORDER BY job_id)
       FROM (SELECT * FROM online_knowledge.jobs ORDER BY job_id) AS row),
  'online_history',
    (SELECT jsonb_agg(to_jsonb(row) ORDER BY history_id)
       FROM (SELECT * FROM online_knowledge.history ORDER BY history_id) AS row),
  'md_jobs',
    (SELECT jsonb_agg(to_jsonb(row) ORDER BY job_id)
       FROM (SELECT * FROM md.monomer_md_jobs ORDER BY job_id) AS row),
  'lab_test_projects',
    (SELECT jsonb_agg(to_jsonb(row) ORDER BY id)
       FROM (SELECT * FROM lab.test_projects ORDER BY id) AS row),
  'lab_sample_measurements',
    (SELECT jsonb_agg(to_jsonb(row) ORDER BY id)
       FROM (SELECT * FROM lab.sample_measurements ORDER BY id) AS row),
  'mutable_sequences',
    jsonb_build_object(
      'online_history',
        (SELECT jsonb_build_object(
           'last_value', last_value,
           'is_called', is_called
         ) FROM online_knowledge.history_history_id_seq),
      'lab_test_projects',
        (SELECT jsonb_build_object(
           'last_value', last_value,
           'is_called', is_called
         ) FROM lab.test_projects_id_seq),
      'lab_sample_measurements',
        (SELECT jsonb_build_object(
           'last_value', last_value,
           'is_called', is_called
         ) FROM lab.sample_measurements_id_seq)
    )
)::text;
COMMIT;
SQL
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
  'lab_test_projects',
    (SELECT jsonb_agg(to_jsonb(row) ORDER BY id)
       FROM (SELECT * FROM lab.test_projects ORDER BY id) AS row),
  'lab_sample_measurements',
    (SELECT jsonb_agg(to_jsonb(row) ORDER BY id)
       FROM (SELECT * FROM lab.sample_measurements ORDER BY id) AS row),
  'dft_jobs',
    (SELECT jsonb_agg(to_jsonb(row) ORDER BY job_id)
       FROM (SELECT * FROM monomer_dft.jobs ORDER BY job_id) AS row),
  'dft_attempts',
    (SELECT jsonb_agg(to_jsonb(row) ORDER BY job_id, attempt)
       FROM (SELECT * FROM monomer_dft.job_attempts ORDER BY job_id, attempt) AS row),
  'dft_artifacts',
    (SELECT jsonb_agg(to_jsonb(row) ORDER BY job_id, artifact_id)
       FROM (SELECT * FROM monomer_dft.artifacts ORDER BY job_id, artifact_id) AS row),
  'mutable_sequences',
    jsonb_build_object(
      'online_history',
        (SELECT jsonb_build_object(
           'last_value', last_value,
           'is_called', is_called
         ) FROM online_knowledge.history_history_id_seq),
      'lab_test_projects',
        (SELECT jsonb_build_object(
           'last_value', last_value,
           'is_called', is_called
         ) FROM lab.test_projects_id_seq),
      'lab_sample_measurements',
        (SELECT jsonb_build_object(
           'last_value', last_value,
           'is_called', is_called
         ) FROM lab.sample_measurements_id_seq),
      'dft_jobs',
        (SELECT jsonb_build_object(
           'last_value', last_value,
           'is_called', is_called
         ) FROM monomer_dft.jobs_enqueue_sequence_seq)
    )
)::text;
COMMIT;
SQL
}

assert_frozen_b_parser_accepts_policy() {
  local temporary status=0
  temporary="$(mktemp -d)"
  if ! git show "${B_SHA}:scripts/bridge_deploy_core.py" \
    >"$temporary/bridge_deploy_core_b.py" \
    || ! git show "${B_SHA}:backend/migrations/postgres/manifest.json" \
      >"$temporary/manifest-b.json" \
    || ! git show "${B_SHA}:release-input.json" \
      >"$temporary/release-input-b.json"; then
    rm -rf -- "$temporary"
    return 1
  fi
  python3 - \
    "$temporary/bridge_deploy_core_b.py" \
    "$temporary/manifest-b.json" \
    "$temporary/release-input-b.json" \
    "$REPOSITORY_ROOT/scripts/bridge_deploy_core.py" \
    "$REPOSITORY_ROOT/backend/migrations/postgres/manifest.json" \
    "$REPOSITORY_ROOT/release-input.json" \
    "$REPOSITORY_ROOT/ops/config/production-bridge-policy.json" \
    "$REPOSITORY_ROOT/ops/config/postgres-media-authority-rules.json" \
    "$REPOSITORY_ROOT/ops/config/postgres-media-audit-role.sql.example" \
    "$B_SHA" \
    "$B_TREE" \
    "$B_BACKEND_IMAGE" \
    "$B_WEB_IMAGE" <<'PY' \
    || status=$?
import hashlib
import importlib.util
import json
import pathlib
import sys


def load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


(
    b_path,
    b_manifest_path,
    b_release_path,
    f_path,
    f_manifest_path,
    f_release_path,
    policy_path,
    rules_path,
    role_path,
) = map(pathlib.Path, sys.argv[1:10])
target_sha, target_tree, target_backend_image, target_web_image = sys.argv[10:]
b = load("nexpoly_exact_bridge_frozen_b", b_path)
f = load("nexpoly_exact_bridge_candidate_f", f_path)
b_manifest_bytes = b_manifest_path.read_bytes()
f_manifest_bytes = f_manifest_path.read_bytes()
b_manifest = json.loads(b_manifest_bytes)
f_manifest = json.loads(f_manifest_bytes)
policy_bytes = policy_path.read_bytes()
policy = json.loads(policy_bytes)
parsed_by_f = f.parse_policy(policy_bytes)
parsed_by_b = b.parse_policy(policy_bytes)
assert parsed_by_f == policy
assert parsed_by_b == policy
assert b_release_path.read_bytes() == f_release_path.read_bytes()
release_input = json.loads(f_release_path.read_bytes())
assert policy["target_sha"] == target_sha
assert policy["target_tree"] == target_tree
assert policy["target_ref"] == f"refs/nexpoly/bridge-target/{target_sha}"
assert policy["target_images"] == {
    "backend": target_backend_image,
    "web": target_web_image,
}
assert policy["asset_manifest_digest"] == release_input["asset_manifest_digest"]
assert release_input["datasets_on_asset_change"] == []
assert policy["datasets_on_asset_change"] == []
assert policy["accepted_migration_ledgers"] == f.expected_migration_registry(
    target_manifest_sha256=(
        "sha256:" + hashlib.sha256(b_manifest_bytes).hexdigest()
    ),
    target_records=b_manifest["migrations"],
    authority_manifest_sha256=(
        "sha256:" + hashlib.sha256(f_manifest_bytes).hexdigest()
    ),
    authority_records=f_manifest["migrations"],
)
assert policy["external_database_audit"] == {
    **f.EXTERNAL_DATABASE_AUDIT_POLICY,
    "media_authority_rules_sha256": (
        "sha256:" + hashlib.sha256(rules_path.read_bytes()).hexdigest()
    ),
    "audit_role_sql_sha256": (
        "sha256:" + hashlib.sha256(role_path.read_bytes()).hexdigest()
    ),
}
assert policy["required_ci_jobs"] == sorted(f.REQUIRED_CI_JOBS)
assert policy["policy_id"] == f.canonical_json_digest(
    {key: value for key, value in policy.items() if key != "policy_id"}
)
assert "exact-B bridge compatibility" in policy["required_ci_jobs"]
assert "exact-B bridge compatibility" not in b.REQUIRED_CI_JOBS
PY
  rm -rf -- "$temporary"
  return "$status"
}

assert_frozen_b_parser_accepts_policy

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

# Seed online/MD/lab business rows before F creates any DFT table. This is the
# actual future transition: an exact B/post-0012 database is upgraded in place
# by F applying only 0013. Neither the mutable rows nor their sequences may
# change while the migration ledger advances.
psql -X -v ON_ERROR_STOP=1 "$(database_dsn "$B_DATABASE")" <<'SQL' >/dev/null
BEGIN;
INSERT INTO online_knowledge.jobs (
  job_id, status, material, mode, max_papers, progress_stage,
  progress_message, processed_papers, total_papers, created_at, updated_at,
  result_data
) VALUES (
  'bridge-b-online-job', 'completed', 'polyimide', 'deep', 3, 'done',
  'complete', 3, 3, '2026-07-18T00:00:00Z', '2026-07-18T00:01:00Z',
  '{"papers": 3}'::jsonb
);
INSERT INTO online_knowledge.history (
  history_id, material, mode, created_at, papers_found,
  reactions_extracted, max_papers, result_data
) VALUES (
  920001, 'polyimide', 'deep', '2026-07-18T00:01:00Z',
  3, 2, 3, '{"source": "bridge-b-transition"}'::jsonb
);
INSERT INTO md.monomer_md_jobs (
  job_id, status, input_smiles, canonical_smiles, requested_steps,
  completed_steps, progress_percent, progress_stage, progress_message,
  engine, artifacts, result_data, created_at, updated_at, finished_at
) VALUES (
  'bridge-b-md-job', 'completed', 'CCO', 'CCO', 10, 10, 100,
  'completed', 'complete', 'bridge-ci', '{"trajectory": "kept"}'::jsonb,
  '{"energy": -1.25}'::jsonb, '2026-07-18T00:00:00Z',
  '2026-07-18T00:01:00Z', '2026-07-18T00:01:00Z'
);
INSERT INTO lab.test_projects (
  project_name, result_unit
) VALUES (
  'bridge-b-project', 'MPa'
);
INSERT INTO lab.sample_measurements (
  sample_id, experiment_project, instrument_id, "operator",
  collection_time, temperature, concentration, result_value,
  result_unit, remarks
) VALUES (
  'bridge-b-sample', 'bridge-b-project', 'bridge-instrument',
  'bridge-operator', '2026-07-18T00:00:30', 25.00, 0.1250,
  42.5000, 'MPa', 'mutable row retained while F applies 0013'
);
COMMIT;
SQL

b_transition_before="$(pre_dft_mutable_digest "$B_DATABASE")"
readonly b_transition_before
[[ "$b_transition_before" =~ ^[0-9a-f]{64}$ ]]

run_backend_command "$F_BACKEND_IMAGE" "$B_DATABASE" \
  python -m app.postgres_migrations --mode expand
run_backend_command "$F_BACKEND_IMAGE" "$B_DATABASE" \
  python -m app.postgres_preflight --mode schema --strict >/dev/null
[[ "$(
  psql -X -v ON_ERROR_STOP=1 -At "$(database_dsn "$B_DATABASE")" \
    --command "SELECT version || ':' || checksum FROM governance.schema_migrations ORDER BY version DESC LIMIT 1"
)" == "0013_monomer_dft_jobs:ab633a6253887dad45103c288d54a0d02d4d69ce1f9a14c1271338d448f9acbc" ]]
[[ "$(pre_dft_mutable_digest "$B_DATABASE")" == "$b_transition_before" ]]

b_transition_f_name="${CONTAINER_PREFIX}-f-after-0013"
start_backend "$F_BACKEND_IMAGE" "$B_DATABASE" "$b_transition_f_name" 18106
assert_dft_state 18106 true
stop_backend "$b_transition_f_name"
[[ "$(pre_dft_mutable_digest "$B_DATABASE")" == "$b_transition_before" ]]

# The same transitioned database must remain readable by exact B and then F
# again without rewriting the forward 0013 ledger or business state.
run_backend_command "$B_BACKEND_IMAGE" "$B_DATABASE" \
  python -m app.postgres_preflight --mode schema --strict >/dev/null
b_transition_b_name="${CONTAINER_PREFIX}-b-after-0013"
start_backend "$B_BACKEND_IMAGE" "$B_DATABASE" "$b_transition_b_name" 18107
stop_backend "$b_transition_b_name"
[[ "$(pre_dft_mutable_digest "$B_DATABASE")" == "$b_transition_before" ]]
run_backend_command "$F_BACKEND_IMAGE" "$B_DATABASE" \
  python -m app.postgres_preflight --mode schema --strict >/dev/null
[[ "$(pre_dft_mutable_digest "$B_DATABASE")" == "$b_transition_before" ]]

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
INSERT INTO lab.test_projects (
  project_name, result_unit
) VALUES (
  'bridge-ci-project', 'MPa'
);
INSERT INTO lab.sample_measurements (
  sample_id, experiment_project, instrument_id, "operator",
  collection_time, temperature, concentration, result_value,
  result_unit, remarks
) VALUES (
  'bridge-ci-sample', 'bridge-ci-project', 'bridge-instrument',
  'bridge-operator', '2026-07-18T00:00:30', 25.00, 0.1250,
  42.5000, 'MPa', 'mutable row retained across F/B/F'
);
INSERT INTO monomer_dft.jobs (
  job_id, idempotency_key, request_sha256, request_json, calculation_type,
  model_name, input_smiles, canonical_smiles, effective_charge, multiplicity,
  status, attempt_token, stage, progress_percent, scientific_status,
  result_json, timings, provenance, created_at, updated_at, submitted_at,
  started_at, finished_at, last_reconciled_at
) VALUES (
  '018f0000-0000-4000-8000-000000000001',
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
  '018f0000-0000-4000-8000-000000000001', 1, repeat('2', 64),
  repeat('1', 64), 'completed', '{"energy_ev": -7.5}'::jsonb,
  '2026-07-18T00:00:00Z', '2026-07-18T00:00:10Z',
  '2026-07-18T00:00:20Z', '2026-07-18T00:01:00Z'
);
INSERT INTO monomer_dft.artifacts (
  job_id, artifact_id, name, relative_location, media_type, size_bytes,
  sha256, metadata, created_at, updated_at
) VALUES (
  '018f0000-0000-4000-8000-000000000001', 'result-json',
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
# The frozen B Nginx configuration resolves its production ``backend``
# upstream at startup even though this smoke reads only the immutable static
# root.  Give that name a loopback-only, non-routable target so the container
# can start without attaching it to any application or production network.
docker run -d --name "$web_name" \
  --add-host backend:127.0.0.1 \
  -p 127.0.0.1:18105:80 \
  "$B_WEB_IMAGE" >/dev/null
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

#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

log() {
  printf '[nexpoly-deploy] %s\n' "$*"
}

die() {
  printf '[nexpoly-deploy] ERROR: %s\n' "$*" >&2
  exit 1
}

trim_value() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

read_dotenv_value() {
  local key="$1"
  local line=""
  local name=""
  local value=""
  local first=""
  local last=""

  [[ -f "$ROOT_DIR/.env" ]] || return 1

  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" == *=* ]] || continue
    name="$(trim_value "${line%%=*}")"
    [[ "$name" == "$key" ]] || continue

    value="$(trim_value "${line#*=}")"
    if [[ ${#value} -ge 2 ]]; then
      first="${value:0:1}"
      last="${value: -1}"
      if [[ "$first" == '"' && "$last" == '"' ]] || [[ "$first" == "'" && "$last" == "'" ]]; then
        value="${value:1:${#value}-2}"
      fi
    fi
    printf '%s\n' "$value"
    return 0
  done < "$ROOT_DIR/.env"

  return 1
}

config_value() {
  local key="$1"
  local default_value="$2"
  local dotenv_value=""

  if [[ -n "${!key:-}" ]]; then
    printf '%s\n' "${!key}"
    return 0
  fi

  dotenv_value="$(read_dotenv_value "$key" || true)"
  if [[ -n "$dotenv_value" ]]; then
    printf '%s\n' "$dotenv_value"
  else
    printf '%s\n' "$default_value"
  fi
}

validate_port() {
  local name="$1"
  local value="$2"

  case "$value" in
    *[!0-9]*|"")
      die "$name must be a numeric TCP port."
      ;;
  esac
  if (( value < 1 || value > 65535 )); then
    die "$name must be between 1 and 65535."
  fi
}

DEPLOY_REF="${NEXPOLY_DEPLOY_REF:-main}"
DEPLOY_BRANCH="${NEXPOLY_DEPLOY_BRANCH:-main}"
WEB_PORT="$(config_value NEXPOLY_WEB_PORT 9000)"
POSTGRES_PORT="$(config_value NEXPOLY_POSTGRES_PORT 55432)"
validate_port NEXPOLY_WEB_PORT "$WEB_PORT"
validate_port NEXPOLY_POSTGRES_PORT "$POSTGRES_PORT"
export NEXPOLY_WEB_PORT="$WEB_PORT"
export NEXPOLY_POSTGRES_PORT="$POSTGRES_PORT"

PYTHON_BIN="${NEXPOLY_TEST_PYTHON:-/home/lzq390/miniconda3/envs/screen312/bin/python}"
TEST_POSTGRES_DSN="${NEXPOLY_TEST_POSTGRES_DSN:-postgresql://polyprop:polyprop@localhost:${POSTGRES_PORT}/nexpoly}"
BACKUP_DIR="${NEXPOLY_BACKUP_DIR:-$ROOT_DIR/backups}"
TMP_DIR=""
TEST_WORKTREE=""
TARGET_COMMIT=""

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command is missing: $1"
}

dump_compose_state() {
  log "Docker Compose service state:"
  docker compose ps || true
  log "Recent service logs:"
  docker compose logs --tail=120 postgres-init backend nginx lab-postgres || true
}

cleanup() {
  local status=$?
  if [[ -n "$TEST_WORKTREE" ]]; then
    git worktree remove --force "$TEST_WORKTREE" >/dev/null 2>&1 || true
  fi
  if [[ -n "$TMP_DIR" && "$TMP_DIR" == /tmp/nexpoly-deploy.* ]]; then
    rm -rf "$TMP_DIR"
  fi
  if [[ "$status" -ne 0 ]]; then
    dump_compose_state
  fi
}
trap cleanup EXIT

resolve_target_ref() {
  local requested="$1"
  local resolved=""

  if [[ "$requested" == "$DEPLOY_BRANCH" || "$requested" == "refs/heads/$DEPLOY_BRANCH" || "$requested" == "origin/$DEPLOY_BRANCH" ]]; then
    resolved="origin/$DEPLOY_BRANCH"
  elif git rev-parse --verify --quiet "origin/${requested}^{commit}" >/dev/null; then
    resolved="origin/$requested"
  elif git rev-parse --verify --quiet "${requested}^{commit}" >/dev/null; then
    resolved="$requested"
  else
    die "Cannot resolve deploy ref after fetch: $requested"
  fi

  git rev-parse "${resolved}^{commit}"
}

assert_tracked_worktree_clean() {
  git diff --quiet || die "Deployment checkout has unstaged tracked changes."
  git diff --cached --quiet || die "Deployment checkout has staged tracked changes."
}

wait_for_postgres() {
  log "Waiting for lab-postgres to become ready."
  for _ in $(seq 1 60); do
    if docker compose exec -T lab-postgres pg_isready -U polyprop -d nexpoly >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  die "lab-postgres did not become ready."
}

wait_for_service_healthy() {
  local service="$1"
  local container_id=""
  local health=""

  log "Waiting for $service healthcheck."
  for _ in $(seq 1 60); do
    container_id="$(docker compose ps -q "$service" 2>/dev/null || true)"
    if [[ -n "$container_id" ]]; then
      health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$container_id" 2>/dev/null || true)"
      if [[ "$health" == "healthy" || "$health" == "no-healthcheck" ]]; then
        return 0
      fi
      if [[ "$health" == "unhealthy" ]]; then
        die "$service healthcheck reported unhealthy."
      fi
    fi
    sleep 2
  done
  die "$service did not become healthy."
}

check_compose_contract() {
  docker compose config --quiet
  docker compose config --services | grep -qx 'lab-postgres' || die "docker-compose.yml must define lab-postgres."
  docker compose config --services | grep -qx 'postgres-init' || die "docker-compose.yml must define postgres-init."
  docker compose config --services | grep -qx 'backend' || die "docker-compose.yml must define backend."
  docker compose config --services | grep -qx 'nginx' || die "docker-compose.yml must define nginx."
}

check_required_data_sources() {
  local missing=0
  local required_path
  local required_paths=(
    "database/data1.csv"
    "database/data_txt.zip"
    "database/polymer_process_material_filtered_cleaned_office_utf8_bom.csv"
    "database/polymer_property_detail_cleaned_office_utf8_bom.csv"
    "backend/data/polyprop.db"
    "backend/data/pi_reverse_design.db"
    "backend/data/fumol.db"
  )

  log "Checking deployment data sources."
  for required_path in "${required_paths[@]}"; do
    if [[ ! -e "$ROOT_DIR/$required_path" && ! -L "$ROOT_DIR/$required_path" ]]; then
      printf '[nexpoly-deploy] Missing data source: %s\n' "$required_path" >&2
      missing=1
    fi
  done

  [[ "$missing" -eq 0 ]] || die "Required deployment data sources are missing."
}

check_required_model_assets() {
  local missing=0
  local asset_path

  log "Checking deployment model assets."
  while IFS= read -r asset_path; do
    if [[ ! -s "$ROOT_DIR/$asset_path" ]]; then
      printf '[nexpoly-deploy] Missing model asset: %s\n' "$asset_path" >&2
      missing=1
    fi
  done < <(cd "$TEST_WORKTREE" && PYTHONPATH=backend "$PYTHON_BIN" -m app.model_asset_manifest --format paths)

  if [[ ! -d "$ROOT_DIR/model/reactiont5-retrosynthesis" ]]; then
    printf '[nexpoly-deploy] Missing model directory: model/reactiont5-retrosynthesis\n' >&2
    missing=1
  fi

  [[ "$missing" -eq 0 ]] || die "Required deployment model assets are missing."
}

run_backend_tests() {
  [[ -x "$PYTHON_BIN" ]] || die "screen312 Python is not executable: $PYTHON_BIN"

  log "Running backend pytest in temporary worktree."
  (
    cd "$TEST_WORKTREE/backend"
    APP_POSTGRES_DSN="$TEST_POSTGRES_DSN" \
      PI_POSTGRES_DSN="$TEST_POSTGRES_DSN" \
      LAB_DATA_POSTGRES_DSN="$TEST_POSTGRES_DSN" \
      "$PYTHON_BIN" -m pytest
  )
}

create_database_backup() {
  local short_sha="${TARGET_COMMIT:0:12}"
  local backup_file="$BACKUP_DIR/nexpoly-${short_sha}.dump"

  mkdir -p "$BACKUP_DIR"
  if [[ -e "$backup_file" ]]; then
    backup_file="$BACKUP_DIR/nexpoly-${short_sha}-$(date +%Y%m%d-%H%M%S).dump"
  fi

  log "Backing up Postgres database to $backup_file."
  docker compose exec -T lab-postgres pg_dump -U polyprop -d nexpoly -Fc > "$backup_file"
}

checkout_target_ref() {
  log "Updating deployment checkout to $DEPLOY_REF ($TARGET_COMMIT)."
  if [[ "$DEPLOY_REF" == "$DEPLOY_BRANCH" || "$DEPLOY_REF" == "refs/heads/$DEPLOY_BRANCH" || "$DEPLOY_REF" == "origin/$DEPLOY_BRANCH" ]]; then
    if git show-ref --verify --quiet "refs/heads/$DEPLOY_BRANCH"; then
      git checkout "$DEPLOY_BRANCH"
    else
      git checkout -b "$DEPLOY_BRANCH" "origin/$DEPLOY_BRANCH"
    fi
    git pull --ff-only origin "$DEPLOY_BRANCH"
    [[ "$(git rev-parse HEAD)" == "$TARGET_COMMIT" ]] || die "Branch HEAD does not match tested target commit."
  else
    git checkout --detach "$TARGET_COMMIT"
  fi
}

deploy_compose_stack() {
  log "Building Docker images."
  docker compose build

  log "Running Postgres import gate."
  docker compose run --rm postgres-init

  log "Recreating backend."
  docker compose up -d --no-deps --force-recreate backend
  wait_for_service_healthy backend

  log "Recreating nginx."
  docker compose up -d --no-deps --force-recreate nginx

  log "Running runtime Postgres preflight."
  docker compose exec -T backend python -m app.postgres_preflight --mode runtime --strict

  log "Checking public health endpoint on localhost:$WEB_PORT."
  curl --fail --silent --show-error --max-time 10 "http://localhost:${WEB_PORT}/health"
  printf '\n'
}

main() {
  case "$DEPLOY_REF" in
    *[!A-Za-z0-9._/@:-]*)
      die "NEXPOLY_DEPLOY_REF contains unsupported characters: $DEPLOY_REF"
      ;;
  esac

  [[ -d .git ]] || die "$ROOT_DIR is not a Git checkout."
  [[ -f docker-compose.yml ]] || die "docker-compose.yml is missing from $ROOT_DIR."

  require_cmd git
  require_cmd docker
  require_cmd curl
  require_cmd grep

  assert_tracked_worktree_clean
  check_compose_contract

  log "Using web port $WEB_PORT and Postgres host port $POSTGRES_PORT."
  log "Fetching refs from origin."
  git fetch --prune origin '+refs/heads/*:refs/remotes/origin/*' '+refs/tags/*:refs/tags/*'
  TARGET_COMMIT="$(resolve_target_ref "$DEPLOY_REF")"

  TMP_DIR="$(mktemp -d /tmp/nexpoly-deploy.XXXXXX)"
  TEST_WORKTREE="$TMP_DIR/worktree"
  git worktree add --detach "$TEST_WORKTREE" "$TARGET_COMMIT" >/dev/null

  docker compose up -d lab-postgres
  wait_for_postgres

  run_backend_tests
  check_required_data_sources
  check_required_model_assets
  create_database_backup
  checkout_target_ref
  deploy_compose_stack

  log "Deployment completed at commit $TARGET_COMMIT."
}

main "$@"

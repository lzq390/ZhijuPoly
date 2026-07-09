# nexpoly Deployment

This deployment runs `nexpoly` as the Docker Compose project defined by
`docker-compose.yml`. It is designed to run in parallel with the existing
`polyprop` deployment and must not stop, remove, prune, or overwrite any
`polyprop` containers, images, volumes, or deployment directories.

Default server deployment path:

```text
/data/lzq/gith/nexpoly
```

Default public URL:

```text
http://114.214.255.154:9000
```

## Services

- `lab-postgres`: PostgreSQL 16 service for all runtime data.
- `postgres-init`: one-shot import gate that runs
  `python -m app.import_postgres --dataset all --refresh-analytics-snapshot`.
  It intentionally does not pass `--rebuild`.
- `backend`: FastAPI service on port `8000` inside the Docker network. By
  default it is limited to host GPU `2` through `NEXPOLY_GPU_DEVICE`. OCSR,
  conditional generation, retrosynthesis, and PolyTAO all run as backend GPU
  model services.
- `nginx`: Nginx static site on host port `${NEXPOLY_WEB_PORT:-9000}`, with
  `/api` and `/health` proxied to the backend.

Local build images are named explicitly:

```text
nexpoly-backend:latest
nexpoly-nginx:latest
```

## Runtime Contract

The application runtime is Postgres-only. SQLite files are retained only as
legacy import, migration-source, or audit rollback inputs; they are not runtime
backends.

The Compose startup order is:

```text
lab-postgres -> postgres-init -> backend -> nginx
```

`backend` is considered healthy only after:

```bash
python -m app.postgres_preflight --strict
```

and the local FastAPI `/health` route both succeed.

## Required Runtime Files

The server checkout must keep deployment-only files outside Git control. They
are mounted into containers by `docker-compose.yml`:

```text
database/data1.csv
database/PolymerDatabaseV2.0_reliable085_standardized.csv
database/data_txt.zip
database/polymer_process_material_filtered_cleaned_office_utf8_bom.csv
database/polymer_property_detail_cleaned_office_utf8_bom.csv
backend/data/polyprop.db
backend/data/fumol.db
backend/data/pi_reverse_design.db
model/
.env
.env.ai, if present
.env.monomer-md-worker, if monomer MD real-mode worker is enabled
```

Required model artifacts are defined by `backend/app/model_asset_manifest.py`.
The deployment script checks that manifest before rebuilding containers. The
current required set includes:

```text
model/rf_*.pkl
model/ocsr/swin_base_char_aux_1m.pth
model/conditional_generation/
model/reactiont5-retrosynthesis/
```

The backend PolyTAO runtime additionally requires these deployment-only files
when `POLYTAO_ENABLED=true`:

```text
model/polytao/config.json
model/polytao/pytorch_model.bin
model/polytao/tokenizer.json
model/polytao/spiece.model
```

If these files are absent while `POLYTAO_ENABLED=true`, deployment blocks before
recreating backend. Set `POLYTAO_ENABLED=false` to keep the rest of backend
available while PolyTAO submissions are disabled.

Treat the existing `polyprop` deployment as read-only when copying or refreshing
any of these assets.

## Server Environment

Create `/data/lzq/gith/nexpoly/.env` on the server:

```bash
NEXPOLY_WEB_PORT=9000
NEXPOLY_POSTGRES_PORT=55432
NEXPOLY_GPU_DEVICE=2
GEN_MODEL_ENABLED=true
RETRO_MODEL_ENABLED=true
RETRO_MODEL_ID=/app/model/reactiont5-retrosynthesis
RETRO_DEVICE=auto
POLYTAO_ENABLED=true
POLYTAO_MODEL_DIR=/app/model/polytao
POLYTAO_DEVICE=auto
POLYTAO_JOB_WORKERS=1
POLYTAO_MAX_ACTIVE_JOBS=1
MONOMER_MD_SUBMIT_ENABLED=true
MONOMER_MD_RATE_LIMIT_PER_IP_PER_MINUTE=3
MONOMER_MD_RATE_LIMIT_WINDOW_SECONDS=60
MONOMER_MD_MAX_ACTIVE_JOBS=1
```

Optional local secrets such as online knowledge or assistant API credentials can
remain in `/data/lzq/gith/nexpoly/.env.ai`. Do not commit `.env` or `.env.ai`.

Required server tools:

```text
git
docker compose
NVIDIA container runtime for GPU-backed backend workloads
systemctl --user, if the host-side monomer MD worker is supervised by user systemd
Postgres role able to create and drop temporary test databases, only when server-side pytest is enabled
```

## GitHub Actions Pipeline

The workflow is `.github/workflows/nexpoly-deploy.yml`.

Triggers:

- `pull_request` to `main`: frontend build, Compose config validation, backend
  monomer MD tests, backend PolyTAO/Postgres tests, and worker monomer MD tests.
- `push` to `main`: CI, then SSH deployment to the server.
- `workflow_dispatch`: CI and manual deployment of a selected Git ref, with an
  optional post-deploy monomer MD `CCO` smoke.

Required GitHub Secrets:

| Secret | Purpose |
| --- | --- |
| `NEXPOLY_SSH_HOST` | Deployment server host or IP. |
| `NEXPOLY_SSH_USER` | SSH user that owns `/data/lzq/gith/nexpoly`. |
| `NEXPOLY_SSH_PRIVATE_KEY` | Private key for the deployment user. |
| `NEXPOLY_SSH_KNOWN_HOSTS` | Optional pinned SSH host key entries. If omitted, the workflow uses `ssh-keyscan`. |

Required GitHub Variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `NEXPOLY_SSH_PORT` | `22` | SSH port. |
| `NEXPOLY_DEPLOY_PATH` | `/data/lzq/gith/nexpoly` | Server checkout path. |
| `NEXPOLY_WEB_PORT` | optional | Optional override for the server `.env` web port. If omitted, the server `.env` value is used, then `9000`. |
| `NEXPOLY_MONOMER_MD_SMOKE` | `false` | Optional default for running the post-deploy monomer MD `CCO` smoke on push deploys. Manual dispatch input overrides it. |
| `NEXPOLY_MONOMER_MD_SMOKE_TIMEOUT_SECONDS` | `300` | Optional timeout for the post-deploy monomer MD `CCO` smoke. Manual dispatch input overrides it. |

CI on GitHub hosted runners runs the monomer MD backend test module against a
Postgres service with `backend/requirements-monomer-md-ci.txt`, runs the
PolyTAO/database-governance backend tests against Postgres, and runs the
standalone monomer MD worker tests. It intentionally does not run the full
backend pytest suite. Full backend tests, when enabled on the deployment
server with `NEXPOLY_RUN_SERVER_TESTS=true`, depend on a configured
`NEXPOLY_TEST_PYTHON` and a Postgres role that can create/drop isolated test
databases.
The server deployment step first bootstraps the checkout to the requested ref,
then runs the `scripts/deploy_server.sh` from that ref so first-time deployments
and later script updates use the correct script version. The deploy job creates
a Git bundle on the GitHub runner, uploads it to
`$NEXPOLY_DEPLOY_PATH/ops/incoming/`, and fetches deployment refs from that
bundle on the server. The normal CI/CD path therefore does not depend on the
server being able to fetch from GitHub directly.

`NEXPOLY_WEB_PORT` in GitHub Variables is optional. Leave it unset to use the
server `.env` value. `NEXPOLY_POSTGRES_PORT` is intentionally managed on the
server through `.env`, not as a GitHub Variable.

## Manual Server Deployment

From the server:

```bash
cd /data/lzq/gith/nexpoly
NEXPOLY_DEPLOY_REF=main scripts/deploy_server.sh
```

The GitHub workflow bootstraps the deployment checkout before invoking the
script: it verifies tracked files are clean, fetches refs from the uploaded
bundle, checks out the target commit, and fails clearly if the target ref does
not contain `scripts/deploy_server.sh`. Manual server deployments without
`NEXPOLY_DEPLOY_BUNDLE` still fetch from `origin`.

If real-mode monomer MD is enabled, install the user-level worker service once:

```bash
cd /data/lzq/gith/nexpoly
scripts/install_monomer_md_worker_user_service.sh
```

The deploy script restarts `nexpoly-monomer-md-worker.service` when the user
unit is installed. If the unit is absent or user systemd is unavailable, it
falls back to the pidfile-managed worker and still blocks deployment unless the
worker health endpoint reports `status=ok` and `runtime_ready=true`.

The script then performs these gates in order:

1. Verifies the deployment checkout has no tracked local modifications.
2. Reads `NEXPOLY_WEB_PORT` and `NEXPOLY_POSTGRES_PORT` from explicit shell env,
   then server `.env`, then defaults `9000` and `55432`.
3. Validates the Docker Compose service contract.
4. Fetches the requested Git ref, from `NEXPOLY_DEPLOY_BUNDLE` when present,
   and creates a temporary worktree for tests.
5. Starts or confirms `lab-postgres`.
6. Optionally runs backend pytest against local Postgres when
   `NEXPOLY_RUN_SERVER_TESTS=true`.
7. Checks required data sources and model assets.
8. Creates `backups/nexpoly-$SHA.dump` with `pg_dump -Fc`.
9. Updates the deployment checkout with a fast-forward merge to the tested
   target commit for the normal `main` deployment path.
10. Runs `docker compose build`.
11. Runs `docker compose run --rm postgres-init`.
12. Recreates `backend` and `nginx`.
13. Restarts the monomer MD worker through user systemd or pidfile fallback when
    `.env.monomer-md-worker` exists.
14. Verifies strict Postgres preflight, backend monomer MD status, optional
    `NEXPOLY_MONOMER_MD_SMOKE=true` CCO artifact smoke, backend PolyTAO status
    when PolyTAO is enabled, and `http://127.0.0.1:$NEXPOLY_WEB_PORT/health`.

The script never runs `--rebuild`, never prunes Docker resources, and never
targets the old `polyprop` compose project.

## Monomer MD Public Demo Guardrails

The monomer MD endpoint is a public demo entrypoint on the `9000` service, so it
does not require login but is resource-limited before a job row is created:

```bash
MONOMER_MD_SUBMIT_ENABLED=true
MONOMER_MD_RATE_LIMIT_PER_IP_PER_MINUTE=3
MONOMER_MD_RATE_LIMIT_WINDOW_SECONDS=60
MONOMER_MD_MAX_ACTIVE_JOBS=1
```

`MONOMER_MD_MAX_ACTIVE_JOBS` counts `pending`, `submitted`, and `running` jobs in
Postgres. When the service is busy or an IP exceeds the window limit, the
backend returns `429` and does not create another `md.monomer_md_jobs` row.
Set `MONOMER_MD_SUBMIT_ENABLED=false` to keep `/status` available while
rejecting new submissions with `503`.

To make deployment run a real 1000-step `CCO` smoke, set:

```bash
NEXPOLY_MONOMER_MD_SMOKE=true
NEXPOLY_MONOMER_MD_SMOKE_TIMEOUT_SECONDS=300
```

For GitHub Actions, use the `workflow_dispatch` inputs `monomer_md_smoke=true`
and `monomer_md_smoke_timeout_seconds=300`, or set the GitHub Variables above
for push deploys. The smoke is disabled by default to avoid consuming GPU time
on every deploy.

## Health Checks

Check the new `nexpoly` deployment:

```bash
cd /data/lzq/gith/nexpoly
docker compose ps
docker compose config --images
docker compose exec -T backend python -m app.postgres_preflight --mode runtime --strict
curl http://localhost:9000/health
```

Confirm the old `polyprop` deployment separately, without modifying it:

```bash
docker compose ls
curl http://localhost:10000/health
```

## Rollback

Each deployment creates a compressed Postgres backup under:

```text
/data/lzq/gith/nexpoly/backups/
```

For a code rollback, SSH to the server, checkout the previous known-good Git ref,
and rerun the deployment script:

```bash
cd /data/lzq/gith/nexpoly
NEXPOLY_DEPLOY_REF=<previous-sha-or-tag> scripts/deploy_server.sh
```

For a data rollback, restore the matching dump into `lab-postgres` before
rerunning the deployment script. Keep restored data and code refs paired; schema
migration checksum mismatches should be treated as blockers, not ignored. See
[`docs/postgres-migration-governance.md`](postgres-migration-governance.md) for
the guarded reconcile process.

## Release Package

The SSH pipeline is the default deployment path. A release bundle can still be
created for offline handoff:

```bash
scripts/package_release.sh
```

By default, legacy data files are excluded to keep the package small. To bundle
prepared data too:

```bash
INCLUDE_DATA=1 scripts/package_release.sh
```

The package is written to `release/nexpoly-release-*.tar.gz`. Model artifacts
are included by default and validated through the shared model asset manifest.

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
  default it is limited to host GPU `2` through `NEXPOLY_GPU_DEVICE`.
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
database/data_txt.zip
database/polymer_process_material_filtered_cleaned_office_utf8_bom.csv
database/polymer_property_detail_cleaned_office_utf8_bom.csv
backend/data/polyprop.db
backend/data/fumol.db
backend/data/pi_reverse_design.db
model/
.env
.env.ai, if present
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
```

Optional local secrets such as online knowledge or assistant API credentials can
remain in `/data/lzq/gith/nexpoly/.env.ai`. Do not commit `.env` or `.env.ai`.

Required server tools:

```text
git
docker compose
NVIDIA container runtime for GPU-backed backend workloads
/home/lzq390/miniconda3/envs/screen312/bin/python
Postgres role able to create and drop temporary test databases
```

## GitHub Actions Pipeline

The workflow is `.github/workflows/nexpoly-deploy.yml`.

Triggers:

- `pull_request` to `main`: frontend build and Compose config validation only.
- `push` to `main`: CI, then SSH deployment to the server.
- `workflow_dispatch`: CI and manual deployment of a selected Git ref.

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

CI on GitHub hosted runners intentionally does not run the full backend pytest
suite. Backend tests are Postgres-only and depend on the server's `screen312`
environment and a Postgres role that can create/drop isolated test databases.
The server deployment step first bootstraps the checkout to the requested ref,
then runs the `scripts/deploy_server.sh` from that ref so first-time deployments
and later script updates use the correct script version.

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
script: it verifies tracked files are clean, fetches refs, checks out the target
commit, and fails clearly if the target ref does not contain
`scripts/deploy_server.sh`.

The script then performs these gates in order:

1. Verifies the deployment checkout has no tracked local modifications.
2. Reads `NEXPOLY_WEB_PORT` and `NEXPOLY_POSTGRES_PORT` from explicit shell env,
   then server `.env`, then defaults `9000` and `55432`.
3. Validates the Docker Compose service contract.
4. Fetches the requested Git ref and creates a temporary worktree for tests.
5. Starts or confirms `lab-postgres`.
6. Runs backend pytest with `screen312` against local Postgres.
7. Checks required data sources and model assets.
8. Creates `backups/nexpoly-$SHA.dump` with `pg_dump -Fc`.
9. Updates the deployment checkout using `git pull --ff-only origin main` for
   the normal `main` deployment path.
10. Runs `docker compose build`.
11. Runs `docker compose run --rm postgres-init`.
12. Recreates `backend` and `nginx`.
13. Verifies strict Postgres preflight and `http://localhost:$NEXPOLY_WEB_PORT/health`.

The script never runs `--rebuild`, never prunes Docker resources, and never
targets the old `polyprop` compose project.

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
migration checksum mismatches should be treated as blockers, not ignored.

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

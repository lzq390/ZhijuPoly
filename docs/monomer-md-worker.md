# Monomer MD Worker

This worker is an independently deployed FastAPI service for the monomer density
demo path. It does not change or start the main `nexpoly` backend.

## Endpoints

- `GET /health` returns mode, database configuration, ByteFF2 root visibility,
  runtime import readiness, job root, step defaults, and active job count.
- `POST /jobs` accepts:

```json
{
  "job_id": "md-job-123",
  "smiles": "CCO",
  "canonical_smiles": "CCO",
  "steps": 1000
}
```

The request is accepted with `202` and runs asynchronously. The backend should
create the `md.monomer_md_jobs` row first, then call the worker.

## Database Contract

The worker reads `APP_POSTGRES_DSN` and updates `md.monomer_md_jobs`. It does not
print or write the DSN. The default contract matches the monomer MD migration:

- status transitions: `submitted` -> `running` -> `completed` or `failed`
- result JSON: `result_data`
- error text: `error_message`
- artifact root: `artifact_root`
- artifact JSON: `artifacts`
- progress fields: `completed_steps`, `progress_percent`, `progress_stage`, `progress_message`
- worker identity fields: `worker_id`, `worker_job_id`, `worker_version`
- timestamps: `started_at`, `finished_at`, `updated_at`

Column and table names can be overridden with `MONOMER_MD_JOB_TABLE`,
`MONOMER_MD_JOB_ID_COLUMN`, `MONOMER_MD_STATUS_COLUMN`,
`MONOMER_MD_RESULT_COLUMN`, `MONOMER_MD_ERROR_COLUMN`,
`MONOMER_MD_OUTPUT_DIR_COLUMN`, `MONOMER_MD_ARTIFACTS_COLUMN`,
`MONOMER_MD_COMPLETED_STEPS_COLUMN`, `MONOMER_MD_PROGRESS_PERCENT_COLUMN`,
`MONOMER_MD_PROGRESS_STAGE_COLUMN`, `MONOMER_MD_PROGRESS_MESSAGE_COLUMN`,
`MONOMER_MD_WORKER_ID_COLUMN`, `MONOMER_MD_WORKER_JOB_ID_COLUMN`,
`MONOMER_MD_WORKER_VERSION_COLUMN`, `MONOMER_MD_STARTED_AT_COLUMN`,
`MONOMER_MD_FINISHED_AT_COLUMN`, and `MONOMER_MD_UPDATED_AT_COLUMN`.

The worker package does not own migrations. Apply the backend migration that
creates `md.monomer_md_jobs` before starting a real worker:

```bash
cd /data/lzq/gith/nexpoly/backend
python -m app.postgres_migrations
python -m app.postgres_preflight --mode runtime --strict
```

## ByteFF2 Execution

Production mode uses the server ByteFF2 path:

```bash
BYTEFF2_ROOT=/data/lzq/gith/byteff2
MONOMER_MD_WORKER_MODE=real
```

The worker runs the bundled `byteff2_density_demo.py` adapter by default. That
adapter imports ByteFF2, builds a single-component DensityProtocol config from
the submitted monomer SMILES, monkey-patches only the NPT run length, and skips
formal long-run post-processing. It deliberately does not call the stock
`example/4_MD_simulations/run_md.py` formal path because that path is hardcoded
for long production runs and fixed example configs. The demo contract is:

- `steps=1000`
- `report_interval=10`
- output `density_demo_results.json`
- output `npt_state.csv`
- output `npt.dcd`
- mark the result as not equilibrated and not a physical density estimate

By default the worker launches:

```bash
python workers/monomer_md_worker/app/byteff2_density_demo.py
```

inside the ByteFF2 environment. The adapter first honors an optional
`BYTEFF2_DENSITY_DEMO_ENTRY`; if unset, it runs the built-in single-SMILES demo
path. If `BYTEFF2_DENSITY_DEMO_ENTRY` is set but the path does not exist, the
adapter exits with an explicit error instead of falling back. Use
`BYTEFF2_DENSITY_DEMO_ENTRY` only for a dedicated demo script that accepts
`--job-id`, `--smiles`, `--steps`, `--report-interval`, and `--output-dir`. Set
`BYTEFF2_DENSITY_DEMO_ENTRY_MODE=legacy-env` only for old reproduction scripts
that expect `RUN_ROOT` and `REPO`; that compatibility mode is not recommended
for production because legacy scripts may ignore submitted SMILES.

For a completely custom command, set `BYTEFF2_DEMO_COMMAND`, for example:

```bash
BYTEFF2_DEMO_COMMAND="python /data/lzq/gith/byteff2/example/4_MD_simulations/run_density_demo.py --job-id {job_id} --smiles {canonical_smiles} --steps {steps} --report-interval {report_interval} --output-dir {output_dir}"
```

The command is executed without a shell. Placeholders are quoted before parsing.

## Recommended Server Deployment

For real MD on `devuser@114.214.255.154`, run this worker as an independent host
service in the same Python or conda environment that can already run ByteFF2 and
OpenMM. This keeps it outside the `nexpoly` backend container while still using
the existing `/data/lzq/gith/byteff2` runtime. A read-only check on 2026-07-06
found no dedicated `run_density_demo.py` inside `/data/lzq/gith/byteff2`, so the
recommended first deployment path is the bundled single-SMILES adapter, not the
old `/data/lzq/repro_runs/.../run_density_demo.py` reproduction script.

The CI/CD deployment script restarts the host-side worker when a server-local
`/data/lzq/gith/nexpoly/.env.monomer-md-worker` file exists. Keep that file out
of Git and put only deployment-local runtime values in it:

```bash
MONOMER_MD_PYTHON=/home/devuser/miniconda3/envs/byteff2-repro/bin/python
BYTEFF2_PYTHON=/home/devuser/miniconda3/envs/byteff2-repro/bin/python
APP_POSTGRES_DSN=postgresql://polyprop:polyprop@127.0.0.1:55432/nexpoly
BYTEFF2_ROOT=/data/lzq/gith/byteff2
PYTHONPATH=/data/lzq/gith/byteff2:/data/lzq/gith/byteff2/submodules/bytemol
MONOMER_MD_JOB_ROOT=/data/lzq/monomer-md-worker-runs
MONOMER_MD_WORKER_MODE=real
MONOMER_MD_WORKER_HOST=172.17.0.1
MONOMER_MD_WORKER_HEALTH_HOST=172.17.0.1
MONOMER_MD_WORKER_PORT=18010
MONOMER_MD_HEALTH_PROBE_TIMEOUT_SECONDS=20
NEXPOLY_GPU_DEVICE=2
```

```bash
cd /data/lzq/gith/nexpoly/workers/monomer_md_worker
MONOMER_MD_PYTHON=/path/to/byteff2/python
$MONOMER_MD_PYTHON -m pip install -r requirements.txt
APP_POSTGRES_DSN="postgresql://polyprop:***@127.0.0.1:55432/nexpoly" \
BYTEFF2_ROOT=/data/lzq/gith/byteff2 \
NEXPOLY_GPU_DEVICE=${NEXPOLY_GPU_DEVICE:-2} \
MONOMER_MD_PYTHON="$MONOMER_MD_PYTHON" \
./run_host_worker.sh
```

Before enabling backend submissions, check the worker itself and then the
backend-facing status:

```bash
curl http://127.0.0.1:18010/health
NEXPOLY_PORT=${NEXPOLY_PORT:-9000}
curl "http://127.0.0.1:${NEXPOLY_PORT}/api/v1/monomer-md/status"
```

Both should show the worker as available, with `db_configured=true`,
`byteff2_root_exists=true`, and `runtime_ready=true`. After deploy, run a real
1000-step smoke with `CCO`, poll the returned job until `completed`, and confirm
the artifacts `density_demo_results.json`, `npt_state.csv`, and `npt.dcd`
exist. The result must retain warnings that the run is not equilibrated and is
not a physical density estimate.

Point the backend at `http://host.docker.internal:18010` if the backend stays in
Docker, or `http://127.0.0.1:18010` if the backend runs on the host. The worker
binds to loopback by default and is not a public service.

## Docker Deployment

Start the worker with its standalone compose file only when the image has access
to a ByteFF2 capable Python runtime. The provided slim image installs only the
worker API dependencies; it is expected to report `runtime_ready=false` in real
mode unless extended with ByteFF2/OpenMM/MDAnalysis or configured with a
`BYTEFF2_PYTHON` executable available inside the container. For the current
server, the host service mode above is the safer real-run deployment.

Standalone compose commands:

```bash
docker compose -f docker-compose.monomer-md-worker.yml build
docker compose -f docker-compose.monomer-md-worker.yml up -d
```

GPU selection defaults to the same setting used by the stack:

```bash
NEXPOLY_GPU_DEVICE=${NEXPOLY_GPU_DEVICE:-2}
```

The worker exposes port `8010` only inside its Docker network and binds the host
port to `127.0.0.1:18010`. It is not exposed on a public interface by default.
The default `APP_POSTGRES_DSN` uses `host.docker.internal:55432`, matching the
main stack's host-bound Postgres port.

Backend URL options:

- Host URL from a container: `http://host.docker.internal:18010`
- Host URL from the host: `http://127.0.0.1:18010`

For Docker-internal routing from the main backend, attach the running worker
container to the main stack network with an alias:

```bash
docker network connect --alias monomer-md-worker nexpoly_default <worker-container>
```

Then the backend can call `http://monomer-md-worker:8010`.

Health check:

```bash
curl http://127.0.0.1:18010/health
```

## Local Dry Run

Use dry-run mode when `/data/lzq/gith/byteff2` is unavailable:

```bash
MONOMER_MD_WORKER_MODE=dry-run APP_POSTGRES_DSN= uvicorn app.main:app --host 127.0.0.1 --port 8010
```

Dry-run writes `density_demo_results.json`, `npt_state.csv`, and a placeholder
`npt.dcd`. The result explicitly says it is dry-run output, not equilibrated,
and not a physical density estimate.

## Operational Notes

- The worker stores run artifacts under `MONOMER_MD_JOB_ROOT`.
- `MONOMER_MD_MAX_CONCURRENT_JOBS` defaults to `1`.
- `MONOMER_MD_MAX_STEPS` defaults to `1000`; larger requests are rejected.
- `MONOMER_MD_HEALTH_PROBE_TIMEOUT_SECONDS` defaults to `5`; use a larger
  server-local value when ByteFF2/OpenMM imports are slower on the deployment
  host.
- The worker logs job IDs and status errors, but not secret values.
- In real mode, if the job row is missing or cannot be updated to `submitted`,
  the worker rejects the request and does not start the background MD task.

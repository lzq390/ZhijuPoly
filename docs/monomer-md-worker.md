# Monomer MD Worker

The Monomer MD Worker is a host-side FastAPI process that executes ByteFF2 jobs
and stores state in `md.monomer_md_jobs`. Production uses a Unix socket shared
with the Backend container; the Worker is not a public service. Its source,
runtime hash lock, and offline wheelhouse travel in the single release bundle
and produce a per-release Worker venv. PolyTAO runs inside the Backend process
with bounded in-memory job state and is not packaged, installed, or supervised
as a host Worker; deployment drain accounts for it through the Backend all-job
status instead.

## Immutable production runtime

The frozen ByteFF2 Conda environment is a read-only base. Deployment never runs
pip against it. CI puts the Worker source, runtime hash lock, and offline
wheelhouse into `nexpoly-release-<sha>.tar.gz`. For each release the controller
creates:

```text
/data/lzq/gith/nexpoly/ops/releases/<sha>/worker-venv
```

with `--system-site-packages`, then installs every locked NexPoly distribution
into that venv using
`--no-index --require-hashes --ignore-installed --only-binary=:all:`. A
post-install check searches only the venv's own site-packages, so a matching
package inherited from the frozen base cannot satisfy the check. The user
systemd unit starts from `ops/current`, so switching or rolling back the symlink
selects the matching source and venv together.

`deploy.env` pins both the absolute base Python path and the
`NEXPOLY_WORKER_BASE_PYTHON_IDENTITY_SHA256` produced by
`release_controller.py worker-base-identity`. The recorded identity covers the
resolved executable, Python ABI, installed-distribution metadata, and Conda
metadata and is persisted in the release directory and
`ops/state/release-state.json`; there is no separate release inventory.

The production configuration is a mode-0600
`/data/lzq/gith/nexpoly/ops/config/worker.env`, based on
[`ops/config/worker.env.example`](../ops/config/worker.env.example). Important
values are:

```bash
APP_POSTGRES_DSN=postgresql://polyprop:<url-encoded-random-password>@127.0.0.1:55432/nexpoly
BYTEFF2_ROOT=/data/lzq/gith/nexpoly/ops/current-assets/byteff2
BYTEFF2_PYTHON=/home/devuser/miniconda3/envs/byteff2-repro/bin/python
PYTHONPATH=/data/lzq/gith/nexpoly/ops/current-assets/byteff2:/data/lzq/gith/nexpoly/ops/current-assets/byteff2/submodules/bytemol
MONOMER_MD_PYTHON=/data/lzq/gith/nexpoly/ops/current/worker-venv/bin/python
MONOMER_MD_JOB_ROOT=/data/lzq/gith/nexpoly/ops/state/monomer-md-worker-runs
MONOMER_MD_WORKER_UDS=/data/lzq/gith/nexpoly/ops/state/monomer-md-worker-socket/worker.sock
MONOMER_MD_WORKER_MODE=real
MONOMER_MD_MAX_ACTIVE_JOBS=1
NEXPOLY_GPU_DEVICE=2
```

`BYTEFF2_ROOT` is part of the content-addressed asset release. Its
`BYTEFF2-COMMIT` must be a full commit SHA and is copied into release state.
Do not point production at a mutable ByteFF2 checkout.

Install the tracked user unit during the first approved maintenance window:

```bash
install -m 0644 ops/systemd/nexpoly-monomer-md-worker.service \
  ~/.config/systemd/user/nexpoly-monomer-md-worker.service
systemctl --user daemon-reload
systemctl --user enable nexpoly-monomer-md-worker.service
```

Start it only after the first release has prepared `ops/current/worker-venv`.
The unit prepends the frozen ByteFF2 environment to `PATH`, so `gmx` resolves
without activating or modifying Conda. It also requires the ByteFF2 asset tree
through `ops/current-assets`, creates the socket directory with mode `0700`, and
uses a `0077` umask so the stable Worker socket is not exposed to other users.
Only the Backend container mounts that directory, read-only; `postgres-init`
never receives the Worker control socket.

## Deployment and drain behavior

Before any runtime switch the release controller:

1. enables application write drain;
2. calls the Worker `POST /drain`, which stops new Worker jobs;
3. waits for Monomer MD and every other active job class to reach zero;
4. defers the entire release after 30 minutes rather than stopping a busy
   Worker, calls `POST /resume`, and reopens the unchanged API;
5. restarts systemd from the new `ops/current` only after backup and migration;
6. requires `/health` to report `status=ok` and `runtime_ready=true`.

The backup, migration, `ops/current` switch, and Worker restart all occur only
after every active-job category is zero. A 30-minute timeout therefore changes
no database, asset pointer, code, or Worker process. There is no online
dependency installation or forced Worker kill. A failed switch restores the
previous `ops/current` and Worker. Drain remains enabled if runtime rollback is
not verified.

## API contract

- `GET /health` reports database, runtime, instance, accepting/draining state,
  ByteFF2 commit, and active job count.
- `POST /drain` is host-local and prevents new jobs while allowing accepted
  work to finish.
- `POST /resume` is host-local and restores acceptance on the unchanged Worker
  when a deployment is safely deferred before backup or runtime switch.
- `POST /jobs` accepts a job already created by Backend and returns `202`.

Jobs transition `submitted -> running -> completed|failed`. The Worker records
progress, artifact manifest, Worker identity, heartbeat, and lease timestamps.
A restarted instance marks jobs owned by an expired previous instance failed;
it does not silently duplicate them.

The density demo contract uses 300 steps with a 10-step report interval and
produces `density_demo_results.json`, `npt_state.csv`, and `npt.dcd`. Results
must retain the warning that a short demo is not equilibrated and not a physical
density estimate.

## Development

Development uses its own venv, socket, run directory, database, and asset pin.
Configure these in the ignored mode-0600 `.env.dev`:

```bash
MONOMER_MD_DEV_WORKER_PYTHON=./.venv-monomer-md-worker/bin/python
MONOMER_MD_DEV_WORKER_BASE_PYTHON=/path/to/frozen-byteff2-base/bin/python
MONOMER_MD_DEV_WORKER_BASE_PYTHON_IDENTITY_SHA256=sha256:<base-python-identity>
MONOMER_MD_DEV_WORKER_SOCKET_DIR=./.runtime/monomer-md-worker-socket
MONOMER_MD_DEV_WORKER_JOB_ROOT=./.runtime/monomer-md-worker-runs
BYTEFF2_ROOT=/data/lzq/nexpoly-assets/releases/<dev-asset-digest>/byteff2
```

First fingerprint the frozen ByteFF2 base with
`scripts/dev_server_gpu.sh worker-base-identity`, copy its `identity_sha256`
into `.env.dev`, and run `scripts/dev_server_gpu.sh worker-venv`. The bootstrap
creates a staging venv with `--system-site-packages`, installs the hash-locked
Worker distributions into that venv, verifies that none are inherited from the
base, checks the base identity before and after, and only then atomically
replaces the dev venv. An optional
`MONOMER_MD_DEV_WORKER_WHEELHOUSE` makes the installation offline.

Use `scripts/dev_server_gpu.sh worker-up|worker-status|worker-stop` for the
runtime. The script validates the asset release, venv lock, frozen base identity,
managed PID, and configured Python before accepting a healthy process. It
refuses to adopt an unknown PID/socket and refuses to stop a Worker when active
jobs cannot be proven to be zero. Never use production's release venv or frozen
Conda base directly as the dev Worker Python.

Dry-run mode remains available for API development and creates placeholder
artifacts explicitly marked as non-physical:

```bash
MONOMER_MD_WORKER_MODE=dry-run APP_POSTGRES_DSN= \
  python -m uvicorn workers.monomer_md_worker.app.main:app \
  --host 127.0.0.1 --port 8010
```

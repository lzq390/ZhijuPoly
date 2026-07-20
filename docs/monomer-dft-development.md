# Monomer DFT development and recovery

This guide covers the development-only AIMNet2 monomer DFT stack. In this
repository, “DFT” means a machine-learned potential trained against DFT
reference data; it is not a conventional self-consistent-field DFT engine.

The stack consists of a host Worker reached through a Unix socket, a CPU-only
Backend and frontend, a dedicated PostgreSQL database, and a host GPU
Broker/MPS control plane. Historical `/api/v1/dft` routes are independent and
remain unchanged.

## Hard boundaries

- Never run these controls from `/data/lzq/gith/nexpoly`, the production
  checkout. The setup, Worker, stack, smoke, and acceptance tools all reject
  that path.
- Use a clean, committed development worktree containing the exact authority
  commit under test. Do not build from an adjacent dirty NexPoly,
  `aimnetcentral`, ByteFF2, or other repository.
- All generated state belongs below the current worktree's owner-private
  `.runtime/`. The only root-level runtime file is the ignored
  `.env.monomer-dft.dev`, which must be owned by the current user and mode
  `0600`.
- Physical GPU1 is the development primary/residency device. Physical GPU3 is
  overflow only. GPUs 0 and 2 are forbidden to this stack; GPU2 is the
  production device and must not be queried through a development CUDA
  process, socket, mount, or container.
- Development GPU work is admitted by the current authority's Broker and MPS
  policy. Do not use the production Broker socket, production MPS paths, the
  production systemd unit templates, or an older authority's running control
  plane.
- Backend containers are CPU-only. They do not receive GPU devices, the
  Docker socket, production paths, or database credentials for any database
  other than the dedicated development database.
- Do not use `docker system prune`, `compose down --volumes`, or broad cleanup
  commands. Worker journals, artifacts, model caches, and PostgreSQL volumes
  are recovery data.

See [GPU resource governance](gpu-resource-governance.md) for the lease,
scope, MPS, drain, and emergency-control contracts.

## Reproducible runtime

The authoritative source and build identity is
`workers/monomer_dft_worker/aimnet-source.lock.json`. It pins the full AIMNet
commit and tree, Python 3.12, `uv`, build lock, source archive inventory,
non-editable wheel name and digest, wheel file inventory and `RECORD`, model
registry, and six model files.

Create a private standalone, full-history clone at the path expected by the
environment. It must have no shared objects, alternates, partial-clone
configuration, ignored files, or local changes:

```bash
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
install -d -m 0700 .runtime

python3 - <<'PY'
import json
import pathlib
import subprocess

root = pathlib.Path.cwd()
lock = json.loads(
    (root / "workers/monomer_dft_worker/aimnet-source.lock.json").read_text()
)
source = lock["source"]
destination = root / ".runtime/aimnet-source-clone"
if destination.exists():
    raise SystemExit(f"refusing to replace existing path: {destination}")
subprocess.run(
    ["git", "clone", "--no-local", source["repository_url"], str(destination)],
    check=True,
)
subprocess.run(
    ["git", "-C", str(destination), "checkout", "--detach", source["commit"]],
    check=True,
)
PY

env -u PYTHONPATH ./scripts/setup_monomer_dft_env.sh --check-aimnet-source
env -u PYTHONPATH ./scripts/setup_monomer_dft_env.sh
```

Setup refuses a dirty authority tree and builds only from `git archive` of the
locked AIMNet commit. It recreates the isolated venv and wheelhouse from
hashed locks, verifies the installed wheel and all model bytes, and runs a
CUDA-blind provenance preflight. It does not modify the source clone and does
not silently download missing models.

Re-running setup is safe only while the Worker is stopped. Never repair the
venv with an ad-hoc `pip install`; update the input locks, regenerated hash
locks, source/model manifest, and release tests as one reviewed change.

Useful CPU-only checks are:

```bash
env -u PYTHONPATH ./scripts/setup_monomer_dft_env.sh --check-repository
env -u PYTHONPATH ./scripts/setup_monomer_dft_env.sh --check-aimnet-source
env -u PYTHONPATH -u CUDA_VISIBLE_DEVICES \
  .runtime/venvs/monomer-dft-worker/bin/python -I \
  scripts/preflight_monomer_dft_env.py
python3 scripts/validate_monomer_dft_release_contract.py
```

Do not add `PYTHONPATH` or expose a CUDA device to Broker-enabled preflight.
A standalone CUDA smoke is a separately authorized diagnostic and is not a
release acceptance result.

## Broker, MPS, and Worker

Before starting the Worker, establish a fresh development Broker/MPS control
plane from the exact committed authority being tested. Its socket, state,
external-reservation copy, and MPS directories must all be below
`.runtime/gpu-resource`; the release acceptance runner owns this lifecycle
when producing formal evidence. If those identities cannot be proved, stop:
never fall back to a production socket, direct CUDA, or a pre-existing
unknown Broker.

The normal Worker controls are:

```bash
./scripts/monomer_dft_worker_ctl.sh start
./scripts/monomer_dft_worker_ctl.sh status
./scripts/monomer_dft_worker_ctl.sh health
./scripts/monomer_dft_worker_ctl.sh stop
```

`start` performs the complete CPU/provenance preflight before it creates a
supervisor. The HTTP supervisor remains CUDA-blind. A resident executor is
admitted on GPU1 and sees only its leased UUID as `cuda:0`; a transient
single-model child may use GPU3 only after an exact overflow lease.

The controller owns only the current worktree's PID/start-ticks record, log,
private home/temp directories, Worker socket, jobs, and GPU runtime. It
rejects PID reuse and foreign sockets. Exit status 70 has a bounded restart
budget; other exits fail closed. Inspect
`.runtime/monomer-dft-worker.log` before restarting a stopped or stale
Worker.

For a quick post-start protocol check, run:

```bash
env -u PYTHONPATH -u CUDA_VISIBLE_DEVICES \
  .runtime/venvs/monomer-dft-worker/bin/python \
  scripts/smoke_monomer_dft_env.py
```

In Broker mode this uses the registered Worker/UDS path. It is not a
substitute for release acceptance, which additionally proves the Backend,
database, cancellation, durable journals, artifacts, overflow fencing, and
production/GPU2 non-interference.

## Dedicated Compose stack

The full-stack controller is intentionally fail-closed. It requires a clean
committed tree based on the current `origin/main`, the canonical CI and
release controls, the exact 0012 contract migration, the 0013 expand
migration, and a passing release-contract validator.

For routine development use the default private project and only the scoped
controller:

```bash
./scripts/monomer_dft_dev_stack.sh config
./scripts/monomer_dft_dev_stack.sh start
./scripts/monomer_dft_dev_stack.sh status
./scripts/monomer_dft_dev_stack.sh logs
./scripts/monomer_dft_dev_stack.sh stop
```

Formal acceptance does not reuse that project and must not be configured by
editing the private env file. The acceptance runner injects a one-run
`nexpoly_dft_fresh_<id>` namespace after loading the fixed development
configuration, then proves that its network, PostgreSQL volume, containers,
Worker, Broker and MPS state were all freshly created and collected.

The fixed loopback endpoints are:

- Frontend: `http://127.0.0.1:25173/monomer-dft`
- Backend status:
  `http://127.0.0.1:28000/api/v1/monomer-dft/status`
- PostgreSQL: `127.0.0.1:25532`

Startup verifies the Worker before starting the dedicated database,
migrations, Backend, and frontend. Shutdown drains a fenced Worker instance,
waits for active work, stops only that Compose project, and retains the
database volume, queue journals, artifacts, models, and caches. If an
instance changes while draining, the controller drains and verifies the
replacement instead of acting on stale state.

## Schema and API behavior

Migration `0013_monomer_dft_jobs` immediately follows
`0012_drop_polytao_jobs`. Before 0013 is applied, `status` and `capabilities`
remain safe and report `schema_ready: false`; job/history/deep-link routes
return a stable `503` with code `schema_not_ready` before issuing job-table
SQL. The frontend loads history only while `schema_ready` is true and clears
in-flight/old state if readiness falls back to false.

The public routes are:

```text
GET    /api/v1/monomer-dft/status
GET    /api/v1/monomer-dft/capabilities
GET    /api/v1/monomer-dft/jobs
POST   /api/v1/monomer-dft/jobs
GET    /api/v1/monomer-dft/jobs/{job_id}
POST   /api/v1/monomer-dft/jobs/{job_id}/cancel
GET    /api/v1/monomer-dft/jobs/{job_id}/artifacts/{artifact_id}
GET    /api/v1/monomer-dft/jobs/{job_id}/bundle
DELETE /api/v1/monomer-dft/jobs/{job_id}/artifacts
```

Every submission requires an 8–128 character URL-safe `Idempotency-Key`.
Reusing a key with the same normalized request replays the original job;
reusing it for a different request returns a conflict. For example:

```bash
curl --fail-with-body \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: dev-water-0001' \
  --data '{
    "input": {"smiles": "O", "multiplicity": 1},
    "model": "aimnet2",
    "conformer": {"seed": 1, "max_iterations": 500},
    "calculation_type": "single_point",
    "single_point": {
      "properties": ["energy", "charges", "forces", "hessian"]
    }
  }' \
  http://127.0.0.1:28000/api/v1/monomer-dft/jobs
```

Backend is the only PostgreSQL writer. It reconciles database sequence and
state with durable Worker journals over UDS. Downloads are fully spooled and
verified against persisted size/SHA-256 manifests before response headers are
sent; bundles are validated and deterministically repacked. Artifact deletion
is the recoverable `available → delete_requested → deleted` transition.

## Journal recovery

Worker state is under `.runtime/monomer-dft-worker-runs`. Do not edit a
journal, infer queue order, or delete an incomplete job by hand. Stop the
Worker, then inspect legacy journals without loading CUDA:

```bash
PY=.runtime/venvs/monomer-dft-worker/bin/python
"$PY" -m workers.monomer_dft_worker.app.journal_upgrade \
  --job-root .runtime/monomer-dft-worker-runs \
  --check
```

V1 active/queued journals without an authoritative database
`enqueue_sequence` fail closed. If an upgrade is required, export and review
the exact sequence map from the dedicated development database, choose a new
owner-private backup directory, and apply while the Worker remains stopped:

```bash
"$PY" -m workers.monomer_dft_worker.app.journal_upgrade \
  --job-root .runtime/monomer-dft-worker-runs \
  --apply \
  --sequence-map .runtime/reviewed-journal-sequence-map.json \
  --backup-dir ".runtime/journal-backups/$(date -u +%Y%m%dT%H%M%SZ)"
```

The tool takes the job-root lock, backs up and hashes the complete batch,
re-reads every source byte before replacement, and writes private atomic
updates. Run `--check` again and require `v1_count=0` and
`changes_required=false` before restarting.

## Release acceptance and shutdown

Formal GPU acceptance has two deliberately different stages after B is
frozen. Before merge, run `candidate-tree` against the clean candidate tree
to exercise the real science and control plane; its report is provisional and
can never satisfy production readiness. After squash merge, wait for the main
release to publish Backend/Web images labelled with the final F SHA, pull
their exact GHCR digest references, and rerun `final-main`. That second run
uses Compose `--no-build`, verifies the registry index and linux/amd64
platform digest, local image ID, RepoDigest, OCI labels, and running container
image ID, and is the only run allowed to produce final acceptance evidence.

Use `scripts/run_monomer_dft_gpu_acceptance.py --help` from the isolated
Python environment. Both stages require the full F/B commit and tree IDs;
`final-main` additionally requires an owner-private OCI evidence JSON file.
Do not call the runner's internal child mode directly or relabel a
`candidate-tree` report after publication.

The runner must own a fresh exact-F Broker, MPS, Worker, and fresh Compose
project for the entire evidence window. A passing sealed report proves:

- GPU1 Broker/UDS/Backend energy, forces, Hessian, journal, cancellation, and
  artifact/bundle paths;
- either an exact GPU3 overflow execution or a causally proven external fence;
- exact source, wheel, model, runtime, image, F, and B provenance;
- no development mutation of GPU2 or production sockets, mounts, containers,
  database, repository, or asset pointer. The harness performs only the
  explicit read-only GPU2 samples, Docker inventory needed to fence GPU3, and
  production-repository content CAS described by the acceptance contract;
- complete drain and removal of runner-owned processes, scopes, leases,
  sockets, and containers before the final production/GPU2 comparison.

After ordinary development, call the scoped stack `stop`, confirm the Worker
is stopped or intentionally drained, and inspect its project with
`docker compose ... ps --all`. Remove only explicitly identified disposable
containers. Retain private evidence and volumes until the production upgrade
and rollback acceptance has completed.

# Monomer MD Worker

The Monomer MD Worker is a host process reached by the Backend through a private
Unix socket. The Backend owns public API and database orchestration; the Worker
executes the reviewed ByteFF2/OpenMM protocols and records progress in the
platform job tables.

## Production layout

Worker source comes from the verified production checkout:

```text
/data/lzq/gith/nexpoly/workers/monomer_md_worker
```

All mutable or host-specific state is external:

```text
/data/lzq/gith/nexpoly-runtime/
├── bin/
│   ├── control_runtime_selector.py
│   ├── nexpoly-pull-deploy
│   └── nexpoly-pull-contract-0012
├── control-releases/<content-addressed-release>/
│   ├── monomer_worker_env.py
│   ├── monomer_md_worker_launcher.py
│   └── worker_slot_runtime.py
├── config/worker.env
├── state/
│   ├── current-assets/byteff2/
│   ├── monomer-md-active-slot.json
│   ├── worker-slots/
│   │   ├── md-a.json
│   │   └── md-b.json
│   ├── monomer-md-worker-socket/worker.sock
│   └── monomer-md-worker-runs/
└── worker-venvs/
    ├── md-a/venv/
    └── md-b/venv/
```

There is no active-venv symlink. `runtime/bin` must contain exactly the immutable
selector and two stable wrappers shown above. The selector verifies the active
control authority and content-addressed release before loading its environment
helper and launcher. The launcher reads the active-slot and slot records,
validates their owner/mode/schema/source identity, selects the recorded final
venv path, and executes the Worker from the live checkout.

The systemd entry uses only stable external helpers:

```text
/usr/bin/python3 -I -B \
  /data/lzq/gith/nexpoly-runtime/bin/control_runtime_selector.py \
  run monomer-md
```

The launcher prepares the private socket directory and finally `exec`s Uvicorn
with the Python interpreter selected from the active slot record. Shell parsing
of JSON and environment files is forbidden.

## Environment contract

Install `ops/config/worker.env.example` as mode `0600` at:

```text
/data/lzq/gith/nexpoly-runtime/config/worker.env
```

The production values bind these paths:

```dotenv
BYTEFF2_ROOT=/data/lzq/gith/nexpoly-runtime/state/current-assets/byteff2
PYTHONPATH=/data/lzq/gith/nexpoly:/data/lzq/gith/nexpoly-runtime/state/current-assets/byteff2:/data/lzq/gith/nexpoly-runtime/state/current-assets/byteff2/submodules/bytemol
MONOMER_MD_JOB_ROOT=/data/lzq/gith/nexpoly-runtime/state/monomer-md-worker-runs
MONOMER_MD_WORKER_UDS=/data/lzq/gith/nexpoly-runtime/state/monomer-md-worker-socket/worker.sock
MONOMER_MD_GPU_BROKER_SOCKET_PATH=/data/lzq/gith/nexpoly-runtime/state/gpu-resource/broker.sock
MONOMER_MD_GPU_MPS_PIPE_ROOT=/data/lzq/gith/nexpoly-runtime/state/gpu-resource
```

`MONOMER_MD_PYTHON` must not be configured or inherited in production. Both the
controller and launcher reject it. The interpreter is selected only from the
sealed active-slot and slot records. The strict environment helper accepts
only reviewed literal keys and scrubs inherited Python, loader, shell and CUDA
overrides before starting the launcher.

The frozen ByteFF2 base Python, Conda executable, GROMACS executable and OpenMM
native root are separately pinned. The managed asset pointer must resolve to a
read-only schema-v2 asset release whose ByteFF2 commit and audited runtime files
match the deployment record.

## A/B environment preparation

The pull-deployment controller prepares only the inactive slot while the active
Worker continues serving:

1. Read the MD requirements lock from the requested Git commit.
2. Verify every requirement hash and populate the private wheel cache.
3. Create the venv directly at `md-a/venv` or `md-b/venv`; do not relocate it.
4. Install offline with hashes, binary-only artifacts and an isolated package
   environment.
5. Verify the frozen base Python and toolchain before and after installation.
6. Record the complete distribution inventory, venv prefix, lock digest and
   target source SHA in the slot JSON.
7. Fsync the slot and record without changing the active-slot record.

The deployment switches the active record only after drain, backup, source
fast-forward and candidate preflight. The previous slot remains intact for
rollback. The next preparation may reuse that now-inactive slot only after the
current deployment is durably successful.

## Runtime identity and readiness

Health reports include:

- source SHA and tree of the checked-out Worker code;
- resolved source root, venv prefix and Python executable;
- Worker instance ID and active-slot identity;
- ByteFF2 commit and runtime asset verification;
- OpenMM, CUDA, Transport plugin and GPU readiness;
- drain, recovery and active-job state.

Startup fails closed if the checkout, deployment record, active-slot record,
venv prefix, lock inventory or asset identity differs. In production, `ready`
requires real runtime preflight; a dry-run or degraded Transport state cannot
open admission.

## Deployment, drain and rollback

Before changing the source checkout, the controller:

1. places the Worker in drain;
2. records its instance ID and active jobs;
3. waits for active work to finish without killing it;
4. stops the Worker before Git changes the working tree.

After switching source and active slot, systemd starts a new instance. The
controller requires the new source and slot identity, strict runtime health and
an authoritative 300-step smoke before Backend admission resumes.

If the candidate fails, the controller stops it, selects the previous slot,
restores the previous source and assets, starts a new old-runtime instance and
repeats the health and smoke gates. Drain remains enabled if rollback cannot be
proved.

The deployment marker guard prevents a Worker from starting after a crashed
deployment unless the deployment lock is actively held by recovery.

## API contract

The Worker listens only on its Unix socket. Internal endpoints provide:

- health, capability and protocol readiness;
- drain and resume;
- idempotent submit, status and cancellation;
- artifact access and cleanup.

The public browser never connects to the Worker. Backend remains the only
public interface and the only writer of platform orchestration state.

Maximum production concurrency is one. Formal protocols use the reviewed
300-step contract. The Worker tracks its process group and any governed GPU
lease so cancellation, timeout and shutdown cannot leave child processes or GPU
capacity behind.

## Development

Development keeps its source, venv, socket, jobs and cache under the development
worktree's ignored runtime directory. Use the commands documented by
`scripts/dev_server_gpu.sh`; do not point development at the production A/B
slots, socket, asset pointer or GPU2.

Development defaults to GPU1 and may use GPU3 only through the governed
overflow policy. Production GPU2 is never a development fallback.

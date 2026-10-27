# Opt-in MD Backend configuration

These overlays configure the existing development Backend only. They do not start
a Host Worker, initialize a database, generate credentials or enable submission
by default. The ordinary user-isolation development overlays and a separately
prepared private Host Worker/socket/job root remain prerequisites.

`docker-compose.polymer-md-dev.yml` requires explicit private socket/job paths
and start-authorization input. Both host binds are read only and refuse automatic
host-directory creation. Preflight keeps the exact polymer MD 0023 schema and
service-context checks. Submission defaults to false.

The separate registry overlay requires an explicit existing registry path and
its UID/GID, mounts the registry read only with `create_host_path: false`, and
does not turn on MD or submission. It belongs only in a reviewed drained setup.
No registry contents or private values are versioned here.

Validation renders Compose configuration with synthetic environment inputs;
it contacts no Docker daemon and starts or stops no services. Actual deployment,
database, authorization-token and scientific acceptance are not established by
these configuration tests.

## Explicit main development composition

The ordinary isolated development launcher can select these overlays with
`NEXPOLY_DEV_POLYMER_MD_ENABLED=true`, `NEXPOLY_DEV_LITERATURE_ENABLED=true`
and, optionally, `NEXPOLY_DEV_POLYMER_MD_REGISTRY_ENABLED=true`. All selectors
accept only `true` or `false`; defaults remain false. Registry selection requires
MD selection. Research selection rejects native cohorts so their frozen CPU
configuration and existing recovery contracts remain unchanged.
The launcher requires registry UID/GID `1001:1001`, matching the governed Backend
and operator peer-identity contract; a registry cannot override the GPU user.

Set `NEXPOLY_DEV_RESEARCH_ENV_FILE` to an absolute, unlinked, owner-private regular
0600 file with one link and at most 64 KiB. It contains literal `KEY=value` lines,
optional matching outer quotes, blank lines and whole-line comments. Values are
not shell-expanded or executed; inline comments and escapes are literal. Unknown
and duplicate keys are rejected. Inherited research values cannot fill missing
private-file fields. Only these keys are accepted:

- `POLYMER_MD_SOCKET_DIR`, `POLYMER_MD_JOB_ROOT`,
  `POLYMER_MD_START_AUTHORIZATION_TOKEN`, `POLYMER_MD_SUBMIT_ENABLED`.
- Optional registry: `POLYMER_MD_ACCEPTANCE_HOST_ROOT`,
  `POLYMER_MD_ACCEPTANCE_UID`, `POLYMER_MD_ACCEPTANCE_GID`.
- Online literature assets: `LITERATURE_MINIO_ENDPOINT`,
  `LITERATURE_MINIO_ACCESS_KEY`, `LITERATURE_MINIO_SECRET_KEY`,
  `LITERATURE_MINIO_SECURE`.

Do not copy a standalone deployment's DSNs, identities or old Worker socket into
this file. The existing isolated main database configuration remains authoritative.
Prepare a compatible Worker, source/runtime manifest, private job/socket roots
and authorization identity separately. Submission defaults to false and requires
an explicit setting after those prerequisites have been reviewed. Literature uses
the existing main API database and an explicitly configured online object store;
its overlay provides `host.docker.internal:host-gateway` when a host-local service
must be reached from the Backend. It does not import or publish a generation.

Both CPU and governed GPU Compose commands include the selected MD, registry,
literature and common research overlays. The latter layers follow the GPU session
overlay. MD alone selects `polymer-md-0023`; literature selects
`literature-0024`, including when both features are enabled. All selected overlay
files enter the Backend image's build-configuration fingerprint. Effective private
values enter the existing runtime configuration hash, with an additional opaque
private-file digest label. Runtime drift checks compare research environment,
schema boundary, readonly mounts and registry UID/GID without printing private
values. Research deployment retains `USAGE_ENTITLEMENTS_ENABLED=false` and enables
the persistent deployment-drain middleware.

The operator freezes the three selectors, private-file path and opaque digest in
its identity and forwards only that metadata to recovery. It reloads private
values from the owner-private file in the child launcher. A changed selection or
file digest requires a new operator; an old operator cannot silently recover a
different composition. Credentials are not passed as systemd environment inputs.

## Maintenance startup and explicit restoration

Before the maintenance window, use `scripts/dev_server_gpu.sh build-backend`
with the intended selectors and private file. It requires a clean committed
candidate, builds through the default Docker builder and verifies image
source/tree, dependency-lock and selected configuration fingerprints. It starts
or stops no services and runs no migrations. Updating the selected image tag does
not replace a running container's image ID.

First use the normal owner-fenced deployment control to enable persistent drain,
finish the approved drain/backup/migration procedure, and stop the existing
frontend and batch Worker. Teardown of a running GPU session continues through
`gpu-session-maintenance-stop`; it is not replaced by process-group termination
or direct Broker/MPS manipulation.

`scripts/dev_server_gpu.sh maintenance-up-prebuilt` requires the explicit research
selection, existing isolated 9001 direct-start prerequisites, prebuilt source/lock/
configuration image identity, exact selected schema preflight and an already
enabled persistent database drain. It refuses running frontend/batch services.
It starts only the CPU Backend and necessary operator, verifies drift and checks
drain again. It does not initialize or migrate a database, start a PMD Worker,
start the frontend/batch, activate a GPU session or alter drain ownership.

If GPU activation is required, invoke the existing `gpu-session-up` explicitly
with its normal execution opt-in. Controller, Worker, descriptor, candidate,
session and source fencing remain in force. The persistent public API drain stays
enabled while startup and non-submitting acceptance checks are completed.

`scripts/dev_server_gpu.sh maintenance-resume` requires drain to remain enabled
and verifies the normal CPU or exact ready GPU session before starting only
frontend/batch. It does not recreate the Backend or clear drain. After readiness
is established, use the existing owner-fenced deployment control as the separate
final admission-restoration operation. A failure leaves the persistent guard
enabled. These entry points do not authorize scientific submissions or constitute
scientific acceptance evidence.

## Separate development PMD Worker

Use `WORKER_PYTHON scripts/polymer_md_worker_dev.py
{run-worker|check|drain|resume} --config /absolute/runtime/worker.json` for a newly
prepared Worker. The JSON has exactly `schema_version: 1`, `runtime_root`,
`release_root`, `runtime_manifest`, `runner_python`, `worker_python`, `job_root`,
`socket_path`, `start_authorization_url` and `start_authorization_token_file`.
Paths must be absolute without symlinks. The external runtime root must be
separate from source, release and the existing standalone `polymer-md-dev`
deployment. Config, md10 manifest and token belong inside that root; jobs use
`runtime_root/jobs` and the socket uses `runtime_root/socket/worker.sock`.
Runtime directories are owner-private 0700 and config/manifest/token files 0600.

The verified release root is 0700, named by its source digest, and contains owned
files without write bits or Python bytecode/cache files. `runner_python` must
match the md10 sealed manifest and its verified files; an external verified
read-only regular binary may be reused. `worker_python` can use a separate venv.
Provisioning and the systemd unit remain external to this wrapper. Start it with
CPU 16, memory 32 GiB and zero GPUs; the existing manager verifies the actual
scope. `run-worker` uses a clean environment, one Uvicorn worker, socket mode
0600 and initial drain. Explicit resume history persists across restarts.

`check` returns a redacted status: a healthy drained Worker exits zero with
readiness false; invalid runtime state exits one. `drain` closes admission even
when the runtime is not ready. `resume` requires a verified runtime, resource
scope, ready analysis and the same connected socket instance. This is deployment
verification; these commands provide no scientific acceptance evidence.

# NexPoly deployment

GPU Broker/MPS accounting, fixed budgets, and the separately authorized
activation sequence are documented in
[GPU resource governance](gpu-resource-governance.md). The units ship disabled;
normal release automation must not change production compute mode.

NexPoly uses one CI/CD workflow for application code, the host Monomer-MD
Worker, and the immutable model/data asset pin.  Production remains rooted at
`/data/lzq/gith/nexpoly`, but a deployment never builds an image or installs a
package from the network on that host.

## Environment boundaries

- Development: `/data/lzq/gith/nexpoly-dev`, Compose project `nexpoly_dev`.
- Production: `/data/lzq/gith/nexpoly`, Compose project `nexpoly`.
- Immutable assets: `/data/lzq/nexpoly-assets/releases/<manifest-sha256>`.
- Backend and Web releases: private GHCR images referenced by `@sha256`.
- Monomer-MD Worker: a per-release venv layered on the frozen
  `/home/devuser/miniconda3/envs/byteff2-repro` environment.

Production `worker.env` is not a shell file and is not a systemd
`EnvironmentFile`. A stable owner-only Python wrapper accepts only literal,
allowlisted `KEY=VALUE` lines and supplies the same child environment to
systemd and candidate preflight. OpenMM loader variables are derived from
`BYTEFF2_OPENMM_DIR`; `OPENMM_DIR`, `OPENMM_PLUGIN_DIR`, `LD_LIBRARY_PATH`, and
the deploy-only Transport gate are forbidden in `worker.env`.

The two PostgreSQL volumes remain independent.  A deployment must never copy
models or data directly from the development checkout into production.  New
assets are first sealed in the content-addressed asset store and then selected
by the tracked `release-input.json` digest.

## The single workflow

`.github/workflows/ci.yml` is the only delivery workflow.

- Pull requests run secret-free Backend, Frontend, Worker, script, Compose and
  image-build checks.  The stable required status is `ci-gate`.
- A push to protected `main` repeats the immutable-SHA gate, builds and pushes
  Backend/Web images, tests their exact digests, and creates one release bundle.
  Production deployment is forced off during the migration-epoch bridge.
- `workflow_dispatch` has no SHA input and is accepted only for the one-time
  bootstrap of the current `main` commit.

All Actions are pinned to full commits.  PR jobs have no secrets.  The deploy
step alone receives the `nexpoly-production` SSH secrets and requires a pinned
`known_hosts`; host-key discovery is forbidden.  GitHub concurrency and the
server `deploy.lock` prevent overlapping production changes.

There is no `workflow_run` hand-off, control/Worker OCI image, Actions release
inventory, standalone data workflow, or manual arbitrary-SHA redeploy path.

## Release input and bundle

`release-input.json` has this public contract:

```json
{
  "schema_version": 1,
  "asset_manifest_digest": "sha256:<64 lowercase hex>",
  "datasets_on_asset_change": ["governance", "core"]
}
```

Dataset names must be explicit, unique and supported by
`python -m app.import_postgres`; `all` and `none` are forbidden.  The repository
uses the complete explicit set so an asset change can rebuild governed data
without relying on an implicit default.  When the asset digest is unchanged,
the deployment runs no import at all.

The main release job creates:

- a manifest containing the source SHA, CI run, Backend/Web digests, the single
  release-bundle digest, asset digest, datasets and migrations;
- a release bundle containing the exact Compose/control files, Worker source,
  hash lock and offline wheelhouse.

The bundle is hashed and copied over the pinned SSH connection.  The server
validates the manifest, file type, size, hash and archive paths before using any
content.  Application containers are still sourced only from GHCR digest
references.

## Production state

Only these mutable control records are required:

```text
ops/current -> releases/<sha>
ops/current-assets -> /data/lzq/nexpoly-assets/releases/<digest>
ops/state/release-state.json
ops/state/deploy-in-progress.json
ops/state/deploy.lock
```

`release-state.json` records the current and previous release identities,
Backend/Web digests, the single release-bundle digest, Worker base/toolchain
identity, asset digest, migrations and the verified pre-deploy backup. It is the
only persistent source of the active asset digest. `deploy-in-progress.json` is
a small crash marker, not a journal; an unfinished marker is reconciled before
another deployment or Worker start.

## Automatic deployment

The remote release flow invokes two fail-stop controller commands under the
same non-blocking server lock contract. `provision-release --apply` completes
first; only then may `deploy --apply` run.

The controller performs the following:

1. Verify the production root, current `main` SHA, manifest, images, bundle,
   asset manifest and free space.
2. In the explicit provisioning command, unpack the candidate and build the
   per-SHA Worker venv from the offline wheelhouse. Verify the frozen
   Python/Conda identity, `gmx`, ByteFF2 commit,
   strict literal Worker configuration, and stable systemd wrapper. For every
   release carrying the Worker payload, before any drain the resolved candidate
   asset must use manifest schema v2 and contain all three fixed ByteFF2 runtime
   assets: the tracked `bond_length_ref.csv` table plus the audited upstream
   trainer YAML and model overlay. The ByteFF2 Git source and approved v1.0.0
   commit, plus the overlay source, revision, source paths, runtime target
   paths, sizes, and SHA-256 values,
   are exact contract fields. This candidate-only check
   intentionally does not reject a broken legacy schema-v1 current asset that
   the candidate repairs. Seal a mode-0600 READY record over the manifest,
   bundle, lock, wheelhouse, payload, venv inventory, final prefix and frozen
   runtime identities. Provisioning scratch stays under `ops/state`.
3. Start `deploy` by recomputing the READY evidence. This path performs no
   venv creation, pip invocation, or candidate-Python import; venv distribution
   metadata is checked statically before the governed runtime preflight. When
   the deploy-only Transport gate is
   enabled, drain the old Worker and
   wait for its active jobs to reach zero, then run the candidate runtime
   preflight before pulling images or changing PostgreSQL/runtime state. Direct
   mode requires an idle selected GPU; Broker mode obtains a temporary fenced
   execution lease and never runs an ungoverned CUDA probe. The outer controller
   contains the candidate with a temporary child-subreaper scope, exact pidfds,
   repeated freeze/adoption scans, and a bounded stdout pipe, so a quick
   double-fork/`setsid()` cannot escape timeout cleanup. A failure resumes the
   unchanged Worker.
4. Pull the two image digests. Drain Backend (and the Worker if it was not
   already drained), then wait up to 30 minutes for persistent jobs,
   in-memory jobs, GPU queues, API writes and Worker jobs to reach zero.
5. Create a custom-format PostgreSQL dump, SHA-256 sidecar, and successfully run
   `pg_restore --list`.
6. Apply only expand migrations. A contract remains pending only while no later
   epoch depends on its exact checksum; a missing/mismatched dependency rejects
   the deployment before later-epoch DDL.
7. If the asset digest changed, use the candidate asset root to run the explicit
   complete dataset set with `--rebuild`.  Otherwise skip imports.  Refresh the
   PostgreSQL analytics snapshot for the target SHA.
8. Atomically switch `current-assets` and `current`, start the target Worker and
   digest-pinned Backend/Web, and keep public ingress isolated.
9. Run strict PostgreSQL/GPU checks, Worker health, Backend status and protocol
   catalog gates, versioned Web asset health, PolyTAO smoke, and the 300-step
   Monomer-MD/ByteFF2/GROMACS smoke. Strict Transport readiness requires
   `supported=true`, `runtime_ready=true`, and `runtime_error=null` everywhere.
10. Commit `release-state.json`, remove the crash marker, and resume ingress.

If draining times out, the old release is resumed without backup, migration or
switch.  The Worker is never force-killed while it reports an active job.

## Rollback

- For a code-only failure, restore the previous Backend/Web images, Worker
  release and pointers.  Compatible expand migrations remain.  Regenerate the
  previous SHA analytics snapshot with the previous image and run all previous
  release preflights before resuming.
- If an asset pointer changed or a dataset import began, first restore the
  verified pre-change dump, then restore the old asset and all old runtimes.
- If rollback cannot be verified, keep drain/ingress isolation in place and
  fail closed.

All health helpers receive the release they are checking explicitly.  A failed
target must never be used to run the previous release's PostgreSQL or GPU
preflight.

## One-time bootstrap

Bootstrap is a reviewed maintenance-window operation for current `main` only.
It creates private `ops` directories, stores read-only GHCR credentials, binds
PostgreSQL to `127.0.0.1:55432`, rotates the `polyprop` password in App and
Worker configuration, installs the `ops/current` Worker systemd unit, and
records the legacy Backend/Web image IDs and Worker unit as one canonical
rollback identity. The audited rollback helper must restore and health-check
all three runtimes plus ingress; merely restarting nginx is rejected. See
[`release-controller.md`](release-controller.md) for the capture, GHCR pull
credential, rehearsal, and evidence procedure.

The initial asset release is
`sha256:ad19a4f1cb954b3ee6999b7157c798fd887ecd3fd7ae12e40ac20a97637575e2`.
Its file content matches the current production model/database/backend-data
trees, so bootstrap treats it as the baseline pin and does not rebuild data.
It applies migrations 0009-0011 and leaves 0012 pending. Automatic deployment
remains disabled during the epoch bridge. The checksum-pinned 0012 maintenance
operation and the later automatic-deployment re-enable are separately reviewed
changes.

## GitHub configuration

- Protect `main`: require a pull request, require the branch to be up to date,
  require `ci-gate`, and forbid force-push and deletion.
- The current single-maintainer personal repository does not require an
  approval count or merge queue.
- Restrict `nexpoly-production` to protected `main`, with no reviewer so the
  explicitly dispatched bridge bootstrap does not require a second redundant
  approval. Push-triggered production deployment remains disabled in CI.
- Store `NEXPOLY_SSH_HOST`, `NEXPOLY_SSH_USER`,
  `NEXPOLY_SSH_PRIVATE_KEY`, and the independently verified
  `NEXPOLY_SSH_KNOWN_HOSTS` as environment secrets.  Store the SSH port as an
  environment variable.  Remove the old repository-scoped SSH secrets only
  after a successful bootstrap.

Off-host backups, blue/green deployment, full ByteFF2 containerization,
scheduled restore drills and automatic retention are deliberately outside this
minimal delivery path.

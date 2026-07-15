# NexPoly single-pipeline release runbook

Production is controlled from `/data/lzq/gith/nexpoly`. Development remains in
`/data/lzq/gith/nexpoly-dev`; a release never copies files from that mutable
checkout into production.

## One workflow and one release identity

`.github/workflows/ci.yml` is the only active workflow:

1. Pull requests run the complete Backend PostgreSQL shards, Frontend Vitest and
   build, Monomer-MD Worker tests, deployment-script tests, policy checks, and
   Backend/Web image builds without publishing.
2. A main push repeats those tests. After the stable `ci-gate` succeeds, the
   same immutable 40-character SHA builds and pushes Backend and Web to GHCR.
3. CI smokes the resulting `image@sha256:...` references against a disposable
   PostgreSQL instance. It then creates one release bundle containing the
   tested control tree, Monomer-MD Worker source and lock, and an offline
   wheelhouse.
4. CI checks that the SHA is still current main. During the migration-epoch
   bridge, push-triggered SSH deployment is forced off regardless of the legacy
   `NEXPOLY_AUTODEPLOY_ENABLED` variable.
5. `workflow_dispatch` has no SHA input. It accepts only `operation=bootstrap`,
   must be dispatched from main, and still repeats the current-main check.

There is no `workflow_run`, Actions artifact inventory, control/Worker OCI
artifact, independent data workflow, arbitrary redeploy, or branch/tag/short-SHA
deployment path. Backend and Web are the only GHCR images; production uses their
manifest digests, never the `sha-<SHA>` index tags.

Configure these GitHub environment secrets only on `nexpoly-production`:

- `NEXPOLY_SSH_HOST`
- `NEXPOLY_SSH_USER`
- `NEXPOLY_SSH_PRIVATE_KEY`
- `NEXPOLY_SSH_KNOWN_HOSTS`

`NEXPOLY_SSH_KNOWN_HOSTS` is mandatory and must be reviewed out of band. The
transport has no `ssh-keyscan` fallback. `NEXPOLY_SSH_PORT` is an optional
environment variable. Keep `NEXPOLY_AUTODEPLOY_ENABLED=false`; re-enabling push
deployment is a separate reviewed bridge-removal change.

## Release input, bundle, and manifest

[`release-input.json`](../release-input.json) is the reviewed data/asset input:

```json
{
  "schema_version": 1,
  "asset_manifest_digest": "sha256:<64 lowercase hex>",
  "datasets_on_asset_change": [
    "governance", "core", "knowledge", "online", "pi",
    "dft", "experimental", "lab", "property_filter"
  ]
}
```

Dataset names are explicit, ordered, unique, and cannot contain `all` or
`none`. Changing this file in main authorizes that exact asset release and the
listed rebuild. When its asset digest is unchanged, the controller performs no
data import.

CI creates `nexpoly-release-<SHA>.tar.gz` directly from `git archive <SHA>`,
excluding model/database payloads, then adds `wheelhouse/`. The bundle contains
`docker-compose.yml`, `docker-compose.prod.yml`, deployment scripts,
`workers/monomer_md_worker`, its hash lock, and the downloaded wheels. The
manifest is detached so it can record the bundle's size and SHA-256:

```bash
python3 scripts/release_controller.py build-manifest \
  --sha "$RELEASE_SHA" \
  --ci-run-id "$GITHUB_RUN_ID" \
  --backend-image "$BACKEND_IMAGE_DIGEST" \
  --web-image "$WEB_IMAGE_DIGEST" \
  --release-bundle "nexpoly-release-${RELEASE_SHA}.tar.gz" \
  --release-input release-input.json \
  --migration-manifest backend/migrations/postgres/manifest.json \
  --output release-manifest.json

python3 scripts/release_controller.py verify-manifest \
  --manifest release-manifest.json --sha "$RELEASE_SHA"
```

`scripts/ci/remote_release.sh` accepts only `auto|bootstrap`, the detached
manifest, and that one bundle. It verifies the pair before SSH, uploads with
mode `0600`, verifies it again remotely, extracts the controller from the
verified bundle, and executes it under the server-side deployment lock.

New manifests use schema V2. Every non-baseline migration record contains
`version`, `kind`, `epoch`, the canonical newline-normalized `checksum`, and
`requires_contracts` as `{version, checksum}` records. The controller continues
to read schema V1 manifests throughout the rollback window, but V1 approvals
cannot unlock an epoch-2 expansion.

## One-time production preparation

First audit, then create the private directory layout:

```bash
python3 scripts/bootstrap_release_root.py \
  --production-root /data/lzq/gith/nexpoly

python3 scripts/bootstrap_release_root.py --apply \
  --production-root /data/lzq/gith/nexpoly \
  --confirm-production-root /data/lzq/gith/nexpoly
```

The bootstrap tool creates only private directories and
`ops/state/deploy.lock`. It does not touch containers, PostgreSQL, systemd,
credentials, `ops/current`, or `ops/current-assets`. Runtime state is limited to:

```text
ops/state/release-state.json
ops/state/deploy-in-progress.json
ops/state/deploy.lock
```

There is no `ops/state/releases` inventory.

Complete the following during the reviewed maintenance window:

1. Before replacing any container or unit, preserve the exact legacy rollback
   identity in a private directory:

   ```bash
   umask 077
   legacy=/data/lzq/gith/nexpoly/ops/config/bootstrap-legacy
   install -d -m 0700 "$legacy"
   backend_container="$(docker compose -p nexpoly \
     -f /data/lzq/gith/nexpoly/docker-compose.yml ps -q backend)"
   web_container="$(docker compose -p nexpoly \
     -f /data/lzq/gith/nexpoly/docker-compose.yml ps -q nginx)"
   docker inspect --format '{{.Image}}' "$backend_container" \
     >"$legacy/backend-image-id"
   docker inspect --format '{{.Config.Image}}' "$backend_container" \
     >"$legacy/backend-image-ref"
   docker inspect --format '{{.Image}}' "$web_container" \
     >"$legacy/web-image-id"
   docker inspect --format '{{.Config.Image}}' "$web_container" \
     >"$legacy/web-image-ref"
   cp -a ~/.config/systemd/user/nexpoly-monomer-md-worker.service \
     "$legacy/nexpoly-monomer-md-worker.service"
   if [[ -d ~/.config/systemd/user/nexpoly-monomer-md-worker.service.d ]]; then
     cp -a ~/.config/systemd/user/nexpoly-monomer-md-worker.service.d \
       "$legacy/"
   fi
   chmod -R go-rwx "$legacy"
   python3 - "$legacy" <<'PY'
   import hashlib
   import json
   from pathlib import Path
   import sys

   root = Path(sys.argv[1])
   digest = lambda payload: "sha256:" + hashlib.sha256(payload).hexdigest()
   file_digest = lambda path: digest(path.read_bytes())
   unit_files = sorted(
       path for path in root.glob("nexpoly-monomer-md-worker.service*")
       if path.is_file()
   ) + sorted(
       path for path in root.glob("nexpoly-monomer-md-worker.service.d/*")
       if path.is_file()
   )
   worker_units = {
       str(path.relative_to(root)): file_digest(path) for path in unit_files
   }
   worker_unit_sha256 = digest(
       json.dumps(
           worker_units, sort_keys=True, separators=(",", ":"), ensure_ascii=True
       ).encode("utf-8")
   )
   identity = {
       "backend_image_id": (root / "backend-image-id").read_text().strip(),
       "web_image_id": (root / "web-image-id").read_text().strip(),
       "worker_unit_sha256": worker_unit_sha256,
   }
   legacy_runtime_sha256 = digest(
       json.dumps(
           identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True
       ).encode("utf-8")
   )
   document = {
       "schema_version": 1,
       **identity,
       "backend_image_ref": (root / "backend-image-ref").read_text().strip(),
       "web_image_ref": (root / "web-image-ref").read_text().strip(),
       "worker_units": worker_units,
       "legacy_runtime_sha256": legacy_runtime_sha256,
   }
   output = root / "legacy-runtime.json"
   output.write_text(
       json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
       encoding="utf-8",
   )
   output.chmod(0o600)
   print(legacy_runtime_sha256)
   PY
   docker image inspect "$(<"$legacy/backend-image-id")" \
     "$(<"$legacy/web-image-id")" >/dev/null
   ```

   Record the legacy Backend, Web and Worker health responses while they are
   still running. Image IDs—not mutable tags—are the rollback identity. Keep
   all saved files deploy-user-owned and private.
2. Create a custom-format PostgreSQL dump, its SHA-256, and verify it with
   `pg_restore --list`. Rotate the production database password and update all
   App/Worker DSNs together. Production Compose binds PostgreSQL only to
   `127.0.0.1:55432`.
3. Install `ops/config/deploy.env.example`, `app.env.example`, and
   `worker.env.example` as `deploy.env`, `app.env`, and `worker.env`, owned by
   the deployment user with mode `0600`. Replace every placeholder.
4. Configure a dedicated GHCR credential for the production deployment user.
   It must have package read permission only; do not put the raw token in the
   repository, `deploy.env`, shell history, a command argument, or this runbook.
   Enter it through stdin so Docker stores it only in the deployment user's
   mode-`0600` credential configuration, then verify an exact private digest
   produced while auto-deploy remains disabled:

   ```bash
   read -rsp 'GHCR read-only token: ' GHCR_PULL_TOKEN; echo
   printf '%s' "$GHCR_PULL_TOKEN" | \
     docker login ghcr.io --username lzq390 --password-stdin
   unset GHCR_PULL_TOKEN
   chmod 700 ~/.docker
   chmod 600 ~/.docker/config.json
   docker pull ghcr.io/lzq390/nexpoly-backend@sha256:<reviewed-digest>
   docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
     ghcr.io/lzq390/nexpoly-backend@sha256:<reviewed-digest>
   ```

   Run main CI once to publish and smoke that digest; the bridge gate forces
   push-triggered SSH deployment off. The label must equal the intended bootstrap SHA.
   Repeat the pull check for the Web digest. A failed private pull blocks the
   maintenance window.
5. Freeze `/home/devuser/miniconda3/envs/byteff2-repro`; never run pip in it.
   Record its identity in `deploy.env`:

   ```bash
   python3 scripts/release_controller.py worker-base-identity \
     --python /home/devuser/miniconda3/envs/byteff2-repro/bin/python
   ```

6. Create the baseline read-only asset release. The command audits first; the
   apply form requires all exact-path confirmations:

   ```bash
   python3 scripts/bootstrap_asset_release.py \
     --source-root /data/lzq/gith/nexpoly \
     --byteff2-root /data/lzq/gith/byteff2 \
     --asset-store /data/lzq/nexpoly-assets

   python3 scripts/bootstrap_asset_release.py --apply \
     --source-root /data/lzq/gith/nexpoly \
     --byteff2-root /data/lzq/gith/byteff2 \
     --asset-store /data/lzq/nexpoly-assets \
     --confirm-source-root /data/lzq/gith/nexpoly \
     --confirm-byteff2-root /data/lzq/gith/byteff2 \
     --confirm-asset-store /data/lzq/nexpoly-assets
   ```

   Point `ops/current-assets` atomically at
   `/data/lzq/nexpoly-assets/releases/<digest>` and use the same digest in
   `release-input.json`. The controller injects that verified manifest value at
   runtime; do not duplicate it in `deploy.env`. Bootstrap rejects a different
   target.
7. Stage the tracked candidate user unit without replacing the running legacy
   unit yet:

   ```bash
   install -m 0644 ops/systemd/nexpoly-monomer-md-worker.service \
     /data/lzq/gith/nexpoly/ops/config/nexpoly-monomer-md-worker.candidate.service
   ```

8. From a verified archive of the exact bootstrap SHA—not the mutable
   development checkout—install and audit the bootstrap hooks:

   ```bash
   install -m 0700 ops/config/bootstrap-quiesce.example \
     /data/lzq/gith/nexpoly/ops/config/bootstrap-quiesce
   install -m 0700 ops/config/bootstrap-rollback.example \
     /data/lzq/gith/nexpoly/ops/config/bootstrap-rollback
   ```

   `bootstrap-quiesce` stops legacy nginx, then invokes the separately audited
   `/data/lzq/gith/nexpoly/ops/config/bootstrap-active-jobs-probe`. That probe
   must inspect the legacy runtime and print exactly one JSON object containing
   all eight job categories with zero counts. Do not install a probe that merely
   prints canned zeroes. The optional protocol discriminator is named
   `active_jobs_schema_version`: absence is legacy-only and must match the
   caller's exact expected category set; integer `1` means the full eight-category
   V1 set, while integer `2` adds `monomer_dft`. The legacy generic field
   `schema_version`, dual fields, nulls, booleans, and unknown versions are rejected.
   The bootstrap `postgres-init` follow-up is deliberately unversioned because
   it rechecks only the two durable PostgreSQL categories after this global
   proof; it must never claim to be a V1 full-category payload.

   `bootstrap-rollback` delegates to a separately audited, deploy-user-owned,
   mode-`0700` executable at
   `/data/lzq/gith/nexpoly/ops/config/bootstrap-legacy-runtime-restore`. Implement
   it for this host before dispatch. It must stop the candidate runtime, retag
   or select the saved local Backend/Web image IDs, restore the saved Worker
   user unit and drop-ins, run `systemctl --user daemon-reload`, restart all
   legacy services without building or pulling, and verify Backend, Web,
   Worker, and ingress health. It must read the saved `legacy-runtime.json` and
   return the exact three identity digests it restored. Its stdout must be
   exactly:

   ```json
   {
     "schema_version": 1,
     "legacy_runtime_restored": true,
     "backend_image_id": "sha256:<64 hex>",
     "web_image_id": "sha256:<64 hex>",
     "worker_unit_sha256": "sha256:<canonical saved-unit-set digest>",
     "backend_healthy": true,
     "web_healthy": true,
     "worker_healthy": true,
     "ingress_restored": true
   }
   ```

   The wrapper rejects missing fields, false health evidence, wrong digests,
   unsafe ownership/mode, or restore failure. Rehearse this restore while the
   legacy runtime is still authoritative: replace/restart it from the saved
   image IDs and unit, recompute the installed unit-set digest, run all three
   health probes, and confirm the wrapper exits zero. Returning saved values
   without checking the restored containers/unit is not valid evidence. If the
   rehearsal fails, restore service manually and cancel bootstrap.
   After a successful rehearsal, install the staged candidate unit without
   starting it; the rollback helper must remain able to restore the saved unit:

   ```bash
   install -m 0644 \
     /data/lzq/gith/nexpoly/ops/config/nexpoly-monomer-md-worker.candidate.service \
     ~/.config/systemd/user/nexpoly-monomer-md-worker.service
   systemctl --user daemon-reload
   systemctl --user enable nexpoly-monomer-md-worker.service
   ```
9. Add the target SHA and hook paths to `deploy.env`:

   ```text
   NEXPOLY_BOOTSTRAP_RELEASE_SHA=<current 40-character main SHA>
   NEXPOLY_BOOTSTRAP_QUIESCE_COMMAND=/data/lzq/gith/nexpoly/ops/config/bootstrap-quiesce
   NEXPOLY_BOOTSTRAP_ROLLBACK_COMMAND=/data/lzq/gith/nexpoly/ops/config/bootstrap-rollback
   NEXPOLY_BOOTSTRAP_LEGACY_RUNTIME_SHA256=sha256:<canonical legacy identity>
   ```

Dispatch `CI` from main with `operation=bootstrap`. The server rejects bootstrap
if either `ops/current` or `release-state.json` already exists. It isolates
legacy ingress, validates zero active work, creates and verifies another dump,
applies pending expand migrations, starts the new runtime, and runs the same
production smokes as a normal release. Migration `0012_drop_polytao_jobs` is a
trailing contract migration and remains pending; this minimal pipeline never
executes destructive migrations.

After success, verify the state, running image digests, Worker identity, Web,
PolyTAO, and Monomer-MD results. Remove the bootstrap-only environment entries,
retain the hooks for audit. Automatic deployment stays disabled for the epoch
bridge; re-enabling it is a separate reviewed change after 0012 maintenance and
rollback evidence are complete.

## Checksum-pinned 0012 maintenance

Production contract execution is not a deployment mode and cannot run an
arbitrary pending contract. The dry-run plan is:

```bash
python3 scripts/release_controller.py maintain-contract-0012 \
  --manifest /path/to/release-manifest.json \
  --operation-id contract-0012-YYYYMMDD
```

With `--apply`, the command requires the exact production root and current
immutable release manifest. It uses `deploy.lock`, drains all V1/V2 active-job
categories, first hard-locks the target to `nexpoly`, rejects every unknown
database or migration-ledger row, and requires the exact canonical ledger
prefix through 0011. It verifies the expected 9-row archive (7 completed, 2 failed), writes
full/table/schema backups and canonical digests, fsyncs the files and directory
entries before destructive authorization, restores the full backup into
an isolated database owned by the current operation's mode-0600 marker, and invokes only `postgres_migrations --mode
contract-0012`. Success atomically stores
`{version, checksum, operation_id, approved_at}`, a checksum-bound rollback
floor and the epoch barrier. The durable in-progress marker drives idempotent
resume or full database/state restoration after interruption; it also rebuilds
the success journal from checksum-verified audit evidence if state committed
immediately before a crash. A rolled-back attempt keeps its journal/audit
identity; any explicit retry must use a new operation ID.

The gate also requires the external read-only database inventory command and
the two pinned audit-role identities documented in
`postgres-migration-governance.md`. Production `nexpoly` remains the only
writable target. The dev and health stacks must be reachable and exactly
accounted for before the operation starts; their captured evidence is reused
only for rollback after the destructive marker is durable.

Contract approval authority comes only from a complete
`{version, checksum, operation_id, approved_at}` record whose checksum, operation,
timestamp, compatibility floor, and epoch barrier agree exactly. Migration
history, a release/candidate manifest, a floor by itself, and the legacy
name-only approval list never imply approval. For this operation, the only
accepted identity is canonical 0012 at epoch 1 and `approved_at` must be the
second-precision `+00:00` UTC form emitted by the controller.

## Automatic deployment state machine

The controller holds non-blocking `ops/state/deploy.lock`; a second deployment
fails before any state change. It then:

1. verifies the manifest/bundle, production paths and permissions, asset digest,
   frozen Worker Python/Conda/GROMACS identity, Compose digest policy, image
   labels, disk space, and current main SHA;
2. extracts `<sha>.staging`, pulls exact Backend/Web digests, and builds the
   release venv offline without modifying the frozen Conda environment;
3. enables API and Worker drain and waits up to 30 minutes for every API, GPU,
   PolyTAO, Monomer-MD and in-flight write category to reach zero;
4. creates `pg_dump -Fc`, SHA-256 and JSON sidecars and runs
   `pg_restore --list`;
5. applies expand migrations only. A contract may remain pending only while no
   later epoch depends on it; dependency names and checksums are validated
   before any later-epoch DDL;
6. if the asset digest changed, rebuilds exactly the datasets from
   `release-input.json`, then atomically switches `ops/current-assets`;
7. writes the target-SHA analytics snapshot, atomically switches `ops/current`,
   starts Backend and the release Worker, and keeps public nginx stopped;
8. runs strict PostgreSQL and GPU preflight, real Conditional Generation and
   PolyTAO generation, the 300-step Monomer-MD ByteFF2/GROMACS smoke, an
   isolated Web/static-asset smoke, and final public health checks;
9. rechecks current main before each irreversible exposure point, atomically
   writes `release-state.json`, removes `deploy-in-progress.json`, and resumes
   admission only after success.

`deploy-in-progress.json` records `prepared`, `db-changed`, `switched`, or
`verified`. A later invocation must complete fail-closed recovery before it may
start another release.

## Failure and rollback runbook

- Before database change: the target is discarded; the existing runtime and
  assets remain selected. Any supported Worker drain is resumed.
- After expand migration with unchanged assets: the controller restores the
  previous Backend/Web digest, `ops/current`, Worker venv and Worker process.
  Compatible expand migrations remain. It regenerates the previous-SHA
  analytics snapshot before strict old-runtime health checks.
- After an asset/data change: nginx and writers remain stopped, the verified
  dump is restored, the previous asset pointer and runtime are restored, and
  drain is released only after strict health and Worker smoke succeed.
- During first bootstrap: the audited restore helper must restore and verify the
  saved legacy Backend/Web image IDs, Worker unit/process, and ingress before
  the rollback wrapper returns success. Merely starting nginx is insufficient.
  Bootstrap-expand migrations are retained as compatible changes.
- If rollback, database restore, or drain resume cannot be proven, production
  remains drained and `deploy-in-progress.json` is retained. Do not delete or
  edit it; rerun the controller through the same workflow after correcting the
  external fault so recovery occurs under `deploy.lock`.
- More than 30 minutes of active work records a safe deferred failure before
  backup/migration/switch and resumes the unchanged runtime. Active work is
  never killed to force deployment.

Useful read-only checks:

```bash
cat /data/lzq/gith/nexpoly/ops/state/release-state.json
readlink -f /data/lzq/gith/nexpoly/ops/current
readlink -f /data/lzq/gith/nexpoly/ops/current-assets
docker compose -p nexpoly \
  -f /data/lzq/gith/nexpoly/ops/current/docker-compose.yml \
  -f /data/lzq/gith/nexpoly/ops/current/docker-compose.prod.yml \
  --env-file /data/lzq/gith/nexpoly/ops/config/deploy.env config --images
systemctl --user status nexpoly-monomer-md-worker.service
```

Every application image printed by `config --images` must contain `@sha256:`;
the Backend/Web labels, release state and Worker identities must report the same
source SHA. This minimal path intentionally has no manual arbitrary rollback,
destructive migration operation, blue/green runtime, off-host backup, or
self-hosted PR runner.

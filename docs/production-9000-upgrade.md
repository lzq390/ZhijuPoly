# Account-enabled production upgrade on HTTP 9000

This operation preserves `http://114.214.255.154:9000`, all existing production
data and available artifacts. Use a new Compose project, a separate PG16 volume,
and independent worker state. The historical manual release remains the model
for the deployment lock, draining, ingress isolation and recovery. Historical
scripts contain fixed releases and must not be executed for this upgrade.

## Freeze inputs before maintenance

Record the reviewed main SHA and tree, successful CI run, backend and web image
digests, PG image digest, worker source/runtime identities, effective production
configuration, current containers/units and recovery commands. Keep credentials
and raw configuration in an operation directory with mode `0700`; files containing
credentials use `0600`. Do not copy development accounts or calculation results.

Render `docker-compose.yml`, `docker-compose.prod.yml` and
`docker-compose.prod-accounts.yml` together under a new project name. Supply an
independent `NEXPOLY_RUNTIME_ROOT`, the required worker/socket roots, explicit file
owner UID/GID, and the new external PG volume. Candidate ingress is loopback
`19000`; worker authorization callbacks use loopback `18002`; PG uses loopback
`55433`. Rendered configuration contains secrets and must remain private.

The overlay enables exact-origin production HTTP authentication, account
isolation, structure library, literature, batch jobs, polymer MD and metering.
Quota enforcement and automatic historical artifact expiry remain disabled.
The batch worker uses its own service login and environment file, without auth
credentials or scientific worker launch tokens. Online services must not receive
the migration administrator or literature source administrator credentials.

Build monomer workers with `scripts/prepare_production_worker_cohort.py` from a
complete clean Git clone at
`/data/lzq/gith/nexpoly-release-private/<operation_id>/<full-SHA>`. Keep this code
outside the runtime tree so the existing controller import boundary is preserved.
Its sealed cohort manifest is required
by the candidate launchers. It reuses the existing dependency/model build checks,
but does not migrate databases, publish applications or switch services. A
candidate toolchain seal does not replace actual scientific acceptance.
The build validates the governed asset release and seals its canonical ByteFF2
root and inventory. Candidate assets point directly to that immutable release;
the old stack's mutable `current-assets` pointer must stay unchanged during the
build and is not used as the candidate's ongoing authority.

The candidate build normalizes only newly generated metadata: the four MD venv
activation scripts lose group/world write bits, and a successful DFT `uv pip
install` may seal its empty, owned, single-link `venv/.lock` as `0600` after
checking the exact preparing operation and SHA. Other mode drift fails the full
runtime inventory. Installed AIMNet console scripts are accepted only when the
locked wheel declares them, their installed metadata matches that wheel, and
their ordinary, owned, single-link files stay in this venv's `bin` with valid
RECORD hashes and sizes. Existing runtimes and scientific inputs are not changed.
Read-only Python identity/version probes use explicit `-B`, including under
`-I`, so startup `.pth` imports cannot add bytecode to sealed runtimes.
The candidate DFT worker reads GPU guard state from its own
`<cohort-root>/dft/gpu2-guard.json`. Provide an operation-specific monitor bound
to the candidate units, backend project and GPU identity; the old monitor's
hard-coded service identities cannot classify the new stack. Keep the sealed
guard mode and wait for unrelated GPU processes without signalling them.

Polymer MD uses the production runtime contract and a separate jobs root, 16 CPU,
32 GiB, no GPU and one execution slot. Render the analysis service with
`scripts/polymer_md_analysis_container.py`, using the fixed image built from
`workers/polymer_md_analysis/Dockerfile`; it receives read-only job/source/runtime
mounts, a private spool/socket, 1 CPU, 2 GiB and no network or GPU. Verify kernel
read-only mounts: user systemd `ReadOnlyPaths` alone is insufficient on some hosts.

## Rehearse against copied production data

1. Back up the production database and roles; inventory and copy associated files
   without changing their relative paths. Restore into the new PG cluster.
2. Expand through `0017`. Create ordinary account `legacy-owner` and independent
   API, authentication and service login identities with the intended group roles.
3. Make a backup of this candidate itself and actually restore it. Use that receipt
   for `0018` ownership cutover, then apply `0019` service privilege contraction.
   Expand through `0022`.
4. Set the candidate's persistent deployment drain with the operation ID and target
   SHA. Stop all candidate writers. Back up and restore again, including all table
   content, sequences, roles/memberships and copied files. In PG16, preserve the
   original bootstrap role identity when replaying `GRANTED BY` role grants.
5. Run the production-only `literature-prod-0024` maintenance entry documented in
   [knowledge-literature.md](knowledge-literature.md), binding the original and
   candidate database system identifiers, operation, SHA, stopped-writer evidence
   and candidate-owned backup receipt. It applies `0023` and `0024` atomically;
   ordinary expand and the development maintenance entry cannot substitute for it.
6. Use production-specific read-only credentials against the real literature source
   and object store. Run the complete `dry-run`, then `prepare --verify-assets`,
   inspect the full report, and `publish`. Do not use a partial scan as a production
   generation. Verify object version retention or a separate asset backup.
   Repeat a full preparation and verify native generation switching. Reused
   immutable snapshot bodies must compare with PostgreSQL JSONB semantics;
   numeric serialization round trips must not prevent an identical generation,
   while changed content must still fail.

Compare original columns in all original business tables and all original
sequences before/after migration; separately verify approved owner backfill and
new schema. A successful migration ledger alone is insufficient. Preserve every
report, backup checksum and actual restore result in the operation directory.

## Final maintenance and switch

Only begin maintenance after the rehearsal, CI, fixed images and runtime checks
pass. Acquire the existing deployment lock and serve a maintenance page on 9000.
Stop submissions, drain jobs and leases, then stop all old writers. Take the final
consistent database/role/file snapshot and rebuild the formal candidate from it;
the rehearsal snapshot is not the final production data. Repeat the proven
migration and full literature import sequence. Freeze the old database and files.

Do not rewrite historical job metadata to accommodate new paths. Keep the copied
directory structure and configure the candidate's fixed `job_root`. Stop old GPU
workers before starting candidate GPU workers. Wait for unrelated GPU tasks to
finish naturally before scientific acceptance.

Use controlled local routing to test the candidate with the formal HTTP origin;
do not broaden origin checks to admit loopback. Verify login, password change,
logout/revocation, multiple tabs, SSE/downloads, origin rejection, account isolation
and the existing 9011 integration. Verify historical tasks as `legacy-owner`,
including artifacts older than the former expiry limits. Run real submit/cancel/
result tests for monomer MD/DFT, batch jobs, polymer MD and trajectory/distance
analysis; verify metering replay does not double count and quotas remain disabled.
Validate literature search, details, original files, generation switch and restore.
Bind scientific evidence to the candidate runtime; pending evidence remains pending.
When using `scripts/polymer_md_acceptance_registry.py`, pass
`--retention-enabled false` to match this production stack's protocol binding;
the default retains the development retention policy and is a different binding.

After all acceptance gates pass and at least 15 minutes of observation, stop the
old web ingress, render the new web binding as `0.0.0.0:9000`, and record durable
public-reopen intent before allowing submissions. Synchronize the production
checkout to the exact main SHA and seal the final inventories.

Before public reopening, failure permits restoring the old configuration, workers,
database/files and ingress. After reopening, or if reopening is uncertain, preserve
new writes and repair forward; never redirect to the frozen old database.

Check every five minutes for the first hour, then hourly through 12 hours. After
seven stable days and a verified new-version backup restore, remove only obsolete
application containers, units and proven unreferenced temporary resources. Keep
old database and historical file backups. Never use full-stack `down -v` or global
pruning for this operation.

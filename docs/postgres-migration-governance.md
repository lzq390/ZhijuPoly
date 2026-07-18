# Postgres Migration Governance

This project treats Postgres migrations as an append-only runtime contract. A
checksum mismatch is a blocker until the schema and migration history are
reconciled explicitly. The runner and strict preflight inspect raw ledger rows
before normalizing them; duplicate versions are blockers even when their
checksums happen to match.

The migration manifest accepts legacy schema V1 for rollback compatibility and
is authored as schema V2. V2 records bind every migration to
`version/kind/epoch/checksum/requires_contracts`. It contains exactly one
historical `baseline`, and it must remain the first migration. Within an epoch,
contracts must form the trailing suffix. A later epoch may resume expansions
only when every record checksum-binds all contracts from earlier epochs. Epoch
1 ends at `0012`; epoch 2 starts at `0013` and therefore requires the canonical
0012 checksum.

Migration `0012_drop_polytao_jobs` is a known, review-required contract
migration. Its presence in the manifest is not approval to run it against an
existing database. It sets a transaction-local 10-second lock timeout, drops
only `generation.polytao_jobs`, and removes the `generation` schema only when it
is empty; it never uses `CASCADE`. A genuinely fresh database bootstrap may
apply the full baseline/expand/contract chain to reach the final schema. A
database with any recorded migration may expose only the checksum-pinned
`--mode contract-0012` runner operation, and production invokes it only through
a dedicated pull-state maintenance adapter. The legacy
`release_controller.py maintain-contract-0012` entry is intentionally retired:
it requires `ops/releases` and a source bundle and therefore cannot authorize a
live-checkout deployment. There is no generic "run pending contracts" CLI.
Automatic CI/CD never grants destructive approval and `--mode bootstrap` is not
a destructive-upgrade shortcut.

The one-time controller cutover uses `--mode bootstrap-expand`. On the audited
production ledger through 0008 it applies 0009-0011, then defers the trailing
0012 contract. Deferral is permitted only for a trailing contract suffix; an
unexpected baseline, ordering, or checksum must stop the cutover. The resulting
release state lists 0012 as pending rather than applied.

Before that cutover, the content-addressed
`nexpoly-reconcile-production-0005-polytao-alias` control removes the one known
legacy alias row only after proving the exact production cluster, canonical
0001-0008 ledger plus alias, fixed PolyTAO schema identity, the business rows
dynamically sealed while locked, a full backup, and a real isolated PostgreSQL
16 restore. Its only database mutation is
an advisory/table-locked compare-and-swap delete of the exact
`0005_polytao_jobs` version/checksum/applied-at tuple. It never runs migration
SQL or changes `generation.polytao_jobs`. A missing alias without the matching
durable operation intent is not treated as success.

Governed production Pull deployments use `--mode expand`; it applies compatible
expansions and may defer only contracts that do not gate a later epoch.
`bootstrap-expand` and `restore-expand` use the same rule for first cutover and
isolated restore checks. If 0012 is missing or has a different checksum, every
epoch-2 expansion is rejected during planning, before any epoch-2 SQL executes.

Before epoch 2 exists, strict preflight requires 0001-0011. It reports an unapplied 0012 under
`migrations.pending_contracts` but does not treat it as a missing required
migration. This lets the Backend run and be tested without converting a health
check or a merge to `main` into destructive-migration approval. The minimal
initial Pull deployment path deliberately leaves 0012 pending. CI builds and
validates candidates and images; it never executes a production migration.

## Checksum Policy

- Migration SQL files under `backend/migrations/postgres/*.sql` are normalized to
  LF in Git through `.gitattributes`.
- `app.postgres_migrations.migration_checksum()` hashes SQL after normalizing
  CRLF and bare CR to LF. This makes the checksum stable across Windows, WSL, and
  Linux checkouts.
- The SQL executed by Postgres is not rewritten by the checksum helper. Only the
  checksum input is normalized.
- Schema V2 repeats that canonical checksum in the repository policy and the
  immutable release manifest. CI rejects drift between either record and SQL.
- Contract approvals and the rollback compatibility floor are never newly
  written as names. The durable approval shape is
  `{version, checksum, operation_id, approved_at}` and the floor shape is
  `{version, checksum}`. V1 state remains readable during the rollback window,
  but a name-only approval cannot satisfy an epoch-2 dependency.

### Known isolated Dev checksum drift (0009)

`nexpoly_dev` is currently blocked by a historical dirty-image checksum and is
not an exact ledger. Read-only provenance established that image
`nexpoly-dev-backend:latest`
(`sha256:da206c67c21f70f80df54d242c4aa56595c0e06cae4d03054ff246f383225d27`, revision label
`b875829c3f008b5ee733d8ffced3093e4cbb07c5`) carried 0009 ending in
`...;\n\n`, with checksum
`79a6956fc934794d61bc003f02a6b5280e9e8bd77a217b61a28d3dbdb8b7be0b`.
The committed canonical file ends in `...;\n`, with checksum
`ef1757a81976f351459e8257bd492aa6267cbf507c4ea85506fefa2d465d2db8`.
The extra trailing LF is the only byte difference, but it remains a real ledger
mismatch: neither runner nor preflight accepts it as an alias.

The file evidence can be reproduced without changing a database:

```bash
docker image inspect nexpoly-dev-backend:latest \
  --format '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
docker run --rm --entrypoint sha256sum nexpoly-dev-backend:latest \
  /app/backend/migrations/postgres/0009_monomer_md_job_leases.sql
PYTHONPATH=backend python3 -c \
  'from app.postgres_migrations import MIGRATIONS_DIR,migration_checksum; print(migration_checksum(MIGRATIONS_DIR / "0009_monomer_md_job_leases.sql"))'
```

Use a read-only SQL session to capture ledger and schema/index evidence:

```sql
SELECT version, checksum, applied_at
FROM governance.schema_migrations
WHERE version = '0009_monomer_md_job_leases';

SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'md'
  AND table_name = 'monomer_md_jobs'
  AND column_name IN ('worker_instance_id', 'heartbeat_at', 'lease_expires_at')
ORDER BY column_name;

SELECT i.indisvalid, i.indisready, i.indisunique,
       pg_get_indexdef(i.indexrelid) AS index_definition,
       pg_get_expr(i.indpred, i.indrelid) AS predicate
FROM pg_index AS i
JOIN pg_class AS c ON c.oid = i.indexrelid
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = 'md'
  AND c.relname = 'idx_monomer_md_jobs_active_lease';
```

Until a separate maintenance PR and explicit authorization perform an exact
old-to-canonical compare-and-swap with schema/index verification, backup, and an
audited operation record, `nexpoly_dev` remains isolated and deployment must
fail closed. That future operation is not part of the 0012 maintenance entry
point. Do not edit the ledger manually and do not add the dirty checksum to an
accepted-checksum list.

## Production 0012 Maintenance

No production destructive command is exposed by the retired bundle controller.
The private runtime bootstrap installs the dedicated pull-state adapter and a
byte-identical governance core; invoking the old `maintain-contract-0012`
command still fails closed. Operators must not recreate `ops/releases`,
synthesize a legacy release manifest, or edit the ledger to bypass the adapter.

After the production Pull deployment and the separate Dev checksum repair have
passed their own gates, run the read-only plan first:

```bash
/data/lzq/gith/nexpoly-runtime/bin/nexpoly-pull-contract-0012 plan \
  --operation-id contract-0012-<utc-timestamp>
```

The `apply` subcommand is also a dry-run unless the explicit mutation flag and
both exact-root confirmations are present. During the authorized maintenance
window, use the same reviewed operation ID:

```bash
/data/lzq/gith/nexpoly-runtime/bin/nexpoly-pull-contract-0012 apply \
  --operation-id contract-0012-<utc-timestamp> \
  --apply \
  --confirm-production-root /data/lzq/gith/nexpoly \
  --confirm-runtime-root /data/lzq/gith/nexpoly-runtime
```

Both commands fail closed unless the deployment state contains the exact,
ordered canonical history from 0001 through 0011. The plan records the adapter,
governance-core, sealed descriptor, source SHA/tree and manifest identities;
apply persists the same authority inside the private audit manifest.

The pull-state adapter accepts only a sealed deployment descriptor/current
state, the clean live-checkout source SHA/tree, and its canonical migration
manifest. Its dry-run is the only allowed first step; `--apply` must be accepted
only at `/data/lzq/gith/nexpoly`. While holding the
same deployment lock used by code releases, the controller drains admission and
workers, verifies all active-job categories are zero, and before any destructive
action proves that the current database is exactly `nexpoly`, inventories the
whole cluster and its database purposes, rejects unregistered databases, and
matches the ledger byte-for-byte to the canonical prefix through 0011. It writes a mode-0600 full
dump plus table/schema archives in a mode-0700 audit directory, fsyncs each
file and every newly created directory entry before recording an audit
manifest, records a
canonical row and structure digest (columns, indexes, constraints, and triggers)
for every PolyTAO business row present after the maintenance-window drain and
active-job lock; row totals and per-status counts are sealed dynamically rather
than fixed in code. It then restores the full dump into an isolated verification
database. A pre-existing verification database may be
cleaned only when a mode-0600 marker binds its exact name to the current operation
ID and immutable source SHA and proves the database was absent before the
create intent. A crash after `createdb` but before the marker advances to
`created` is recovered by validating that deterministic identity, dropping
only that operation-owned database, and persisting `dropped`. The same gate
handles an unknown client result after possible server-side creation: admission
remains drained and the global operation marker remains durable until a fresh
inventory proves the reserved name absent, or an exact cleanup plus a second
inventory proves it absent. A different operation ID cannot adopt or delete
the uncertain database. It then runs
only checksum-pinned 0012, verifies the ledger and removed schema, runs strict
preflight and an ingress-isolated production smoke, and atomically records the
approval, epoch barrier, rollback floor, operation journal, and audit manifest.

`/data/lzq/gith/nexpoly-runtime/state/contract-0012-in-progress.json` is the
required durable recovery marker. A retry with the same sealed pull descriptor,
live source identity and operation ID either resumes a fully committed operation
(including reconstructing a success journal if interruption occurred between
the atomic deployment-state write and journal write) or restores
the verified full dump and previous release state before asking for an explicit
retry under a new operation ID, preserving the failed/recovered journal and
audit evidence. Failed recovery keeps admission drained and retains the marker;
operators must never manufacture a ledger row.

The production-cluster inventory is not treated as a global inventory. Before
the destructive marker is created, the controller must execute the pinned,
deploy-user-owned mode-0700 command configured by
`NEXPOLY_CONTRACT_0012_EXTERNAL_DATABASE_AUDIT_COMMAND`. Its two online sessions
use non-superuser/non-CREATEDB/non-CREATEROLE audit users and prove
`transaction_read_only=true`. It also consumes the deploy-user-owned mode-0600
schema-v3 `postgres-media-registry.json`. Discovery enumerates every local
Docker container and volume without using a Nexpoly name prefix, derives
arbitrary PGDATA volume and bind mounts from the complete container inventory,
recursively read-only probes every volume, and scans all four compiled
private backup roots for the fixed PostgreSQL custom and tar formats. It requires
`expected_media_ids == discovered_media_ids`. Missing, additional, unreachable
or duplicate media blocks maintenance.

Every discovered local Docker volume and every regular file below the four
backup roots has one explicit classification: `nexpoly-db`,
`adjacent-record-only`, `reviewed-non-pg`, or `unsupported-blocking`.
PostgreSQL signatures require the matching `PG_VERSION`,
`global/pg_control`, and `base` directory. Adjacent PostgreSQL media is
content-CAS recorded without being started under PG16. The adjacent-only
envelope accepts complete PostgreSQL major versions 9 through 18; this does not
relax the fixed PG16 runtime or permit a Nexpoly database/backup to be started
or restored under another major. A partial signature or an unsupported
classification blocks deployable evidence. Reviewed non-PG
content is hashed and excluded from Nexpoly migration, never silently ignored.
Valid custom/tar dumps that contain only an adjacent PostgreSQL system or
administrative database may be classified `postgres_backup` plus
`adjacent-record-only`. That path never restores, starts, mounts, or passes the
source to Docker: it opens the file through the fixed private-root
`openat`/`O_NOFOLLOW` chain and CAS-seals format, path, device, inode, size,
nanosecond mtime, mode, owner, and complete digest before and after. Unknown,
partial, or magic-mismatched formats still fail discovery or require an
explicit non-PG/blocking review. Nexpoly logical dumps remain
`isolated-backup-restore-read-only`; record-only classification cannot replace
their disposable restore and ledger audit.
An exited PostgreSQL-candidate container may retain an empty or otherwise
complete non-PG volume at its former PGDATA mount only when the registry
explicitly classifies that volume as `reviewed-non-pg`: every attachment and
container/config identity is sealed, the content digest is stable, and any
active reader or partial PostgreSQL signature fails closed. Read-only
init/configuration binds whose destinations are provably disjoint from PGDATA
remain covered by the complete Docker inventory but are not database media;
writable or overlapping binds still block discovery.

The generic `/data/lzq/gith/nexpoly-runtime/backups` operation root is
deliberately forbidden as a legacy discovery root. Pull's newly created
rollback dump is bound separately by the prepared descriptor and must not make
the external-media set drift after preparation. A takeover first copies
reviewed legacy dumps into the exact mode-0700
`legacy-takeover/preserved-postgres-backups` root and reviewed recovery dumps
into `/data/lzq/recovery/nexpoly-postgres-media`. The boundary also includes
the read-only legacy production root `/data/lzq/gith/nexpoly/backups` and the
fixed dirty-0009 quarantine root
`/data/lzq/recovery/nexpoly-pre-merge-20260717T090623Z/dev-0009-quarantine`.
The former currently contains 23 dump names and the latter contains two; the
auditor only opens them read-only. Every parent below those anchors and every
dump must be deploy-user-owned and private. The roots and
accepted suffix/magic pairs are compiled into the builder and repeated
byte-for-byte in registry v3; callers cannot select fewer roots or formats.
At this development freeze the production backup directory is still mode
`0775`, and the two takeover staging roots may not exist. This is an explicit
readiness blocker, not an exception: the future maintenance preflight must
first CAS-record the directory identities, create any missing deploy-user-owned
mode-0700 staging roots, and tighten the production backup directory to 0700
without reading or rewriting dump contents. The current main-only repair does
none of those production mutations.

The raw SHA-256 of that private registry is configured as
`NEXPOLY_CONTRACT_0012_MEDIA_REGISTRY_SHA256`. Preparation seals the same digest
into the bridge descriptor, one-time token and current-state transition.
Preflight, the gate immediately before 0012, completion, rollback and B→F resume
all compare-and-swap the same value. Changing only `deploy.env` never authorizes
a different registry.

Registry v3 pins the complete discovery boundary, PG16 image, the image's
exact `postgres` runtime identity (`uid=70`, `gid=70` for the pinned Alpine
digest), exact source-pinned builder digest and private `pg_service.conf`
digest. The service
file is checked before and after the audit as sealed authority, but live SQL is
bound to the exact discovered PostgreSQL container and its local Unix socket;
it never follows a service-file TCP/DNS endpoint. Every volume record binds its arbitrary name,
driver, mountpoint, labels, complete Docker inspect identity, PGDATA subpath,
all attached container IDs, image IDs, immutable config digests, restart
identity, states and destinations, plus a content digest before and after the
audit. Online SQL system identifiers must equal an independent
`pg_controldata` read from that exact active container and PGDATA. Every bind is
copied through an `openat`/`O_NOFOLLOW` private
traversal; Docker never receives the source bind path. Every backup uses the
same private parent-chain discipline and binds path, device, inode, size,
nanosecond mtime, mode, owner, format and complete file digest.

Dormant volumes are mounted read-only only by a copy helper; PostgreSQL starts
against a disposable copied volume with `network=none`, never against the
source. Every isolated socket tmpfs uses the registry-sealed and image-probed
`uid=70,gid=70`; any image/runtime-user drift fails before a source is started.
Dumps are copied to private staging and restored with fixed flags into
a new disposable PG16 volume. Exact scratch container/volume deletion is
verified even on failure. Source identity and content digest are captured again
after audit and must compare equal. The complete Docker/backup discovery
boundary is rescanned after all media audits and its before/after state digests
must match. Online sources are never mounted or started by the auditor and are
observed only through the exact container's local Unix socket plus its
read-only `pg_controldata`.
For every Nexpoly medium, registry v3 declares every non-template database by
name, OID, owner, connection state, audit role and migration scope. The auditor
re-enumerates that full list and audits each connectable database. An omitted
database, including a hidden database carrying the superseded 0013 checksum,
blocks the operation; any such checksum sets the envelope-wide
`requires_0014=true`. Per-database evidence recomputes system-identifier scope,
database identity, complete raw ledger, canonical ordinary-table relation
authority (owner, columns/defaults, indexes and constraints), legacy relation
schema/content identity,
auditor digest and a self-sealed evidence digest. Logical dumps cannot preserve
their source cluster system identifier, so they explicitly record
`isolated-restore-cluster`; copied physical media records
`copied-source-cluster`.

The required-online projection is a canonical ordered subset of
`nexpoly_dev` and `nexpoly_md_health_opt`, and may be empty when neither has an
active, deliberately controlled read-only reader. Every non-projected medium
for either stack must be `retained-private-isolated` and audited through a
read-only physical copy. This permits the known dirty development volume to
remain offline and permits readiness after the side-dev container is removed
while both volumes are retained; it never fabricates an online clone or starts
a source volume. A site may project either stack only while its exact
registry-bound medium is deliberately online. If either database is also visible in the production cluster,
both observations must agree byte-for-byte on the ledger and legacy relation.
`nexpoly_dev` must already have the exact canonical 0012 checksum and removed
legacy relation. `nexpoly_md_health_opt` may be at any non-empty, ordered,
checksum-exact canonical prefix through 0012; relation presence is derived from
whether 0007 has applied and 0012 has not. Neither database is migrated or
cleaned. The validated inventory is stored as
`database-inventory.before.json` inside the immutable audit manifest. Recovery
reuses that durable pre-change external evidence so an unrelated audit-stack
outage cannot prevent restoration of the production database.

The command emits schema v3; schema-v2 registries and evidence fail closed. The repository templates
`ops/config/contract-0012-external-database-audit.example` and
`ops/config/postgres-media-registry.json.example` define the complete envelope.
The repository registry example intentionally contains
`unsupported-blocking` replacement sentinels for the populated production and
dirty-0009 roots; it cannot be used as a completeness claim. Before a
maintenance window, the private site registry must replace those sentinels
with one reviewed descriptor for every discovered dump. Missing or additional
files still fail the exact ID-set comparison.
`ops/config/postgres-media-audit-role.sql.example` grants CONNECT in every
declared connectable database and only the present read-only relations plus
`pg_control_system()` execution needed by the builder;
login secrets remain out of band. Its top-level identity is:

```json
{
  "schema_version": 3,
  "inventory_complete": true,
  "writable_target": {"stack": "production", "database": "nexpoly"},
  "media_registry": {
    "schema_version": 3,
    "sha256": "sha256:<private registry digest>",
    "discovery_boundary_sha256": "sha256:<compiled boundary>",
    "discovery_state_sha256_before": "sha256:<complete pre-audit state>",
    "discovery_state_sha256_after": "sha256:<same post-audit state>",
    "captured_at": "<UTC timestamp>",
    "expected_media_ids": ["<sorted complete IDs>"],
    "discovered_media_ids": ["<same sorted complete IDs>"],
    "docker_inventory_sha256": "sha256:<all inspected Docker objects>",
    "backup_inventory_sha256": "sha256:<all files in four fixed roots>"
  },
  "databases": ["<required-online primary-database projections>"],
  "media": ["<classified record for every local volume and backup-root file>"],
  "requires_0014": false
}
```

Raw media ledgers must be the exact contiguous canonical prefix; only isolated
media may use the empty prefix. The historical
`0005_polytao_jobs` alias and the quarantined dirty 0009 checksum are explicit
known identities. The alias is accepted only alongside canonical 0007 and
before 0012, and relation presence must equal “0007 applied and 0012 absent.”
The dirty 0009 medium must remain isolated. A canonical
`ab633a62…` 0013 is accepted. The superseded `a60cbf66…` checksum sets
`requires_0014=true`; any other or multiple 0013 rows fail closed. The registry
and evidence must be regenerated in the actual maintenance window, not reused
from development-time discovery.

The maintenance inventory is captured with immutable operation evidence before
`--apply`:

- `nexpoly` is the only destructive target; archive its legacy history and move
  it through checksum-pinned 0012.
- `nexpoly_dev` must never execute 0012 twice. Its known 0009 dirty-image
  mismatch keeps the whole source volume isolated until the separately
  authorized repair above; it is not falsely projected as an online database.
- `nexpoly_md_health_opt` is an independent temporary stack whose container may
  be removed; its retained volume stays read-only and is audited only through
  a disposable physical copy.
- Any database, volume or backup outside the reviewed inventory blocks the
  window. Do not let a migration runner discover or mutate media implicitly.

## Safe Reconcile Rule

Do not update `governance.schema_migrations` only to make preflight pass, and do
not copy a ledger `UPDATE` from a runbook. A future reconciliation is permitted
only through a separately reviewed, checksum-specific maintenance PR and
operation that proves all of the following before it exposes an apply mode:

1. The live schema already matches the current migration file.
2. The row being updated still has the exact old checksum you audited.
3. The old artifact and canonical SQL difference is captured byte-for-byte.
4. A verified backup, immutable operation ID, compare-and-swap row count, and
   before/after ledger evidence are written to a private audit directory.
5. The operation targets one named database and cannot execute an arbitrary
   pending migration or arbitrary ledger statement.

If any check fails, stop and inspect the drift. Do not rebuild or truncate
business tables as a shortcut. Historical one-off repairs are evidence, not a
reusable authorization mechanism.

## Property Filter Migration And Import

Apply migrations first:

```bash
cd backend
python -m app.postgres_migrations
```

Place `database/PolymerDatabaseV2.0_reliable085_standardized.csv` on the target
host before importing the standardized property filter dataset:

```bash
cd backend
python -m app.import_postgres --dataset property_filter
python -m app.postgres_preflight --mode runtime --strict
```

The local import CLI understands `--dataset all`, but `all` now expands only to
the explicitly classified static datasets. `online` and `lab` remain accepted
as retired names solely so old invocations fail with a clear error; neither can
import, upsert, or rebuild live rows. A static `--rebuild` truncates only the
selected static tables and never uses `CASCADE`, so an unexpected foreign-key
dependency aborts the transaction instead of deleting an unclassified table.

`release-input.json` is schema v2 and fixes
`datasets_on_asset_change=[]`. The deployment controller publishes a changed
content-addressed asset pointer without invoking `app.import_postgres`.
Static-data maintenance is a separate, explicit operation. The property-filter
import replaces only `core.polymer_property_filter_records`; it cannot truncate
or overwrite online knowledge, laboratory, monomer MD, or monomer DFT runtime
tables.

## Acceptance Checks

For schema-only release checks, run:

```bash
cd backend
python -m app.postgres_preflight --mode schema --strict
```

After the production first cutover this must succeed with 0001-0011 applied and
0012 listed only in `migrations.pending_contracts`. The currently isolated
`nexpoly_dev` database is expected to fail on the documented 0009 checksum until
its separate repair is authorized. Runtime strict preflight is separate: it
also requires governed runtime tables, the property-filter source, and
non-empty property-filter data.

Run these checks before using the database browser property filter module:

```sql
SELECT version, checksum
FROM governance.schema_migrations
WHERE version IN (
  '0001_app_data_governance',
  '0002_lab_identity_defaults',
  '0003_runtime_postgres_cutover',
  '0004_monomer_md_jobs',
  '0005_byteff2_formal_monomer_md',
  '0006_property_filter_records',
  '0007_polytao_jobs',
  '0008_polytao_backend_runtime',
  '0009_monomer_md_job_leases',
  '0010_deployment_control',
  '0011_monomer_md_demo_steps',
  '0012_drop_polytao_jobs'
)
ORDER BY version;

SELECT COUNT(*) AS row_count
FROM core.polymer_property_filter_records;

SELECT logical_name, status, row_count
FROM governance.source_files
WHERE logical_name = 'property_filter_csv';

SELECT dataset_key, status, row_count
FROM governance.import_batches
WHERE dataset_key = 'property_filter'
ORDER BY finished_at DESC NULLS LAST, started_at DESC
LIMIT 1;
```

Before maintenance, the query is expected to return 0001-0011 but not 0012.
After the governed operation it must return 0012 at checksum
`c59b6f1efe9f926ad135379bd1a7141a7920730fa93c0e802646b1b913511728`.
Do not insert or edit the 0012 ledger row manually.

Expected property filter row count for the current standardized CSV is
`615159`.

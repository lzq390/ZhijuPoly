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
0001-0008 ledger plus alias, nine-row PolyTAO archive and schema digests, a full
backup, and a real isolated PostgreSQL 16 restore. Its only database mutation is
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
for the reviewed 9 rows (7 completed, 2 failed), and restores the full dump into
an isolated verification database. A pre-existing verification database may be
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
the destructive marker is created, the controller must also execute the pinned,
deploy-user-owned mode-0700 command configured by
`NEXPOLY_CONTRACT_0012_EXTERNAL_DATABASE_AUDIT_COMMAND`. Its two sessions use
the configured, non-superuser/non-CREATEDB/non-CREATEROLE audit users and must
prove `transaction_read_only=true`. The JSON inventory must be complete and
contain exactly `nexpoly_dev` and `nexpoly_md_health_opt`; a missing, unreachable,
duplicate, or additional stack blocks maintenance. If either database is also
visible in the production cluster, both observations must agree byte-for-byte
on the ledger and legacy relation. The registry must additionally bind its one
and only writable target to `{stack: production, database: nexpoly}`. Every
same-cluster dev/health audit starts with `SET TRANSACTION READ ONLY` and must
report `transaction_read_only=true`; it never authorizes a write outside the
production target.

`nexpoly_dev` must already have the exact canonical 0012 checksum and removed
legacy relation. `nexpoly_md_health_opt` may be at any non-empty, ordered,
checksum-exact canonical prefix through 0012; relation presence is derived from
whether 0007 has applied and 0012 has not. Neither database is migrated or
cleaned. The validated inventory is stored as
`database-inventory.before.json` inside the immutable audit manifest. Recovery
reuses that durable pre-change external evidence so an unrelated audit-stack
outage cannot prevent restoration of the production database.

The external command prints exactly this field envelope; the empty ledger
arrays below are illustrative and must be replaced by the complete canonical
rows required for each database:

```json
{
  "schema_version": 1,
  "inventory_complete": true,
  "writable_target": {
    "stack": "production",
    "database": "nexpoly"
  },
  "databases": [
    {
      "stack": "nexpoly_dev",
      "database": "nexpoly_dev",
      "current_user": "nexpoly_dev_auditor",
      "transaction_read_only": true,
      "role_superuser": false,
      "role_create_db": false,
      "role_create_role": false,
      "ledger": [],
      "legacy_relation_present": false
    },
    {
      "stack": "nexpoly_md_health_opt",
      "database": "nexpoly_md_health_opt",
      "current_user": "nexpoly_health_auditor",
      "transaction_read_only": true,
      "role_superuser": false,
      "role_create_db": false,
      "role_create_role": false,
      "ledger": [],
      "legacy_relation_present": true
    }
  ]
}
```

The maintenance inventory is explicit and must be captured with the immutable
operation evidence before `--apply`:

- `nexpoly` is the only destructive target; archive its legacy history and move
  it through checksum-pinned 0012.
- `nexpoly_dev` already contains the exact 0012 checksum and must never execute
  0012 twice, but its known 0009 dirty-image mismatch keeps the whole database
  isolated until the separately authorized repair above.
- `nexpoly_md_health_opt` is an independent temporary stack and remains
  read-only during this maintenance.
- Any database outside the reviewed inventory blocks the window. Do not let a
  migration runner discover and mutate databases implicitly.

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

The local import CLI still understands `--dataset all`, but CI/CD deliberately
does not use it. `release-input.json` enumerates every intended dataset. The
controller compares the requested immutable asset digest with active release
state: an unchanged digest runs migrations only and performs no import; a
changed digest first creates a verified dump and then runs the complete
explicit dataset set with `--rebuild`.

The import replaces only `core.polymer_property_filter_records`. It must not
truncate or rebuild `core.polymer_properties`, knowledge, DFT, PI, or monomer MD
runtime tables.

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

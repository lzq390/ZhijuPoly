# Postgres Migration Governance

This project treats Postgres migrations as an append-only runtime contract. A
checksum mismatch is a blocker until the schema and migration history are
reconciled explicitly.

The manifest contains exactly one historical `baseline`, and it must remain
the first migration. CI rejects any later baseline; every post-bootstrap schema
change must be classified as `expand` or `contract`. This prevents a baseline
candidate that neither the automatic expand path nor the reviewed operations
path could safely advance.

Migration `0012_drop_polytao_jobs` is a known, review-required contract
migration. Its presence in the manifest is not approval to run it against an
existing database. It sets a transaction-local 10-second lock timeout, drops
only `generation.polytao_jobs`, and removes the `generation` schema only when it
is empty; it never uses `CASCADE`. A genuinely fresh database bootstrap may
apply the full baseline/expand/contract chain to reach the final schema. A
database with any recorded migration must use an explicit maintenance-window
`--mode contract`; the automatic CI/CD path never grants that approval and
`--mode bootstrap` is not a destructive-upgrade shortcut.

The one-time controller cutover uses `--mode bootstrap-expand`. On the audited
production ledger through 0008 it applies 0009-0011, then defers the trailing
0012 contract. Deferral is permitted only for a trailing contract suffix; an
unexpected baseline, ordering, or checksum must stop the cutover. The resulting
release state lists 0012 as pending rather than applied.

Automatic production releases use `--mode expand`; it applies compatible
expansions and defers only a trailing contract suffix. `bootstrap-expand` and
`restore-expand` use the same ordering rule for first cutover and isolated
restore checks. If any later expand appears after a pending contract, every
mode fails closed until the contract is handled in a maintenance window.

Strict preflight requires 0001-0011. It reports an unapplied 0012 under
`migrations.pending_contracts` but does not treat it as a missing required
migration. This lets the Backend run and be tested without converting a health
check or a merge to `main` into destructive-migration approval. The minimal
automatic delivery path deliberately leaves 0012 pending.

## Checksum Policy

- Migration SQL files under `backend/migrations/postgres/*.sql` are normalized to
  LF in Git through `.gitattributes`.
- `app.postgres_migrations.migration_checksum()` hashes SQL after normalizing
  CRLF and bare CR to LF. This makes the checksum stable across Windows, WSL, and
  Linux checkouts.
- The SQL executed by Postgres is not rewritten by the checksum helper. Only the
  checksum input is normalized.

## Safe Reconcile Rule

Do not update `governance.schema_migrations` only to make preflight pass. A
ledger update is allowed only when both checks are true:

1. The live schema already matches the current migration file.
2. The row being updated still has the exact old checksum you audited.

If either check fails, stop and inspect the drift. Do not rebuild or truncate
business tables as a shortcut.

The local repair for `0004_monomer_md_jobs` used this guarded pattern after
verifying that `md.monomer_md_jobs` matched the current migration:

```sql
UPDATE governance.schema_migrations
SET checksum = 'b3ad64728f399f42b2bf9edb47ad035ac70f09fce6ced48e7b422ea74d5a7e8e'
WHERE version = '0004_monomer_md_jobs'
  AND checksum = '41a2670a08ab7a2b90cea84c2fe3332165eef1b15f0c3e4c3cfba69c93efd5f1';
```

This statement is environment-specific. It should not be run blindly on another
database.

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

After first cutover this must succeed with 0001-0011 applied and 0012 listed only
in `migrations.pending_contracts`. Runtime strict preflight is separate: it also
requires governed runtime tables, the property-filter source, and non-empty
property-filter data.

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

The query is expected to return 0001-0011 but not 0012. Do not insert the 0012
ledger row manually. A future maintenance-window contract operation must create
its own backup and archive evidence before it changes this expectation.

Expected property filter row count for the current standardized CSV is
`615159`.

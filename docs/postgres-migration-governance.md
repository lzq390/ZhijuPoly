# Postgres Migration Governance

This project treats Postgres migrations as an append-only runtime contract. A
checksum mismatch is a blocker until the schema and migration history are
reconciled explicitly.

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
python -m app.postgres_preflight --mode runtime --strict
```

Then import the standardized property filter dataset:

```bash
cd backend
python -m app.import_postgres --dataset property_filter
```

The import replaces only `core.polymer_property_filter_records`. It must not
truncate or rebuild `core.polymer_properties`, knowledge, DFT, PI, or monomer MD
runtime tables.

## Acceptance Checks

Run these checks before using the database browser property filter module:

```sql
SELECT version, checksum
FROM governance.schema_migrations
WHERE version IN (
  '0001_initial_core',
  '0002_knowledge_and_analytics',
  '0003_runtime_postgres_cutover',
  '0004_monomer_md_jobs',
  '0005_byteff2_formal_monomer_md',
  '0006_property_filter_records'
)
ORDER BY version;

SELECT COUNT(*) AS row_count
FROM core.polymer_property_filter_records;

SELECT logical_name, status, row_count
FROM governance.source_files
WHERE logical_name = 'property_filter_csv';

SELECT dataset, status, row_count
FROM governance.import_batches
WHERE dataset = 'property_filter'
ORDER BY finished_at DESC NULLS LAST, started_at DESC
LIMIT 1;
```

Expected property filter row count for the current standardized CSV is
`615159`.

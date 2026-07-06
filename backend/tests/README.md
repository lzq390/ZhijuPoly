# Backend Test Environment

Backend tests are Postgres-only. They do not fall back to SQLite runtime paths.

## Required Environment

Run the backend suite from the `screen312` environment:

```bash
cd backend
/home/lzq390/miniconda3/envs/screen312/bin/python -m pytest
```

`Settings().app_postgres_dsn` must resolve to a reachable Postgres server. The
configured role must be able to create and drop temporary databases because the
session fixture creates an isolated database named `zhijupoly_test_<pid>_<suffix>`,
applies `backend/migrations/postgres/*.sql`, resets seeded rows before each test,
and drops the temporary database at teardown.

The default DSN comes from `APP_POSTGRES_DSN` or `backend/.env`. On screen312 it
is expected to point at the local Nexpoly Postgres service.

## Common Failures

- Connection refused or name resolution failures: start Postgres or fix
  `APP_POSTGRES_DSN`.
- Permission denied for `CREATE DATABASE` or `DROP DATABASE`: grant the test role
  database creation privileges or use a Postgres role that already has them.
- Migration checksum errors: the target database has a migration version recorded
  with different SQL contents; create a fresh test database or reconcile the
  migration history.

Do not make backend tests silently skip Postgres integration coverage. Data
governance, runtime routes, and repository tests must exercise Postgres schemas.
SQLite is only valid for legacy import and migration-source tests.

## Deployment Gate

Docker startup uses the same Postgres-only contract. `postgres-init` runs:

```bash
python -m app.import_postgres --dataset all --refresh-analytics-snapshot
```

It intentionally does not pass `--rebuild`. Runtime healthchecks should use
`python -m app.postgres_preflight --strict` so a missing migration or runtime
schema fails before the backend is considered healthy.

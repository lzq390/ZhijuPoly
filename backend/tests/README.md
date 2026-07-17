# Backend Test Environment

Backend tests are Postgres-only. They do not fall back to SQLite runtime paths.

## Required Environment

CI installs the Python 3.11 hash lock and runs all modules in three deterministic
PostgreSQL shards. Locally, use any isolated Python 3.11 venv installed from the
same lock:

```bash
python3.11 -m venv .venv-ci
.venv-ci/bin/python -m pip install --require-hashes -r backend/requirements-ci.lock
APP_POSTGRES_DSN=postgresql://<test-role>:<password>@127.0.0.1:5432/<admin-db> \
  .venv-ci/bin/python -m pytest backend/tests
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

Automatic code deployment runs only compatible migrations:

```bash
python -m app.postgres_migrations --mode expand
```

The single `ci.yml` pipeline reads the immutable asset digest from schema-v2
`release-input.json`, whose dataset list must be empty. Asset pointer changes
never invoke the importer. Standalone static maintenance may use an explicit
dataset or `all` (which expands only to static datasets); its rebuild table list
excludes business-mutable relations and never uses `CASCADE`. Runtime
healthchecks use:

```bash
python -m app.postgres_preflight --mode runtime --strict \
  --expected-source-sha <sha>
```

A missing migration, analytics snapshot, or runtime schema therefore fails
before the Backend is considered healthy.

# Local literature publication core

The existing reviewed activation sequence is separated from the source-reader
and object-store imports. A caller supplies an authorized connection and owns
its lifetime. The core validates UUID, generation status, current normalizer,
partial-data approval and document counts before mutating active flags, inside
one explicit transaction. Publisher and catalog guards use ascending parent
identity locks before flag changes.

These synthetic tests import the actual module and exercise transaction rollback,
lock-order inversion, idempotency and rejection before mutation. They do not
establish real PostgreSQL concurrency or readiness. This module has no CLI or API
entry point, opens no connections, and does not enable deferred migration 0024.
The old import CLI and source/asset dependency integration remain separate.

The supplied connection must return mapping rows (for example psycopg dict_row);
the core does not configure or replace the caller-owned connection row factory.

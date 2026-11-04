"""Explicit membership migration; read-only preview unless --apply is supplied."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re

import psycopg
from psycopg.rows import dict_row

from app.auth.isolation_ledger import expected_isolation_ledger, validate_application_migration_ledger
from app.literature_contract import LITERATURE_MIGRATION
from app.services.billing_schema import validate_billing_schema
from task_usage.billing_contract import BILLING_VERSION, BILLING_CHECKSUM


def migrate_memberships(dsn: str, *, apply: bool = False):
    path = Path(__file__).resolve().parents[1] / 'migrations/postgres' / (BILLING_VERSION + '.sql')
    source = path.read_bytes()
    if hashlib.sha256(source).hexdigest() != BILLING_CHECKSUM:
        raise ValueError('Membership migration checksum differs from this release')
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as connection:
        if not apply:
            connection.execute('SET TRANSACTION READ ONLY')
        connection.execute("SET LOCAL lock_timeout='5s'")
        connection.execute("SET LOCAL statement_timeout='60s'")
        if apply:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('nexpoly-membership-migration',0))")
            connection.execute('LOCK TABLE governance.schema_migrations IN SHARE ROW EXCLUSIVE MODE')
        observed = validate_application_migration_ledger(connection, require_usage=False, require_entitlements=False)
        if observed == expected_isolation_ledger(BILLING_VERSION):
            validate_billing_schema(connection)
            return {'version': BILLING_VERSION, 'mode': 'apply' if apply else 'preview', 'written': False, 'already_applied': True}
        if observed != expected_isolation_ledger(LITERATURE_MIGRATION):
            raise ValueError('Membership migration requires the exact complete 0024 prefix')
        from app.auth.schema import validate_isolation_schema
        from app.auth.isolation_ledger import validate_service_members
        from app.services.entitlement_schema import validate_entitlement_schema
        validate_isolation_schema(connection, LITERATURE_MIGRATION)
        validate_service_members(connection)
        validate_entitlement_schema(connection)
        if connection.execute("SELECT to_regnamespace('billing') AS schema").fetchone()['schema'] is not None:
            raise ValueError('Unexpected billing schema before membership migration')
        if not apply:
            return {'version': BILLING_VERSION, 'mode': 'preview', 'written': False, 'would_apply': True}
        # Serialize legacy account classification with registration and explicit
        # tier writes; no public free account can slip into the legacy snapshot.
        connection.execute('LOCK TABLE auth.users IN SHARE ROW EXCLUSIVE MODE')
        connection.execute('LOCK TABLE usage_entitlements.owner_entitlements IN SHARE ROW EXCLUSIVE MODE')
        connection.execute(source.decode())
        connection.execute('INSERT INTO governance.schema_migrations(version,checksum) VALUES(%s,%s)',
                           (BILLING_VERSION, BILLING_CHECKSUM))
        validate_application_migration_ledger(connection, require_usage=False, require_entitlements=False)
        return {'version': BILLING_VERSION, 'mode': 'apply', 'written': True, 'already_applied': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dsn-env', default='AUTH_ADMIN_POSTGRES_DSN')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Z_][A-Z0-9_]{0,127}', args.dsn_env) or not os.environ.get(args.dsn_env):
        print(json.dumps({'ok': False, 'code': 'membership_migration_configuration'}))
        return 2
    try:
        result = migrate_memberships(os.environ[args.dsn_env], apply=args.apply)
    except Exception:
        print(json.dumps({'ok': False, 'code': 'membership_migration_failed'}))
        return 3
    print(json.dumps({'ok': True, **result}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

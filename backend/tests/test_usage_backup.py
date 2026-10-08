"""Exact 0020 backups on disposable synthetic PG, with offline matching clients."""
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import pytest

from app.auth.backup import backup_and_verify,validate_backup_schema
from app.auth.usage_backup import usage_backup_seal
from app.migration_policy import validate_migration_manifest_entries
from task_usage.ledger import record_event
from test_usage_repository import usage_db,enabled,event,A,ROOT


@pytest.fixture(scope='module')
def ledgered_usage_db(usage_db):
    with psycopg.connect(usage_db) as connection:
        connection.execute('CREATE TABLE IF NOT EXISTS governance.schema_migrations(version text PRIMARY KEY,checksum text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())')
        for item in validate_migration_manifest_entries(ROOT/'backend/migrations/postgres'):
            if item.version > "0020_usage_metering":
                continue  # usage_db deliberately applies only this historical prefix.
            connection.execute('INSERT INTO governance.schema_migrations(version,checksum) VALUES(%s,%s)',(item.version,item.checksum))
    yield usage_db


def test_backup_0020_is_explicit_and_rejects_unknown_ledger(ledgered_usage_db):
    with psycopg.connect(ledgered_usage_db,row_factory=dict_row) as connection:
        result=validate_backup_schema(connection)
        assert result['current_readiness'] and result['usage_schema']['validated']
        assert result['migration_ledger'][-1]['version']=='0020_usage_metering'
        connection.execute("INSERT INTO governance.schema_migrations(version,checksum) VALUES('9999_unknown',%s)",('0'*64,))
        with pytest.raises(ValueError):validate_backup_schema(connection)
        connection.rollback()


def test_usage_seal_has_no_row_values_and_is_timezone_stable(ledgered_usage_db):
    value=event()
    with psycopg.connect(ledgered_usage_db,row_factory=dict_row) as connection:
        record_event(connection,**value)
        first=usage_backup_seal(connection)
        connection.execute("SET LOCAL TIME ZONE 'Pacific/Honolulu'")
        second=usage_backup_seal(connection)
        assert first==second
        rendered=json.dumps(first)
        assert A not in rendered and value['task_id'] not in rendered and value['event_key'] not in rendered
        assert set(first['tables'])=={'usage.events','usage.attempts'}
        assert all(set(row)=={'rows','sha256','metadata_sha256'} for row in first['tables'].values())
        connection.rollback()


def test_usage_seal_detects_content_and_metadata_changes(ledgered_usage_db):
    value=event()
    with psycopg.connect(ledgered_usage_db,row_factory=dict_row) as connection:
        record_event(connection,**value)
        before=usage_backup_seal(connection)
        with connection.transaction(force_rollback=True):
            # Simulate privileged storage corruption, then restore the guard;
            # source/restore content seal must still detect the altered data.
            connection.execute('ALTER TABLE usage.attempts DISABLE TRIGGER usage_projection_derived')
            connection.execute('UPDATE usage.attempts SET artifact_bytes=321 WHERE task_id=%s',(value['task_id'],))
            connection.execute('ALTER TABLE usage.attempts ENABLE TRIGGER usage_projection_derived')
            after=usage_backup_seal(connection)
            assert after['tables']['usage.attempts']['sha256']!=before['tables']['usage.attempts']['sha256']
            assert after['tables']['usage.events']==before['tables']['usage.events']
        with connection.transaction(force_rollback=True):
            connection.execute('CREATE INDEX usage_test_extra_index ON usage.events(source)')
            after=usage_backup_seal(connection)
            assert after['tables']['usage.events']['metadata_sha256']!=before['tables']['usage.events']['metadata_sha256']
            assert after['tables']['usage.events']['sha256']==before['tables']['usage.events']['sha256']
        connection.rollback()


def test_0020_real_dump_restore_preserves_ledger_and_usage(ledgered_usage_db,tmp_path):
    binary=Path(os.environ.get('POSTGRES_BIN','/usr/lib/postgresql/16/bin'))
    if not (binary/'pg_dump').exists() or not (binary/'pg_restore').exists():
        pytest.skip('Offline matching PostgreSQL clients are required')
    name='usage_restore_'+uuid4().hex
    restore_dsn=make_conninfo(ledgered_usage_db,dbname=name)
    with psycopg.connect(ledgered_usage_db,autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        with psycopg.connect(ledgered_usage_db,row_factory=dict_row) as connection:
            record_event(connection,**event(status='completed',metrics={'artifact_bytes':0,'observed_wall_ms':5}))
            expected=usage_backup_seal(connection)
        receipt=backup_and_verify(ledgered_usage_db,restore_dsn,tmp_path/'verified')
        result=json.loads(receipt.read_text())
        assert result['restore_verified'] and result['usage_state']==expected
        assert result['isolation_schema']['migration_ledger'][-1]['version']=='0020_usage_metering'
        with psycopg.connect(restore_dsn,row_factory=dict_row) as connection:
            assert usage_backup_seal(connection)==expected
            assert validate_backup_schema(connection)['migration_ledger']==result['isolation_schema']['migration_ledger']
    finally:
        with psycopg.connect(ledgered_usage_db,autocommit=True) as connection:
            connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))

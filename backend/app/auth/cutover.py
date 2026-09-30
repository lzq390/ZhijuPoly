"""Offline identity cutover. Never invoked by ordinary bootstrap/deployment."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.migration_policy import validate_migration_manifest_entries
from app.postgres_migrations import MIGRATIONS_DIR
from .isolation_ledger import CURRENT_VERSION, expected_isolation_ledger, validate_service_members

VERSION = '0018_user_isolation_cutover'
TABLES = ('online_knowledge.history','online_knowledge.jobs','md.monomer_md_jobs',
          'monomer_dft.jobs','polymerization_batch.imports','polymerization_batch.jobs')
BACKUP_TABLES = (*TABLES,'monomer_dft.job_attempts','monomer_dft.artifacts','polymerization_batch.chunks',
                'polymerization_batch.worker_status','auth.users','auth.sessions')


def backup_state_seal(connection):
    """Complete restoration seal; includes private child rows and credentials.

    Used only by the privileged backup operator, not the read-only audit role.
    Output contains aggregate SHA256/counts, never row values or secret hashes.
    """
    seal = {}
    for relation in BACKUP_TABLES:
        digest,count = hashlib.sha256(),0
        query = sql.SQL('SELECT to_jsonb(t)::text AS value FROM {} t ORDER BY 1').format(sql.Identifier(*relation.split('.')))
        with connection.cursor(name='complete_backup_seal') as cursor:
            cursor.execute(query)
            for row in cursor:
                value = row['value'].encode('utf-8')
                digest.update(len(value).to_bytes(8,'big'))
                digest.update(value)
                count += 1
        seal[relation] = {'rows':count,'sha256':digest.hexdigest()}
    return seal


def private_data_seal(connection):
    """Only counts/digests escape this function; no values/credentials in audit."""
    seal = {}
    for relation in TABLES:
        query = sql.SQL('''SELECT encode(sha256(convert_to((to_jsonb(t)-'owner_user_id'-'start_authorized_at')::text,'UTF8')),'hex') digest
            FROM {} t ORDER BY digest''').format(sql.Identifier(*relation.split('.')))
        digest, count = hashlib.sha256(), 0
        with connection.cursor(name='identity_seal') as cursor:
            cursor.execute(query)
            for row in cursor:
                digest.update(row['digest'].encode('ascii')+b'\n')
                count += 1
        seal[relation] = {'rows':count,'sha256':digest.hexdigest()}
    return seal


def auth_metadata_seal(connection):
    seal = {}
    for relation,secret in (('auth.users','password_hash'),('auth.sessions','token_hash')):
        digest,count = hashlib.sha256(),0
        query = sql.SQL('SELECT (to_jsonb(t)-%s)::text AS value FROM {} t ORDER BY 1').format(sql.Identifier(*relation.split('.')))
        with connection.cursor(name='auth_metadata_seal') as cursor:
            cursor.execute(query,(secret,))
            for row in cursor:
                value = row['value'].encode('utf-8')
                digest.update(len(value).to_bytes(8,'big'))
                digest.update(value)
                count += 1
        seal[relation] = {'rows':count,'sha256':digest.hexdigest()}
    return seal


def apply_identity_cutover(dsn: str, owner_user_id: str, *, expected_business_data: dict | None = None,
                           expected_auth_metadata: dict | None = None, expected_backup_state: dict | None = None,
                           expected_assets: dict | None = None) -> dict:
    owner = str(UUID(owner_user_id))
    entries = validate_migration_manifest_entries(MIGRATIONS_DIR)
    target = next(entry for entry in entries if entry.version == VERSION)
    prefix = {entry.version: entry.checksum for entry in entries if entry.version < VERSION}
    with psycopg.connect(dsn,row_factory=dict_row,connect_timeout=5) as connection:
        connection.execute("SET LOCAL lock_timeout='10s'")
        connection.execute("SET LOCAL statement_timeout='10min'")
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('nexpoly-identity-cutover',0))")
        rows = [dict(row) for row in connection.execute('SELECT version,checksum FROM governance.schema_migrations ORDER BY version')]
        if rows == expected_isolation_ledger(CURRENT_VERSION):
            # Do not run historical 0018 grants or read any data seal on 0019.
            from .schema import validate_isolation_schema
            validate_isolation_schema(connection)
            validate_service_members(connection)
            return {'version':VERSION,'already_applied':True,'current_version':CURRENT_VERSION,
                    'current_readiness':False}
        ledger = {row['version']:row['checksum'] for row in rows}
        if len(ledger) != len(rows):
            raise RuntimeError('Duplicate identity migration ledger entry')
        if ledger == {**prefix,VERSION:target.checksum}:
            return {'version':VERSION,'already_applied':True,'current_readiness':False}
        if ledger != prefix:
            raise RuntimeError('Cutover requires the exact canonical migration ledger through 0017')
        # Stable row set throughout the ownership change; API/Workers must also
        # be stopped, as acknowledged by the maintenance CLI operator.
        connection.execute('LOCK TABLE auth.users IN SHARE ROW EXCLUSIVE MODE')
        connection.execute('LOCK TABLE auth.sessions IN SHARE ROW EXCLUSIVE MODE')
        for relation in TABLES:
            connection.execute(sql.SQL('LOCK TABLE {} IN ACCESS EXCLUSIVE MODE').format(sql.Identifier(*relation.split('.'))))
        for relation in BACKUP_TABLES:
            if relation not in TABLES and not relation.startswith('auth.'):
                connection.execute(sql.SQL('LOCK TABLE {} IN ACCESS EXCLUSIVE MODE').format(sql.Identifier(*relation.split('.'))))
        if expected_backup_state is not None and backup_state_seal(connection) != expected_backup_state:
            raise RuntimeError('Restorable data changed after the verified backup; create a new backup/restore receipt')
        if expected_assets is not None:
            from .assets import validate_assets
            validate_assets(expected_assets,verify_source=True)
        before = private_data_seal(connection)
        if expected_business_data is not None and before != expected_business_data:
            raise RuntimeError('Source business data changed after the verified backup; create a new backup/restore receipt')
        if expected_auth_metadata is not None and auth_metadata_seal(connection) != expected_auth_metadata:
            raise RuntimeError('Account/session metadata changed after the verified backup; create a new backup/restore receipt')
        connection.execute("SELECT set_config('nexpoly.legacy_owner',%s,true)",(owner,))
        connection.execute((MIGRATIONS_DIR/(VERSION+'.sql')).read_text())
        after = private_data_seal(connection)
        if before != after:
            raise RuntimeError('Unexpected private business-data change during cutover')
        for relation in TABLES:
            row = connection.execute(sql.SQL('SELECT count(*) n FROM {} WHERE owner_user_id IS NULL').format(sql.Identifier(*relation.split('.')))).fetchone()
            if row['n']:
                raise RuntimeError('Null private owner after cutover')
        connection.execute('INSERT INTO governance.schema_migrations(version,checksum) VALUES(%s,%s)',(VERSION,target.checksum))
        return {'version':VERSION,'checksum':target.checksum,'owner_user_id':owner,'business_data':after,'already_applied':False}


def validate_backup_receipt(path: Path, dsn: str):
    from .assets import file_sha256, validate_assets, ASSET_TABLES
    receipt = json.loads(path.read_text())
    backup = Path(receipt['backup_path'])
    if not receipt.get('restore_verified') or file_sha256(backup) != receipt.get('backup_sha256'):
        raise ValueError('A verified, unchanged backup/restore receipt is required')
    with psycopg.connect(dsn,row_factory=dict_row) as connection:
        identity = connection.execute('SELECT current_database() name,system_identifier::text system_id FROM pg_control_system()').fetchone()
        if receipt['source'] != identity:
            raise ValueError('Backup receipt belongs to a different source database')
    if not isinstance(receipt.get('business_data'),dict):
        raise ValueError('Backup receipt is missing its verified business-data seal')
    if not isinstance(receipt.get('auth_metadata'),dict):
        raise ValueError('Backup receipt is missing its verified account/session metadata seal')
    if not isinstance(receipt.get('backup_state'),dict):
        raise ValueError('Backup receipt is missing its complete restorable-data seal')
    if not isinstance(receipt.get('assets'),dict):
        raise ValueError('Backup receipt is missing its file restoration evidence')
    if any(receipt['backup_state'][table]['rows'] and name not in receipt['assets'] for name,table in ASSET_TABLES.items()):
        raise ValueError('Existing private resources are missing their file backup')
    validate_assets(receipt['assets'],verify_source=False)
    return receipt


def file_inventory(dsn: str, roots: dict[str,Path]) -> dict:
    """Unowned legacy directories remain inaccessible; report IDs, never contents."""
    mapping = {'batch_imports':('polymerization_batch.imports','id'),
               'batch_jobs':('polymerization_batch.jobs','id'),
               'md':('md.monomer_md_jobs','job_id'),'dft':('monomer_dft.jobs','job_id')}
    report = {}
    with psycopg.connect(dsn) as connection:
        for name, root in roots.items():
            relation,key = mapping[name]
            ids = {str(row[0]).replace('-','') if name.startswith('batch') else str(row[0]) for row in connection.execute(sql.SQL('SELECT {} FROM {}').format(sql.Identifier(key),sql.Identifier(*relation.split('.'))))}
            directories = {item.name for item in root.iterdir() if item.is_dir() or item.is_symlink()} if root.exists() else set()
            report[name] = {'root':str(root.resolve()),'associated_directories':len(ids & directories),
                            'unassociated_directories':sorted(directories-ids)}
    return report


def main():
    parser = argparse.ArgumentParser(description='离线切换用户归属；失败保持业务入口关闭，禁止回滚无鉴权版本')
    parser.add_argument('--dsn-env',default='AUTH_ADMIN_POSTGRES_DSN')
    parser.add_argument('--legacy-owner',required=True,type=lambda value:str(UUID(value)))
    parser.add_argument('--backup-receipt',required=True,type=Path)
    parser.add_argument('--writers-stopped',required=True,action='store_true')
    parser.add_argument('--audit-output',required=True,type=Path)
    parser.add_argument('--batch-root',type=Path)
    parser.add_argument('--md-root',type=Path)
    parser.add_argument('--dft-root',type=Path)
    args = parser.parse_args()
    dsn = os.environ[args.dsn_env]
    receipt = validate_backup_receipt(args.backup_receipt,dsn)
    roots = {name:Path(item['source_root']) for name,item in receipt['assets'].items()}
    supplied = {}
    if args.batch_root:
        supplied.update(batch_imports=args.batch_root/'imports',batch_jobs=args.batch_root/'jobs')
    if args.md_root: supplied['md'] = args.md_root
    if args.dft_root: supplied['dft'] = args.dft_root
    if any(name not in roots or path.resolve() != roots[name].resolve() for name,path in supplied.items()):
        raise ValueError('Inventory roots must match the verified file backup')
    inventory = file_inventory(dsn,roots)
    # Reserve the evidence path before changing the database. Never overwrite an
    # earlier cutover record or discover an unwritable output after committing.
    descriptor = os.open(args.audit_output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w') as audit:
        result = apply_identity_cutover(dsn,args.legacy_owner,expected_business_data=receipt['business_data'],
                                       expected_auth_metadata=receipt['auth_metadata'],expected_backup_state=receipt['backup_state'],expected_assets=receipt['assets'])
        result['files'] = inventory
        audit.write(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
        audit.flush()
        os.fsync(audit.fileno())
    print(f'完成 {VERSION}；验收记录：{args.audit_output}')


if __name__ == '__main__':
    main()

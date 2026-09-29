"""Create a private dump and verify restore into an explicitly empty database."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import re

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from .cutover import private_data_seal, auth_metadata_seal, backup_state_seal
from .assets import archive_assets, file_sha256


def postgres_environment(dsn: str):
    environment = dict(os.environ)
    mappings = {'host':'PGHOST','port':'PGPORT','dbname':'PGDATABASE','user':'PGUSER',
                'password':'PGPASSWORD','sslmode':'PGSSLMODE','service':'PGSERVICE',
                'passfile':'PGPASSFILE','connect_timeout':'PGCONNECT_TIMEOUT'}
    for key,value in conninfo_to_dict(dsn).items():
        if key not in mappings:
            raise ValueError(f'Unsupported backup connection option: {key}')
        environment[mappings[key]] = value
    return environment


def backup_and_verify(source_dsn: str, restore_dsn: str, directory: Path, *, asset_roots: dict[str,Path] | None = None):
    if any(directory.resolve().is_relative_to(root.resolve()) for root in (asset_roots or {}).values()):
        raise ValueError('Backup output must be outside every source asset directory')
    directory.mkdir(parents=True,exist_ok=False,mode=0o700)
    dump = directory/'database.dump'
    with psycopg.connect(source_dsn,row_factory=dict_row) as source, psycopg.connect(restore_dsn,row_factory=dict_row) as restored:
        identity_sql = 'SELECT current_database() name,system_identifier::text system_id FROM pg_control_system()'
        source_id,restore_id = source.execute(identity_sql).fetchone(),restored.execute(identity_sql).fetchone()
        if source_id == restore_id:
            raise ValueError('Restore target must differ from the source database')
        if restored.execute("SELECT 1 FROM pg_namespace WHERE nspname NOT IN ('public','information_schema') AND nspname NOT LIKE 'pg_%' LIMIT 1").fetchone():
            raise ValueError('Restore target must be empty')
        if restored.execute("SELECT 1 FROM pg_class WHERE relnamespace='public'::regnamespace LIMIT 1").fetchone():
            raise ValueError('Restore target must have no public relations')
        source_seal = private_data_seal(source)
        source_auth_seal = auth_metadata_seal(source)
        source_backup_seal = backup_state_seal(source)
        major = source.info.server_version // 10000
    binary_dir = Path(os.environ.get('POSTGRES_BIN',f'/usr/lib/postgresql/{major}/bin'))
    dump_command = str(binary_dir/'pg_dump') if binary_dir.exists() else 'pg_dump'
    restore_command = str(binary_dir/'pg_restore') if binary_dir.exists() else 'pg_restore'
    for command in (dump_command,restore_command):
        version = subprocess.check_output([command,'--version'],text=True)
        if int(re.search(r'(\d+)\.',version).group(1)) != major:
            raise ValueError('Backup and restore client major version must match the source server')
    # Do not put credentials in subprocess arguments or diagnostic output.
    descriptor = os.open(dump,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'wb') as output:
        subprocess.run([dump_command,'--format=custom','--no-owner'],env=postgres_environment(source_dsn),stdout=output,check=True)
    subprocess.run([restore_command,'--exit-on-error','--no-owner','--dbname='+conninfo_to_dict(restore_dsn)['dbname'],str(dump)],
                   env=postgres_environment(restore_dsn),check=True)
    with psycopg.connect(restore_dsn,row_factory=dict_row) as restored:
        restore_seal = private_data_seal(restored)
        restore_auth_seal = auth_metadata_seal(restored)
        restore_backup_seal = backup_state_seal(restored)
        cutover = restored.execute("SELECT 1 FROM governance.schema_migrations WHERE version='0018_user_isolation_cutover'").fetchone()
        if cutover:
            from .schema import validate_isolation_schema
            validate_isolation_schema(restored)
    if source_seal != restore_seal or source_auth_seal != restore_auth_seal or source_backup_seal != restore_backup_seal:
        raise RuntimeError('Backup restored with a different private business-data seal')
    assets = archive_assets(asset_roots or {},directory,source_backup_seal)
    result = {'source':source_id,'restore_target':restore_id,'backup_path':str(dump.resolve()),
              'backup_sha256':file_sha256(dump),
              'restore_verified':True,'business_data':source_seal,'auth_metadata':source_auth_seal,'backup_state':source_backup_seal,'assets':assets}
    receipt = directory/'receipt.json'
    receipt.write_text(json.dumps(result,indent=2)+'\n')
    receipt.chmod(0o600)
    return receipt


def main():
    parser = argparse.ArgumentParser(description='离线备份并恢复到显式指定的空测试库；不输出账号秘密')
    parser.add_argument('--source-dsn-env',default='AUTH_ADMIN_POSTGRES_DSN')
    parser.add_argument('--restore-dsn-env',default='AUTH_RESTORE_TEST_DSN')
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--asset-root',action='append',default=[],metavar='NAME=PATH',help='Repeat for batch_imports, batch_jobs, md, dft')
    args = parser.parse_args()
    roots = {name:Path(path) for name,path in (value.split('=',1) for value in args.asset_root)}
    receipt = backup_and_verify(os.environ[args.source_dsn_env],os.environ[args.restore_dsn_env],args.output,asset_roots=roots)
    print(f'备份恢复验证通过：{receipt}')


if __name__ == '__main__':
    main()

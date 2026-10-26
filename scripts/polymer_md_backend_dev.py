#!/usr/bin/env python3
"""Read-only preflight and loopback serve adapter for an existing private MD dev identity.

This module does not provision accounts, change passwords, initialize databases,
install services or dispatch scientific work. Serving requires a separate
operator decision and the normal platform startup contracts.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT)]
DATABASE = "nexpoly_polymer_md_dev"
ORIGIN = "http://127.0.0.1:39871"
ROLE_PREFIX = "polymer_md_local_dev_"


MAX_PRIVATE_BYTES = 1024 * 1024


def _parent_descriptor(path: Path):
    path = Path(os.path.abspath(path))
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("runtime configuration parent must be private and owned")
        return path, descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _private_identity(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > MAX_PRIVATE_BYTES):
        raise ValueError("runtime configuration must be a bounded private owned regular file")
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_private(path: Path):
    """Read one bounded inode; reject links, identity changes and public access."""
    parent = descriptor = current_parent = None
    try:
        path, parent = _parent_descriptor(path)
        parent_info = os.fstat(parent)
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=parent)
        expected = _private_identity(os.fstat(descriptor))
        data = bytearray()
        while len(data) <= MAX_PRIVATE_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_PRIVATE_BYTES + 1 - len(data)))
            if not chunk: break
            data.extend(chunk)
        if len(data) > MAX_PRIVATE_BYTES or _private_identity(os.fstat(descriptor)) != expected:
            raise ValueError("runtime configuration changed while reading")
        _, current_parent = _parent_descriptor(path)
        now = os.fstat(current_parent)
        if (now.st_dev, now.st_ino) != (parent_info.st_dev, parent_info.st_ino):
            raise ValueError("runtime configuration parent identity changed")
        if _private_identity(os.stat(path.name, dir_fd=current_parent, follow_symlinks=False)) != expected:
            raise ValueError("runtime configuration path identity changed")
        return data.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("runtime configuration is not a stable private owned file") from None
    finally:
        for fd in (current_parent, descriptor, parent):
            if fd is not None: os.close(fd)


def full_app_settings(runtime: Path, *, asset_root: Path | None = None):
    """Prepare the complete app without adopting any shared database identity."""
    from psycopg.conninfo import conninfo_to_dict
    # Explicit private DSNs must not inherit libpq routing or credential files.
    blocked = ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS", "PGPASSFILE", "PGPASSWORD")
    if any(key in os.environ for key in blocked):
        raise ValueError("full application refuses inherited libpq routing or credentials")
    config=json.loads(read_private(runtime/'backend-private.json'))
    flags={key:value for line in read_private(runtime/'dev.env').splitlines() if line and not line.startswith('#') for key,_,value in [line.partition('=')]}
    if config.get('database')!=DATABASE or flags.get('POLYMER_MD_ENVIRONMENT')!='dev' or flags.get('POLYMER_MD_DEV_ENABLED')!='true':
        raise ValueError('full application requires the existing private development identity')
    for kind in ('api','auth','service'):
        info=conninfo_to_dict(config['dsns'][kind])
        if set(info) != {'host', 'port', 'dbname', 'user', 'password'} or not info.get('password'):
            raise ValueError('full application requires only canonical task connection fields')
        if (info.get('host')!='127.0.0.1' or info.get('port')!='32952' or info.get('dbname')!=DATABASE
                or info.get('user')!=ROLE_PREFIX+kind):
            raise ValueError('full application refuses a non-task database or role')
    frontend_origin=os.environ.get('POLYMER_MD_DEV_FRONTEND_ORIGIN','')
    if frontend_origin and frontend_origin!='http://127.0.0.1:39872':
        raise ValueError('the optional frontend origin must be exact loopback http://127.0.0.1:39872')
    origins=','.join(value for value in (ORIGIN,frontend_origin) if value)
    # This is a whole-application integration host, not a second set of model
    # workers. Never inherit a shared model/Worker URL or database credential.
    os.environ.update({
        'APP_POSTGRES_DSN':config['dsns']['api'],'PI_POSTGRES_DSN':config['dsns']['api'],
        'LAB_DATA_POSTGRES_DSN':config['dsns']['api'],'AUTH_POSTGRES_DSN':config['dsns']['auth'],
        'APP_SERVICE_POSTGRES_DSN':config['dsns']['service'],
        'AUTH_COOKIE_NAME':'nexpoly_dev_polymer_md_session','AUTH_COOKIE_SECURE':'false',
        'AUTH_DEV_HTTP_ORIGINS':'','ALLOWED_ORIGINS':origins,
        'STRUCTURED_DATA_BACKEND':'postgres','PI_REVERSE_BACKEND':'postgres',
        'GPU_PRELOAD_MODE':'lazy','GPU_BROKER_ENABLED':'false','DEV_GPU_OPERATOR_ENABLED':'false',
        'MODEL_ENABLED':'false','OCSR_ENABLED':'false','GEN_MODEL_ENABLED':'false',
        'RETRO_MODEL_ENABLED':'false','POLYTAO_ENABLED':'false','SMIPOLY_ENABLED':'false',
        'MONOMER_MD_SUBMIT_ENABLED':'false','MONOMER_MD_WORKER_BASE_URL':'',
        'MONOMER_DFT_SUBMIT_ENABLED':'false','MONOMER_DFT_WORKER_BASE_URL':'','MONOMER_DFT_WORKER_UDS':'',
        'MONOMER_MD_JOB_RETENTION_ENABLED':'false','MONOMER_DFT_JOB_RETENTION_ENABLED':'false',
        'MONOMER_POLYMERIZATION_BATCH_ENABLED':'false','DEPLOYMENT_DRAIN_ENABLED':'true',
        'POLYMER_MD_DEV_ENABLED':'true','POLYMER_MD_ENVIRONMENT':'dev',
        'POLYMER_MD_SUBMIT_ENABLED':flags.get('POLYMER_MD_SUBMIT_ENABLED','false'),
        'POLYMER_MD_WORKER_UDS':flags['POLYMER_MD_WORKER_UDS'],
        'POLYMER_MD_START_AUTHORIZATION_TOKEN':flags['POLYMER_MD_START_AUTHORIZATION_TOKEN'],
        'POLYMER_MD_ARTIFACT_ROOT':flags.get('POLYMER_MD_ARTIFACT_ROOT',''),
        'POLYMER_MD_DOWNLOAD_SPOOL_ROOT':str(runtime/'downloads'),
        'OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1','MKL_NUM_THREADS':'1',
        'CUDA_VISIBLE_DEVICES':'','NVIDIA_VISIBLE_DEVICES':'none',
    })
    from app.config import Settings,get_settings
    paths={}
    if asset_root is not None:
        asset_root=asset_root.resolve(strict=True)
        if not asset_root.is_dir() or not (asset_root/'database').is_dir():
            raise ValueError('full application asset root must contain the immutable database directory')
        data=asset_root/'database'
        paths={'csv_source_path':str(data/'data1.csv'),
            'property_filter_csv_path':str(data/'PolymerDatabaseV2.0_reliable085_standardized.csv'),
            'knowledge_zip_path':str(data/'data_txt.zip'),
            'experimental_process_csv_path':str(data/'polymer_process_material_filtered_cleaned_office_utf8_bom.csv'),
            'experimental_property_csv_path':str(data/'polymer_property_detail_cleaned_office_utf8_bom.csv'),
            'model_dir':str(asset_root/'model')}
        os.environ.update({key.removesuffix('_path').upper()+'_PATH':value
            for key,value in paths.items() if key!='model_dir'})
        os.environ['MODEL_DIR']=paths['model_dir']
    get_settings.cache_clear()
    return Settings(**paths)

def create_full_app(runtime: Path, *, asset_root: Path | None = None):
    settings=full_app_settings(runtime,asset_root=asset_root)
    from app.main import create_app as create_platform_app
    return create_platform_app(settings)

def preflight_full_app(runtime: Path, *, asset_root: Path | None = None):
    """Read-only full startup prerequisites; never starts lifespan/dispatch."""
    settings=full_app_settings(runtime,asset_root=asset_root)
    from app.auth.context import service_context
    from app.auth.settings import AuthSettings
    from app.auth.service import AuthService
    from app.postgres_preflight import run_preflight,preflight_blockers,SCHEMA_TARGET_CURRENT_ISOLATION,SCHEMA_TARGET_POLYMER_MD,SCHEMA_TARGET_LITERATURE
    auth_settings=AuthSettings.from_settings(settings)
    with service_context(auth_settings):
        report=run_preflight(settings,mode='runtime',strict=True,schema_target=SCHEMA_TARGET_CURRENT_ISOLATION)
        blockers=preflight_blockers(report)
        if report.get("schema_target") not in {SCHEMA_TARGET_POLYMER_MD,SCHEMA_TARGET_LITERATURE}:
            blockers.append("Polymer MD requires a validated current migration boundary")
        AuthService(auth_settings).assert_application_ready()
    # Do not emit the report's DSN/connection diagnostics or asset source rows.
    codes=set()
    for error in blockers:
        text=str(error).lower()
        if 'property filter' in text or 'property_filter' in text:
            codes.add('property_filter_source_or_dataset_not_ready')
        elif 'analytics snapshot' in text:
            codes.add('analytics_snapshot_missing_or_inconsistent')
        elif 'migration' in text or 'schema' in text or 'table' in text:
            codes.add('database_schema_or_migration_not_ready')
        elif 'postgres' in text:
            codes.add('postgres_not_ready')
        else:
            codes.add('runtime_preflight_blocked')
    return {'mode':'full-app','ready':not blockers,'schema_target':report.get('schema_target'),
        'postgres_reachable':bool(report.get('postgres',{}).get('reachable')),
        'property_filter_records':report.get('postgres',{}).get('tables',{}).get('core.polymer_property_filter_records'),
        'analytics_source':report.get('analytics_snapshot',{}).get('source'),
        'assets':{key:bool(value.get('exists')) for key,value in report.get('files',{}).items()},
        'blocker_codes':sorted(codes),'blocker_count':len(blockers),
        'submit_enabled':settings.polymer_md_submit_enabled}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight-full", "serve-full"))
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--asset-root", type=Path)
    args = parser.parse_args()
    if args.action == "preflight-full":
        report = preflight_full_app(args.runtime, asset_root=args.asset_root)
        print(json.dumps(report))
        return 0 if report["ready"] else 1
    app = create_full_app(args.runtime, asset_root=args.asset_root)
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=39871, proxy_headers=False)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"ready": False, "error_type": type(error).__name__}))
        raise SystemExit(1)

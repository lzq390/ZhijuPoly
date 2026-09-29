"""Maintenance-only private file backup; no files are made publicly addressable."""
from __future__ import annotations

import hashlib
from pathlib import Path
import tarfile
import tempfile

ASSET_TABLES = {'batch_imports':'polymerization_batch.imports','batch_jobs':'polymerization_batch.jobs',
                'md':'md.monomer_md_jobs','dft':'monomer_dft.jobs'}


def file_sha256(path: Path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda:handle.read(4*1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()


def asset_snapshot(root: Path):
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f'Asset root must be an existing regular directory: {root}')
    files = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'Asset backup requires resolving symbolic links explicitly: {path}')
        if path.is_file():
            files.append({'path':path.relative_to(root).as_posix(),'bytes':path.stat().st_size,'sha256':file_sha256(path)})
        elif not path.is_dir():
            raise ValueError(f'Unsupported asset entry: {path}')
    return files


def archive_assets(roots: dict[str,Path],directory: Path,backup_state: dict):
    if any(directory.resolve().is_relative_to(root.resolve()) for root in roots.values()):
        raise ValueError('Backup output must be outside every source asset directory')
    missing = [name for name,table in ASSET_TABLES.items() if backup_state[table]['rows'] and name not in roots]
    if missing:
        raise ValueError('Explicit asset roots are required for existing resources: '+','.join(missing))
    result = {}
    for name,root in roots.items():
        if name not in ASSET_TABLES:
            raise ValueError(f'Unknown asset root: {name}')
        root = root.absolute()
        files = asset_snapshot(root)
        archive = directory/(name+'.tar.gz')
        with tarfile.open(archive,'x:gz') as output:
            output.add(root,arcname='assets',recursive=True)
        archive.chmod(0o600)
        with tempfile.TemporaryDirectory(prefix='restore-'+name+'-',dir=directory) as target:
            with tarfile.open(archive,'r:gz') as source:
                source.extractall(target,filter='data')
            if asset_snapshot(Path(target)/'assets') != files or asset_snapshot(root) != files:
                raise RuntimeError('Asset content changed during backup or restore: '+name)
        result[name] = {'source_root':str(root),'archive_path':str(archive.resolve()),
                        'archive_sha256':file_sha256(archive),'files':files}
    return result


def validate_assets(records: dict, *, verify_source: bool):
    for name,item in records.items():
        if name not in ASSET_TABLES or file_sha256(Path(item['archive_path'])) != item['archive_sha256']:
            raise ValueError('Asset backup receipt is invalid')
        if verify_source and asset_snapshot(Path(item['source_root'])) != item['files']:
            raise RuntimeError('Source assets changed after backup; create a new backup/restore receipt')

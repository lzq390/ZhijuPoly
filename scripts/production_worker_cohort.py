#!/usr/bin/env python3
"""Explicit, private production Worker cohort; legacy launch paths stay fixed.

This module validates and launches already provisioned runtimes. It never
creates environments, changes a serving process, migrates a database or
publishes deployment authority.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Mapping
from urllib.parse import urlsplit

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.worker_slot_runtime import inspect_git_checkout, require_private_directory

PRODUCTION_RUNTIME_ROOT = Path('/data/lzq/gith/nexpoly-runtime')
PRODUCTION_SOURCE_ROOT = Path('/data/lzq/gith/nexpoly-release-private')
PRODUCTION_ASSET_RELEASES_ROOT = Path('/data/lzq/nexpoly-assets/releases')
FILE_KEY = 'NEXPOLY_PROD_WORKER_COHORT_FILE'
DIGEST_KEY = 'NEXPOLY_PROD_WORKER_COHORT_SHA256'
SOURCE_KEY = 'NEXPOLY_PROD_WORKER_SOURCE_ROOT'
FIELDS = frozenset({'schema_version', 'cohort_root', 'source_root', 'source_sha', 'source_tree',
    'callback_base_url', 'md_worker_env_sha256', 'dft_runtime_env_sha256',
    'dft_start_authorization_token_sha256', 'dft_toolchain_sha256', 'md_assets_root',
    'md_assets_inventory_sha256'})
DIGEST_RE = re.compile(r'sha256:[0-9a-f]{64}\Z')


class CohortError(RuntimeError):
    """Public-safe failure; private configuration values are never echoed."""


def private_payload(path: Path, *, maximum: int = 65536) -> bytes:
    if path.resolve(strict=True) != path:
        raise CohortError('Production cohort file has a linked ancestor')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or not 0 < before.st_size <= maximum):
            raise CohortError('Production cohort file is not private and regular')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            payload = stream.read(maximum + 1)
        fields = ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        for after in (os.fstat(descriptor), path.lstat()):
            if any(getattr(before, name) != getattr(after, name) for name in fields):
                raise CohortError('Production cohort file changed while reading')
        if len(payload) != before.st_size:
            raise CohortError('Production cohort file changed size')
        return payload
    finally:
        os.close(descriptor)


def sealed_payload(path: Path, expected: str) -> bytes:
    payload = private_payload(path)
    if not isinstance(expected, str) or DIGEST_RE.fullmatch(expected) is None or (
            'sha256:' + hashlib.sha256(payload).hexdigest() != expected):
        raise CohortError('Production cohort file differs from its sealed digest')
    return payload


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CohortError('Production cohort contains duplicate fields')
        result[key] = value
    return result


@dataclass(frozen=True)
class ProductionWorkerCohort:
    path: Path
    digest: str
    root: Path
    source_root: Path
    source_sha: str
    source_tree: str
    callback_base_url: str
    md_worker_env_sha256: str
    dft_runtime_env_sha256: str
    dft_start_authorization_token_sha256: str
    dft_toolchain_sha256: str
    md_assets_root: Path
    md_assets_inventory_sha256: str

    @property
    def md_runtime_root(self):
        return self.root / 'md-runtime'

    @property
    def md_worker_env(self):
        return self.root / 'config/md-worker.env'

    @property
    def dft_runtime_env(self):
        return self.root / 'config/dft-runtime.env'

    @property
    def dft_token_file(self):
        return self.root / 'config/dft-start-authorization.token'

    def validate_md_assets(self):
        from scripts.pull_deploy_controller import directory_inventory_digest
        asset_root = self.md_assets_root
        link = self.md_runtime_root / 'state/current-assets/byteff2'
        if (not link.is_symlink() or os.readlink(link) != str(asset_root)
                or link.lstat().st_uid != os.geteuid()
                or not _canonical_md_assets_root(asset_root)):
            raise CohortError('Candidate MD assets must reference the frozen production asset tree')
        for path in (asset_root, *asset_root.rglob('*')):
            metadata = path.lstat()
            if (metadata.st_uid != os.geteuid() or metadata.st_mode & 0o222
                    or not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode))):
                raise CohortError('Production MD assets are not owner-owned and read-only')
        if directory_inventory_digest(asset_root) != self.md_assets_inventory_sha256:
            raise CohortError('Production MD assets differ from their sealed inventory')

    def toolchain(self):
        from scripts.production_dft_toolchain import load
        return load(self.root / 'config/dft-toolchain.json', self.dft_toolchain_sha256)

    def environment(self):
        return {FILE_KEY: str(self.path), DIGEST_KEY: self.digest, SOURCE_KEY: str(self.source_root)}

    def authorization_token(self):
        token = sealed_payload(self.dft_token_file, self.dft_start_authorization_token_sha256).decode('ascii').removesuffix('\n')
        if re.fullmatch(r'[A-Za-z0-9_-]{32,256}', token) is None:
            raise CohortError('Production cohort DFT authorization token is invalid')
        return token


def _canonical_md_assets_root(path: Path) -> bool:
    return (path.name == 'byteff2' and path.parent.parent == PRODUCTION_ASSET_RELEASES_ROOT
        and re.fullmatch(r'[0-9a-f]{64}', path.parent.name) is not None
        and path.resolve(strict=True) == path)


def load_cohort(path: Path, digest: str) -> ProductionWorkerCohort:
    payload = sealed_payload(path, digest)
    try:
        data = json.loads(payload, object_pairs_hook=_unique_object)
        if type(data) is not dict or set(data) != FIELDS or type(data['schema_version']) is not int or data['schema_version'] != 1:
            raise CohortError('Production cohort shape is invalid')
        root, source = Path(data['cohort_root']), Path(data['source_root'])
        if (root.parent != PRODUCTION_RUNTIME_ROOT / 'state'
                or re.fullmatch(r'p9000-[a-z0-9][a-z0-9-]{7,39}', root.name) is None
                or path != root / 'config/worker-cohort.json'
                or re.fullmatch(r'[0-9a-f]{40}', data['source_sha']) is None
                or re.fullmatch(r'[0-9a-f]{40}', data['source_tree']) is None
                or source != PRODUCTION_SOURCE_ROOT / root.name / data['source_sha']):
            raise CohortError('Production cohort source or root is outside the reviewed layout')
        for directory in (root, root / 'config', PRODUCTION_SOURCE_ROOT, PRODUCTION_SOURCE_ROOT / root.name):
            require_private_directory(directory)
        url = urlsplit(data['callback_base_url'])
        if (url.scheme != 'http' or url.hostname != '127.0.0.1' or url.port is None
                or not 1024 <= url.port <= 65535 or url.username or url.password
                or url.path or url.query or url.fragment
                or data['callback_base_url'] != f'http://127.0.0.1:{url.port}'):
            raise CohortError('Production cohort callback must be one exact loopback origin')
        for key in ('md_worker_env_sha256', 'dft_runtime_env_sha256', 'dft_start_authorization_token_sha256', 'dft_toolchain_sha256', 'md_assets_inventory_sha256'):
            if DIGEST_RE.fullmatch(data[key]) is None:
                raise CohortError('Production cohort input digest is invalid')
        assets = Path(data['md_assets_root'])
        if not _canonical_md_assets_root(assets):
            raise CohortError('Production cohort MD assets are outside the immutable release layout')
        checkout = inspect_git_checkout(source)
        if checkout.source_sha != data['source_sha'] or checkout.source_tree != data['source_tree']:
            raise CohortError('Production cohort source differs from its sealed release')
        cohort = ProductionWorkerCohort(path, digest, root, source, data['source_sha'], data['source_tree'],
            data['callback_base_url'], data['md_worker_env_sha256'], data['dft_runtime_env_sha256'],
            data['dft_start_authorization_token_sha256'], data['dft_toolchain_sha256'], assets,
            data['md_assets_inventory_sha256'])
        socket = cohort.md_runtime_root / 'state/monomer-md-worker-socket/worker.sock'
        if len(os.fsencode(socket)) > 107:
            raise CohortError('Production cohort MD Unix socket path is too long')
        return cohort
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise CohortError('Production cohort configuration is invalid') from exc


def from_environment(environment: Mapping[str, str] | None = None) -> ProductionWorkerCohort | None:
    values = os.environ if environment is None else environment
    if FILE_KEY not in values and DIGEST_KEY not in values and SOURCE_KEY not in values:
        return None
    if not values.get(FILE_KEY) or not values.get(DIGEST_KEY):
        raise CohortError('Production cohort requires both its file and sealed digest')
    cohort = load_cohort(Path(values[FILE_KEY]), values[DIGEST_KEY])
    if SOURCE_KEY in values and values[SOURCE_KEY] != str(cohort.source_root):
        raise CohortError('Production cohort process names a different source')
    return cohort


def md_environment(cohort: ProductionWorkerCohort, inherited=None):
    from scripts import monomer_worker_env, monomer_md_worker_launcher
    cohort.validate_md_assets()
    sealed_payload(cohort.md_worker_env, cohort.md_worker_env_sha256)
    values = monomer_worker_env.load_worker_env(cohort.md_worker_env)
    if (set(values) != {'APP_SERVICE_POSTGRES_DSN', 'USAGE_METERING_ENABLED', 'USAGE_ENTITLEMENTS_ENABLED'}
            or not values['APP_SERVICE_POSTGRES_DSN'] or values['USAGE_METERING_ENABLED'] != 'true'
            or values['USAGE_ENTITLEMENTS_ENABLED'] != 'false'):
        raise CohortError('Production cohort MD service inputs are incomplete')
    sealed_payload(cohort.md_worker_env, cohort.md_worker_env_sha256)
    environment = monomer_worker_env.build_worker_process_environment(values, inherited=inherited,
        overrides=monomer_md_worker_launcher.expected_environment(source_root=cohort.source_root,
            runtime_root=cohort.md_runtime_root))
    environment.update(cohort.environment())
    return environment


def dft_environment(cohort: ProductionWorkerCohort, inherited=None):
    from scripts import monomer_dft_worker_env
    cohort.toolchain()
    sealed_payload(cohort.dft_runtime_env, cohort.dft_runtime_env_sha256)
    values = monomer_dft_worker_env.load_runtime_environment(cohort.dft_runtime_env)
    if values['MONOMER_DFT_RELEASE_SHA'] != cohort.source_sha:
        raise CohortError('Production cohort DFT runtime names a different release')
    sealed_payload(cohort.dft_runtime_env, cohort.dft_runtime_env_sha256)
    token = cohort.authorization_token()
    environment = monomer_dft_worker_env.build_environment(values, inherited)
    environment.update(cohort.environment())
    environment.update({
        'MONOMER_DFT_JOB_ROOT': str(cohort.root / 'dft/jobs'),
        'MONOMER_DFT_WORKER_UDS': str(cohort.root / 'dft/socket/worker.sock'),
        'MONOMER_DFT_DOWNLOAD_SPOOL_ROOT': str(cohort.root / 'dft/spool'),
        'MONOMER_DFT_WORKER_INSTANCE': str(cohort.source_root),
        'MONOMER_DFT_START_AUTHORIZATION_URL': cohort.callback_base_url,
        'MONOMER_DFT_START_AUTHORIZATION_TOKEN': token,
    })
    return environment


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('validate', 'source-root', 'exec-md', 'exec-dft'))
    args = parser.parse_args(argv)
    try:
        cohort = from_environment()
        if cohort is None:
            raise CohortError('An explicit production cohort is required')
        if args.command == 'source-root':
            print(cohort.source_root)
        elif args.command == 'validate':
            md_environment(cohort)
            dft_environment(cohort)
        elif args.command == 'exec-md':
            from scripts.monomer_md_worker_launcher import launch
            launch(md_environment(cohort), source_root=cohort.source_root, runtime_root=cohort.md_runtime_root)
        else:
            from scripts import monomer_dft_worker_launcher, preflight_monomer_dft_prod
            environment = dft_environment(cohort)
            preflight_monomer_dft_prod.validate(repo_root=cohort.source_root, environment=environment,
                runtime_environment_path=cohort.dft_runtime_env, worker_state_root=cohort.root / 'dft')
            descriptor = monomer_dft_worker_launcher.validate(environment, source_root=cohort.source_root)
            environment['NEXPOLY_DFT_GOVERNED_FD_LAUNCH'] = '1'
            os.chdir(cohort.source_root)
            os.execve('/usr/bin/bash', ['/usr/bin/bash', f'/proc/self/fd/{descriptor}'], environment)
    except Exception:
        # Native loader/DB/runtime errors can contain private paths or input.
        print('production-worker-cohort: validation or execution failed', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

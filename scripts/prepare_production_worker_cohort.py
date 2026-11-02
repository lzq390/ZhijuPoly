#!/usr/bin/env python3
"""Build sealed, independent production Worker runtimes; never publish services.

This narrowly scoped facade reuses the controller's scientific build and full
runtime validation. It has no database, Docker, systemd, deployment or migration
operation. GPU warmup/transport acceptance must be performed separately.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import re
import sys
from urllib.parse import urlsplit

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import production_worker_cohort as cohorts
from scripts import production_dft_toolchain as tools
from scripts import pull_deploy_controller as core
from scripts import worker_slot_runtime as slots


def validate_build_proxy(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise cohorts.CohortError('Build proxy must be one explicit loopback HTTP endpoint')
    try:
        url = urlsplit(value)
        host = f'[{url.hostname}]' if url.hostname == '::1' else url.hostname
        if (url.scheme != 'http' or url.hostname not in {'127.0.0.1', '::1'}
                or url.port is None or not 1024 <= url.port <= 65535
                or value != f'http://{host}:{url.port}'):
            raise ValueError('noncanonical proxy')
    except ValueError as exc:
        raise cohorts.CohortError('Build proxy must be one explicit loopback HTTP endpoint') from exc
    return value


class BuildOnlyRunner(core.SystemRunner):
    """Apply a sealed proxy solely to hash-locked pip downloads."""

    def __init__(self, proxy, download_pythons):
        self.proxy = validate_build_proxy(proxy)
        self.download_pythons = frozenset(download_pythons)

    def run(self, command, *, env=None, **kwargs):
        environment = {key: value for key, value in (env or {}).items()
            if key.lower() not in {'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'}}
        if (self.proxy is not None and len(command) >= 5
                and command[0] in self.download_pythons
                and command[1:5] == ['-I', '-m', 'pip', 'download']
                and '--require-hashes' in command and '--only-binary=:all:' in command):
            environment.update(HTTP_PROXY=self.proxy, HTTPS_PROXY=self.proxy)
        return super().run(command, env=environment, **kwargs)


def _bytes(document):
    return json.dumps(document, sort_keys=True, separators=(',', ':')).encode() + b'\n'


def _write_once(path: Path, payload: bytes):
    if path.exists() or path.is_symlink():
        if cohorts.private_payload(path) != payload:
            raise cohorts.CohortError('Candidate build inputs differ from the existing operation seal')
    else:
        core.atomic_bytes(path, payload)
    return core.sha256_bytes(payload)


def _private_directory(path: Path):
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
    slots.require_private_directory(path)


def _lock_file(path: Path):
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        # Lock files are intentionally empty, unlike sealed payload inputs.
        metadata = path.lstat()
        if (path.is_symlink() or not path.is_file() or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o777 != 0o600 or metadata.st_nlink != 1):
            raise cohorts.CohortError('Candidate build lock is unsafe')
    else:
        os.close(descriptor)


def source_identity(root: Path, sha: str, tree: str):
    if (root.parent.parent != cohorts.PRODUCTION_SOURCE_ROOT
            or re.fullmatch(r'p9000-[a-z0-9][a-z0-9-]{7,39}', root.parent.name) is None
            or root.name != sha
            or re.fullmatch(r'[0-9a-f]{40}', sha) is None
            or re.fullmatch(r'[0-9a-f]{40}', tree) is None
            or not (root / '.git').is_dir() or (root / '.git').is_symlink()):
        raise cohorts.CohortError('Build requires the exact standalone candidate release clone')
    checkout = slots.inspect_git_checkout(root)
    if checkout.source_sha != sha or checkout.source_tree != tree:
        raise cohorts.CohortError('Build source differs from the requested release')
    return checkout


class CandidateBuildController(core.PullDeployController):
    """Adapt source reads and observed tools, preserving scientific build checks."""

    def __init__(self, source: Path, runtime: Path, *, sha: str, tree: str,
                 toolchain_path: Path, toolchain_digest: str, runner=None):
        source_identity(source, sha, tree)
        super().__init__(source, runtime, runner=runner)
        self.source_sha, self.source_tree = sha, tree
        self.toolchain_path, self.toolchain_digest = toolchain_path, toolchain_digest

    def control_environment(self):
        return {'HOME': pwd.getpwuid(os.geteuid()).pw_dir, 'PATH': core.SAFE_PATH,
            'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1',
            'PYTHONNOUSERSITE': '1', 'PIP_CONFIG_FILE': os.devnull,
            'PIP_DISABLE_PIP_VERSION_CHECK': '1', 'PIP_NO_INPUT': '1'}

    def _git_show(self, target_sha, relative):
        if target_sha != self.source_sha or relative not in {
                'workers/monomer_dft_worker/requirements.lock',
                'workers/monomer_dft_worker/aimnet-source.lock.json'}:
            raise cohorts.CohortError('Candidate build source read is outside its sealed inputs')
        source_identity(self.production_root, self.source_sha, self.source_tree)
        return slots.GIT_SOURCE_TRUST.run_git(self.production_root, 'show',
            f'{target_sha}:{relative}', ambient={}, text=False).stdout

    def _dft_build_toolchain(self):
        return tools.load(self.toolchain_path, self.toolchain_digest)


def _active_record(record, operation_id):
    return {'schema_version': slots.ACTIVE_RECORD_SCHEMA_VERSION, 'component': 'monomer-md',
        'slot': record['slot'], 'source_sha': record['source_sha'], 'source_tree': record['source_tree'],
        'worker_lock_sha256': record['worker_lock_sha256'],
        'slot_record_sha256': core.worker_record_digest(record), 'operation_id': operation_id,
        'activated_at': core.utc_now()}


def _validate_slot(record, runtime, operation_id):
    active = _active_record(record, operation_id)
    return slots.validate_selected_venv(slots.RuntimeSelection(
        slots.validate_active_record(active), slots.validate_slot_record(record, runtime_root=runtime, expected_slot=record['slot']),
        runtime / slots.ACTIVE_RECORD_RELATIVE_PATH,
        runtime / slots.SLOT_RECORD_DIRECTORY / f"md-{record['slot']}.json"), runtime)


def prepare(*, cohort_root: Path, sha: str, tree: str, operation_id: str,
            md_base_python: Path, md_base_identity: str, uv_source: Path,
            callback_base_url: str = 'http://127.0.0.1:18002', guard_mode: str = 'observe',
            build_proxy: str | None = None):
    """Prepare both candidate MD slots and one immutable DFT release, offline of services."""
    if (cohort_root.parent != cohorts.PRODUCTION_RUNTIME_ROOT / 'state'
            or cohort_root.name != operation_id
            or re.fullmatch(r'p9000-[a-z0-9][a-z0-9-]{7,39}', operation_id) is None):
        raise cohorts.CohortError('Build operation must equal the explicit candidate root name')
    core.require_operation_id(operation_id)
    build_proxy = validate_build_proxy(build_proxy)
    callback = urlsplit(callback_base_url)
    if (callback.scheme != 'http' or callback.hostname != '127.0.0.1'
            or callback.port is None or not 1024 <= callback.port <= 65535
            or callback_base_url != f'http://127.0.0.1:{callback.port}'
            or guard_mode not in {'observe', 'enforce'}):
        raise cohorts.CohortError('Candidate callback or GPU guard mode is outside the reviewed profile')
    for path in (cohort_root, cohort_root / 'config', cohorts.PRODUCTION_SOURCE_ROOT,
            cohorts.PRODUCTION_SOURCE_ROOT / operation_id):
        slots.require_private_directory(path)
    source = cohorts.PRODUCTION_SOURCE_ROOT / operation_id / sha
    source_identity(source, sha, tree)
    if md_base_python != Path('/home/devuser/miniconda3/envs/byteff2-repro/bin/python'):
        raise cohorts.CohortError('Candidate MD must retain the production scientific base Python')
    base = slots.inspect_base_python_identity(md_base_python, expected_identity=md_base_identity,
        environment={'PATH': core.SAFE_PATH, 'HOME': pwd.getpwuid(os.geteuid()).pw_dir})
    toolchain = tools.observe(uv_source)
    toolchain_path = cohort_root / 'config/dft-toolchain.json'
    toolchain_digest = _write_once(toolchain_path, _bytes(toolchain))
    # Read existing private DSN/token inputs without copying or displaying secrets.
    md_env_path = cohort_root / 'config/md-worker.env'
    token_path = cohort_root / 'config/dft-start-authorization.token'
    md_env_digest = core.sha256_bytes(cohorts.private_payload(md_env_path))
    token_digest = core.sha256_bytes(cohorts.private_payload(token_path))
    asset_root = cohorts.PRODUCTION_RUNTIME_ROOT / 'state/current-assets/byteff2'
    asset_digest = core.directory_inventory_digest(asset_root)
    locks = {name: core.sha256_bytes((source / name).read_bytes()) for name in (
        'workers/monomer_md_worker/requirements.lock',
        'workers/monomer_dft_worker/requirements.lock',
        'workers/monomer_dft_worker/aimnet-source.lock.json')}
    inputs = {'schema_version': 1, 'operation_id': operation_id, 'cohort_root': str(cohort_root),
        'source_root': str(source), 'source_sha': sha, 'source_tree': tree,
        'callback_base_url': callback_base_url, 'guard_mode': guard_mode,
        'build_proxy': build_proxy,
        'md_base_python_identity': base, 'md_worker_env_sha256': md_env_digest,
        'dft_start_authorization_token_sha256': token_digest,
        'dft_toolchain_sha256': toolchain_digest, 'md_assets_inventory_sha256': asset_digest,
        'scientific_locks': locks}
    inputs_digest = _write_once(cohort_root / 'config/worker-build-inputs.json', _bytes(inputs))
    md_runtime = cohort_root / 'md-runtime'
    for path in (md_runtime, md_runtime / 'config', md_runtime / 'state',
            md_runtime / 'state/prepared', md_runtime / 'state/prepared' / operation_id,
            md_runtime / 'state/worker-slots', md_runtime / 'worker-venvs', md_runtime / 'wheel-cache',
            md_runtime / 'state/monomer-md-worker-runs', md_runtime / 'state/monomer-md-worker-socket',
            md_runtime / 'state/gpu-resource', md_runtime / 'state/current-assets',
            cohort_root / 'dft', cohort_root / 'dft/jobs', cohort_root / 'dft/socket', cohort_root / 'dft/spool'):
        _private_directory(path)
    asset_link = md_runtime / 'state/current-assets/byteff2'
    if not asset_link.exists() and not asset_link.is_symlink():
        asset_link.symlink_to(asset_root, target_is_directory=True)
    _write_once(md_runtime / 'config/deploy.env', (
        f'NEXPOLY_WORKER_BASE_PYTHON={md_base_python}\n'
        f'NEXPOLY_WORKER_BASE_PYTHON_IDENTITY_SHA256={md_base_identity}\n').encode())
    _lock_file(md_runtime / 'state/deploy.lock')
    builder = CandidateBuildController(source, md_runtime, sha=sha, tree=tree,
        toolchain_path=toolchain_path, toolchain_digest=toolchain_digest,
        runner=BuildOnlyRunner(build_proxy, {base['resolved_path'], str(tools.PYTHON)}))
    result_path = cohort_root / 'config/worker-build-result.json'
    # The original lock serializes writes to the shared immutable DFT release store.
    global_lock = core.PullDeployController(core.PRODUCTION_ROOT, cohorts.PRODUCTION_RUNTIME_ROOT)
    with global_lock.deployment_lock(), builder.deployment_lock():
        if result_path.exists() or result_path.is_symlink():
            result = json.loads(cohorts.private_payload(result_path))
            if result.get('inputs_sha256') != inputs_digest:
                raise cohorts.CohortError('Existing build result has different inputs')
            for record in result['md_slots']:
                _validate_slot(record, md_runtime, operation_id)
            builder.venv_root = cohorts.PRODUCTION_RUNTIME_ROOT / 'worker-venvs'
            builder._validate_dft_runtime_directory(result['dft_runtime'])
            cohort = cohorts.load_cohort(cohort_root / 'config/worker-cohort.json', result['cohort_sha256'])
            cohorts.md_environment(cohort, {})
            cohorts.dft_environment(cohort, {})
            slots.verify_runtime_binding(source_root=source, runtime_root=md_runtime)
            return cohort
        records = []
        # Only candidate selection metadata is written; no serving authority changes.
        for slot in ('a', 'b'):
            record_path = builder.slots_state_dir / f'md-{slot}.json'
            if record_path.exists() or record_path.is_symlink():
                record = core.load_private_json(record_path)
                if (record.get('prepared_operation_id') != operation_id
                        or record.get('source_sha') != sha or record.get('source_tree') != tree):
                    raise cohorts.CohortError('Candidate MD slot belongs to another operation')
                _validate_slot(record, md_runtime, operation_id)
            else:
                record = builder.prepare_md_slot(operation_id=operation_id, target_sha=sha,
                    target_tree=tree, lock_payload=(source / 'workers/monomer_md_worker/requirements.lock').read_bytes())
                if record['slot'] != slot:
                    raise cohorts.CohortError('Candidate MD inactive slot differs')
            records.append(record)
            if slot == 'a':
                core.atomic_json(builder.active_slot_path, _active_record(record, operation_id))
        builder.venv_root = cohorts.PRODUCTION_RUNTIME_ROOT / 'worker-venvs'
        runtime = builder.prepare_dft_runtime(operation_id=operation_id, target_sha=sha, target_tree=tree)
        warp = cohorts.PRODUCTION_RUNTIME_ROOT / 'state/monomer-dft-warp-cache' / sha
        _private_directory(warp)
        dft_env = builder._dft_runtime_env_bytes(runtime, guard_mode=guard_mode, warp_cache_path=warp)
        dft_env_digest = _write_once(cohort_root / 'config/dft-runtime.env', dft_env)
        document = {key: inputs[key] for key in ('schema_version', 'cohort_root', 'source_root',
            'source_sha', 'source_tree', 'callback_base_url', 'md_worker_env_sha256',
            'dft_start_authorization_token_sha256', 'dft_toolchain_sha256', 'md_assets_inventory_sha256')}
        document['dft_runtime_env_sha256'] = dft_env_digest
        cohort_path = cohort_root / 'config/worker-cohort.json'
        cohort_digest = _write_once(cohort_path, _bytes(document))
        cohort = cohorts.load_cohort(cohort_path, cohort_digest)
        cohorts.md_environment(cohort, {})
        cohorts.dft_environment(cohort, {})
        slots.verify_runtime_binding(source_root=source, runtime_root=md_runtime)
        source_identity(source, sha, tree)
        _write_once(result_path, _bytes({'schema_version': 1, 'inputs_sha256': inputs_digest,
            'cohort_sha256': cohort_digest, 'md_slots': records, 'dft_runtime': runtime,
            'gpu_acceptance': 'not-run'}))
        return cohort


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cohort-root', required=True, type=Path)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--source-tree', required=True)
    parser.add_argument('--operation-id', required=True)
    parser.add_argument('--md-base-python', type=Path,
        default=Path('/home/devuser/miniconda3/envs/byteff2-repro/bin/python'))
    parser.add_argument('--md-base-identity-sha256', required=True)
    parser.add_argument('--uv-source', required=True, type=Path)
    parser.add_argument('--callback-base-url', default='http://127.0.0.1:18002')
    parser.add_argument('--guard-mode', choices=('observe', 'enforce'), default='observe')
    parser.add_argument('--build-proxy', default=None, help='explicit loopback HTTP proxy for pip downloads only')
    args = parser.parse_args(argv)
    try:
        cohort = prepare(cohort_root=args.cohort_root, sha=args.source_sha, tree=args.source_tree,
            operation_id=args.operation_id, md_base_python=args.md_base_python,
            md_base_identity=args.md_base_identity_sha256, uv_source=args.uv_source,
            callback_base_url=args.callback_base_url, guard_mode=args.guard_mode,
            build_proxy=args.build_proxy)
        print(json.dumps({'cohort_file': str(cohort.path), 'cohort_sha256': cohort.digest,
            'source_sha': cohort.source_sha, 'gpu_acceptance': 'not-run'}, sort_keys=True))
    except Exception:
        print('prepare-production-worker-cohort: build failed; inspect private operation evidence', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

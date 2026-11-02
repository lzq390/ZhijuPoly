"""Sealed candidate boundaries without production services, databases or GPU."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts import production_worker_cohort as module
from scripts import monomer_dft_worker_env, monomer_md_worker_launcher, production_dft_toolchain
from scripts import prepare_production_worker_cohort as builder

OBSERVE_TOOLCHAIN = production_dft_toolchain.observe
VALIDATE_MD_ASSETS = module.ProductionWorkerCohort.validate_md_assets

SHA = 'a' * 40
TREE = 'b' * 40
DIGEST = 'sha256:' + 'c' * 64


def digest(payload):
    return 'sha256:' + hashlib.sha256(payload).hexdigest()


def write_private(path, payload):
    path.write_bytes(payload)
    path.chmod(0o600)
    return digest(payload)


@pytest.fixture
def cohort_files(monkeypatch):
    # Keep real Unix-socket path limits in effect with a short synthetic root.
    with tempfile.TemporaryDirectory(prefix='pc-') as temporary:
        runtime = Path(temporary)
        root = runtime / 'state/p9000-20261102'
        source_base = runtime / 'sources'
        source_parent = source_base / root.name
        source = source_parent / SHA
        source.mkdir(mode=0o700, parents=True)
        for path in (root, root / 'releases', root / 'config', source_base, source_parent):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)
        monkeypatch.setattr(module, 'PRODUCTION_RUNTIME_ROOT', runtime)
        monkeypatch.setattr(module, 'PRODUCTION_SOURCE_ROOT', source_base)
        assets_base = runtime / 'assets'
        assets = assets_base / ('d' * 64) / 'byteff2'
        assets.mkdir(parents=True)
        (assets / 'scientific-model').write_bytes(b'frozen scientific model')
        (assets / 'scientific-model').chmod(0o400)
        assets.chmod(0o500)
        monkeypatch.setattr(module, 'PRODUCTION_ASSET_RELEASES_ROOT', assets_base)
        monkeypatch.setattr(monomer_dft_worker_env, 'RUNTIME_ROOT', runtime)
        monkeypatch.setattr(module, 'inspect_git_checkout',
            lambda path: SimpleNamespace(source_sha=SHA, source_tree=TREE))
        monkeypatch.setattr(module.ProductionWorkerCohort, 'validate_md_assets', lambda self: None)
        data = {'md_assets_root': str(assets), 'md_assets_inventory_sha256': DIGEST,
            'schema_version': 1, 'cohort_root': str(root), 'source_root': str(source),
            'source_sha': SHA, 'source_tree': TREE, 'callback_base_url': 'http://127.0.0.1:18002'}
        data['md_worker_env_sha256'] = write_private(root / 'config/md-worker.env',
            b'APP_SERVICE_POSTGRES_DSN=postgresql://cohort_service:fixture-secret@127.0.0.1:55433/nexpoly\n'
            b'USAGE_METERING_ENABLED=true\nUSAGE_ENTITLEMENTS_ENABLED=false\n')
        dft_root = runtime / 'worker-venvs/dft' / SHA
        dft = {'MONOMER_DFT_RELEASE_SHA': SHA, 'MONOMER_DFT_RUNTIME_CONTRACT_SHA256': DIGEST,
            'MONOMER_DFT_RUNTIME_INVENTORY_SHA256': DIGEST,
            'MONOMER_DFT_PYTHON': str(dft_root / 'venv/bin/python'),
            'AIMNET_CACHE_DIR': str(dft_root / 'aimnet-cache'),
            'WARP_CACHE_PATH': str(runtime / 'state/monomer-dft-warp-cache' / SHA),
            'NEXPOLY_DFT_GPU_GUARD_MODE': 'observe'}
        data['dft_runtime_env_sha256'] = write_private(root / 'config/dft-runtime.env',
            ''.join(f'{key}={value}\n' for key, value in dft.items()).encode())
        data['dft_start_authorization_token_sha256'] = write_private(
            root / 'config/dft-start-authorization.token', b'private-fixture-token-' + b'x' * 32 + b'\n')
        tools = {'schema_version': 1, 'python': '/usr/bin/python3.12', 'python_sha256': DIGEST,
            'pip_root': '/usr/lib/python3/dist-packages/pip', 'pip_inventory_sha256': DIGEST,
            'uv': '/sealed/tools/uv', 'uv_sha256': production_dft_toolchain.UV_SHA256,
            'uv_uid': os.geteuid(), 'uv_mode': 0o700, 'uv_version': '0.11.21'}
        data['dft_toolchain_sha256'] = write_private(root / 'config/dft-toolchain.json', json.dumps(tools).encode())
        monkeypatch.setattr(production_dft_toolchain, 'observe', lambda uv: tools)
        path = root / 'config/worker-cohort.json'

        def seal(changes=None):
            return write_private(path, json.dumps({**data, **(changes or {})}).encode())

        yield root, source, path, seal(), seal


def test_assets_bind_canonical_release_and_survive_legacy_pointer_switch(cohort_files, monkeypatch):
    root, _, path, sealed, _ = cohort_files
    cohort = module.load_cohort(path, sealed)
    assets = cohort.md_assets_root
    cohort = replace(cohort, md_assets_inventory_sha256=builder.core.directory_inventory_digest(assets))
    link = cohort.md_runtime_root / 'state/current-assets/byteff2'
    link.parent.mkdir(parents=True)
    link.symlink_to(assets, target_is_directory=True)
    legacy_pointer = module.PRODUCTION_RUNTIME_ROOT / 'state/current-assets'
    legacy_pointer.symlink_to(assets.parent, target_is_directory=True)
    checks = []
    monkeypatch.setattr(builder.core, 'inspect_asset_release',
        lambda release, expected: checks.append((release, expected)))
    assert builder.production_md_assets_root() == assets
    assert checks == [(assets.parent, 'sha256:' + assets.parent.name)]
    VALIDATE_MD_ASSETS(cohort)
    other = assets.parent.parent / ('e' * 64) / 'byteff2'
    other.mkdir(parents=True)
    legacy_pointer.unlink()
    legacy_pointer.symlink_to(other.parent, target_is_directory=True)
    # A future old-stack release does not change the candidate's sealed assets.
    VALIDATE_MD_ASSETS(cohort)
    assert builder.production_md_assets_root() == other


@pytest.mark.parametrize('change', ['legacy-link', 'other-link', 'linked-file', 'writable', 'hash'])
def test_assets_reject_unsealed_pointer_or_mutated_tree(cohort_files, change):
    _, _, path, sealed, _ = cohort_files
    cohort = module.load_cohort(path, sealed)
    assets = cohort.md_assets_root
    cohort = replace(cohort, md_assets_inventory_sha256=builder.core.directory_inventory_digest(assets))
    link = cohort.md_runtime_root / 'state/current-assets/byteff2'
    link.parent.mkdir(parents=True)
    target = assets
    if change == 'legacy-link':
        legacy = module.PRODUCTION_RUNTIME_ROOT / 'state/current-assets'
        legacy.symlink_to(assets.parent, target_is_directory=True)
        target = legacy / 'byteff2'
    elif change == 'other-link':
        target = assets.parent.parent / ('e' * 64) / 'byteff2'
        target.mkdir(parents=True)
    elif change == 'linked-file':
        assets.chmod(0o700)
        (assets / 'extra').symlink_to(assets / 'scientific-model')
        assets.chmod(0o500)
    elif change == 'writable':
        (assets / 'scientific-model').chmod(0o600)
    elif change == 'hash':
        model = assets / 'scientific-model'
        model.chmod(0o600)
        model.write_bytes(b'changed scientific model')
        model.chmod(0o400)
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(module.CohortError):
        VALIDATE_MD_ASSETS(cohort)


def test_asset_authority_rejects_pointer_changed_during_validation(cohort_files, monkeypatch):
    _, _, path, sealed, _ = cohort_files
    cohort = module.load_cohort(path, sealed)
    pointer = module.PRODUCTION_RUNTIME_ROOT / 'state/current-assets'
    pointer.symlink_to(cohort.md_assets_root.parent, target_is_directory=True)
    other = cohort.md_assets_root.parent.parent / ('e' * 64) / 'byteff2'
    other.mkdir(parents=True)
    def change_pointer(*_):
        pointer.unlink()
        pointer.symlink_to(other.parent, target_is_directory=True)
    monkeypatch.setattr(builder.core, 'inspect_asset_release', change_pointer)
    with pytest.raises(module.CohortError, match='changed while validating'):
        builder.production_md_assets_root()


@pytest.mark.parametrize('target', ['outside', 'linked-release', 'invalid-release'])
def test_cohort_rejects_noncanonical_asset_release(cohort_files, target):
    _, _, path, sealed, seal = cohort_files
    cohort = module.load_cohort(path, sealed)
    if target == 'outside':
        assets = cohort.root / 'arbitrary-assets/byteff2'
        assets.mkdir(parents=True)
    elif target == 'linked-release':
        release = cohort.md_assets_root.parent.parent / ('e' * 64)
        release.symlink_to(cohort.md_assets_root.parent, target_is_directory=True)
        assets = release / 'byteff2'
    else:
        assets = cohort.md_assets_root.parent.parent / 'latest/byteff2'
        assets.mkdir(parents=True)
    with pytest.raises(module.CohortError, match='immutable release layout'):
        module.load_cohort(path, seal({'md_assets_root': str(assets)}))


def test_candidate_has_independent_state_and_scrubbed_native_environments(cohort_files):
    root, source, path, sealed, _ = cohort_files
    cohort = module.load_cohort(path, sealed)
    inherited = {'HOME': '/private/home', 'LD_PRELOAD': '/untrusted/loader',
        'PYTHONPATH': '/untrusted/source', 'APP_POSTGRES_DSN': 'never-copy',
        'MONOMER_DFT_START_AUTHORIZATION_TOKEN': 'never-copy',
        'USAGE_ENTITLEMENTS_ENABLED': 'true'}
    md = module.md_environment(cohort, inherited)
    assert md['APP_SERVICE_POSTGRES_DSN'].endswith(':55433/nexpoly')
    assert md['USAGE_METERING_ENABLED'] == 'true' and md['USAGE_ENTITLEMENTS_ENABLED'] == 'false'
    assert md['MONOMER_MD_GPU_BROKER_ENVIRONMENT'] == 'prod'
    assert md['MONOMER_MD_WORKER_UDS'] == str(root / 'md-runtime/state/monomer-md-worker-socket/worker.sock')
    assert md['MONOMER_MD_JOB_ROOT'] == str(root / 'md-runtime/state/monomer-md-worker-runs')
    assert 'LD_PRELOAD' not in md and 'APP_POSTGRES_DSN' not in md
    assert md['PYTHONPATH'].split(':')[0] == str(source)
    assert md['MONOMER_MD_MAX_ACTIVE_JOBS'] == '3' and md['MONOMER_MD_MAX_CONCURRENT_JOBS'] == '1'
    dft = module.dft_environment(cohort, inherited)
    assert dft['MONOMER_DFT_DEPLOYMENT'] == 'prod'
    assert dft['MONOMER_DFT_JOB_ROOT'] == str(root / 'dft/jobs')
    assert dft['MONOMER_DFT_WORKER_UDS'] == str(root / 'dft/socket/worker.sock')
    assert dft['MONOMER_DFT_DOWNLOAD_SPOOL_ROOT'] == str(root / 'dft/spool')
    assert dft['MONOMER_DFT_START_AUTHORIZATION_URL'] == 'http://127.0.0.1:18002'
    assert dft['MONOMER_DFT_START_AUTHORIZATION_TOKEN'].startswith('private-fixture-token-')
    assert dft['NEXPOLY_DFT_GPU_DEVICE'] == '2' and dft['NEXPOLY_DFT_OVERFLOW_GPU_DEVICES'] == ''
    assert dft['MONOMER_DFT_GPU_BROKER_ENABLED'] == '0'
    assert 'PYTHONPATH' not in dft and 'LD_PRELOAD' not in dft and 'APP_POSTGRES_DSN' not in dft


@pytest.mark.parametrize('changes', [
    {'schema_version': True}, {'extra': 'unreviewed'}, {'source_sha': 'short'},
    {'source_root': '/data/lzq/gith/nexpoly'},
    {'source_root': '/data/lzq/gith/nexpoly-runtime/state/p9000-20261102/releases/' + SHA},
    {'source_root': '/data/lzq/gith/nexpoly-release-private/p9000-unrelated/' + SHA},
    {'cohort_root': '/data/lzq/gith/nexpoly-runtime'},
    {'callback_base_url': 'http://114.214.255.154:9000'},
    {'callback_base_url': 'http://127.0.0.1:18002/'},
    {'callback_base_url': 'http://user:password@127.0.0.1:18002'},
    {'md_worker_env_sha256': 'sha256:short'},
])
def test_cohort_cannot_expand_source_state_or_callback(cohort_files, changes):
    _, _, path, _, seal = cohort_files
    with pytest.raises(module.CohortError):
        module.load_cohort(path, seal(changes))


def test_source_drift_is_rejected(cohort_files, monkeypatch):
    _, _, path, sealed, _ = cohort_files
    monkeypatch.setattr(module, 'inspect_git_checkout',
        lambda path: SimpleNamespace(source_sha='d' * 40, source_tree=TREE))
    with pytest.raises(module.CohortError, match='sealed release'):
        module.load_cohort(path, sealed)


@pytest.mark.parametrize('leaf', ['md-worker.env', 'dft-runtime.env', 'dft-start-authorization.token', 'dft-toolchain.json'])
def test_private_input_drift_cannot_retarget_candidate(cohort_files, leaf):
    root, _, path, sealed, _ = cohort_files
    cohort = module.load_cohort(path, sealed)
    target = root / 'config' / leaf
    target.write_bytes(target.read_bytes() + b'changed')
    with pytest.raises(module.CohortError, match='sealed digest'):
        (module.md_environment if leaf == 'md-worker.env' else module.dft_environment)(cohort, {})


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'public'])
def test_manifest_requires_private_single_unlinked_inode(cohort_files, kind):
    root, _, path, sealed, _ = cohort_files
    if kind == 'public':
        path.chmod(0o644)
    else:
        original = root / 'config/original.json'
        path.rename(original)
        if kind == 'symlink':
            path.symlink_to(original)
        else:
            os.link(original, path)
    with pytest.raises(module.CohortError):
        module.load_cohort(path, sealed)


def test_cohort_environment_requires_both_seals(cohort_files):
    _, source, path, sealed, _ = cohort_files
    assert module.from_environment({}) is None
    for values in ({module.FILE_KEY: str(path)}, {module.DIGEST_KEY: sealed},
                   {module.SOURCE_KEY: str(source)}):
        with pytest.raises(module.CohortError):
            module.from_environment(values)
    with pytest.raises(module.CohortError, match='different source'):
        module.from_environment({module.FILE_KEY: str(path), module.DIGEST_KEY: sealed,
                                 module.SOURCE_KEY: '/different/source'})


def test_toolchain_hashes_cannot_be_operator_self_assertions(cohort_files):
    root, _, path, sealed, _ = cohort_files
    cohort = module.load_cohort(path, sealed)
    tool_path = root / 'config/dft-toolchain.json'
    document = json.loads(tool_path.read_bytes())
    document['python_sha256'] = 'sha256:' + 'f' * 64
    changed = write_private(tool_path, json.dumps(document).encode())
    with pytest.raises(production_dft_toolchain.ToolchainError, match='observed seal'):
        production_dft_toolchain.load(tool_path, changed)
    # The external cohort seal also rejects replacing the entire tool record.
    with pytest.raises(module.CohortError, match='sealed digest'):
        cohort.toolchain()


def test_candidate_toolchain_cannot_accept_an_arbitrary_uv_path():
    with pytest.raises(production_dft_toolchain.ToolchainError, match='audited frozen'):
        OBSERVE_TOOLCHAIN(Path('/tmp/unreviewed-tools/uv'))


def test_existing_controller_default_toolchain_pins_are_unchanged(tmp_path):
    controller = builder.core.PullDeployController(tmp_path / 'source', tmp_path / 'runtime')
    defaults = controller._dft_build_toolchain()
    assert defaults['python_sha256'] == 'sha256:1643dacd9feaedc58f3cc581e4d22577dfe25c09b10282936186ccf0f2e61118'
    assert defaults['uv'] == '/snap/astral-uv/1551/bin/uv'
    assert defaults['uv_sha256'] == production_dft_toolchain.UV_SHA256
    assert defaults['uv_uid'] == 0 and defaults['uv_mode'] == 0o755


@pytest.mark.parametrize('proxy', ['https://127.0.0.1:17891', 'http://localhost:17891',
    'http://127.0.0.1:17891/', 'http://user:secret@127.0.0.1:17891',
    'http://127.0.0.1:17891?option=true', 'http://127.0.0.1:17891#fragment',
    'http://127.0.0.1:80', '', True])
def test_build_proxy_cannot_select_credentials_remote_endpoints_or_paths(proxy):
    with pytest.raises(module.CohortError, match='loopback HTTP'):
        builder.validate_build_proxy(proxy)


def test_sealed_build_proxy_only_reaches_real_hash_locked_pip_downloads(monkeypatch):
    calls = []
    def run(self, command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(builder.core.SystemRunner, 'run', run)
    proxy = 'http://127.0.0.1:17891'
    python = '/usr/bin/python3.12'
    arguments = [python, '-I', '-m', 'pip', 'download', '--require-hashes', '--only-binary=:all:']
    runner = builder.BuildOnlyRunner(proxy, {python})
    original = {'PATH': '/usr/bin', 'HTTP_PROXY': 'http://unexpected:1', 'all_proxy': 'http://unexpected:2'}
    runner.run(arguments, env=original)
    assert calls[-1][1]['env'] == {'PATH': '/usr/bin', 'HTTP_PROXY': proxy, 'HTTPS_PROXY': proxy}
    for command in ([python, '-I', '-m', 'pip', 'install', '--no-index'],
                    ['/sealed/tools/uv', 'pip', 'install', '--offline'],
                    ['/tmp/other-python', *arguments[1:]], arguments[:-1],
                    [python, '-m', 'uvicorn', 'app:app']):
        runner.run(command, env=original)
        assert calls[-1][1]['env'] == {'PATH': '/usr/bin'}
    builder.BuildOnlyRunner(None, {python}).run(arguments, env=original)
    assert calls[-1][1]['env'] == {'PATH': '/usr/bin'}
    assert original['HTTP_PROXY'] == 'http://unexpected:1'
    assert builder.validate_build_proxy('http://[::1]:17891') == 'http://[::1]:17891'


@pytest.mark.parametrize('unsafe', [None, 'symlink', 'hardlink', 'foreign-operation'])
def test_candidate_venv_only_normalizes_new_owned_activation_outputs(tmp_path, monkeypatch, unsafe):
    operation = 'p9000-20261102r'
    runtime = tmp_path / 'md-runtime'
    venv = runtime / 'worker-venvs/md-a/venv'
    (venv / 'bin').mkdir(parents=True, mode=0o700)
    write_private(venv.parent / '.preparing.json', json.dumps({'schema_version': 1,
        'operation_id': 'p9000-other' if unsafe == 'foreign-operation' else operation, 'slot': 'a'}).encode())
    for name in ('activate', 'activate.csh', 'activate.fish', 'Activate.ps1'):
        target = venv / 'bin' / name
        target.write_bytes(b'copied activation output')
        target.chmod(0o664)
    if unsafe in {'symlink', 'hardlink'}:
        target = venv / 'bin/activate'
        target.unlink()
        original = tmp_path / 'unchanged-base-activate'
        original.write_bytes(b'original base activation')
        original.chmod(0o664)
        if unsafe == 'symlink':
            target.symlink_to(original)
        else:
            os.link(original, target)
    monkeypatch.setattr(builder.core.SystemRunner, 'run',
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    python = '/sealed/md/python'
    runner = builder.BuildOnlyRunner(None, {python}, md_runtime=runtime, operation_id=operation)
    command = [python, '-I', '-m', 'venv', '--system-site-packages', str(venv)]
    if unsafe:
        with pytest.raises((module.CohortError, OSError)):
            runner.run(command, env={})
        if unsafe in {'symlink', 'hardlink'}:
            assert original.stat().st_mode & 0o777 == 0o664
    else:
        runner.run(command, env={})
        assert all(path.stat().st_mode & 0o777 == 0o644 for path in (venv / 'bin').iterdir())


@pytest.mark.parametrize('unsafe', [None, 'nonempty', 'symlink', 'hardlink', 'wrong-operation', 'wrong-release'])
def test_only_empty_owned_new_dft_uv_lock_is_normalized(tmp_path, monkeypatch, unsafe):
    operation = 'p9000-20261102u'
    monkeypatch.setattr(module, 'PRODUCTION_RUNTIME_ROOT', tmp_path)
    stage = tmp_path / 'worker-venvs/dft' / f'.{SHA}.preparing-{operation}'
    (stage / 'venv').mkdir(parents=True, mode=0o700)
    owner = {'schema_version': 1, 'operation_id': 'other-operation' if unsafe == 'wrong-operation' else operation,
        'release_sha': 'f' * 40 if unsafe == 'wrong-release' else SHA, 'source_tree': TREE}
    write_private(stage / '.preparing.json', json.dumps(owner).encode())
    target = stage / 'venv/.lock'
    target.write_bytes(b'not a generated lock' if unsafe == 'nonempty' else b'')
    target.chmod(0o666)
    if unsafe in {'symlink', 'hardlink'}:
        target.unlink();outside = tmp_path / 'outside-empty-lock';outside.write_bytes(b'');outside.chmod(0o666)
        if unsafe == 'symlink': target.symlink_to(outside)
        else: os.link(outside, target)
    monkeypatch.setattr(builder.core.SystemRunner, 'run', lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    runner = builder.BuildOnlyRunner(None, set(), operation_id=operation, source_sha=SHA, source_tree=TREE)
    command = [str(stage / 'tools/uv'), 'pip', 'install', '--python', str(stage / 'venv/bin/python'), '--offline']
    if unsafe:
        with pytest.raises((module.CohortError, OSError)):
            runner.run(command, env={})
        if unsafe in {'symlink', 'hardlink'}:
            assert outside.stat().st_mode & 0o777 == 0o666
    else:
        # Other commands do not authorize metadata normalization.
        runner.run([str(stage / 'tools/uv'), '--version'], env={})
        assert target.stat().st_mode & 0o777 == 0o666
        runner.run(command, env={})
        assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('change', ['worktree', 'dirty', 'wrong-sha', 'wrong-tree'])
def test_build_facade_requires_fixed_clean_standalone_clone(cohort_files, monkeypatch, change):
    _, source, _, _, _ = cohort_files
    (source / '.git').mkdir(mode=0o700)
    if change == 'worktree':
        (source / '.git').rmdir()
        (source / '.git').write_text('gitdir: /outside/shared-worktree')
    def inspect(path):
        if change == 'dirty':
            raise builder.slots.WorkerSlotError('source is dirty')
        return SimpleNamespace(source_sha='f' * 40 if change == 'wrong-sha' else SHA,
            source_tree='f' * 40 if change == 'wrong-tree' else TREE)
    monkeypatch.setattr(builder.slots, 'inspect_git_checkout', inspect)
    with pytest.raises((module.CohortError, builder.slots.WorkerSlotError)):
        builder.source_identity(source, SHA, TREE)


@pytest.mark.parametrize('changes', [
    {'operation_id': 'unrelated-operation'}, {'guard_mode': 'disabled'},
    {'callback_base_url': 'http://114.214.255.154:9000'},
    {'callback_base_url': 'http://127.0.0.1:18002/'},
    {'md_base_python': Path('/tmp/custom-python')},
])
def test_build_facade_rejects_authority_expansion_before_build(cohort_files, monkeypatch, changes):
    root, source, _, _, _ = cohort_files
    (source / '.git').mkdir(mode=0o700)
    monkeypatch.setattr(builder.slots, 'inspect_git_checkout',
        lambda path: SimpleNamespace(source_sha=SHA, source_tree=TREE))
    arguments = dict(cohort_root=root, sha=SHA, tree=TREE, operation_id=root.name,
        md_base_python=Path('/home/devuser/miniconda3/envs/byteff2-repro/bin/python'),
        md_base_identity=DIGEST, uv_source=Path('/not-used'))
    with pytest.raises(module.CohortError):
        builder.prepare(**{**arguments, **changes})
    assert not (root / 'config/worker-build-inputs.json').exists()


def test_facade_reads_only_sealed_scientific_objects(cohort_files, monkeypatch):
    root, source, _, _, _ = cohort_files
    (source / '.git').mkdir(mode=0o700)
    monkeypatch.setattr(builder.slots, 'inspect_git_checkout',
        lambda path: SimpleNamespace(source_sha=SHA, source_tree=TREE))
    calls = []
    def run_git(path, *args, **kwargs):
        calls.append((path, args, kwargs))
        return SimpleNamespace(stdout=b'locked-scientific-input')
    monkeypatch.setattr(builder.slots.GIT_SOURCE_TRUST, 'run_git', run_git)
    facade = builder.CandidateBuildController(source, root / 'md-runtime', sha=SHA, tree=TREE,
        toolchain_path=root / 'config/dft-toolchain.json', toolchain_digest=DIGEST)
    assert facade._git_show(SHA, 'workers/monomer_dft_worker/requirements.lock') == b'locked-scientific-input'
    assert calls[0][2] == {'ambient': {}, 'text': False}
    for sha, path in ((SHA, 'backend/migrations/postgres/manifest.json'),
                      ('f' * 40, 'workers/monomer_dft_worker/requirements.lock')):
        with pytest.raises(module.CohortError, match='sealed inputs'):
            facade._git_show(sha, path)
    assert len(calls) == 1


def test_md_launcher_still_requires_complete_ab_binding(cohort_files, monkeypatch):
    root, source, path, sealed, _ = cohort_files
    cohort = module.load_cohort(path, sealed)
    for directory in (cohort.md_runtime_root, cohort.md_runtime_root / 'state',
                      cohort.md_runtime_root / 'state/monomer-md-worker-runs',
                      cohort.md_runtime_root / 'state/current-assets/byteff2'):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    calls = []

    def bad_binding(**kwargs):
        calls.append(kwargs)
        raise monomer_md_worker_launcher.WorkerSlotError('unsealed runtime')

    monkeypatch.setattr(monomer_md_worker_launcher, 'verify_runtime_binding', bad_binding)
    with pytest.raises(monomer_md_worker_launcher.LauncherError, match='unsealed runtime'):
        monomer_md_worker_launcher.launch(module.md_environment(cohort, {}), source_root=source,
            runtime_root=cohort.md_runtime_root, execute=lambda *_: pytest.fail('must not execute'))
    assert calls == [{'source_root': source, 'runtime_root': cohort.md_runtime_root}]


def test_cli_failure_does_not_echo_private_environment(cohort_files):
    _, _, path, _, _ = cohort_files
    result = subprocess.run([sys.executable, '-I', '-B', str(Path(module.__file__)), 'exec-dft'],
        env={'PATH': '/usr/bin:/bin', module.FILE_KEY: str(path), module.DIGEST_KEY: DIGEST,
             'APP_SERVICE_POSTGRES_DSN': 'private-dsn-marker'}, capture_output=True, text=True)
    assert result.returncode == 2
    assert result.stdout == '' and 'private-dsn-marker' not in result.stderr
    assert 'fixture-secret' not in result.stderr


def test_dft_config_accepts_only_exact_sealed_candidate_state(cohort_files, monkeypatch):
    from workers.monomer_dft_worker.app import config
    root, source, path, sealed, _ = cohort_files
    cohort = module.load_cohort(path, sealed)
    environment = module.dft_environment(cohort, {})
    monkeypatch.delenv('PYTHONPATH', raising=False)
    monkeypatch.setattr(config, 'REPO_ROOT', source)
    monkeypatch.setattr(config, 'PRODUCTION_RUNTIME_ROOT', module.PRODUCTION_RUNTIME_ROOT)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('CUDA_VISIBLE_DEVICES', raising=False)
    settings = config.load_settings()
    assert settings.deployment == 'prod' and settings.physical_gpu == '2'
    assert settings.job_root == root / 'dft/jobs'
    assert settings.start_authorization_url == 'http://127.0.0.1:18002'
    monkeypatch.setenv('MONOMER_DFT_JOB_ROOT', str(module.PRODUCTION_RUNTIME_ROOT / 'old-jobs'))
    with pytest.raises(ValueError, match='sealed cohort'):
        config.load_settings()


def test_dft_source_exception_requires_cohort_not_development_identity(cohort_files, monkeypatch):
    from workers.monomer_dft_worker.app import config
    _, source, path, sealed, _ = cohort_files
    for key in (module.FILE_KEY, module.DIGEST_KEY, module.SOURCE_KEY):
        monkeypatch.delenv(key, raising=False)
    assert config._production_source_allowed(source) is False
    assert config._production_source_allowed(config.PRODUCTION_REPO_ROOT) is True
    monkeypatch.setenv(module.FILE_KEY, str(path))
    monkeypatch.setenv(module.DIGEST_KEY, sealed)
    assert config._production_source_allowed(source) is True
    assert config._production_source_allowed(config.PRODUCTION_REPO_ROOT) is False

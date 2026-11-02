"""Non-Broker recovery uses exact service/kernel identity, never a health flag."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from workers.monomer_md_worker.app import recovery_resources as r
from workers.monomer_md_worker.app import runner as runner_module
from workers.monomer_md_worker.app.config import load_settings
from workers.monomer_md_worker.tests.test_runner import _settings

UNIT = 'nexpoly-test-md-worker.service'


@pytest.fixture
def kernel(tmp_path, monkeypatch):
    proc, cgroup = tmp_path / 'proc', tmp_path / 'cgroup'
    (proc / 'self').mkdir(parents=True); cgroup.mkdir()
    monkeypatch.setattr(r, '_PROC', proc); monkeypatch.setattr(r, '_CGROUP', cgroup)
    pid = os.getpid(); process = proc / str(pid); process.mkdir()
    group = '/user.slice/user-1001.slice/user@1001.service/app.slice/' + UNIT
    root = cgroup / group.removeprefix('/'); root.mkdir(parents=True)
    (process / 'stat').write_text(f'{pid} (worker with ) spaces) S ' + '0 ' * 18 + '12345\n')
    (process / 'cgroup').write_text('0::' + group + '\n')
    mount = f'38 25 0:28 / {cgroup} rw,nosuid,nodev,noexec - cgroup2 cgroup rw\n'
    (proc / 'self/mountinfo').write_text(mount)
    (root / 'cgroup.type').write_text('domain\n')
    (root / 'cgroup.events').write_text('populated 1\nfrozen 0\n')
    (root / 'cgroup.procs').write_text(f'{pid}\n')
    properties = {'Id': UNIT, 'LoadState': 'loaded', 'MainPID': str(pid),
                  'ControlGroup': group, 'KillMode': 'control-group', 'Delegate': 'no'}
    calls = []
    def show(command, **kwargs):
        assert command[:4] == ['/usr/bin/systemctl', '--user', 'show', UNIT]
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout='\n'.join(f'{key}={value}' for key, value in properties.items()))
    monkeypatch.setattr(r, 'subprocess', SimpleNamespace(run=show))
    return SimpleNamespace(proc=proc, cgroup=cgroup, root=root, pid=pid, process=process,
                           group=group, properties=properties, calls=calls, mount=mount)


def test_self_only_domain_proof_is_bound_and_runtime_uses_no_systemd_process(kernel):
    proof = r.validate_recovery_systemd_unit(UNIT)
    assert proof['pid'] == kernel.pid and proof['start_ticks'] == 12345
    assert proof['cgroup'] == kernel.group and proof['unit'] == UNIT
    assert r.recovery_cgroup_empty(UNIT, binding=proof)
    assert len(kernel.calls) == 1
    # Threads belong to the same process and are not independent executions.
    (kernel.root / 'cgroup.threads').write_text(f'{kernel.pid}\n{kernel.pid + 1}\n')
    assert r.recovery_cgroup_empty(UNIT, binding=proof)
    assert not r.recovery_cgroup_empty(UNIT)


@pytest.mark.parametrize('key,value', [('Id', 'other.service'), ('LoadState', 'not-found'), ('MainPID', '0'),
    ('MainPID', '123'), ('ControlGroup', '/other/' + UNIT), ('KillMode', 'process'), ('KillMode', 'mixed'),
    ('KillMode', 'none'), ('Delegate', 'yes')])
def test_startup_contract_rejects_unsafe_unit_properties(kernel, key, value):
    kernel.properties[key] = value
    with pytest.raises(r.RecoveryResourceError): r.validate_recovery_systemd_unit(UNIT)


@pytest.mark.parametrize('mutation', ['extra-pid', 'missing-self', 'empty', 'invalid-pid', 'duplicate-self',
    'child-cgroup', 'threaded', 'not-populated', 'symlink-procs', 'symlink-directory', 'wrong-group',
    'root-group', 'double-root', 'legacy-cgroup', 'traversal', 'not-cgroup2', 'subtree-mount', 'duplicate-mount',
    'missing-file', 'oversized', 'permission', 'starttime-race', 'cgroup-race', 'directory-race'])
def test_kernel_uncertainty_fails_closed(kernel, monkeypatch, mutation):
    proof = r.validate_recovery_systemd_unit(UNIT)
    if mutation == 'extra-pid': (kernel.root / 'cgroup.procs').write_text(f'{kernel.pid}\n999999\n')
    elif mutation == 'missing-self': (kernel.root / 'cgroup.procs').write_text('999999\n')
    elif mutation == 'empty': (kernel.root / 'cgroup.procs').write_text('')
    elif mutation == 'invalid-pid': (kernel.root / 'cgroup.procs').write_text('not-a-pid\n')
    elif mutation == 'duplicate-self': (kernel.root / 'cgroup.procs').write_text(f'{kernel.pid}\n{kernel.pid}\n')
    elif mutation == 'child-cgroup': (kernel.root / 'unknown-empty-subtree').mkdir()
    elif mutation == 'threaded': (kernel.root / 'cgroup.type').write_text('threaded\n')
    elif mutation == 'not-populated': (kernel.root / 'cgroup.events').write_text('populated 0\n')
    elif mutation == 'symlink-procs':
        (kernel.root / 'cgroup.procs').unlink(); (kernel.root / 'cgroup.procs').symlink_to(kernel.process / 'stat')
    elif mutation == 'symlink-directory':
        moved = kernel.root.with_suffix('.moved'); kernel.root.rename(moved); kernel.root.symlink_to(moved, target_is_directory=True)
    elif mutation in ('wrong-group', 'root-group', 'double-root', 'legacy-cgroup', 'traversal'):
        (kernel.process / 'cgroup').write_text({'wrong-group': '0::/other.service\n', 'root-group': '0::/\n',
            'double-root': '0::/' + kernel.group + '\n', 'legacy-cgroup': '2:cpu:/unit\n',
            'traversal': '0::/other/../' + UNIT + '\n'}[mutation])
    elif mutation in ('not-cgroup2', 'subtree-mount', 'duplicate-mount'):
        text = kernel.mount.replace('- cgroup2 ', '- tmpfs ') if mutation == 'not-cgroup2' else (
            kernel.mount.replace('0:28 / ', '0:28 /delegated ') if mutation == 'subtree-mount' else kernel.mount * 2)
        (kernel.proc / 'self/mountinfo').write_text(text)
    elif mutation == 'missing-file': (kernel.root / 'cgroup.events').unlink()
    elif mutation == 'oversized': (kernel.root / 'cgroup.procs').write_text('1' * 65537)
    elif mutation == 'permission': monkeypatch.setattr(r, '_read_at', lambda *a: (_ for _ in ()).throw(PermissionError()))
    elif mutation == 'starttime-race':
        values = iter((12345, 12346)); monkeypatch.setattr(r, '_start_ticks', lambda pid: next(values))
    elif mutation == 'cgroup-race':
        values = iter((kernel.group, '/other/' + UNIT)); monkeypatch.setattr(r, '_self_group', lambda *a: next(values))
    else:
        original = r._members; calls = []
        def changed(fd, pid):
            original(fd, pid); calls.append(True)
            if len(calls) == 2: (kernel.root / 'new-file').write_text('directory changed')
        monkeypatch.setattr(r, '_members', changed)
    assert not r.recovery_cgroup_empty(UNIT, binding=proof)


@pytest.mark.parametrize('field', ['pid', 'start_ticks', 'unit', 'cgroup', 'cgroup_device', 'cgroup_inode'])
def test_old_or_another_workers_binding_is_not_release_proof(kernel, field):
    proof = r.validate_recovery_systemd_unit(UNIT); proof[field] = 'changed'
    assert not r.recovery_cgroup_empty(UNIT, binding=proof)


def production_settings(tmp_path, **overrides):
    return replace(_settings(tmp_path), mode='real', gpu_broker_environment='prod',
                   recovery_systemd_unit=UNIT, **overrides)


def test_nonbroker_runner_requires_startup_binding_and_kernel_resource_absence(kernel, tmp_path):
    runner = runner_module.MonomerMdRunner(production_settings(tmp_path))
    assert runner.recovery_resources_released()
    (kernel.root / 'cgroup.procs').write_text(f'{kernel.pid}\n999999\n')
    assert not runner.recovery_resources_released()
    (kernel.root / 'cgroup.procs').write_text(f'{kernel.pid}\n')
    assert runner.recovery_resources_released()
    runner._gpu_admission_uncertain = True
    assert not runner.recovery_resources_released()


def test_unconfigured_nonbroker_and_missing_enabled_broker_do_not_fall_back_to_true(kernel, tmp_path):
    settings = replace(_settings(tmp_path), mode='real', gpu_broker_environment='prod')
    runner = runner_module.MonomerMdRunner(settings)
    assert not runner.recovery_resources_released() and kernel.calls == []
    runner._settings = replace(settings, gpu_broker_enabled=True)
    assert not runner.recovery_resources_released() and kernel.calls == []


@pytest.mark.parametrize('overrides', [{'mode': 'dry-run'}, {'gpu_broker_enabled': True},
    {'gpu_broker_environment': 'dev'}, {'acceptance_attachment_id': 'r2-a-20261019-md'}])
def test_native_unit_recovery_is_only_an_explicit_nonbroker_production_contract(tmp_path, overrides):
    values = dict(mode='real', gpu_broker_environment='prod', recovery_systemd_unit=UNIT); values.update(overrides)
    with pytest.raises(ValueError): replace(_settings(tmp_path), **values)


@pytest.mark.parametrize('unit', ['/tmp/unit.service', '../unit.service', '-unit.service', 'unit.scope', 'unit.service\n', '', 'unit service'])
def test_unit_configuration_rejects_paths_flags_and_nonservices(tmp_path, unit):
    with pytest.raises(ValueError): replace(_settings(tmp_path), mode='real', gpu_broker_environment='prod', recovery_systemd_unit=unit)


def test_load_settings_defaults_to_no_recovery_proof_and_explicit_production_optin(monkeypatch):
    for key in ('MONOMER_MD_RECOVERY_SYSTEMD_UNIT', 'MONOMER_MD_GPU_BROKER_ENABLED', 'MONOMER_MD_WORKER_MODE'):
        monkeypatch.delenv(key, raising=False)
    assert load_settings().recovery_systemd_unit is None
    monkeypatch.setenv('MONOMER_MD_WORKER_MODE', 'real'); monkeypatch.setenv('MONOMER_MD_GPU_BROKER_ENVIRONMENT', 'prod')
    monkeypatch.setenv('MONOMER_MD_RECOVERY_SYSTEMD_UNIT', UNIT)
    assert load_settings().recovery_systemd_unit == UNIT


def test_native_recovery_never_reconciles_database_until_old_children_are_absent(kernel, tmp_path, monkeypatch):
    from workers.monomer_md_worker.app import main
    settings = production_settings(tmp_path, app_postgres_dsn='postgresql://synthetic.invalid/database')
    runner = runner_module.MonomerMdRunner(settings)
    calls = []
    repository = SimpleNamespace(reconcile_cancel_requested_jobs=lambda: calls.append('cancel') or 0,
        reconcile_orphaned_jobs=lambda instance: calls.append(('recover', instance)) or 0)
    monkeypatch.setattr(main, 'settings', settings); monkeypatch.setattr(main, 'runner', runner)
    monkeypatch.setattr(main, 'repository', repository); monkeypatch.setattr(main, 'recovery_ready', False)
    (kernel.root / 'cgroup.procs').write_text(f'{kernel.pid}\n999999\n')
    assert asyncio.run(main._attempt_recovery()) is False and calls == [] and not main.recovery_ready
    (kernel.root / 'cgroup.procs').write_text(f'{kernel.pid}\n')
    assert asyncio.run(main._attempt_recovery()) is True and len(calls) == 2 and main.recovery_ready


def test_cleanup_wait_retains_capacity_until_kernel_proves_absence(kernel, tmp_path, monkeypatch):
    from workers.monomer_md_worker.app import main
    settings = production_settings(tmp_path, app_postgres_dsn='postgresql://synthetic.invalid/database')
    runner = runner_module.MonomerMdRunner(settings)
    monkeypatch.setattr(main, 'settings', settings); monkeypatch.setattr(main, 'runner', runner)
    monkeypatch.setattr(main, 'shutting_down', False)
    events = []
    async def update(*args, **kwargs): events.append(('pending', kwargs['error_category']))
    async def sleep(seconds):
        events.append(('wait', seconds)); (kernel.root / 'cgroup.procs').write_text(f'{kernel.pid}\n')
    monkeypatch.setattr(main, '_safe_update_status', update); monkeypatch.setattr(main.asyncio, 'sleep', sleep)
    (kernel.root / 'cgroup.procs').write_text(f'{kernel.pid}\n999999\n')
    assert asyncio.run(main._wait_for_safe_resource_cleanup('synthetic-job'))
    assert events == [('pending', 'resource_cleanup_unconfirmed'), ('wait', settings.recovery_retry_seconds)]


def test_nonbroker_release_requires_the_same_absence_proof_without_sticky_failure(kernel, tmp_path):
    from gpu_resource import GpuBrokerClientError
    runner = runner_module.MonomerMdRunner(production_settings(tmp_path))
    (kernel.root / 'cgroup.procs').write_text(f'{kernel.pid}\n999999\n')
    with pytest.raises(GpuBrokerClientError) as error:
        asyncio.run(runner.release_execution_lease(None))
    assert error.value.code == 'gpu_runtime_unhealthy' and not runner.gpu_admission_uncertain
    (kernel.root / 'cgroup.procs').write_text(f'{kernel.pid}\n')
    asyncio.run(runner.release_execution_lease(None))


@pytest.mark.parametrize('cancelled', [False, True])
def test_completion_and_cancel_keep_capacity_until_old_processes_are_absent(kernel, tmp_path, monkeypatch, cancelled):
    from collections import deque
    from workers.monomer_md_worker.app import main
    from workers.monomer_md_worker.app.models import JobRequest
    from workers.monomer_md_worker.app.repository import JobUpdateResult
    settings = production_settings(tmp_path, app_postgres_dsn='postgresql://synthetic.invalid/database')
    runner = runner_module.MonomerMdRunner(settings)
    observed = []; computed = []
    class Repository:
        def authorize_start(self, *args, **kwargs): return True
        def update_status(self, job, status, **kwargs):
            observed.append((status, kwargs)); return JobUpdateResult.UPDATED
    async def compute(*args, **kwargs):
        computed.append(True)
        (kernel.root / 'cgroup.procs').write_text(f'{kernel.pid}\n999999\n')
        if cancelled: raise asyncio.CancelledError
        return SimpleNamespace(output_dir=tmp_path, result={}, completed_steps=300)
    monkeypatch.setattr(runner, 'run', compute)
    monkeypatch.setattr(main, 'settings', settings); monkeypatch.setattr(main, 'runner', runner)
    monkeypatch.setattr(main, 'repository', Repository()); monkeypatch.setattr(main, 'shutting_down', False)
    monkeypatch.setattr(main, 'cancel_requested_jobs', {'owned-job'} if cancelled else set())
    monkeypatch.setattr(main, 'active_jobs', {}); monkeypatch.setattr(main, 'active_formal_jobs', set())
    monkeypatch.setattr(main, 'formal_job_queue', deque()); monkeypatch.setattr(main, 'execution_job_id', 'owned-job')
    # Fast retries exercise the actual wait loop without changing its proof.
    monkeypatch.setattr(main, 'settings', replace(settings, recovery_retry_seconds=0.001))
    async def scenario():
        start = asyncio.Event(); start.set()
        monkeypatch.setattr(main, 'job_start_events', {'owned-job': start})
        monkeypatch.setattr(main, 'active_jobs_lock', asyncio.Lock()); monkeypatch.setattr(main, 'semaphore', asyncio.Semaphore(1))
        task = asyncio.create_task(main._run_job(JobRequest(job_id='owned-job', smiles='CCO'), 300))
        main.active_jobs['owned-job'] = task
        try:
            for _ in range(1000):
                if any(value.get('progress_stage') == 'cleanup_pending' for _, value in observed): break
                await asyncio.sleep(0.001)
            assert computed and not task.done() and main.semaphore.locked()
            assert main.active_jobs['owned-job'] is task and main.execution_job_id == 'owned-job'
            assert not any(status in {'completed', 'cancelled', 'failed'} for status, _ in observed)
        finally:
            (kernel.root / 'cgroup.procs').write_text(f'{kernel.pid}\n')
            if cancelled:
                with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, timeout=2)
            else: await asyncio.wait_for(task, timeout=2)
        assert not main.semaphore.locked()
        assert observed[-1][0] == ('cancelled' if cancelled else 'failed')
        assert not any(status == 'completed' for status, _ in observed)
    asyncio.run(scenario())

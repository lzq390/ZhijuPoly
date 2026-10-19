"""Exact-worker recovery with a shared Broker; no CUDA, process, DB, or RPC."""
from dataclasses import replace
import hashlib
from types import SimpleNamespace
from pathlib import Path

import pytest

from app.runner import MonomerMdRunner
from app.repository import PostgresJobRepository
from test_runner import _settings


def request(job):
    return 'md:dev:' + hashlib.sha256(job.encode()).hexdigest()[:16]


def runner(leases=(), *, waiters=0, attached=True, uncertain=False):
    value = object.__new__(MonomerMdRunner)
    value._settings = SimpleNamespace(mode='real', gpu_broker_environment='dev',
        acceptance_attachment_id='r2-a-20261019-md' if attached else None)
    value._gpu_admission_uncertain = uncertain
    snapshot = dict(schema_version=1, broker_instance_id='unit-broker',
                    quarantined_gpus={}, leases=list(leases), waiters=waiters)
    value._gpu_broker_client = SimpleNamespace(status=lambda: snapshot)
    return value


def lease(job='foreign', **changes):
    return dict(component='md', environment='dev', request_id=request(job), status='active') | changes


def test_unrelated_main_md_lease_does_not_block_exact_worker():
    value = runner([lease()])
    assert value.recovery_resources_released(owned_job_ids=('owned',))
    assert value.recovery_resources_released(owned_job_ids=())


@pytest.mark.parametrize('status', ['reserved', 'active', 'suspect', 'terminating'])
def test_any_remaining_owned_lease_blocks_even_for_a_terminal_job(status):
    value = runner([lease('already-completed', status=status)])
    assert not value.recovery_resources_released(owned_job_ids=('already-completed',))


@pytest.mark.parametrize('jobs', [None, [], ('',), ('x'*129,), tuple(str(x) for x in range(65))])
def test_missing_or_unbounded_ownership_inventory_is_not_release_proof(jobs):
    assert not runner().recovery_resources_released(owned_job_ids=jobs)


@pytest.mark.parametrize('remaining', [
    {'component': 'md', 'environment': 'dev'}, lease(request_id=''),
    lease(request_id='md:dev:probe:unknown-instance'), {'component': 'md'},
])
def test_ambiguous_md_or_probe_identity_remains_blocked(remaining):
    assert not runner([remaining]).recovery_resources_released(owned_job_ids=())


@pytest.mark.parametrize('waiters', [True, 1, -1, None])
def test_unattributed_global_waiters_are_not_assumed_foreign(waiters):
    assert not runner(waiters=waiters).recovery_resources_released(owned_job_ids=())


def test_legacy_and_uncertain_admission_keep_the_original_conservative_boundary():
    assert not runner([lease()], attached=False).recovery_resources_released()
    assert runner([], attached=False).recovery_resources_released()
    assert not runner(uncertain=True).recovery_resources_released(owned_job_ids=())


def test_attachment_configuration_cannot_select_production_or_another_worker(tmp_path):
    base = _settings(tmp_path)
    valid = dict(mode='real', gpu_broker_enabled=True, gpu_broker_environment='dev',
        worker_id='r2-a-20261019-md', acceptance_attachment_id='r2-a-20261019-md',
        app_postgres_dsn='postgresql://unit@invalid/nexpoly_accept_r2_1c55866ff0e4')
    assert replace(base, **valid).acceptance_attachment_id == 'r2-a-20261019-md'
    for changes in [{'mode': 'dry-run'}, {'gpu_broker_environment': 'prod'},
                    {'worker_id': 'main-worker'}, {'gpu_broker_enabled': False},
                    {'app_postgres_dsn': 'postgresql://unit@invalid/production'}]:
        with pytest.raises(ValueError):
            replace(base, **(valid | changes))


def repository_fixture(monkeypatch, tmp_path, rows=()):
    import app.repository as module
    settings = replace(_settings(tmp_path), mode='real', gpu_broker_enabled=True,
        worker_id='r2-a-20261019-md', acceptance_attachment_id='r2-a-20261019-md',
        app_postgres_dsn='postgresql://unit@invalid/nexpoly_accept_r2_1c55866ff0e4')
    calls = []

    class Cursor:
        rowcount = 0
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, statement, parameters):
            calls.append((statement.as_string(), parameters))
        def fetchall(self): return list(rows)

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def cursor(self): return Cursor()

    monkeypatch.setattr(module.psycopg, 'connect', lambda dsn: Connection())
    monkeypatch.setattr(module, 'metering_enabled', lambda: False)
    monkeypatch.setattr(module.ne, 'enabled', lambda: False)
    return PostgresJobRepository(settings), calls


def test_recovery_inventory_includes_terminal_jobs_but_only_the_exact_worker(monkeypatch, tmp_path):
    repository, calls = repository_fixture(monkeypatch, tmp_path, [('old-completed',), ('old-running',)])
    assert repository.recovery_job_ids() == ('old-completed', 'old-running')
    statement, parameters = calls[0]
    assert 'WHERE "worker_id" = %s' in statement and 'LIMIT 65' in statement
    assert 'status' not in statement and parameters == ('r2-a-20261019-md',)


def test_cancel_reconciliation_cannot_update_other_worker_rows(monkeypatch, tmp_path):
    repository, calls = repository_fixture(monkeypatch, tmp_path)
    assert repository.reconcile_cancel_requested_jobs() == 0
    statement, parameters = calls[0]
    assert 'WHERE "status" = \'cancel_requested\' AND "worker_id" = %s' in statement
    assert parameters[-1] == 'r2-a-20261019-md'


def test_overflowing_history_is_not_truncated_into_release_proof(monkeypatch, tmp_path):
    repository, _ = repository_fixture(monkeypatch, tmp_path, [(str(index),) for index in range(65)])
    with pytest.raises(RuntimeError, match='finite scope'):
        repository.recovery_job_ids()


def test_short_cohort_cancel_recovery_is_also_exact_worker(monkeypatch,tmp_path):
    repository,calls=repository_fixture(monkeypatch,tmp_path)
    worker='monomer-md-dev-'+'a'*32
    short_root=Path('/data/lzq/gith/nexpoly-acceptance-a-20261019/.runtime/cohorts/r2-a-20261019/md-runs')
    repository._settings=replace(repository._settings,worker_id=worker,acceptance_attachment_id=None,job_root=short_root)
    assert repository._settings.acceptance_cancel_recovery_scoped
    repository.reconcile_cancel_requested_jobs()
    statement,parameters=calls[0]
    assert 'AND "worker_id" = %s' in statement and parameters[-1]==worker


def test_short_cohort_recovery_requires_native_identity_and_exact_database(tmp_path):
    base=_settings(tmp_path)
    valid=dict(mode='real',gpu_broker_enabled=True,gpu_broker_environment='dev',
        worker_id='monomer-md-dev-'+'a'*32,job_root=Path('/data/lzq/gith/nexpoly-acceptance-a-20261019/.runtime/cohorts/r2-a-20261019/md-runs'),
        app_postgres_dsn='postgresql://unit@invalid/nexpoly_accept_r2_1c55866ff0e4')
    assert replace(base,**valid).acceptance_cancel_recovery_scoped
    for changes in [{'worker_id':'main-worker'},{'mode':'dry-run'},{'gpu_broker_environment':'prod'},
                    {'app_postgres_dsn':'postgresql://unit@invalid/production'}]:
        with pytest.raises(ValueError):replace(base,**(valid|changes))
    assert not base.acceptance_cancel_recovery_scoped

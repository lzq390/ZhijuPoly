"""Maintenance survives restarts; execution budgets belong to their original jobs."""
from fastapi.testclient import TestClient
import pytest

from workers.polymer_md_worker.app.config import load_settings
from workers.polymer_md_worker.app.main import create_app
from test_manager import request, supervisor


def test_maintenance_survives_restart_and_resume_is_explicit(tmp_path):
    first, calls = supervisor(tmp_path)
    with TestClient(create_app(manager=first)) as client:
        response = client.post('/drain')
        assert response.status_code == 200
        assert response.json()['admission_reason'] == 'maintenance'
        assert response.json()['maintenance']['persisted']
    second, _ = supervisor(tmp_path)
    with TestClient(create_app(manager=second)) as client:
        assert client.get('/health').json()['draining']
        assert client.post('/jobs', json=request().model_dump(mode='json')).status_code == 503
        assert client.post('/resume').json()['accepting_jobs']
    third, _ = supervisor(tmp_path)
    with TestClient(create_app(manager=third)) as client:
        assert not client.get('/health').json()['draining']
    assert calls == []


def test_corrupt_maintenance_fails_closed(tmp_path):
    manager, _ = supervisor(tmp_path)
    manager.settings.job_root.mkdir()
    (manager.settings.job_root / '.maintenance.json').write_text('{"version":1,"draining":"false"}')
    with pytest.raises(ValueError, match='maintenance'):
        with TestClient(create_app(manager=manager)):
            pass


def test_fresh_production_worker_starts_drained(monkeypatch):
    monkeypatch.delenv('POLYMER_MD_START_DRAINED', raising=False)
    assert load_settings().start_drained
    monkeypatch.setenv('POLYMER_MD_START_DRAINED', 'FALSE-ish')
    with pytest.raises(ValueError, match='must be true or false'):
        load_settings()


def test_new_budget_and_original_budget_are_separate(tmp_path):
    manager, _ = supervisor(tmp_path)
    fresh = manager._new_record(request())
    assert manager._time_limits(fresh) == (86400, 345600)
    old = manager._new_record(request())
    old['snapshot']['results']['resource_limits'].update(stage_timeout_seconds=28800, job_timeout_seconds=172800)
    assert manager._time_limits(old) == (28800, 172800)
    old['snapshot']['results'].pop('resource_limits')
    assert manager._time_limits(old) == (28800, 172800)
    old['snapshot']['results']['resource_limits'] = {'stage_timeout_seconds': float('nan')}
    with pytest.raises(ValueError, match='persisted worker time budget'):
        manager._time_limits(old)


def test_external_probe_with_unknown_cleanup_fences_a_restarted_worker(tmp_path):
    from workers.polymer_md_worker.app.external_execution import claim_execution
    from workers.polymer_md_worker.app.journal import atomic_json
    manager, calls = supervisor(tmp_path)
    root = manager.settings.job_root; root.mkdir(mode=0o700)
    runtime = tmp_path / 'runtime'; runtime.mkdir(mode=0o700)
    evidence = runtime / 'parameter-probe/test/case/attempt'; evidence.mkdir(mode=0o700, parents=True)
    nonce, unit = 'a' * 32, 'nexpoly-polymer-md-parameter-test.scope'
    fact = {'job_root': str(root), 'runtime_root': str(runtime),
            'scope': {'nonce': nonce, 'unit': unit}, 'cleanup_confirmed': False}
    atomic_json(evidence / 'journal.json', fact)
    claim_execution(root, evidence, runtime, nonce, unit)
    with TestClient(create_app(manager=manager)) as client:
        health = client.get('/health').json()
        assert not health['accepting_jobs'] and health['admission_reason'] == 'cleanup_unconfirmed'
        assert health['fatal_error'] == 'external_cleanup_unconfirmed'
    assert calls == []
    fact['cleanup_confirmed'] = True
    atomic_json(evidence / 'journal.json', fact)
    recovered, _ = supervisor(tmp_path)
    with TestClient(create_app(manager=recovered)) as client:
        assert client.get('/health').json()['accepting_jobs']


def test_failed_maintenance_write_fences_new_execution(tmp_path, monkeypatch):
    from workers.polymer_md_worker.app import manager as module
    active, calls = supervisor(tmp_path)
    with TestClient(create_app(manager=active)) as client:
        original = module.atomic_json

        def fail_maintenance(path, data):
            if path.name == '.maintenance.json':
                raise OSError('test persistence failure')
            original(path, data)

        monkeypatch.setattr(module, 'atomic_json', fail_maintenance)
        with pytest.raises(OSError, match='test persistence failure'):
            client.post('/drain')
        state = client.get('/health').json()
        assert not state['accepting_jobs']
        assert state['fatal_error'] == 'maintenance_persistence_failed'
        assert client.post('/jobs', json=request().model_dump(mode='json')).status_code == 503
    assert calls == []

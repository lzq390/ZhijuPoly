"""The bounded GPU1 read contract and explicit native factory composition."""
from types import SimpleNamespace

import pytest

from scripts import dev_gpu_session as session
from scripts.gpu1_audit import (GPU_UUID, Gpu1AuditError, query_gpu1_inventory,
                                query_gpu1_compute_processes)


def fake_query(output, calls):
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=output)
    return run


def test_inventory_and_process_queries_always_bind_only_the_gpu1_uuid():
    calls = []
    assert query_gpu1_inventory(run=fake_query('1, '+GPU_UUID+'\n', calls)) == {1: GPU_UUID}
    assert query_gpu1_compute_processes(run=fake_query(GPU_UUID+', 123\n', calls)) == {GPU_UUID: frozenset({123})}
    for command, options in calls:
        assert command[:3] == ('nvidia-smi', '-i', GPU_UUID)
        assert options['timeout'] == 10 and options['check'] is True
    assert query_gpu1_compute_processes(run=fake_query('', [])) == {GPU_UUID: frozenset()}


@pytest.mark.parametrize('output', ['2, other\n', '1, '+GPU_UUID+'\n2, other\n', '', 'invalid'])
def test_inventory_never_accepts_another_or_ambiguous_card(output):
    with pytest.raises(Gpu1AuditError):
        query_gpu1_inventory(run=fake_query(output, []))


@pytest.mark.parametrize('output', ['other, 3\n', GPU_UUID+', 0\n', GPU_UUID+', -1\n', 'invalid'])
def test_process_rows_cannot_introduce_another_gpu_or_invalid_pid(output):
    with pytest.raises(Gpu1AuditError):
        query_gpu1_compute_processes(run=fake_query(output, []))


def test_independent_snapshot_passes_the_same_explicit_factory_to_native_systemd_inventory(monkeypatch):
    from ops.gpu_broker import server
    factory = object()
    observed = []
    reader = lambda: {GPU_UUID: frozenset()}
    monkeypatch.setattr(session, 'COHORT', None)
    monkeypatch.setattr(session, 'query_gpu1_inventory', lambda: {1: GPU_UUID})
    monkeypatch.setattr(session, 'query_gpu1_compute_processes', reader)
    monkeypatch.setattr(session, 'require_gpu1_default_compute_mode', lambda: None)
    monkeypatch.setattr(session, 'capture_compute_process_declarers', lambda pids: {})
    monkeypatch.setattr(server, 'query_docker_gpu_claims', lambda: ())

    def systemd(**kwargs):
        observed.append(kwargs)
        return ()

    monkeypatch.setattr(server, 'query_systemd_gpu_claims', systemd)
    snapshot = session.collect_target_snapshot(system_configuration_factory=factory)
    assert snapshot.process_pids == () and snapshot.systemd_claims == ()
    assert observed[0]['system_configuration_factory'] is factory
    assert observed[0]['compute_process_query'] is reader
    assert observed[0]['compute_processes'] == {GPU_UUID: frozenset()}


def test_an_independent_factory_cannot_replace_an_active_cohort_factory(monkeypatch):
    monkeypatch.setattr(session, 'COHORT', object())
    monkeypatch.setattr(session, 'query_gpu1_inventory', lambda: pytest.fail('no hardware read before this rejection'))
    with pytest.raises(session.DevGpuSessionError, match='cannot override'):
        session.collect_target_snapshot(system_configuration_factory=object())

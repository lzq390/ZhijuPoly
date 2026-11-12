from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from types import SimpleNamespace

import pytest

from scripts import verify_server_monitoring_live as verify


COLLECTOR = '12345678-1234-4234-8234-123456789abc'
START = 1794384000.0


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def snapshot(sequence=20):
    return {'collector_id': COLLECTOR, 'started_at': iso(START),
            'captured_at': iso(START + (sequence - 1) * 5 + .2), 'sequence': sequence,
            'logical_cpu_count': 8, 'memory': {'total_bytes': 1000},
            'gpus': [{'uuid': 'GPU-test'}], 'freshness': 'fresh', 'age_seconds': 1,
            'components': {'cpu': {'status': 'ok'}, 'gpu': {'status': 'ok'}}}


def event(sequence, *, status='passed'):
    sample = snapshot(sequence)
    return {'checked_at': iso(verify.epoch(sample['captured_at']) + 1),
            'captured_at': sample['captured_at'], 'sequence': sequence,
            'age_seconds': 1, 'history_points': 1, 'status': status}


def history(count=120):
    captured = START + 3601
    return {'collector_id': COLLECTOR, 'started_at': iso(START), 'captured_at': iso(captured),
            'freshness': 'fresh', 'interval_seconds': 30,
            'points': [{'timestamp': iso(captured - 1 - (count - 1 - index) * 30),
                        'cpu_percent': 10, 'memory_percent': 20,
                        'gpus': [{'uuid': 'GPU-test', 'utilization_percent': 30, 'memory_used_bytes': 100}]}
                       for index in range(count)]}


def write_events(path, events):
    raw = ''.join(json.dumps(item) + '\n' for item in events).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return raw


@pytest.fixture
def setup(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    args = SimpleNamespace(output_dir=tmp_path, base_url='http://127.0.0.1:9003',
                           duration_seconds=3600, interval_seconds=15,
                           resume=True, expected_collector_id=COLLECTOR)
    events = [event(2), event(5), event(8)]
    write_events(tmp_path / 'samples.jsonl', events)
    monkeypatch.setattr(verify.time, 'time', lambda: START + 101)
    monkeypatch.setattr(verify, 'stamp', lambda: iso(START + 101))
    return args, events


def test_legacy_resume_binds_identity_and_preserves_actual_first_check(setup):
    args, events = setup
    raw = (args.output_dir / 'samples.jsonl').read_bytes()
    loaded, metadata = verify.prepare_resume(args, snapshot(), 'zhiju000')
    assert loaded == events
    assert metadata['started_at'] == events[0]['checked_at']
    assert metadata['identity']['collector_id'] == COLLECTOR
    assert metadata['legacy_evidence']['old_samples_contain_collector_id'] is False
    assert metadata['resumes'][0]['prior_samples'] == 3
    assert (args.output_dir / 'samples.jsonl').read_bytes() == raw
    assert not (args.output_dir / 'run.json').exists()


@pytest.mark.parametrize('expected', [None, 'aaaaaaaa-1234-4234-8234-123456789abc', 'invalid'])
def test_legacy_resume_requires_trusted_exact_collector(setup, expected):
    args, _ = setup
    args.expected_collector_id = expected
    with pytest.raises(ValueError):
        verify.prepare_resume(args, snapshot(), 'zhiju000')


def test_legacy_resume_rejects_samples_from_another_sequence_timeline(setup):
    args, events = setup
    events[0]['sequence'] = 1
    events[1]['sequence'] = 2
    write_events(args.output_dir / 'samples.jsonl', events)
    with pytest.raises(ValueError, match='timeline'):
        verify.prepare_resume(args, snapshot(), 'zhiju000')


@pytest.mark.parametrize('field,value', [('interval_seconds', 20), ('duration_required_seconds', 300),
                                        ('output_dir', '/somewhere/else'), ('user', 'zhiju001')])
def test_resume_requires_original_run_settings(setup, field, value):
    args, _ = setup
    _, metadata = verify.prepare_resume(args, snapshot(), 'zhiju000')
    metadata[field] = value
    verify.atomic_private_json(args.output_dir / 'run.json', metadata)
    with pytest.raises(ValueError, match='exact output directory'):
        verify.prepare_resume(args, snapshot(), 'zhiju000')


def test_resume_rejects_changed_collector_with_metadata(setup):
    args, _ = setup
    _, metadata = verify.prepare_resume(args, snapshot(), 'zhiju000')
    verify.atomic_private_json(args.output_dir / 'run.json', metadata)
    changed = snapshot()
    changed['collector_id'] = 'aaaaaaaa-1234-4234-8234-123456789abc'
    with pytest.raises(ValueError, match='exact output directory'):
        verify.prepare_resume(args, changed, 'zhiju000')


def test_completed_result_is_never_overwritten(setup):
    args, _ = setup
    verify.atomic_private_json(args.output_dir / 'result.json', {'status': 'passed'})
    with pytest.raises(ValueError, match='completed'):
        verify.prepare_resume(args, snapshot(), 'zhiju000')


@pytest.mark.parametrize('unsafe', ['public', 'symlink', 'truncated'])
def test_existing_evidence_must_be_private_complete_regular_file(setup, unsafe):
    args, _ = setup
    path = args.output_dir / 'samples.jsonl'
    if unsafe == 'public':
        path.chmod(0o644)
    elif unsafe == 'symlink':
        original = path.with_name('original.jsonl')
        path.rename(original)
        path.symlink_to(original)
    else:
        path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(ValueError):
        verify.prepare_resume(args, snapshot(), 'zhiju000')


def test_full_hour_requires_120_recent_contiguous_populated_points():
    verify.verify_history(history(), verify.hardware_identity(snapshot()), full_hour=True)


@pytest.mark.parametrize('defect', ['missing_point', 'missing_interval', 'stale_tail', 'null_cpu', 'null_gpu', 'changed_gpu', 'changed_collector'])
def test_history_rejects_unproven_continuity(defect):
    data = history()
    if defect == 'missing_point':
        data['points'].pop(0)
    elif defect == 'missing_interval':
        data['points'][40]['timestamp'] = iso(START + 1250)
    elif defect == 'stale_tail':
        data['captured_at'] = iso(START + 3700)
    elif defect == 'null_cpu':
        data['points'][40]['cpu_percent'] = None
    elif defect == 'null_gpu':
        data['points'][40]['gpus'][0]['utilization_percent'] = None
    elif defect == 'changed_gpu':
        data['points'][40]['gpus'][0]['uuid'] = 'GPU-other'
    else:
        data['collector_id'] = 'aaaaaaaa-1234-4234-8234-123456789abc'
    with pytest.raises(ValueError):
        verify.verify_history(data, verify.hardware_identity(snapshot()), full_hour=True)


def test_resume_appends_only_actual_observation_and_reports_gap(setup):
    args, old = setup
    args.duration_seconds = 60
    original = (args.output_dir / 'samples.jsonl').read_bytes()
    replies = {'snapshot': snapshot(), 'history': history(2)}
    assert verify.observe(args, replies.__getitem__, 'zhiju000') == 0
    raw = (args.output_dir / 'samples.jsonl').read_bytes()
    assert raw.startswith(original)
    events, _ = verify.read_events(args.output_dir / 'samples.jsonl')
    assert len(events) == len(old) + 1
    report = json.loads((args.output_dir / 'result.json').read_text())
    assert report['started_at'] == old[0]['checked_at']
    assert report['elapsed_seconds'] == pytest.approx(94.8)
    assert report['status'] == 'passed_with_observer_gap'
    assert report['observer_continuity'] == 'interrupted'
    assert report['collection_history_continuity'] == 'passed'
    assert report['observer_gaps'] == [{'after_checked_at': old[-1]['checked_at'],
                                      'before_checked_at': iso(START + 101), 'seconds_between_checks': 64.8}]
    assert (args.output_dir / 'final-history.json').stat().st_mode & 0o777 == 0o600


def test_fresh_run_cannot_truncate_prior_samples(setup):
    args, _ = setup
    args.resume = False
    original = (args.output_dir / 'samples.jsonl').read_bytes()
    with pytest.raises(ValueError, match='not be truncated'):
        verify.observe(args, lambda _: snapshot(), 'zhiju000')
    assert (args.output_dir / 'samples.jsonl').read_bytes() == original

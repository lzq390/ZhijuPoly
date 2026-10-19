"""Offline fault and ownership tests. No root, systemd or GPU is used."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from ops.acceptance import root_guard as g


BOOT = '11223344-1234-5678-9012-123456789abc'
NOW = datetime(2026, 10, 19, 4, 0, tzinfo=timezone.utc)


def materials():
    before = (Path(__file__).parent/'fixtures/inventory-manifest.before.public.json').read_bytes()
    data = json.loads(before)
    data.update(source_sha='a'*40, source_tree='b'*40)
    candidate = json.dumps(data, sort_keys=True).encode()
    request = dict(schema_version=1, scope_id=g.SCOPE, source_sha='a'*40, source_tree='b'*40,
        candidate_manifest_sha256=g.digest(candidate), guard_sha256='c'*64,
        global_deadline_utc='2026-10-20T18:53:34Z')
    return before, candidate, request


def test_exact_manifest_change_preserves_all_nine_root_modules():
    before, candidate, request = materials()
    old, new = g.validate_manifests(before, candidate, request)
    assert old['files'] == new['files'] and len(new['files']) == 9
    for mutation in ['files', 'schema_version', 'extra']:
        bad = json.loads(candidate)
        if mutation == 'files':
            bad['files']['gpu_resource/client.py'] = 'd'*64
        elif mutation == 'schema_version':
            bad[mutation] = 2
        else:
            bad[mutation] = True
        raw = json.dumps(bad).encode()
        with pytest.raises(g.GuardError):
            g.validate_manifests(before, raw, {**request, 'candidate_manifest_sha256':g.digest(raw)})


@pytest.mark.parametrize('change', [
    {'scope_id':'other'}, {'global_deadline_utc':'2026-10-21T00:00:00Z'},
    {'source_sha':'a'*39}, {'source_tree':'../x'}, {'guard_sha256':'f'*63},
    {'unit':'other.service'},
])
def test_request_has_no_arbitrary_paths_commands_or_budget_reset(change):
    _, _, request = materials()
    with pytest.raises(g.GuardError):
        g.validate_request({**request, **change})


def test_deadline_is_global_and_monotonic_without_restart_reset():
    _, _, request = materials()
    plan = g.deadline_plan(request, now=NOW, monotonic=100, boot_id=BOOT)
    g.validate_plan(plan, request)
    assert g.remaining(plan, now=NOW+timedelta(seconds=50), monotonic=150, boot_id=BOOT) == g.EXECUTION_SECONDS-50
    assert g.remaining(plan, now=NOW-timedelta(seconds=50), monotonic=150, boot_id=BOOT) == g.EXECUTION_SECONDS-50
    for changes in [{'deadline_monotonic':200000}, {'execution_seconds':100000}, {'source_sha':'e'*40},
                    {'sealed_monotonic':float('nan')}, {'extra':True}]:
        with pytest.raises(g.GuardError):
            g.validate_plan({**plan, **changes}, request)
    with pytest.raises(g.GuardError, match='boot_changed'):
        g.remaining(plan, now=NOW, monotonic=200, boot_id='0'*36)
    with pytest.raises(g.GuardError, match='monotonic_clock_changed'):
        g.remaining(plan, now=NOW, monotonic=99, boot_id=BOOT)
    with pytest.raises(g.GuardError, match='insufficient'):
        g.deadline_plan(request, now=g.GLOBAL_DEADLINE-timedelta(seconds=g.EXECUTION_SECONDS-1), monotonic=100, boot_id=BOOT)


class IO:
    def __init__(self, *, current=None, pid='12', invocation='1'*32, owned=True, stop_fails=False):
        self.before, self.candidate, self.request = materials()
        self.current = current or self.candidate
        self.pid, self.invocation, self.owned, self.stop_fails = pid, invocation, owned, stop_fails
        self.calls = []

    def verify_fixed_files(self): self.calls.append('verify')
    def state(self): return {'MainPID':self.pid, 'InvocationID':self.invocation}
    def binding(self): return {'InvocationID':'1'*32}
    def owns(self, state, binding): return self.owned and state['InvocationID'] == binding['InvocationID']
    def stop(self):
        self.calls.append('stop')
        if not self.stop_fails: self.pid = '0'
    def manifest(self): return self.current
    def backup(self): return self.before
    def replace(self, data, *, expected):
        assert self.pid == '0' and g.digest(self.current) == expected
        self.calls.append('replace')
        self.current = data


def test_cleanup_stops_owned_epoch_then_compare_and_swap_restores():
    io = IO()
    result = g.restore_manifest(io.request, io)
    assert result['status'] == 'ROOT_STOPPED_OLD_MANIFEST_RESTORED'
    assert io.calls == ['verify','stop','replace','verify'] and io.current == io.before
    io.calls.clear()
    g.restore_manifest(io.request, io)
    assert io.calls == ['verify','verify']


def test_cleanup_never_stops_an_unowned_restart_or_overwrites_conflicting_manifest():
    io = IO(invocation='2'*32)
    with pytest.raises(g.GuardError, match='epoch_changed'):
        g.restore_manifest(io.request, io)
    assert io.calls == ['verify']
    io = IO(current=b'changed-by-another-owner', pid='0')
    with pytest.raises(g.GuardError, match='cas_conflict'):
        g.restore_manifest(io.request, io)
    assert io.calls == ['verify']
    io = IO(stop_fails=True)
    with pytest.raises(g.GuardError, match='stop_unconfirmed'):
        g.restore_manifest(io.request, io)
    assert 'replace' not in io.calls


def test_crash_before_binding_only_claims_fixed_manifest_in_exclusive_start_window(monkeypatch):
    _, candidate, request = materials()
    io = g.RootIO(request)
    monkeypatch.setattr(io, 'manifest', lambda:candidate)
    monkeypatch.setattr(g, 'boot_identity', lambda:BOOT)
    monkeypatch.setattr(Path, 'exists', lambda path:True)
    intent = dict(scope_id=g.SCOPE, boot_id=BOOT, start_monotonic_us=1000000, end_monotonic_us=21000000)
    monkeypatch.setattr(g, 'read_root', lambda path:json.dumps(intent).encode())
    state = dict(MainPID='12', InvocationID='1'*32, ActiveState='active', ExecMainStartTimestampMonotonic='2000000')
    assert io.owns(state,None)
    assert not io.owns({**state,'ExecMainStartTimestampMonotonic':'999999'},None)
    assert not io.owns({**state,'ExecMainStartTimestampMonotonic':'21000001'},None)
    monkeypatch.setattr(io, 'manifest', lambda:b'foreign')
    assert not io.owns(state,None)
    monkeypatch.setattr(io, 'manifest', lambda:candidate)
    intent['boot_id']='0'*36
    assert not io.owns(state,None)
    assert not io.owns({**state,'InvocationID':'2'*32},state)


def test_no_user_copy_root_execution_or_arbitrary_systemd_target(monkeypatch):
    monkeypatch.setattr(g.os, 'geteuid', lambda:0)
    with pytest.raises(g.GuardError, match='root_owned_installation'):
        g.root_only()
    commands=[]
    monkeypatch.setattr(g.subprocess,'run',lambda command,**kwargs:commands.append(command) or type('P',(),{'stdout':''})())
    g.systemctl('show')
    assert commands[0][-2:] == ['--',g.SERVICE]
    with pytest.raises(g.GuardError,match='unlisted'):
        g.systemctl('restart other.service')
    assert len(commands)==1


def test_duplicate_json_is_rejected():
    with pytest.raises(g.GuardError):
        g.document(b'{"scope_id":"x","scope_id":"y"}')


def test_root_mailbox_only_stops_fixed_campaign_for_kernel_peer_owner():
    _,_,request = materials()
    message = dict(operation='stop_restore',scope_id=g.SCOPE,
                   candidate_manifest_sha256=request['candidate_manifest_sha256'])
    raw = json.dumps(message).encode()
    assert g.stop_message_allowed(raw,(12,1001,1001),request)
    for credentials in [(12,0,0),(12,1002,1001),(12,1001,0),(0,1001,1001)]:
        assert not g.stop_message_allowed(raw,credentials,request)
    for change in [{'operation':'start'},{'operation':'exec'},{'scope_id':'other'},
                   {'candidate_manifest_sha256':'f'*64},{'unit':'other.service'}]:
        assert not g.stop_message_allowed(json.dumps({**message,**change}).encode(),(12,1001,1001),request)
    assert not g.stop_message_allowed(b'x'*1025,(12,1001,1001),request)

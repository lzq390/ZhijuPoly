"""Offline policy, timers and no-submit/no-root boundaries of the short window."""
from datetime import timedelta
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import acceptance_short_window as w
from scripts import acceptance_cpu_gate as g


def test_short_window_defaults_do_not_submit_or_automatically_fault():
    value=w.plan()
    assert value['dry_run'] and value['automatic_scientific_submissions']==0
    assert not value['automatic_fault_loop'] and not value['sudo_executed']
    assert value['Main_pause_soft_seconds']==3300 and value['Main_pause_hard_seconds']==3600
    assert value['execution_ready'] is False


def test_short_window_does_not_reset_or_overrun_increment_deadline():
    w.require_short_budget(w.a.GLOBAL_DEADLINE-timedelta(seconds=3900))
    with pytest.raises(w.n.Blocked,match='increment_deadline'):
        w.require_short_budget(w.a.GLOBAL_DEADLINE-timedelta(seconds=3899))


def test_Main_pause_preflight_requires_fresh_owner_member_contexts(monkeypatch):
    raw=b'public owner context receipt'
    monkeypatch.setattr(w,'CURRENT',{'source_sha':'a'*40,'owner_sessions_receipt_sha256':w.a.sha(raw)})
    owners=['b57ded4a-6ee7-4200-acb3-e7f86fbe1016','f2a3e9cf-2978-4138-9c79-ced36861d7f3']
    value=dict(scope_id=w.a.SCOPE,source_sha='a'*40,admitted=True,authenticated_owners=owners,
        member_owners=owners,must_change_password=False,session_policy_hours=36,at_utc=w.c.now().isoformat())
    monkeypatch.setattr(w.c,'read_public',lambda path:(raw,value))
    w.owner_gate()
    value['authenticated_owners']=[]
    with pytest.raises(w.n.Blocked,match='contexts_not_ready'):w.owner_gate()


def test_unarmed_restore_sends_no_native_control(monkeypatch,tmp_path):
    monkeypatch.setattr(w,'BASE',tmp_path)
    monkeypatch.setattr(w.n,'native',lambda *args,**kw:pytest.fail('unarmed restore must not control anything'))
    assert w.restore()['Main_control_sent'] is False


def test_scientific_fault_choices_do_not_include_automatic_broker_or_backend_exit():
    for case in ['broker-exit','backend-exit','foreign-worker']:
        with pytest.raises(w.n.Blocked,match='not_in_prepared'):
            w.fault(case)


def test_fault_refuses_foreign_owner_before_native_control(monkeypatch):
    monkeypatch.setattr(w,'CURRENT',{'source_sha':'a'*40})
    monkeypatch.setattr(w.n,'CONFIG_SHA','b'*64,raising=False)
    witness=dict(scope_id=w.a.SCOPE,at_utc=w.c.now().isoformat(),case_id='isolated_md_interrupt',
        job_id='c'*32,owner_user_id='foreign-owner',source_sha='a'*40,cohort_sha256='b'*64,phase='running')
    monkeypatch.setattr(w.c,'read_public',lambda path:(b'',witness))
    monkeypatch.setattr(w.n,'fault',lambda case:pytest.fail('foreign witness cannot reach native fault'))
    with pytest.raises(w.n.Blocked,match='owner_running'):w.fault('md-worker-exit')


def test_expired_witness_does_not_create_an_extra_science_attempt(monkeypatch):
    monkeypatch.setattr(w,'CURRENT',{'source_sha':'a'*40})
    monkeypatch.setattr(w.n,'CONFIG_SHA','b'*64,raising=False)
    witness=dict(scope_id=w.a.SCOPE,at_utc=(w.c.now()-timedelta(seconds=16)).isoformat(),
        case_id='isolated_md_interrupt',job_id='c'*32,owner_user_id='b57ded4a-6ee7-4200-acb3-e7f86fbe1016',
        source_sha='a'*40,cohort_sha256='b'*64,phase='running')
    monkeypatch.setattr(w.c,'read_public',lambda path:(b'',witness))
    with pytest.raises(w.n.Blocked,match='stale'):w.fault('md-worker-exit')


def test_selected_native_helper_source_has_no_root_privilege_or_automatic_entrypoint():
    source=Path(w.n.__file__).read_text()
    tree=ast.parse(source)
    assert '/usr/bin/sudo' not in source and 'def run(' not in source
    assert not any(isinstance(n,ast.If) for n in tree.body)
    assert not any(isinstance(n,ast.FunctionDef) and n.name in {'provider_stop','guard','restore'} for n in tree.body)


def test_cpu_gate_executes_only_native_readonly_schema_preflight():
    text=g.NATIVE_READONLY
    assert "default_transaction_read_only=on" in text
    assert 'AuthService.assert_application_ready' in text
    assert not any(word in text for word in ['initial-login','save-existing-passwords','auth.login(','change_password(','hash_password('])
    assert 'nexpoly_accept_r2_1c55866ff0e4' in text

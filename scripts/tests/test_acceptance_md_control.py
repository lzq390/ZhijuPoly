"""Offline controller boundaries. No launch, database, systemd or GPU calls."""
from datetime import datetime,timezone,timedelta
import json
from types import SimpleNamespace

import pytest

from scripts import acceptance_md_control as c


def resources():
    sizes=[(100,3*1024**3),(50,512*1024**2),(100,1536*1024**2),(400,8*1024**3),(25,256*1024**2),(10,128*1024**2),(100,256*1024**2),(5,96*1024**2)]
    names=['cpu-backend','preview','md-supervisor-worker','md-execution-scope','md-auditor','root-guard','root-inventory-provider','gpu-status-publisher']
    return dict(phase='long-md',whole_host_limit=False,owned_storage_bytes=1024,
        allocations=[dict(name=n,cpu_percent=cpu,memory_bytes=mem,swap_bytes=0,enforced=True)
                     for n,(cpu,mem) in zip(names,sizes)])


def test_aggregate_resources_include_retained_cpu_preview_and_root():
    receipt=resources();c.validate_resources(receipt)
    assert sum(r['cpu_percent'] for r in receipt['allocations'])==790
    receipt['allocations'][-1]['cpu_percent']=200
    with pytest.raises(c.a.AttachmentError,match='aggregate'):c.validate_resources(receipt)
    receipt=resources();receipt['allocations'].pop()
    with pytest.raises(c.a.AttachmentError,match='incomplete'):c.validate_resources(receipt)
    receipt=resources();receipt['owned_storage_bytes']=24*1024**3+1
    with pytest.raises(c.a.AttachmentError,match='storage'):c.validate_resources(receipt)
    receipt=resources();receipt['allocations'][0]['enforced']=False
    with pytest.raises(c.a.AttachmentError,match='unproven'):c.validate_resources(receipt)


def test_clock_is_clamped_to_root_and_global_without_reset(monkeypatch):
    monkeypatch.setattr(c,'boot',lambda:'boot')
    monkeypatch.setattr(c.time,'monotonic',lambda:200)
    monkeypatch.setattr(c,'now',lambda:c.a.GLOBAL_DEADLINE-timedelta(seconds=1000))
    seal=dict(boot_id='boot',sealed_monotonic=100,deadline_monotonic=500)
    assert c.remaining(seal)==300
    seal['deadline_monotonic']=200000
    with pytest.raises(c.a.AttachmentError):c.remaining(seal)
    seal['deadline_monotonic']=float('nan')
    with pytest.raises(c.a.AttachmentError):c.remaining(seal)


def test_no_other_systemd_unit_can_be_queried_or_controlled(monkeypatch):
    calls=[]
    monkeypatch.setattr(c.subprocess,'run',lambda cmd,**kw:calls.append(cmd) or SimpleNamespace(
        stdout='MainPID=12\nInvocationID='+'a'*32+'\nActiveState=active\n'))
    assert c.unit_state(c.UNIT)['MainPID']=='12'
    assert calls[0][-2:]==['--',c.UNIT] and calls[0][2]=='show'
    with pytest.raises(c.a.AttachmentError):c.unit_state('nexpoly-production.service')
    assert len(calls)==1


def test_worker_control_paths_are_finite_before_connection():
    for method,path in [('POST','/jobs/foreign/cancel'),('POST','/shutdown'),('GET','http://other.invalid/health')]:
        with pytest.raises(c.a.AttachmentError,match='unlisted'):c.worker_request({'pid':1},method,path)


def test_cancel_has_durable_no_retry_intent(monkeypatch,tmp_path):
    monkeypatch.setattr(c.a,'RUNTIME',tmp_path)
    io=object.__new__(c.IO);calls=[];job='1'*32
    monkeypatch.setattr(io,'verify_worker',lambda binding:None)
    monkeypatch.setattr(io,'jobs',lambda:(job,))
    monkeypatch.setattr(c,'worker_request',lambda *args:calls.append(args))
    io.cancel_owned(job,{'pid':1});io.cancel_owned(job,{'pid':1})
    assert len(calls)==1
    with pytest.raises(c.a.AttachmentError):io.cancel_owned('2'*32,{'pid':1})


def test_absence_requires_exact_owner_leases_probe_waiters_and_quarantine(monkeypatch):
    io=object.__new__(c.IO)
    record={'pid':12,'start_ticks':20,'worker_instance_id':'a'*32}
    monkeypatch.setattr(c.RECORD.__class__,'exists',lambda path:True)
    monkeypatch.setattr(c.process,'load_record',lambda path:record)
    monkeypatch.setattr(c.a.native,'_canonical_broker_leases',lambda status:status['leases'])
    monkeypatch.setattr(c.time,'sleep',lambda seconds:None)
    def run(status):
        io.broker=SimpleNamespace(status=lambda:status)
        ticks=iter([0,0,2]);monkeypatch.setattr(c.time,'monotonic',lambda:next(ticks))
        return io.wait_owned_resources(frozenset({'md:dev:owned'}),timeout_seconds=1)
    base=dict(leases=[],waiters=0,quarantined_gpus={})
    assert run(base)=='ABSENT_EXACT_OWNED_RESOURCES'
    for change in [{'waiters':1},{'quarantined_gpus':{'1':'uncertain'}}]:assert run({**base,**change})=='UNKNOWN'
    own=SimpleNamespace(component='md',environment='dev',request_id='md:dev:owned',owner_pid=12,owner_process_start_ticks=20)
    foreign=SimpleNamespace(component='md',environment='dev',request_id='md:dev:foreign',owner_pid=99,owner_process_start_ticks=90)
    assert run({**base,'leases':[own]})=='UNKNOWN'
    assert run({**base,'leases':[foreign]})=='ABSENT_EXACT_OWNED_RESOURCES'
    foreign.request_id='md:dev:probe:foreign'
    assert run({**base,'leases':[foreign]})=='UNKNOWN'

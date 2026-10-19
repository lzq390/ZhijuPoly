from datetime import datetime,timedelta,timezone
import copy
from types import SimpleNamespace

import pytest

from scripts import acceptance_md_attachment as a


def restored(now):
    return dict(at_utc=now.isoformat(),source_sha=a.MAIN_SOURCE,source_tree=a.MAIN_TREE,
        direct_start=True,controller_status='ready',contaminated=False,full_native_factory=True,
        controller_pid=10,controller_start_ticks=20,session_id='a'*32,broker_instance_id='b'*32)


def test_original_mode_source_and_fresh_successor_identity_required():
    now=datetime(2026,10,19,5,tzinfo=timezone.utc)
    value=restored(now)
    a.verify_main_restore(value,now=now)
    a.verify_main_restore({**value,'controller_pid':999,'controller_start_ticks':1000,'session_id':'c'*32},now=now)
    for change in [{'source_sha':'0'*40},{'direct_start':False},{'controller_status':'stopped'},
                   {'contaminated':True},{'full_native_factory':False},{'controller_pid':True}]:
        with pytest.raises(a.AttachmentError):a.verify_main_restore({**value,**change},now=now)
    with pytest.raises(a.AttachmentError):a.verify_main_restore(value,now=now+timedelta(seconds=901))


def test_transport_retains_setup_and_cleanup_reserves_without_clock_reset():
    a.require_transport_budget(now=a.GLOBAL_DEADLINE-timedelta(seconds=83100))
    with pytest.raises(a.AttachmentError):a.require_transport_budget(now=a.GLOBAL_DEADLINE-timedelta(seconds=83099))
    assert a.plan()['execution_ready'] is False and a.plan()['dry_run'] is True


def status():
    return dict(broker_instance_id='b'*32,next_fencing_token=1,lease_authority_sequence=1,draining=False,leases=[])


def auditor(monkeypatch,*,statuses=None,snapshots=None,mps=None):
    calls=[]
    monkeypatch.setattr(a.native,'snapshot_fingerprint',lambda s:s)
    monkeypatch.setattr(a.native,'broker_authority_token',lambda s:(s['broker_instance_id'],s['next_fencing_token']))
    ss=iter(statuses or [status(),status(),status()]); snap=iter(snapshots or ['same','same']); mm=iter(mps or ['mps','mps'])
    obj=a.FullAudit(source_sha='a'*40,source_tree='c'*40,broker_instance_id='b'*32,
        collector=lambda **kw:calls.append(kw['system_configuration_factory']) or next(snap),
        mps_reader=lambda:next(mm),classifier=lambda *args:None)
    client=SimpleNamespace(status=lambda:next(ss))
    return obj,client,calls


def test_identical_explicit_native_factory_for_initial_and_trailing(monkeypatch):
    obj,client,calls=auditor(monkeypatch)
    # The real collector returns a native dataclass; supply its shape for counts.
    snap=SimpleNamespace(process_pids=(1,),docker_claims=(),systemd_claims=())
    obj.collector=lambda **kw:calls.append(kw['system_configuration_factory']) or snap
    monkeypatch.setattr(a.native,'snapshot_fingerprint',lambda s:'same')
    value=obj.capture(client)
    assert len(calls)==2 and calls[0] is calls[1] is obj.factory
    assert obj.factory.keywords==dict(expected_source_sha='a'*40,expected_source_tree='c'*40)
    assert value['native_full_factory'] and not value['global_drain_or_release_sent']


@pytest.mark.parametrize('kind',['broker_epoch','broker_authority','mps','inventory'])
def test_audit_rejects_changed_epoch_mps_and_inventory(monkeypatch,kind):
    sts=[status(),status(),status()]
    if kind=='broker_epoch':sts[0]['broker_instance_id']='f'*32
    if kind=='broker_authority':sts[2]['next_fencing_token']=2
    obj,client,_=auditor(monkeypatch,statuses=sts,
        snapshots=['first','changed'] if kind=='inventory' else None,
        mps=['mps','changed'] if kind=='mps' else None)
    with pytest.raises(a.AttachmentError):obj.capture(client)
    assert obj.sequence==0


def test_worker_environment_has_unique_identity_and_no_global_or_production_control():
    public=dict(source_sha='a'*40,source_tree='b'*40,worker_lock_sha256='sha256:'+'c'*64,
        byteff2_root='/frozen/byteff2',openmm_dir='/frozen/openmm')
    value=a.worker_environment(public,private_service_dsn='postgresql://unit@invalid/nexpoly_accept_r2_1c55866ff0e4')
    assert value['MONOMER_MD_WORKER_ID']==a.WORKER==value['MONOMER_MD_ACCEPTANCE_ATTACHMENT_ID']
    assert value['MONOMER_MD_GPU_BROKER_SOCKET_PATH']==str(a.MAIN_GPU_ROOT/'broker.sock')
    assert value['MONOMER_MD_FORMAL_TIMEOUT_SECONDS']=='79200'
    assert 'NEXPOLY_DEV_GPU_SESSION_ID' not in value and 'LD_PRELOAD' not in value
    with pytest.raises(a.AttachmentError):a.worker_environment(public,private_service_dsn='postgresql://unit@invalid/production')


def test_cleanup_never_drains_or_stops_main_and_requires_absence():
    calls=[]
    io=SimpleNamespace(verify_worker=lambda b:calls.append(('verify',b)),
        cancel_owned=lambda j,b:calls.append(('cancel',j)),
        terminate_worker_pidfd=lambda b:calls.append(('pidfd',b)),
        wait_owned_resources=lambda r,**kw:'ABSENT_EXACT_OWNED_RESOURCES')
    job='1'*32
    result=a.selective_cleanup(worker_binding='bound',owned_jobs=(job,),io=io)
    assert calls==[('verify','bound'),('cancel',job),('pidfd','bound')]
    assert result['Main_Broker_MPS_stopped'] is False
    io.wait_owned_resources=lambda *args,**kwargs:'UNKNOWN'
    with pytest.raises(a.AttachmentError):a.selective_cleanup(worker_binding='bound',owned_jobs=(job,),io=io)

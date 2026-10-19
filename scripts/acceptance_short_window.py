"""Finite Main-pause window around the new isolated candidate.

Default: dry-run plan. No automatic scientific job, fault loop, sudo, Root
start/stop or credential file read. Native user commands remain opaque.
"""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import acceptance_md_control as c
from scripts import acceptance_md_attachment as a
from scripts import acceptance_short_native as n
from scripts.dev_gpu_cohort import DevGpuCohort

BASE=ROOT/'.runtime/acceptance-a-20261019/short-window'
BINDING=BASE/'binding.public.json'
CONFIG=ROOT/'.runtime/cohorts/r2-a-20261019/cohort.json'
SOURCE_PROOF=ROOT/'scripts/acceptance_original_main_proof.py'
UNIT='nexpoly-r2-a-20261019-short-window.service'
GUARD_UNIT='nexpoly-r2-a-20261019-Main-restore.service'
SOFT=3300
HARD=3600
CURRENT=None


def plan():
    return {'scope_id':a.SCOPE,'dry_run':True,'Main_pause_soft_seconds':SOFT,'Main_pause_hard_seconds':HARD,
        'window_unit':UNIT,'independent_restore_unit':GUARD_UNIT,
        'Root_already_separately_reviewed_and_running_required':True,
        'Root_retained_only_for_approved_independent_MD':True,'sudo_executed':False,
        'automatic_scientific_submissions':0,'automatic_fault_loop':False,
        'native_fault_choices':['md-worker-exit','dft-worker-exit'],
        'fault_requires_fresh_exact_owner_job_witness_and_native_cohort_identity':True,
        'execution_ready':False}


def need(value,code):
    if not value:raise n.Blocked(code)


def original_mode_proof(*,live):
    metadata=SOURCE_PROOF.lstat()
    need(__import__('stat').S_ISREG(metadata.st_mode) and metadata.st_uid==1001
         and metadata.st_nlink==1 and metadata.st_size<=65536 and SOURCE_PROOF.resolve()==SOURCE_PROOF,'unsafe_public_mode_helper')
    raw=SOURCE_PROOF.read_bytes()
    need(a.sha(raw)==CURRENT['original_mode_proof_sha256'],'original_mode_helper_changed')
    q=subprocess.run(['/usr/bin/python3','-I','-S','-B',str(SOURCE_PROOF),
                      '--live' if live else '--source-only'],stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=25)
    need(q.returncode==0 and len(q.stdout)<=65536,'original_native_mode_readonly_proof_failed')
    return json.loads(q.stdout)


def cpu_gate(cohort):
    raw,result=c.read_public(ROOT/'.runtime/acceptance-a-20261019/cpu-readonly-gate.public.json')
    need(a.sha(raw)==CURRENT['cpu_gate_sha256'],'cpu_gate_receipt_changed')
    for key,value in [('status','PASS_NATIVE_CPU_READONLY_GATE'),('source_sha',CURRENT['source_sha']),
        ('source_tree',CURRENT['source_tree']),('image_id',CURRENT['image_id']),
        ('cohort_sha256',cohort.sha256),('database','nexpoly_accept_r2_1c55866ff0e4'),
        ('all_connections_read_only',True),('native_role_ledger_schema_privilege_preflight',True)]:
        need(result.get(key)==value,'cpu_database_gate_not_fixed')
    stamp=datetime.fromisoformat(result['at_utc'].replace('Z','+00:00'))
    need(stamp.tzinfo is not None and 0<=(c.now()-stamp).total_seconds()<=900,'cpu_database_gate_stale')
    from scripts.dev_gpu_cohort import verify_cpu_backend
    need(verify_cpu_backend(cohort)==result['container_id'],'actual_cpu_epoch_changed')
    return result


def resource_gate():
    raw,value=c.read_public(BASE/'resources.public.json')
    need(a.sha(raw)==CURRENT['resource_receipt_sha256'] and value.get('scope_id')==a.SCOPE
         and value.get('phase')=='short-gpu' and value.get('admitted') is True,'short_resource_gate_missing')
    rows=value.get('allocations')
    need(type(rows) is list and rows and all(type(r.get('cpu_percent')) is int and r['cpu_percent']>0
         and type(r.get('memory_bytes')) is int and r['memory_bytes']>0
         and r.get('swap_bytes')==0 and r.get('enforced') is True for r in rows),'short_limits_unproven')
    need(sum(r['cpu_percent'] for r in rows)<=800 and sum(r['memory_bytes'] for r in rows)<=16*1024**3
         and type(value.get('owned_storage_bytes')) is int and 0<=value['owned_storage_bytes']<=24*1024**3,
         'short_aggregate_resource_cap_exceeded')
    return value['owned_storage_bytes']


def worker_runtime_gate():
    raw,value=c.read_public(BASE/'worker-runtime-preflight.public.json')
    need(a.sha(raw)==CURRENT['worker_runtime_receipt_sha256']
         and value.get('scope_id')==a.SCOPE and value.get('source_sha')==CURRENT['source_sha']
         and value.get('source_tree')==CURRENT['source_tree']
         and value.get('status')=='PASS_NATIVE_WORKER_CPU_PREFLIGHT'
         and value.get('GPU_context_created') is False,'native_worker_runtime_preflight_missing')
    workers=value.get('workers')
    need(type(workers) is dict and set(workers)=={'md','dft'}
         and all(type(row) is dict and row.get('native_preflight_passed') is True
                 and row.get('requirements_lock_sha256')==a.sha((ROOT/('workers/monomer_'+name+'_worker/requirements.lock')).read_bytes())
                 and type(row.get('runtime_identity_sha256')) is str
                 and re.fullmatch('[0-9a-f]{64}',row['runtime_identity_sha256'])
                 for name,row in workers.items()),'native_worker_cpu_preflight_not_bound')
    stamp=datetime.fromisoformat(value['at_utc'].replace('Z','+00:00'))
    need(stamp.tzinfo is not None and 0<=(c.now()-stamp).total_seconds()<=900,
         'native_worker_runtime_preflight_stale')


def load(expected_hash):
    global CURRENT
    c.owner_only()
    raw,value=c.read_public(BINDING)
    fields={'schema_version','scope_id','source_sha','source_tree','image_id','dependency_lock','build_config',
        'cohort_sha256','cohort_device','cohort_inode','public_runtime_sha256','cpu_gate_sha256',
        'original_mode_proof_sha256','resource_receipt_sha256','owner_sessions_receipt_sha256','worker_runtime_receipt_sha256','candidate_manifest_sha256',
        'root_guard_binding','root_provider_binding','retain_root_for_independent_md'}
    need(a.sha(raw)==expected_hash and set(value)==fields and value['schema_version']==1
         and value['scope_id']==a.SCOPE,'short_binding_not_exact_reviewed_document')
    need(value['source_sha']==c.git('rev-parse','HEAD') and value['source_tree']==c.git('rev-parse','HEAD^{tree}'),
         'short_source_changed')
    c.git('diff','--quiet','--ignore-submodules','--');c.git('diff','--cached','--quiet','--ignore-submodules','--')
    for name in ['public_runtime_sha256','cpu_gate_sha256','original_mode_proof_sha256',
                 'resource_receipt_sha256','owner_sessions_receipt_sha256','worker_runtime_receipt_sha256','candidate_manifest_sha256','cohort_sha256']:
        need(type(value[name]) is str and re.fullmatch('[0-9a-f]{64}',value[name]),'invalid_short_public_pin')
    need(value['retain_root_for_independent_md'] in {True,False},'invalid_root_retention_selection')
    cohort=DevGpuCohort.load(CONFIG,value['cohort_sha256'],repo_root=ROOT,
        expected_identity=(value['cohort_device'],value['cohort_inode']))
    cohort.require_prepared()
    CURRENT=value
    # Only selected native helpers receive this fixed, public context.
    n.ROOT=ROOT;n.PRIMARY=a.MAIN_ROOT;n.BASE=BASE;n.CONFIG=CONFIG;n.COHORT=cohort
    n.CONFIG_SHA=cohort.sha256;n.CONFIG_IDENTITY=(cohort.device,cohort.inode)
    n.COMMIT=value['source_sha'];n.TREE=value['source_tree'];n.IMAGE_ID=value['image_id']
    n.DEPENDENCY_LOCK=value['dependency_lock'];n.BUILD_CONFIG=value['build_config']
    n.PRIMARY_COMMIT=a.MAIN_SOURCE;n.GPU=a.native.GPU_UUID;n.SOFT=SOFT;n.HARD=HARD
    n.DISK_MAX=24*1024**3;n.CANDIDATE_BASE=ROOT/'.runtime/acceptance-a-20261019'
    n.PUBLIC_RUNTIME_SHA=value['public_runtime_sha256']
    n.MAIN_NATIVE_RESTORE_PREFLIGHT_VERIFIED=False;n.MAIN_NATIVE_RESTORE_BINDING=None
    n.ROOT_NATIVE_FACTORY_AUDIT_VERIFIED=False
    n.original_mode_proof=original_mode_proof;n.require_cpu_runtime=cpu_gate
    n.ORIGINAL_MD_CGROUP=''
    n.disk_bytes=resource_gate
    a.native._select_cohort(CONFIG,cohort.sha256,identity=(cohort.device,cohort.inode))
    return cohort


def configure_main_binding(before,*,live):
    n.authorize_original_restore(before,live=live)
    worker=before['original_restore_proof']['md_process']
    n.ORIGINAL_MD_CGROUP=worker['cgroup'].removeprefix('0::')


def owner_gate():
    raw,value=c.read_public(BASE/'owner-sessions.public.json')
    owners=['b57ded4a-6ee7-4200-acb3-e7f86fbe1016','f2a3e9cf-2978-4138-9c79-ced36861d7f3']
    need(a.sha(raw)==CURRENT['owner_sessions_receipt_sha256'] and value.get('scope_id')==a.SCOPE
         and value.get('source_sha')==CURRENT['source_sha'] and value.get('admitted') is True
         and value.get('authenticated_owners')==owners and value.get('member_owners')==owners
         and value.get('must_change_password') is False and value.get('session_policy_hours')==36,
         'native_owner_contexts_not_ready')
    stamp=datetime.fromisoformat(value['at_utc'].replace('Z','+00:00'))
    need(stamp.tzinfo is not None and 0<=(c.now()-stamp).total_seconds()<=900,'native_owner_context_receipt_stale')


def require_short_budget(observed):
    need(observed.tzinfo is not None and (a.GLOBAL_DEADLINE-observed).total_seconds()>=HARD+300,
         'short_window_exceeds_original_increment_deadline')


def preflight(expected_hash):
    load(expected_hash);require_short_budget(c.now());owner_gate();worker_runtime_gate()
    cpu=cpu_gate(n.COHORT);resource_gate();c.require_root(CURRENT)
    a.require_transport_budget(now=c.now()) if CURRENT['retain_root_for_independent_md'] else None
    need(not (n.COHORT.runtime_root/'gpu-session/controller.json').exists(),'preexisting_private_gpu_session')
    before=n.primary_snapshot();configure_main_binding(before,live=True)
    n.audit_main_with_native_cohort_factory(before)
    need(n.same_authority(before,n.primary_snapshot()),'Main_changed_during_preflight')
    return before,cpu


def restore():
    if not (BASE/'armed.json').exists():
        return {'scope_id':a.SCOPE,'restoration_outcome':'NO_MAIN_WINDOW_ARMED','Main_control_sent':False}
    fd=os.open(BASE/'recovery.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        if (BASE/'finished.json').exists():return n.read('finished.json')
        before=n.read('original-before.json');configure_main_binding(before,live=False)
        # Existing authorized native-down flag stays scoped to this candidate.
        n.ROOT_NATIVE_FACTORY_AUDIT_VERIFIED=True
        result={'scope_id':a.SCOPE,'at_utc':n.utc(),'original_restored':False,
                'private_cpu_restored':False,'Root_retained':CURRENT['retain_root_for_independent_md'],'errors':[]}
        if (BASE/'main-drain-intent.json').exists():
            try:n.broker(a.MAIN_GPU_ROOT,draining=False,expected=before)
            except Exception:pass
        try:
            if (BASE/'private-start-intent.json').exists():
                n.native(True,'gpu-session-down',75,recovery=True)
            result['private_cpu_container_id']=n.verify_restored_private_cpu()
            result['private_cpu_restored']=True
        except Exception:
            result['errors'].append('private_cpu_or_owned_gpu_cleanup_unconfirmed')
        try:
            current=None
            try:current=n.primary_snapshot()
            except Exception:pass
            if current is None:
                need(result['private_cpu_restored'],'private_cpu_successor_missing_no_Main_takeover')
                n.restore_references_unchanged(before)
                # Fresh complete Root-factory double-free proof, not only NVML.
                n.free_audit()
                n.native(False,'gpu-session-up',180,recovery=True)
                current=n.primary_snapshot()
            n.native(False,'gpu-session-status',30,recovery=True)
            result['original_restored']=True;result['restored_identity']=current
            # Re-run the same Root-factory ownership collection after restore.
            configure_main_binding(current,live=True)
            n.audit_main_with_native_cohort_factory(current)
            controller=current['controller']
            public=dict(at_utc=c.now().isoformat(),source_sha=a.MAIN_SOURCE,source_tree=a.MAIN_TREE,
                direct_start=True,controller_status='ready',contaminated=False,full_native_factory=True,
                controller_pid=controller['pid'],controller_start_ticks=controller['start_ticks'],
                session_id=controller['session_id'],broker_instance_id=current['broker_instance_id'])
            n.write('Main-restore.public.json',public)
        except Exception:
            result['original_restored']=False
            result['errors'].append('original_Main_restore_or_full_factory_proof_unconfirmed')
        result['finished_at_utc']=n.utc()
        result['elapsed_seconds']=time.monotonic()-n.read('armed.json')['monotonic']
        result['within_Main_hard_limit']=result['elapsed_seconds']<=HARD
        result['restoration_outcome']='PASS' if (result['original_restored'] and result['private_cpu_restored']
            and result['within_Main_hard_limit']) else 'BLOCKED'
        if not CURRENT['retain_root_for_independent_md'] and result['original_restored']:
            try:c.root_stop_only();result['Root_stopped_old_manifest_restored']=True
            except Exception:result['Root_stopped_old_manifest_restored']=False;result['restoration_outcome']='BLOCKED'
        n.write('finished.json',result)
        return result
    finally:os.close(fd)


def guard(expected_hash,parent_pid,parent_ticks):
    load(expected_hash)
    n.write('guard.public.json',{'at_utc':n.utc(),'pid':os.getpid(),'start_ticks':n.tick(os.getpid()),'phase':'READY'})
    while True:
        if (BASE/'finished.json').exists():return
        if not (BASE/'armed.json').exists():
            if n.tick(parent_pid)!=parent_ticks:return
            time.sleep(.2);continue
        armed=n.read('armed.json')
        if n.tick(parent_pid)!=parent_ticks or time.monotonic()-armed['monotonic']>=SOFT:
            n.write('cancel.json',{'at_utc':n.utc(),'reason':'independent_Main_restore_guard'})
            if (BASE/'active-child.json').exists():
                child=n.read('active-child.json');n.stop_launcher(child['pid'],child['start_ticks'])
            restore();return
        time.sleep(.5)


def run(expected_hash):
    before,cpu=preflight(expected_hash)
    need(not (BASE/'armed.json').exists() and not (BASE/'finished.json').exists(),'existing_Main_window_no_reset')
    n.write('original-before.json',before,exclusive=True)
    guard_command=['/usr/bin/python3','-I','-B',str(Path(__file__).resolve()),'guard',
        '--binding-sha256',expected_hash,'--parent-pid',str(os.getpid()),'--parent-ticks',str(n.tick(os.getpid()))]
    stop_command='/usr/bin/python3 -I -B '+str(Path(__file__).resolve())+' restore --binding-sha256 '+expected_hash
    command=['/usr/bin/systemd-run','--user','--collect','--service-type=exec','--quiet','--unit='+GUARD_UNIT,
        '--property=Restart=no','--property=RuntimeMaxSec=3600','--property=TimeoutStopSec=300',
        '--property=KillMode=mixed','--property=CPUQuota=50%','--property=MemoryMax=536870912',
        '--property=MemorySwapMax=0','--property=TasksMax=128','--property=NoNewPrivileges=yes',
        '--property=ExecStopPost='+stop_command,*guard_command]
    q=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
    need(q.returncode==0,'independent_Main_guard_not_started')
    limit=time.monotonic()+5
    while time.monotonic()<limit:
        if (BASE/'guard.public.json').exists():
            proof=n.read('guard.public.json')
            if proof.get('pid') and proof.get('start_ticks')==n.tick(proof['pid']):break
        time.sleep(.1)
    else:raise n.Blocked('independent_Main_guard_start_timeout')
    armed=dict(at_utc=n.utc(),monotonic=time.monotonic(),cleanup_after_seconds=SOFT,hard_seconds=HARD)
    n.write('armed.json',armed,exclusive=True)
    try:
        need(n.same_authority(before,n.primary_snapshot()),'original_Main_epoch_changed_before_pause')
        n.write('main-drain-intent.json',{'at_utc':n.utc(),'broker_instance_id':before['broker_instance_id']},exclusive=True)
        drained,_=n.broker(a.MAIN_GPU_ROOT,draining=True,expected=before)
        _,clients=n.mps_clients(a.MAIN_GPU_ROOT);n.validate_residencies(drained,clients,draining=True)
        n.write('dev-stop-intent.json',{'at_utc':n.utc()},exclusive=True)
        n.native(False,'gpu-session-down',180)
        n.free_audit()
        n.write('private-start-intent.json',{'at_utc':n.utc()},exclusive=True)
        n.native(True,'gpu-session-up',240)
        ready=n.private_state();n.write('ready.public.json',{'at_utc':n.utc(),'scope_id':a.SCOPE,
            'source_sha':CURRENT['source_sha'],'cohort_sha256':n.CONFIG_SHA,'state':ready,
            'scientific_submissions_by_window':0})
        # The owner-bound native browser executes only the approved frozen
        # cases. This process never manufactures a science job or a PASS.
        while time.monotonic()-armed['monotonic']<SOFT:
            if (BASE/'finish.request.public.json').exists() or (BASE/'cancel.json').exists():break
            c.require_root(CURRENT)
            time.sleep(1)
    finally:
        restore()


def fault(case):
    need(case in {'md-worker-exit','dft-worker-exit'},'fault_not_in_prepared_scientific_cases')
    _,w=c.read_public(BASE/('witness-'+case+'.public.json'))
    required={'scope_id','at_utc','case_id','job_id','owner_user_id','source_sha','cohort_sha256','phase'}
    need(set(w)==required and w['scope_id']==a.SCOPE and w['owner_user_id']=='b57ded4a-6ee7-4200-acb3-e7f86fbe1016'
         and w['source_sha']==CURRENT['source_sha'] and w['cohort_sha256']==n.CONFIG_SHA
         and w['phase']=='running' and type(w['job_id']) is str and re.fullmatch('[0-9a-f]{32}',w['job_id']),
         'exact_owner_running_science_witness_missing')
    expected='isolated_md_interrupt' if case=='md-worker-exit' else 'isolated_dft_interrupt'
    need(w['case_id']==expected,'fault_case_mismatch')
    stamp=datetime.fromisoformat(w['at_utc'].replace('Z','+00:00'))
    need(stamp.tzinfo is not None and 0<=(c.now()-stamp).total_seconds()<=15,'science_witness_stale')
    need((BASE/'ready.public.json').exists() and not (BASE/'finished.json').exists()
         and n.budget_remaining(n.read('armed.json'),reserve=600)>240,'fault_window_not_ready')
    before=n.private_state()
    n.write('fault-'+case+'.intent.public.json',{'at_utc':n.utc(),'case_id':expected,'job_id':w['job_id']},exclusive=True)
    n.fault(case)
    value=n.observe_recovery(case,before['status_file'])
    n.write('fault-'+case+'.public.json',{'at_utc':n.utc(),'scope':'native_lifecycle_only',
        'job_id':w['job_id'],'recovery':value,'scientific_new_job_result_not_proven':True})


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',nargs='?',default='plan',choices=('plan','check','run','guard','restore','finish','fault-md','fault-dft'))
    p.add_argument('--binding-sha256');p.add_argument('--parent-pid',type=int);p.add_argument('--parent-ticks',type=int)
    args=p.parse_args()
    if args.action=='plan':print(json.dumps(plan(),sort_keys=True));return
    try:
        need(args.binding_sha256 and re.fullmatch('[0-9a-f]{64}',args.binding_sha256),'fixed_public_binding_hash_required')
        def interrupted(*_):
            n.write('cancel.json',{'at_utc':n.utc(),'reason':'owner_signal'})
        signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
        load(args.binding_sha256)
        if args.action=='check':preflight(args.binding_sha256)
        elif args.action=='run':run(args.binding_sha256)
        elif args.action=='guard':
            need(args.parent_pid and args.parent_ticks,'exact_guard_parent_required')
            guard(args.binding_sha256,args.parent_pid,args.parent_ticks)
        elif args.action=='restore':restore()
        elif args.action=='finish':n.write('finish.request.public.json',{'at_utc':n.utc()},exclusive=True)
        else:fault('md-worker-exit' if args.action=='fault-md' else 'dft-worker-exit')
    except Exception:p.exit(2,'Finite Main window blocked; no credentials or raw diagnostics exported.\n')


if __name__=='__main__':main()

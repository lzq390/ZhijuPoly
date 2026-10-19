"""Action-time-only user controller for the fixed independent MD attachment.

Default: print the public execution plan. Preparation tests never call seal,
supervise, watch or cleanup. This file cannot stop Main, its Broker/MPS, or root.
"""
from __future__ import annotations

import argparse
from datetime import datetime,timezone,timedelta
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from uuid import uuid4

SOURCE_ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE_ROOT))
from scripts import acceptance_md_attachment as a
from scripts import dev_worker_process as process

UNIT='nexpoly-r2-a-20261019-md.service'
AUDIT_UNIT='nexpoly-r2-a-20261019-md-audit.service'
ROOT_GUARD_UNIT='nexpoly-r2-a-20261019-inventory-guard.service'
ROOT_PROVIDER_UNIT='nexpoly-gpu-systemd-inventory.service'
ROOT_PLAN=Path('/run/nexpoly-r2-acceptance-20261019/plan.json')
ROOT_MANIFEST=Path('/usr/local/lib/nexpoly-gpu-systemd-inventory/manifest.json')
ACTIVATION=a.RUNTIME/'activation.public.json'
SEAL=a.RUNTIME/'seal.public.json'
RECORD=a.RUNTIME/'worker.process.public.json'
PYTHON=a.ROOT/'.venv-monomer-md-worker/bin/python'
WORKER_SOCKET=a.RUNTIME/'socket/worker.sock'
MAX_SECONDS=93900
STOP_RESERVE=300
SESSION_CLOCK_TOLERANCE=5


def now():return datetime.now(timezone.utc)


def owner_only():
    a.need((os.geteuid(),os.getegid())==(1001,1001) and SOURCE_ROOT==a.ROOT,
           'exact_nonprivileged_source_and_owner_required')


def read_public(path,*,root_owned=False):
    if root_owned:
        for parent in reversed(path.parents):
            item=parent.lstat()
            a.need(stat.S_ISDIR(item.st_mode) and item.st_uid==0 and not item.st_mode&0o022,'unsafe_root_public_ancestor')
    else:
        a.need(a.ROOT in path.parents,'public_receipt_outside_fixed_source')
        for parent in path.parents:
            if parent==a.ROOT:break
            item=parent.lstat()
            a.need(stat.S_ISDIR(item.st_mode) and not stat.S_ISLNK(item.st_mode)
                   and item.st_uid==1001 and not item.st_mode&0o022,'unsafe_public_ancestor')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        st=os.fstat(fd)
        a.need(stat.S_ISREG(st.st_mode) and st.st_uid==(0 if root_owned else 1001)
               and st.st_nlink==1 and not st.st_mode&0o022 and st.st_size<=65536,'unsafe_public_receipt')
        raw=os.read(fd,65537)
        end=os.fstat(fd);linked=path.lstat()
        identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        a.need(identity(st)==identity(end)==identity(linked) and len(raw)==st.st_size,'public_receipt_changed')
        def strict(pairs):
            result={}
            for key,value in pairs:
                a.need(key not in result,'duplicate_public_field');result[key]=value
            return result
        value=json.loads(raw,object_pairs_hook=strict)
        a.need(type(value) is dict,'invalid_public_receipt')
        return raw,value
    finally:os.close(fd)


def write_once(path,value):
    raw=(json.dumps(value,sort_keys=True)+'\n').encode()
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())


def publish(path,value):
    a.native._atomic_json(path,value,replace=True)


def git(*args):
    q=subprocess.run(['/usr/bin/git','--no-optional-locks','-C',str(a.ROOT),*args],
        env={'PATH':'/usr/bin:/bin','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null'},
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=10,check=True,text=True)
    return q.stdout.strip()


def unit_state(unit,*,system=False):
    a.need(unit in {UNIT,AUDIT_UNIT,ROOT_GUARD_UNIT,ROOT_PROVIDER_UNIT},'unlisted_unit')
    command=['/usr/bin/systemctl']+([] if system else ['--user'])+['show',
        '--property=MainPID','--property=InvocationID','--property=ActiveState','--',unit]
    q=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
        timeout=5,check=True,text=True)
    state=dict(line.split('=',1) for line in q.stdout.splitlines() if '=' in line)
    a.need(set(state)=={'MainPID','InvocationID','ActiveState'} and state['MainPID'].isdecimal(),
           'invalid_fixed_unit_state')
    return state


def active_unit(unit,binding,*,system=False):
    state=unit_state(unit,system=system)
    a.need(state==binding and state['ActiveState']=='active' and int(state['MainPID'])>0
           and re.fullmatch('[0-9a-f]{32}',state['InvocationID']),'fixed_unit_epoch_changed')


def validate_resources(receipt):
    a.need(receipt.get('phase')=='long-md' and receipt.get('whole_host_limit') is False,
           'invalid_resource_phase')
    rows=receipt.get('allocations')
    expected={'cpu-backend','preview','md-supervisor-worker','md-execution-scope','md-auditor','root-guard','root-inventory-provider','gpu-status-publisher'}
    a.need(type(rows) is list and len(rows)==len(expected)
           and {r.get('name') for r in rows}==expected,'incomplete_aggregate_resource_inventory')
    for row in rows:
        a.need(all(type(row.get(k)) is int and row[k]>0 for k in ['cpu_percent','memory_bytes'])
               and row.get('swap_bytes')==0 and row.get('enforced') is True,'resource_limit_unproven')
    a.need(sum(r['cpu_percent'] for r in rows)<=800
           and sum(r['memory_bytes'] for r in rows)<=16*1024**3,'aggregate_acceptance_limit_exceeded')
    a.need(type(receipt.get('owned_storage_bytes')) is int and 0<=receipt['owned_storage_bytes']<=24*1024**3,
           'owned_storage_cap_unproven')


def load_activation(expected_hash,*,fresh=False):
    owner_only()
    raw,value=read_public(ACTIVATION)
    a.need(a.sha(raw)==expected_hash,'activation_review_pin_changed')
    fields={'schema_version','scope_id','at_utc','global_deadline_utc','source_sha','source_tree',
            'worker_inputs','main_restore','broker_binding','root_guard_binding','root_provider_binding',
            'candidate_manifest_sha256','resource_receipt_sha256','owner_sessions_receipt_sha256','scientific_assets_receipt_sha256',
            'native_private_reference_sha256'}
    a.need(set(value)==fields and value['schema_version']==1 and value['scope_id']==a.SCOPE
           and value['global_deadline_utc']=='2026-10-20T18:53:34Z','invalid_fixed_activation')
    for key in ['candidate_manifest_sha256','resource_receipt_sha256','owner_sessions_receipt_sha256','scientific_assets_receipt_sha256','native_private_reference_sha256']:
        a.need(type(value[key]) is str and re.fullmatch('[0-9a-f]{64}',value[key]),'missing_review_receipt_pin')
    a.need(value['source_sha']==git('rev-parse','HEAD') and value['source_tree']==git('rev-parse','HEAD^{tree}'),
           'attachment_source_changed')
    git('diff','--quiet','--ignore-submodules','--');git('diff','--cached','--quiet','--ignore-submodules','--')
    a.need(value['worker_inputs']['source_sha']==value['source_sha']
           and value['worker_inputs']['source_tree']==value['source_tree'],'worker_source_differs')
    lock='sha256:'+a.sha((a.ROOT/'workers/monomer_md_worker/requirements.lock').read_bytes())
    a.need(value['worker_inputs']['worker_lock_sha256']==lock,'worker_lock_changed')
    private_raw,private_reference=read_public(a.ROOT/'.runtime/acceptance-a-20261019/native-private-reference.public.json')
    from ops.acceptance import native_private_reference as reference
    a.need(a.sha(private_raw)==value['native_private_reference_sha256']
           and private_reference.get('source_sha')==value['source_sha']
           and private_reference.get('source_tree')==value['source_tree'],
           'native_private_reference_receipt_changed')
    reference.verify(private_reference.get('reference'))
    for name,key in [('resources.public.json','resource_receipt_sha256'),('owner-sessions.public.json','owner_sessions_receipt_sha256'),('scientific-assets.public.json','scientific_assets_receipt_sha256')]:
        r,d=read_public(a.RUNTIME/name)
        a.need(a.sha(r)==value[key] and d.get('scope_id')==a.SCOPE and d.get('admitted') is True,
               'action_time_prerequisite_unproven')
        stamp=datetime.fromisoformat(d['at_utc'].replace('Z','+00:00'))
        a.need(stamp.tzinfo is not None and 0<=(now()-stamp).total_seconds()
               and (not fresh or (now()-stamp).total_seconds()<=900),'prerequisite_receipt_stale')
        if name=='resources.public.json':validate_resources(d)
        if name=='owner-sessions.public.json':
            from backend.app.auth.acceptance import OWNERS
            a.need(d.get('authenticated_owners')==list(OWNERS) and d.get('member_owners')==list(OWNERS)
                   and d.get('source_sha')==value['source_sha'] and d.get('must_change_password') is False
                   and d.get('session_policy_hours')==36,'exact_owner_contexts_unproven')
        if name=='scientific-assets.public.json':
            a.need(d.get('native_preflight_verified') is True and d.get('immutable_root_identity_verified') is True
                   and all(d.get(k)==value['worker_inputs'][k] for k in ['byteff2_root','openmm_dir']),
                   'native_scientific_asset_identity_unproven')
    return value


def require_root(value):
    active_unit(ROOT_GUARD_UNIT,value['root_guard_binding'],system=True)
    active_unit(ROOT_PROVIDER_UNIT,value['root_provider_binding'],system=True)
    raw,_=read_public(ROOT_MANIFEST,root_owned=True)
    a.need(a.sha(raw)==value['candidate_manifest_sha256'],'root_manifest_epoch_changed')
    _,plan=read_public(ROOT_PLAN,root_owned=True)
    a.need(plan.get('source_sha')==value['source_sha'] and plan.get('source_tree')==value['source_tree']
           and plan.get('candidate_manifest_sha256')==value['candidate_manifest_sha256']
           and plan.get('boot_id')==boot() and plan.get('execution_seconds')==MAX_SECONDS
           and plan.get('global_deadline_utc')=='2026-10-20T18:53:34Z'
           and plan.get('guard_sha256')==a.sha((a.ROOT/'ops/acceptance/root_guard.py').read_bytes()),
           'root_guard_plan_changed')
    a.need(type(plan.get('deadline_monotonic')) in {int,float} and type(plan.get('sealed_monotonic')) in {int,float}
           and plan['deadline_monotonic']==plan['sealed_monotonic']+MAX_SECONDS,'invalid_root_guard_clock')
    return plan


def boot():return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def remaining(seal):
    a.need(type(seal['sealed_monotonic']) in {int,float} and type(seal['deadline_monotonic']) in {int,float}
           and __import__('math').isfinite(seal['sealed_monotonic'])
           and __import__('math').isfinite(seal['deadline_monotonic']),'invalid_attachment_clock')
    a.need(seal['boot_id']==boot() and 0<seal['deadline_monotonic']-seal['sealed_monotonic']<=MAX_SECONDS,
           'attachment_clock_or_boot_changed')
    elapsed=time.monotonic()-seal['sealed_monotonic']
    a.need(elapsed>=0,'attachment_monotonic_clock_changed')
    return min(seal['deadline_monotonic']-time.monotonic(),(a.GLOBAL_DEADLINE-now()).total_seconds()-STOP_RESERVE)


def seal_activation(expected_hash):
    value=load_activation(expected_hash,fresh=True)
    a.verify_main_restore(value['main_restore'],now=now())
    a.require_transport_budget(now=now())
    a.need((a.GLOBAL_DEADLINE-now()).total_seconds()>=MAX_SECONDS,'guard_and_attachment_execution_budget_insufficient')
    root_plan=require_root(value)
    effective_deadline=min(time.monotonic()+MAX_SECONDS,root_plan['deadline_monotonic']-STOP_RESERVE)
    a.need(effective_deadline-time.monotonic()>=a.TRANSPORT_ADMISSION_REMAINING,'root_guard_remaining_budget_insufficient')
    for p in [a.RUNTIME,a.RUNTIME/'socket',a.RUNTIME/'jobs',a.RUNTIME/'home']:
        if not p.exists():p.mkdir(mode=0o700)
        st=p.lstat();a.need(stat.S_ISDIR(st.st_mode) and st.st_uid==1001 and stat.S_IMODE(st.st_mode)==0o700,'unsafe_attachment_directory')
    a.need(not SEAL.exists() and not RECORD.exists() and not WORKER_SOCKET.exists(),'existing_attachment_no_reset')
    for unit in [UNIT,AUDIT_UNIT]:a.need(unit_state(unit)['MainPID']=='0','existing_attachment_unit')
    mono=time.monotonic()
    write_once(SEAL,dict(schema_version=1,scope_id=a.SCOPE,activation_sha256=expected_hash,
        sealed_at_utc=now().isoformat(),sealed_monotonic=mono,deadline_monotonic=effective_deadline,
        boot_id=boot(),session_id=uuid4().hex))


def loaded():
    _,seal=read_public(SEAL)
    a.need(set(seal)=={'schema_version','scope_id','activation_sha256','sealed_at_utc',
           'sealed_monotonic','deadline_monotonic','boot_id','session_id'} and seal['scope_id']==a.SCOPE
           and re.fullmatch('[0-9a-f]{32}',seal['session_id']),'invalid_attachment_seal')
    return seal,load_activation(seal['activation_sha256'])


class BrokerStatus:
    """Only status, with peer credentials on the same request connection."""
    def __init__(self,binding):
        required={'pid','start_ticks','socket_device','socket_inode','instance_id'}
        a.need(type(binding) is dict and set(binding)==required,'invalid_broker_binding')
        for name in required-{'instance_id'}:a.need(type(binding[name]) is int and binding[name]>0,'invalid_broker_binding')
        a.need(re.fullmatch('[0-9a-f]{32}',binding['instance_id']),'invalid_broker_instance')
        self.binding=binding;self.path=a.MAIN_GPU_ROOT/'broker.sock'

    def status(self):
        before=self.path.lstat()
        a.need(stat.S_ISSOCK(before.st_mode) and before.st_uid==1001 and not stat.S_ISLNK(before.st_mode)
               and (before.st_dev,before.st_ino)==(self.binding['socket_device'],self.binding['socket_inode']),
               'broker_socket_changed')
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(5);connection.connect(str(self.path))
            peer=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            a.need(peer==(self.binding['pid'],1001,1001)
                   and process.process_start_ticks(peer[0])==self.binding['start_ticks'],'broker_peer_changed')
            connection.sendall(b'{"action":"status","schema_version":1}\n')
            data=bytearray()
            while not data.endswith(b'\n'):
                part=connection.recv(min(65536,1048577-len(data)))
                a.need(part and len(data)+len(part)<=1048576,'invalid_broker_status_size');data.extend(part)
            a.need(process.process_start_ticks(peer[0])==self.binding['start_ticks'],'broker_peer_changed')
        after=self.path.lstat()
        a.need((before.st_dev,before.st_ino)==(after.st_dev,after.st_ino),'broker_socket_changed')
        value=json.loads(data)
        a.need(type(value) is dict and value.get('ok') is True and type(value.get('result')) is dict,'broker_status_failed')
        result=value['result'];a.need(result.get('broker_instance_id')==self.binding['instance_id'],'broker_instance_changed')
        return result


class WorkerHTTP(http.client.HTTPConnection):
    def __init__(self,expected_pid):super().__init__('localhost',timeout=5);self.pid=expected_pid
    def connect(self):
        st=WORKER_SOCKET.lstat()
        a.need(stat.S_ISSOCK(st.st_mode) and st.st_uid==1001 and stat.S_IMODE(st.st_mode)==0o600,'unsafe_worker_socket')
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(self.timeout)
        self.sock.connect(str(WORKER_SOCKET))
        a.need(struct.unpack('3i',self.sock.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))==(self.pid,1001,1001),
               'worker_socket_peer_changed')


def worker_request(record,method,path):
    a.need((method,path) in {('GET','/health'),('POST','/drain')}
           or method=='POST' and re.fullmatch('/jobs/[0-9a-f]{32}/cancel',path),'unlisted_worker_request')
    with WorkerHTTP(record['pid']) as connection:
        connection.request(method,path,body=b'' if method=='POST' else None)
        response=connection.getresponse();body=response.read(1048577)
        a.need(response.status==200 and len(body)<=1048576,'worker_native_request_failed')
        return json.loads(body)


class IO:
    def __init__(self,seal,value):
        self.seal,self.value=seal,value
        private=os.environ.get('APP_SERVICE_POSTGRES_DSN','')
        a.need(bool(private),'native_opaque_service_environment_required')
        self.environment=a.worker_environment(value['worker_inputs'],private_service_dsn=private)
        self.common=dict(python=PYTHON,socket=WORKER_SOCKET,source_sha=value['source_sha'],
            source_tree=value['source_tree'],worker_lock_sha256=value['worker_inputs']['worker_lock_sha256'],
            session_id=seal['session_id'])
        self.broker=BrokerStatus(value['broker_binding'])

    def verify_worker(self,binding):
        record=process.verify_record(RECORD,**self.common,require_instance=bool(binding.get('worker_instance_id')))
        a.need(record==binding,'worker_record_changed')
        return record

    def jobs(self):
        # Native SELECT is scoped to the fixed worker ID, including terminal
        # jobs. DSN stays in the child's environment and stderr is discarded.
        code="import sys,json;sys.path.insert(0,"+repr(str(a.ROOT))+");from workers.monomer_md_worker.app.config import load_settings;from workers.monomer_md_worker.app.repository import PostgresJobRepository;print(json.dumps(PostgresJobRepository(load_settings()).recovery_job_ids()))"
        q=subprocess.run([str(PYTHON),'-I','-B','-c',code],env=self.environment,stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=10,check=True)
        a.need(len(q.stdout)<8192,'oversized_owned_job_inventory')
        jobs=json.loads(q.stdout);a.need(type(jobs) is list and len(jobs)<=64 and len(jobs)==len(set(jobs)),
            'invalid_owned_job_inventory')
        for job in jobs:a.request_id(job)
        return tuple(jobs)

    def cancel_owned(self,job,binding):
        self.verify_worker(binding)
        a.need(job in self.jobs(),'job_no_longer_owned_by_attachment')
        intent=a.RUNTIME/('cancel-'+job+'.public.json')
        if not intent.exists():
            write_once(intent,{'scope_id':a.SCOPE,'job_id':job,'at_utc':now().isoformat(),'attempts':1})
            worker_request(binding,'POST','/jobs/'+job+'/cancel')

    def terminate_worker_pidfd(self,binding):
        self.verify_worker(binding)
        fd=os.pidfd_open(binding['pid'])
        try:
            self.verify_worker(binding);signal.pidfd_send_signal(fd,signal.SIGTERM)
        finally:os.close(fd)

    def wait_owned_resources(self,requests,*,timeout_seconds):
        deadline=time.monotonic()+timeout_seconds
        while time.monotonic()<deadline:
            status=self.broker.status()
            leases=a.native._canonical_broker_leases(status)
            record=process.load_record(RECORD) if RECORD.exists() else getattr(self,'dead_binding',None)
            probe=('md:dev:probe:'+hashlib.sha256(('runtime-probe:'+record['worker_instance_id']).encode()).hexdigest()[:16]) if record and record.get('worker_instance_id') else None
            own=[lease for lease in leases if lease.component=='md' and lease.environment=='dev'
                 and (lease.request_id in requests or lease.request_id==probe or (record
                     and lease.owner_pid==record['pid'] and lease.owner_process_start_ticks==record['start_ticks']))]
            ambiguous_probe=any(lease.component=='md' and lease.environment=='dev'
                                and lease.request_id.startswith('md:dev:probe:') for lease in leases)
            if not own and not ambiguous_probe and status.get('waiters')==0 and status.get('quarantined_gpus')=={}:
                return 'ABSENT_EXACT_OWNED_RESOURCES'
            time.sleep(1)
        return 'UNKNOWN'


def health(record,value):
    payload=worker_request(record,'GET','/health')
    fixed={'status':'ok','mode':'real','runtime_ready':True,'db_configured':True,
        'source_sha':value['source_sha'],'source_tree':value['source_tree'],'source_root':str(a.ROOT),
        'venv_prefix':str(PYTHON.parent.parent.resolve(strict=True)),
        'worker_lock_sha256':value['worker_inputs']['worker_lock_sha256'],
        'python_executable':str(PYTHON.resolve(strict=True))}
    a.need(all(payload.get(k)==v for k,v in fixed.items()),'native_worker_identity_or_readiness_failed')
    protocol=payload.get('protocols',{}).get('Transport',{})
    a.need(protocol.get('supported') is True and protocol.get('runtime_ready') is True,'transport_runtime_not_ready')
    instance=payload.get('worker_instance_id')
    a.need(type(instance) is str and re.fullmatch('[0-9a-f]{32}',instance),'worker_instance_unproven')
    return instance


def lock(name):
    path=a.RUNTIME/(name+'.lock')
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    st=os.fstat(fd);a.need(st.st_uid==1001 and st.st_nlink==1 and stat.S_ISREG(st.st_mode),'unsafe_attachment_lock')
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    return fd


def cleanup():
    seal,value=loaded();io=IO(seal,value)
    fd=lock('cleanup')
    try:
        if not RECORD.exists():
            if (a.RUNTIME/'worker-started-once.public.json').exists():
                _,previous=read_public(a.RUNTIME/'cleanup.public.json')
                a.need(previous.get('status') in {'EXACT_OWNED_MD_RESOURCES_ABSENT','DEAD_OWNED_WORKER_RESOURCES_ABSENT'},
                       'missing_managed_worker_identity')
                return
            result={'scope_id':a.SCOPE,'status':'NO_WORKER_WAS_LAUNCHED','Main_Broker_MPS_stopped':False}
        else:
            record=process.load_record(RECORD)
            jobs=io.jobs()
            try:
                io.verify_worker(record)
            except (process.WorkerProcessRecordError,OSError):
                # Native collect-dead rejects PID reuse and wrong socket paths.
                a.need(not (Path('/proc')/str(record['pid'])).exists(),'worker_identity_changed_no_signal')
                a.need(io.wait_owned_resources(frozenset(a.request_id(j) for j in jobs),timeout_seconds=180)
                       =='ABSENT_EXACT_OWNED_RESOURCES','dead_worker_resource_cleanup_unconfirmed')
                process.collect_dead_record(RECORD,collect_socket=True,**io.common)
                result={'scope_id':a.SCOPE,'status':'DEAD_OWNED_WORKER_RESOURCES_ABSENT','Main_Broker_MPS_stopped':False}
            else:
                result=a.selective_cleanup(worker_binding=record,owned_jobs=jobs,io=io)
                deadline=time.monotonic()+60
                while time.monotonic()<deadline:
                    try:
                        process.collect_dead_record(RECORD,collect_socket=True,**io.common);break
                    except process.WorkerProcessRecordError:time.sleep(1)
                else:raise a.AttachmentError('owned_worker_exit_unconfirmed')
        result['at_utc']=now().isoformat();publish(a.RUNTIME/'cleanup.public.json',result)
    finally:os.close(fd)


def supervise():
    seal,value=loaded();fd=lock('supervise');io=IO(seal,value)
    try:
        a.need(remaining(seal)>a.TRANSPORT_ADMISSION_REMAINING,'insufficient_attachment_start_budget')
        a.verify_main_restore(value['main_restore'],now=now());require_root(value)
        _,watcher=read_public(a.RUNTIME/'watcher.public.json')
        active_unit(AUDIT_UNIT,watcher['unit_binding'])
        a.need(watcher['activation_sha256']==seal['activation_sha256'],'watcher_campaign_changed')
        _,audit=read_public(a.RUNTIME/'audit.public.json')
        stamp=datetime.fromisoformat(audit['at_utc'])
        a.need(audit['native_full_factory'] is True and 0<=(now()-stamp).total_seconds()<=15,'fresh_full_factory_required')
        write_once(a.RUNTIME/'worker-started-once.public.json',{'at_utc':now().isoformat()})
        command=a.plan()['native_worker_argv']
        child=subprocess.Popen(command,cwd=a.ROOT/'workers/monomer_md_worker',env=io.environment,
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            record=None
            for _ in range(100):
                try:
                    record=process.create_record(RECORD,pid=child.pid,expected_argv=command,**io.common);break
                except process.WorkerProcessRecordError:
                    a.need(child.poll() is None,'worker_exited_before_binding');time.sleep(.02)
            a.need(record is not None,'worker_launch_binding_failed')
            for _ in range(45):
                try:
                    st=WORKER_SOCKET.lstat()
                    a.need(stat.S_ISSOCK(st.st_mode) and st.st_uid==1001,'unsafe_worker_socket')
                    os.chmod(WORKER_SOCKET,0o600,follow_symlinks=False)
                    instance=health(record,value)
                    record=process.bind_instance(RECORD,instance,**io.common)
                    break
                except (OSError,a.AttachmentError,http.client.HTTPException):
                    a.need(child.poll() is None,'worker_exited_during_native_readiness');time.sleep(1)
            else:raise a.AttachmentError('native_worker_startup_timeout')
            publish(a.RUNTIME/'ready.public.json',{'scope_id':a.SCOPE,'at_utc':now().isoformat(),
                'source_sha':value['source_sha'],'worker_id':a.WORKER,'worker_instance_id':record['worker_instance_id'],
                'worker_socket':str(WORKER_SOCKET),'no_scientific_submit_sent':True})
            while remaining(seal)>STOP_RESERVE:
                io.verify_worker(record);require_root(value)
                active_unit(AUDIT_UNIT,watcher['unit_binding'])
                _,audit=read_public(a.RUNTIME/'audit.public.json')
                stamp=datetime.fromisoformat(audit['at_utc'])
                a.need(audit['native_full_factory'] is True and 0<=(now()-stamp).total_seconds()<=15,'audit_freshness_lost')
                time.sleep(2)
        finally:
            if RECORD.exists():cleanup()
            elif child.poll() is None:
                # Popen still owns this just-created child; no PID discovery.
                child.terminate();child.wait(timeout=30)
    finally:os.close(fd)


def watch():
    seal,value=loaded();fd=lock('watch');io=IO(seal,value);root_fd=-1
    try:
        write_once(a.RUNTIME/'watcher.public.json',{'scope_id':a.SCOPE,'at_utc':now().isoformat(),
            'activation_sha256':seal['activation_sha256'],'unit_binding':unit_state(AUDIT_UNIT)})
        root_fd=os.open(a.MAIN_GPU_ROOT,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        metadata=os.fstat(root_fd)
        a.need(metadata.st_uid==1001 and metadata.st_gid==1001 and stat.S_IMODE(metadata.st_mode)==0o700,
               'main_mps_root_not_descriptor_owned')
        from ops.gpu_broker.server import MpsRuntimeGuard
        guard=MpsRuntimeGuard(Path('/proc')/str(os.getpid())/'fd'/str(root_fd))
        def mps():
            current=a.MAIN_GPU_ROOT.lstat()
            a.need((current.st_dev,current.st_ino)==(metadata.st_dev,metadata.st_ino),'main_mps_root_changed')
            return guard.authority_snapshot(1,a.native.GPU_UUID)
        audit=a.FullAudit(source_sha=value['source_sha'],source_tree=value['source_tree'],
            broker_instance_id=value['broker_binding']['instance_id'],mps_reader=mps)
        while remaining(seal)>STOP_RESERVE:
            require_root(value)
            evidence=None
            # Read-only audit retries never retry a scientific submission.
            for attempt in range(3):
                try:evidence=audit.capture(io.broker);break
                except Exception:
                    if attempt==2:raise a.AttachmentError('bounded_native_full_audit_failed') from None
                    time.sleep(1)
            evidence['at_utc']=now().isoformat();publish(a.RUNTIME/'audit.public.json',evidence)
            time.sleep(5)
    finally:
        if root_fd>=0:os.close(root_fd)
        os.close(fd)
        if RECORD.exists():cleanup()


def root_stop_only():
    """Request only this already approved finite guard's stop/restore action."""
    owner_only()
    _,plan=read_public(ROOT_PLAN,root_owned=True)
    a.need(plan.get('scope_id')==a.SCOPE and plan.get('source_sha')==git('rev-parse','HEAD')
           and plan.get('source_tree')==git('rev-parse','HEAD^{tree}')
           and plan.get('guard_sha256')==a.sha((a.ROOT/'ops/acceptance/root_guard.py').read_bytes()),
           'root_stop_campaign_pin_changed')
    path=ROOT_PLAN.parent/'stop.sock';st=path.lstat()
    a.need(stat.S_ISSOCK(st.st_mode) and st.st_uid==0 and st.st_gid==1001
           and stat.S_IMODE(st.st_mode)==0o660,'unsafe_root_stop_socket')
    body=json.dumps({'operation':'stop_restore','scope_id':a.SCOPE,
                     'candidate_manifest_sha256':plan['candidate_manifest_sha256']}).encode()
    with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as connection:
        connection.settimeout(3);connection.sendto(body,str(path))
    deadline=time.monotonic()+40
    while time.monotonic()<deadline:
        root=unit_state(ROOT_PROVIDER_UNIT,system=True)
        if root['MainPID']=='0' and root['ActiveState'] in {'inactive','failed'}:
            try:
                _,result=read_public(ROOT_PLAN.parent/'result.public.json',root_owned=True)
                raw,_=read_public(ROOT_MANIFEST,root_owned=True)
                a.need(result.get('status')=='ROOT_STOPPED_OLD_MANIFEST_RESTORED'
                       and result.get('candidate_manifest_sha256')==plan['candidate_manifest_sha256']
                       and a.sha(raw)=='fb31f3f4d958a0227fb7081bade119bb5a7922de434fec0f4b0578a7788a1f5f',
                       'root_restore_receipt_unconfirmed')
                return
            except FileNotFoundError:pass
        time.sleep(.5)
    raise a.AttachmentError('finite_root_stop_or_rollback_unconfirmed')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',nargs='?',default='plan',choices=('plan','seal','supervise','watch','cleanup','root-stop'))
    parser.add_argument('--activation-sha256')
    args=parser.parse_args()
    if args.action=='plan':print(json.dumps(a.plan(),sort_keys=True));return
    try:
        owner_only();os.umask(0o077)
        def interrupted(_signal,_frame):raise a.AttachmentError('owned_attachment_interrupted')
        signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
        if args.action=='seal':
            a.need(args.activation_sha256 and re.fullmatch('[0-9a-f]{64}',args.activation_sha256),'explicit_reviewed_activation_hash_required')
            seal_activation(args.activation_sha256)
        else:{'supervise':supervise,'watch':watch,'cleanup':cleanup,'root-stop':root_stop_only}[args.action]()
    except Exception:
        parser.exit(2,'Independent MD control stopped closed. No credentials or raw runtime diagnostics printed.\n')


if __name__=='__main__':main()

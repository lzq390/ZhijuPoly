"""Selected native lifecycle helpers copied from the reviewed 2026-10-16 control.

Baseline SHA256: eb5dc409a7050e4d275e9254058cd071c4bbec0d4c0dd7b5e73e6de929be161f
No entry point, sudo helper, root lifecycle, or automatic fault loop is copied.
Use only through acceptance_short_window with fixed action-time public pins.
"""
import contextlib,fcntl,hashlib,importlib.util,json,os,signal,socket,stat,subprocess,sys,time,urllib.request
from pathlib import Path
from datetime import datetime,timezone

class Blocked(RuntimeError): pass

def utc():return time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())

def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != 1001 or st.st_mode & 0o077:
        raise Blocked('private_directory_identity')


def write(name, value, exclusive=False):
    private_dir(BASE)
    path = BASE / name
    raw = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(raw); out.flush(); os.fsync(out.fileno())
    else:
        temporary = BASE / ('.' + name + '.' + str(os.getpid()))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(raw); out.flush(); os.fsync(out.fileno())
        os.replace(temporary, path)


def event(phase, **fields):
    value = {'at_utc': utc(), 'phase': phase, **fields}
    private_dir(BASE)
    fd = os.open(BASE / 'events.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(value, sort_keys=True) + '\n').encode())
    finally:
        os.close(fd)
    print(json.dumps(value, sort_keys=True), flush=True)


def read(name):
    return json.loads((BASE / name).read_text())


def tick(pid):
    try:
        raw = (Path('/proc') / str(pid) / 'stat').read_text()
        return int(raw[raw.rindex(')') + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def public_bytes(path):
    if path.resolve() != path:
        raise Blocked('public_gate_noncanonical_path')
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise Blocked('public_gate_unsafe_parent')
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 1001 or before.st_gid != 1001
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size > 2 * 1024**2):
            raise Blocked('public_gate_file_metadata')
        raw = os.read(fd, 2 * 1024**2 + 1)
        after = os.fstat(fd); leaf = path.lstat()
        fields = lambda v: (v.st_dev, v.st_ino, v.st_size, v.st_mtime_ns, v.st_ctime_ns)
        if len(raw) != before.st_size or fields(before) != fields(after) or fields(before) != fields(leaf):
            raise Blocked('public_gate_file_changed')
        return raw
    finally:
        os.close(fd)


def require_main_native_restore_preflight():
    if not MAIN_NATIVE_RESTORE_PREFLIGHT_VERIFIED or MAIN_NATIVE_RESTORE_BINDING is None:
        raise Blocked('main_original_native_restore_preflight_not_verified')


def authorize_original_restore(before, *, live):
    global MAIN_NATIVE_RESTORE_PREFLIGHT_VERIFIED, MAIN_NATIVE_RESTORE_BINDING
    MAIN_NATIVE_RESTORE_PREFLIGHT_VERIFIED=False;MAIN_NATIVE_RESTORE_BINDING=None
    proof=before.get('original_restore_proof',{})
    if (proof.get('status')!='PASS_ORIGINAL_NATIVE_DIRECT_MODE_READONLY_PROOF'
            or proof.get('source_sha')!=PRIMARY_COMMIT
            or proof.get('source_tree')!=before.get('controller',{}).get('source_tree')
            or proof.get('controller')!=before.get('controller')
            or proof.get('original_native_direct_start_selection') is not True
            or proof.get('original_native_9001_launcher_only') is not True
            or proof.get('legacy_full_collector_required_by_this_original_mode') is not False
            or proof.get('new_trusted_flag_or_Main_modification') is not False
            or proof.get('native_status_ready_direct_unchanged') is not True
            or proof.get('native_activation_md_identity_checked') is not True
            or proof.get('restore_argv')!=['/bin/bash',str(PRIMARY/'scripts/dev_server_gpu.sh'),'gpu-session-up']):
        raise Blocked('original_native_mode_binding_incomplete')
    current=original_mode_proof(live=live)
    if live:
        if current!=proof:raise Blocked('original_native_live_identity_changed')
    else:
        source=current.get('source',{})
        if current.get('status')!='PASS_ORIGINAL_NATIVE_SOURCE_ONLY' or any(source.get(k)!=v for k,v in proof.items() if k in source):
            raise Blocked('original_native_restore_source_changed')
        required=('source_sha','source_tree','public_source_sha256','restore_argv',
                  'original_native_direct_start_selection','original_native_9001_launcher_only',
                  'legacy_full_collector_required_by_this_original_mode','new_trusted_flag_or_Main_modification')
        if any(source.get(k)!=proof.get(k) for k in required):raise Blocked('original_native_source_proof_incomplete')
    MAIN_NATIVE_RESTORE_BINDING=dict(before['controller'])
    MAIN_NATIVE_RESTORE_PREFLIGHT_VERIFIED=True


def checked_output(command, timeout=10):
    return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL, timeout=timeout).strip()


def broker(root, draining=None, expected=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
        c.settimeout(5); c.connect(str(root / 'broker.sock'))
        import struct
        peer = struct.unpack('3i', c.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if peer[1:] != (1001, 1001):
            raise Blocked('broker_peer_identity')
        if expected and (peer[0]!=expected['broker_pid'] or tick(peer[0])!=expected['broker_start_ticks']):
            raise Blocked('broker_instance_changed_before_admission_operation')
        request={'action':'status','schema_version':1}
        if draining is not None:
            if not isinstance(draining,bool) or expected is None:
                raise Blocked('drain_requires_exact_saved_broker')
            request.update(action='drain',draining=draining)
        c.sendall((json.dumps(request)+'\n').encode())
        raw = b''
        while not raw.endswith(b'\n'):
            part = c.recv(65536)
            if not part: break
            raw += part
            if len(raw) > 262144: raise Blocked('broker_response_bound')
        response = json.loads(raw)
        if response.get('ok') is not True: raise Blocked('broker_status_failed')
        if expected and response['result'].get('broker_instance_id')!=expected['broker_instance_id']:
            raise Blocked('broker_instance_changed_after_admission_operation')
        return response['result'], peer[0]


def mps_clients(root):
    pipe = root / 'mps-1/pipe'
    for p in (root, root / 'mps-1', pipe):
        st = p.lstat()
        if not stat.S_ISDIR(st.st_mode) or (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) != (1001, 1001, 0o700):
            raise Blocked('mps_directory_identity')
    env = {'PATH': '/usr/bin:/bin', 'CUDA_VISIBLE_DEVICES': '', 'CUDA_MPS_PIPE_DIRECTORY': str(pipe)}
    def query(command):
        r = subprocess.run(['/usr/bin/nvidia-cuda-mps-control'], input=command+'\n',
                           text=True, capture_output=True, env=env, timeout=5)
        parts = r.stdout.split()
        if r.returncode or len(parts)>64 or any(not p.isdigit() for p in parts):
            raise Blocked('mps_readonly_query_failed')
        return set(map(int, parts))
    servers = query('get_server_list')
    if len(servers) != 1: raise Blocked('unexpected_mps_server_count')
    return servers, query('get_client_list ' + str(next(iter(servers))))


def validate_residencies(status, clients, draining=False):
    leases = status.get('leases', [])
    if (status.get('draining') is not draining or status.get('waiters') != 0
            or status.get('quarantined_gpus') or len(leases) != 2):
        raise Blocked('primary_not_empty_of_other_activity')
    if {l.get('component') for l in leases} != {'dft', 'backend'}:
        raise Blocked('unexpected_primary_components')
    for lease in leases:
        if (lease.get('kind') != 'residency' or lease.get('environment') != 'dev'
                or lease.get('gpu_uuid') != GPU or lease.get('gpu_index') != 1
                or lease.get('status') != 'active' or not lease.get('workload_pid')
                or tick(lease['workload_pid']) != lease.get('workload_process_start_ticks')):
            raise Blocked('primary_residency_identity_or_execution_changed')
    if clients != {l['workload_pid'] for l in leases}:
        raise Blocked('unknown_mps_client_wait_for_natural_exit')
    return leases


def primary_snapshot():
    proof=original_mode_proof(live=True)
    if proof.get('status')!='PASS_ORIGINAL_NATIVE_DIRECT_MODE_READONLY_PROOF':raise Blocked('primary_controller_not_exact_ready')
    controller=proof['controller']
    public, broker_pid = broker(PRIMARY / '.runtime/gpu-resource')
    servers, clients = mps_clients(PRIMARY / '.runtime/gpu-resource')
    leases = validate_residencies(public, clients)
    backend = next(l for l in leases if l['component'] == 'backend')
    cgroup = backend['workload_cgroup']
    import re
    found = re.search(r'docker-([0-9a-f]{64})\.scope', cgroup)
    if not found: raise Blocked('backend_container_identity_missing')
    cid = found.group(1)
    template = '{{.Id}} {{.Image}} {{.State.Pid}} {{index .Config.Labels "com.docker.compose.project"}} {{index .Config.Labels "com.docker.compose.service"}}'
    identity = checked_output(['/usr/bin/docker','inspect','--format',template,cid]).split()
    if len(identity)!=5 or identity[0]!=cid or int(identity[2])!=backend['workload_pid'] or identity[3:]!=['nexpoly_dev','backend']:
        raise Blocked('original_backend_not_exact_dev_instance')
    references=[]
    for name in ('.env.dev','.env.dev.auth','scripts/dev_server_gpu.sh','scripts/dev_gpu_session.py'):
        p=PRIMARY/name; st=p.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid!=1001: raise Blocked('restore_reference_unsafe')
        references.append({'path':str(p),'device':st.st_dev,'inode':st.st_ino,'uid':st.st_uid,
                           'mode':stat.S_IMODE(st.st_mode),'size':st.st_size,
                           'mtime_ns':st.st_mtime_ns,'ctime_ns':st.st_ctime_ns})
    fields=('lease_id','component','kind','environment','gpu_index','owner_pid','owner_process_start_ticks',
            'workload_pid','workload_process_start_ticks','workload_cgroup','memory_mib')
    return {'at_utc':utc(),'controller':{k:controller[k] for k in ('pid','start_ticks','session_id','source_sha','source_tree','status_file')},
            'broker_pid':broker_pid,'broker_start_ticks':tick(broker_pid),'broker_instance_id':public['broker_instance_id'],
            'leases':[{k:l[k] for k in fields if k in l} for l in leases],
            'mps_servers':sorted(servers),'mps_clients':sorted(clients),
            'backend_container_id':cid,'backend_image_id':identity[1], 'restore_references':references,
            'original_restore_proof':proof,
            'restore_command_reference':'original scripts/dev_server_gpu.sh gpu-session-up with original direct-start selection'}


def same_authority(before,after):
    keys=('controller','leases','mps_servers','mps_clients','backend_container_id',
          'backend_image_id','broker_pid','broker_start_ticks','broker_instance_id','restore_references','original_restore_proof')
    return all(before.get(k)==after.get(k) for k in keys)


def restore_references_unchanged(before):
    for record in before['restore_references']:
        st=Path(record['path']).lstat()
        observed={'path':record['path'],'device':st.st_dev,'inode':st.st_ino,'uid':st.st_uid,
                  'mode':stat.S_IMODE(st.st_mode),'size':st.st_size,
                  'mtime_ns':st.st_mtime_ns,'ctime_ns':st.st_ctime_ns}
        if observed!=record or not stat.S_ISREG(st.st_mode):
            raise Blocked('original_configuration_reference_changed_no_restore_with_new_settings')


def budget_remaining(armed, now=None, reserve=0):
    return max(0.0, armed['monotonic']+HARD-(time.monotonic() if now is None else now)-reserve)


def cancelled():
    return (BASE/'cancel.json').exists()


def native_env(private):
    env=dict(os.environ)
    for key in tuple(env):
        if key.startswith('NEXPOLY_DEV_GPU_COHORT_'): env.pop(key)
    env.update({'NEXPOLY_DEV_GPU_SESSION_EXECUTE':'1','HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1'})
    public_raw=public_bytes(CANDIDATE_BASE/'public-runtime-values.json')
    if hashlib.sha256(public_raw).hexdigest()!=PUBLIC_RUNTIME_SHA: raise Blocked('public_runtime_projection_changed')
    public_values=json.loads(public_raw)
    if private:
        env.update(public_values)
        env.update(COHORT.environment());env['NEXPOLY_DEV_GPU_DIRECT_START']='0'
    else:
        for key in public_values: env.pop(key,None)
        env['NEXPOLY_DEV_GPU_DIRECT_START']='1'
    return env


def native(private, action, maximum, recovery=False):
    if action not in ('gpu-session-up','gpu-session-down','gpu-session-status'):
        raise Blocked('native_action_outside_batch_allowlist')
    if action=='gpu-session-up' or not private and action=='gpu-session-down':
        require_main_native_restore_preflight()
    if private and action=='gpu-session-up' or not private and action=='gpu-session-down':
        if not ROOT_NATIVE_FACTORY_AUDIT_VERIFIED:raise Blocked('native_cohort_root_factory_audit_required_before_Main_down_or_private_GPU_start')
    if cancelled() and not recovery: raise Blocked('cleanup_requested')
    armed=read('armed.json'); seconds=min(maximum,budget_remaining(armed,reserve=35))
    if seconds<2: raise Blocked('deadline_no_native_operation_budget')
    repo=ROOT if private else PRIMARY
    label=('private-' if private else 'original-')+action
    path=BASE/(label+'-'+str(time.monotonic_ns())+'.private.log')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as log:
        command=['/bin/bash',str(repo/'scripts/dev_server_gpu.sh'),action]
        if not private and action=='gpu-session-up':
            # Original dev daemons must not inherit this batch's 1 CPU/512 MiB
            # control scope. The new user scope retains its native descendants.
            command=['/usr/bin/systemd-run','--user','--scope','--quiet',
                     '--unit=nexpoly-r2-original-restore-20261016-b0044266',*command]
        process=subprocess.Popen(command,cwd=repo,
                                 env=native_env(private),stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        child_start=tick(process.pid)
        write('active-child.json',{'pid':process.pid,'start_ticks':child_start,'stage':label})
        started=time.monotonic()
        while process.poll() is None:
            if time.monotonic()-started>seconds or (cancelled() and not recovery):
                stop_launcher(process.pid,child_start)
                try: process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    # This is only the newly created native launcher process.
                    stop_launcher(process.pid,child_start,signal.SIGKILL)
                raise Blocked('native_command_timeout_or_cleanup_requested')
            time.sleep(.25)
        event('NATIVE_COMMAND_FINISHED',private=private,action=action,exit_code=process.returncode)
        if process.returncode: raise Blocked('native_command_failed_'+label)


def signal_own_command(pid,tick_expected,sig=signal.SIGTERM):
    if not tick_expected or tick(pid)!=tick_expected: return
    fd=os.pidfd_open(pid)
    try:
        if tick(pid)==tick_expected: signal.pidfd_send_signal(fd,sig)
    finally: os.close(fd)


def stop_launcher(pid, started, sig=signal.SIGTERM):
    """A launcher has a newly allocated session/group; never use this for faults."""
    if started and tick(pid)==started:
        try:
            if os.getpgid(pid)==pid: os.killpg(pid,sig)
        except ProcessLookupError:
            pass


def private_state():
    p=COHORT.runtime_root/'gpu-session/controller.json'
    if not p.exists(): return {'status':'stopped'}
    controller=json.loads(p.read_text());status=Path(controller['status_file'])
    status.relative_to(COHORT.runtime_root/'gpu-session/runs')
    if (controller.get('source_sha')!=COMMIT or controller.get('cohort_sha256')!=CONFIG_SHA
            or tick(controller['pid'])!=controller['start_ticks']):
        raise Blocked('private_controller_binding_changed')
    value=json.loads(status.read_text())
    out={k:value[k] for k in ('status','session_id','controller_pid','controller_start_ticks','cpu_restored','owned_components_stopped','contaminated') if k in value}
    out['status_file']=str(status)
    return out


def claims_public(snapshot):
    return {'cuda_pids':list(snapshot.process_pids),
            'docker_claims':[{'container_id':c.container_id,'compose_project':c.compose_project,'compose_service':c.compose_service} for c in snapshot.docker_claims],
            'systemd_claims':[{'scope':c.scope,'unit':c.unit,'main_pid':c.main_pid,'process_pids':sorted(c.process_pids)} for c in snapshot.systemd_claims]}


def free_audit():
    from scripts import dev_gpu_session as s
    s._select_cohort(CONFIG,CONFIG_SHA,identity=CONFIG_IDENTITY)
    first=s.collect_target_snapshot();time.sleep(1);second=s.collect_target_snapshot()
    write('free-audit.json',{'at_utc':utc(),'first':claims_public(first),'second':claims_public(second)})
    rounds=iter((first,second))
    s.require_double_free_audit(lambda:next(rounds),pause=lambda _:None)
    event('NATIVE_DOUBLE_FREE_AUDIT_PASSED')


def audit_main_with_native_cohort_factory(before):
    global ROOT_NATIVE_FACTORY_AUDIT_VERIFIED
    ROOT_NATIVE_FACTORY_AUDIT_VERIFIED=False
    from scripts import dev_gpu_session as session
    session._select_cohort(CONFIG,CONFIG_SHA,identity=CONFIG_IDENTITY)
    if not COHORT.systemd_configuration_options().get('system_configuration_factory'):
        raise Blocked('verified_cohort_did_not_select_native_Root_factory')
    snapshot=session.collect_target_snapshot()
    known=set(before['mps_servers'])|set(before['mps_clients'])|{before['broker_pid'],before['controller']['pid']}
    worker=before['original_restore_proof']['md_process'];known.add(worker['pid'])
    if set(snapshot.process_pids)-known:raise Blocked('foreign_GPU1_process_before_Main_pause')
    if any(c.container_id!=before['backend_container_id'] or c.compose_project!='nexpoly_dev'
           or c.compose_service!='backend' for c in snapshot.docker_claims):
        raise Blocked('foreign_GPU1_Docker_claim_before_Main_pause')
    md_cgroup=worker['cgroup'].removeprefix('0::')
    for claim in snapshot.systemd_claims:
        pids=set(claim.process_pids)
        # An exact original native restore scope can contain its CPU supervisor
        # siblings. This is only a pre-pause ownership audit; the post-pause
        # native double-free audit still requires every claim to disappear.
        native_scope=(md_cgroup==ORIGINAL_MD_CGROUP and claim.scope=='user'
                      and claim.unit==Path(ORIGINAL_MD_CGROUP).name
                      and claim.control_group==md_cgroup and worker['pid'] in pids)
        if not pids or not (pids<=known or native_scope):
            raise Blocked('foreign_GPU1_systemd_claim_before_Main_pause')
    if not same_authority(before,primary_snapshot()):raise Blocked('original_authority_changed_during_native_Root_factory_audit')
    write('root-factory-before-Main-pause.json',{'at_utc':utc(),'status':'PASS_NATIVE_COHORT_FACTORY_OWNERSHIP_AUDIT_ONLY',
          'source_sha':COMMIT,'source_tree':TREE,'cohort_sha256':CONFIG_SHA,
          'Main_session_id':before['controller']['session_id'],**claims_public(snapshot),
          'not_a_free_GPU_or_lease_admission':True})
    ROOT_NATIVE_FACTORY_AUDIT_VERIFIED=True


def fault(case):
    state=private_state()
    if state.get('status')!='ready': raise Blocked('private_not_ready_for_owned_fault')
    session=state['session_id']
    if case=='backend-exit':
        cid=COHORT.backend_container_id(session)
        if not cid: raise Blocked('private_backend_binding_missing')
        info=checked_output(['/usr/bin/docker','inspect','--format','{{.Id}} {{index .Config.Labels "com.docker.compose.project"}} {{.State.Running}}',cid]).split()
        if info!=[cid,COHORT.compose_project,'true']: raise Blocked('private_backend_identity_changed')
        subprocess.run(['/usr/bin/docker','kill','--signal','KILL',cid],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
        return
    if case=='broker-exit':
        _,pid=broker(COHORT.path_for('gpu-resource'))
        started=tick(pid)
        parent=[line.split()[1] for line in (Path('/proc')/str(pid)/'status').read_text().splitlines() if line.startswith('PPid:')]
        if parent!=[str(state['controller_pid'])]:raise Blocked('private_broker_not_child_of_exact_controller')
    else:
        p=COHORT.path_for('md-runs/worker.pid' if case=='md-worker-exit' else 'dft-process.json')
        if case=='md-worker-exit':
            from scripts.dev_worker_process import verify_record
            lock=ROOT/'workers/monomer_md_worker/requirements.lock'
            metadata=json.loads(p.read_text())
            lock_digest=hashlib.sha256(lock.read_bytes()).hexdigest()
            lock_value=metadata.get('worker_lock_sha256')
            if lock_value not in (lock_digest,'sha256:'+lock_digest):
                raise Blocked('md_worker_public_lock_identity_changed')
            value=verify_record(p,python=ROOT/'.venv-monomer-md-worker/bin/python',
                                socket=COHORT.path_for('md-socket/worker.sock'),source_sha=COMMIT,source_tree=TREE,
                                worker_lock_sha256=lock_value,
                                session_id=session,require_instance=True)
        else:
            value=json.loads(p.read_text())
            if value.get('cohort_sha256')!=CONFIG_SHA or value.get('session_id')!=session:
                raise Blocked('worker_record_not_bound_to_this_private_session')
        pid=value.get('pid',value.get('supervisor_pid'))
        started=value.get('start_ticks',value.get('process_start_ticks',value.get('supervisor_start_ticks')))
        if not isinstance(pid,int) or not isinstance(started,int): raise Blocked('worker_record_identity_not_supported')
        cgroup=(Path('/proc')/str(pid)/'cgroup').read_text()
        cwd=os.readlink(Path('/proc')/str(pid)/'cwd')
        if str(ROOT) not in cwd and COHORT.cohort_id not in cgroup: raise Blocked('worker_not_proven_private')
    if tick(pid)!=started: raise Blocked('owned_fault_pid_reused')
    uid=[int(v) for line in (Path('/proc')/str(pid)/'status').read_text().splitlines() if line.startswith('Uid:') for v in line.split()[1:]]
    if uid!=[1001]*4: raise Blocked('owned_fault_uid_changed')
    signal_own_command(pid,started,signal.SIGKILL)


def observe_recovery(case,status_file):
    path=Path(status_file);path.relative_to(COHORT.runtime_root/'gpu-session/runs')
    end=time.monotonic()+110
    while time.monotonic()<end and not cancelled():
        value=json.loads(path.read_text())
        state={k:value[k] for k in ('status','session_id','cpu_restored','owned_components_stopped') if k in value}
        if state.get('status')=='recovered' and state.get('cpu_restored') is True and state.get('owned_components_stopped') is True:
            if not (COHORT.path_for('gpu-resource/broker.sock')).exists() and not COHORT.path_for('gpu-resource/mps-1').exists():
                native(True,'gpu-session-status',30)
                event('ISOLATED_RECOVERY_OBSERVED',case=case,state=state)
                return state
        time.sleep(1)
    raise Blocked('automatic_recovery_not_proven_'+case)


def verify_restored_private_cpu():
    # Native down/recovery legitimately recreates CPU: use the exact native
    # owned successor pointer, never the original pre-window fixed CID verifier.
    from scripts.dev_gpu_cohort import cpu_snapshot,verify_cpu_backend
    frozen=cpu_snapshot(COHORT)
    if any(frozen.get(k)!=v for k,v in (('image_id',IMAGE_ID),('dependency_lock',DEPENDENCY_LOCK),('build_config',BUILD_CONFIG))):
        raise Blocked('restored_private_CPU_frozen_identity_changed')
    cid=verify_cpu_backend(COHORT)
    if not isinstance(cid,str) or len(cid)!=64 or any(c not in '0123456789abcdef' for c in cid):
        raise Blocked('restored_private_CPU_native_pointer_invalid')
    write('restored-private-cpu.json',{'at_utc':utc(),'container_id':cid,'source_sha':COMMIT,
          'source_tree':TREE,'cohort_sha256':CONFIG_SHA,'image_id':IMAGE_ID,
          'binding':'native_owned_successor_verified_against_frozen_CPU_source_config_and_full_identity',
          'original_pre_window_CID_required':False,'GPU_or_scientific_payload_started':False})
    return cid

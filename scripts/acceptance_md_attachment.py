"""Finite independent MD attachment contracts and native full audit.

Import/default invocation performs no GPU, service, database or credential I/O.
The execution plan must be sealed with fresh action-time identities before use.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from functools import partial
import hashlib
import json
import math
from pathlib import Path
import re

from scripts import dev_gpu_session as native

SCOPE = 'acceptance-a-20261019'
WORKER = 'r2-a-20261019-md'
ROOT = Path('/data/lzq/gith/nexpoly-acceptance-a-20261019')
RUNTIME = ROOT/'.runtime/acceptance-a-20261019/md-independent'
MAIN_ROOT = Path('/data/lzq/gith/nexpoly-dev')
MAIN_SOURCE = 'a2ef949e68826bd542eda9bec823df942bab9689'
MAIN_TREE = '875d4d44bd53a508badcf653089c0ef55c68f673'
MAIN_GPU_ROOT = MAIN_ROOT/'.runtime/gpu-resource'
GLOBAL_DEADLINE = datetime(2026,10,20,18,53,34,tzinfo=timezone.utc)
TRANSPORT_TIMEOUT = 79200
TRANSPORT_ADMISSION_REMAINING = 83100
MAX_RECEIPT_AGE = 900


class AttachmentError(RuntimeError):
    pass


def need(value, code):
    if not value:
        raise AttachmentError(code)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def request_id(job_id):
    need(type(job_id) is str and re.fullmatch('[0-9a-f]{32}',job_id), 'invalid_owned_job_identity')
    return 'md:dev:'+sha(job_id.encode())[:16]


def require_transport_budget(*, now):
    need(now.tzinfo is not None and (GLOBAL_DEADLINE-now).total_seconds() >= TRANSPORT_ADMISSION_REMAINING,
         'transport_setup_execution_cleanup_budget_insufficient')


def verify_main_restore(receipt, *, now):
    required = {'at_utc','source_sha','source_tree','direct_start','controller_status',
                'contaminated','full_native_factory','controller_pid','controller_start_ticks',
                'session_id','broker_instance_id'}
    need(type(receipt) is dict and set(receipt) == required, 'invalid_main_restore_receipt')
    observed = datetime.fromisoformat(receipt['at_utc'].replace('Z','+00:00'))
    need(observed.tzinfo is not None and 0 <= (now-observed).total_seconds() <= MAX_RECEIPT_AGE,
         'main_restore_receipt_stale')
    need(receipt['source_sha'] == MAIN_SOURCE and receipt['source_tree'] == MAIN_TREE
         and receipt['direct_start'] is True and receipt['controller_status'] == 'ready'
         and receipt['contaminated'] is False and receipt['full_native_factory'] is True,
         'main_native_direct_restore_not_proven')
    for name in ['controller_pid','controller_start_ticks']:
        need(type(receipt[name]) is int and receipt[name] > 0, 'invalid_main_process_binding')
    for name in ['session_id','broker_instance_id']:
        need(type(receipt[name]) is str and re.fullmatch('[0-9a-f]{32}',receipt[name]),
             'invalid_main_epoch_binding')


def native_factory(source_sha, source_tree):
    from gpu_resource.systemd_inventory import configuration_client
    need(all(type(v) is str and re.fullmatch('[0-9a-f]{40}',v) for v in (source_sha,source_tree)),
         'invalid_attachment_source')
    return partial(configuration_client,expected_source_sha=source_sha,expected_source_tree=source_tree)


def classify(status,snapshot,authority):
    """Reuse native Docker, host-scope, PID and MPS client attribution."""
    need(authority.descriptor_authority is True and len(authority.server_pids) == 1,
         'main_mps_descriptor_authority_unproven')
    clients = frozenset(client.client_pid for client in authority.clients)
    nvml, managed_clients, managed_claims = native.SessionController._managed_workload_authority(
        status,snapshot,authorized_mps_declarers=authority.gpu_declarers,
        authorized_mps_client_pids=clients,authorized_mps_server_pids=authority.server_pids)
    expected = authority.gpu_declarers | frozenset(
        declarer for claim in snapshot.systemd_claims
        if (claim.scope,claim.unit,claim.control_group) in managed_claims
        for declarer in claim.live_gpu_declarers)
    mismatch = native.SessionController._captured_pid_identity_mismatches(
        authority.server_pids|nvml,frozenset(snapshot.process_pids),snapshot.process_declarers,expected)
    reasons = native.foreign_gpu1_reasons(snapshot,authorized_mps_pids=authority.server_pids,
        managed_workload_pids=nvml,managed_systemd_claims=managed_claims)
    need(not mismatch and not reasons and not clients-managed_clients,
         'native_full_factory_contamination_or_unknown_client')


class FullAudit:
    """Same explicit Root factory for initial and trailing inventories.

    Broker and descriptor-bound MPS identities are checked around both complete
    GPU1-only native collections. No global drain, admission or release RPC is
    available through this object. Ordinary lifecycle churn fails this round;
    the caller may retry a bounded round without submitting another job.
    """
    def __init__(self,*,source_sha,source_tree,broker_instance_id,collector=None,mps_reader,
                 classifier=classify):
        need(type(broker_instance_id) is str and re.fullmatch('[0-9a-f]{32}',broker_instance_id),
             'invalid_main_broker_epoch')
        self.factory = native_factory(source_sha,source_tree)
        self.collector = collector or native.collect_target_snapshot
        self.mps_reader = mps_reader
        self.classifier = classifier
        self.instance = broker_instance_id
        self.sequence = 0

    def capture(self,client):
        before = client.status()
        need(before.get('broker_instance_id') == self.instance and before.get('draining') is False,
             'main_broker_epoch_changed_or_draining')
        token = native.broker_authority_token(before)
        first_mps = self.mps_reader()
        first = self.collector(system_configuration_factory=self.factory)
        self.classifier(before,first,first_mps)
        middle = client.status()
        need(native.broker_authority_token(middle) == token,'broker_changed_during_initial_audit')
        second = self.collector(system_configuration_factory=self.factory)
        second_mps = self.mps_reader()
        after = client.status()
        need(native.broker_authority_token(after) == token,'broker_changed_during_trailing_audit')
        need(first_mps == second_mps and native.snapshot_fingerprint(first) == native.snapshot_fingerprint(second),
             'native_inventory_changed_during_full_audit')
        self.classifier(after,second,second_mps)
        self.sequence += 1
        return {'schema_version':1,'scope_id':SCOPE,'audit_sequence':self.sequence,
                'broker_instance_id':self.instance,'gpu_index':1,'gpu_uuid':native.GPU_UUID,
                'native_full_factory':True,'initial_trailing_same_factory':True,
                'gpu1_processes':len(second.process_pids),'gpu1_docker_claims':len(second.docker_claims),
                'gpu1_systemd_claims':len(second.systemd_claims),'lease_admission_sent':False,
                'global_drain_or_release_sent':False}


def worker_environment(public, *, private_service_dsn):
    """Private DSN remains in memory and is never included in a public plan."""
    from psycopg.conninfo import conninfo_to_dict
    try:
        valid = conninfo_to_dict(private_service_dsn).get('dbname') == 'nexpoly_accept_r2_1c55866ff0e4'
    except Exception:
        valid = False
    need(valid,'exact_acceptance_service_database_required')
    expected = {'source_sha','source_tree','worker_lock_sha256','byteff2_root','openmm_dir'}
    need(type(public) is dict and set(public) == expected,'invalid_public_worker_inputs')
    need(all(re.fullmatch('[0-9a-f]{40}',public[k]) for k in ['source_sha','source_tree']),
         'invalid_attachment_source')
    need(re.fullmatch('sha256:[0-9a-f]{64}',public['worker_lock_sha256']), 'invalid_worker_lock')
    for key in ['byteff2_root','openmm_dir']:
        path=Path(public[key])
        need(path.is_absolute() and '..' not in path.parts,'invalid_frozen_scientific_asset_path')
    python=str(ROOT/'.venv-monomer-md-worker/bin/python')
    return {'PATH':str(Path(python).parent)+':/usr/bin:/bin','HOME':str(RUNTIME/'home'),
        'PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1',
        'PYTHONPATH':str(ROOT)+':'+public['byteff2_root']+':'+public['byteff2_root']+'/submodules/bytemol',
        'APP_SERVICE_POSTGRES_DSN':private_service_dsn,'USAGE_METERING_ENABLED':'true',
        'USAGE_ENTITLEMENTS_ENABLED':'true','BYTEFF2_ROOT':public['byteff2_root'],
        'BYTEFF2_PYTHON':python,'BYTEFF2_OPENMM_DIR':public['openmm_dir'],
        'MONOMER_MD_PYTHON':python,'MONOMER_MD_WORKER_MODE':'real',
        'MONOMER_MD_WORKER_ID':WORKER,'MONOMER_MD_ACCEPTANCE_ATTACHMENT_ID':WORKER,
        'MONOMER_MD_WORKER_VERSION':'dev:'+public['source_sha']+':'+public['source_tree']+':'+public['worker_lock_sha256'],
        'MONOMER_MD_WORKER_UDS':str(RUNTIME/'socket/worker.sock'),
        'MONOMER_MD_JOB_ROOT':str(RUNTIME/'jobs'),'MONOMER_MD_CUDA_VISIBLE_DEVICES':'1','NEXPOLY_GPU_DEVICE':'1',
        'MONOMER_MD_DEFAULT_STEPS':'300','MONOMER_MD_MAX_STEPS':'300','MONOMER_MD_REPORT_INTERVAL':'10',
        'MONOMER_MD_MAX_ACTIVE_JOBS':'3','MONOMER_MD_MAX_CONCURRENT_JOBS':'1',
        'MONOMER_MD_FORMAL_TIMEOUT_SECONDS':str(TRANSPORT_TIMEOUT),'MONOMER_MD_TIMEOUT_SECONDS':'3600',
        'MONOMER_MD_GPU_BROKER_ENABLED':'true','MONOMER_MD_GPU_BROKER_ENVIRONMENT':'dev',
        'MONOMER_MD_GPU_BROKER_SOCKET_PATH':str(MAIN_GPU_ROOT/'broker.sock'),
        'MONOMER_MD_GPU_MPS_PIPE_ROOT':str(MAIN_GPU_ROOT),'MONOMER_MD_GPU_SCOPE_LAUNCHER':'systemd-user-scope',
        'MONOMER_MD_GPU_SCOPE_CPU_QUOTA_PERCENT':'400','MONOMER_MD_GPU_SCOPE_MEMORY_MAX_BYTES':str(8*1024**3),
        'MONOMER_MD_GPU_SCOPE_MEMORY_SWAP_MAX_BYTES':'0','MONOMER_MD_GPU_SCOPE_TASKS_MAX':'256'}


def selective_cleanup(*,worker_binding,owned_jobs,io):
    """Only native worker cancel and exact pidfd termination; no Broker writes."""
    need(type(owned_jobs) is tuple and len(owned_jobs)<=64 and len(set(owned_jobs))==len(owned_jobs),
         'invalid_finite_owned_jobs')
    requests=frozenset(request_id(job) for job in owned_jobs)
    io.verify_worker(worker_binding)
    for job in owned_jobs:
        io.cancel_owned(job,worker_binding)
    io.terminate_worker_pidfd(worker_binding)
    state=io.wait_owned_resources(requests,timeout_seconds=180)
    need(state == 'ABSENT_EXACT_OWNED_RESOURCES','owned_resources_cleanup_unconfirmed')
    return {'scope_id':SCOPE,'worker_id':WORKER,'status':'EXACT_OWNED_MD_RESOURCES_ABSENT',
            'Main_Broker_MPS_stopped':False,'foreign_jobs_cancelled':False}


def plan():
    return {'schema_version':1,'scope_id':SCOPE,'worker_id':WORKER,'dry_run':True,
        'source_root':str(ROOT),'runtime':str(RUNTIME),'worker_unit':'nexpoly-r2-a-20261019-md.service',
        'audit_unit':'nexpoly-r2-a-20261019-md-audit.service','Main_source_sha':MAIN_SOURCE,
        'Main_source_tree':MAIN_TREE,'Main_pause_soft_seconds':3300,'Main_pause_hard_seconds':3600,
        'Main_Broker_MPS_reused':True,'old_private_cohort_keepalive':False,
        'transport_timeout_seconds':TRANSPORT_TIMEOUT,'minimum_transport_remaining_seconds':TRANSPORT_ADMISSION_REMAINING,
        'global_deadline_utc':'2026-10-20T18:53:34Z',
        'native_worker_argv':[str(ROOT/'.venv-monomer-md-worker/bin/python'),'-m','uvicorn','app.main:app','--uds',str(RUNTIME/'socket/worker.sock')],
        'preconditions':['fresh Main native-direct restore from original source and full Root factory',
            'action-time approved installed Root guard and finite source manifest',
            'pinned Main Broker peer PID/start/socket inode and broker instance',
            'descriptor-bound Main GPU1 MPS root and native full audit before submission',
            'native verified isolated venv/lock and immutable ByteFF2/OpenMM asset identities',
            'fresh exact-owner A/B sessions; DSN consumed opaquely by native runtime only',
            'aggregate phase resource and storage proof; no orphan prior resources',
            'durable one-submit intent before Transport; no timeout or ambiguous retry'],
        'cleanup':'native cancel exact worker jobs; pidfd terminate exact worker; prove only owned scopes/leases absent',
        'controller_source':'scripts/acceptance_md_control.py',
        'runtime_proofs_not_yet_obtained':['native worker launch and continuous watcher failure/timeout delivery',
            'action-time live peer/descriptors and Main restore binding'],
        'execution_ready':False}


if __name__=='__main__':
    print(json.dumps(plan(),sort_keys=True))

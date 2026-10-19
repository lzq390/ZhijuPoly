"""Action-time native CPU/database read-only gate; default describes the plan.

No private environment file is opened. The existing container consumes its own
environment internally. No account/login/password or data writes are performed.
"""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.acceptance_md_attachment import SCOPE,ROOT as FIXED_ROOT
from scripts.dev_gpu_cohort import DevGpuCohort,verify_cpu_backend,cpu_snapshot

RUNTIME=ROOT/'.runtime/acceptance-a-20261019'
COHORT=ROOT/'.runtime/cohorts/r2-a-20261019/cohort.json'

NATIVE_READONLY = r'''
import sys,json
sys.path[:0]=['/app/backend','/app']
try:
 import psycopg
 from psycopg.conninfo import conninfo_to_dict
 original=psycopg.connect
 def readonly(*args,**kwargs):
  dsn=args[0] if args else kwargs.get('conninfo','')
  options=conninfo_to_dict(dsn)
  if (options.get('dbname')!='nexpoly_accept_r2_1c55866ff0e4'
      or kwargs.get('dbname','nexpoly_accept_r2_1c55866ff0e4')!='nexpoly_accept_r2_1c55866ff0e4'
      or options.get('service') or kwargs.get('service')):
   raise RuntimeError('fixed database required before any connection')
  kwargs['options']='-c default_transaction_read_only=on'
  connection=original(*args,**kwargs)
  try:
   with connection.cursor() as cursor:
    cursor.execute("SELECT current_database(), current_setting('transaction_read_only')")
    row=cursor.fetchone()
    values=list(row.values()) if isinstance(row,dict) else list(row)
    if values!=['nexpoly_accept_r2_1c55866ff0e4','on']:raise RuntimeError('fixed database or read-only identity differs')
   return connection
  except Exception:
   connection.close();raise
 psycopg.connect=readonly
 from app.config import Settings
 from app.auth.settings import AuthSettings
 from app.auth.service import AuthService
 from types import SimpleNamespace
 settings=Settings();auth=AuthSettings.from_settings(settings)
 if settings.gpu_broker_enabled or settings.monomer_md_submit_enabled or settings.monomer_dft_submit_enabled:
  raise RuntimeError('CPU profile differs')
 AuthService.assert_application_ready(SimpleNamespace(settings=auth))
 policy=auth.isolated_acceptance_policy
 if policy is None or not policy.allows('b57ded4a-6ee7-4200-acb3-e7f86fbe1016','dev_gpu.status.read'):raise RuntimeError('finite acceptance scope absent or expired')
 print(json.dumps({'status':'PASS_NATIVE_CPU_READONLY_GATE','database':'nexpoly_accept_r2_1c55866ff0e4',
  'all_connections_read_only':True,'native_role_ledger_schema_privilege_preflight':True,
  'account_or_business_write':False,'credential_values_exported':False,
  'features':sorted(policy.features),'expires_at':policy.expires_at.isoformat()}))
except Exception as error:
 print(json.dumps({'status':'BLOCKED_NATIVE_CPU_READONLY_GATE','error_type':type(error).__name__,
  'credential_or_raw_error_exported':False}));raise SystemExit(2)
'''


def gate(cohort_sha,expected_image):
    if ROOT!=FIXED_ROOT or (os.geteuid(),os.getegid())!=(1001,1001):raise RuntimeError('fixed UID/source required')
    if not re.fullmatch('[0-9a-f]{64}',cohort_sha) or not re.fullmatch('sha256:[0-9a-f]{64}',expected_image):
        raise RuntimeError('explicit public pins required')
    cohort=DevGpuCohort.load(COHORT,cohort_sha,repo_root=ROOT)
    cohort.require_prepared()
    frozen=cpu_snapshot(cohort)
    if frozen['image_id']!=expected_image:raise RuntimeError('frozen CPU image differs')
    cid=verify_cpu_backend(cohort)
    command=['docker','container','inspect','--format',
        '{{.Image}}|{{.State.Pid}}|{{.State.StartedAt}}|{{.RestartCount}}|{{json .HostConfig.DeviceRequests}}',cid]
    def identity():
        out=subprocess.check_output(command,stderr=subprocess.DEVNULL,timeout=10,text=True).strip().split('|')
        if len(out)!=5 or out[0]!=expected_image or not out[1].isdecimal() or int(out[1])<=0 or json.loads(out[4]):
            raise RuntimeError('exact CPU container required')
        return out
    before=identity()
    q=subprocess.run(['docker','exec','-i',cid,'python','-I','-B','-c',NATIVE_READONLY],
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=45)
    if q.returncode or len(q.stdout)>8192:raise RuntimeError('native read-only preflight failed')
    result=json.loads(q.stdout)
    if result.get('status')!='PASS_NATIVE_CPU_READONLY_GATE' or identity()!=before or verify_cpu_backend(cohort)!=cid:
        raise RuntimeError('CPU epoch changed during preflight')
    result.update(schema_version=1,scope_id=SCOPE,at_utc=datetime.now(timezone.utc).isoformat(),
        source_sha=cohort.source_sha,source_tree=cohort.source_tree,cohort_sha256=cohort.sha256,
        cohort_device=cohort.device,cohort_inode=cohort.inode,container_id=cid,image_id=expected_image,
        container_pid=int(before[1]),container_started_at=before[2],restart_count=int(before[3]),
        dependency_lock=frozen['dependency_lock'],build_config=frozen['build_config'])
    from scripts.dev_gpu_session import _atomic_json
    _atomic_json(RUNTIME/'cpu-readonly-gate.public.json',result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--execute-readonly',action='store_true')
    p.add_argument('--cohort-sha256');p.add_argument('--expected-image-id')
    args=p.parse_args()
    if not args.execute_readonly:
        print(json.dumps({'scope_id':SCOPE,'dry_run':True,'native_readonly_gate_source':__file__,
            'no_private_files_read':True,'no_database_or_container_exec_performed':True}));return
    try:print(json.dumps(gate(args.cohort_sha256,args.expected_image_id),sort_keys=True))
    except Exception:p.exit(2,'CPU/database readiness unconfirmed; no credentials or raw diagnostics exported.\n')


if __name__=='__main__':main()

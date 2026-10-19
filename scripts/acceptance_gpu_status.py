"""Publish only the native status projection; no operator/recover socket exists."""
from datetime import datetime,timezone
import argparse
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.acceptance_md_attachment import SCOPE,ROOT as FIXED_ROOT,GLOBAL_DEADLINE
from scripts.dev_gpu_cohort import DevGpuCohort
from scripts.dev_gpu_operator import DevGpuOperator
from scripts import dev_gpu_session as native

CONFIG=ROOT/'.runtime/cohorts/r2-a-20261019/cohort.json'


def project(controller,*,source_sha,source_tree):
    # Reuse the existing native response projection without instantiating the
    # operator, reading an operation ledger, or exposing a recovery method.
    obj=SimpleNamespace(source_sha=source_sha,source_tree=source_tree,_operation=None,
                        _operation_active_locked=lambda:False)
    return DevGpuOperator._public_status_locked(obj,controller)


def serve(cohort_sha):
    if ROOT!=FIXED_ROOT or (os.geteuid(),os.getegid())!=(1001,1001) or not re.fullmatch('[0-9a-f]{64}',cohort_sha):
        raise RuntimeError('Fixed scope and owner required')
    cohort=DevGpuCohort.load(CONFIG,cohort_sha,repo_root=ROOT);cohort.require_prepared()
    native._select_cohort(CONFIG,cohort_sha,identity=(cohort.device,cohort.inode))
    target=cohort.path_for('acceptance-status')
    st=target.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid!=1001 or st.st_mode&0o022:raise RuntimeError('unsafe public status directory')
    stop=False
    def requested(*_):
        nonlocal stop
        stop=True
    signal.signal(signal.SIGTERM,requested);signal.signal(signal.SIGINT,requested)
    deadline=time.monotonic()+max(0,(GLOBAL_DEADLINE-datetime.now(timezone.utc)).total_seconds())
    while not stop and time.monotonic()<deadline and datetime.now(timezone.utc)<GLOBAL_DEADLINE:
        cohort.verify()
        controller=native.status()
        value=project(controller,source_sha=cohort.source_sha,source_tree=cohort.source_tree)
        envelope=dict(schema_version=1,scope_id=SCOPE,source_sha=cohort.source_sha,source_tree=cohort.source_tree,
            cohort_sha256=cohort_sha,observed_at=datetime.now(timezone.utc).isoformat(),
            expires_at='2026-10-20T18:53:34Z',native_status=value)
        # The directory is a new public-only read-only container mount.
        fd,temp=tempfile.mkstemp(prefix='.status-',dir=target)
        try:
            with os.fdopen(fd,'wb') as output:
                os.fchmod(output.fileno(),0o644)
                output.write((json.dumps(envelope,sort_keys=True)+'\n').encode());output.flush();os.fsync(output.fileno())
            os.replace(temp,target/'status.public.json')
        finally:
            try:os.unlink(temp)
            except FileNotFoundError:pass
        time.sleep(2)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--serve',action='store_true');p.add_argument('--cohort-sha256')
    args=p.parse_args()
    if not args.serve:
        print(json.dumps({'scope_id':SCOPE,'dry_run':True,'native_recovery_surface':False,'service_started':False}));return
    try:serve(args.cohort_sha256 or '')
    except Exception:p.exit(2,'Read-only native status publisher stopped; raw diagnostics withheld.\n')


if __name__=='__main__':main()

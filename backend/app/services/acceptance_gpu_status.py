"""Read the fixed, read-only native status projection for this finite scope."""
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import re
import stat

from app.auth.acceptance import SCOPE_ID,DEADLINE
from app.services.dev_gpu_operator import DevGpuOperatorError

STATUS_PATH=Path('/app/.runtime/acceptance-status/status.public.json')


class AcceptanceGpuStatusClient:
    def __init__(self,*,expected_source_sha,expected_source_tree):
        if any(not isinstance(v,str) or re.fullmatch('[0-9a-f]{40}',v) is None
               for v in (expected_source_sha,expected_source_tree)):
            raise ValueError('Exact acceptance source identity is required')
        self.source=(expected_source_sha,expected_source_tree)

    def request(self,command):
        if command!='status':
            raise DevGpuOperatorError('The acceptance status source cannot execute recovery')
        try:
            fd=os.open(STATUS_PATH,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            try:
                before=os.fstat(fd)
                if (not stat.S_ISREG(before.st_mode) or before.st_uid!=1001 or before.st_nlink!=1
                        or before.st_mode&0o022 or before.st_size>16384):raise ValueError
                raw=os.read(fd,16385);after=os.fstat(fd)
                identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
                if identity(before)!=identity(after) or len(raw)!=before.st_size:raise ValueError
            finally:os.close(fd)
            def strict(pairs):
                value={}
                for key,item in pairs:
                    if key in value:raise ValueError
                    value[key]=item
                return value
            value=json.loads(raw,object_pairs_hook=strict)
            if (type(value) is not dict or set(value)!={'schema_version','scope_id','source_sha','source_tree',
                'cohort_sha256','observed_at','expires_at','native_status'} or value['schema_version']!=1
                or type(value['schema_version']) is not int or not isinstance(value['observed_at'],str)
                or value['scope_id']!=SCOPE_ID or (value['source_sha'],value['source_tree'])!=self.source
                or not isinstance(value['cohort_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',value['cohort_sha256'])
                or value['expires_at']!='2026-10-20T18:53:34Z'):raise ValueError
            observed=datetime.fromisoformat(value['observed_at'].replace('Z','+00:00'))
            now=datetime.now(timezone.utc)
            if observed.tzinfo is None or not 0<=(now-observed).total_seconds()<=15 or now>=DEADLINE:raise ValueError
            # Reuse the native response model; do not rewrite can_recover or
            # pretend a stopped/failed controller is ready.
            from app.routers.dev_gpu_session import DevGpuSessionStatusResponse
            result=DevGpuSessionStatusResponse.model_validate(value['native_status']).model_dump()
            if (result['source_sha'],result['source_tree'])!=self.source:raise ValueError
            return result
        except (OSError,TypeError,ValueError,KeyError):
            raise DevGpuOperatorError('Finite native GPU status is absent, stale or changed') from None

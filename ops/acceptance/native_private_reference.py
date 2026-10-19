"""Metadata-only reference to one already provisioned native acceptance file.

No credential byte is opened for reading, copied, hashed, printed or exported.
Only the native application/Compose/systemd consumer may load its values.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

WORKTREE=Path('/data/lzq/gith/nexpoly-acceptance-a-20261019')
COHORT_ID='r2-a-20261019'
PRIVATE_PATH=Path('/data/lzq/gith/nexpoly-gpu-interface-r2-20261014/.runtime/cohorts/r2-science-20261016-ab36-fix1/backend.auth.env')
FIELDS=('device','inode','uid','gid','mode','nlink','size','mtime_ns','ctime_ns')


class ReferenceError(RuntimeError):
    pass


def need(value,code):
    if not value:raise ReferenceError(code)


def snapshot():
    """O_PATH permits metadata inspection and cannot read credential contents."""
    path=PRIVATE_PATH
    need(path.resolve(strict=True)==path,'native_private_reference_symlink')
    fd=os.open(path,os.O_PATH|os.O_CLOEXEC|os.O_NOFOLLOW)
    try:
        before=os.fstat(fd)
        linked=path.lstat()
        def record(item):
            return dict(device=item.st_dev,inode=item.st_ino,uid=item.st_uid,gid=item.st_gid,
                mode=stat.S_IMODE(item.st_mode),nlink=item.st_nlink,size=item.st_size,
                mtime_ns=item.st_mtime_ns,ctime_ns=item.st_ctime_ns)
        need(stat.S_ISREG(before.st_mode) and before.st_uid==before.st_gid==1001
             and before.st_nlink==1 and stat.S_IMODE(before.st_mode)==0o600
             and 0<before.st_size<=65536,'unsafe_native_private_reference')
        value=record(before)
        need(value==record(linked),'native_private_reference_changed')
        return dict(path=str(path),**value)
    finally:os.close(fd)


def verify(value):
    need(type(value) is dict and set(value)=={'path',*FIELDS}
         and value.get('path')==str(PRIVATE_PATH)
         and all(type(value.get(key)) is int for key in FIELDS),'invalid_native_private_reference')
    need(value==snapshot(),'native_private_reference_changed')
    return PRIVATE_PATH

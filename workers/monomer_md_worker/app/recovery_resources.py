"""Kernel-backed absence proof for an explicitly governed, non-Broker worker.

Broker-free children stay in the worker's own service cgroup.  Recovery may
proceed only while that exact, non-delegated cgroup contains the worker alone.
This module neither signals processes nor writes to systemd or cgroupfs.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from typing import Mapping

_PROC = Path('/proc')
_CGROUP = Path('/sys/fs/cgroup')
_UNIT = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.@:-]{0,199}\.service\Z')


class RecoveryResourceError(RuntimeError):
    """A required execution-resource identity could not be proven."""


def _require(value: object, code: str) -> None:
    if not value:
        raise RecoveryResourceError(code)


def validate_unit_name(unit: str) -> str:
    _require(type(unit) is str and _UNIT.fullmatch(unit), 'MD_RECOVERY_UNIT_INVALID')
    return unit


def _start_ticks(pid: int) -> int:
    payload = (_PROC / str(pid) / 'stat').read_text()
    leader, separator, tail = payload.rpartition(')')
    _require(separator and leader.startswith(str(pid) + ' ('), 'MD_RECOVERY_PROCESS_STAT_INVALID')
    fields = tail.split()
    _require(len(fields) >= 20 and fields[19].isdecimal(), 'MD_RECOVERY_PROCESS_START_INVALID')
    return int(fields[19])


def _self_group(pid: int, unit: str) -> str:
    rows = (_PROC / str(pid) / 'cgroup').read_text().splitlines()
    _require(len(rows) == 1 and rows[0].startswith('0::'), 'MD_RECOVERY_UNIFIED_CGROUP_REQUIRED')
    group = rows[0][3:]
    path = PurePosixPath(group)
    _require(path.is_absolute() and not group.startswith('//') and str(path) == group and '..' not in path.parts
             and path.name == unit and group != '/', 'MD_RECOVERY_DEDICATED_UNIT_CGROUP_REQUIRED')
    return group


def _unescape_mount(value: str) -> str:
    return re.sub(r'\\([0-7]{3})', lambda item: chr(int(item[1], 8)), value)


def _check_mount() -> None:
    matches = []
    for line in (_PROC / 'self/mountinfo').read_text().splitlines():
        left, separator, right = line.partition(' - ')
        if not separator:
            continue
        fields, filesystem = left.split(), right.split()
        if len(fields) >= 6 and filesystem and _unescape_mount(fields[4]) == str(_CGROUP):
            matches.append((fields, filesystem))
    _require(len(matches) == 1 and matches[0][1][0] == 'cgroup2'
             and _unescape_mount(matches[0][0][3]) == '/', 'MD_RECOVERY_HOST_CGROUP2_MOUNT_REQUIRED')
    _require(_CGROUP.resolve(strict=True) == _CGROUP, 'MD_RECOVERY_CGROUP_MOUNT_NOT_CANONICAL')


def _read_at(descriptor: int, name: str) -> str:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
    try:
        _require(stat.S_ISREG(os.fstat(fd).st_mode), 'MD_RECOVERY_CGROUP_FILE_INVALID')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            payload = stream.read(65537)
        _require(len(payload) <= 65536, 'MD_RECOVERY_CGROUP_FILE_UNBOUNDED')
        return payload.decode('ascii')
    finally:
        os.close(fd)


def _members(descriptor: int, pid: int) -> None:
    _require(_read_at(descriptor, 'cgroup.type').strip() == 'domain', 'MD_RECOVERY_DOMAIN_CGROUP_REQUIRED')
    events = dict(row.split() for row in _read_at(descriptor, 'cgroup.events').splitlines())
    _require(events.get('populated') == '1', 'MD_RECOVERY_POPULATED_CGROUP_REQUIRED')
    values = _read_at(descriptor, 'cgroup.procs').splitlines()
    _require(len(values) == 1 and values[0].isdecimal() and int(values[0]) == pid,
             'MD_RECOVERY_OTHER_EXECUTION_PRESENT')
    # Broker-free workers create no child cgroups. Even an empty, unrecognised
    # subtree is not an ownership proof and must not be silently skipped.
    with os.scandir(descriptor) as entries:
        for entry in entries:
            _require(not entry.is_symlink() and not entry.is_dir(follow_symlinks=False),
                     'MD_RECOVERY_UNRECOGNIZED_CHILD_CGROUP')


def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode,
            metadata.st_mtime_ns, metadata.st_ctime_ns)


def _kernel_proof(unit: str, pid: int) -> dict[str, object]:
    validate_unit_name(unit)
    _require(pid == os.getpid() and pid > 1, 'MD_RECOVERY_CURRENT_WORKER_REQUIRED')
    start = _start_ticks(pid)
    group = _self_group(pid, unit)
    _check_mount()
    path = _CGROUP / group.removeprefix('/')
    _require(path.is_relative_to(_CGROUP) and path.resolve(strict=True) == path,
             'MD_RECOVERY_CGROUP_PATH_NOT_CANONICAL')
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        _members(descriptor, pid)
        _members(descriptor, pid)
        _require(_directory_identity(before) == _directory_identity(os.fstat(descriptor))
                 == _directory_identity(path.lstat()), 'MD_RECOVERY_CGROUP_CHANGED_DURING_PROOF')
        _require(_start_ticks(pid) == start and _self_group(pid, unit) == group
                 and path.resolve(strict=True) == path, 'MD_RECOVERY_PROCESS_CHANGED_DURING_PROOF')
    finally:
        os.close(descriptor)
    return {'unit': unit, 'pid': pid, 'start_ticks': start, 'cgroup': group,
            'cgroup_device': before.st_dev, 'cgroup_inode': before.st_ino}


def validate_recovery_systemd_unit(unit: str, *, pid: int | None = None) -> dict[str, object]:
    """Validate the startup contract; this may run only read-only systemctl show."""
    validate_unit_name(unit)
    pid = os.getpid() if pid is None else pid
    answer = subprocess.run(
        ['/usr/bin/systemctl', '--user', 'show', unit, '-p', 'Id', '-p', 'LoadState',
         '-p', 'MainPID', '-p', 'ControlGroup', '-p', 'KillMode', '-p', 'Delegate'],
        capture_output=True, text=True, timeout=10,
        env=dict(os.environ, XDG_RUNTIME_DIR=f'/run/user/{os.geteuid()}',
                 DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{os.geteuid()}/bus'),
    )
    _require(answer.returncode == 0, 'MD_RECOVERY_SYSTEMD_UNAVAILABLE')
    values = dict(row.split('=', 1) for row in answer.stdout.splitlines())
    _require(values.get('Id') == unit and values.get('LoadState') == 'loaded'
             and values.get('MainPID') == str(pid) and values.get('KillMode') == 'control-group'
             and values.get('Delegate') == 'no', 'MD_RECOVERY_SYSTEMD_CONTRACT_DIFFERS')
    proof = _kernel_proof(unit, pid)
    _require(values.get('ControlGroup') == proof['cgroup'], 'MD_RECOVERY_SYSTEMD_KERNEL_CGROUP_DIFFERS')
    return proof


def recovery_cgroup_empty(unit: str, *, binding: Mapping[str, object] | None = None) -> bool:
    """Prove absence of old execution processes, failing closed on any uncertainty."""
    try:
        proof = _kernel_proof(unit, os.getpid())
        return binding is not None and proof == dict(binding)
    except (RecoveryResourceError, OSError, ValueError, TypeError, UnicodeError):
        return False

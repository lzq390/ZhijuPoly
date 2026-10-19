"""Review-only source for one finite inventory guard; never run this copy as root.

Only an action-time approved, root-owned installation at INSTALL/guard.py may
seal, serve, or clean up. No sudo, user code, credentials, or arbitrary unit is
accepted. Default invocation describes the fixed boundary without OS actions.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
import sys
import time

SCOPE = 'acceptance-a-20261019'
SERVICE = 'nexpoly-gpu-systemd-inventory.service'
GUARD_UNIT = 'nexpoly-r2-a-20261019-inventory-guard.service'
INSTALL = Path('/usr/local/lib/nexpoly-r2-acceptance-guard')
RUNTIME = Path('/run/nexpoly-r2-acceptance-20261019')
PROVIDER = Path('/usr/local/lib/nexpoly-gpu-systemd-inventory')
MANIFEST = PROVIDER / 'manifest.json'
UNIT = Path('/etc/systemd/system') / SERVICE
OLD_SHA = 'fb31f3f4d958a0227fb7081bade119bb5a7922de434fec0f4b0578a7788a1f5f'
UNIT_SHA = '6d94403524a58459b18c44bea87a8e9dfcb9e5e1a984d757d4612d227ac07404'
APPROVED_AT = datetime(2026, 10, 19, 2, 53, 34, tzinfo=timezone.utc)
GLOBAL_DEADLINE = datetime(2026, 10, 20, 18, 53, 34, tzinfo=timezone.utc)
START_WINDOW_SECONDS = 20
EXECUTION_SECONDS = 93900
STOP_RESERVE_SECONDS = 300
MODULES = frozenset({'ops/__init__.py', 'ops/gpu_broker/__init__.py',
    'ops/gpu_broker/broker.py', 'ops/gpu_broker/server.py',
    'ops/gpu_broker/systemd_provider.py', 'gpu_resource/__init__.py',
    'gpu_resource/client.py', 'gpu_resource/transient_scope.py',
    'gpu_resource/systemd_inventory.py'})


class GuardError(RuntimeError):
    pass


def need(condition, code):
    if not condition:
        raise GuardError(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def document(data):
    def strict(pairs):
        result = {}
        for key, value in pairs:
            need(key not in result, 'duplicate_public_field')
            result[key] = value
        return result
    try:
        result = json.loads(data, object_pairs_hook=strict)
    except (ValueError, UnicodeError):
        raise GuardError('invalid_public_document') from None
    need(type(result) is dict, 'invalid_public_document')
    return result


def validate_manifests(before, candidate, request):
    need(digest(before) == OLD_SHA, 'old_manifest_changed')
    old, new = document(before), document(candidate)
    need(set(old) == set(new) == {'schema_version', 'source_sha', 'source_tree', 'files'},
         'unexpected_manifest_fields')
    need(old['schema_version'] == new['schema_version'] == 1, 'unexpected_manifest_version')
    need(type(old['files']) is dict and set(old['files']) == MODULES
         and old['files'] == new['files'], 'root_module_change_forbidden')
    need(all(type(v) is str and re.fullmatch('[0-9a-f]{64}', v) for v in old['files'].values()),
         'invalid_root_module_pin')
    need(new['source_sha'] == request['source_sha'] and new['source_tree'] == request['source_tree']
         and digest(candidate) == request['candidate_manifest_sha256'], 'candidate_manifest_changed')
    return old, new


def validate_request(request):
    fields = {'schema_version', 'scope_id', 'source_sha', 'source_tree',
              'candidate_manifest_sha256', 'guard_sha256', 'global_deadline_utc'}
    need(set(request) == fields and request['schema_version'] == 1 and request['scope_id'] == SCOPE,
         'invalid_fixed_guard_request')
    for key, count in [('source_sha', 40), ('source_tree', 40),
                       ('candidate_manifest_sha256', 64), ('guard_sha256', 64)]:
        need(type(request[key]) is str and re.fullmatch('[0-9a-f]{%d}' % count, request[key]),
             'invalid_public_source_pin')
    need(request['global_deadline_utc'] == '2026-10-20T18:53:34Z', 'budget_extension_forbidden')


def deadline_plan(request, *, now, monotonic, boot_id):
    validate_request(request)
    need(now.tzinfo is not None and math.isfinite(monotonic) and monotonic >= 0,
         'invalid_clock_evidence')
    need(APPROVED_AT <= now and (GLOBAL_DEADLINE - now).total_seconds() >= EXECUTION_SECONDS,
         'insufficient_remaining_execution_budget')
    need(type(boot_id) is str and re.fullmatch('[0-9a-f-]{36}', boot_id), 'invalid_boot_identity')
    return dict(request, boot_id=boot_id, sealed_at_utc=now.isoformat(),
                sealed_monotonic=monotonic, deadline_monotonic=monotonic + EXECUTION_SECONDS,
                execution_seconds=EXECUTION_SECONDS)


def validate_plan(plan, request):
    validate_request(request)
    extra = {'boot_id', 'sealed_at_utc', 'sealed_monotonic', 'deadline_monotonic', 'execution_seconds'}
    need(set(plan) == set(request) | extra and all(plan[k] == v for k, v in request.items()),
         'sealed_request_changed')
    need(type(plan['sealed_monotonic']) in {int, float}
         and type(plan['deadline_monotonic']) in {int, float}
         and math.isfinite(plan['sealed_monotonic']) and plan['sealed_monotonic'] >= 0
         and plan['deadline_monotonic'] == plan['sealed_monotonic'] + EXECUTION_SECONDS
         and type(plan['execution_seconds']) is int and plan['execution_seconds'] == EXECUTION_SECONDS,
         'invalid_sealed_clock')
    need(type(plan['boot_id']) is str and re.fullmatch('[0-9a-f-]{36}', plan['boot_id']),
         'invalid_boot_identity')
    sealed = datetime.fromisoformat(plan['sealed_at_utc'])
    need(sealed.tzinfo is not None and APPROVED_AT <= sealed and (GLOBAL_DEADLINE-sealed).total_seconds() >= EXECUTION_SECONDS,
         'invalid_sealed_start')


def remaining(plan, *, now, monotonic, boot_id):
    need(now.tzinfo is not None and type(monotonic) in {int, float}, 'invalid_clock_evidence')
    need(plan['boot_id'] == boot_id, 'boot_changed_cleanup_only')
    need(plan['execution_seconds'] == EXECUTION_SECONDS
         and plan['deadline_monotonic'] == plan['sealed_monotonic'] + EXECUTION_SECONDS,
         'execution_deadline_changed')
    need(math.isfinite(monotonic) and monotonic >= plan['sealed_monotonic'], 'monotonic_clock_changed')
    return min(plan['deadline_monotonic'] - monotonic, (GLOBAL_DEADLINE - now).total_seconds())


def restore_manifest(plan, io):
    """Stop only the fixed owned provider before any compare-and-swap restore."""
    io.verify_fixed_files()
    state = io.state()
    if state['MainPID'] != '0':
        binding = io.binding()
        need(io.owns(state, binding), 'provider_epoch_changed_no_stop')
        io.stop()
    need(io.state()['MainPID'] == '0', 'provider_stop_unconfirmed')
    current = digest(io.manifest())
    if current != OLD_SHA:
        need(current == plan['candidate_manifest_sha256'], 'manifest_cas_conflict')
        io.replace(io.backup(), expected=plan['candidate_manifest_sha256'])
    need(digest(io.manifest()) == OLD_SHA, 'manifest_restore_unconfirmed')
    io.verify_fixed_files()
    return {'scope_id': SCOPE, 'status': 'ROOT_STOPPED_OLD_MANIFEST_RESTORED',
            'provider_main_pid': 0, 'root_modules_changed': False, 'original_unit_changed': False}


def trusted_directory(path):
    for parent in (*reversed(path.parents), path):
        item = parent.lstat()
        need(stat.S_ISDIR(item.st_mode) and not stat.S_ISLNK(item.st_mode)
             and item.st_uid == 0 and not item.st_mode & 0o022, 'untrusted_root_ancestor')


def read_root(path, *, limit=2*1024*1024):
    trusted_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        item = os.fstat(fd)
        need(stat.S_ISREG(item.st_mode) and item.st_uid == 0 and item.st_nlink == 1
             and not item.st_mode & 0o022 and item.st_size <= limit, 'untrusted_root_file')
        data = os.read(fd, limit + 1)
        after, linked = os.fstat(fd), path.lstat()
        identity = lambda v: (v.st_dev, v.st_ino, v.st_size, v.st_mtime_ns, v.st_ctime_ns)
        need(identity(item) == identity(after) == identity(linked) and len(data) == item.st_size,
             'root_file_changed')
        return data
    finally:
        os.close(fd)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new(path, data, *, mode=0o600):
    trusted_directory(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as output:
        os.fchmod(output.fileno(), mode)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    sync_directory(path.parent)


@contextmanager
def exclusive_lock():
    trusted_directory(INSTALL)
    path = INSTALL/'guard.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        item = os.fstat(fd)
        need(stat.S_ISREG(item.st_mode) and item.st_uid == 0 and item.st_nlink == 1
             and not item.st_mode & 0o077, 'untrusted_guard_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GuardError('guard_action_already_running') from None
        yield
    finally:
        os.close(fd)


def root_only():
    need(os.geteuid() == 0 and Path(__file__).absolute() == INSTALL/'guard.py',
         'only_the_reviewed_root_owned_installation_may_execute')
    raw = read_root(INSTALL/'guard.py')
    request = document(read_root(INSTALL/'request.json'))
    validate_request(request)
    need(digest(raw) == request['guard_sha256'], 'guard_code_changed')


def systemctl(action):
    need(action in {'start', 'stop', 'show'}, 'unlisted_systemd_action')
    command = ['/usr/bin/systemctl', action]
    if action == 'show':
        command += ['--property=ActiveState', '--property=MainPID', '--property=InvocationID',
                    '--property=ExecMainStartTimestampMonotonic', '--property=UnitFileState']
    command += ['--', SERVICE]
    try:
        result = subprocess.run(command, check=True, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=20,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'SYSTEMD_COLORS': '0', 'SYSTEMD_PAGER': ''})
    except (OSError, subprocess.SubprocessError):
        raise GuardError('fixed_provider_systemd_action_failed') from None
    return result.stdout


class RootIO:
    def __init__(self, request):
        self.request = request

    def verify_fixed_files(self):
        need(digest(read_root(UNIT)) == UNIT_SHA, 'original_unit_changed')
        before = document(self.backup())
        need(set(before['files']) == MODULES, 'root_module_set_changed')
        for name, expected in before['files'].items():
            need(digest(read_root(PROVIDER/name)) == expected, 'root_module_changed')

    def state(self):
        pairs = [line.split('=', 1) for line in systemctl('show').splitlines() if '=' in line]
        state = dict(pairs)
        need(set(state) == {'ActiveState', 'MainPID', 'InvocationID', 'ExecMainStartTimestampMonotonic', 'UnitFileState'}
             and len(pairs) == 5 and state['MainPID'].isdecimal()
             and state['ExecMainStartTimestampMonotonic'].isdecimal(), 'provider_state_invalid')
        return state

    def binding(self):
        path = RUNTIME/'provider-owned.json'
        return document(read_root(path)) if path.exists() else None

    def owns(self, state, binding):
        if binding is not None:
            return (set(binding) == set(state) and state['InvocationID'] == binding['InvocationID']
                    and state['MainPID'] == binding['MainPID']
                    and state['ExecMainStartTimestampMonotonic'] == binding['ExecMainStartTimestampMonotonic'])
        # The reviewer must grant an exclusive start window for this fixed
        # inventory provider. This intent closes the crash-before-binding gap;
        # it never claims a later restart or a service with another manifest.
        path = RUNTIME/'provider-start-intent.json'
        if not path.exists():
            return False
        intent = document(read_root(path))
        if set(intent) != {'scope_id', 'boot_id', 'start_monotonic_us', 'end_monotonic_us'}:
            return False
        start, end = intent['start_monotonic_us'], intent['end_monotonic_us']
        return (intent['scope_id'] == SCOPE and intent['boot_id'] == boot_identity()
                and type(start) is int and type(end) is int and start > 0
                and end-start == START_WINDOW_SECONDS*1000000
                and start <= int(state['ExecMainStartTimestampMonotonic']) <= end
                and re.fullmatch('[0-9a-f]{32}', state['InvocationID']) is not None
                and digest(self.manifest()) == self.request['candidate_manifest_sha256'])

    def manifest(self):
        return read_root(MANIFEST)

    def backup(self):
        data = read_root(INSTALL/'manifest.before.json')
        need(digest(data) == OLD_SHA, 'backup_changed')
        return data

    def replace(self, data, *, expected):
        need(self.state()['MainPID'] == '0', 'provider_must_be_stopped')
        need(digest(self.manifest()) == expected, 'manifest_cas_conflict')
        stage = PROVIDER/'.manifest.acceptance-a-20261019.tmp'
        write_new(stage, data, mode=0o644)
        need(digest(self.manifest()) == expected and self.state()['MainPID'] == '0',
             'manifest_changed_before_atomic_replace')
        os.replace(stage, MANIFEST)
        sync_directory(PROVIDER)
        need(digest(self.manifest()) == digest(data), 'manifest_replace_unconfirmed')

    def stop(self):
        systemctl('stop')


def boot_identity():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def stop_message_allowed(data, credentials, plan):
    """A fixed stop-only request; peer UID/GID comes from SCM_CREDENTIALS."""
    if (type(credentials) is not tuple or len(credentials) != 3
            or type(credentials[0]) is not int or credentials[0] <= 0
            or credentials[1:] != (1001,1001) or len(data) > 1024):
        return False
    try:
        value = document(data)
    except GuardError:
        return False
    return (set(value) == {'operation','scope_id','candidate_manifest_sha256'}
            and value['operation'] == 'stop_restore' and value['scope_id'] == SCOPE
            and value['candidate_manifest_sha256'] == plan['candidate_manifest_sha256'])


class StopMailbox:
    """New reviewed root:1001 mode-0660 Unix socket, stop/restore only."""
    def __init__(self, plan):
        self.plan = plan
        self.path = RUNTIME/'stop.sock'
        self.connection = None

    def __enter__(self):
        trusted_directory(RUNTIME)
        need(not self.path.exists() and not self.path.is_symlink(), 'existing_stop_socket')
        self.connection = socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM)
        self.connection.setsockopt(socket.SOL_SOCKET,socket.SO_PASSCRED,1)
        self.connection.settimeout(2)
        self.connection.bind(str(self.path))
        os.chmod(self.path,0o660,follow_symlinks=False)
        item = self.path.lstat()
        need(stat.S_ISSOCK(item.st_mode) and item.st_uid == 0 and item.st_gid == 1001
             and stat.S_IMODE(item.st_mode) == 0o660,'unsafe_stop_socket')
        write_new(RUNTIME/'stop-socket.public.json',json.dumps(
            {'scope_id':SCOPE,'device':item.st_dev,'inode':item.st_ino},sort_keys=True).encode(),mode=0o644)
        return self

    def requested(self):
        try:
            data, ancillary, flags, _ = self.connection.recvmsg(1025,socket.CMSG_SPACE(12))
        except socket.timeout:
            return False
        if flags & (socket.MSG_TRUNC|socket.MSG_CTRUNC):
            return False
        credentials = [struct.unpack('3i',value) for level,kind,value in ancillary
                       if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(value) == 12]
        return len(credentials) == 1 and stop_message_allowed(data,credentials[0],self.plan)

    def __exit__(self,*_):
        if self.connection is not None:
            self.connection.close()


def collect_stop_socket():
    path, proof = RUNTIME/'stop.sock', RUNTIME/'stop-socket.public.json'
    if not path.exists() and not path.is_symlink():
        return
    value = document(read_root(proof))
    item = path.lstat()
    need(set(value) == {'scope_id','device','inode'} and value['scope_id'] == SCOPE
         and stat.S_ISSOCK(item.st_mode) and item.st_uid == 0 and item.st_gid == 1001
         and (item.st_dev,item.st_ino) == (value['device'],value['inode']), 'stop_socket_identity_changed')
    path.unlink()
    sync_directory(RUNTIME)


def prepare_runtime():
    trusted_directory(RUNTIME.parent)
    RUNTIME.mkdir(mode=0o755, exist_ok=True)
    trusted_directory(RUNTIME)


def seal():
    root_only()
    request_raw = read_root(INSTALL/'request.json')
    request = document(request_raw)
    validate_request(request)
    need(digest(read_root(INSTALL/'guard.py')) == request['guard_sha256'], 'guard_code_changed')
    before, candidate = read_root(MANIFEST), read_root(INSTALL/'manifest.candidate.json')
    validate_manifests(before, candidate, request)
    need(not (INSTALL/'manifest.before.json').exists() and not (RUNTIME/'plan.json').exists(),
         'existing_campaign_seal_no_reset')
    state = dict(line.split('=', 1) for line in systemctl('show').splitlines() if '=' in line)
    need(state.get('MainPID') == '0' and state.get('ActiveState') in {'inactive', 'failed'}
         and state.get('UnitFileState') in {'static', 'disabled'},
         'provider_already_active')
    plan = deadline_plan(request, now=datetime.now(timezone.utc), monotonic=time.monotonic(),
                         boot_id=boot_identity())
    prepare_runtime()
    read_root(INSTALL/'guard.py')
    write_new(INSTALL/'manifest.before.json', before, mode=0o644)
    io = RootIO(request)
    io.verify_fixed_files()
    write_new(RUNTIME/'plan.json', json.dumps(plan, sort_keys=True).encode(), mode=0o644)
    write_new(INSTALL/'recovery.json', json.dumps(request, sort_keys=True).encode())


def serve():
    root_only()
    need(os.getegid() == 1001, 'reviewed_guard_group_required')
    plan = document(read_root(RUNTIME/'plan.json'))
    request = document(read_root(INSTALL/'request.json'))
    validate_plan(plan, request)
    need(digest(read_root(INSTALL/'guard.py')) == plan['guard_sha256'], 'guard_code_changed')
    io = RootIO(plan)
    io.verify_fixed_files()
    write_new(RUNTIME/'started.json', b'{"started_once":true}')
    boot = boot_identity()
    need(remaining(plan, now=datetime.now(timezone.utc), monotonic=time.monotonic(), boot_id=boot)
         > STOP_RESERVE_SECONDS, 'guard_deadline_reached')
    candidate = read_root(INSTALL/'manifest.candidate.json')
    validate_manifests(io.backup(), candidate, plan)
    io.replace(candidate, expected=OLD_SHA)
    intent_start = int(time.monotonic()*1000000)
    intent = {'scope_id': SCOPE, 'boot_id': boot, 'start_monotonic_us': intent_start,
              'end_monotonic_us': intent_start+START_WINDOW_SECONDS*1000000}
    write_new(RUNTIME/'provider-start-intent.json', json.dumps(intent, sort_keys=True).encode())
    systemctl('start')
    state = io.state()
    need(state['MainPID'] != '0' and re.fullmatch('[0-9a-f]{32}', state['InvocationID']),
         'provider_start_unconfirmed')
    write_new(RUNTIME/'provider-owned.json', json.dumps(state, sort_keys=True).encode())
    with StopMailbox(plan) as mailbox:
        while remaining(plan, now=datetime.now(timezone.utc), monotonic=time.monotonic(), boot_id=boot) > STOP_RESERVE_SECONDS:
            need(io.state() == state, 'provider_epoch_changed')
            need(digest(io.manifest()) == plan['candidate_manifest_sha256'], 'running_manifest_changed')
            if mailbox.requested():
                return


def cleanup():
    root_only()
    request = document(read_root(INSTALL/'recovery.json'))
    validate_request(request)
    io = RootIO(request)
    result = restore_manifest(request, io)
    prepare_runtime()
    collect_stop_socket()
    # Retain one public receipt. Installation removal is a separately reviewed
    # fixed OS action after the transient unit has finished, never an rm -rf.
    result['guard_installation_retained_for_reviewed_uninstall'] = True
    result.update(source_sha=request['source_sha'],source_tree=request['source_tree'],
                  candidate_manifest_sha256=request['candidate_manifest_sha256'],guard_sha256=request['guard_sha256'])
    result['at_utc'] = datetime.now(timezone.utc).isoformat()
    target = RUNTIME/'result.public.json'
    if not target.exists():
        write_new(target, json.dumps(result, sort_keys=True).encode(), mode=0o644)


def describe():
    return {'scope_id': SCOPE, 'provider_unit': SERVICE, 'transient_guard_unit': GUARD_UNIT,
        'root_installation': str(INSTALL), 'root_runtime': str(RUNTIME),
        'allowed_systemd_actions': ['show', 'start', 'stop'],
        'manifest_target': str(MANIFEST), 'original_modules': sorted(MODULES),
        'owner_stop_socket':str(RUNTIME/'stop.sock'),'owner_stop_socket_uid':1001,
        'owner_stop_operation':'stop_restore_only_no_arbitrary_command',
        'execution_limit_seconds': EXECUTION_SECONDS, 'stop_reserve_seconds': STOP_RESERVE_SECONDS,
        'global_deadline_utc': '2026-10-20T18:53:34Z', 'sudoers_changes': False,
        'user_code_executed_as_root': False, 'default_mode': 'describe'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=('describe', 'seal', 'serve', 'cleanup'), default='describe')
    args = parser.parse_args()
    try:
        if args.action == 'describe':
            print(json.dumps(describe(), sort_keys=True))
        else:
            with exclusive_lock():
                {'seal': seal, 'serve': serve, 'cleanup': cleanup}[args.action]()
    except (GuardError, OSError, ValueError, KeyError, TypeError, OverflowError):
        parser.exit(2, 'Fixed inventory guard failed closed; no credentials or raw OS diagnostics printed.\n')


if __name__ == '__main__':
    main()

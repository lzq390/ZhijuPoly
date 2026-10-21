"""Read-only proof of the existing Main native direct launcher and live identity.

No full host inventory, EnvironmentFile, descriptor, model, or control operation.
Native status/activation records remain opaque; only public identities are emitted.
"""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys

MAIN = Path('/data/lzq/gith/nexpoly-dev')
SOURCE = 'a2ef949e68826bd542eda9bec823df942bab9689'
TREE = '875d4d44bd53a508badcf653089c0ef55c68f673'
FILES = {
    'gpu_resource/authority.py': '0fc51931ad07fa641b2dbf4cb55abe23d3c802ac371dbed8f630cb1677695b69',
    'ops/gpu_broker/server.py': 'fb00eabae80f2b6a7669e26afcb5fefc3d3e4fed041761f0de384b5ce1c8a525',
    'scripts/dev_gpu_session.py': 'ee052905b1c9863b5af2f50cb56067821c4857ef063b9d9a69cf5fe71979bfdd',
    'scripts/dev_server_gpu.sh': 'dd72355e52ebc76552a21f25af1fef11f7b7e3085ae7110b563aa3548370cde7',
    'scripts/dev_worker_process.py': '2d65ca6ffb25e66f78185fc1883f6f9e24247b19fb737b6de7c54afe19c3e0dd',
    'scripts/gpu_mps_control.sh': 'e2aaf9fa965e95a597207f1490d9d217d5391a0306a16705a0f2babb50f4e58f'}


def need(value):
    if not value: raise RuntimeError('fixed original native mode proof failed')


def git(args):
    value = subprocess.run(['/usr/bin/git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
                            '-C', str(MAIN), *args], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8)
    need(value.returncode == 0)
    return value.stdout.decode().strip()


def source_proof():
    need(git(['rev-parse', '--verify', 'HEAD']) == SOURCE
         and git(['rev-parse', '--verify', 'HEAD^{tree}']) == TREE)
    git(['diff', '--quiet', '--ignore-submodules', '--'])
    git(['diff', '--cached', '--quiet', '--ignore-submodules', '--'])
    for name, digest in FILES.items():
        path = MAIN / name
        need(path.resolve() == path and hashlib.sha256(path.read_bytes()).hexdigest() == digest)
    return {'source_sha': SOURCE, 'source_tree': TREE, 'public_source_sha256': FILES,
            'restore_argv': ['/bin/bash', str(MAIN / 'scripts/dev_server_gpu.sh'), 'gpu-session-up'],
            'original_native_direct_start_selection': True,
            'original_native_9001_launcher_only': True,
            'legacy_full_collector_required_by_this_original_mode': False,
            'new_trusted_flag_or_Main_modification': False}


def live_proof():
    proof = source_proof()
    sys.path.insert(0, str(MAIN))
    from scripts import dev_gpu_session as native
    from scripts.dev_worker_process import _validate_record_shape
    state = native.status(); record = native._controller_record()
    need(state.get('status') == 'ready' and state.get('direct_start') is True
         and state.get('contaminated') is False and state.get('source_sha') == SOURCE
         and state.get('source_tree') == TREE)
    directory = Path(record['run_directory'])
    directory.relative_to(MAIN / '.runtime/gpu-session/runs')
    argv = record['argv']
    expected = [argv[0], str(MAIN / 'scripts/dev_gpu_session.py'), 'serve', '--execute',
                '--run-directory', str(directory), '--source-sha', SOURCE, '--source-tree', TREE,
                '--direct-start']
    need(argv == expected and Path(argv[0]).resolve() == Path('/usr/bin/python3').resolve())
    manifest = native._load_private_json(directory / 'activation-manifest.json')
    worker = _validate_record_shape(manifest.get('md_process'))
    for key, value in (('session_id', record['session_id']), ('source_sha', SOURCE), ('source_tree', TREE)):
        need(manifest.get(key) == value and worker.get(key) == value)
    socket = Path(worker['socket']); socket.relative_to(MAIN / '.runtime')
    worker_argv = [worker['python'], '-m', 'uvicorn', 'app.main:app', '--uds', str(socket)]
    need(native.process_start_ticks(worker['pid']) == worker['start_ticks']
         and list(native.process_argv(worker['pid'])) == worker_argv
         and worker['argv'] == worker_argv)
    proof.update(status='PASS_ORIGINAL_NATIVE_DIRECT_MODE_READONLY_PROOF',
                 controller={key: record[key] for key in ('pid', 'start_ticks', 'session_id', 'source_sha', 'source_tree', 'status_file')},
                 controller_argv=argv, native_status_ready_direct_unchanged=True,
                 md_process={'pid': worker['pid'], 'start_ticks': worker['start_ticks'],
                             'cgroup': (Path('/proc') / str(worker['pid']) / 'cgroup').read_text().strip()},
                 native_activation_md_identity_checked=True)
    return proof


def main():
    try:
        need(os.geteuid() == os.getegid() == 1001)
        if sys.argv[1:] == ['--source-only']:
            value = {'status': 'PASS_ORIGINAL_NATIVE_SOURCE_ONLY', 'source': source_proof()}
        elif sys.argv[1:] == ['--live']:
            value = live_proof()
        else:
            raise RuntimeError('fixed proof mode required')
        print(json.dumps(value, sort_keys=True)); return 0
    except Exception as error:
        print(json.dumps({'status': 'BLOCKED_ORIGINAL_NATIVE_MODE_PROOF',
                          'error_type': type(error).__name__, 'private_values_or_exception_text_exported': False}))
        return 3


if __name__ == '__main__':
    raise SystemExit(main())

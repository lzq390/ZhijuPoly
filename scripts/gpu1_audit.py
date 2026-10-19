"""GPU1-only NVML queries for the isolated native acceptance controller."""
from __future__ import annotations

import subprocess
from ops.gpu_broker.broker import BrokerError

GPU_INDEX = 1
GPU_UUID = 'GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771'


class Gpu1AuditError(BrokerError):
    def __init__(self, message: str):
        super().__init__('gpu_inventory_unavailable', message)


def _query(fields: str, *, run=None) -> list[list[str]]:
    if fields not in {'--query-gpu=index,uuid', '--query-compute-apps=gpu_uuid,pid'}:
        raise Gpu1AuditError('Unsupported fixed GPU1 inventory query')
    command = ('nvidia-smi', '-i', GPU_UUID, fields, '--format=csv,noheader,nounits')
    try:
        completed = (run or subprocess.run)(command, check=True, capture_output=True,
                                            text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        raise Gpu1AuditError('GPU1 inventory is unavailable') from None
    return [[field.strip() for field in row.split(',')]
            for row in completed.stdout.splitlines() if row.strip()]


def query_gpu1_inventory(*, run=None) -> dict[int, str]:
    rows = _query('--query-gpu=index,uuid', run=run)
    if rows != [[str(GPU_INDEX), GPU_UUID]]:
        raise Gpu1AuditError('Physical GPU1 identity differs from the fixed policy')
    return {GPU_INDEX: GPU_UUID}


def query_gpu1_compute_processes(*, run=None) -> dict[str, frozenset[int]]:
    rows = _query('--query-compute-apps=gpu_uuid,pid', run=run)
    processes = set()
    for fields in rows:
        if (len(fields) != 2 or fields[0] != GPU_UUID or not fields[1].isdecimal()
                or int(fields[1]) <= 0):
            raise Gpu1AuditError('GPU1 compute process inventory is invalid')
        processes.add(int(fields[1]))
    return {GPU_UUID: frozenset(processes)}

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .models import BatchError, BatchSettings
from .storage import read_json, write_json


class ExecutionStopped(Exception):
    pass


def run_isolated(request: dict, config: BatchSettings, scratch: Path, *, guard=None, timeout: float | None = None) -> dict:
    scratch.mkdir(parents=True, exist_ok=True)
    identity = uuid4().hex
    request_path, response_path = scratch / f"{identity}.request.json", scratch / f"{identity}.response.json"
    error_path = scratch / f"{identity}.stderr"
    settings = {**asdict(config), "storage_root": str(config.storage_root)}
    write_json(request_path, {**request, "config": settings})
    environment = {**os.environ, "NEXPOLY_BATCH_PARENT_PID": str(os.getpid()), "TMPDIR": str(scratch.resolve()), "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "PYTHONUNBUFFERED": "1"}
    backend = str(Path(__file__).resolve().parents[3])
    environment["PYTHONPATH"] = backend + os.pathsep + environment.get("PYTHONPATH", "")
    started = time.monotonic()
    process = None
    try:
        with error_path.open("wb") as errors:
            process = subprocess.Popen([sys.executable, "-m", "app.services.polymerization_batch.process", str(request_path), str(response_path)],
                                       stdout=subprocess.DEVNULL, stderr=errors, env=environment,
                                       start_new_session=True)
            while process.poll() is None:
                if guard:
                    guard()
                if time.monotonic() - started > (timeout if timeout is not None else config.subprocess_seconds):
                    raise BatchError("计算子进程超时。", "subprocess_timeout", 422)
                time.sleep(0.2)
        if not response_path.exists():
            raise BatchError("计算子进程异常退出或超过内存限额。", "process_exit", 422)
        result = read_json(response_path)
        if not result["ok"]:
            raise BatchError(result["message"], result["code"], result.get("status", 422))
        return result["data"]
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                # The child can exit between poll() and killpg(). Preserve the
                # original cancellation/timeout instead of reporting a new error.
                pass
            process.wait(timeout=5)
        for path in (request_path, response_path, error_path):
            path.unlink(missing_ok=True)


SYSTEM_ERRORS = {"engine_unavailable", "engine_version_mismatch", "artifact_corrupt", "result_limit", "job_timeout"}


def classify_isolated(smiles: list[str], call) -> dict:
    try:
        return call({"action": "classify", "smiles": smiles})
    except BatchError as exc:
        if exc.code in SYSTEM_ERRORS:
            raise
        if len(smiles) == 1:
            return {"rows": [], "errors": {smiles[0]: f"{exc.code}: {exc}"}}
        middle = len(smiles) // 2
        first, second = classify_isolated(smiles[:middle], call), classify_isolated(smiles[middle:], call)
        return {"rows": first["rows"] + second["rows"], "errors": {**first["errors"], **second["errors"]}}


def generate_isolated(a: list[str], b: list[str], call):
    try:
        yield from call({"action": "generate", "a": a, "b": b})["pairs"]
    except BatchError as exc:
        if exc.code in SYSTEM_ERRORS:
            raise
        if len(a) == len(b) == 1:
            yield {"a": a[0], "b": b[0], "candidates": [], "status": "error", "error_code": exc.code, "message": str(exc)}
        elif len(a) >= len(b):
            middle = len(a) // 2
            yield from generate_isolated(a[:middle], b, call)
            yield from generate_isolated(a[middle:], b, call)
        else:
            middle = len(b) // 2
            yield from generate_isolated(a, b[:middle], call)
            yield from generate_isolated(a, b[middle:], call)

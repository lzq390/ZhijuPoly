#!/usr/bin/env python3
"""Run and seal the development-only real GPU acceptance for monomer DFT.

The harness owns the complete observation interval: it snapshots physical
GPU2, starts or verifies the development stack, runs a raw AIMNet
energy/forces/Hessian calculation under an exact Broker lease on GPU1, runs
the public Backend -> UDS -> Worker job/cancel/journal/artifact path, resolves
GPU3 as either an actual overflow calculation or an exact foreign Docker
claim plus Broker rejection, then stops any stack it started and snapshots
GPU2 again.

Run this script with the isolated monomer DFT Python interpreter.  It refuses
the production checkout and a dirty or mismatched Git tree.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import Any
import urllib.error
import urllib.request
import uuid


SCRIPT_ROOT = Path(__file__).absolute().parent
REPO_ROOT = SCRIPT_ROOT.parent
PRODUCTION_REPO_ROOT = Path("/data/lzq/gith/nexpoly")
GPU_UUIDS = {
    "1": "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
    "2": "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe",
    "3": "GPU-0818ca6b-d9b6-af6a-71bf-afe3777ee3a5",
}
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_HTTP_BYTES = 256 * 1024 * 1024

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SCRIPT_ROOT))

import monomer_dft_gpu_acceptance as acceptance_contract
import monomer_dft_runtime_contract as runtime_contract
import preflight_monomer_dft_env as preflight
import smoke_monomer_dft_env as smoke_runtime


class AcceptanceHarnessError(RuntimeError):
    """The live acceptance run did not prove its complete contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceHarnessError(message)


def _run(
    *command: str,
    timeout: float = 120.0,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=REPO_ROOT,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AcceptanceHarnessError(
            f"acceptance command failed: {' '.join(command)}"
        ) from exc


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise AcceptanceHarnessError(f"acceptance file is unsafe: {path}")
    return _sha256_bytes(path.read_bytes())


def _load_json(path: Path, name: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise AcceptanceHarnessError(f"{name} is unavailable or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AcceptanceHarnessError(f"{name} is invalid") from exc
    if not isinstance(value, dict):
        raise AcceptanceHarnessError(f"{name} is not an object")
    return value


def _write_private_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8") + b"\n"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _safe_runtime_root() -> Path:
    root = REPO_ROOT / ".runtime"
    metadata = root.lstat()
    _require(
        stat.S_ISDIR(metadata.st_mode)
        and not root.is_symlink()
        and metadata.st_uid == os.geteuid(),
        "acceptance runtime root must be an owner-controlled real directory",
    )
    root.chmod(0o700)
    runs = root / "runs"
    if not runs.exists():
        runs.mkdir(mode=0o700)
    metadata = runs.lstat()
    _require(
        stat.S_ISDIR(metadata.st_mode)
        and not runs.is_symlink()
        and metadata.st_uid == os.geteuid(),
        "acceptance run root must be an owner-controlled real directory",
    )
    runs.chmod(0o700)
    return root


def validate_git_authority(authority_sha: str, authority_tree: str) -> None:
    _require(
        REPO_ROOT.resolve() != PRODUCTION_REPO_ROOT.resolve(),
        "real DFT acceptance is forbidden in the production repository",
    )
    _require(
        SHA_RE.fullmatch(authority_sha) is not None
        and SHA_RE.fullmatch(authority_tree) is not None,
        "acceptance requires full Git commit and tree IDs",
    )
    actual_sha = _run("git", "rev-parse", "HEAD").stdout.strip()
    actual_tree = _run("git", "rev-parse", "HEAD^{tree}").stdout.strip()
    _require(
        (actual_sha, actual_tree) == (authority_sha, authority_tree),
        "worktree HEAD/tree differs from the requested F authority",
    )
    status = _run(
        "git",
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=none",
    ).stdout
    _require(not status, "GPU acceptance requires a completely clean worktree")
    _require(
        _run("git", "check-ignore", "-q", ".runtime/probe", check=False).returncode
        == 0,
        ".runtime is not ignored by Git",
    )


def _read_proc_start_ticks(pid: int) -> int:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        command_end = raw.rfind(")")
        ticks = int(raw[command_end + 1 :].split()[19])
    except (OSError, IndexError, ValueError) as exc:
        raise AcceptanceHarnessError(
            f"cannot establish GPU2 process identity for PID {pid}"
        ) from exc
    _require(ticks > 0, "GPU2 process start time is invalid")
    return ticks


def snapshot_gpu2() -> dict[str, Any]:
    gpu_rows = _run(
        "nvidia-smi",
        "--query-gpu=index,uuid,memory.used",
        "--format=csv,noheader,nounits",
        timeout=10.0,
    ).stdout.splitlines()
    matches: list[tuple[str, str, str]] = []
    for row in gpu_rows:
        fields = tuple(part.strip() for part in row.split(",", 2))
        if len(fields) == 3 and fields[0] == "2":
            matches.append(fields)
    _require(len(matches) == 1, "physical GPU2 inventory is missing or duplicated")
    index, gpu_uuid, memory = matches[0]
    _require(gpu_uuid == GPU_UUIDS["2"], "physical GPU2 UUID drifted")
    try:
        memory_used_mib = int(memory)
    except ValueError as exc:
        raise AcceptanceHarnessError("GPU2 memory inventory is invalid") from exc
    _require(memory_used_mib >= 0, "GPU2 memory inventory is negative")

    process_rows = _run(
        "nvidia-smi",
        "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
        "--format=csv,noheader,nounits",
        timeout=10.0,
    ).stdout.splitlines()
    processes: list[dict[str, Any]] = []
    for row in process_rows:
        fields = [part.strip() for part in row.split(",", 3)]
        if len(fields) != 4 or fields[0] != GPU_UUIDS["2"]:
            continue
        try:
            pid = int(fields[1])
            used_memory_mib = int(fields[3])
        except ValueError as exc:
            raise AcceptanceHarnessError(
                "GPU2 CUDA process inventory is invalid"
            ) from exc
        processes.append(
            {
                "pid": pid,
                "process_start_ticks": _read_proc_start_ticks(pid),
                "process_name": fields[2],
                "used_memory_mib": used_memory_mib,
            }
        )
    processes.sort(
        key=lambda item: (
            item["pid"],
            item["process_start_ticks"],
            item["process_name"],
            item["used_memory_mib"],
        )
    )
    return {
        "index": int(index),
        "uuid": gpu_uuid,
        "memory_used_mib": memory_used_mib,
        "compute_processes": processes,
    }


def _http(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> tuple[int, bytes, dict[str, str]]:
    body = (
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if payload is not None
        else None
    )
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers=request_headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_HTTP_BYTES + 1)
            _require(len(data) <= MAX_HTTP_BYTES, "acceptance HTTP response is oversized")
            return response.status, data, dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        data = exc.read(MAX_HTTP_BYTES + 1)
        _require(len(data) <= MAX_HTTP_BYTES, "acceptance HTTP error is oversized")
        return exc.code, data, dict(exc.headers.items())
    except (OSError, urllib.error.URLError) as exc:
        raise AcceptanceHarnessError(f"acceptance HTTP request failed: {url}") from exc


def _json_response(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    expected: set[int] = {200},
) -> dict[str, Any]:
    status, body, _response_headers = _http(
        method,
        url,
        payload=payload,
        headers=headers,
    )
    _require(status in expected, f"{method} {url} returned HTTP {status}")
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AcceptanceHarnessError(f"{method} {url} returned invalid JSON") from exc
    _require(isinstance(value, dict), f"{method} {url} did not return an object")
    return value


def _wait_job(base_url: str, job_id: str, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        job = _json_response("GET", f"{base_url}/jobs/{job_id}")
        if job.get("status") in TERMINAL_STATUSES:
            return job
        _require(time.monotonic() < deadline, f"DFT job timed out: {job_id}")
        time.sleep(0.5)


def _wait_job_running(
    base_url: str,
    job_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Require a cancellation target to become an active Worker workload."""

    deadline = time.monotonic() + timeout_seconds
    while True:
        job = _json_response("GET", f"{base_url}/jobs/{job_id}")
        status = job.get("status")
        if status == "running":
            return job
        _require(
            status not in TERMINAL_STATUSES,
            f"DFT cancellation target became {status} before active cancellation",
        )
        _require(
            time.monotonic() < deadline,
            f"DFT cancellation target did not start: {job_id}",
        )
        time.sleep(0.25)


def _journal_path(job_root: Path, job_id: str) -> Path:
    root = job_root.resolve()
    _require(
        root == (REPO_ROOT / ".runtime/monomer-dft-worker-runs").resolve(),
        "DFT journal root escaped the current worktree",
    )
    job_dir = root / job_id
    _require(job_dir.is_dir() and not job_dir.is_symlink(), "DFT job journal is missing")
    journals = [
        path
        for path in job_dir.glob("*/journal.json")
        if path.is_file() and not path.is_symlink()
    ]
    _require(len(journals) == 1, "DFT job must have exactly one durable journal")
    return journals[0]


def _validate_fenced_provenance(job: dict[str, Any]) -> tuple[int, str]:
    provenance = job.get("provenance")
    _require(isinstance(provenance, dict), "completed job lacks provenance")
    gpu_index = provenance.get("gpu_index")
    gpu_uuid = provenance.get("gpu_uuid")
    lease_id = provenance.get("lease_id")
    fencing_token = provenance.get("fencing_token")
    _require(
        gpu_index in {1, "1", 3, "3"}
        and gpu_uuid == GPU_UUIDS[str(gpu_index)]
        and isinstance(lease_id, str)
        and bool(lease_id)
        and isinstance(fencing_token, int)
        and not isinstance(fencing_token, bool)
        and fencing_token > 0,
        "completed job lacks exact lease/fencing provenance",
    )
    return int(gpu_index), str(gpu_uuid)


def run_backend_e2e(
    *,
    base_url: str,
    job_root: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    status = _json_response("GET", f"{base_url}/status")
    _require(
        status.get("schema_ready") is True
        and status.get("available") is True,
        "Backend DFT status is not schema-ready and available",
    )
    completed = _json_response(
        "POST",
        f"{base_url}/jobs",
        payload={
            "input": {
                "smiles": "O",
                "net_charge": 0,
                "multiplicity": 1,
                "psmiles_mode": None,
            },
            "model": "aimnet2",
            "conformer": {"seed": 1, "max_iterations": 500},
            "calculation_type": "single_point",
            "single_point": {
                "properties": ["energy", "forces", "hessian"],
            },
        },
        headers={"Idempotency-Key": f"gpu-accept-complete-{uuid.uuid4().hex}"},
        expected={202},
    )
    completed_id = str(completed.get("job_id") or "")
    _require(bool(completed_id), "Backend did not return a completed-job ID")
    completed = _wait_job(base_url, completed_id, timeout_seconds)
    _require(completed.get("status") == "completed", "Backend E2E job did not complete")
    gpu_index, gpu_uuid = _validate_fenced_provenance(completed)
    result = completed.get("result")
    _require(isinstance(result, dict), "Backend E2E job lacks scientific result")
    serialized_result = json.dumps(result, sort_keys=True)
    for property_name in ("energy", "forces", "hessian"):
        _require(
            property_name in serialized_result,
            f"Backend E2E result lacks {property_name}",
        )

    artifacts = completed.get("artifacts")
    _require(isinstance(artifacts, list) and artifacts, "completed job has no artifacts")
    artifact = artifacts[0]
    _require(
        isinstance(artifact, dict)
        and isinstance(artifact.get("artifact_id"), str)
        and isinstance(artifact.get("sha256"), str),
        "completed job artifact descriptor is invalid",
    )
    artifact_status, artifact_bytes, _artifact_headers = _http(
        "GET",
        f"{base_url}/jobs/{completed_id}/artifacts/{artifact['artifact_id']}",
    )
    _require(artifact_status == 200, "Backend artifact download failed")
    artifact_sha256 = _sha256_bytes(artifact_bytes)
    _require(
        artifact_sha256 == f"sha256:{artifact['sha256']}",
        "Backend artifact download differs from the journal manifest",
    )
    bundle_status, bundle_bytes, _bundle_headers = _http(
        "GET",
        f"{base_url}/jobs/{completed_id}/bundle",
    )
    _require(bundle_status == 200 and bundle_bytes[:2] == b"PK", "artifact bundle is invalid")

    cancelled = _json_response(
        "POST",
        f"{base_url}/jobs",
        payload={
            "input": {
                "smiles": "CCCCCCCC",
                "net_charge": 0,
                "multiplicity": 1,
                "psmiles_mode": None,
            },
            "model": "aimnet2",
            "conformer": {"seed": 1, "max_iterations": 500},
            "calculation_type": "optimization",
            "optimization": {
                "fmax_eV_per_A": 0.001,
                "max_steps": 50,
                "post_optimization_properties": ["hessian"],
            },
        },
        headers={"Idempotency-Key": f"gpu-accept-cancel-{uuid.uuid4().hex}"},
        expected={202},
    )
    cancelled_id = str(cancelled.get("job_id") or "")
    _require(bool(cancelled_id), "Backend did not return a cancellation-job ID")
    _wait_job_running(base_url, cancelled_id, timeout_seconds)
    _json_response("POST", f"{base_url}/jobs/{cancelled_id}/cancel")
    cancelled = _wait_job(base_url, cancelled_id, timeout_seconds)
    _require(cancelled.get("status") == "cancelled", "Backend cancellation was not durable")

    completed_journal = _journal_path(job_root, completed_id)
    cancelled_journal = _journal_path(job_root, cancelled_id)
    provenance = completed.get("provenance")
    assert isinstance(provenance, dict)
    return {
        "status": "passed",
        "transport": "broker+uds+backend",
        "gpu_indices": [gpu_index],
        "overflow_test_status": "pending",
        "completed_job_id": completed_id,
        "cancelled_job_id": cancelled_id,
        "submit": True,
        "poll": True,
        "cancel": True,
        "journal": True,
        "artifact": True,
        "bundle": True,
        "fencing": True,
        "completed_journal_sha256": _sha256_file(completed_journal),
        "cancelled_journal_sha256": _sha256_file(cancelled_journal),
        "artifact_sha256": artifact_sha256,
        "bundle_sha256": _sha256_bytes(bundle_bytes),
        "provenance_sha256": acceptance_contract.canonical_json_digest(provenance),
        "_gpu_uuid": gpu_uuid,
    }


def _leased_child(spec_path: Path, output_path: Path) -> int:
    try:
        spec = _load_json(spec_path, "leased direct specification")
        report = smoke_runtime.run_calculations(
            {"default_model_path": spec["default_model_path"]}
        )
        report["gpu_index"] = spec["gpu_index"]
        report["gpu_uuid"] = spec["gpu_uuid"]
        _write_private_json(output_path, report)
        return 0
    except Exception as exc:  # noqa: BLE001 - isolated execution boundary
        with contextlib.suppress(Exception):
            _write_private_json(
                output_path,
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
        return 2


def _cleanup_failed_direct_process(
    process: subprocess.Popen[bytes],
    *,
    registered: bool,
    managed: Any,
) -> None:
    """Collect a failed gated child without releasing live GPU capacity."""

    if process.poll() is not None:
        process.wait()
        return
    if registered:
        try:
            managed.prepare_process_termination()
        except Exception as exc:
            # ManagedGpuLease fail-closes and abandons the reservation on an
            # unproven MPS termination.  Never send a host signal afterward.
            raise AcceptanceHarnessError(
                "direct GPU cleanup was not proven; scope remains fail-closed"
            ) from exc
    try:
        process.wait(timeout=10.0)
        return
    except subprocess.TimeoutExpired:
        pass
    # Before registration the outer exec gate was never opened, so no target
    # interpreter or CUDA code can exist.  After a successful Broker
    # prepare_process_termination, signalling is explicitly safe.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, 15)
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, 9)
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired as exc:
            raise AcceptanceHarnessError(
                "direct GPU scope could not be collected"
            ) from exc


def run_leased_direct(
    *,
    resolved: dict[str, str],
    default_model_path: str,
    gpu_index: str,
    placement: str,
    run_directory: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from gpu_resource import (
        GpuBrokerClient,
        GpuBrokerClientError,
        mps_client_environment,
        transient_scope_command,
        wait_for_scope_membership,
    )

    broker = GpuBrokerClient(resolved["MONOMER_DFT_GPU_BROKER_UDS"])
    request_id = f"dft-acceptance-{gpu_index}-{uuid.uuid4().hex}"
    try:
        managed = broker.acquire_managed(
            kind="execution",
            placement=placement,
            component="dft",
            environment="dev",
            client_id=f"dft-acceptance-gpu{gpu_index}",
            memory_mib=4096,
            thread_percent=50,
            wait_timeout_seconds=0.0,
            heartbeat_interval_seconds=2.0,
            request_id=request_id,
        )
    except GpuBrokerClientError as exc:
        raise AcceptanceHarnessError(
            f"Broker did not admit direct GPU{gpu_index}: {exc.code}"
        ) from exc

    spec_path = run_directory / f"direct-gpu{gpu_index}-spec.json"
    output_path = run_directory / f"direct-gpu{gpu_index}-result.json"
    _write_private_json(
        spec_path,
        {
            "default_model_path": default_model_path,
            "gpu_index": int(gpu_index),
            "gpu_uuid": GPU_UUIDS[gpu_index],
        },
    )
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    os.set_inheritable(read_fd, True)
    child_environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "CUDA_VISIBLE_DEVICES"}
    }
    child_environment.update(
        mps_client_environment(
            managed.lease,
            pipe_root=Path(resolved["MONOMER_DFT_GPU_MPS_PIPE_ROOT"]),
        )
    )
    child_environment["PYTHONNOUSERSITE"] = "1"
    child_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    exec_gate = REPO_ROOT / "gpu_resource/exec_gate.py"
    _require(
        exec_gate.is_file() and not exec_gate.is_symlink(),
        "audited GPU execution gate is unavailable",
    )
    child_environment["NEXPOLY_GPU_EXEC_GATE_FD"] = str(read_fd)
    target_command = (
        resolved["MONOMER_DFT_PYTHON"],
        "-I",
        str(Path(__file__).absolute()),
        "--leased-child",
        "--spec",
        str(spec_path),
        "--output",
        str(output_path),
    )
    gated_command = (
        sys.executable,
        "-I",
        "-S",
        str(exec_gate),
        "--",
        *target_command,
    )
    scoped_command = transient_scope_command(
        managed.lease.lease_id,
        gated_command,
    )
    process: subprocess.Popen[bytes] | None = None
    registered_workload = False
    try:
        process = subprocess.Popen(
            scoped_command,
            cwd=REPO_ROOT,
            env=child_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(read_fd,),
            start_new_session=True,
        )
        os.close(read_fd)
        read_fd = -1
        process_start_ticks = wait_for_scope_membership(
            process.pid,
            managed.lease.lease_id,
        )
        registered = managed.register_workload(process.pid)
        registered_workload = True
        _require(
            str(registered.gpu_index) == gpu_index
            and registered.gpu_uuid == GPU_UUIDS[gpu_index],
            "Broker registration changed direct GPU identity",
        )
        os.write(write_fd, b"1")
        os.close(write_fd)
        write_fd = -1
        stdout, stderr = process.communicate(timeout=600.0)
        _require(
            process.returncode == 0,
            "direct AIMNet calculation failed: "
            + stderr.decode("utf-8", errors="replace")[-1000:],
        )
        _require(not stdout, "leased direct child wrote unexpected stdout")
        confirmed = managed.confirm_current()
        _require(
            confirmed.lease_id == registered.lease_id
            and confirmed.fencing_token == registered.fencing_token
            and confirmed.gpu_uuid == registered.gpu_uuid,
            "direct result fencing identity changed",
        )
        report = _load_json(output_path, "direct AIMNet result")
        _require(report.get("status") == "ok", "direct AIMNet result did not pass")
        return report, {
            "lease_id": registered.lease_id,
            "fencing_token": registered.fencing_token,
            "gpu_index": int(gpu_index),
            "gpu_uuid": registered.gpu_uuid,
            "request_id": request_id,
            "process_start_ticks": process_start_ticks,
            "report_sha256": _sha256_file(output_path),
        }
    except BaseException as exc:
        if write_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(write_fd)
            write_fd = -1
        if process is not None:
            _cleanup_failed_direct_process(
                process,
                registered=registered_workload,
                managed=managed,
            )
        if isinstance(exc, subprocess.TimeoutExpired):
            raise AcceptanceHarnessError(
                "direct AIMNet calculation timed out"
            ) from exc
        raise
    finally:
        for descriptor in (read_fd, write_fd):
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
        managed.close()


def _docker_gpu3_claim() -> dict[str, Any] | None:
    container_ids = _run("docker", "ps", "-q", timeout=10.0).stdout.split()
    if not container_ids:
        return None
    inspection = _run(
        "docker",
        "inspect",
        *container_ids,
        timeout=30.0,
    ).stdout
    try:
        containers = json.loads(inspection)
    except json.JSONDecodeError as exc:
        raise AcceptanceHarnessError("Docker claim inventory is invalid") from exc
    _require(isinstance(containers, list), "Docker claim inventory is not an array")
    matches: list[dict[str, Any]] = []
    for container in containers:
        if not isinstance(container, dict):
            continue
        full_id = container.get("Id")
        name = str(container.get("Name") or "").lstrip("/")
        requests = container.get("HostConfig", {}).get("DeviceRequests") or []
        if not isinstance(requests, list):
            raise AcceptanceHarnessError("Docker DeviceRequest inventory is invalid")
        for request in requests:
            if not isinstance(request, dict):
                continue
            capabilities = request.get("Capabilities") or []
            is_gpu = request.get("Driver") == "nvidia" or any(
                isinstance(group, list) and "gpu" in group
                for group in capabilities
            )
            if not is_gpu:
                continue
            raw_devices = request.get("DeviceIDs")
            _require(
                raw_devices is None or isinstance(raw_devices, list),
                "Docker GPU DeviceIDs inventory is invalid",
            )
            devices = [str(item) for item in (raw_devices or [])]
            claims_all = not devices and request.get("Count") == -1
            if not devices and not claims_all:
                raise AcceptanceHarnessError(
                    "Docker GPU DeviceRequest does not identify exact devices"
                )
            claims_gpu3 = claims_all or any(
                item in {"all", "3", GPU_UUIDS["3"]} for item in devices
            )
            if claims_gpu3:
                matches.append(
                    {
                        "kind": "docker",
                        "container_id": full_id,
                        "container_name": name,
                        "device_request_sha256": (
                            acceptance_contract.canonical_json_digest(request)
                        ),
                    }
                )
    _require(len(matches) <= 1, "GPU3 has multiple foreign Docker claims")
    return matches[0] if matches else None


def _prove_gpu3_rejection(
    *,
    resolved: dict[str, str],
    claim: dict[str, Any],
) -> dict[str, Any]:
    from gpu_resource import GpuBrokerClient, GpuBrokerClientError

    broker = GpuBrokerClient(resolved["MONOMER_DFT_GPU_BROKER_UDS"])
    request_id = f"dft-acceptance-gpu3-reject-{uuid.uuid4().hex}"
    try:
        managed = broker.acquire_managed(
            kind="execution",
            placement="overflow",
            component="dft",
            environment="dev",
            client_id="dft-acceptance-gpu3-rejection",
            memory_mib=4096,
            thread_percent=50,
            wait_timeout_seconds=0.0,
            heartbeat_interval_seconds=2.0,
            request_id=request_id,
        )
    except GpuBrokerClientError as exc:
        _require(
            exc.code == "gpu_capacity_unavailable",
            f"GPU3 Broker rejection used unexpected code: {exc.code}",
        )
        rejection = {
            "code": exc.code,
            "gpu_index": 3,
            "gpu_uuid": GPU_UUIDS["3"],
            "placement": "overflow",
            "request_id": request_id,
            "claim": claim,
        }
        return {
            "code": exc.code,
            "gpu_index": 3,
            "gpu_uuid": GPU_UUIDS["3"],
            "placement": "overflow",
            "broker_report_sha256": (
                acceptance_contract.canonical_json_digest(rejection)
            ),
        }
    else:
        managed.close()
        raise AcceptanceHarnessError(
            "Broker admitted GPU3 despite the governed foreign Docker claim"
        )


def _runtime_evidence(preflight_result: dict[str, Any]) -> dict[str, Any]:
    source = preflight_result["source"]
    worker_runtime = preflight_result["runtime"]
    return {
        "contract_sha256": runtime_contract.RUNTIME_CONTRACT_SHA256,
        "python_version": worker_runtime["python"],
        "uv_version": worker_runtime["uv"],
        "build_lock_sha256": runtime_contract.RUNTIME_CONTRACT[
            "build_lock_sha256"
        ],
        "source": {
            "commit": source["commit"],
            "tree": source["tree"],
            "archive_sha256": f"sha256:{source['archive_inventory_sha256']}",
        },
        "wheel": {
            key: runtime_contract.RUNTIME_CONTRACT["wheel"][key]
            for key in (
                "filename",
                "sha256",
                "inventory_sha256",
                "record_sha256",
            )
        },
        "model_registry_sha256": runtime_contract.RUNTIME_CONTRACT[
            "registry_sha256"
        ],
        "models_sha256": runtime_contract.RUNTIME_CONTRACT["models_sha256"],
    }


def _direct_coverage(
    report: dict[str, Any],
    lease_evidence: dict[str, Any],
) -> dict[str, Any]:
    water = report.get("water")
    _require(isinstance(water, dict), "direct AIMNet water result is missing")
    _require(
        water.get("forces_shape") == [3, 3]
        and water.get("hessian_shape") == [3, 3, 3, 3],
        "direct AIMNet force/Hessian shapes differ",
    )
    return {
        "status": "passed",
        "gpu_index": 1,
        "gpu_uuid": GPU_UUIDS["1"],
        "properties": ["energy", "forces", "hessian"],
        "energy_eV": water["energy_eV"],
        "max_force_eV_per_A": water["max_force_eV_per_A"],
        "hessian_symmetry_max_abs_eV_per_A2": (
            water["hessian_symmetry_max_abs_eV_per_A2"]
        ),
        "report_sha256": lease_evidence["report_sha256"],
    }


def _stack_running() -> bool:
    result = _run(
        str(SCRIPT_ROOT / "monomer_dft_worker_ctl.sh"),
        "status",
        timeout=15.0,
        check=False,
    )
    return result.returncode == 0


def _stack_command(command: str, timeout: float) -> None:
    environment = {
        key: value for key, value in os.environ.items() if key != "PYTHONPATH"
    }
    result = _run(
        str(SCRIPT_ROOT / "monomer_dft_dev_stack.sh"),
        command,
        timeout=timeout,
        check=False,
        env=environment,
    )
    if result.returncode != 0:
        raise AcceptanceHarnessError(
            f"development DFT stack {command} failed: {result.stderr[-2000:]}"
        )


def run_acceptance(args: argparse.Namespace) -> Path:
    validate_git_authority(args.authority_sha, args.authority_tree)
    runtime_root = _safe_runtime_root()
    images = _load_json(args.images, "authority OCI image evidence")
    _require(set(images) == {"backend", "web"}, "authority OCI images must be backend/web")
    production_repo_metadata = PRODUCTION_REPO_ROOT.lstat()
    production_repo_identity = (
        production_repo_metadata.st_dev,
        production_repo_metadata.st_ino,
        production_repo_metadata.st_mtime_ns,
    )
    before_gpu2 = snapshot_gpu2()
    stack_was_running = _stack_running()
    if args.stack_mode == "existing":
        _require(stack_was_running, "existing stack mode requires a running Worker")

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    run_directory = runtime_root / "runs" / f"gpu-acceptance-{stamp}-{os.getpid()}"
    run_directory.mkdir(mode=0o700)
    stack_started_here = False
    report: dict[str, Any] | None = None
    try:
        if args.stack_mode == "manage" and not stack_was_running:
            _stack_command("start", args.stack_timeout)
            stack_started_here = True
        elif args.stack_mode == "manage":
            _stack_command("start", args.stack_timeout)

        preflight_result = smoke_runtime.prepare_runtime(REPO_ROOT)
        _require(
            preflight_result["broker_enabled"] is True,
            "real acceptance requires the governed Host Broker",
        )
        direct, direct_lease = run_leased_direct(
            resolved={
                **preflight.load_env_file(REPO_ROOT / ".env.monomer-dft.dev"),
                "MONOMER_DFT_PYTHON": sys.executable,
                "MONOMER_DFT_GPU_BROKER_UDS": str(
                    preflight_result["worker_uds"]
                ).replace(
                    "monomer-dft-worker-socket/worker.sock",
                    "gpu-resource/broker.sock",
                ),
                "MONOMER_DFT_GPU_MPS_PIPE_ROOT": str(
                    runtime_root / "gpu-resource"
                ),
            },
            default_model_path=preflight_result["default_model_path"],
            gpu_index="1",
            placement="preferred",
            run_directory=run_directory,
        )
        e2e = run_backend_e2e(
            base_url=args.backend_url.rstrip("/"),
            job_root=runtime_root / "monomer-dft-worker-runs",
            timeout_seconds=args.job_timeout,
        )
        _require(
            e2e["gpu_indices"] == [1]
            and e2e.pop("_gpu_uuid") == GPU_UUIDS["1"],
            "Backend E2E did not use the exact governed GPU1 path",
        )

        claim = _docker_gpu3_claim()
        requested_mode = args.gpu3_mode
        if requested_mode == "externally_fenced":
            _require(claim is not None, "GPU3 externally_fenced mode lacks a Docker claim")
        if requested_mode == "actual":
            _require(claim is None, "GPU3 actual mode conflicts with a Docker claim")
        if claim is not None:
            reservations_path = (
                REPO_ROOT / "ops/config/gpu-external-reservations.json"
            )
            reservations = _load_json(
                reservations_path,
                "GPU external reservation policy",
            )
            blocked_reason = (
                reservations.get("blocked_gpu_uuids", {}).get(GPU_UUIDS["3"])
                if isinstance(reservations.get("blocked_gpu_uuids"), dict)
                else None
            )
            _require(
                blocked_reason == acceptance_contract.GPU3_BLOCKED_REASON,
                "GPU3 foreign claim is not bound to the governed blocked reason",
            )
            rejection = _prove_gpu3_rejection(
                resolved={
                    "MONOMER_DFT_GPU_BROKER_UDS": str(
                        runtime_root / "gpu-resource/broker.sock"
                    )
                },
                claim=claim,
            )
            gpu3_identity = {
                "index": 3,
                "uuid": GPU_UUIDS["3"],
                "mode": "externally_fenced",
                "cuda_started": False,
                "fencing_verified": True,
                "evidence_sha256": acceptance_contract.canonical_json_digest(
                    {
                        "claim": claim,
                        "rejection": rejection,
                        "blocked_reason": blocked_reason,
                    }
                ),
                "reservations_sha256": (
                    _sha256_file(reservations_path)
                ),
                "blocked_reason": blocked_reason,
                "claim": claim,
                "rejection": rejection,
            }
            e2e["gpu_indices"] = [1]
            e2e["overflow_test_status"] = "externally_fenced"
        else:
            gpu3_direct, gpu3_lease = run_leased_direct(
                resolved={
                    "MONOMER_DFT_PYTHON": sys.executable,
                    "MONOMER_DFT_GPU_BROKER_UDS": str(
                        runtime_root / "gpu-resource/broker.sock"
                    ),
                    "MONOMER_DFT_GPU_MPS_PIPE_ROOT": str(
                        runtime_root / "gpu-resource"
                    ),
                },
                default_model_path=preflight_result["default_model_path"],
                gpu_index="3",
                placement="overflow",
                run_directory=run_directory,
            )
            gpu3_identity = {
                "index": 3,
                "uuid": GPU_UUIDS["3"],
                "mode": "actual",
                "cuda_started": True,
                "fencing_verified": True,
                "evidence_sha256": acceptance_contract.canonical_json_digest(
                    {"result": gpu3_direct, "lease": gpu3_lease}
                ),
            }
            # This is an exact Broker-leased overflow calculation, not a
            # Backend/UDS job. Keep the Backend provenance truthful.
            e2e["gpu_indices"] = [1]
            e2e["overflow_test_status"] = "passed"

        if stack_started_here:
            _stack_command("stop", args.stack_timeout)
            stack_started_here = False
        after_gpu2 = snapshot_gpu2()
        _require(before_gpu2 == after_gpu2, "physical GPU2 changed during acceptance")
        current_production_metadata = PRODUCTION_REPO_ROOT.lstat()
        _require(
            (
                current_production_metadata.st_dev,
                current_production_metadata.st_ino,
                current_production_metadata.st_mtime_ns,
            )
            == production_repo_identity,
            "production repository metadata changed during development acceptance",
        )

        report = acceptance_contract.seal_report(
            {
                "schema_version": 1,
                "status": "passed",
                "captured_at": dt.datetime.now(dt.UTC)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
                "authority": {
                    "sha": args.authority_sha,
                    "tree": args.authority_tree,
                },
                "bridge": {
                    "sha": args.bridge_sha,
                    "tree": args.bridge_tree,
                },
                "images": images,
                "runtime": _runtime_evidence(preflight_result),
                "coverage": {
                    "direct_science": _direct_coverage(direct, direct_lease),
                    "broker_uds_backend_e2e": e2e,
                },
                "gpus": {
                    "1": {
                        "index": 1,
                        "uuid": GPU_UUIDS["1"],
                        "mode": "actual",
                        "cuda_started": True,
                        "fencing_verified": True,
                        "evidence_sha256": (
                            acceptance_contract.canonical_json_digest(
                                {"result": direct, "lease": direct_lease}
                            )
                        ),
                    },
                    "2": {
                        "index": 2,
                        "uuid": GPU_UUIDS["2"],
                        "mode": "unchanged",
                        "cuda_started": False,
                        "before": before_gpu2,
                        "after": after_gpu2,
                        "processes_unchanged": True,
                        "memory_unchanged": True,
                    },
                    "3": gpu3_identity,
                },
            }
        )
        acceptance_contract.validate_report(
            report,
            authority={"sha": args.authority_sha, "tree": args.authority_tree},
            bridge={"sha": args.bridge_sha, "tree": args.bridge_tree},
            authority_images=images,
            runtime_contract=runtime_contract.RUNTIME_CONTRACT,
            runtime_contract_sha256=runtime_contract.RUNTIME_CONTRACT_SHA256,
        )
        output = run_directory / "gpu-acceptance-report.json"
        _write_private_json(output, report)
        return output
    finally:
        if stack_started_here:
            with contextlib.suppress(Exception):
                _stack_command("stop", args.stack_timeout)
        if report is None:
            with contextlib.suppress(Exception):
                _write_private_json(
                    run_directory / "gpu-acceptance-failure.json",
                    {
                        "schema_version": 1,
                        "status": "failed",
                        "captured_at": dt.datetime.now(dt.UTC)
                        .replace(microsecond=0)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    },
                )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authority-sha")
    parser.add_argument("--authority-tree")
    parser.add_argument("--bridge-sha")
    parser.add_argument("--bridge-tree")
    parser.add_argument("--images", type=Path)
    parser.add_argument(
        "--gpu3-mode",
        choices=("auto", "actual", "externally_fenced"),
        default="auto",
    )
    parser.add_argument(
        "--stack-mode",
        choices=("manage", "existing"),
        default="manage",
    )
    parser.add_argument(
        "--backend-url",
        default="http://127.0.0.1:28000/api/v1/monomer-dft",
    )
    parser.add_argument("--job-timeout", type=float, default=600.0)
    parser.add_argument("--stack-timeout", type=float, default=1200.0)
    parser.add_argument("--leased-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--spec", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.leased_child:
        if args.spec is None or args.output is None:
            return 2
        return _leased_child(args.spec, args.output)
    required = (
        args.authority_sha,
        args.authority_tree,
        args.bridge_sha,
        args.bridge_tree,
        args.images,
    )
    if any(item is None for item in required):
        print(
            "authority SHA/tree, bridge SHA/tree and --images are required",
            file=sys.stderr,
        )
        return 2
    try:
        output = run_acceptance(args)
    except Exception as exc:  # noqa: BLE001 - live acceptance boundary
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"status": "ok", "report": str(output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

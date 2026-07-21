#!/usr/bin/env python3
"""Own the opt-in, non-exclusive development GPU1 Broker/MPS session."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    # Derived only from this script's resolved location; never trust PYTHONPATH.
    sys.path.insert(0, str(REPO_ROOT))
RUNTIME_ROOT = REPO_ROOT / ".runtime"
SESSION_ROOT = RUNTIME_ROOT / "gpu-session"
RUNS_ROOT = SESSION_ROOT / "runs"
CONTROLLER_RECORD = SESSION_ROOT / "controller.json"
GPU_ROOT = RUNTIME_ROOT / "gpu-resource"
RESERVATIONS_SOURCE = REPO_ROOT / "ops/config/gpu-external-reservations.json"
POLICY_FILE = REPO_ROOT / "ops/config/gpu-broker-policy.json"
MPS_CONTROL = REPO_ROOT / "scripts/gpu_mps_control.sh"
GPU_INDEX = 1
GPU_UUID = "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771"
GPU3_UUID = "GPU-0818ca6b-d9b6-af6a-71bf-afe3777ee3a5"
POLYPROP_CONTAINER = "polyprop-backend-gpu-1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class DevGpuSessionError(RuntimeError):
    """The session cannot safely proceed."""


@dataclass(frozen=True, slots=True)
class TargetSnapshot:
    process_pids: tuple[int, ...]
    docker_claims: tuple[Any, ...]
    systemd_claims: tuple[Any, ...]


def parse_mps_client_inventory(output: bytes) -> frozenset[int]:
    """Parse the private MPS `ps` response without trusting command text."""

    if len(output) > 1024 * 1024:
        raise DevGpuSessionError("MPS client inventory is oversized")
    if output in {b"", b"Server not found", b"Server not found\n"}:
        return frozenset()
    try:
        lines = output.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise DevGpuSessionError("MPS client inventory is invalid") from exc
    if (
        not lines
        or lines[0].split()
        != ["PID", "ID", "SERVER", "DEVICE", "NAMESPACE", "COMMAND"]
    ):
        raise DevGpuSessionError("MPS client inventory is invalid")
    result: set[int] = set()
    for line in lines[1:]:
        fields = line.split(maxsplit=5)
        if len(fields) != 6 or not all(field.isdigit() for field in fields[:5]):
            raise DevGpuSessionError("MPS client inventory row is invalid")
        pid = int(fields[0])
        if pid <= 0 or pid in result or not fields[5].strip():
            raise DevGpuSessionError("MPS client inventory identity is invalid")
        result.add(pid)
    return frozenset(result)


def process_start_ticks(pid: int, *, proc_root: Path = Path("/proc")) -> int:
    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="ascii")
    except OSError as exc:
        raise DevGpuSessionError("session process identity is unavailable") from exc
    close = raw.rfind(")")
    fields = raw[close + 2 :].split() if close >= 0 else []
    if len(fields) <= 19 or not fields[19].isdigit():
        raise DevGpuSessionError("session process start identity is invalid")
    return int(fields[19])


def process_argv(pid: int, *, proc_root: Path = Path("/proc")) -> tuple[str, ...]:
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
        result = tuple(item.decode("utf-8") for item in raw.split(b"\0") if item)
    except (OSError, UnicodeError) as exc:
        raise DevGpuSessionError("session command identity is unavailable") from exc
    if not result:
        raise DevGpuSessionError("session command identity is empty")
    return result


def _atomic_json(path: Path, value: dict[str, Any], *, replace: bool = True) -> None:
    if path.is_symlink() or (not replace and path.exists()):
        raise DevGpuSessionError(f"unsafe preexisting session record: {path}")
    descriptor, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_temporary)
    try:
        os.fchmod(descriptor, 0o600)
        payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        parent_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_private_json(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > 64 * 1024
        ):
            raise DevGpuSessionError(f"session record is unsafe: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DevGpuSessionError(f"session record is unavailable: {path}") from exc
    if not isinstance(value, dict):
        raise DevGpuSessionError(f"session record is invalid: {path}")
    return value


def _private_directory(path: Path, *, create: bool) -> None:
    if path.is_symlink():
        raise DevGpuSessionError(f"session directory is a symlink: {path}")
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DevGpuSessionError(f"session directory is unavailable: {path}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise DevGpuSessionError(f"session directory is not owner-private: {path}")


def collect_target_snapshot() -> TargetSnapshot:
    """Collect one fail-closed inventory while intentionally ignoring GPU3."""

    from ops.gpu_broker.server import (
        EXPECTED_GPU_UUIDS,
        query_compute_processes,
        query_docker_gpu_claims,
        query_gpu_inventory,
        query_systemd_gpu_claims,
    )

    if EXPECTED_GPU_UUIDS.get(GPU_INDEX) != GPU_UUID:
        raise DevGpuSessionError("compiled GPU1 identity changed")
    inventory = query_gpu_inventory()
    if inventory.get(GPU_INDEX) != GPU_UUID:
        raise DevGpuSessionError("physical GPU1 identity differs from policy")
    processes = query_compute_processes()
    docker_claims = tuple(
        claim for claim in query_docker_gpu_claims() if GPU_UUID in claim.gpu_uuids
    )
    systemd_claims = tuple(
        claim
        for claim in query_systemd_gpu_claims(compute_processes=processes)
        if GPU_UUID in claim.gpu_uuids
    )
    return TargetSnapshot(
        process_pids=tuple(sorted(processes.get(GPU_UUID, frozenset()))),
        docker_claims=docker_claims,
        systemd_claims=systemd_claims,
    )


def read_gpu3_guard_fingerprint(
    *, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
) -> dict[str, Any]:
    """Read immutable GPU3/PolyProp evidence without issuing any mutation."""

    reservation_sha256 = hashlib.sha256(RESERVATIONS_SOURCE.read_bytes()).hexdigest()
    gpu = run(
        (
            "nvidia-smi",
            "--query-gpu=uuid,compute_mode",
            "--format=csv,noheader,nounits",
            "-i",
            "3",
        ),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    rows = [row.strip() for row in gpu.stdout.splitlines() if row.strip()]
    if len(rows) != 1:
        raise DevGpuSessionError("GPU3 read-only identity returned an invalid inventory")
    fields = [field.strip() for field in rows[0].split(",")]
    if len(fields) != 2 or fields[0] != GPU3_UUID:
        raise DevGpuSessionError("GPU3 read-only identity differs from policy")
    inspected = run(
        ("docker", "container", "inspect", POLYPROP_CONTAINER),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    try:
        payload = json.loads(inspected.stdout)
    except json.JSONDecodeError as exc:
        raise DevGpuSessionError("PolyProp read-only identity is invalid") from exc
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise DevGpuSessionError("PolyProp read-only inventory is invalid")
    container = payload[0]
    if container.get("Name") not in {POLYPROP_CONTAINER, f"/{POLYPROP_CONTAINER}"}:
        raise DevGpuSessionError("PolyProp container identity differs")
    device_requests = container.get("HostConfig", {}).get("DeviceRequests") or []
    config = container.get("Config") or {}
    config_material = {
        "labels": config.get("Labels") or {},
        "environment": sorted(config.get("Env") or []),
        "command": config.get("Cmd"),
        "entrypoint": config.get("Entrypoint"),
    }
    return {
        "reservation_sha256": reservation_sha256,
        "gpu3_uuid": fields[0],
        "gpu3_compute_mode": fields[1],
        "container_id": container.get("Id"),
        "container_image": container.get("Image"),
        "container_restart_count": container.get("RestartCount"),
        "container_status": (container.get("State") or {}).get("Status"),
        "device_requests_sha256": hashlib.sha256(
            json.dumps(device_requests, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "config_sha256": hashlib.sha256(
            json.dumps(config_material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }

def _claim_fingerprint(claim: Any) -> tuple[Any, ...]:
    if hasattr(claim, "container_id"):
        return (
            "docker",
            claim.container_id,
            claim.init_pid,
            claim.started_at,
            claim.restart_count,
            claim.registration_id,
            claim.component,
            claim.environment,
            claim.compose_project,
            claim.compose_service,
            tuple(sorted(claim.gpu_uuids)),
        )
    return (
        "systemd",
        claim.scope,
        claim.unit,
        claim.main_pid,
        claim.control_group,
        tuple(sorted(claim.process_pids)),
        tuple(sorted(claim.gpu_uuids)),
        tuple(sorted(claim.static_gpu_uuids)),
        tuple(sorted(claim.active_gpu_uuids)),
    )


def snapshot_fingerprint(snapshot: TargetSnapshot) -> tuple[Any, ...]:
    return (
        snapshot.process_pids,
        tuple(_claim_fingerprint(claim) for claim in snapshot.docker_claims),
        tuple(_claim_fingerprint(claim) for claim in snapshot.systemd_claims),
    )


def require_double_free_audit(
    collector: Callable[[], TargetSnapshot] = collect_target_snapshot,
    *,
    pause: Callable[[float], None] = time.sleep,
    interval_seconds: float = 1.0,
) -> tuple[TargetSnapshot, TargetSnapshot]:
    first = collector()
    pause(interval_seconds)
    second = collector()
    for snapshot in (first, second):
        if snapshot.process_pids or snapshot.docker_claims or snapshot.systemd_claims:
            raise DevGpuSessionError("GPU1 is busy; opportunistic session remains unavailable")
    if snapshot_fingerprint(first) != snapshot_fingerprint(second):
        raise DevGpuSessionError("GPU1 authority changed across startup double audit")
    return first, second


def foreign_gpu1_reasons(
    snapshot: TargetSnapshot,
    *,
    authorized_mps_pids: frozenset[int],
    managed_workload_pids: frozenset[int] = frozenset(),
) -> tuple[str, ...]:
    """Return contamination evidence; GPU3 never participates in this snapshot."""

    reasons: list[str] = []
    allowed_processes = authorized_mps_pids | managed_workload_pids
    foreign_processes = set(snapshot.process_pids) - allowed_processes
    if foreign_processes:
        reasons.append("foreign CUDA PID(s): " + ",".join(map(str, sorted(foreign_processes))))
    for claim in snapshot.docker_claims:
        if not (
            claim.registration_id == "backend-dev"
            and claim.component == "backend"
            and claim.environment == "dev"
            and claim.compose_project == "nexpoly_dev"
            and claim.compose_service == "backend"
            and claim.gpu_uuids == frozenset({GPU_UUID})
        ):
            reasons.append(f"foreign Docker claim: {claim.container_id[:12]}")
    for claim in snapshot.systemd_claims:
        if not claim.process_pids or not claim.process_pids <= allowed_processes:
            reasons.append(f"foreign systemd claim: {claim.scope}:{claim.unit}")
    return tuple(reasons)


def drain_on_contamination(
    snapshot: TargetSnapshot,
    *,
    authorized_mps_pids: frozenset[int],
    broker_client: Any,
    managed_workload_pids: frozenset[int] = frozenset(),
) -> tuple[str, ...]:
    """Drain only NexPoly admission. This function never signals any process."""

    reasons = foreign_gpu1_reasons(
        snapshot,
        authorized_mps_pids=authorized_mps_pids,
        managed_workload_pids=managed_workload_pids,
    )
    if reasons:
        broker_client.set_draining(True)
    return reasons


def broker_authority_token(status: dict[str, Any]) -> tuple[Any, ...]:
    """Identity relevant to matching a host snapshot to Broker ownership."""

    leases = status.get("leases")
    if not isinstance(leases, list):
        raise DevGpuSessionError("Broker returned an invalid lease inventory")
    normalized: list[tuple[Any, ...]] = []
    for lease in leases:
        if not isinstance(lease, dict):
            raise DevGpuSessionError("Broker returned an invalid lease record")
        normalized.append(
            tuple(
                lease.get(key)
                for key in (
                    "lease_id",
                    "fencing_token",
                    "gpu_uuid",
                    "owner_pid",
                    "workload_pid",
                    "status",
                )
            )
        )
    return (
        status.get("broker_instance_id"),
        status.get("draining"),
        tuple(sorted(normalized, key=repr)),
    )


def consistent_broker_snapshot(
    client: Any,
    collector: Callable[[], TargetSnapshot] = collect_target_snapshot,
    *,
    attempts: int = 3,
) -> tuple[dict[str, Any], TargetSnapshot]:
    """Bind an expensive host inventory between two stable Broker reads."""

    if attempts < 1:
        raise ValueError("attempts must be positive")
    for _ in range(attempts):
        before = client.status()
        snapshot = collector()
        after = client.status()
        if broker_authority_token(before) == broker_authority_token(after):
            return after, snapshot
    raise DevGpuSessionError("Broker authority changed throughout the host audit")


def dry_run_plan(action: str) -> dict[str, Any]:
    steps = {
        "up": [
            "validate owner-private worktree runtime roots",
            "perform two independent fail-closed GPU1 audits",
            "copy the exact external-reservation policy through a 0600 descriptor",
            "start a descriptor-bound Broker and GPU1-only MPS in Default mode",
            "re-audit GPU1 and start the contamination watchdog",
            "start only NexPoly session workers/backend through dev_server_gpu.sh",
        ],
        "down": [
            "drain Broker admission",
            "stop only NexPoly backend and host workers",
            "require zero Broker leases",
            "quit only the descriptor-owned GPU1 MPS server",
            "stop only the exact Broker/controller process identities",
            "restore the CPU-only development backend",
        ],
    }[action]
    return {
        "schema_version": 1,
        "action": action,
        "dry_run": True,
        "gpu_index": GPU_INDEX,
        "gpu_uuid": GPU_UUID,
        "compute_mode": "Default",
        "exclusive": False,
        "gpu3_untouched": True,
        "foreign_policy": "fail-closed-and-drain-nexpoly-only",
        "steps": steps,
    }


class SessionController:
    def __init__(self, run_directory: Path, source_sha: str, source_tree: str) -> None:
        self.run_directory = run_directory
        self.source_sha = source_sha
        self.source_tree = source_tree
        self.status_file = run_directory / "status.json"
        self.session_id = run_directory.name.rsplit("-", 1)[-1]
        if re.fullmatch(r"[0-9a-f]{32}", self.session_id) is None:
            raise DevGpuSessionError("run directory lacks an exact session identity")
        self.broker_state = run_directory / "broker-state.json"
        self.broker_log_path = run_directory / "broker.log"
        self.session_policy_path = run_directory / "gpu-policy.json"
        self.root_fd = -1
        self.reservations_fd = -1
        self.policy_fd = -1
        self.slot_fd = -1
        self.pipe_fd = -1
        self.log_fd = -1
        self.broker: subprocess.Popen[bytes] | None = None
        self.broker_log: Any = None
        self.stop_requested = False
        self.activation_requested = False
        self.automatic_recovery = False
        self.plane_ready_published = False
        self.plane_cleaned = False
        self.owned_components_stopped = False
        self.cpu_restored = False
        self.mps_started = False
        self.audit_sequence = 0
        self.last_audit_duration = 0.0
        self.gpu3_guard: dict[str, Any] | None = None

    def _state(self, status: str, **extra: Any) -> None:
        _atomic_json(
            self.status_file,
            {
                "schema_version": 1,
                "status": status,
                "controller_pid": os.getpid(),
                "controller_start_ticks": process_start_ticks(os.getpid()),
                "source_sha": self.source_sha,
                "source_tree": self.source_tree,
                "session_id": self.session_id,
                "run_directory": str(self.run_directory),
                "gpu_index": GPU_INDEX,
                "gpu_uuid": GPU_UUID,
                "gpu3_untouched": True,
                "gpu3_guard": self.gpu3_guard,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                **extra,
            },
        )

    @staticmethod
    def _open_directory(parent_fd: int, name: str, *, create: bool) -> int:
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        metadata = os.fstat(descriptor)
        if (
            metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            os.close(descriptor)
            raise DevGpuSessionError("GPU descriptor directory is unsafe")
        return descriptor

    def _authority_path(self, descriptor: int) -> Path:
        return Path(f"/proc/{os.getpid()}/fd/{descriptor}")

    @staticmethod
    def _child_authority_path(descriptor: int) -> Path:
        if (
            isinstance(descriptor, bool)
            or not isinstance(descriptor, int)
            or descriptor <= 2
        ):
            raise DevGpuSessionError("child descriptor authority is invalid")
        return Path(f"/proc/self/fd/{descriptor}")

    def _prepare_descriptors(self) -> None:
        _private_directory(GPU_ROOT, create=True)
        self.root_fd = os.open(
            GPU_ROOT, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        for forbidden in ("broker.sock", "mps-1"):
            try:
                os.stat(forbidden, dir_fd=self.root_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise DevGpuSessionError(f"refusing preexisting GPU1 session state: {forbidden}")
        source = RESERVATIONS_SOURCE.read_bytes()
        try:
            policy = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DevGpuSessionError("repository GPU policy is unavailable") from exc
        expected_scalars = {
            "schema_version": 1,
            "gpu_total_budget_mib": 20736,
            "component_budgets_mib": {"backend": 8192, "dft": 4096, "md": 8192},
            "component_thread_percent": {"backend": 100, "dft": 50, "md": 50},
        }
        if any(policy.get(key) != value for key, value in expected_scalars.items()):
            raise DevGpuSessionError("repository GPU budget policy differs")
        if (policy.get("gpu_uuids") or {}).get("1") != GPU_UUID:
            raise DevGpuSessionError("repository GPU1 policy identity differs")
        device_policy = policy.get("device_policy")
        if not isinstance(device_policy, dict):
            raise DevGpuSessionError("repository GPU device policy is invalid")
        session_policy = json.loads(json.dumps(policy))
        for component in ("backend", "dft", "md"):
            session_policy["device_policy"][f"dev.{component}"] = [GPU_INDEX]
        _atomic_json(self.session_policy_path, session_policy, replace=False)
        self.policy_fd = os.open(
            self.session_policy_path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            self.reservations_fd = os.open(
                "external-reservations.json",
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=self.root_fd,
            )
        except FileNotFoundError:
            descriptor = os.open(
                "external-reservations.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=self.root_fd,
            )
            try:
                os.write(descriptor, source)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self.reservations_fd = os.open(
                "external-reservations.json",
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=self.root_fd,
            )
        metadata = os.fstat(self.reservations_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or os.pread(self.reservations_fd, len(source) + 1, 0) != source
        ):
            raise DevGpuSessionError("external reservation descriptor differs from exact policy")
        self.slot_fd = self._open_directory(self.root_fd, "mps-1", create=True)
        self.pipe_fd = self._open_directory(self.slot_fd, "pipe", create=True)
        self.log_fd = self._open_directory(self.slot_fd, "log", create=True)

    def _safe_env(self, **extra: str) -> dict[str, str]:
        environment = {
            "HOME": os.environ.get("HOME", str(RUNTIME_ROOT / "home")),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LOGNAME": os.environ.get("LOGNAME", "devuser"),
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(REPO_ROOT),
            "USER": os.environ.get("USER", "devuser"),
        }
        environment.update(extra)
        return environment

    def _start_broker(self) -> Any:
        root = self._child_authority_path(self.root_fd)
        reservations = self._child_authority_path(self.reservations_fd)
        descriptor = os.open(
            self.broker_log_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        self.broker_log = os.fdopen(descriptor, "ab", buffering=0)
        command = (
            sys.executable,
            "-E",
            "-s",
            "-B",
            "-m",
            "ops.gpu_broker.server",
            "--socket",
            str(root / "broker.sock"),
            "--state",
            str(self.broker_state),
            "--policy",
            str(self._child_authority_path(self.policy_fd)),
            "--external-reservations",
            str(reservations),
            "--mps-state-root",
            str(root),
        )
        self.broker = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=self._safe_env(NEXPOLY_GPU1_ONLY_SESSION="1"),
            stdin=subprocess.DEVNULL,
            stdout=self.broker_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            pass_fds=(self.root_fd, self.reservations_fd, self.policy_fd),
        )
        from gpu_resource import GpuBrokerClient

        client = GpuBrokerClient(GPU_ROOT / "broker.sock")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.broker.poll() is not None:
                break
            try:
                status = client.status()
                if status.get("leases") == [] and status.get("draining") is False:
                    return client
            except Exception:
                time.sleep(0.1)
        raise DevGpuSessionError("development GPU Broker failed to start empty")

    def _mps_env(self) -> dict[str, str]:
        root = self._authority_path(self.root_fd)
        return self._safe_env(
            NEXPOLY_GPU_STATE_ROOT=str(root),
            NEXPOLY_GPU_EXTERNAL_RESERVATIONS=str(
                self._authority_path(self.reservations_fd)
            ),
            NEXPOLY_GPU_BROKER_SOCKET=str(root / "broker.sock"),
            NEXPOLY_GPU_MPS_SLOT_DIRECTORY=str(self._authority_path(self.slot_fd)),
            NEXPOLY_GPU_MPS_PIPE_DIRECTORY=str(self._authority_path(self.pipe_fd)),
            NEXPOLY_GPU_MPS_LOG_DIRECTORY=str(self._authority_path(self.log_fd)),
            NEXPOLY_GPU_MPS_DESCRIPTOR_AUTHORITY="1",
            NEXPOLY_GPU_MPS_AUTHORITY_PID=str(os.getpid()),
            NEXPOLY_GPU_MPS_AUTHORITY_START_TICKS=str(process_start_ticks(os.getpid())),
            NEXPOLY_GPU_MPS_EXPECTED_ROOT=str(GPU_ROOT),
            NEXPOLY_GPU_MPS_REQUIRE_DEFAULT_MODE="1",
        )

    def _mps_command(self, action: str) -> None:
        completed = subprocess.run(
            (str(MPS_CONTROL), action, "1"),
            cwd=REPO_ROOT,
            env=self._mps_env(),
            pass_fds=(
                self.root_fd,
                self.reservations_fd,
                self.slot_fd,
                self.pipe_fd,
                self.log_fd,
            ),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        if completed.returncode != 0:
            raise DevGpuSessionError(
                f"GPU1 MPS {action} failed: {completed.stderr[-1000:]}"
            )

    def _authorized_mps(self) -> frozenset[int]:
        from ops.gpu_broker.server import MpsRuntimeGuard

        return MpsRuntimeGuard(self._authority_path(self.root_fd)).authorized_server_pids(
            GPU_INDEX, GPU_UUID
        )

    def _mps_client_pids(self) -> frozenset[int]:
        completed = subprocess.run(
            ("nvidia-cuda-mps-control",),
            input=b"ps\n",
            cwd=REPO_ROOT,
            env=self._mps_env(),
            pass_fds=(
                self.root_fd,
                self.reservations_fd,
                self.slot_fd,
                self.pipe_fd,
                self.log_fd,
            ),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        if completed.returncode != 0 or completed.stderr:
            raise DevGpuSessionError("MPS client inventory query failed")
        return parse_mps_client_inventory(completed.stdout)

    @staticmethod
    def _managed_workload_pids(status: dict[str, Any]) -> frozenset[int]:
        result: set[int] = set()
        for lease in status.get("leases", []):
            if not isinstance(lease, dict) or lease.get("gpu_uuid") != GPU_UUID:
                continue
            for key in ("owner_pid", "workload_pid"):
                value = lease.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    result.add(value)
        return frozenset(result)

    def _cleanup_owned_tree(self) -> None:
        for descriptor, kind in ((self.pipe_fd, "pipe"), (self.log_fd, "log")):
            for name in os.listdir(descriptor):
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                allowed_pipe = kind == "pipe" and name in {
                    "control", "control_lock", "control_privileged"
                }
                allowed_log = (
                    kind == "log"
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)
                    and stat.S_ISREG(metadata.st_mode)
                    and metadata.st_size <= 16 * 1024 * 1024
                )
                if metadata.st_uid != os.geteuid() or not (allowed_pipe or allowed_log):
                    raise DevGpuSessionError("owned MPS cleanup encountered foreign residue")
                os.unlink(name, dir_fd=descriptor)
        if os.listdir(self.pipe_fd) or os.listdir(self.log_fd):
            raise DevGpuSessionError("owned MPS residue survived cleanup")
        os.rmdir("pipe", dir_fd=self.slot_fd)
        os.rmdir("log", dir_fd=self.slot_fd)
        os.rmdir("mps-1", dir_fd=self.root_fd)

    def _cleanup(self, client: Any) -> bool:
        drained = client.set_draining(True)
        if any(
            isinstance(lease, dict) and lease.get("gpu_uuid") == GPU_UUID
            for lease in drained.get("leases", [])
        ):
            self._state("cleanup-blocked", reason="NexPoly GPU1 leases are still active")
            return False
        if self.mps_started:
            self._mps_command("stop")
            self.mps_started = False
        self._cleanup_owned_tree()
        assert self.broker is not None
        self.broker.terminate()
        try:
            self.broker.wait(timeout=15)
        except subprocess.TimeoutExpired as exc:
            raise DevGpuSessionError("owned Broker did not stop cleanly") from exc
        socket = GPU_ROOT / "broker.sock"
        if socket.exists() or socket.is_symlink():
            metadata = socket.lstat()
            if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise DevGpuSessionError("Broker socket residue is unsafe")
            socket.unlink()
        return True

    def _close_descriptors(self) -> None:
        if self.broker_log is not None:
            self.broker_log.close()
            self.broker_log = None
        for name in ("log_fd", "pipe_fd", "slot_fd", "policy_fd", "reservations_fd", "root_fd"):
            descriptor = getattr(self, name)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, name, -1)

    def _terminate_partial_broker(self) -> None:
        if self.broker is None or self.broker.poll() is not None:
            return
        self.broker.terminate()
        try:
            self.broker.wait(timeout=15)
        except subprocess.TimeoutExpired as exc:
            raise DevGpuSessionError("partial Broker did not stop cleanly") from exc

    def _remove_controller_record(self) -> None:
        try:
            record = _controller_record()
        except (DevGpuSessionError, OSError):
            return
        if record.get("pid") == os.getpid() and record.get("session_id") == self.session_id:
            CONTROLLER_RECORD.unlink()

    def _audit(self, client: Any) -> tuple[dict[str, Any], TargetSnapshot, tuple[str, ...]]:
        started = time.monotonic()
        status, snapshot = consistent_broker_snapshot(client)
        managed = self._managed_workload_pids(status)
        reasons = foreign_gpu1_reasons(
            snapshot,
            authorized_mps_pids=self._authorized_mps(),
            managed_workload_pids=managed,
        )
        unknown_mps_clients = self._mps_client_pids() - managed
        if unknown_mps_clients:
            reasons = (
                *reasons,
                "unknown private MPS client PID(s): "
                + ",".join(map(str, sorted(unknown_mps_clients))),
            )
        self.audit_sequence += 1
        self.last_audit_duration = time.monotonic() - started
        return status, snapshot, reasons

    def _recovery_command(self, command: str) -> bool:
        completed = subprocess.run(
            (str(REPO_ROOT / "scripts/dev_server_gpu.sh"), command),
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "NEXPOLY_DEV_GPU_SESSION_INTERNAL_RECOVERY": "1",
                "NEXPOLY_DEV_GPU_SESSION_ID": self.session_id,
            },
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
        )
        if completed.returncode != 0:
            self._state(
                "isolation-waiting",
                contaminated=True,
                recovery_command=command,
                recovery_error=(completed.stderr or completed.stdout)[-1000:],
            )
            return False
        return True

    def _serve_loop(self, client: Any) -> int:
        next_audit = time.monotonic()
        while True:
            if self.stop_requested:
                # On contamination, stop only the session-owned components and
                # restore the CPU backend *before* waiting for an unknown MPS
                # client to exit naturally.
                if self.automatic_recovery and not self.owned_components_stopped:
                    if not self._recovery_command("gpu-session-stop-owned-internal"):
                        time.sleep(1)
                        continue
                    self.owned_components_stopped = True
                if self.automatic_recovery and not self.cpu_restored:
                    if not self._recovery_command("gpu-session-restore-cpu-internal"):
                        time.sleep(1)
                        continue
                    self.cpu_restored = True
                try:
                    cleaned = self.plane_cleaned or self._cleanup(client)
                except (DevGpuSessionError, OSError, subprocess.SubprocessError) as exc:
                    # Never signal an unknown client.  Admission stays drained
                    # and the exact owned plane is retried after natural exit.
                    self._state(
                        "isolation-waiting",
                        contaminated=self.automatic_recovery,
                        owned_components_stopped=self.owned_components_stopped,
                        cpu_restored=self.cpu_restored,
                        recovery_error=str(exc),
                    )
                    time.sleep(1)
                    continue
                if not cleaned:
                    time.sleep(1)
                    continue
                self.plane_cleaned = True
                current_gpu3 = read_gpu3_guard_fingerprint()
                if current_gpu3 != self.gpu3_guard:
                    self._state(
                        "gpu3-drift",
                        contaminated=self.automatic_recovery,
                        gpu3_guard_after=current_gpu3,
                        recovery_error="GPU3/PolyProp read-only fingerprint changed",
                    )
                    self._remove_controller_record()
                    return 2
                self._state(
                    "recovered" if self.automatic_recovery else "stopped",
                    contaminated=self.automatic_recovery,
                    owned_components_stopped=self.owned_components_stopped,
                    cpu_restored=self.cpu_restored,
                )
                self._remove_controller_record()
                return 0

            try:
                _status, _snapshot, reasons = self._audit(client)
            except Exception as exc:
                # Inventory uncertainty is itself fail-closed, but never a
                # naked controller exit that would strand Broker/MPS state.
                try:
                    client.set_draining(True)
                except Exception:
                    pass
                self._state(
                    "audit-failed",
                    contaminated=True,
                    broker_draining=True,
                    recovery_error=str(exc),
                    audit_sequence=self.audit_sequence,
                )
                self.automatic_recovery = True
                self.stop_requested = True
                continue
            if reasons:
                client.set_draining(True)
                self._state(
                    "contaminated",
                    contaminated=True,
                    reasons=list(reasons),
                    broker_draining=True,
                    audit_sequence=self.audit_sequence,
                    audit_duration_seconds=round(self.last_audit_duration, 3),
                )
                self.automatic_recovery = True
                self.stop_requested = True
                continue
            phase = "ready" if self.activation_requested else "plane-ready"
            self._state(
                phase,
                authorized_mps_pids=sorted(self._authorized_mps()),
                contaminated=False,
                audit_sequence=self.audit_sequence,
                audit_duration_seconds=round(self.last_audit_duration, 3),
                audit_heartbeat_monotonic=time.monotonic(),
            )
            next_audit += 1.0
            delay = next_audit - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                # Slow host inventories run continuously with no extra sleep.
                next_audit = time.monotonic()

    def run(self, expected_argv: tuple[str, ...]) -> int:
        _private_directory(RUNTIME_ROOT, create=False)
        _private_directory(SESSION_ROOT, create=False)
        _private_directory(RUNS_ROOT, create=False)
        _private_directory(self.run_directory, create=False)
        if os.geteuid() != 1001 or os.getegid() != 1001:
            raise DevGpuSessionError("development GPU session requires uid/gid 1001")
        if process_argv(os.getpid()) != expected_argv:
            raise DevGpuSessionError("controller command identity differs")
        _atomic_json(
            CONTROLLER_RECORD,
            {
                "schema_version": 1,
                "pid": os.getpid(),
                "start_ticks": process_start_ticks(os.getpid()),
                "argv": list(expected_argv),
                "source_sha": self.source_sha,
                "source_tree": self.source_tree,
                "session_id": self.session_id,
                "run_directory": str(self.run_directory),
                "status_file": str(self.status_file),
            },
            replace=False,
        )
        signal.signal(signal.SIGTERM, lambda *_args: setattr(self, "stop_requested", True))
        signal.signal(signal.SIGINT, lambda *_args: setattr(self, "stop_requested", True))
        signal.signal(signal.SIGUSR1, lambda *_args: setattr(self, "activation_requested", True))
        client: Any | None = None
        try:
            self._state("auditing")
            self.gpu3_guard = read_gpu3_guard_fingerprint()
            self._state("auditing", gpu3_guard_captured=True)
            require_double_free_audit()
            self._prepare_descriptors()
            client = self._start_broker()
            self._mps_command("start")
            self.mps_started = True
            _status, _snapshot, reasons = self._audit(client)
            if reasons:
                client.set_draining(True)
                raise DevGpuSessionError(
                    "GPU1 changed during session startup: " + "; ".join(reasons)
                )
            # From this point the shell is allowed to start session-owned
            # components.  Any later unexpected controller failure must use
            # exact-session recovery even when activation was not yet sent.
            self.plane_ready_published = True
            self._state(
                "plane-ready",
                authorized_mps_pids=sorted(self._authorized_mps()),
                contaminated=False,
                audit_sequence=self.audit_sequence,
            )
            return self._serve_loop(client)
        except Exception as exc:
            try:
                self._state("startup-failed", contaminated=True, recovery_error=str(exc))
            except Exception:
                pass
            if client is not None:
                try:
                    client.set_draining(True)
                except Exception:
                    pass
                self.automatic_recovery = self.plane_ready_published
                self.stop_requested = True
                return self._serve_loop(client) or 2
            try:
                self._terminate_partial_broker()
                if min(self.slot_fd, self.pipe_fd, self.log_fd) >= 0:
                    self._cleanup_owned_tree()
            finally:
                self._remove_controller_record()
            raise
        finally:
            self._close_descriptors()


def _git_identity() -> tuple[str, str]:
    def run(argument: str) -> str:
        completed = subprocess.run(
            ("git", "--no-optional-locks", "-C", str(REPO_ROOT), "rev-parse", "--verify", argument),
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return completed.stdout.strip()

    source_sha, source_tree = run("HEAD"), run("HEAD^{tree}")
    if SHA_RE.fullmatch(source_sha) is None or SHA_RE.fullmatch(source_tree) is None:
        raise DevGpuSessionError("Git source identity is invalid")
    return source_sha, source_tree


def _controller_record() -> dict[str, Any]:
    record = _load_private_json(CONTROLLER_RECORD)
    expected_keys = {
        "schema_version", "pid", "start_ticks", "argv", "source_sha",
        "source_tree", "session_id", "run_directory", "status_file",
    }
    if set(record) != expected_keys or record.get("schema_version") != 1:
        raise DevGpuSessionError("controller record schema is invalid")
    pid = record.get("pid")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or process_start_ticks(pid) != record.get("start_ticks")
        or list(process_argv(pid)) != record.get("argv")
    ):
        raise DevGpuSessionError("controller PID/start/command identity changed")
    return record


def status() -> dict[str, Any]:
    if not CONTROLLER_RECORD.exists() and not CONTROLLER_RECORD.is_symlink():
        return {"schema_version": 1, "status": "stopped", "gpu_index": 1}
    record = _controller_record()
    state = _load_private_json(Path(record["status_file"]))
    if (
        state.get("controller_pid") != record["pid"]
        or state.get("controller_start_ticks") != record["start_ticks"]
        or state.get("source_sha") != record["source_sha"]
        or state.get("source_tree") != record["source_tree"]
        or state.get("session_id") != record["session_id"]
        or state.get("run_directory") != record["run_directory"]
        or state.get("gpu_index") != GPU_INDEX
        or state.get("gpu_uuid") != GPU_UUID
        or state.get("gpu3_untouched") is not True
    ):
        raise DevGpuSessionError("controller status identity differs")
    return state


def up_execute() -> dict[str, Any]:
    _private_directory(RUNTIME_ROOT, create=False)
    _private_directory(SESSION_ROOT, create=True)
    _private_directory(RUNS_ROOT, create=True)
    if CONTROLLER_RECORD.exists() or CONTROLLER_RECORD.is_symlink():
        raise DevGpuSessionError("GPU session controller record already exists")
    source_sha, source_tree = _git_identity()
    run_directory = RUNS_ROOT / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex
    )
    run_directory.mkdir(mode=0o700)
    log_path = run_directory / "controller.log"
    log_descriptor = os.open(
        log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    command = (
        sys.executable,
        str(Path(__file__).resolve()),
        "serve",
        "--execute",
        "--run-directory",
        str(run_directory),
        "--source-sha",
        source_sha,
        "--source-tree",
        source_tree,
    )
    with os.fdopen(log_descriptor, "ab", buffering=0) as log:
        process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise DevGpuSessionError("GPU session controller exited during startup")
        try:
            state = status()
        except DevGpuSessionError:
            time.sleep(0.1)
            continue
        if state.get("status") == "plane-ready":
            return state
        if state.get("status") in {
            "failed", "startup-failed", "contaminated", "audit-failed",
            "isolation-waiting", "gpu3-drift",
        }:
            raise DevGpuSessionError(f"GPU session startup ended as {state['status']}")
        time.sleep(0.1)
    raise DevGpuSessionError("timed out waiting for GPU session controller")


def down_execute() -> dict[str, Any]:
    record = _controller_record()
    pid = record["pid"]
    descriptor = os.pidfd_open(pid)
    try:
        if process_start_ticks(pid) != record["start_ticks"]:
            raise DevGpuSessionError("controller PID was reused")
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
    finally:
        os.close(descriptor)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if not CONTROLLER_RECORD.exists():
            return {"schema_version": 1, "status": "stopped", "gpu_index": 1}
        current = status()
        if current.get("status") == "cleanup-blocked":
            raise DevGpuSessionError("GPU session cleanup is blocked by active NexPoly leases")
        time.sleep(0.25)
    raise DevGpuSessionError("timed out waiting for GPU session cleanup")


def activate_execute(session_id: str) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{32}", session_id) is None:
        raise DevGpuSessionError("activation requires an exact session identity")
    record = _controller_record()
    if record.get("session_id") != session_id:
        raise DevGpuSessionError("activation session identity differs")
    current = status()
    if current.get("status") != "plane-ready":
        raise DevGpuSessionError("controller plane is not awaiting activation")
    manifest_path = Path(record["run_directory"]) / "activation-manifest.json"
    manifest = _load_private_json(manifest_path)
    if (
        set(manifest)
        != {
            "schema_version", "session_id", "source_sha", "source_tree",
            "backend_container_id", "backend_image_id", "backend_config_hash",
            "md_process", "dft_process",
        }
        or manifest.get("schema_version") != 1
        or manifest.get("session_id") != session_id
        or manifest.get("source_sha") != record["source_sha"]
        or manifest.get("source_tree") != record["source_tree"]
        or not isinstance(manifest.get("md_process"), dict)
        or not isinstance(manifest.get("dft_process"), dict)
        or manifest["md_process"].get("session_id") != session_id
        or manifest["dft_process"].get("session_id") != session_id
        or any(
            process.get("source_sha") != record["source_sha"]
            or process.get("source_tree") != record["source_tree"]
            or re.fullmatch(
                r"sha256:[0-9a-f]{64}", str(process.get("worker_lock_sha256"))
            )
            is None
            for process in (manifest["md_process"], manifest["dft_process"])
        )
        or re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("backend_container_id"))) is None
        or not str(manifest.get("backend_image_id", "")).startswith("sha256:")
    ):
        raise DevGpuSessionError("activation manifest differs from the exact session")
    pid = record["pid"]
    descriptor = os.pidfd_open(pid)
    try:
        if _controller_record() != record:
            raise DevGpuSessionError("controller identity changed before activation")
        signal.pidfd_send_signal(descriptor, signal.SIGUSR1)
    finally:
        os.close(descriptor)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        current = status()
        if current.get("status") == "ready":
            return current
        if current.get("status") not in {"plane-ready", "auditing"}:
            raise DevGpuSessionError(
                f"controller activation ended as {current.get('status')}"
            )
        time.sleep(0.1)
    raise DevGpuSessionError("timed out waiting for controller activation")


def drain_execute() -> dict[str, Any]:
    _controller_record()
    from gpu_resource import GpuBrokerClient

    result = GpuBrokerClient(GPU_ROOT / "broker.sock").set_draining(True)
    if result.get("draining") is not True or not isinstance(result.get("leases"), list):
        raise DevGpuSessionError("Broker returned an invalid drain result")
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("up", "status", "down", "drain", "activate", "serve")
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--run-directory", type=Path)
    parser.add_argument("--source-sha")
    parser.add_argument("--source-tree")
    parser.add_argument("--session-id")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command in {"up", "down"} and args.dry_run:
            result = dry_run_plan(args.command)
        elif args.command == "status":
            result = status()
        elif not args.execute:
            raise DevGpuSessionError("state-changing session commands require --execute")
        elif args.command == "up":
            result = up_execute()
        elif args.command == "down":
            result = down_execute()
        elif args.command == "drain":
            result = drain_execute()
        elif args.command == "activate":
            if args.session_id is None:
                raise DevGpuSessionError("activate requires --session-id")
            result = activate_execute(args.session_id)
        else:
            if (
                args.run_directory is None
                or args.source_sha is None
                or args.source_tree is None
            ):
                raise DevGpuSessionError("serve requires the exact run and Git identity")
            controller = SessionController(
                args.run_directory, args.source_sha, args.source_tree
            )
            expected = (sys.executable, *sys.argv)
            return controller.run(expected)
    except (DevGpuSessionError, OSError, subprocess.SubprocessError) as exc:
        print(f"dev GPU session error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Own the opt-in, non-exclusive development GPU1 Broker/MPS session."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
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
CONTROLLER_START_LOCK = SESSION_ROOT / "controller-start.lock"
GPU_ROOT = RUNTIME_ROOT / "gpu-resource"
RESERVATIONS_SOURCE = REPO_ROOT / "ops/config/gpu-external-reservations.json"
POLICY_FILE = REPO_ROOT / "ops/config/gpu-broker-policy.json"
MPS_CONTROL = REPO_ROOT / "scripts/gpu_mps_control.sh"
GPU_INDEX = 1
GPU_UUID = "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771"
GPU3_UUID = "GPU-0818ca6b-d9b6-af6a-71bf-afe3777ee3a5"
POLYPROP_CONTAINER = "polyprop-backend-gpu-1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DFT_WARMUP_CHURN_TIMEOUT_SECONDS = 90.0
STEADY_CHURN_TIMEOUT_SECONDS = 12.0
FULL_AUDIT_ATTEMPTS = 3
PREACTIVATION_ROLLOUT_AUDIT_ATTEMPTS = 8
DEFAULT_DFT_START_TIMEOUT_SECONDS = 60.0
LATE_SESSION_OWNED_STOP_GRACE_SECONDS = 5.0
_PREACTIVATION_DOCKER_CHURN_MESSAGES = frozenset(
    {
        "Docker container inventory changed during audit",
        "Docker container fingerprint changed during audit",
    }
)
_TRANSIENT_MPS_INVENTORY_MESSAGES = frozenset(
    {
        # The MPS control daemon can emit one torn client row while that
        # client disconnects.  Discard the whole audit round; never accept or
        # normalize the malformed row itself.
        "MPS ps row is invalid",
    }
)


class DevGpuSessionError(RuntimeError):
    """The session cannot safely proceed."""


class _AuditRoundChanged(DevGpuSessionError):
    """A bounded audit round lost its CAS identity and must be discarded."""


class _ExactDftTrailingChurn(_AuditRoundChanged):
    """A separately proven DFT-warmup transition may use the 90s budget."""


@dataclass(frozen=True, slots=True)
class TargetSnapshot:
    process_pids: tuple[int, ...]
    docker_claims: tuple[Any, ...]
    systemd_claims: tuple[Any, ...]
    process_declarers: tuple[Any, ...] = ()


def require_gpu1_default_compute_mode(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Fresh, strict GPU1 UUID/compute-mode proof for every controller audit."""

    try:
        completed = run(
            (
                "nvidia-smi",
                "--query-gpu=uuid,compute_mode",
                "--format=csv,noheader,nounits",
                "-i",
                str(GPU_INDEX),
            ),
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DevGpuSessionError("GPU1 compute-mode inventory failed") from exc
    rows = [row.strip() for row in completed.stdout.splitlines() if row.strip()]
    if len(rows) != 1:
        raise DevGpuSessionError("GPU1 compute-mode inventory is invalid")
    fields = [field.strip() for field in rows[0].split(",")]
    normalized_mode = " ".join(fields[1].replace("_", " ").upper().split()) if len(fields) == 2 else ""
    if fields[:1] != [GPU_UUID] or normalized_mode != "DEFAULT":
        raise DevGpuSessionError("GPU1 must retain its exact UUID and Default compute mode")


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
    if path.is_symlink():
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
        if replace:
            os.replace(temporary, path)
        else:
            # Publish a fully written inode with a kernel-enforced no-clobber
            # operation.  An exists()+replace sequence lets two controller
            # creators both pass the check and silently replace each other.
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError as exc:
                raise DevGpuSessionError(
                    f"unsafe preexisting session record: {path}"
                ) from exc
            temporary.unlink()
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


def _open_private_lock(path: Path) -> int:
    """Open a stable owner-private lock inode without following a symlink."""

    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as exc:
        raise DevGpuSessionError(
            f"controller start lock is unavailable: {path}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        path_metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (metadata.st_dev, metadata.st_ino)
            != (path_metadata.st_dev, path_metadata.st_ino)
        ):
            raise DevGpuSessionError(f"controller start lock is unsafe: {path}")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


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
    require_gpu1_default_compute_mode()
    processes = query_compute_processes()
    target_processes = frozenset(processes.get(GPU_UUID, frozenset()))
    process_declarers = capture_compute_process_declarers(target_processes)
    docker_claims = tuple(
        claim for claim in query_docker_gpu_claims() if GPU_UUID in claim.gpu_uuids
    )
    systemd_claims = tuple(
        claim
        for claim in query_systemd_gpu_claims(compute_processes=processes)
        if GPU_UUID in claim.gpu_uuids
    )
    return TargetSnapshot(
        process_pids=tuple(sorted(target_processes)),
        docker_claims=docker_claims,
        systemd_claims=systemd_claims,
        process_declarers=tuple(
            sorted(
                process_declarers.values(),
                key=lambda declarer: (
                    declarer.pid,
                    declarer.process_start_ticks,
                    declarer.process_cgroup,
                ),
            )
        ),
    )


def capture_compute_process_declarers(
    pids: frozenset[int],
) -> dict[int, Any]:
    """Best-effort stable identities adjacent to one NVML process snapshot."""

    from ops.gpu_broker.server import (
        SystemdGpuDeclarer,
        _read_process_uids,
        _read_unified_process_cgroup,
    )
    from ops.gpu_broker.broker import read_process_start_ticks

    result: dict[int, Any] = {}
    for pid in pids:
        try:
            before = (
                read_process_start_ticks(pid),
                _read_process_uids(pid),
                _read_unified_process_cgroup(pid),
            )
            after = (
                read_process_start_ticks(pid),
                _read_process_uids(pid),
                _read_unified_process_cgroup(pid),
            )
        except Exception:
            continue
        if before != after or before[1] != (1001, 1001, 1001, 1001):
            continue
        result[pid] = SystemdGpuDeclarer(
            pid=pid,
            process_start_ticks=before[0],
            process_cgroup=before[2],
            gpu_uuids=frozenset({GPU_UUID}),
        )
    return result


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
        tuple(
            sorted(
                (
                    declarer.pid,
                    declarer.process_start_ticks,
                    declarer.process_cgroup,
                    tuple(sorted(declarer.gpu_uuids)),
                )
                for declarer in claim.live_gpu_declarers
            )
        ),
    )


def snapshot_fingerprint(snapshot: TargetSnapshot) -> tuple[Any, ...]:
    return (
        snapshot.process_pids,
        tuple(_claim_fingerprint(claim) for claim in snapshot.docker_claims),
        tuple(_claim_fingerprint(claim) for claim in snapshot.systemd_claims),
        tuple(
            sorted(
                (
                    declarer.pid,
                    declarer.process_start_ticks,
                    declarer.process_cgroup,
                    tuple(sorted(declarer.gpu_uuids)),
                )
                for declarer in snapshot.process_declarers
            )
        ),
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
    managed_systemd_claims: frozenset[tuple[str, str, str]] = frozenset(),
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
        claim_identity = (claim.scope, claim.unit, claim.control_group)
        if claim_identity in managed_systemd_claims:
            continue
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

    next_fencing_token = status.get("next_fencing_token")
    if (
        isinstance(next_fencing_token, bool)
        or not isinstance(next_fencing_token, int)
        or next_fencing_token < 1
    ):
        raise DevGpuSessionError(
            "Broker returned an invalid fencing authority generation"
        )
    leases = status.get("leases")
    if not isinstance(leases, list):
        raise DevGpuSessionError("Broker returned an invalid lease inventory")
    authority_fields = (
        "lease_id",
        "fencing_token",
        "kind",
        "placement",
        "preferred",
        "component",
        "environment",
        "client_id",
        "gpu_index",
        "gpu_uuid",
        "parent_lease_id",
        "status",
        "mps_termination_status",
        "owner_pid",
        "owner_process_start_ticks",
        "owner_boot_id",
        "workload_pid",
        "workload_process_start_ticks",
        "workload_process_group_id",
        "workload_cgroup",
    )
    normalized: list[tuple[Any, ...]] = []
    for lease in leases:
        if not isinstance(lease, dict):
            raise DevGpuSessionError("Broker returned an invalid lease record")
        normalized.append(
            tuple((key in lease, lease.get(key)) for key in authority_fields)
        )
    return (
        status.get("broker_instance_id"),
        next_fencing_token,
        status.get("draining"),
        tuple(sorted(normalized, key=repr)),
    )


def _canonical_broker_leases(status: dict[str, Any]) -> tuple[Any, ...]:
    """Decode the complete Broker inventory without trusting partial records."""

    from ops.gpu_broker.broker import Lease

    records = status.get("leases")
    if not isinstance(records, list):
        raise DevGpuSessionError("Broker returned an invalid lease inventory")
    result: list[Lease] = []
    for record in records:
        if not isinstance(record, dict):
            raise DevGpuSessionError("Broker returned an invalid lease record")
        try:
            lease = Lease(**record)
        except (TypeError, ValueError) as exc:
            raise DevGpuSessionError(
                "Broker returned an undecodable lease record"
            ) from exc
        if lease.public_dict() != record:
            raise DevGpuSessionError("Broker lease record is not canonical")
        result.append(lease)
    return tuple(result)


def _exact_dft_residency_leases(status: dict[str, Any]) -> tuple[Any, ...]:
    """Decode only exact active GPU1 DFT residency records from Broker status."""

    from ops.gpu_broker.server import exact_dft_residency_scope_authority

    result = []
    for lease in _canonical_broker_leases(status):
        if exact_dft_residency_scope_authority(
            lease,
            index=GPU_INDEX,
            uuid=GPU_UUID,
        ) is not None:
            result.append(lease)
    return tuple(result)


def _exact_backend_docker_workload_pids(
    leases: tuple[Any, ...],
    snapshot: TargetSnapshot,
) -> frozenset[int]:
    """Bind the Backend allowlist to one active lease and Docker identity."""

    from ops.gpu_broker.server import (
        exact_dev_gpu1_backend_docker_workload_pids,
    )

    return exact_dev_gpu1_backend_docker_workload_pids(
        leases,
        snapshot.docker_claims,
    )


def _canonical_systemd_control_group(value: object) -> str | None:
    """Validate a cgroup-v2 path without resolving ambiguous components."""

    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value == "/"
        or value.endswith("/")
        or "//" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(part in {"", ".", ".."} for part in value.split("/")[1:])
    ):
        return None
    return value


def _canonical_systemd_identity_map(
    identities: object,
) -> dict[int, tuple[int, str]] | None:
    """Decode complete PID identities without normalizing hostile cgroups."""

    if not isinstance(identities, tuple):
        return None
    result: dict[int, tuple[int, str]] = {}
    for identity in identities:
        if (
            not isinstance(identity, tuple)
            or len(identity) != 3
            or not isinstance(identity[0], int)
            or isinstance(identity[0], bool)
            or identity[0] <= 0
            or not isinstance(identity[1], int)
            or isinstance(identity[1], bool)
            or identity[1] <= 0
            or identity[0] in result
        ):
            return None
        process_cgroup = _canonical_systemd_control_group(identity[2])
        if process_cgroup is None:
            return None
        result[identity[0]] = identity[1], process_cgroup
    return result


def _structured_membership_delta_within(
    error: Exception,
    *,
    scope: str,
    unit: str,
    unit_cgroup: str,
    workload_cgroup: str,
) -> bool:
    """Accept only a nonempty, identity-stable delta inside one workload."""

    from ops.gpu_broker.server import (
        SystemdMembershipChanged,
        _systemd_cgroup_contains,
    )

    if (
        _canonical_systemd_control_group(unit_cgroup) != unit_cgroup
        or _canonical_systemd_control_group(workload_cgroup) != workload_cgroup
        or not _systemd_cgroup_contains(workload_cgroup, unit_cgroup)
        or not isinstance(error, SystemdMembershipChanged)
        or error.code != "gpu_claim_inventory_changed"
        or error.scope != scope
        or error.unit != unit
        or error.control_group != unit_cgroup
    ):
        return False
    expected_identities = _canonical_systemd_identity_map(
        error.expected_identities
    )
    current_identities = _canonical_systemd_identity_map(
        error.current_identities
    )
    if expected_identities is None or current_identities is None:
        return False
    if any(
        not _systemd_cgroup_contains(process_cgroup, unit_cgroup)
        for _start_ticks, process_cgroup in (
            *expected_identities.values(),
            *current_identities.values(),
        )
    ):
        return False
    if any(
        expected_identities[pid] != current_identities[pid]
        for pid in expected_identities.keys() & current_identities.keys()
    ):
        # PID reuse or a cgroup move is never ordinary membership churn.
        return False
    changed_identities = tuple(
        (pid, *identity)
        for membership, other in (
            (expected_identities, current_identities),
            (current_identities, expected_identities),
        )
        for pid, identity in membership.items()
        if pid not in other
    )
    return bool(changed_identities) and all(
        _systemd_cgroup_contains(process_cgroup, workload_cgroup)
        for _pid, _start_ticks, process_cgroup in changed_identities
    )


def _is_exact_dft_membership_churn(
    error: Exception,
    status: dict[str, Any],
) -> bool:
    """Limit transient retries to the exact active DFT residency ancestry."""

    from gpu_resource.transient_scope import (
        scope_unit_name,
        user_manager_control_group,
    )
    from ops.gpu_broker.server import (
        SystemdMembershipChanged,
        exact_dft_residency_scope_authority,
    )

    if (
        not isinstance(error, SystemdMembershipChanged)
        or status.get("draining") is not False
    ):
        return False
    leases = _exact_dft_residency_leases(status)
    if len(leases) != 1:
        return False
    lease = leases[0]
    authority = exact_dft_residency_scope_authority(
        lease,
        index=GPU_INDEX,
        uuid=GPU_UUID,
    )
    if authority is None:
        return False
    workload_cgroup = authority[2]
    if error.scope == "system":
        return _structured_membership_delta_within(
            error,
            scope="system",
            unit="user@1001.service",
            unit_cgroup=user_manager_control_group(1001),
            workload_cgroup=workload_cgroup,
        )
    if error.scope == "user":
        return _structured_membership_delta_within(
            error,
            scope="user",
            unit=scope_unit_name(lease.lease_id),
            unit_cgroup=workload_cgroup,
            workload_cgroup=workload_cgroup,
        )
    return False


def _is_exact_managed_scope_membership_transition(
    error: Exception,
    before: dict[str, Any],
    after: dict[str, Any],
) -> bool:
    """Prove an exact PID membership delta belongs to one lease transition."""

    from ops.gpu_broker.broker import Lease
    from ops.gpu_broker.server import (
        SystemdMembershipChanged,
        SystemdProcessDisappeared,
        _exact_dev_gpu1_host_scope_authority,
    )
    from gpu_resource.transient_scope import user_manager_control_group

    if not isinstance(
        error,
        (SystemdProcessDisappeared, SystemdMembershipChanged),
    ):
        return False
    before_instance = before.get("broker_instance_id")
    if (
        not isinstance(before_instance, str)
        or not before_instance
        or after.get("broker_instance_id") != before_instance
        or before.get("draining") is not False
        or after.get("draining") is not False
        or before.get("quarantined_gpus") != {}
        or after.get("quarantined_gpus") != {}
    ):
        return False
    before_generation = before.get("next_fencing_token")
    after_generation = after.get("next_fencing_token")
    if (
        isinstance(before_generation, bool)
        or not isinstance(before_generation, int)
        or isinstance(after_generation, bool)
        or not isinstance(after_generation, int)
        or after_generation not in (before_generation, before_generation + 1)
    ):
        return False
    try:
        before_leases = _canonical_broker_leases(before)
        after_leases = _canonical_broker_leases(after)
    except DevGpuSessionError:
        return False
    if any(
        len({getattr(item, field) for item in leases}) != len(leases)
        for leases in (before_leases, after_leases)
        for field in ("lease_id", "fencing_token")
    ):
        return False

    def live_authority(leases: tuple[Lease, ...]) -> tuple[str, ...]:
        # Heartbeats may legitimately advance while the systemd inventory is
        # running.  Every other canonical field remains ownership authority,
        # including budgets, request identity and MPS termination state.
        normalized: list[str] = []
        for item in leases:
            record = item.public_dict()
            record.pop("heartbeat_at")
            normalized.append(
                json.dumps(record, sort_keys=True, separators=(",", ":"))
            )
        return tuple(sorted(normalized))

    def released_tombstone(
        status: dict[str, Any],
        leases: tuple[Lease, ...],
        generation: int,
    ) -> tuple[dict[str, Any] | None, Lease | None]:
        record = status.get("last_released_lease")
        if record is None:
            return None, None
        if not isinstance(record, dict):
            raise ValueError("released lease tombstone is not an object")
        try:
            lease = Lease(**record)
        except (TypeError, ValueError):
            raise ValueError("released lease tombstone is not canonical") from None
        if lease.public_dict() != record:
            raise ValueError("released lease tombstone is not canonical")
        if (
            lease.broker_instance_id != before_instance
            or isinstance(lease.fencing_token, bool)
            or not isinstance(lease.fencing_token, int)
            or lease.fencing_token <= 0
            or lease.fencing_token >= generation
            or any(
                live.lease_id == lease.lease_id
                or live.fencing_token == lease.fencing_token
                for live in leases
            )
        ):
            raise ValueError("released lease tombstone collides with live authority")
        return record, lease

    try:
        before_record, before_released = released_tombstone(
            before,
            before_leases,
            before_generation,
        )
        record, released_candidate = released_tombstone(
            after,
            after_leases,
            after_generation,
        )
    except ValueError:
        return False

    tombstone_advanced = (
        released_candidate is not None
        and (
            before_released is None
            or (
                before_released.lease_id != released_candidate.lease_id
                and before_released.fencing_token
                != released_candidate.fencing_token
            )
        )
    )

    candidate: Lease | None = None
    if (
        after_generation == before_generation + 1
        and before_record == record
    ):
        issued = [
            item
            for item in after_leases
            if item.fencing_token == before_generation
            and all(
                existing.lease_id != item.lease_id
                and existing.fencing_token != item.fencing_token
                for existing in before_leases
            )
        ]
        if len(issued) == 1:
            issued_candidate = issued[0]
            after_without_issue = tuple(
                item
                for item in after_leases
                if item.lease_id != issued_candidate.lease_id
            )
            if live_authority(before_leases) == live_authority(
                after_without_issue
            ):
                candidate = issued_candidate

    if (
        candidate is None
        and after_generation == before_generation + 1
        and tombstone_advanced
        and released_candidate is not None
    ):
        if (
            released_candidate.fencing_token == before_generation
            and all(
                item.lease_id != released_candidate.lease_id
                and item.fencing_token != released_candidate.fencing_token
                for item in (*before_leases, *after_leases)
            )
            and live_authority(before_leases) == live_authority(after_leases)
        ):
            candidate = released_candidate

    if (
        candidate is None
        and after_generation == before_generation
        and tombstone_advanced
        and released_candidate is not None
    ):
        released_matches = tuple(
            item
            for item in before_leases
            if item.lease_id == released_candidate.lease_id
            and item.fencing_token == released_candidate.fencing_token
        )
        if len(released_matches) == 1:
            visible_candidate = released_matches[0]
            before_without_release = tuple(
                item
                for item in before_leases
                if item.lease_id != visible_candidate.lease_id
            )
            if (
                all(
                    item.lease_id != visible_candidate.lease_id
                    and item.fencing_token != visible_candidate.fencing_token
                    for item in after_leases
                )
                and visible_candidate.fencing_token < before_generation
                and live_authority((visible_candidate,))
                == live_authority((released_candidate,))
                and live_authority(before_without_release)
                == live_authority(after_leases)
            ):
                candidate = visible_candidate

    if candidate is None:
        return False
    if candidate.broker_instance_id != before_instance:
        return False
    authority = _exact_dev_gpu1_host_scope_authority(
        candidate,
        index=GPU_INDEX,
        uuid=GPU_UUID,
    )
    if authority is None:
        return False
    expected_scope_cgroup = authority[3]
    if isinstance(error, SystemdProcessDisappeared):
        return (
            isinstance(error.pid, int)
            and not isinstance(error.pid, bool)
            and error.pid > 0
            and isinstance(error.expected_start_ticks, int)
            and not isinstance(error.expected_start_ticks, bool)
            and error.expected_start_ticks > 0
            and error.expected_control_group == expected_scope_cgroup
        )

    return _structured_membership_delta_within(
        error,
        scope="system",
        unit="user@1001.service",
        unit_cgroup=user_manager_control_group(1001),
        workload_cgroup=expected_scope_cgroup,
    )


def consistent_broker_snapshot(
    client: Any,
    collector: Callable[[], TargetSnapshot] = collect_target_snapshot,
    *,
    attempts: int = 3,
    membership_churn_retries: int = 8,
    membership_churn_timeout_seconds: float = STEADY_CHURN_TIMEOUT_SECONDS,
    membership_churn_guard: Callable[[dict[str, Any]], bool] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[dict[str, Any], TargetSnapshot]:
    """Bind an expensive host inventory between two stable Broker reads."""

    if attempts < 1:
        raise ValueError("attempts must be positive")
    if membership_churn_retries < 0:
        raise ValueError("membership_churn_retries must not be negative")
    if (
        isinstance(membership_churn_timeout_seconds, bool)
        or not isinstance(membership_churn_timeout_seconds, (int, float))
        or not 0 < float(membership_churn_timeout_seconds)
        <= (
            DFT_WARMUP_CHURN_TIMEOUT_SECONDS
            if membership_churn_guard is not None
            else 30.0
        )
    ):
        raise ValueError(
            "membership_churn_timeout_seconds exceeds its bounded audit window"
        )
    authority_changes = 0
    churn_retries = 0
    audit_started = monotonic()
    while authority_changes < attempts:
        before = client.status()
        before_authority = broker_authority_token(before)
        try:
            snapshot = collector()
        except Exception as exc:
            after = client.status()
            after_authority = broker_authority_token(after)
            if (
                before_authority != after_authority
                and _is_exact_managed_scope_membership_transition(
                    exc, before, after
                )
            ):
                authority_changes += 1
                continue
            if (
                before_authority == after_authority
                and _is_exact_dft_membership_churn(exc, after)
            ):
                if (
                    churn_retries >= membership_churn_retries
                    or monotonic() - audit_started
                    >= float(membership_churn_timeout_seconds)
                ):
                    raise DevGpuSessionError(
                        "exact DFT residency membership remained unstable "
                        "throughout the host audit"
                    ) from exc
                if (
                    membership_churn_guard is not None
                    and not membership_churn_guard(after)
                ):
                    raise DevGpuSessionError(
                        "exact DFT membership guard did not prove isolation"
                    ) from exc
                churn_retries += 1
                continue
            raise
        after = client.status()
        if before_authority == broker_authority_token(after):
            return after, snapshot
        authority_changes += 1
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
        self.activation_generation = 0
        self.dft_stabilization_generation = 0
        self.dft_stabilized = False
        self.dft_warmup_open = True
        self.dft_churn_started_at: float | None = None
        self.automatic_recovery = False
        self.plane_ready_published = False
        self.plane_cleaned = False
        self.owned_components_stopped = False
        self.final_owned_components_stop_confirmed = False
        self.late_session_owned_stop_attempts = 0
        self.late_session_owned_stop_deadline: float | None = None
        raw_dft_start_timeout = os.environ.get(
            "MONOMER_DFT_START_TIMEOUT_SECONDS",
            str(int(DEFAULT_DFT_START_TIMEOUT_SECONDS)),
        )
        if re.fullmatch(r"[1-9][0-9]*", raw_dft_start_timeout) is None:
            raise DevGpuSessionError(
                "MONOMER_DFT_START_TIMEOUT_SECONDS must be a positive integer"
            )
        self.dft_start_timeout_seconds = float(raw_dft_start_timeout)
        self.cpu_restored = False
        self.mps_started = False
        self.audit_sequence = 0
        self.fast_audit_sequence = 0
        self.full_audit_generation = 0
        self.audit_mode = "full"
        self.last_audit_activation_generation = 0
        self.last_audit_stabilization_generation = 0
        self.last_audit_duration = 0.0
        self.last_mps_authority: Any | None = None
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
                "audit_mode": self.audit_mode,
                "full_audit_generation": self.full_audit_generation,
                "fast_audit_sequence": self.fast_audit_sequence,
                "activation_generation": self.activation_generation,
                "dft_stabilization_generation": self.dft_stabilization_generation,
                "last_audit_activation_generation": (
                    self.last_audit_activation_generation
                ),
                "last_audit_stabilization_generation": (
                    self.last_audit_stabilization_generation
                ),
                "dft_stabilized": self.dft_stabilized,
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
        runtime_directory = f"/run/user/{os.geteuid()}"
        environment = {
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_directory}/bus",
            "HOME": os.environ.get("HOME", str(RUNTIME_ROOT / "home")),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LOGNAME": os.environ.get("LOGNAME", "devuser"),
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(REPO_ROOT),
            "USER": os.environ.get("USER", "devuser"),
            "XDG_RUNTIME_DIR": runtime_directory,
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
        pipe = self._authority_path(self.pipe_fd)
        log = self._authority_path(self.log_fd)
        return self._safe_env(
            CUDA_MPS_LOG_DIRECTORY=str(log),
            CUDA_MPS_PIPE_DIRECTORY=str(pipe),
            CUDA_VISIBLE_DEVICES=GPU_UUID,
            NEXPOLY_GPU_STATE_ROOT=str(root),
            NEXPOLY_GPU_EXTERNAL_RESERVATIONS=str(
                self._authority_path(self.reservations_fd)
            ),
            NEXPOLY_GPU_BROKER_SOCKET=str(root / "broker.sock"),
            NEXPOLY_GPU_MPS_SLOT_DIRECTORY=str(self._authority_path(self.slot_fd)),
            NEXPOLY_GPU_MPS_PIPE_DIRECTORY=str(pipe),
            NEXPOLY_GPU_MPS_LOG_DIRECTORY=str(log),
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

    def _mps_authority(self) -> Any:
        from ops.gpu_broker.server import MpsRuntimeGuard

        authority = MpsRuntimeGuard(
            self._authority_path(self.root_fd)
        ).authority_snapshot(GPU_INDEX, GPU_UUID)
        if authority.descriptor_authority is not True:
            raise DevGpuSessionError(
                "MPS authority lost its descriptor-bound root"
            )
        return authority

    def _mps_authority_for_audit(self, client: Any) -> Any:
        """Discard torn inventories and retry proven DFT warmup churn."""

        from ops.gpu_broker.broker import BrokerError

        published = False
        while True:
            try:
                return self._mps_authority()
            except BrokerError as exc:
                if (
                    type(exc) is BrokerError
                    and exc.code == "mps_control_unavailable"
                    and len(exc.args) == 1
                    and exc.args[0] in _TRANSIENT_MPS_INVENTORY_MESSAGES
                ):
                    raise _AuditRoundChanged(
                        "MPS client inventory changed while it was serialized"
                    ) from exc
                if (
                    exc.code != "mps_authority_changed"
                    or not self.dft_warmup_open
                    or self.dft_stabilized
                    or self.activation_generation != 0
                ):
                    raise
                status = client.status()
                if status.get("draining") is not False:
                    raise
                self._only_exact_dft_residency(status)
                now = time.monotonic()
                if self.dft_churn_started_at is None:
                    self.dft_churn_started_at = now
                if (
                    now - self.dft_churn_started_at
                    >= DFT_WARMUP_CHURN_TIMEOUT_SECONDS
                ):
                    raise DevGpuSessionError(
                        "exact DFT residency did not stabilize within 90 seconds"
                    ) from exc
                if not published:
                    self.audit_mode = "fast"
                    self._state(
                        "stabilizing",
                        contaminated=False,
                        broker_draining=False,
                        mps_authority_churn=True,
                    )
                    published = True
                time.sleep(0.05)

    @staticmethod
    def _mps_authority_core(authority: Any) -> tuple[Any, ...]:
        return (
            authority.server_pids,
            authority.gpu_declarers,
            authority.descriptor_authority,
        )

    @staticmethod
    def _mps_control_declarers(authority: Any) -> frozenset[Any]:
        return frozenset(
            declarer
            for declarer in authority.gpu_declarers
            if declarer.pid not in authority.server_pids
        )

    @staticmethod
    def _mps_server_declarers(authority: Any) -> dict[int, Any]:
        return {
            declarer.pid: declarer
            for declarer in authority.gpu_declarers
            if declarer.pid in authority.server_pids
        }

    @staticmethod
    def _captured_pids_match_expected_declarers(
        authorized_pids: frozenset[int],
        process_pids: frozenset[int],
        process_declarers: tuple[Any, ...],
        expected_declarers: frozenset[Any],
    ) -> bool:
        """Never authorize a captured NVML PID using a later reused identity."""

        return not SessionController._captured_pid_identity_mismatches(
            authorized_pids,
            process_pids,
            process_declarers,
            expected_declarers,
        )

    @staticmethod
    def _captured_pid_identity_mismatches(
        authorized_pids: frozenset[int],
        process_pids: frozenset[int],
        process_declarers: tuple[Any, ...],
        expected_declarers: frozenset[Any],
    ) -> frozenset[int]:
        """Return captured PIDs whose adjacent identity cannot be proven."""

        mismatches: set[int] = set()
        for pid in authorized_pids & process_pids:
            try:
                captured = frozenset(
                    declarer
                    for declarer in process_declarers
                    if declarer.pid == pid
                )
                expected = frozenset(
                    declarer
                    for declarer in expected_declarers
                    if declarer.pid == pid
                )
            except (AttributeError, TypeError):
                mismatches.add(pid)
                continue
            if len(captured) != 1 or captured != expected:
                mismatches.add(pid)
        return frozenset(mismatches)

    @staticmethod
    def _mps_declarers_bound_to_claim(
        authorized_declarers: frozenset[Any],
        claim: Any,
    ) -> bool:
        """Bind only the descriptor MPS processes that live in this claim.

        A controller launched from a login session and a workload launched by
        the user manager occupy sibling cgroups.  MPS identities outside the
        claim remain covered by descriptor authority; any MPS identity inside
        the claim must be present in its atomic live-environment snapshot.
        """

        from ops.gpu_broker.server import _systemd_cgroup_contains

        try:
            live_declarers = frozenset(claim.live_gpu_declarers)
            claim_declarers = frozenset(
                declarer
                for declarer in authorized_declarers
                if _systemd_cgroup_contains(
                    declarer.process_cgroup,
                    claim.control_group,
                )
            )
        except (AttributeError, TypeError, ValueError):
            return False
        return claim_declarers <= live_declarers

    @staticmethod
    def _mps_server_declarers_are_user_manager_siblings(
        server_declarers: frozenset[Any],
    ) -> bool:
        """Prove descriptor-owned servers live beside, not inside, user@.service."""

        from gpu_resource.transient_scope import user_manager_control_group
        from ops.gpu_broker.server import _systemd_cgroup_contains

        manager_control_group = user_manager_control_group(1001)
        user_slice_control_group = manager_control_group.rsplit("/", 1)[0]
        try:
            return bool(server_declarers) and all(
                _canonical_systemd_control_group(declarer.process_cgroup)
                == declarer.process_cgroup
                and _systemd_cgroup_contains(
                    declarer.process_cgroup,
                    user_slice_control_group,
                )
                and not _systemd_cgroup_contains(
                    declarer.process_cgroup,
                    manager_control_group,
                )
                for declarer in server_declarers
            )
        except (AttributeError, TypeError, ValueError):
            return False

    @staticmethod
    def _managed_workload_authority(
        status: dict[str, Any],
        snapshot: TargetSnapshot,
        authorized_mps_declarers: frozenset[Any] = frozenset(),
        authorized_mps_client_pids: frozenset[int] = frozenset(),
        authorized_mps_server_pids: frozenset[int] = frozenset(),
    ) -> tuple[
        frozenset[int],
        frozenset[int],
        frozenset[tuple[str, str, str]],
    ]:
        from ops.gpu_broker.server import (
            claim_is_exact_dev_gpu1_host_workloads_scope,
        )

        leases = _canonical_broker_leases(status)
        backend_leases = tuple(
            lease
            for lease in leases
            if lease.gpu_index == GPU_INDEX
            and lease.gpu_uuid == GPU_UUID
            and lease.component == "backend"
        )
        backend_pids = _exact_backend_docker_workload_pids(leases, snapshot)
        if backend_leases and not backend_pids:
            raise DevGpuSessionError(
                "GPU1 Backend lease lacks exact active Docker workload authority"
            )
        managed_client_pids = set(backend_pids)
        managed_systemd_claims: set[tuple[str, str, str]] = set()
        for claim in snapshot.systemd_claims:
            if claim_is_exact_dev_gpu1_host_workloads_scope(
                claim,
                index=GPU_INDEX,
                uuid=GPU_UUID,
                leases=leases,
                authorized_mps_declarers=authorized_mps_declarers,
                authorized_mps_server_pids=authorized_mps_server_pids,
            ):
                managed_systemd_claims.add(
                    (claim.scope, claim.unit, claim.control_group)
                )
                managed_client_pids.update(
                    declarer.pid
                    for declarer in claim.live_gpu_declarers
                    if declarer not in authorized_mps_declarers
                    and declarer.gpu_uuids == frozenset({GPU_UUID})
                )
        exact_client_pids = frozenset(managed_client_pids)
        return (
            exact_client_pids & authorized_mps_client_pids,
            exact_client_pids,
            frozenset(managed_systemd_claims),
        )

    @staticmethod
    def _managed_workload_pids(
        status: dict[str, Any], snapshot: TargetSnapshot
    ) -> frozenset[int]:
        return SessionController._managed_workload_authority(status, snapshot)[1]

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

    def _exact_session_owned_late_dft_lease(
        self,
        status: dict[str, Any],
    ) -> tuple[str, int] | None:
        """Bind a late DFT lease to this exact controller session.

        A residency lease can become visible after the first stop-owned sweep
        but before the Worker publishes its socket/session record.  Broker
        scope authority alone is not a session identity, so require the sole
        lease's stable owner and workload plus the inherited, GPU1-only
        session environment before another fenced shell sweep is allowed.
        """

        from ops.gpu_broker.broker import (
            OwnerIdentity,
            process_identity_alive,
            read_boot_id,
        )
        from ops.gpu_broker.server import (
            _read_process_environment,
            exact_dft_residency_scope_authority,
            process_is_exact_dft_residency_descendant,
        )

        if status.get("draining") is not True:
            return None
        try:
            leases = _canonical_broker_leases(status)
        except (DevGpuSessionError, TypeError, ValueError):
            return None
        if len(leases) != 1:
            return None
        lease = leases[0]
        authority = exact_dft_residency_scope_authority(
            lease,
            index=GPU_INDEX,
            uuid=GPU_UUID,
        )
        if authority is None or status.get("leases") != [lease.public_dict()]:
            return None
        workload_pid = authority[0]
        if lease.owner_pid == workload_pid:
            return None
        try:
            boot_id = read_boot_id()
            owner = OwnerIdentity(
                pid=lease.owner_pid,
                process_start_ticks=lease.owner_process_start_ticks,
                boot_id=lease.owner_boot_id,
            )

            def identities_are_live() -> bool:
                return (
                    lease.owner_boot_id == boot_id
                    and process_identity_alive(owner, current_boot_id=boot_id)
                    and process_is_exact_dft_residency_descendant(
                        workload_pid,
                        lease,
                        index=GPU_INDEX,
                        uuid=GPU_UUID,
                    )
                )

            if not identities_are_live():
                return None
            owner_environment = _read_process_environment(lease.owner_pid)
            workload_environment = _read_process_environment(workload_pid)
            common_environment = {
                "MONOMER_DFT_DEPLOYMENT": "dev",
                "NEXPOLY_DEV_GPU1_ONLY_SESSION": "1",
                "NEXPOLY_DEV_GPU_SESSION_ID": self.session_id,
                "NEXPOLY_DFT_GPU_DEVICE": str(GPU_INDEX),
                "NEXPOLY_DFT_OVERFLOW_GPU_DEVICES": "",
            }
            workload_only_environment = {
                "MONOMER_DFT_EXECUTOR_PROCESS": "1",
                "NEXPOLY_DFT_EXECUTOR_GPU_DEVICE": str(GPU_INDEX),
                "NEXPOLY_DFT_EXECUTOR_GPU_UUID": GPU_UUID,
            }
            if any(
                owner_environment.get(name) != value
                or workload_environment.get(name) != value
                for name, value in common_environment.items()
            ) or any(
                workload_environment.get(name) != value
                for name, value in workload_only_environment.items()
            ):
                return None
            if not identities_are_live():
                return None
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        return lease.lease_id, lease.fencing_token

    def _retry_late_session_owned_stop(self, status: dict[str, Any]) -> bool:
        """Retry a proven late DFT lease through its complete startup window."""

        if (
            not self.automatic_recovery
            or not self.owned_components_stopped
        ):
            return False
        identity = self._exact_session_owned_late_dft_lease(status)
        if identity is None:
            return False
        now = time.monotonic()
        if self.late_session_owned_stop_deadline is None:
            self.late_session_owned_stop_deadline = (
                now
                + self.dft_start_timeout_seconds
                + LATE_SESSION_OWNED_STOP_GRACE_SECONDS
            )
        if now >= self.late_session_owned_stop_deadline:
            return False
        self.late_session_owned_stop_attempts += 1
        succeeded = self._recovery_command("gpu-session-stop-owned-internal")
        if succeeded:
            self._state(
                "isolation-waiting",
                contaminated=True,
                reason="late exact session-owned DFT lease required another stop sweep",
            )
        return True

    def _cleanup(self, client: Any) -> bool:
        drained = client.set_draining(True)
        gpu1_lease_active = any(
            isinstance(lease, dict) and lease.get("gpu_uuid") == GPU_UUID
            for lease in drained.get("leases", [])
        )
        if gpu1_lease_active:
            self.final_owned_components_stop_confirmed = False
            if self._retry_late_session_owned_stop(drained):
                return False
            self._state("cleanup-blocked", reason="NexPoly GPU1 leases are still active")
            return False
        if self.automatic_recovery and not self.final_owned_components_stop_confirmed:
            # The first stop-owned sweep can race a DFT start before its PID,
            # socket, session record, or Broker lease appears.  Once the lease
            # inventory looks empty, cross the Worker ctl lock one final time;
            # the shell command succeeds only after all three runtime identity
            # paths are absent.  Re-read Broker admission afterward so this is
            # a two-sided fence around the complete late-start window.
            if not self._recovery_command("gpu-session-stop-owned-internal"):
                return False
            self.owned_components_stopped = True
            drained = client.set_draining(True)
            gpu1_lease_active = any(
                isinstance(lease, dict) and lease.get("gpu_uuid") == GPU_UUID
                for lease in drained.get("leases", [])
            )
            if gpu1_lease_active:
                if self._retry_late_session_owned_stop(drained):
                    return False
                self._state(
                    "cleanup-blocked",
                    reason="NexPoly GPU1 leases appeared during the final owned stop",
                )
                return False
            self.final_owned_components_stop_confirmed = True
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

    @staticmethod
    def _only_exact_dft_residency(status: dict[str, Any]) -> Any:
        leases = _exact_dft_residency_leases(status)
        if len(leases) != 1:
            raise DevGpuSessionError(
                "DFT stabilization requires one exact residency lease"
            )
        lease = leases[0]
        if status.get("leases") != [lease.public_dict()]:
            raise DevGpuSessionError(
                "DFT stabilization encountered another Broker lease"
            )
        return lease

    @staticmethod
    def _exact_dft_descendants(
        status: dict[str, Any],
        pids: frozenset[int],
    ) -> bool:
        from ops.gpu_broker.server import (
            process_is_exact_dft_residency_descendant,
        )

        try:
            lease = SessionController._only_exact_dft_residency(status)
        except DevGpuSessionError:
            return False
        return all(
            process_is_exact_dft_residency_descendant(
                pid,
                lease,
                index=GPU_INDEX,
                uuid=GPU_UUID,
            )
            for pid in pids
        )

    def _exact_dft_mps_client_growth(
        self,
        status: dict[str, Any],
        before: Any,
        after: Any,
    ) -> bool:
        """Prove a monotonic private-MPS client addition is exact DFT warmup.

        A disappearing or replaced client is deliberately not retryable: its
        former process identity can no longer be proven from the trailing
        snapshot.  The unchanged descriptor-bound control/server identity and
        every newly visible client PID must still bind to the sole DFT
        residency ancestry.
        """

        if (
            not self.dft_warmup_open
            or self.dft_stabilized
            or self.activation_generation != 0
            or self._mps_authority_core(before)
            != self._mps_authority_core(after)
            or not before.clients < after.clients
        ):
            return False
        added_pids = frozenset(
            client.client_pid for client in after.clients - before.clients
        )
        return bool(added_pids) and self._exact_dft_descendants(
            status,
            added_pids,
        )

    def _exact_dft_lazy_mps_server_growth(
        self,
        status: dict[str, Any],
        before: Any,
        after: Any,
    ) -> bool:
        """Prove the one allowed descriptor-owned lazy MPS server transition.

        CUDA MPS starts its server on the first client connection.  The
        descriptor-bound control identity must remain unchanged, exactly one
        server may appear, and every client already visible in the later
        snapshot must belong to the sole DFT residency.  This proof only
        selects the authority used to classify the already captured host
        inventory; a fresh fast audit is still required before retrying.
        """

        from ops.gpu_broker.server import MpsRuntimeGuard

        if (
            not self.dft_warmup_open
            or self.dft_stabilized
            or self.activation_generation != 0
            or before.descriptor_authority is not True
            or after.descriptor_authority is not True
            or before.server_pids
            or len(after.server_pids) != 1
            or before.clients
        ):
            return False
        control_before = self._mps_control_declarers(before)
        control_after = self._mps_control_declarers(after)
        if control_before != control_after or len(control_before) != 1:
            return False
        server_pid = next(iter(after.server_pids))
        server_declarers = self._mps_server_declarers(after)
        if (
            set(server_declarers) != {server_pid}
            or before.gpu_declarers != control_before
            or after.gpu_declarers
            != control_after | frozenset(server_declarers.values())
            or any(
                client.server_pid != server_pid
                or not MpsRuntimeGuard._device_matches(
                    client.device_uuid,
                    GPU_UUID,
                )
                for client in after.clients
            )
        ):
            return False
        try:
            lease = self._only_exact_dft_residency(status)
        except DevGpuSessionError:
            return False
        client_pids = frozenset(
            client.client_pid for client in after.clients
        )
        exact_pids = client_pids or frozenset({lease.workload_pid})
        return self._exact_dft_descendants(
            status,
            exact_pids,
        )

    def _fast_dft_churn_guard(
        self,
        client: Any,
        status: dict[str, Any],
    ) -> bool:
        """Prove isolation during exact DFT-only systemd membership churn.

        A successful fast proof never means ready.  It only permits another
        attempt at the unchanged, authoritative full audit.
        """

        from ops.gpu_broker.server import (
            EXPECTED_GPU_UUIDS,
            process_is_exact_dft_residency_descendant,
            query_compute_processes,
            query_docker_gpu_claims,
            query_gpu_inventory,
        )

        if (
            not self.dft_warmup_open
            or self.dft_stabilized
            or self.activation_generation != 0
        ):
            raise DevGpuSessionError(
                "exact DFT churn occurred outside the DFT stabilization phase"
            )
        now = time.monotonic()
        if self.dft_churn_started_at is None:
            self.dft_churn_started_at = now
        if (
            now - self.dft_churn_started_at
            >= DFT_WARMUP_CHURN_TIMEOUT_SECONDS
        ):
            raise DevGpuSessionError(
                "exact DFT residency did not stabilize within 90 seconds"
            )
        lease = self._only_exact_dft_residency(status)
        expected_token = broker_authority_token(status)
        before = client.status()
        if broker_authority_token(before) != expected_token:
            raise _AuditRoundChanged(
                "Broker authority changed before the DFT fast audit"
            )
        if (
            EXPECTED_GPU_UUIDS.get(GPU_INDEX) != GPU_UUID
            or query_gpu_inventory().get(GPU_INDEX) != GPU_UUID
        ):
            raise DevGpuSessionError("physical GPU1 identity changed")
        require_gpu1_default_compute_mode()

        mps_before = self._mps_authority_for_audit(client)
        authorized_before = mps_before.server_pids
        compute_before = frozenset(
            query_compute_processes().get(GPU_UUID, frozenset())
        )
        compute_before_declarers = capture_compute_process_declarers(
            compute_before
        )
        docker_before = tuple(
            claim
            for claim in query_docker_gpu_claims()
            if GPU_UUID in claim.gpu_uuids
        )
        clients_before = frozenset(
            client.client_pid for client in mps_before.clients
        )

        if len(authorized_before) > 1:
            raise DevGpuSessionError(
                "DFT fast audit found multiple descriptor-owned MPS servers"
            )
        if docker_before:
            raise DevGpuSessionError(
                "Docker declared GPU1 during DFT-only stabilization"
            )

        compute_after = frozenset(
            query_compute_processes().get(GPU_UUID, frozenset())
        )
        compute_after_declarers = capture_compute_process_declarers(
            compute_after
        )
        docker_after = tuple(
            claim
            for claim in query_docker_gpu_claims()
            if GPU_UUID in claim.gpu_uuids
        )
        mps_after = self._mps_authority_for_audit(client)
        authorized_after = mps_after.server_pids
        clients_after = frozenset(
            client.client_pid for client in mps_after.clients
        )

        if docker_after:
            raise DevGpuSessionError(
                "Docker declared GPU1 during DFT-only stabilization"
            )
        core_before = self._mps_authority_core(mps_before)
        core_after = self._mps_authority_core(mps_after)
        lazy_server_appeared = (
            core_after != core_before
            and self._mps_control_declarers(mps_before)
            == self._mps_control_declarers(mps_after)
            and len(self._mps_control_declarers(mps_before)) == 1
            and not authorized_before
            and len(authorized_after) == 1
            and not mps_before.clients
        )
        server_declarers_before = self._mps_server_declarers(mps_before)
        server_declarers_after = self._mps_server_declarers(mps_after)
        verified_before_servers = frozenset(
            pid
            for pid in authorized_before & compute_before
            if compute_before_declarers.get(pid)
            == server_declarers_before.get(pid)
        )
        verified_after_servers = frozenset(
            pid
            for pid in authorized_after & compute_after
            if compute_after_declarers.get(pid)
            == server_declarers_after.get(pid)
        )
        verified_lazy_before_servers = frozenset(
            pid
            for pid in authorized_after & compute_before
            if lazy_server_appeared
            and compute_before_declarers.get(pid)
            == server_declarers_after.get(pid)
        )
        observed_before = (
            compute_before
            - verified_before_servers
            - verified_lazy_before_servers
        ) | clients_before
        observed_after = (
            compute_after - verified_after_servers
        ) | clients_after
        for pid in observed_before | observed_after:
            if not process_is_exact_dft_residency_descendant(
                pid,
                lease,
                index=GPU_INDEX,
                uuid=GPU_UUID,
            ):
                raise DevGpuSessionError(
                    f"foreign PID {pid} appeared during DFT stabilization"
                )
        known_mps_churn = False
        if core_after != core_before:
            if lazy_server_appeared:
                known_mps_churn = True
            else:
                raise DevGpuSessionError(
                    "descriptor-owned MPS authority changed during DFT audit"
                )
        if mps_after.clients != mps_before.clients:
            if mps_before.clients < mps_after.clients:
                known_mps_churn = True
            else:
                raise DevGpuSessionError(
                    "MPS client identity disappeared or changed during DFT audit"
                )
        # Revalidate the root even when NVML and MPS report no DFT children.
        if not process_is_exact_dft_residency_descendant(
            lease.workload_pid,
            lease,
            index=GPU_INDEX,
            uuid=GPU_UUID,
        ):
            raise DevGpuSessionError("DFT residency root identity changed")
        if query_gpu_inventory().get(GPU_INDEX) != GPU_UUID:
            raise DevGpuSessionError("physical GPU1 identity changed")
        require_gpu1_default_compute_mode()
        final = client.status()
        if broker_authority_token(final) != expected_token:
            raise _AuditRoundChanged(
                "Broker authority changed during the DFT fast audit"
            )

        self.fast_audit_sequence += 1
        self.audit_mode = "fast"
        self._state(
            "stabilizing",
            contaminated=False,
            broker_draining=False,
            authorized_mps_pids=sorted(authorized_after),
            dft_churn_elapsed_seconds=round(
                time.monotonic() - self.dft_churn_started_at,
                3,
            ),
            mps_membership_changed=known_mps_churn,
        )
        return True

    def _audit(self, client: Any) -> tuple[dict[str, Any], TargetSnapshot, tuple[str, ...]]:
        from ops.gpu_broker.broker import BrokerError

        started = time.monotonic()
        preactivation_rollout = (
            self.plane_ready_published
            and self.dft_stabilized
            and self.activation_generation == 0
        )

        def retryable_docker_rollout_churn(
            exc: BrokerError,
            *,
            captured_activation: int,
        ) -> bool:
            return (
                preactivation_rollout
                and self.plane_ready_published
                and self.dft_stabilized
                and captured_activation == 0
                and self.activation_generation == 0
                and type(exc) is BrokerError
                and exc.code == "gpu_claim_inventory_unavailable"
                and len(exc.args) == 1
                and exc.args[0] in _PREACTIVATION_DOCKER_CHURN_MESSAGES
            )

        # MD runtime-probe acquire/release and Backend residency activation can
        # span several complete host inventories.  These extra attempts only
        # discard changed rounds; the unchanged full classifier still decides
        # whether any captured GPU authority is allowed.
        authority_change_attempts = (
            PREACTIVATION_ROLLOUT_AUDIT_ATTEMPTS
            if preactivation_rollout
            else FULL_AUDIT_ATTEMPTS
        )
        audit_attempts = (
            2048 if self.dft_warmup_open else authority_change_attempts
        )
        for round_index in range(audit_attempts):
            captured_activation = self.activation_generation
            captured_stabilization = self.dft_stabilization_generation
            pending_dft_seal = (
                self.dft_warmup_open
                and not self.dft_stabilized
                and captured_stabilization > 0
                and captured_stabilization
                > self.last_audit_stabilization_generation
            )
            fast_guard = None
            churn_retries = 8
            churn_timeout = STEADY_CHURN_TIMEOUT_SECONDS
            if self.dft_warmup_open:
                fast_guard = lambda status: self._fast_dft_churn_guard(
                    client,
                    status,
                )
                churn_retries = 2048
                churn_timeout = DFT_WARMUP_CHURN_TIMEOUT_SECONDS
            try:
                # The MPS identity must enclose the whole host inventory.  A
                # server that appears only after NVML/systemd were sampled may
                # reuse a foreign PID and must never retroactively authorize it.
                enclosing_broker_token = broker_authority_token(client.status())
                mps_before = self._mps_authority_for_audit(client)
                try:
                    status, snapshot = consistent_broker_snapshot(
                        client,
                        membership_churn_retries=churn_retries,
                        membership_churn_timeout_seconds=churn_timeout,
                        membership_churn_guard=fast_guard,
                    )
                except BrokerError as exc:
                    if retryable_docker_rollout_churn(
                        exc,
                        captured_activation=captured_activation,
                    ):
                        raise _AuditRoundChanged(
                            "Docker inventory changed during preactivation rollout"
                        ) from exc
                    raise
                if broker_authority_token(status) != enclosing_broker_token:
                    raise _AuditRoundChanged(
                        "Broker authority changed after the initial MPS seal"
                    )
                if (
                    self.dft_warmup_open
                    and self.dft_churn_started_at is not None
                    and time.monotonic() - self.dft_churn_started_at
                    >= DFT_WARMUP_CHURN_TIMEOUT_SECONDS
                ):
                    raise DevGpuSessionError(
                        "exact DFT residency did not stabilize within 90 seconds"
                    )
                expected_token = broker_authority_token(status)
                mps_after_inventory = self._mps_authority_for_audit(client)
                mps_inventory_changed = mps_after_inventory != mps_before
                exact_lazy_server_growth = (
                    mps_inventory_changed
                    and self._exact_dft_lazy_mps_server_growth(
                        status,
                        mps_before,
                        mps_after_inventory,
                    )
                )
                clients_before = frozenset(
                    client.client_pid for client in mps_before.clients
                )
                observed_initial_clients = clients_before | frozenset(
                    client.client_pid
                    for client in mps_after_inventory.clients
                )

                def classify_initial_inventory(authority: Any) -> tuple[
                    frozenset[int],
                    frozenset[int],
                    frozenset[tuple[str, str, str]],
                    tuple[str, ...],
                    frozenset[int],
                ]:
                    (
                        classified_nvml,
                        classified_clients,
                        classified_systemd_claims,
                    ) = self._managed_workload_authority(
                        status,
                        snapshot,
                        authorized_mps_declarers=authority.gpu_declarers,
                        authorized_mps_client_pids=observed_initial_clients,
                        authorized_mps_server_pids=authority.server_pids,
                    )
                    classified_expected_declarers = (
                        authority.gpu_declarers
                        | frozenset(
                            declarer
                            for claim in snapshot.systemd_claims
                            if (
                                claim.scope,
                                claim.unit,
                                claim.control_group,
                            )
                            in classified_systemd_claims
                            for declarer in claim.live_gpu_declarers
                        )
                    )
                    classified_identity_mismatches = (
                        self._captured_pid_identity_mismatches(
                            authority.server_pids | classified_nvml,
                            frozenset(snapshot.process_pids),
                            snapshot.process_declarers,
                            classified_expected_declarers,
                        )
                    )
                    classified_reasons: tuple[str, ...] = ()
                    if self.dft_warmup_open and snapshot.docker_claims:
                        classified_reasons = (
                            "Docker declared GPU1 during DFT-only stabilization",
                        )
                    classified_reasons = (
                        *classified_reasons,
                        *foreign_gpu1_reasons(
                            snapshot,
                            authorized_mps_pids=authority.server_pids,
                            managed_workload_pids=classified_nvml,
                            managed_systemd_claims=classified_systemd_claims,
                        ),
                    )
                    if (
                        self.dft_warmup_open
                        and classified_identity_mismatches
                    ):
                        classified_reasons = (
                            *classified_reasons,
                            "managed GPU1 process identity changed after NVML capture: "
                            "PID(s) "
                            + ",".join(
                                map(str, sorted(classified_identity_mismatches))
                            ),
                        )
                    classified_unknown_clients = (
                        observed_initial_clients - classified_clients
                    )
                    if classified_unknown_clients and not (
                        self.dft_warmup_open
                        and self._exact_dft_descendants(
                            status,
                            classified_unknown_clients,
                        )
                    ):
                        classified_reasons = (
                            *classified_reasons,
                            "unknown private MPS client PID(s): "
                            + ",".join(
                                map(str, sorted(classified_unknown_clients))
                            ),
                        )
                    return (
                        classified_nvml,
                        classified_clients,
                        classified_systemd_claims,
                        classified_reasons,
                        classified_unknown_clients,
                    )

                inventory_authority = mps_before
                initial_classification = classify_initial_inventory(
                    inventory_authority
                )
                reasons = initial_classification[3]
                unknown_mps_clients = initial_classification[4]
                if exact_lazy_server_growth and reasons:
                    after_classification = classify_initial_inventory(
                        mps_after_inventory
                    )
                    lazy_server_declarers = frozenset(
                        self._mps_server_declarers(
                            mps_after_inventory
                        ).values()
                    )
                    after_managed_systemd_claims = after_classification[2]
                    lazy_server_bound_to_managed_claim = (
                        bool(lazy_server_declarers)
                        and any(
                            (
                                claim.scope,
                                claim.unit,
                                claim.control_group,
                            )
                            in after_managed_systemd_claims
                            and self._mps_declarers_bound_to_claim(
                                mps_after_inventory.gpu_declarers,
                                claim,
                            )
                            for claim in snapshot.systemd_claims
                        )
                    )
                    lazy_server_is_unclaimed_sibling = (
                        not after_classification[3]
                        and self._mps_server_declarers_are_user_manager_siblings(
                            lazy_server_declarers
                        )
                    )
                    if (
                        lazy_server_bound_to_managed_claim
                        or lazy_server_is_unclaimed_sibling
                    ):
                        inventory_authority = mps_after_inventory
                        reasons = after_classification[3]
                        unknown_mps_clients = after_classification[4]
                if len(inventory_authority.server_pids) > 1:
                    raise DevGpuSessionError(
                        "full audit found multiple descriptor-owned MPS servers"
                    )
                if reasons:
                    self.audit_sequence += 1
                    self.audit_mode = "full"
                    self.last_audit_duration = time.monotonic() - started
                    return status, snapshot, reasons
                if mps_inventory_changed:
                    if exact_lazy_server_growth:
                        self._fast_dft_churn_guard(client, status)
                        raise _ExactDftTrailingChurn(
                            "exact DFT MPS server appeared across the full inventory"
                        )
                    if self._exact_dft_mps_client_growth(
                        status,
                        mps_before,
                        mps_after_inventory,
                    ):
                        self._fast_dft_churn_guard(client, status)
                        raise _ExactDftTrailingChurn(
                            "exact DFT MPS clients grew across the full inventory"
                        )
                    raise _AuditRoundChanged(
                        "MPS authority changed across the full host inventory"
                    )
                if unknown_mps_clients:
                    # A stable client omitted from the full systemd inventory
                    # is uncertainty, not monotonic warmup growth.
                    raise _AuditRoundChanged(
                        "known DFT MPS membership changed after full inventory"
                    )

                from ops.gpu_broker.server import (
                    SystemdMembershipChanged,
                    SystemdProcessDisappeared,
                    query_compute_processes,
                    query_docker_gpu_claims,
                    query_systemd_gpu_claims,
                )

                trailing_process_map = query_compute_processes()
                unsealed_compute = frozenset(
                    trailing_process_map.get(GPU_UUID, frozenset())
                )
                unsealed_process_declarers = (
                    capture_compute_process_declarers(unsealed_compute)
                )
                try:
                    trailing_docker = tuple(
                        claim
                        for claim in query_docker_gpu_claims()
                        if GPU_UUID in claim.gpu_uuids
                    )
                except BrokerError as exc:
                    if retryable_docker_rollout_churn(
                        exc,
                        captured_activation=captured_activation,
                    ):
                        raise _AuditRoundChanged(
                            "Docker inventory changed during preactivation rollout"
                        ) from exc
                    raise
                try:
                    trailing_systemd = tuple(
                        claim
                        for claim in query_systemd_gpu_claims(
                            compute_processes=trailing_process_map,
                        )
                        if GPU_UUID in claim.gpu_uuids
                    )
                except (
                    SystemdProcessDisappeared,
                    SystemdMembershipChanged,
                ) as exc:
                    transition_status = client.status()
                    if _is_exact_managed_scope_membership_transition(
                        exc,
                        status,
                        transition_status,
                    ):
                        raise _AuditRoundChanged(
                            "one exact managed scope changed during the "
                            "trailing systemd audit"
                        ) from exc
                    raise
                sealed_process_map = query_compute_processes()
                sealed_compute = frozenset(
                    sealed_process_map.get(GPU_UUID, frozenset())
                )
                sealed_membership_changed = sealed_compute != unsealed_compute
                sealed_process_declarers = capture_compute_process_declarers(
                    sealed_compute
                )
                trailing_compute = sealed_compute
                mps_after = self._mps_authority_for_audit(client)
                authorized_after = mps_after.server_pids
                clients_after = frozenset(
                    client.client_pid for client in mps_after.clients
                )
                require_gpu1_default_compute_mode()
                final_status = client.status()
                if broker_authority_token(final_status) != expected_token:
                    raise _AuditRoundChanged(
                        "Broker authority changed during trailing full audit"
                    )
                trailing_mps_changed = mps_after != mps_before
                exact_trailing_lazy_server_growth = (
                    trailing_mps_changed
                    and self._exact_dft_lazy_mps_server_growth(
                        final_status,
                        mps_before,
                        mps_after,
                    )
                )

                initial_compute = frozenset(snapshot.process_pids)
                observed_trailing_compute = (
                    unsealed_compute | sealed_compute
                )
                trailing_snapshot = TargetSnapshot(
                    tuple(sorted(observed_trailing_compute)),
                    trailing_docker,
                    trailing_systemd,
                    tuple(
                        sorted(
                            frozenset(
                                (
                                    *unsealed_process_declarers.values(),
                                    *sealed_process_declarers.values(),
                                )
                            ),
                            key=lambda declarer: (
                                declarer.pid,
                                declarer.process_start_ticks,
                                declarer.process_cgroup,
                            ),
                        )
                    ),
                )
                systemd_changed = tuple(
                    _claim_fingerprint(claim)
                    for claim in snapshot.systemd_claims
                ) != tuple(
                    _claim_fingerprint(claim)
                    for claim in trailing_systemd
                )
                after_server_declarers = self._mps_server_declarers(mps_after)
                verified_lazy_server_pids = frozenset(
                    pid
                    for pid in authorized_after
                    & (sealed_compute - unsealed_compute)
                    if exact_trailing_lazy_server_growth
                    and sealed_process_declarers.get(pid)
                    == after_server_declarers.get(pid)
                )

                def classify_trailing_inventory(
                    authority: Any,
                    extra_authorized_mps_pids: frozenset[int] = frozenset(),
                    extra_authorized_mps_declarers: frozenset[Any] = frozenset(),
                ) -> tuple[
                    frozenset[int],
                    bool,
                    frozenset[tuple[str, str, str]],
                    tuple[str, ...],
                    frozenset[int],
                ]:
                    classified_authorized_pids = (
                        authority.server_pids | extra_authorized_mps_pids
                    )
                    classified_additions = (
                        (observed_trailing_compute - initial_compute)
                        | (clients_after - clients_before)
                    ) - classified_authorized_pids
                    classified_additions_are_exact = (
                        bool(classified_additions)
                        and self.dft_warmup_open
                        and self._exact_dft_descendants(
                            final_status,
                            classified_additions,
                        )
                    )
                    (
                        classified_managed_nvml,
                        classified_managed_clients,
                        classified_managed_systemd_claims,
                    ) = self._managed_workload_authority(
                        final_status,
                        trailing_snapshot,
                        authorized_mps_declarers=authority.gpu_declarers,
                        authorized_mps_client_pids=clients_after,
                        authorized_mps_server_pids=authority.server_pids,
                    )
                    classified_expected_declarers = (
                        authority.gpu_declarers
                        | extra_authorized_mps_declarers
                        | frozenset(
                            declarer
                            for claim in trailing_systemd
                            if (
                                claim.scope,
                                claim.unit,
                                claim.control_group,
                            )
                            in classified_managed_systemd_claims
                            for declarer in claim.live_gpu_declarers
                        )
                    )
                    classified_allowed_pids = (
                        classified_authorized_pids | classified_managed_nvml
                    )
                    if classified_additions_are_exact:
                        classified_allowed_pids |= classified_additions
                        expected_pids = frozenset(
                            declarer.pid
                            for declarer in classified_expected_declarers
                        )
                        missing_addition_declarers = (
                            classified_additions - expected_pids
                        )
                        classified_expected_declarers |= frozenset(
                            capture_compute_process_declarers(
                                missing_addition_declarers
                            ).values()
                        )
                    classified_identity_mismatches = (
                        self._captured_pid_identity_mismatches(
                            classified_allowed_pids,
                            unsealed_compute,
                            tuple(unsealed_process_declarers.values()),
                            classified_expected_declarers,
                        )
                        | self._captured_pid_identity_mismatches(
                            classified_allowed_pids,
                            sealed_compute,
                            tuple(sealed_process_declarers.values()),
                            classified_expected_declarers,
                        )
                    )
                    classified_reasons = foreign_gpu1_reasons(
                        trailing_snapshot,
                        authorized_mps_pids=classified_authorized_pids,
                        managed_workload_pids=(
                            classified_managed_nvml | classified_additions
                            if classified_additions_are_exact
                            else classified_managed_nvml
                        ),
                        managed_systemd_claims=(
                            classified_managed_systemd_claims
                        ),
                    )
                    if self.dft_warmup_open and trailing_docker:
                        classified_reasons = (
                            "Docker declared GPU1 during DFT-only stabilization",
                            *classified_reasons,
                        )
                    if (
                        self.dft_warmup_open
                        and classified_identity_mismatches
                    ):
                        classified_reasons = (
                            *classified_reasons,
                            "managed GPU1 process identity changed after NVML capture: "
                            "PID(s) "
                            + ",".join(
                                map(str, sorted(classified_identity_mismatches))
                            ),
                        )
                    classified_unknown_clients = (
                        clients_after - classified_managed_clients
                    )
                    if classified_additions_are_exact:
                        classified_unknown_clients -= classified_additions
                    if classified_unknown_clients:
                        classified_reasons = (
                            *classified_reasons,
                            "unknown private MPS client PID(s): "
                            + ",".join(
                                map(str, sorted(classified_unknown_clients))
                            ),
                        )
                    return (
                        classified_additions,
                        classified_additions_are_exact,
                        classified_managed_systemd_claims,
                        classified_reasons,
                        classified_unknown_clients,
                    )

                if exact_trailing_lazy_server_growth:
                    trailing_classification = classify_trailing_inventory(
                        mps_before,
                        verified_lazy_server_pids,
                        frozenset(
                            after_server_declarers[pid]
                            for pid in verified_lazy_server_pids
                        ),
                    )
                    if trailing_classification[3]:
                        after_classification = classify_trailing_inventory(
                            mps_after
                        )
                        after_managed_systemd_claims = after_classification[2]
                        after_declarers_bound_to_managed_claim = any(
                            (
                                claim.scope,
                                claim.unit,
                                claim.control_group,
                            )
                            in after_managed_systemd_claims
                            and self._mps_declarers_bound_to_claim(
                                mps_after.gpu_declarers,
                                claim,
                            )
                            for claim in trailing_systemd
                        )
                        after_server_is_unclaimed_sibling = (
                            not after_classification[3]
                            and self._mps_server_declarers_are_user_manager_siblings(
                                frozenset(after_server_declarers.values())
                            )
                        )
                        if (
                            after_declarers_bound_to_managed_claim
                            or after_server_is_unclaimed_sibling
                        ):
                            trailing_classification = after_classification
                else:
                    trailing_classification = classify_trailing_inventory(
                        mps_after
                    )
                (
                    added_processes,
                    additions_are_exact_dft,
                    _trailing_managed_systemd_claims,
                    trailing_reasons,
                    _unknown_trailing_clients,
                ) = trailing_classification
                if trailing_reasons:
                    self.audit_sequence += 1
                    self.audit_mode = "full"
                    self.last_audit_duration = time.monotonic() - started
                    return status, snapshot, trailing_reasons

                docker_changed = tuple(
                    _claim_fingerprint(claim) for claim in snapshot.docker_claims
                ) != tuple(
                    _claim_fingerprint(claim) for claim in trailing_docker
                )
                if sealed_membership_changed:
                    sealed_additions = sealed_compute - unsealed_compute
                    sealed_lazy_dft_additions = (
                        sealed_additions - verified_lazy_server_pids
                    )
                    sealed_exact_lazy_growth = (
                        unsealed_compute < sealed_compute
                        and exact_trailing_lazy_server_growth
                        and verified_lazy_server_pids == authorized_after
                        and (
                            not sealed_lazy_dft_additions
                            or self._exact_dft_descendants(
                                final_status,
                                sealed_lazy_dft_additions,
                            )
                        )
                    )
                    if (
                        unsealed_compute < sealed_compute
                        and self.dft_warmup_open
                        and (
                            sealed_exact_lazy_growth
                            or self._exact_dft_descendants(
                                final_status,
                                sealed_additions,
                            )
                        )
                    ):
                        self._fast_dft_churn_guard(client, final_status)
                        raise _ExactDftTrailingChurn(
                            "exact DFT/MPS NVML membership grew across systemd audit"
                        )
                    raise _AuditRoundChanged(
                        "GPU1 NVML membership changed across trailing systemd audit"
                    )
                if trailing_mps_changed:
                    if exact_trailing_lazy_server_growth:
                        self._fast_dft_churn_guard(client, final_status)
                        raise _ExactDftTrailingChurn(
                            "exact DFT MPS server appeared across the trailing audit"
                        )
                    if self._exact_dft_mps_client_growth(
                        final_status,
                        mps_before,
                        mps_after,
                    ):
                        self._fast_dft_churn_guard(client, final_status)
                        raise _ExactDftTrailingChurn(
                            "exact DFT MPS clients grew across the trailing audit"
                        )
                    raise _AuditRoundChanged(
                        "MPS authority changed across the trailing full audit"
                    )
                if systemd_changed:
                    if self.dft_warmup_open:
                        self._fast_dft_churn_guard(client, final_status)
                        raise _ExactDftTrailingChurn(
                            "exact DFT systemd claims changed during trailing audit"
                        )
                    raise _AuditRoundChanged(
                        "GPU1 systemd claims changed during trailing full audit"
                    )
                if (
                    initial_compute != trailing_compute
                    or docker_changed
                ):
                    if (
                        not docker_changed
                        and initial_compute < trailing_compute
                        and additions_are_exact_dft
                    ):
                        self._fast_dft_churn_guard(client, final_status)
                        raise _ExactDftTrailingChurn(
                            "exact DFT NVML membership grew during trailing audit"
                        )
                    raise _AuditRoundChanged(
                        "GPU1 membership changed during trailing full audit"
                    )

                if (
                    pending_dft_seal
                    and captured_stabilization
                    == self.dft_stabilization_generation
                ):
                    sealing_lease = self._only_exact_dft_residency(final_status)
                    if len(authorized_after) != 1:
                        raise DevGpuSessionError(
                            "DFT stabilization lacks one descriptor-owned MPS server"
                        )
                    sealing_server_pid = next(iter(authorized_after))
                    if sealing_lease.workload_pid not in clients_after:
                        raise DevGpuSessionError(
                            "DFT residency root lacks an active private MPS client"
                        )
                    if (
                        sealing_server_pid not in initial_compute
                        or sealing_server_pid not in trailing_compute
                    ):
                        raise DevGpuSessionError(
                            "DFT MPS server is absent from the stable NVML inventory"
                        )
                    if not self._exact_dft_descendants(
                        final_status,
                        frozenset({sealing_lease.workload_pid}),
                    ):
                        raise DevGpuSessionError(
                            "DFT residency root died or changed before stabilization"
                        )
                previous_mask = signal.pthread_sigmask(
                    signal.SIG_BLOCK,
                    {signal.SIGUSR1, signal.SIGUSR2},
                )
                try:
                    self.audit_sequence += 1
                    self.full_audit_generation += 1
                    self.audit_mode = "full"
                    self.last_mps_authority = mps_after
                    self.last_audit_activation_generation = captured_activation
                    self.last_audit_stabilization_generation = captured_stabilization
                    if (
                        pending_dft_seal
                        and captured_stabilization
                        == self.dft_stabilization_generation
                    ):
                        self.dft_stabilized = True
                        self.dft_warmup_open = False
                        self.dft_churn_started_at = None
                finally:
                    signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
                self.last_audit_duration = time.monotonic() - started
                return final_status, snapshot, ()
            except _ExactDftTrailingChurn:
                if round_index + 1 >= audit_attempts:
                    raise DevGpuSessionError(
                        "exact DFT residency did not stabilize within 90 seconds"
                    )
                continue
            except _AuditRoundChanged:
                if round_index + 1 >= authority_change_attempts:
                    raise DevGpuSessionError(
                        "GPU1 authority changed throughout trailing full audits"
                    )
                continue
        raise AssertionError("bounded full audit loop did not terminate")

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
                if not reasons and self.last_mps_authority is None:
                    raise DevGpuSessionError(
                        "full audit did not retain its exact MPS authority"
                    )
            except Exception as exc:
                if self.stop_requested and not self.automatic_recovery:
                    # A graceful drain may arrive while a long host audit is
                    # in flight.  Its now-stale result must not race the shell
                    # teardown into automatic-recovery mode.
                    continue
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
            if self.stop_requested and not self.automatic_recovery:
                # Admission is already closed and the controller owns cleanup;
                # do not classify a result captured across the drain edge.
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
            previous_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK,
                {signal.SIGUSR1, signal.SIGUSR2},
            )
            try:
                activation_audited = (
                    self.dft_stabilized
                    and self.activation_generation > 0
                    and self.last_audit_activation_generation
                    == self.activation_generation
                )
                phase = "ready" if activation_audited else "plane-ready"
                self._state(
                    phase,
                    authorized_mps_pids=sorted(
                        self.last_mps_authority.server_pids
                    ),
                    contaminated=False,
                    audit_sequence=self.audit_sequence,
                    audit_duration_seconds=round(
                        self.last_audit_duration,
                        3,
                    ),
                    audit_heartbeat_monotonic=time.monotonic(),
                )
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
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
        def request_activation(*_args: Any) -> None:
            self.activation_generation += 1

        def request_dft_stabilization(*_args: Any) -> None:
            self.dft_stabilization_generation += 1

        signal.signal(signal.SIGUSR1, request_activation)
        signal.signal(signal.SIGUSR2, request_dft_stabilization)
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
            previous_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK,
                {signal.SIGUSR1, signal.SIGUSR2},
            )
            try:
                if self.last_mps_authority is None:
                    raise DevGpuSessionError(
                        "startup audit did not retain its MPS authority"
                    )
                self._state(
                    "plane-ready",
                    authorized_mps_pids=sorted(
                        self.last_mps_authority.server_pids
                    ),
                    contaminated=False,
                    audit_sequence=self.audit_sequence,
                )
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
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
    lock_descriptor = _open_private_lock(CONTROLLER_START_LOCK)
    try:
        try:
            fcntl.flock(
                lock_descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            raise DevGpuSessionError(
                "GPU session controller startup is already in progress"
            ) from exc
        return _up_execute_locked()
    finally:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(lock_descriptor)


def _up_execute_locked() -> dict[str, Any]:
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
    if not CONTROLLER_RECORD.exists() and not CONTROLLER_RECORD.is_symlink():
        stopped = status()
        expected_stopped = {
            "schema_version": 1,
            "status": "stopped",
            "gpu_index": 1,
        }
        if stopped == expected_stopped:
            return stopped
        raise DevGpuSessionError("stopped controller state is invalid")
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
            # The controller deliberately keeps retrying while an exact owned
            # Worker releases its lease.  Waiting preserves that graceful
            # teardown path; foreign/unknown clients remain isolated by the
            # controller and can never be signalled here.
            time.sleep(0.25)
            continue
        time.sleep(0.25)
    raise DevGpuSessionError("timed out waiting for GPU session cleanup")


def _state_generation(state: dict[str, Any], key: str) -> int:
    value = state.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DevGpuSessionError(f"controller {key} is invalid")
    return value


def stabilize_execute(session_id: str) -> dict[str, Any]:
    """Seal DFT warmup only after a post-request authoritative full audit."""

    if re.fullmatch(r"[0-9a-f]{32}", session_id) is None:
        raise DevGpuSessionError("stabilization requires an exact session identity")
    record = _controller_record()
    if record.get("session_id") != session_id:
        raise DevGpuSessionError("stabilization session identity differs")
    current = status()
    if (
        current.get("dft_stabilized") is True
        and current.get("status") == "plane-ready"
        and current.get("audit_mode") == "full"
    ):
        return current
    if current.get("status") not in {"plane-ready", "stabilizing"}:
        raise DevGpuSessionError("controller plane is not stabilizing DFT")
    if _state_generation(current, "activation_generation") != 0:
        raise DevGpuSessionError("DFT must stabilize before activation")
    baseline_full = _state_generation(current, "full_audit_generation")
    baseline_request = _state_generation(
        current,
        "dft_stabilization_generation",
    )
    pid = record["pid"]
    descriptor = os.pidfd_open(pid)
    try:
        if _controller_record() != record:
            raise DevGpuSessionError(
                "controller identity changed before DFT stabilization"
            )
        signal.pidfd_send_signal(descriptor, signal.SIGUSR2)
    finally:
        os.close(descriptor)
    deadline = time.monotonic() + DFT_WARMUP_CHURN_TIMEOUT_SECONDS + 15.0
    while time.monotonic() < deadline:
        current = status()
        if (
            current.get("status") == "plane-ready"
            and current.get("audit_mode") == "full"
            and current.get("dft_stabilized") is True
            and _state_generation(current, "full_audit_generation")
            > baseline_full
            and _state_generation(current, "dft_stabilization_generation")
            > baseline_request
            and _state_generation(
                current,
                "last_audit_stabilization_generation",
            )
            == _state_generation(
                current,
                "dft_stabilization_generation",
            )
        ):
            return current
        if current.get("status") not in {
            "plane-ready",
            "stabilizing",
            "auditing",
        }:
            raise DevGpuSessionError(
                f"DFT stabilization ended as {current.get('status')}"
            )
        time.sleep(0.1)
    raise DevGpuSessionError(
        "timed out waiting for a post-DFT full controller audit"
    )


def activate_execute(session_id: str) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{32}", session_id) is None:
        raise DevGpuSessionError("activation requires an exact session identity")
    record = _controller_record()
    if record.get("session_id") != session_id:
        raise DevGpuSessionError("activation session identity differs")
    current = status()
    if (
        current.get("status") != "plane-ready"
        or current.get("audit_mode") != "full"
        or current.get("dft_stabilized") is not True
    ):
        raise DevGpuSessionError("controller plane is not awaiting activation")
    baseline_full = _state_generation(current, "full_audit_generation")
    baseline_activation = _state_generation(current, "activation_generation")
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
        if (
            current.get("status") == "ready"
            and current.get("audit_mode") == "full"
            and _state_generation(current, "full_audit_generation")
            > baseline_full
            and _state_generation(current, "activation_generation")
            > baseline_activation
            and _state_generation(
                current,
                "last_audit_activation_generation",
            )
            == _state_generation(current, "activation_generation")
        ):
            return current
        if current.get("status") not in {
            "plane-ready",
            "auditing",
            "ready",
        }:
            raise DevGpuSessionError(
                f"controller activation ended as {current.get('status')}"
            )
        time.sleep(0.1)
    raise DevGpuSessionError("timed out waiting for controller activation")


def drain_execute() -> dict[str, Any]:
    record = _controller_record()
    from gpu_resource import GpuBrokerClient

    pid = record["pid"]
    descriptor = os.pidfd_open(pid)
    try:
        if process_start_ticks(pid) != record["start_ticks"]:
            raise DevGpuSessionError("controller PID was reused before drain")
        result = GpuBrokerClient(GPU_ROOT / "broker.sock").set_draining(True)
        if result.get("draining") is not True or not isinstance(
            result.get("leases"), list
        ):
            raise DevGpuSessionError("Broker returned an invalid drain result")
        if _controller_record() != record:
            raise DevGpuSessionError("controller identity changed during drain")
        # Stop the audit loop before the shell removes exact session-owned
        # workers.  The controller remains the cleanup authority and waits for
        # their leases to drain; this prevents intentional teardown churn from
        # being misclassified as foreign contamination.
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
    finally:
        os.close(descriptor)
    deadline = time.monotonic() + 45.0
    while time.monotonic() < deadline:
        current = status()
        state = current.get("status")
        if state in {"cleanup-blocked", "isolation-waiting", "stopped"}:
            return result
        if state not in {
            "auditing",
            "plane-ready",
            "stabilizing",
            "ready",
        }:
            raise DevGpuSessionError(
                f"controller drain handshake ended as {state}"
            )
        time.sleep(0.1)
    raise DevGpuSessionError("timed out waiting for controller drain handshake")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("up", "status", "down", "drain", "stabilize", "activate", "serve"),
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
        elif args.command == "stabilize":
            if args.session_id is None:
                raise DevGpuSessionError("stabilize requires --session-id")
            result = stabilize_execute(args.session_id)
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

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Mapping


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PROJECT_RE = re.compile(
    r"^nexpoly_dft_fresh_[a-z0-9][a-z0-9_-]{0,40}$"
)
_PROC_FD_RE = re.compile(r"^/proc/([1-9][0-9]*)/fd/([0-9]+)$")
_IDENTITY_RE = re.compile(r"^([1-9][0-9]*|0):([1-9][0-9]*|0)$")
_AUTHORITY_KEYS = frozenset(
    {
        "NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY",
        "NEXPOLY_DFT_GPU_AUTHORITY_PID",
        "NEXPOLY_DFT_GPU_AUTHORITY_START_TICKS",
        "NEXPOLY_DFT_GPU_AUTHORITY_ROOT",
        "NEXPOLY_DFT_GPU_AUTHORITY_ROOT_IDENTITY",
        "NEXPOLY_DFT_GPU_RESERVATIONS_AUTHORITY",
        "NEXPOLY_DFT_GPU_RESERVATIONS_IDENTITY",
        "NEXPOLY_DFT_GPU_RESERVATIONS_SHA256",
        "NEXPOLY_DFT_GPU1_MPS_PIPE_AUTHORITY",
        "NEXPOLY_DFT_GPU1_MPS_PIPE_IDENTITY",
        "NEXPOLY_DFT_GPU3_MPS_PIPE_AUTHORITY",
        "NEXPOLY_DFT_GPU3_MPS_PIPE_IDENTITY",
    }
)


class FormalGpuAuthorityError(ValueError):
    """The fresh-acceptance descriptor authority is absent or unsafe."""


@dataclass(frozen=True, slots=True)
class FormalGpuAuthority:
    acceptance_project: str
    authority_sha: str
    process_id: int
    process_start_ticks: int
    root: Path
    reservations: Path
    reservations_sha256: str
    pipe_directories: tuple[tuple[int, Path], ...]
    root_identity: tuple[int, int]
    reservations_identity: tuple[int, int]
    pipe_directory_identities: tuple[tuple[int, tuple[int, int]], ...]


_PROCESS_AUTHORITY: FormalGpuAuthority | None = None
_PROCESS_AUTHORITY_DESCRIPTORS: tuple[int, ...] = ()


def _process_start_ticks(process_id: int) -> int:
    try:
        payload = Path(f"/proc/{process_id}/stat").read_text(
            encoding="ascii"
        )
    except OSError as exc:
        raise FormalGpuAuthorityError(
            "GPU descriptor authority process is unavailable"
        ) from exc
    close = payload.rfind(")")
    fields = payload[close + 2 :].split() if close >= 0 else []
    if len(fields) <= 19 or not fields[19].isdigit():
        raise FormalGpuAuthorityError(
            "GPU descriptor authority process identity is invalid"
        )
    return int(fields[19])


def _identity(raw: str, *, name: str) -> tuple[int, int]:
    match = _IDENTITY_RE.fullmatch(raw)
    if match is None:
        raise FormalGpuAuthorityError(f"{name} identity is invalid")
    return (int(match.group(1)), int(match.group(2)))


def _proc_fd(
    raw: str,
    *,
    process_id: int,
    name: str,
) -> Path:
    match = _PROC_FD_RE.fullmatch(raw)
    if (
        match is None
        or int(match.group(1)) != process_id
        or int(match.group(2)) <= 2
    ):
        raise FormalGpuAuthorityError(
            f"{name} must use the exact harness /proc PID/fd authority"
        )
    return Path(raw)


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int]:
    return (metadata.st_dev, metadata.st_ino)


def _require_identity(
    path: Path,
    expected: tuple[int, int],
    *,
    name: str,
    kind: str,
    mode: int,
    single_link: bool = False,
) -> os.stat_result:
    try:
        metadata = os.stat(path)
    except OSError as exc:
        raise FormalGpuAuthorityError(f"{name} is unavailable") from exc
    kind_ok = {
        "directory": stat.S_ISDIR(metadata.st_mode),
        "file": stat.S_ISREG(metadata.st_mode),
    }.get(kind, False)
    if (
        not kind_ok
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != mode
        or (single_link and metadata.st_nlink != 1)
        or _metadata_identity(metadata) != expected
    ):
        raise FormalGpuAuthorityError(f"{name} identity is unsafe")
    return metadata


def _bounded_file_digest(
    path: Path,
    expected: tuple[int, int],
    *,
    maximum_bytes: int = 1024 * 1024,
) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except OSError as exc:
        raise FormalGpuAuthorityError(
            "GPU reservation authority cannot be opened"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if (
            _metadata_identity(before) != expected
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > maximum_bytes
        ):
            raise FormalGpuAuthorityError(
                "GPU reservation authority changed"
            )
        payload = bytearray()
        while len(payload) <= maximum_bytes:
            chunk = os.read(
                descriptor,
                min(64 * 1024, maximum_bytes + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
        if (
            len(payload) > maximum_bytes
            or len(payload) != after.st_size
            or _metadata_identity(after) != expected
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ctime_ns != before.st_ctime_ns
        ):
            raise FormalGpuAuthorityError(
                "GPU reservation authority changed while it was read"
            )
        return hashlib.sha256(payload).hexdigest()
    finally:
        os.close(descriptor)


def _require_control_channel(pipe_directory: Path, *, index: int) -> None:
    control = pipe_directory / "control"
    try:
        metadata = os.lstat(control)
    except OSError as exc:
        raise FormalGpuAuthorityError(
            f"GPU{index} MPS control authority is unavailable"
        ) from exc
    if (
        metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or (
            not stat.S_ISFIFO(metadata.st_mode)
            and not stat.S_ISSOCK(metadata.st_mode)
        )
    ):
        raise FormalGpuAuthorityError(
            f"GPU{index} MPS control authority is unsafe"
        )


def load_formal_gpu_authority(
    environment: Mapping[str, str] | None = None,
    *,
    expected_reservations_file: Path | None = None,
    expected_root: Path | None = None,
    require: bool = False,
) -> FormalGpuAuthority | None:
    if _PROCESS_AUTHORITY is not None and environment is None:
        cached = _PROCESS_AUTHORITY
        if cached.process_id != os.getpid():
            raise FormalGpuAuthorityError(
                "process-local GPU descriptor authority cannot cross fork"
            )
        expected_pipes = dict(cached.pipe_directories)
        expected_pipe_identities = dict(
            cached.pipe_directory_identities
        )
        expected_environment = {
            "NEXPOLY_DFT_FORMAL_ACCEPTANCE": "1",
            "NEXPOLY_DFT_PROJECT_NAME": cached.acceptance_project,
            "NEXPOLY_DFT_AUTHORITY_SHA": cached.authority_sha,
            "NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY": "1",
            "NEXPOLY_DFT_GPU_AUTHORITY_PID": str(cached.process_id),
            "NEXPOLY_DFT_GPU_AUTHORITY_START_TICKS": str(
                cached.process_start_ticks
            ),
            "NEXPOLY_DFT_GPU_AUTHORITY_ROOT": str(cached.root),
            "NEXPOLY_DFT_GPU_AUTHORITY_ROOT_IDENTITY": (
                f"{cached.root_identity[0]}:{cached.root_identity[1]}"
            ),
            "NEXPOLY_DFT_GPU_RESERVATIONS_AUTHORITY": str(
                cached.reservations
            ),
            "NEXPOLY_DFT_GPU_RESERVATIONS_IDENTITY": (
                f"{cached.reservations_identity[0]}:"
                f"{cached.reservations_identity[1]}"
            ),
            "NEXPOLY_DFT_GPU_RESERVATIONS_SHA256": (
                cached.reservations_sha256
            ),
        }
        for index, path in expected_pipes.items():
            identity = expected_pipe_identities[index]
            expected_environment[
                f"NEXPOLY_DFT_GPU{index}_MPS_PIPE_AUTHORITY"
            ] = str(path)
            expected_environment[
                f"NEXPOLY_DFT_GPU{index}_MPS_PIPE_IDENTITY"
            ] = f"{identity[0]}:{identity[1]}"
        if any(
            os.environ.get(key) != value
            for key, value in expected_environment.items()
        ) or any(
            os.environ.get(
                f"NEXPOLY_DFT_GPU{index}_MPS_PIPE_AUTHORITY", ""
            )
            or os.environ.get(
                f"NEXPOLY_DFT_GPU{index}_MPS_PIPE_IDENTITY", ""
            )
            for index in ({1, 3} - set(expected_pipes))
        ):
            raise FormalGpuAuthorityError(
                "process-local GPU descriptor authority environment changed"
            )
        if (
            _process_start_ticks(cached.process_id)
            != cached.process_start_ticks
        ):
            raise FormalGpuAuthorityError(
                "process-local GPU descriptor authority process changed"
            )
        _require_identity(
            cached.root,
            cached.root_identity,
            name="process-local GPU root",
            kind="directory",
            mode=0o700,
        )
        _require_identity(
            cached.reservations,
            cached.reservations_identity,
            name="process-local GPU reservations",
            kind="file",
            mode=0o600,
            single_link=True,
        )
        if (
            _bounded_file_digest(
                cached.reservations,
                cached.reservations_identity,
            )
            != cached.reservations_sha256
        ):
            raise FormalGpuAuthorityError(
                "process-local GPU reservation authority changed"
            )
        for index, path in expected_pipes.items():
            _require_identity(
                path,
                expected_pipe_identities[index],
                name=f"process-local GPU{index} MPS pipe",
                kind="directory",
                mode=0o700,
            )
            _require_control_channel(path, index=index)
        try:
            broker_metadata = os.lstat(cached.root / "broker.sock")
        except OSError as exc:
            raise FormalGpuAuthorityError(
                "process-local GPU Broker authority is unavailable"
            ) from exc
        if (
            not stat.S_ISSOCK(broker_metadata.st_mode)
            or broker_metadata.st_uid != os.geteuid()
            or broker_metadata.st_gid != os.getegid()
        ):
            raise FormalGpuAuthorityError(
                "process-local GPU Broker authority is unsafe"
            )
        if expected_root is not None:
            try:
                root_metadata = os.lstat(expected_root)
            except OSError as exc:
                raise FormalGpuAuthorityError(
                    "exact development GPU root is unavailable"
                ) from exc
            if (
                stat.S_ISLNK(root_metadata.st_mode)
                or _metadata_identity(root_metadata)
                != cached.root_identity
            ):
                raise FormalGpuAuthorityError(
                    "process-local GPU root differs from development root"
                )
        if expected_reservations_file is not None:
            try:
                expected_payload = expected_reservations_file.read_bytes()
            except OSError as exc:
                raise FormalGpuAuthorityError(
                    "exact F GPU reservation policy is unavailable"
                ) from exc
            if (
                hashlib.sha256(expected_payload).hexdigest()
                != cached.reservations_sha256
            ):
                raise FormalGpuAuthorityError(
                    "process-local GPU reservations differ from exact F"
                )
        return cached
    values = os.environ if environment is None else environment
    enabled = values.get(
        "NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY", ""
    )
    populated = {
        key for key in _AUTHORITY_KEYS if values.get(key, "") != ""
    }
    if enabled != "1":
        if require or populated:
            raise FormalGpuAuthorityError(
                "GPU descriptor authority is incomplete or not enabled"
            )
        return None
    if values.get("NEXPOLY_DFT_FORMAL_ACCEPTANCE") != "1":
        raise FormalGpuAuthorityError(
            "GPU descriptor authority is restricted to formal acceptance"
        )
    if _PROJECT_RE.fullmatch(
        values.get("NEXPOLY_DFT_PROJECT_NAME", "")
    ) is None or _SHA_RE.fullmatch(
        values.get("NEXPOLY_DFT_AUTHORITY_SHA", "")
    ) is None:
        raise FormalGpuAuthorityError(
            "GPU descriptor authority lacks the exact acceptance identity"
        )

    raw_pid = values.get("NEXPOLY_DFT_GPU_AUTHORITY_PID", "")
    raw_ticks = values.get(
        "NEXPOLY_DFT_GPU_AUTHORITY_START_TICKS", ""
    )
    if (
        not raw_pid.isdigit()
        or raw_pid.startswith("0")
        or not raw_ticks.isdigit()
        or raw_ticks.startswith("0")
    ):
        raise FormalGpuAuthorityError(
            "GPU descriptor authority process identity is invalid"
        )
    process_id = int(raw_pid)
    process_start_ticks = int(raw_ticks)
    try:
        process_metadata = os.stat(f"/proc/{process_id}")
    except OSError as exc:
        raise FormalGpuAuthorityError(
            "GPU descriptor authority process is unavailable"
        ) from exc
    if (
        process_metadata.st_uid != os.geteuid()
        or _process_start_ticks(process_id) != process_start_ticks
    ):
        raise FormalGpuAuthorityError(
            "GPU descriptor authority process changed"
        )

    root = _proc_fd(
        values.get("NEXPOLY_DFT_GPU_AUTHORITY_ROOT", ""),
        process_id=process_id,
        name="GPU root",
    )
    root_identity = _identity(
        values.get("NEXPOLY_DFT_GPU_AUTHORITY_ROOT_IDENTITY", ""),
        name="GPU root",
    )
    _require_identity(
        root,
        root_identity,
        name="GPU root",
        kind="directory",
        mode=0o700,
    )
    if expected_root is None:
        raise FormalGpuAuthorityError(
            "exact development GPU root was not supplied"
        )
    try:
        expected_root_metadata = os.lstat(expected_root)
    except OSError as exc:
        raise FormalGpuAuthorityError(
            "exact development GPU root is unavailable"
        ) from exc
    if (
        stat.S_ISLNK(expected_root_metadata.st_mode)
        or not stat.S_ISDIR(expected_root_metadata.st_mode)
        or expected_root_metadata.st_uid != os.geteuid()
        or expected_root_metadata.st_gid != os.getegid()
        or stat.S_IMODE(expected_root_metadata.st_mode) != 0o700
        or _metadata_identity(expected_root_metadata) != root_identity
    ):
        raise FormalGpuAuthorityError(
            "GPU root authority differs from the exact development root"
        )
    try:
        root_target = os.readlink(root)
    except OSError as exc:
        raise FormalGpuAuthorityError(
            "GPU root authority target cannot be read"
        ) from exc
    forbidden_roots = (
        "/data/lzq/gith/nexpoly",
        "/data/lzq/gith/nexpoly-runtime",
    )
    if any(
        root_target == forbidden
        or root_target.startswith(forbidden + "/")
        for forbidden in forbidden_roots
    ):
        raise FormalGpuAuthorityError(
            "GPU root authority references the production repository"
        )

    reservations = _proc_fd(
        values.get("NEXPOLY_DFT_GPU_RESERVATIONS_AUTHORITY", ""),
        process_id=process_id,
        name="GPU reservations",
    )
    reservations_identity = _identity(
        values.get("NEXPOLY_DFT_GPU_RESERVATIONS_IDENTITY", ""),
        name="GPU reservations",
    )
    _require_identity(
        reservations,
        reservations_identity,
        name="GPU reservations",
        kind="file",
        mode=0o600,
        single_link=True,
    )
    try:
        reservations_relation = os.stat(
            root / "external-reservations.json"
        )
    except OSError as exc:
        raise FormalGpuAuthorityError(
            "GPU reservations escaped the development root"
        ) from exc
    if _metadata_identity(reservations_relation) != reservations_identity:
        raise FormalGpuAuthorityError(
            "GPU reservations authority escaped its root hierarchy"
        )
    reservations_sha256 = values.get(
        "NEXPOLY_DFT_GPU_RESERVATIONS_SHA256", ""
    )
    if _SHA256_RE.fullmatch(reservations_sha256) is None:
        raise FormalGpuAuthorityError(
            "GPU reservations digest is invalid"
        )
    if (
        _bounded_file_digest(reservations, reservations_identity)
        != reservations_sha256
    ):
        raise FormalGpuAuthorityError(
            "GPU reservation authority content changed"
        )
    if expected_reservations_file is not None:
        try:
            expected_payload = expected_reservations_file.read_bytes()
        except OSError as exc:
            raise FormalGpuAuthorityError(
                "exact F GPU reservation policy is unavailable"
            ) from exc
        if hashlib.sha256(expected_payload).hexdigest() != reservations_sha256:
            raise FormalGpuAuthorityError(
                "GPU reservation authority differs from exact F"
            )

    pipe_directories: list[tuple[int, Path]] = []
    identities = {root_identity, reservations_identity}
    for index in (1, 3):
        path_key = f"NEXPOLY_DFT_GPU{index}_MPS_PIPE_AUTHORITY"
        identity_key = f"NEXPOLY_DFT_GPU{index}_MPS_PIPE_IDENTITY"
        raw_path = values.get(path_key, "")
        raw_identity = values.get(identity_key, "")
        if not raw_path and not raw_identity and index == 3:
            continue
        if not raw_path or not raw_identity:
            raise FormalGpuAuthorityError(
                f"GPU{index} MPS descriptor authority is incomplete"
            )
        path = _proc_fd(
            raw_path,
            process_id=process_id,
            name=f"GPU{index} MPS pipe",
        )
        identity = _identity(raw_identity, name=f"GPU{index} MPS pipe")
        _require_identity(
            path,
            identity,
            name=f"GPU{index} MPS pipe",
            kind="directory",
            mode=0o700,
        )
        try:
            relation_metadata = os.stat(
                root / f"mps-{index}" / "pipe"
            )
            pipe_target = os.readlink(path)
        except OSError as exc:
            raise FormalGpuAuthorityError(
                f"GPU{index} MPS pipe hierarchy is unavailable"
            ) from exc
        if _metadata_identity(relation_metadata) != identity:
            raise FormalGpuAuthorityError(
                f"GPU{index} MPS pipe authority escaped its root hierarchy"
            )
        if any(
            pipe_target == forbidden
            or pipe_target.startswith(forbidden + "/")
            for forbidden in forbidden_roots
        ):
            raise FormalGpuAuthorityError(
                f"GPU{index} MPS pipe authority references production"
            )
        if identity in identities:
            raise FormalGpuAuthorityError(
                "GPU descriptor authority identities are not distinct"
            )
        identities.add(identity)
        _require_control_channel(path, index=index)
        pipe_directories.append((index, path))
    if not pipe_directories or pipe_directories[0][0] != 1:
        raise FormalGpuAuthorityError(
            "GPU1 MPS descriptor authority is required"
        )

    broker = root / "broker.sock"
    try:
        broker_metadata = os.lstat(broker)
    except OSError as exc:
        raise FormalGpuAuthorityError(
            "GPU Broker descriptor authority is unavailable"
        ) from exc
    if (
        not stat.S_ISSOCK(broker_metadata.st_mode)
        or broker_metadata.st_uid != os.geteuid()
        or broker_metadata.st_gid != os.getegid()
    ):
        raise FormalGpuAuthorityError(
            "GPU Broker descriptor authority is unsafe"
        )

    return FormalGpuAuthority(
        acceptance_project=values["NEXPOLY_DFT_PROJECT_NAME"],
        authority_sha=values["NEXPOLY_DFT_AUTHORITY_SHA"],
        process_id=process_id,
        process_start_ticks=process_start_ticks,
        root=root,
        reservations=reservations,
        reservations_sha256=reservations_sha256,
        pipe_directories=tuple(pipe_directories),
        root_identity=root_identity,
        reservations_identity=reservations_identity,
        pipe_directory_identities=tuple(
            (
                index,
                _identity(
                    values.get(
                        f"NEXPOLY_DFT_GPU{index}_MPS_PIPE_IDENTITY",
                        "",
                    ),
                    name=f"GPU{index} MPS pipe",
                ),
            )
            for index, _path in pipe_directories
        ),
    )


def materialize_formal_gpu_authority(
    *,
    expected_reservations_file: Path,
    expected_root: Path,
) -> FormalGpuAuthority | None:
    """Rebind parent descriptor authority into this long-lived process."""

    global _PROCESS_AUTHORITY
    global _PROCESS_AUTHORITY_DESCRIPTORS

    enabled = (
        os.environ.get("NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY") == "1"
    )
    if not enabled:
        return load_formal_gpu_authority(
            expected_reservations_file=expected_reservations_file,
            expected_root=expected_root,
        )
    if _PROCESS_AUTHORITY is not None:
        return load_formal_gpu_authority(
            expected_reservations_file=expected_reservations_file,
            expected_root=expected_root,
            require=True,
        )

    source = load_formal_gpu_authority(
        expected_reservations_file=expected_reservations_file,
        expected_root=expected_root,
        require=True,
    )
    assert source is not None
    executor_process = (
        os.environ.get("MONOMER_DFT_EXECUTOR_PROCESS") == "1"
    )
    executor_index_raw = os.environ.get(
        "NEXPOLY_DFT_EXECUTOR_GPU_DEVICE", ""
    )
    original_cuda_pipe = os.environ.get(
        "CUDA_MPS_PIPE_DIRECTORY"
    )
    if executor_process:
        if (
            executor_index_raw not in {"1", "3"}
            or original_cuda_pipe
            != str(
                dict(source.pipe_directories).get(
                    int(executor_index_raw), Path()
                )
            )
        ):
            raise FormalGpuAuthorityError(
                "executor MPS pipe differs from its parent descriptor authority"
            )
    elif original_cuda_pipe is not None:
        raise FormalGpuAuthorityError(
            "CPU-only supervisor inherited an MPS pipe"
        )
    opened: list[int] = []
    try:
        root_descriptor = os.open(
            source.root,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY,
        )
        opened.append(root_descriptor)
        reservations_descriptor = os.open(
            source.reservations,
            os.O_RDONLY | os.O_CLOEXEC,
        )
        opened.append(reservations_descriptor)
        pipe_descriptors: list[tuple[int, int]] = []
        expected_pipe_identities = dict(
            source.pipe_directory_identities
        )
        for index, path in source.pipe_directories:
            descriptor = os.open(
                path,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY,
            )
            opened.append(descriptor)
            pipe_descriptors.append((index, descriptor))

        # Revalidate the parent PID/start-time and every pathname relation
        # after all opens. A disappearing/reused authority can never be
        # accepted between validation and the process-local dup boundary.
        again = load_formal_gpu_authority(
            expected_reservations_file=expected_reservations_file,
            expected_root=expected_root,
            require=True,
        )
        if (
            again is None
            or _metadata_identity(os.fstat(root_descriptor))
            != source.root_identity
            or _metadata_identity(os.fstat(reservations_descriptor))
            != source.reservations_identity
            or any(
                _metadata_identity(os.fstat(descriptor))
                != expected_pipe_identities[index]
                for index, descriptor in pipe_descriptors
            )
        ):
            raise FormalGpuAuthorityError(
                "GPU descriptor authority changed while materialized"
            )

        process_id = os.getpid()
        process_start_ticks = _process_start_ticks(process_id)
        root = Path(f"/proc/{process_id}/fd/{root_descriptor}")
        reservations = Path(
            f"/proc/{process_id}/fd/{reservations_descriptor}"
        )
        pipe_directories = tuple(
            (
                index,
                Path(f"/proc/{process_id}/fd/{descriptor}"),
            )
            for index, descriptor in pipe_descriptors
        )
        materialized = FormalGpuAuthority(
            acceptance_project=source.acceptance_project,
            authority_sha=source.authority_sha,
            process_id=process_id,
            process_start_ticks=process_start_ticks,
            root=root,
            reservations=reservations,
            reservations_sha256=source.reservations_sha256,
            pipe_directories=pipe_directories,
            root_identity=source.root_identity,
            reservations_identity=source.reservations_identity,
            pipe_directory_identities=source.pipe_directory_identities,
        )
        _PROCESS_AUTHORITY_DESCRIPTORS = tuple(opened)
        _PROCESS_AUTHORITY = materialized
        opened = []
        os.environ.update(
            {
                "NEXPOLY_DFT_GPU_AUTHORITY_PID": str(process_id),
                "NEXPOLY_DFT_GPU_AUTHORITY_START_TICKS": str(
                    process_start_ticks
                ),
                "NEXPOLY_DFT_GPU_AUTHORITY_ROOT": str(root),
                "NEXPOLY_DFT_GPU_AUTHORITY_ROOT_IDENTITY": (
                    f"{source.root_identity[0]}:{source.root_identity[1]}"
                ),
                "NEXPOLY_DFT_GPU_RESERVATIONS_AUTHORITY": str(
                    reservations
                ),
                "NEXPOLY_DFT_GPU_RESERVATIONS_IDENTITY": (
                    f"{source.reservations_identity[0]}:"
                    f"{source.reservations_identity[1]}"
                ),
            }
        )
        for index, path in pipe_directories:
            identity = expected_pipe_identities[index]
            os.environ[
                f"NEXPOLY_DFT_GPU{index}_MPS_PIPE_AUTHORITY"
            ] = str(path)
            os.environ[
                f"NEXPOLY_DFT_GPU{index}_MPS_PIPE_IDENTITY"
            ] = f"{identity[0]}:{identity[1]}"
        if executor_process:
            os.environ["CUDA_MPS_PIPE_DIRECTORY"] = str(
                dict(pipe_directories)[int(executor_index_raw)]
            )
        return materialized
    finally:
        for descriptor in opened:
            try:
                os.close(descriptor)
            except OSError:
                pass


def close_process_gpu_authority() -> None:
    """Release a materialized authority in tests or at final process exit."""

    global _PROCESS_AUTHORITY
    global _PROCESS_AUTHORITY_DESCRIPTORS
    for descriptor in _PROCESS_AUTHORITY_DESCRIPTORS:
        try:
            os.close(descriptor)
        except OSError:
            pass
    _PROCESS_AUTHORITY_DESCRIPTORS = ()
    _PROCESS_AUTHORITY = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expected-reservations-file",
        type=Path,
        required=True,
    )
    parser.add_argument("--expected-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        load_formal_gpu_authority(
            expected_reservations_file=args.expected_reservations_file,
            expected_root=args.expected_root,
            require=True,
        )
    except (FormalGpuAuthorityError, OSError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

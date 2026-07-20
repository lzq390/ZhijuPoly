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
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile


SCRIPT_ROOT = Path(__file__).absolute().parent
REPO_ROOT = SCRIPT_ROOT.parent
PRODUCTION_REPO_ROOT = Path("/data/lzq/gith/nexpoly")
PRODUCTION_BASELINE_SHA = "b875829c3f008b5ee733d8ffced3093e4cbb07c5"
PRODUCTION_BASELINE_TREE = "4f68c10a39c6943f7ff13af33d547ebb8f5d7a00"
PRODUCTION_BASELINE_ORIGIN = "https://github.com/lzq390/ZhijuPoly.git"
PRODUCTION_BASELINE_RAW_GIT_AUTHORITY = {
    "entry_count": 26,
    "content_bytes": 60_126,
    "inventory_sha256": (
        "sha256:"
        "c5de05bdd91d6f7c3230632aac6aa6b161a5de1b882ff302605ea181359ac75e"
    ),
}
PRODUCTION_BASELINE_SNAPSHOT = {
    "device": 66_304,
    "inode": 40_763_411,
    "mtime_ns": 1_783_904_390_143_505_793,
    "head": PRODUCTION_BASELINE_SHA,
    "tree": PRODUCTION_BASELINE_TREE,
    "status_sha256": (
        "sha256:"
        "ddcaa922298dfd90458a991016d362d91ce977cd0cfc2522d26f2125ac097931"
    ),
    "status_boundary_count": 25,
    "tracked_path_count": 321,
    "ignored_path_count": 115,
    "untracked_path_count": 0,
    "inventory_entry_count": 507,
    "content_bytes": 11_953_024_420,
    "tracked_content_bytes": 162_602_279,
    "ignored_content_bytes": 11_790_422_141,
    "untracked_content_bytes": 0,
    "boundary_sha256": (
        "sha256:"
        "f47849ef010b29edef466bcbcab63c4d52c4cca498731bdcb7d2d02e788cad2c"
    ),
    "inventory_sha256": (
        "sha256:"
        "9508364fee2ebcfc45e5c340c1db30f0242340e672a4de190ed49984223d58d5"
    ),
    "git_authority_entry_count": 26,
    "git_authority_content_bytes": 60_126,
    "git_authority_sha256": (
        "sha256:"
        "c5de05bdd91d6f7c3230632aac6aa6b161a5de1b882ff302605ea181359ac75e"
    ),
    "git_config_sha256": (
        "sha256:"
        "d122838c3d6989e4c463adcdcd988499f54eaf2f35121f42efe1938aa3f959be"
    ),
    "git_origin_url_count": 1,
    "git_origin_sha256": (
        "sha256:"
        "a88f7eb8b9cee4e1f38bd69b804093ab830b6227b741b820bff4b04b1329fafb"
    ),
    "git_ref_count": 4,
    "git_refs_sha256": (
        "sha256:"
        "58ae4cd4f2368445812e072fc977c076a405f2939728555503db161ab3d95c28"
    ),
    "git_head_ref_sha256": (
        "sha256:"
        "599bbcd7b7f94b50d9b83318ba0dd4b8e1ba9e39d1d3ee73d1fbbd70496d0f93"
    ),
}
PRODUCTION_GIT_CONFIG_OVERRIDES = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.excludesFile=/dev/null",
    "-c",
    "core.attributesFile=/dev/null",
)
GPU_UUIDS = {
    "1": "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
    "2": "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe",
    "3": "GPU-0818ca6b-d9b6-af6a-71bf-afe3777ee3a5",
}
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_HTTP_BYTES = 256 * 1024 * 1024
BACKEND_BASE_URL = "http://127.0.0.1:28000/api/v1/monomer-dft"
LOCAL_DOCKER_SOCKET = Path("/var/run/docker.sock")
SAFE_ENV_KEYS = frozenset(
    {
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "PATH",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "CUDA_DEVICE_ORDER",
    }
)
SAFE_COMMAND_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
GPU2_AUDIT_INTERVAL_SECONDS = 0.25
PRODUCTION_CAS_MAX_ENTRIES = 20_000
PRODUCTION_CAS_MAX_PATHS = 10_000
PRODUCTION_CAS_MAX_DEPTH = 128
PRODUCTION_CAS_MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
PRODUCTION_CAS_MAX_TOTAL_BYTES = 16 * 1024 * 1024 * 1024
PRODUCTION_GIT_AUTHORITY_MAX_ENTRIES = 20_000
PRODUCTION_GIT_AUTHORITY_MAX_FILE_BYTES = 64 * 1024 * 1024
PRODUCTION_GIT_AUTHORITY_MAX_TOTAL_BYTES = 128 * 1024 * 1024
IMAGE_ROOTS = {
    "backend": "ghcr.io/lzq390/nexpoly-backend",
    "web": "ghcr.io/lzq390/nexpoly-web",
}
ORDINARY_DEV_IMAGE_TAGS = (
    "nexpoly-dft-dev-backend:latest",
    "nexpoly-dft-dev-frontend:latest",
)
REPOSITORY_SOURCE_URL = "https://github.com/lzq390/ZhijuPoly"

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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


HTTP_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _NoRedirect(),
)


def _safe_command_environment(
    *,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return an allow-listed environment with loader/proxy hooks absent."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if key in SAFE_ENV_KEYS and value
    }
    environment["PATH"] = SAFE_COMMAND_PATH
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra:
        for key, value in extra.items():
            _require(
                isinstance(key, str)
                and isinstance(value, str)
                and "\x00" not in key
                and "\x00" not in value,
                "acceptance environment override is invalid",
            )
            environment[key] = value
    return environment


def _local_docker_environment() -> dict[str, str]:
    try:
        metadata = LOCAL_DOCKER_SOCKET.lstat()
    except OSError as exc:
        raise AcceptanceHarnessError("local Docker socket is unavailable") from exc
    _require(
        stat.S_ISSOCK(metadata.st_mode) and not LOCAL_DOCKER_SOCKET.is_symlink(),
        "Docker must use the fixed local Unix socket",
    )
    return _safe_command_environment(
        extra={"DOCKER_HOST": f"unix://{LOCAL_DOCKER_SOCKET}"}
    )


def _require_backend_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    _require(
        url == BACKEND_BASE_URL
        and parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and parsed.port == 28000
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment,
        "acceptance Backend URL must be the fixed loopback endpoint",
    )
    return BACKEND_BASE_URL


def _validate_authority_images_input(
    images: dict[str, Any],
    *,
    authority_sha: str,
) -> None:
    expected_fields = {
        "role",
        "digest_ref",
        "index_digest",
        "platform_digest",
        "image_id",
        "revision",
        "source",
        "version",
    }
    _require(
        set(images) == set(IMAGE_ROOTS),
        "authority OCI images must be exactly backend/web",
    )
    for role, root in IMAGE_ROOTS.items():
        image = images.get(role)
        _require(
            isinstance(image, dict)
            and set(image) == expected_fields
            and image["role"] == role
            and image["digest_ref"] == f"{root}@{image['index_digest']}"
            and image["revision"] == authority_sha
            and image["source"] == REPOSITORY_SOURCE_URL
            and image["version"] == f"sha-{authority_sha}",
            f"{role} authority image does not bind exact final main",
        )
        for name in ("index_digest", "platform_digest", "image_id"):
            _require(
                isinstance(image[name], str)
                and re.fullmatch(r"sha256:[0-9a-f]{64}", image[name])
                is not None,
                f"{role} {name} is not a full sha256 digest",
            )
        # Keep these identities in separate, explicitly typed fields.  Docker's
        # containerd image store may expose the selected manifest (or even the
        # single-platform index) as the local image ID, so equality is not by
        # itself evidence that the caller confused the concepts.


def _validate_published_platform_manifest(
    *,
    expected: dict[str, Any],
    environment: dict[str, str],
) -> None:
    try:
        manifest = json.loads(
            _run(
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                expected["digest_ref"],
                "--raw",
                timeout=60.0,
                env=environment,
            ).stdout
        )
    except json.JSONDecodeError as exc:
        raise AcceptanceHarnessError(
            f"{expected['role']} published OCI manifest is invalid"
        ) from exc
    _require(
        isinstance(manifest, dict),
        f"{expected['role']} published OCI manifest is not an object",
    )
    media_type = manifest.get("mediaType")
    if media_type in {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }:
        manifests = manifest.get("manifests")
        _require(
            isinstance(manifests, list),
            f"{expected['role']} published OCI index lacks manifests",
        )
        selected: list[dict[str, Any]] = []
        for descriptor in manifests:
            if not isinstance(descriptor, dict):
                continue
            platform = descriptor.get("platform")
            if (
                isinstance(platform, dict)
                and platform.get("os") == "linux"
                and platform.get("architecture") == "amd64"
                and platform.get("variant") in {None, ""}
            ):
                selected.append(descriptor)
        _require(
            len(selected) == 1
            and selected[0].get("digest") == expected["platform_digest"],
            f"{expected['role']} published OCI index selects another linux/amd64 manifest",
        )
        return
    _require(
        media_type
        in {
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        }
        and expected["platform_digest"] == expected["index_digest"],
        f"{expected['role']} published OCI platform identity differs",
    )


def _run(
    *command: str,
    timeout: float = 120.0,
    check: bool = True,
    env: dict[str, str] | None = None,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    command_environment = env if env is not None else _safe_command_environment()
    try:
        return subprocess.run(
            command,
            cwd=REPO_ROOT,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=command_environment,
            pass_fds=pass_fds,
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
    repository_descriptor = _open_absolute_directory_chain(
        REPO_ROOT,
        "acceptance repository root",
    )
    runtime_descriptor = -1
    runs_descriptor = -1
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        try:
            runtime_descriptor = os.open(
                b".runtime",
                flags,
                dir_fd=repository_descriptor,
            )
        except OSError as exc:
            raise AcceptanceHarnessError(
                "acceptance runtime root must be an owner-controlled real directory"
            ) from exc
        metadata = os.fstat(runtime_descriptor)
        _require(
            stat.S_ISDIR(metadata.st_mode)
            and metadata.st_uid == os.geteuid(),
            "acceptance runtime root must be an owner-controlled real directory",
        )
        os.fchmod(runtime_descriptor, 0o700)
        try:
            os.mkdir(b"runs", mode=0o700, dir_fd=runtime_descriptor)
            os.fsync(runtime_descriptor)
        except FileExistsError:
            pass
        try:
            runs_descriptor = os.open(
                b"runs",
                flags,
                dir_fd=runtime_descriptor,
            )
        except OSError as exc:
            raise AcceptanceHarnessError(
                "acceptance run root must be an owner-controlled real directory"
            ) from exc
        metadata = os.fstat(runs_descriptor)
        _require(
            stat.S_ISDIR(metadata.st_mode)
            and metadata.st_uid == os.geteuid(),
            "acceptance run root must be an owner-controlled real directory",
        )
        os.fchmod(runs_descriptor, 0o700)
    finally:
        if runs_descriptor >= 0:
            os.close(runs_descriptor)
        if runtime_descriptor >= 0:
            os.close(runtime_descriptor)
        os.close(repository_descriptor)
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


def validate_bridge_authority(
    *,
    bridge_sha: str,
    bridge_tree: str,
    authority_sha: str,
) -> None:
    _require(
        SHA_RE.fullmatch(bridge_sha) is not None
        and SHA_RE.fullmatch(bridge_tree) is not None,
        "acceptance requires the full frozen B commit and tree IDs",
    )
    _require(
        _run(
            "git",
            "cat-file",
            "-e",
            f"{bridge_sha}^{{commit}}",
            check=False,
        ).returncode
        == 0
        and _run("git", "rev-parse", f"{bridge_sha}^{{tree}}")
        .stdout.strip()
        == bridge_tree
        and _run(
            "git",
            "merge-base",
            "--is-ancestor",
            bridge_sha,
            authority_sha,
            check=False,
        ).returncode
        == 0,
        "frozen B is unavailable, has another tree, or is not an ancestor of F",
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
    parsed = urllib.parse.urlsplit(url)
    _require(
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and parsed.port == 28000
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and (
            parsed.path == "/api/v1/monomer-dft"
            or parsed.path.startswith("/api/v1/monomer-dft/")
        ),
        "acceptance HTTP target escaped the fixed loopback Backend",
    )
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
        with HTTP_OPENER.open(request, timeout=timeout) as response:
            data = response.read(MAX_HTTP_BYTES + 1)
            _require(len(data) <= MAX_HTTP_BYTES, "acceptance HTTP response is oversized")
            return response.status, data, dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        data = exc.read(MAX_HTTP_BYTES + 1)
        _require(len(data) <= MAX_HTTP_BYTES, "acceptance HTTP error is oversized")
        _require(
            exc.code not in {301, 302, 303, 307, 308},
            "acceptance Backend redirect is forbidden",
        )
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


def _canonical_job_id(value: object) -> str:
    _require(isinstance(value, str), "DFT job ID is not a string")
    try:
        parsed_job_id = uuid.UUID(value)
    except ValueError as exc:
        raise AcceptanceHarnessError("DFT job ID is not a UUID") from exc
    _require(
        str(parsed_job_id) == value and parsed_job_id.version == 4,
        "DFT job ID is not a canonical UUIDv4",
    )
    return value


def _journal_path(job_root: Path, job_id: str) -> Path:
    job_id = _canonical_job_id(job_id)
    root = job_root.resolve()
    _require(
        root == (REPO_ROOT / ".runtime/monomer-dft-worker-runs").resolve(),
        "DFT journal root escaped the current worktree",
    )
    job_dir = root / job_id
    _require(
        job_dir.is_dir()
        and not job_dir.is_symlink()
        and job_dir.resolve().parent == root,
        "DFT job journal is missing or escaped its root",
    )
    journals = [
        path
        for path in job_dir.glob("*/journal.json")
        if path.is_file()
        and not path.is_symlink()
        and path.resolve().parent.parent == job_dir.resolve()
    ]
    _require(len(journals) == 1, "DFT job must have exactly one durable journal")
    return journals[0]


def _validate_fenced_provenance(
    job: dict[str, Any],
) -> dict[str, Any]:
    provenance = job.get("provenance")
    _require(isinstance(provenance, dict), "completed job lacks provenance")
    gpu_index = provenance.get("gpu_index")
    gpu_uuid = provenance.get("gpu_uuid")
    lease_id = provenance.get("lease_id")
    fencing_token = provenance.get("fencing_token")
    _require(
        gpu_index in {1, "1"}
        and gpu_uuid == GPU_UUIDS["1"]
        and isinstance(lease_id, str)
        and bool(lease_id)
        and provenance.get("execution_path") == "primary"
        and isinstance(provenance.get("parent_lease_id"), str)
        and bool(provenance["parent_lease_id"])
        and provenance["parent_lease_id"] != lease_id
        and provenance.get("gpu_preferred") is True
        and provenance.get("model_alias") == "aimnet2"
        and provenance.get("model_id") == "aimnet2"
        and provenance.get("visible_gpu_count") == 1
        and str(provenance.get("gpu_physical_device")) == "1"
        and isinstance(fencing_token, int)
        and not isinstance(fencing_token, bool)
        and fencing_token > 0,
        "completed job lacks exact lease/fencing provenance",
    )
    for name in (
        "worker_instance_id",
        "broker_instance_id",
        "aimnet_commit",
        "aimnet_wheel_sha256",
        "model_sha256",
        "model_registry_key",
    ):
        _require(
            isinstance(provenance.get(name), str) and bool(provenance[name]),
            f"completed job lacks {name} provenance",
        )
    _require(
        re.fullmatch(r"[0-9a-f]{32}", provenance["worker_instance_id"]) is not None
        and SHA_RE.fullmatch(provenance["aimnet_commit"]) is not None
        and re.fullmatch(r"[0-9a-f]{64}", provenance["aimnet_wheel_sha256"])
        is not None
        and re.fullmatch(r"[0-9a-f]{64}", provenance["model_sha256"]) is not None,
        "completed job runtime provenance is malformed",
    )
    return provenance


def _finite(value: object, name: str) -> float:
    _require(
        not isinstance(value, bool) and isinstance(value, (int, float)),
        f"{name} is not numeric",
    )
    result = float(value)
    _require(math.isfinite(result), f"{name} is not finite")
    return result


def _validate_scientific_result(
    result: dict[str, Any],
) -> dict[str, Any]:
    _require(
        result.get("schema_version") == 2
        and result.get("calculation_type") == "single_point"
        and result.get("engine") == "aimnet2"
        and result.get("model") == "aimnet2",
        "Backend E2E result is not the strict scientific V2 contract",
    )
    atoms = result.get("atoms")
    properties = result.get("properties")
    status = result.get("scientific_status")
    _require(
        isinstance(atoms, dict)
        and atoms.get("count") == 3
        and atoms.get("atomic_numbers") == [8, 1, 1],
        "Backend E2E water atom identity differs",
    )
    _require(isinstance(properties, dict), "Backend E2E properties are missing")
    energy = properties.get("energy")
    forces = properties.get("forces")
    hessian = properties.get("hessian")
    _require(
        isinstance(energy, dict)
        and isinstance(forces, dict)
        and isinstance(hessian, dict),
        "Backend E2E energy/forces/Hessian are incomplete",
    )
    energy_eV = _finite(energy.get("value_eV"), "Backend E2E energy")
    force_rows = forces.get("values_eV_per_A")
    _require(
        isinstance(force_rows, list)
        and len(force_rows) == 3
        and all(isinstance(row, list) and len(row) == 3 for row in force_rows),
        "Backend E2E force shape differs",
    )
    force_values = [
        [_finite(value, "Backend E2E force") for value in row]
        for row in force_rows
    ]
    calculated_fmax = max(
        math.sqrt(sum(component * component for component in row))
        for row in force_values
    )
    reported_fmax = _finite(
        forces.get("fmax_eV_per_A"), "Backend E2E maximum force"
    )
    _require(
        math.isclose(calculated_fmax, reported_fmax, rel_tol=1e-7, abs_tol=1e-8),
        "Backend E2E maximum force differs from force vectors",
    )
    symmetry_max = _finite(
        hessian.get("symmetry_max_abs_eV_per_A2"),
        "Backend E2E Hessian symmetry",
    )
    symmetry_relative = _finite(
        hessian.get("symmetry_relative_error"),
        "Backend E2E Hessian relative symmetry",
    )
    _require(
        hessian.get("shape") == [9, 9]
        and hessian.get("artifact_id") == "hessian"
        and hessian.get("units") == "eV/angstrom^2"
        and hessian.get("symmetric_within_tolerance") is True
        and symmetry_max >= 0
        and symmetry_relative >= 0,
        "Backend E2E Hessian contract is incomplete",
    )
    _require(
        isinstance(status, dict)
        and status.get("calculation_completed") is True
        and status.get("geometry_status") == "not_optimized",
        "Backend E2E scientific status is incomplete",
    )
    return {
        "atom_count": 3,
        "atomic_numbers": [8, 1, 1],
        "energy_eV": energy_eV,
        "forces_shape": [3, 3],
        "max_force_eV_per_A": reported_fmax,
        "hessian_shape": [9, 9],
        "hessian_symmetry_max_abs_eV_per_A2": symmetry_max,
        "hessian_symmetry_relative_error": symmetry_relative,
        "hessian_symmetric_within_tolerance": True,
    }


def _validate_artifact_descriptor(value: object) -> dict[str, Any]:
    _require(isinstance(value, dict), "artifact descriptor is not an object")
    descriptor = dict(value)
    _require(
        set(descriptor)
        in (
            {"artifact_id", "name", "media_type", "size_bytes", "sha256"},
            {
                "artifact_id",
                "name",
                "media_type",
                "size_bytes",
                "sha256",
                "available",
            },
        )
        and isinstance(descriptor.get("artifact_id"), str)
        and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", descriptor["artifact_id"])
        is not None
        and isinstance(descriptor.get("name"), str)
        and Path(descriptor["name"]).name == descriptor["name"]
        and "/" not in descriptor["name"]
        and "\\" not in descriptor["name"]
        and isinstance(descriptor.get("media_type"), str)
        and "\r" not in descriptor["media_type"]
        and "\n" not in descriptor["media_type"]
        and isinstance(descriptor.get("size_bytes"), int)
        and not isinstance(descriptor["size_bytes"], bool)
        and 0 <= descriptor["size_bytes"] <= MAX_HTTP_BYTES
        and isinstance(descriptor.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", descriptor["sha256"]) is not None
        and descriptor.get("available", True) is True,
        "artifact descriptor is unsafe or incomplete",
    )
    descriptor.pop("available", None)
    return descriptor


def _download_and_validate_artifacts(
    *,
    base_url: str,
    job_id: str,
    descriptors: list[dict[str, Any]],
) -> tuple[dict[str, bytes], bytes, dict[str, Any]]:
    normalized = [_validate_artifact_descriptor(item) for item in descriptors]
    ids = [item["artifact_id"] for item in normalized]
    names = [item["name"] for item in normalized]
    _require(
        len(ids) == len(set(ids))
        and len(names) == len({name.casefold() for name in names})
        and {"scientific_result", "hessian"}.issubset(ids),
        "artifact manifest is duplicated or incomplete",
    )
    payloads: dict[str, bytes] = {}
    by_id = {item["artifact_id"]: item for item in normalized}
    for artifact_id, descriptor in by_id.items():
        response_status, payload, _headers = _http(
            "GET",
            f"{base_url}/jobs/{job_id}/artifacts/{artifact_id}",
        )
        _require(response_status == 200, f"artifact {artifact_id} download failed")
        _require(
            len(payload) == descriptor["size_bytes"]
            and hashlib.sha256(payload).hexdigest() == descriptor["sha256"],
            f"artifact {artifact_id} differs from its manifest",
        )
        payloads[artifact_id] = payload

    bundle_status, bundle, _headers = _http(
        "GET", f"{base_url}/jobs/{job_id}/bundle"
    )
    _require(bundle_status == 200, "artifact bundle download failed")
    try:
        with zipfile.ZipFile(io.BytesIO(bundle), "r") as archive:
            infos = archive.infolist()
            _require(
                len(infos) == len(normalized)
                and [info.filename for info in infos] == names,
                "artifact bundle members differ from the manifest",
            )
            for info, descriptor in zip(infos, normalized, strict=True):
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                _require(
                    info.filename == descriptor["name"]
                    and not info.is_dir()
                    and not (info.flag_bits & 0x1)
                    and (unix_mode == 0 or stat.S_ISREG(unix_mode))
                    and info.compress_type
                    in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    and info.file_size == descriptor["size_bytes"],
                    "artifact bundle member metadata is unsafe",
                )
                _require(
                    archive.read(info) == payloads[descriptor["artifact_id"]],
                    "artifact bundle member differs from its standalone artifact",
                )
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise AcceptanceHarnessError("artifact bundle is invalid") from exc

    scientific_bytes = payloads["scientific_result"]
    try:
        scientific = json.loads(scientific_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AcceptanceHarnessError("scientific result artifact is invalid") from exc
    _require(
        isinstance(scientific, dict), "scientific result artifact is not an object"
    )
    hessian_bytes = payloads["hessian"]
    try:
        import numpy as np

        with np.load(io.BytesIO(hessian_bytes), allow_pickle=False) as arrays:
            _require(
                set(arrays.files)
                == {
                    "hessian_eV_per_A2",
                    "atomic_numbers",
                    "atomic_masses_u",
                    "isotope_mass_numbers",
                    "coordinates_angstrom",
                },
                "Hessian artifact array inventory differs",
            )
            matrix = np.asarray(arrays["hessian_eV_per_A2"], dtype=np.float64)
            atomic_numbers = np.asarray(arrays["atomic_numbers"])
            atomic_masses = np.asarray(
                arrays["atomic_masses_u"], dtype=np.float64
            )
            isotope_masses = np.asarray(arrays["isotope_mass_numbers"])
            coordinates = np.asarray(
                arrays["coordinates_angstrom"], dtype=np.float64
            )
            scientific_atoms = scientific.get("atoms")
            scientific_geometry = scientific.get("geometry")
            _require(
                matrix.shape == (9, 9)
                and bool(np.isfinite(matrix).all())
                and atomic_numbers.shape == (3,)
                and atomic_numbers.tolist() == [8, 1, 1]
                and atomic_masses.shape == (3,)
                and bool(np.isfinite(atomic_masses).all())
                and bool((atomic_masses > 0).all())
                and isotope_masses.shape == (3,)
                and all(
                    isinstance(value, int) and not isinstance(value, bool)
                    for value in isotope_masses.tolist()
                )
                and coordinates.shape == (3, 3)
                and bool(np.isfinite(coordinates).all())
                and isinstance(scientific_atoms, dict)
                and atomic_numbers.tolist()
                == scientific_atoms.get("atomic_numbers")
                and np.array_equal(
                    atomic_masses,
                    np.asarray(
                        scientific_atoms.get("atomic_masses_u"),
                        dtype=np.float64,
                    ),
                )
                and isotope_masses.tolist()
                == scientific_atoms.get("isotope_mass_numbers")
                and isinstance(scientific_geometry, dict)
                and np.array_equal(
                    coordinates,
                    np.asarray(
                        scientific_geometry.get("final_coordinates_angstrom"),
                        dtype=np.float64,
                    ),
                ),
                "Hessian artifact shape, finiteness, atoms, masses, or geometry differ",
            )
            hessian_max = float(np.max(np.abs(matrix - matrix.T)))
            scale = max(float(np.max(np.abs(matrix))), 1.0e-12)
            hessian_relative = hessian_max / scale
    except (AcceptanceHarnessError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, AcceptanceHarnessError):
            raise
        raise AcceptanceHarnessError("Hessian artifact is invalid") from exc

    return payloads, bundle, {
        "normalized_manifest": normalized,
        "scientific_result": scientific,
        "hessian_max": hessian_max,
        "hessian_relative": hessian_relative,
    }


def _validate_journal(
    path: Path,
    *,
    job_id: str,
    status: str,
    public_result: dict[str, Any] | None,
    artifacts: list[dict[str, Any]],
    worker_instance_id: str | None,
) -> dict[str, Any]:
    journal = _load_json(path, f"{status} DFT journal")
    snapshot = journal.get("snapshot")
    _require(
        journal.get("journal_schema_version") == 2
        and isinstance(snapshot, dict)
        and snapshot.get("job_id") == job_id
        and snapshot.get("status") == status,
        f"{status} journal identity/state differs",
    )
    if worker_instance_id is not None:
        _require(
            snapshot.get("worker_instance_id") == worker_instance_id,
            "completed journal Worker identity differs",
        )
    normalized_artifacts = [_validate_artifact_descriptor(item) for item in artifacts]
    _require(
        snapshot.get("result") == public_result
        and snapshot.get("artifacts") == normalized_artifacts
        and journal.get("artifact_manifest") == normalized_artifacts
        and journal.get("artifact_state")
        == ("available" if status == "completed" else "none"),
        f"{status} journal result/artifact state differs from Backend evidence",
    )
    return journal


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
    completed_id = _canonical_job_id(completed.get("job_id"))
    completed = _wait_job(base_url, completed_id, timeout_seconds)
    _require(completed.get("status") == "completed", "Backend E2E job did not complete")
    provenance = _validate_fenced_provenance(completed)
    result = completed.get("result")
    _require(isinstance(result, dict), "Backend E2E job lacks scientific result")
    _require(
        result.get("provenance") == provenance,
        "Backend E2E result and job provenance differ",
    )
    science = _validate_scientific_result(result)

    artifacts = completed.get("artifacts")
    _require(isinstance(artifacts, list) and artifacts, "completed job has no artifacts")
    payloads, bundle_bytes, artifact_evidence = _download_and_validate_artifacts(
        base_url=base_url,
        job_id=completed_id,
        descriptors=artifacts,
    )
    artifact_result = artifact_evidence["scientific_result"]
    _require(
        artifact_result == result,
        "scientific result artifact differs from the Backend result",
    )
    hessian = result["properties"]["hessian"]
    _require(
        math.isclose(
            artifact_evidence["hessian_max"],
            float(hessian["symmetry_max_abs_eV_per_A2"]),
            rel_tol=1e-7,
            abs_tol=1e-10,
        )
        and math.isclose(
            artifact_evidence["hessian_relative"],
            float(hessian["symmetry_relative_error"]),
            rel_tol=1e-7,
            abs_tol=1e-10,
        ),
        "Hessian artifact symmetry differs from the scientific summary",
    )

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
    cancelled_id = _canonical_job_id(cancelled.get("job_id"))
    _wait_job_running(base_url, cancelled_id, timeout_seconds)
    _json_response("POST", f"{base_url}/jobs/{cancelled_id}/cancel")
    cancelled = _wait_job(base_url, cancelled_id, timeout_seconds)
    _require(
        cancelled.get("status") == "cancelled"
        and cancelled.get("result") is None
        and cancelled.get("artifacts") == []
        and cancelled.get("artifacts_state") == "none",
        "Backend cancellation was not durable or retained partial results",
    )

    completed_journal = _journal_path(job_root, completed_id)
    cancelled_journal = _journal_path(job_root, cancelled_id)
    normalized_manifest = artifact_evidence["normalized_manifest"]
    completed_journal_value = _validate_journal(
        completed_journal,
        job_id=completed_id,
        status="completed",
        public_result=result,
        artifacts=artifacts,
        worker_instance_id=provenance["worker_instance_id"],
    )
    cancelled_journal_value = _validate_journal(
        cancelled_journal,
        job_id=cancelled_id,
        status="cancelled",
        public_result=None,
        artifacts=[],
        worker_instance_id=provenance["worker_instance_id"],
    )
    e2e = {
        "status": "passed",
        "transport": "broker+uds+backend",
        "gpu_indices": [1],
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
        "fresh_worker_instance_id": provenance["worker_instance_id"],
        "cancelled_journal_sha256": _sha256_file(cancelled_journal),
    }
    science.update(
        {
            "status": "passed",
            "gpu_index": 1,
            "gpu_uuid": GPU_UUIDS["1"],
            "properties": ["energy", "forces", "hessian"],
            "completed_job_id": completed_id,
            "worker_instance_id": provenance["worker_instance_id"],
            "execution_path": "primary",
            "parent_lease_id": provenance["parent_lease_id"],
            "lease_id": provenance["lease_id"],
            "fencing_token": provenance["fencing_token"],
            "broker_instance_id": provenance["broker_instance_id"],
            "scientific_result_sha256": _sha256_bytes(
                payloads["scientific_result"]
            ),
            "hessian_artifact_sha256": _sha256_bytes(payloads["hessian"]),
            "artifact_manifest_sha256": (
                acceptance_contract.canonical_json_digest(normalized_manifest)
            ),
            "bundle_manifest_sha256": acceptance_contract.canonical_json_digest(
                {
                    item["name"]: f"sha256:{item['sha256']}"
                    for item in normalized_manifest
                }
            ),
            "bundle_sha256": _sha256_bytes(bundle_bytes),
            "completed_journal_sha256": _sha256_file(completed_journal),
            "provenance_sha256": (
                acceptance_contract.canonical_json_digest(provenance)
            ),
            "aimnet_commit": provenance["aimnet_commit"],
            "aimnet_wheel_sha256": f"sha256:{provenance['aimnet_wheel_sha256']}",
            "model_sha256": f"sha256:{provenance['model_sha256']}",
            "model_registry_key": provenance["model_registry_key"],
        }
    )
    _require(
        completed_journal_value["snapshot"]["result"] == result
        and cancelled_journal_value["snapshot"]["result"] is None,
        "durable journals do not bind the completed/cancelled results",
    )
    return {"e2e": e2e, "science": science}


def _model_metadata_snapshot(metadata: os.stat_result) -> dict[str, int]:
    return {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mode": metadata.st_mode,
        "nlink": metadata.st_nlink,
        "uid": metadata.st_uid,
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "ctime_ns": metadata.st_ctime_ns,
    }


def _directory_identity_snapshot(
    metadata: os.stat_result,
) -> dict[str, int]:
    return {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mode": metadata.st_mode,
        "uid": metadata.st_uid,
    }


def _open_absolute_directory_chain(path: Path, name: str) -> int:
    """Open every absolute directory component without following symlinks."""

    raw_path = os.fspath(path)
    _require(
        isinstance(raw_path, str)
        and raw_path.startswith("/")
        and not raw_path.startswith("//")
        and os.path.normpath(raw_path) == raw_path
        and "\x00" not in raw_path
        and all(
            ord(character) >= 0x20 and ord(character) != 0x7F
            for character in raw_path
        ),
        f"{name} is not a normalized absolute directory",
    )
    components = path.parts
    _require(
        components
        and components[0] == "/"
        and all(component not in {"", ".", ".."} for component in components[1:]),
        f"{name} has an unsafe path component",
    )
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open("/", flags)
        for component in components[1:]:
            next_descriptor = os.open(
                os.fsencode(component),
                flags,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError as exc:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        raise AcceptanceHarnessError(
            f"{name} could not be opened through a symlink-free directory chain"
        ) from exc


def _hash_model_descriptor(
    descriptor: int,
    expected: dict[str, int],
) -> str:
    before = os.fstat(descriptor)
    _require(
        _model_metadata_snapshot(before) == expected
        and stat.S_ISREG(before.st_mode)
        and before.st_nlink == 1
        and before.st_uid == os.geteuid(),
        "stable model identity changed before hashing",
    )
    digest_value = hashlib.sha256()
    offset = 0
    while offset < expected["size"]:
        try:
            chunk = os.pread(
                descriptor,
                min(1024 * 1024, expected["size"] - offset),
                offset,
            )
        except InterruptedError:
            continue
        _require(chunk, "stable model ended before its sealed size")
        offset += len(chunk)
        _require(
            offset <= expected["size"],
            "stable model exceeded its sealed size",
        )
        digest_value.update(chunk)
    while True:
        try:
            trailing = os.pread(descriptor, 1, expected["size"])
            break
        except InterruptedError:
            continue
    after = os.fstat(descriptor)
    _require(
        offset == expected["size"]
        and trailing == b""
        and _model_metadata_snapshot(after) == expected,
        "stable model changed while it was hashed",
    )
    return "sha256:" + digest_value.hexdigest()


def _expected_model_metadata(evidence: dict[str, Any]) -> dict[str, int]:
    return {
        key: evidence[key]
        for key in (
            "device",
            "inode",
            "mode",
            "nlink",
            "uid",
            "size",
            "mtime_ns",
            "ctime_ns",
        )
    }


def _verify_model_descriptor(
    descriptor: int,
    evidence: dict[str, Any],
) -> None:
    _require(
        isinstance(descriptor, int)
        and not isinstance(descriptor, bool)
        and descriptor > 2,
        "stable model descriptor is invalid",
    )
    try:
        access_mode = fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE
    except OSError as exc:
        raise AcceptanceHarnessError(
            "stable model descriptor is not open"
        ) from exc
    _require(
        access_mode == os.O_RDONLY,
        "stable model descriptor is not read-only",
    )
    digest_value = _hash_model_descriptor(
        descriptor,
        _expected_model_metadata(evidence),
    )
    _require(
        digest_value == evidence["sha256"],
        "stable model descriptor digest differs",
    )


def _verify_stable_model_copy(
    evidence: dict[str, Any],
    *,
    descriptor: int | None = None,
) -> dict[str, Any]:
    expected_fields = {
        "path",
        "sha256",
        "directory",
        "device",
        "inode",
        "mode",
        "nlink",
        "uid",
        "size",
        "mtime_ns",
        "ctime_ns",
    }
    _require(
        isinstance(evidence, dict)
        and set(evidence) == expected_fields
        and evidence["sha256"] == acceptance_contract.AIMNET2_MODEL_SHA256,
        "stable model evidence is incomplete",
    )
    path = Path(evidence["path"])
    parent = path.parent
    _require(
        path.is_absolute()
        and path.name == acceptance_contract.AIMNET2_MODEL_FILENAME
        and not os.fspath(path).startswith("//")
        and os.path.normpath(os.fspath(path)) == os.fspath(path),
        "stable model path is invalid",
    )
    directory_expected = evidence["directory"]
    _require(
        isinstance(directory_expected, dict)
        and set(directory_expected)
        == {
            "device",
            "inode",
            "mode",
            "nlink",
            "uid",
            "size",
            "mtime_ns",
            "ctime_ns",
        }
        and stat.S_ISDIR(directory_expected["mode"])
        and directory_expected["uid"] == os.geteuid()
        and stat.S_IMODE(directory_expected["mode"]) == 0o700,
        "stable model directory is not owner-private",
    )
    expected = _expected_model_metadata(evidence)
    _require(
        expected["nlink"] == 1
        and expected["uid"] == os.geteuid()
        and stat.S_ISREG(expected["mode"])
        and stat.S_IMODE(expected["mode"]) == 0o400,
        "stable model metadata is invalid",
    )
    file_flags = (
        os.O_RDONLY
        | os.O_CLOEXEC
        | os.O_NOFOLLOW
        | getattr(os, "O_NONBLOCK", 0)
    )
    directory_fd = _open_absolute_directory_chain(
        parent,
        "stable model directory",
    )
    try:
        _require(
            _model_metadata_snapshot(os.fstat(directory_fd))
            == directory_expected,
            "stable model directory path changed",
        )
        for _attempt in range(2):
            try:
                reopened_descriptor = os.open(
                    os.fsencode(path.name),
                    file_flags,
                    dir_fd=directory_fd,
                )
            except OSError as exc:
                raise AcceptanceHarnessError(
                    "stable model copy could not be reopened safely"
                ) from exc
            try:
                digest_value = _hash_model_descriptor(
                    reopened_descriptor,
                    expected,
                )
            finally:
                os.close(reopened_descriptor)
            _require(
                digest_value == evidence["sha256"],
                "stable model copy digest differs",
            )
        final_metadata = os.stat(
            os.fsencode(path.name),
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        _require(
            _model_metadata_snapshot(final_metadata) == expected,
            "stable model path changed after reopen verification",
        )
        _require(
            _model_metadata_snapshot(os.fstat(directory_fd))
            == directory_expected,
            "stable model directory changed during verification",
        )
        directory_recheck_fd = _open_absolute_directory_chain(
            parent,
            "stable model directory recheck",
        )
        try:
            _require(
                _model_metadata_snapshot(os.fstat(directory_recheck_fd))
                == directory_expected,
                "stable model directory path changed during verification",
            )
        finally:
            os.close(directory_recheck_fd)
    finally:
        os.close(directory_fd)
    if descriptor is not None:
        _verify_model_descriptor(descriptor, evidence)
    return evidence


class _StableModelCopy:
    def __init__(
        self,
        *,
        evidence: dict[str, Any],
        model_descriptor: int,
        directory_descriptor: int,
        run_directory_descriptor: int,
        run_directory_metadata: dict[str, int],
    ) -> None:
        self.evidence = evidence
        self.model_descriptor = model_descriptor
        self.directory_descriptor = directory_descriptor
        self.run_directory_descriptor = run_directory_descriptor
        self.run_directory_metadata = run_directory_metadata
        self.removed = False


def _prepare_stable_model_copy(
    source_path: str,
    run_directory: Path,
) -> _StableModelCopy:
    source = Path(source_path)
    _require(
        source.is_absolute()
        and source.name == acceptance_contract.AIMNET2_MODEL_FILENAME
        and not os.fspath(source).startswith("//")
        and os.path.normpath(os.fspath(source)) == os.fspath(source),
        "AIMNet2 source model path is unsafe",
    )
    run_directory_fd = _open_absolute_directory_chain(
        run_directory,
        "GPU acceptance run directory",
    )
    run_metadata = os.fstat(run_directory_fd)
    try:
        _require(
            stat.S_ISDIR(run_metadata.st_mode)
            and run_metadata.st_uid == os.geteuid()
            and stat.S_IMODE(run_metadata.st_mode) == 0o700,
            "GPU acceptance run directory is not owner-private",
        )
    except BaseException:
        os.close(run_directory_fd)
        raise
    model_directory = run_directory / "direct-gpu3-model"
    model_path = model_directory / acceptance_contract.AIMNET2_MODEL_FILENAME
    directory_flags = (
        os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    source_flags = (
        os.O_RDONLY
        | os.O_CLOEXEC
        | os.O_NOFOLLOW
        | getattr(os, "O_NONBLOCK", 0)
    )
    source_directory_fd = -1
    model_directory_fd = -1
    source_fd = -1
    destination_fd = -1
    model_fd = -1
    model_directory_created = False
    try:
        source_directory_fd = _open_absolute_directory_chain(
            source.parent,
            "AIMNet2 source model directory",
        )
        source_directory_snapshot = _model_metadata_snapshot(
            os.fstat(source_directory_fd)
        )
        os.mkdir(
            os.fsencode(model_directory.name),
            mode=0o700,
            dir_fd=run_directory_fd,
        )
        model_directory_created = True
        os.fsync(run_directory_fd)
        run_snapshot = _directory_identity_snapshot(
            os.fstat(run_directory_fd)
        )
        model_directory_fd = os.open(
            os.fsencode(model_directory.name),
            directory_flags,
            dir_fd=run_directory_fd,
        )
        source_fd = os.open(
            os.fsencode(source.name),
            source_flags,
            dir_fd=source_directory_fd,
        )
        source_metadata = os.fstat(source_fd)
        source_snapshot = _model_metadata_snapshot(source_metadata)
        _require(
            stat.S_ISREG(source_metadata.st_mode)
            and source_metadata.st_nlink == 1
            and source_metadata.st_uid == os.geteuid()
            and source_metadata.st_mode & 0o222 == 0,
            "AIMNet2 source model is not a locked single-link file",
        )
        destination_fd = os.open(
            os.fsencode(model_path.name),
            (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | os.O_NOFOLLOW
            ),
            0o600,
            dir_fd=model_directory_fd,
        )
        source_digest = hashlib.sha256()
        copied = 0
        while True:
            try:
                chunk = os.read(source_fd, 1024 * 1024)
            except InterruptedError:
                continue
            if not chunk:
                break
            source_digest.update(chunk)
            copied += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                _require(written > 0, "stable model copy write stalled")
                view = view[written:]
        _require(
            copied == source_metadata.st_size
            and _model_metadata_snapshot(os.fstat(source_fd))
            == source_snapshot,
            "AIMNet2 source model changed during private copy",
        )
        source_path_metadata = os.stat(
            os.fsencode(source.name),
            dir_fd=source_directory_fd,
            follow_symlinks=False,
        )
        _require(
            _model_metadata_snapshot(source_path_metadata)
            == source_snapshot,
            "AIMNet2 source model path changed during private copy",
        )
        source_sha256 = "sha256:" + source_digest.hexdigest()
        _require(
            source_sha256 == acceptance_contract.AIMNET2_MODEL_SHA256,
            "AIMNet2 source model differs from the locked checkpoint",
        )
        source_recheck_fd = _open_absolute_directory_chain(
            source.parent,
            "AIMNet2 source model directory recheck",
        )
        try:
            _require(
                _model_metadata_snapshot(os.fstat(source_recheck_fd))
                == source_directory_snapshot,
                "AIMNet2 source model parent changed during private copy",
            )
            source_path_recheck = os.stat(
                os.fsencode(source.name),
                dir_fd=source_recheck_fd,
                follow_symlinks=False,
            )
            _require(
                _model_metadata_snapshot(source_path_recheck)
                == source_snapshot,
                "AIMNet2 source model path changed during private copy",
            )
        finally:
            os.close(source_recheck_fd)
        os.fsync(destination_fd)
        os.fchmod(destination_fd, 0o400)
        os.fsync(destination_fd)
        destination_metadata = os.fstat(destination_fd)
        destination_snapshot = _model_metadata_snapshot(
            destination_metadata
        )
        _require(
            stat.S_ISREG(destination_metadata.st_mode)
            and destination_metadata.st_nlink == 1
            and destination_metadata.st_uid == os.geteuid()
            and stat.S_IMODE(destination_metadata.st_mode) == 0o400
            and destination_metadata.st_size == copied,
            "stable model copy metadata is invalid",
        )
        os.close(destination_fd)
        destination_fd = -1
        os.fsync(model_directory_fd)
        directory_snapshot = _model_metadata_snapshot(
            os.fstat(model_directory_fd)
        )
        evidence: dict[str, Any] = {
            "path": str(model_path),
            "sha256": source_sha256,
            "directory": directory_snapshot,
            **destination_snapshot,
        }
        model_fd = os.open(
            os.fsencode(model_path.name),
            source_flags,
            dir_fd=model_directory_fd,
        )
        _verify_stable_model_copy(evidence, descriptor=model_fd)
        run_directory_recheck_fd = _open_absolute_directory_chain(
            run_directory,
            "GPU acceptance run directory recheck",
        )
        try:
            _require(
                _directory_identity_snapshot(
                    os.fstat(run_directory_recheck_fd)
                )
                == run_snapshot,
                "GPU acceptance run directory path changed",
            )
        finally:
            os.close(run_directory_recheck_fd)
        stable_copy = _StableModelCopy(
            evidence=evidence,
            model_descriptor=model_fd,
            directory_descriptor=model_directory_fd,
            run_directory_descriptor=run_directory_fd,
            run_directory_metadata=run_snapshot,
        )
        model_fd = -1
        model_directory_fd = -1
        run_directory_fd = -1
        return stable_copy
    except BaseException:
        if destination_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(destination_fd)
            destination_fd = -1
        if model_directory_fd >= 0:
            try:
                candidate = os.stat(
                    os.fsencode(model_path.name),
                    dir_fd=model_directory_fd,
                    follow_symlinks=False,
                )
            except OSError:
                candidate = None
            if candidate is not None:
                with contextlib.suppress(OSError):
                    os.unlink(
                        os.fsencode(model_path.name),
                        dir_fd=model_directory_fd,
                    )
            with contextlib.suppress(OSError):
                os.fsync(model_directory_fd)
        if model_directory_created and run_directory_fd >= 0:
            with contextlib.suppress(OSError):
                os.rmdir(
                    os.fsencode(model_directory.name),
                    dir_fd=run_directory_fd,
                )
            with contextlib.suppress(OSError):
                os.fsync(run_directory_fd)
        raise
    finally:
        for descriptor in (
            destination_fd,
            model_fd,
            source_fd,
            model_directory_fd,
            source_directory_fd,
            run_directory_fd,
        ):
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)


def _remove_stable_model_copy(stable_copy: _StableModelCopy) -> bool:
    _require(not stable_copy.removed, "stable model copy was already removed")
    evidence = stable_copy.evidence
    path = Path(evidence["path"])
    directory_fd = stable_copy.directory_descriptor
    run_directory_fd = stable_copy.run_directory_descriptor
    verification_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    try:
        _verify_stable_model_copy(
            evidence,
            descriptor=stable_copy.model_descriptor,
        )
    except BaseException as exc:
        verification_error = exc
    try:
        try:
            _require(
                _directory_identity_snapshot(os.fstat(directory_fd))
                == {
                    key: evidence["directory"][key]
                    for key in ("device", "inode", "mode", "uid")
                }
                and _directory_identity_snapshot(os.fstat(run_directory_fd))
                == stable_copy.run_directory_metadata,
                "stable model directory handle changed before cleanup",
            )
            current = os.stat(
                os.fsencode(path.name),
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            expected = _expected_model_metadata(evidence)
            _require(
                _model_metadata_snapshot(current) == expected,
                "stable model copy changed before cleanup",
            )
            os.unlink(os.fsencode(path.name), dir_fd=directory_fd)
            os.fsync(directory_fd)
            os.rmdir(
                os.fsencode(path.parent.name),
                dir_fd=run_directory_fd,
            )
            os.fsync(run_directory_fd)
            stable_copy.removed = True
        except BaseException as exc:
            cleanup_error = exc
    finally:
        for attribute in (
            "model_descriptor",
            "directory_descriptor",
            "run_directory_descriptor",
        ):
            descriptor = getattr(stable_copy, attribute)
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
                setattr(stable_copy, attribute, -1)
    if verification_error is not None:
        raise AcceptanceHarnessError(
            "stable model path CAS failed before exact-handle cleanup"
        ) from verification_error
    if cleanup_error is not None:
        raise AcceptanceHarnessError(
            "stable model exact-handle cleanup failed"
        ) from cleanup_error
    _require(
        not os.path.lexists(path)
        and not os.path.lexists(path.parent),
        "stable model copy survived cleanup",
    )
    return True


def _model_descriptor_load_path(descriptor: int) -> str:
    _require(
        isinstance(descriptor, int)
        and not isinstance(descriptor, bool)
        and descriptor > 2,
        "stable model descriptor is invalid",
    )
    path = f"/proc/self/fd/{descriptor}"
    try:
        metadata = os.stat(path)
    except OSError as exc:
        raise AcceptanceHarnessError(
            "stable model descriptor path is unavailable"
        ) from exc
    _require(
        stat.S_ISREG(metadata.st_mode),
        "stable model descriptor path is not a regular file",
    )
    return path


def _run_calculations_from_stable_model_copy(
    evidence: dict[str, Any],
    descriptor: int,
) -> dict[str, Any]:
    """Load AIMNet through one inherited, read-only descriptor."""

    _verify_stable_model_copy(evidence, descriptor=descriptor)
    descriptor_path = _model_descriptor_load_path(descriptor)
    _require(
        _model_metadata_snapshot(os.stat(descriptor_path))
        == _expected_model_metadata(evidence),
        "stable model descriptor path differs from its sealed identity",
    )
    report = smoke_runtime.run_calculations(
        {"default_model_path": descriptor_path}
    )
    _require(
        isinstance(report, dict)
        and report.get("preflight")
        == {"default_model_path": descriptor_path},
        "AIMNet2 calculation did not preserve its descriptor-bound preflight",
    )
    _verify_stable_model_copy(evidence, descriptor=descriptor)
    # Preserve the durable private-copy path in the report.  The loader used
    # the /proc descriptor alias above, which is bound to this exact inode.
    report["preflight"] = {"default_model_path": evidence["path"]}
    return report


def _leased_child(spec_path: Path, output_path: Path) -> int:
    try:
        from gpu_resource import GpuBrokerClient, scope_control_group

        for path, name in (
            (spec_path, "leased direct specification"),
            (output_path.parent, "leased direct output directory"),
        ):
            metadata = path.lstat()
            _require(
                metadata.st_uid == os.geteuid()
                and not path.is_symlink()
                and (
                    stat.S_ISREG(metadata.st_mode)
                    if path == spec_path
                    else stat.S_ISDIR(metadata.st_mode)
                ),
                f"{name} is not owner-private",
            )
        _require(
            stat.S_IMODE(spec_path.stat().st_mode) == 0o600
            and stat.S_IMODE(output_path.parent.stat().st_mode) == 0o700
            and output_path.parent == spec_path.parent,
            "leased direct files are not in one private run directory",
        )
        spec = _load_json(spec_path, "leased direct specification")
        _require(
            set(spec)
            == {
                "schema_version",
                "state",
                "default_model_path",
                "model_copy",
                "model_fd",
                "gpu_index",
                "gpu_uuid",
                "lease_id",
                "fencing_token",
                "broker_instance_id",
                "broker_socket",
                "workload_pid",
                "workload_process_start_ticks",
                "workload_cgroup",
                "mps_pipe_directory",
            }
            and spec["schema_version"] == 1
            and spec["state"] == "registered"
            and spec["gpu_index"] == 3
            and spec["gpu_uuid"] == GPU_UUIDS["3"]
            and spec["workload_pid"] == os.getpid()
            and spec["workload_process_start_ticks"]
            == _read_proc_start_ticks(os.getpid()),
            "leased child specification is not bound to this GPU3 process",
        )
        cgroup_lines = Path("/proc/self/cgroup").read_text(
            encoding="ascii"
        ).splitlines()
        _require(
            len(cgroup_lines) == 1
            and cgroup_lines[0].startswith("0::/")
            and cgroup_lines[0][3:] == spec["workload_cgroup"]
            and spec["workload_cgroup"] == scope_control_group(spec["lease_id"]),
            "leased child is outside the exact Broker transient scope",
        )
        mps_match = re.fullmatch(
            r"/proc/self/fd/([1-9][0-9]*)",
            str(spec["mps_pipe_directory"]),
        )
        broker_match = re.fullmatch(
            r"/proc/self/fd/([1-9][0-9]*)/broker\.sock",
            str(spec["broker_socket"]),
        )
        _require(
            mps_match is not None and broker_match is not None,
            "leased child descriptor authority paths are invalid",
        )
        mps_descriptor = int(mps_match.group(1))
        broker_root_descriptor = int(broker_match.group(1))
        _require(
            mps_descriptor > 2
            and broker_root_descriptor > 2
            and mps_descriptor != broker_root_descriptor
            and isinstance(spec["model_fd"], int)
            and spec["model_fd"] > 2
            and spec["model_fd"]
            not in {mps_descriptor, broker_root_descriptor},
            "leased child descriptor authority aliases unrelated resources",
        )
        mps_metadata = os.fstat(mps_descriptor)
        broker_root_metadata = os.fstat(broker_root_descriptor)
        control_metadata = os.stat(
            b"control",
            dir_fd=mps_descriptor,
            follow_symlinks=False,
        )
        broker_metadata = os.stat(
            b"broker.sock",
            dir_fd=broker_root_descriptor,
            follow_symlinks=False,
        )
        _require(
            stat.S_ISDIR(mps_metadata.st_mode)
            and stat.S_ISDIR(broker_root_metadata.st_mode)
            and mps_metadata.st_uid == os.geteuid()
            and broker_root_metadata.st_uid == os.geteuid()
            and mps_metadata.st_gid == os.getegid()
            and broker_root_metadata.st_gid == os.getegid()
            and stat.S_IMODE(mps_metadata.st_mode) == 0o700
            and stat.S_IMODE(broker_root_metadata.st_mode) == 0o700
            and control_metadata.st_uid == os.geteuid()
            and control_metadata.st_gid == os.getegid()
            and (
                stat.S_ISFIFO(control_metadata.st_mode)
                or stat.S_ISSOCK(control_metadata.st_mode)
            )
            and broker_metadata.st_uid == os.geteuid()
            and broker_metadata.st_gid == os.getegid()
            and stat.S_ISSOCK(broker_metadata.st_mode),
            "leased child descriptor authority changed",
        )
        broker = GpuBrokerClient(spec["broker_socket"])
        status = broker.status()
        leases = status.get("leases")
        matches = [
            lease
            for lease in leases
            if isinstance(lease, dict)
            and lease.get("lease_id") == spec["lease_id"]
        ] if isinstance(leases, list) else []
        _require(
            len(matches) == 1
            and status.get("broker_instance_id") == spec["broker_instance_id"]
            and matches[0].get("status") == "active"
            and matches[0].get("fencing_token") == spec["fencing_token"]
            and matches[0].get("gpu_index") == 3
            and matches[0].get("gpu_uuid") == GPU_UUIDS["3"]
            and matches[0].get("workload_pid") == os.getpid()
            and matches[0].get("workload_process_start_ticks")
            == spec["workload_process_start_ticks"]
            and matches[0].get("workload_cgroup") == spec["workload_cgroup"],
            "leased child Broker workload proof is absent or stale",
        )
        model_copy = _verify_stable_model_copy(spec["model_copy"])
        _require(
            spec["default_model_path"] == model_copy["path"],
            "leased child load path differs from the stable model copy",
        )
        model_fd = spec["model_fd"]
        _verify_model_descriptor(model_fd, model_copy)
        dangerous = {
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "PYTHONPATH",
            "PYTHONHOME",
            "DOCKER_HOST",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        }
        _require(
            not dangerous.intersection(os.environ)
            and os.environ.get("CUDA_VISIBLE_DEVICES") == GPU_UUIDS["3"]
            and os.environ.get("CUDA_MPS_PIPE_DIRECTORY")
            == spec["mps_pipe_directory"],
            "leased child environment is not isolated",
        )
        report = _run_calculations_from_stable_model_copy(
            model_copy,
            model_fd,
        )
        report["model_sha256"] = acceptance_contract.AIMNET2_MODEL_SHA256
        report["gpu_index"] = spec["gpu_index"]
        report["gpu_uuid"] = spec["gpu_uuid"]
        acceptance_contract.validate_gpu3_direct_result(report)
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


def _prepare_direct_launch(
    *,
    resolved: dict[str, str],
    managed: Any,
    model_copy: _StableModelCopy,
    gpu_index: str,
    run_directory: Path,
) -> tuple[
    Path,
    Path,
    int,
    int,
    int,
    int,
    dict[str, str],
    dict[str, str],
    tuple[str, ...],
]:
    from gpu_resource import mps_client_environment, transient_scope_command

    spec_path = run_directory / f"direct-gpu{gpu_index}-spec.json"
    output_path = run_directory / f"direct-gpu{gpu_index}-result.json"
    read_fd = -1
    write_fd = -1
    mps_pipe_fd = -1
    broker_root_fd = -1
    try:
        try:
            broker_root_fd = os.open(
                Path(resolved["MONOMER_DFT_GPU_MPS_PIPE_ROOT"]),
                os.O_RDONLY
                | os.O_CLOEXEC
                | os.O_DIRECTORY,
            )
        except OSError as exc:
            raise AcceptanceHarnessError(
                "direct GPU Broker root descriptor cannot be opened"
            ) from exc
        broker_root_metadata = os.fstat(broker_root_fd)
        _require(
            stat.S_ISDIR(broker_root_metadata.st_mode)
            and broker_root_metadata.st_uid == os.geteuid()
            and broker_root_metadata.st_gid == os.getegid()
            and stat.S_IMODE(broker_root_metadata.st_mode) == 0o700,
            "direct GPU Broker root descriptor is unsafe",
        )
        child_broker_socket = Path(
            f"/proc/self/fd/{broker_root_fd}/broker.sock"
        )
        direct_pipe_authority = resolved.get(
            f"NEXPOLY_DFT_GPU{gpu_index}_MPS_PIPE_AUTHORITY"
        )
        pipe_directory = (
            Path(direct_pipe_authority)
            if direct_pipe_authority
            else (
                Path(resolved["MONOMER_DFT_GPU_MPS_PIPE_ROOT"])
                / f"mps-{gpu_index}"
                / "pipe"
            )
        )
        try:
            mps_pipe_fd = os.open(
                pipe_directory,
                os.O_RDONLY
                | os.O_CLOEXEC
                | os.O_DIRECTORY,
            )
        except OSError as exc:
            raise AcceptanceHarnessError(
                "direct GPU MPS pipe descriptor cannot be opened"
            ) from exc
        pipe_metadata = os.fstat(mps_pipe_fd)
        _require(
            stat.S_ISDIR(pipe_metadata.st_mode)
            and pipe_metadata.st_uid == os.geteuid()
            and pipe_metadata.st_gid == os.getegid()
            and stat.S_IMODE(pipe_metadata.st_mode) == 0o700,
            "direct GPU MPS pipe descriptor is unsafe",
        )
        child_pipe_directory = Path(
            f"/proc/self/fd/{mps_pipe_fd}"
        )
        local_pipe_directory = Path(
            f"/proc/{os.getpid()}/fd/{mps_pipe_fd}"
        )
        _write_private_json(
            spec_path,
            {
                "schema_version": 1,
                "state": "gated",
                "default_model_path": model_copy.evidence["path"],
                "model_copy": model_copy.evidence,
                "model_fd": model_copy.model_descriptor,
                "gpu_index": int(gpu_index),
                "gpu_uuid": GPU_UUIDS[gpu_index],
                "lease_id": managed.lease.lease_id,
                "fencing_token": managed.lease.fencing_token,
                "broker_instance_id": managed.lease.broker_instance_id,
                "broker_socket": str(child_broker_socket),
                "workload_pid": None,
                "workload_process_start_ticks": None,
                "workload_cgroup": None,
                "mps_pipe_directory": str(child_pipe_directory),
            },
        )
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        os.set_inheritable(read_fd, True)
        mps_environment = mps_client_environment(
            managed.lease,
            pipe_root=Path(resolved["MONOMER_DFT_GPU_MPS_PIPE_ROOT"]),
            pipe_directories={
                int(gpu_index): local_pipe_directory
            },
        )
        mps_environment["CUDA_MPS_PIPE_DIRECTORY"] = str(
            child_pipe_directory
        )
        child_environment = _safe_command_environment(extra=mps_environment)
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
        return (
            spec_path,
            output_path,
            read_fd,
            write_fd,
            mps_pipe_fd,
            broker_root_fd,
            mps_environment,
            child_environment,
            scoped_command,
        )
    except BaseException:
        for descriptor in (
            read_fd,
            write_fd,
            mps_pipe_fd,
            broker_root_fd,
        ):
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
        raise


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
        wait_for_scope_membership,
    )

    _require(
        gpu_index == "3" and placement == "overflow",
        "direct acceptance is permitted only for GPU3 overflow",
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
    try:
        model_copy = _prepare_stable_model_copy(
            default_model_path,
            run_directory,
        )
    except BaseException:
        managed.close()
        raise
    model_copy_removed = False

    try:
        (
            spec_path,
            output_path,
            read_fd,
            write_fd,
            mps_pipe_fd,
            broker_root_fd,
            mps_environment,
            child_environment,
            scoped_command,
        ) = _prepare_direct_launch(
            resolved=resolved,
            managed=managed,
            model_copy=model_copy,
            gpu_index=gpu_index,
            run_directory=run_directory,
        )
    except BaseException:
        try:
            _remove_stable_model_copy(model_copy)
        finally:
            managed.close()
        raise
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
            pass_fds=(
                read_fd,
                model_copy.model_descriptor,
                mps_pipe_fd,
                broker_root_fd,
            ),
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
        _write_private_json(
            spec_path,
            {
                "schema_version": 1,
                "state": "registered",
                "default_model_path": model_copy.evidence["path"],
                "model_copy": model_copy.evidence,
                "model_fd": model_copy.model_descriptor,
                "gpu_index": int(gpu_index),
                "gpu_uuid": GPU_UUIDS[gpu_index],
                "lease_id": registered.lease_id,
                "fencing_token": registered.fencing_token,
                "broker_instance_id": registered.broker_instance_id,
                "broker_socket": (
                    f"/proc/self/fd/{broker_root_fd}/broker.sock"
                ),
                "workload_pid": process.pid,
                "workload_process_start_ticks": process_start_ticks,
                "workload_cgroup": registered.workload_cgroup,
                "mps_pipe_directory": mps_environment[
                    "CUDA_MPS_PIPE_DIRECTORY"
                ],
            },
        )
        os.close(mps_pipe_fd)
        mps_pipe_fd = -1
        os.close(broker_root_fd)
        broker_root_fd = -1
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
        acceptance_contract.validate_gpu3_direct_result(report)
        _require(
            _verify_stable_model_copy(
                model_copy.evidence,
                descriptor=model_copy.model_descriptor,
            )
            == model_copy.evidence,
            "stable model copy changed after the leased child exited",
        )
        model_copy_removed = _remove_stable_model_copy(model_copy)
        lease_evidence = {
            "lease_id": registered.lease_id,
            "fencing_token": registered.fencing_token,
            "broker_instance_id": registered.broker_instance_id,
            "gpu_index": int(gpu_index),
            "gpu_uuid": registered.gpu_uuid,
            "request_id": request_id,
            "workload_pid": process.pid,
            "process_start_ticks": process_start_ticks,
            "workload_cgroup": registered.workload_cgroup,
            "report_sha256": _sha256_file(output_path),
            "model_copy_sha256": model_copy.evidence["sha256"],
            "model_copy_path_sha256": (
                acceptance_contract.canonical_json_digest(
                    {"path": model_copy.evidence["path"]}
                )
            ),
            "model_copy_removed": model_copy_removed,
        }
        acceptance_contract.validate_gpu3_actual_lease(lease_evidence)
        _require(
            lease_evidence["report_sha256"]
            == acceptance_contract.canonical_json_file_digest(report),
            "direct AIMNet file digest differs from its scientific result",
        )
        return report, lease_evidence
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
        for descriptor in (
            read_fd,
            write_fd,
            mps_pipe_fd,
            broker_root_fd,
        ):
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
        try:
            if (
                not model_copy_removed
                and not model_copy.removed
                and model_copy.model_descriptor >= 0
            ):
                _remove_stable_model_copy(model_copy)
        finally:
            managed.close()


def _docker_gpu3_claim() -> dict[str, Any] | None:
    docker_environment = _local_docker_environment()
    container_ids = _run(
        "docker", "ps", "-q", timeout=10.0, env=docker_environment
    ).stdout.split()
    if not container_ids:
        return None
    inspection = _run(
        "docker",
        "inspect",
        *container_ids,
        timeout=30.0,
        env=docker_environment,
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
                        "inspection_sha256": (
                            acceptance_contract.canonical_json_digest(
                                {
                                    "container_id": full_id,
                                    "container_name": name,
                                    "request": request,
                                }
                            )
                        ),
                        "observed_at": dt.datetime.now(dt.UTC)
                        .replace(microsecond=0)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    }
                )
    _require(len(matches) <= 1, "GPU3 has multiple foreign Docker claims")
    return matches[0] if matches else None


def _bind_gpu3_claim_cas(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    identity_fields = {
        "kind",
        "container_id",
        "container_name",
        "device_request_sha256",
        "inspection_sha256",
    }
    _require(
        {key: before.get(key) for key in identity_fields}
        == {key: after.get(key) for key in identity_fields},
        "GPU3 Docker claim changed across Broker rejection",
    )
    return {
        "kind": before["kind"],
        "container_id": before["container_id"],
        "container_name": before["container_name"],
        "device_request_sha256": before["device_request_sha256"],
        "inspection_before_sha256": before["inspection_sha256"],
        "inspection_after_sha256": after["inspection_sha256"],
        "observed_before_at": before["observed_at"],
        "observed_after_at": after["observed_at"],
    }


def _broker_rejection_status_projection(
    status_payload: dict[str, Any],
) -> dict[str, Any]:
    """Return the closed, non-sensitive Broker state used for GPU3 proof."""

    _require(
        isinstance(status_payload, dict),
        "GPU3 Broker status is not an object",
    )
    leases = status_payload.get("leases")
    usage = status_payload.get("usage_mib")
    quarantined = status_payload.get("quarantined_gpus")
    instance_id = status_payload.get("broker_instance_id")
    waiters = status_payload.get("waiters")
    _require(
        status_payload.get("schema_version") == 1
        and isinstance(instance_id, str)
        and re.fullmatch(r"[0-9a-f]{32}", instance_id) is not None
        and isinstance(status_payload.get("draining"), bool)
        and isinstance(leases, list)
        and isinstance(usage, dict)
        and isinstance(quarantined, dict)
        and isinstance(waiters, int)
        and not isinstance(waiters, bool)
        and waiters >= 0,
        "GPU3 Broker status cannot be projected canonically",
    )
    gpu3_leases: list[str] = []
    for lease in leases:
        _require(
            isinstance(lease, dict),
            "GPU3 Broker lease inventory is invalid",
        )
        if lease.get("gpu_uuid") != GPU_UUIDS["3"]:
            continue
        lease_id = lease.get("lease_id")
        _require(
            isinstance(lease_id, str)
            and re.fullmatch(r"[0-9a-f]{32}", lease_id) is not None,
            "GPU3 Broker lease identity is invalid",
        )
        gpu3_leases.append(lease_id)
    gpu3_usage = usage.get("3")
    _require(
        isinstance(gpu3_usage, int)
        and not isinstance(gpu3_usage, bool)
        and gpu3_usage >= 0,
        "GPU3 Broker usage is invalid",
    )
    projection = {
        "schema_version": 1,
        "broker_instance_id": instance_id,
        "draining": status_payload["draining"],
        "gpu3_uuid": GPU_UUIDS["3"],
        "gpu3_usage_mib": gpu3_usage,
        "gpu3_lease_ids": sorted(gpu3_leases),
        "gpu3_quarantined": GPU_UUIDS["3"] in quarantined,
        "waiters": waiters,
    }
    _require(
        projection["draining"] is False
        and projection["gpu3_usage_mib"] == 0
        and projection["gpu3_lease_ids"] == []
        and projection["gpu3_quarantined"] is False
        and projection["waiters"] == 0,
        "GPU3 rejection status has another Broker-side blocker",
    )
    return projection


def _finalize_gpu3_rejection(
    proof: dict[str, Any],
    *,
    claim: dict[str, Any],
) -> dict[str, Any]:
    rejection = {
        **proof,
        "claim_sha256": acceptance_contract.canonical_json_digest(claim),
    }
    rejection["broker_report_sha256"] = (
        acceptance_contract.canonical_json_digest(rejection)
    )
    return rejection


def _prove_gpu3_rejection(
    *,
    resolved: dict[str, str],
) -> dict[str, Any]:
    from gpu_resource import GpuBrokerClient, GpuBrokerClientError

    broker = GpuBrokerClient(resolved["MONOMER_DFT_GPU_BROKER_UDS"])
    request_id = f"dft-acceptance-gpu3-reject-{uuid.uuid4().hex}"
    before_status = broker.status()
    before_projection = _broker_rejection_status_projection(before_status)
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
        after_status = broker.status()
        after_projection = _broker_rejection_status_projection(after_status)
        _require(
            after_projection == before_projection,
            "GPU3 Broker state changed while proving external rejection",
        )
        return {
            "code": exc.code,
            "gpu_index": 3,
            "gpu_uuid": GPU_UUIDS["3"],
            "placement": "overflow",
            "request_id": request_id,
            "blocked_reason": acceptance_contract.GPU3_BLOCKED_REASON,
            "broker_instance_id": before_projection["broker_instance_id"],
            "before_status": before_projection,
            "before_status_sha256": (
                acceptance_contract.canonical_json_digest(before_projection)
            ),
            "after_status": after_projection,
            "after_status_sha256": (
                acceptance_contract.canonical_json_digest(after_projection)
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


def _process_identity(pid: int) -> dict[str, Any]:
    _require(
        isinstance(pid, int) and not isinstance(pid, bool) and pid > 0,
        "acceptance process PID is invalid",
    )
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
        command = Path(f"/proc/{pid}/cmdline").read_bytes()
        start_ticks = _read_proc_start_ticks(pid)
    except OSError as exc:
        raise AcceptanceHarnessError(
            f"acceptance process identity disappeared: {pid}"
        ) from exc
    _require(bool(command) and command.endswith(b"\x00"), "process command is invalid")
    return {
        "pid": pid,
        "process_start_ticks": start_ticks,
        "cwd": cwd,
        "command_sha256": _sha256_bytes(command),
    }


def _stack_running() -> bool:
    result = _run(
        str(SCRIPT_ROOT / "monomer_dft_worker_ctl.sh"),
        "status",
        timeout=15.0,
        check=False,
        env=_safe_command_environment(
            extra={"NEXPOLY_DFT_FORMAL_ACCEPTANCE": "1"}
        ),
    )
    return result.returncode == 0


def _candidate_image_tags(
    *,
    project_name: str,
    authority_sha: str,
) -> dict[str, str]:
    _require(
        re.fullmatch(
            r"nexpoly_dft_fresh_[a-z0-9][a-z0-9_-]{0,40}",
            project_name,
        )
        is not None
        and SHA_RE.fullmatch(authority_sha) is not None,
        "candidate image identity is invalid",
    )
    suffix = f"{project_name}-{authority_sha}"
    return {
        "backend": f"nexpoly-dft-acceptance-backend:{suffix}",
        "web": f"nexpoly-dft-acceptance-web:{suffix}",
    }


def _docker_image_tag_snapshot(
    tags: tuple[str, ...] | list[str],
) -> dict[str, str | None]:
    environment = _local_docker_environment()
    snapshot: dict[str, str | None] = {}
    for tag in tags:
        _require(
            isinstance(tag, str)
            and re.fullmatch(
                r"[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}",
                tag,
            )
            is not None,
            "Docker image tag inventory contains an unsafe tag",
        )
        result = _run(
            "docker",
            "image",
            "inspect",
            tag,
            timeout=15.0,
            check=False,
            env=environment,
        )
        if result.returncode != 0:
            _require(
                "No such image" in result.stderr,
                "Docker image tag absence could not be proven",
            )
            snapshot[tag] = None
            continue
        try:
            images = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AcceptanceHarnessError(
                "Docker image tag inventory is invalid"
            ) from exc
        _require(
            isinstance(images, list)
            and len(images) == 1
            and isinstance(images[0], dict)
            and isinstance(images[0].get("Id"), str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", images[0]["Id"])
            is not None,
            "Docker image tag identity is invalid",
        )
        snapshot[tag] = images[0]["Id"]
    return snapshot


def _stack_command(
    command: str,
    timeout: float,
    *,
    project_name: str,
    authority_sha: str,
    run_kind: str,
    authority_images: dict[str, Any] | None,
    gpu_authority_environment: dict[str, str],
) -> None:
    _require(
        re.fullmatch(r"nexpoly_dft_fresh_[a-z0-9][a-z0-9_-]{0,40}", project_name)
        is not None
        and SHA_RE.fullmatch(authority_sha) is not None,
        "fresh stack identity is invalid",
    )
    _require(
        run_kind in {"candidate-tree", "final-main"},
        "fresh stack run kind is invalid",
    )
    required_gpu_authority = {
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
    }
    _require(
        isinstance(gpu_authority_environment, dict)
        and required_gpu_authority
        <= gpu_authority_environment.keys()
        and all(
            isinstance(key, str)
            and isinstance(value, str)
            and value
            for key, value in gpu_authority_environment.items()
        ),
        "fresh stack lacks complete GPU descriptor authority",
    )
    environment = _local_docker_environment()
    overrides = {
        "NEXPOLY_DFT_ACCEPTANCE_PROJECT_NAME": project_name,
        "NEXPOLY_DFT_AUTHORITY_SHA": authority_sha,
        "NEXPOLY_DFT_ACCEPTANCE_IMAGE_MODE": run_kind,
    }
    if run_kind == "final-main":
        _require(
            isinstance(authority_images, dict)
            and set(authority_images) == {"backend", "web"},
            "final-main stack lacks exact OCI images",
        )
        overrides.update(
            {
                "NEXPOLY_DFT_BACKEND_IMAGE_REF": authority_images["backend"][
                    "digest_ref"
                ],
                "NEXPOLY_DFT_WEB_IMAGE_REF": authority_images["web"][
                    "digest_ref"
                ],
            }
        )
    else:
        candidate_tags = _candidate_image_tags(
            project_name=project_name,
            authority_sha=authority_sha,
        )
        overrides.update(
            {
                "NEXPOLY_DFT_BACKEND_IMAGE_REF": candidate_tags["backend"],
                "NEXPOLY_DFT_WEB_IMAGE_REF": candidate_tags["web"],
            }
        )
    environment.update(overrides)
    environment.update(gpu_authority_environment)
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


def _git_nul_paths(payload: bytes, name: str) -> list[bytes]:
    _require(
        not payload or payload.endswith(b"\0"),
        f"production {name} path inventory is truncated",
    )
    raw_paths = payload[:-1].split(b"\0") if payload else []
    return [_normalize_repo_path(path, name=name) for path in raw_paths]


def _normalize_repo_path(
    path: bytes,
    *,
    name: str,
    allow_directory_suffix: bool = False,
) -> bytes:
    _require(
        isinstance(path, bytes)
        and bool(path)
        and not path.startswith(b"/")
        and b"\0" not in path
        and len(path) <= 4096,
        f"production {name} contains an unsafe path",
    )
    if allow_directory_suffix and path.endswith(b"/"):
        path = path[:-1]
    components = path.split(b"/")
    _require(
        bool(path)
        and len(components) <= PRODUCTION_CAS_MAX_DEPTH
        and all(component not in {b"", b".", b".."} for component in components)
        and components[0] != b".git",
        f"production {name} contains an unsafe path",
    )
    return path


def _status_boundaries(status: bytes) -> list[tuple[bytes, bytes]]:
    _require(
        not status or status.endswith(b"\0"),
        "production Git status inventory is truncated",
    )
    records = status[:-1].split(b"\0") if status else []
    boundaries: list[tuple[bytes, bytes]] = []
    index = 0
    while index < len(records):
        record = records[index]
        _require(
            len(record) >= 4 and record[2:3] == b" ",
            "production Git status inventory is malformed",
        )
        code = record[:2]
        _require(
            code in {b"??", b"!!"},
            "production tracked worktree status differs from the fixed baseline",
        )
        path = _normalize_repo_path(
            record[3:],
            name="Git status",
            allow_directory_suffix=code in {b"??", b"!!"},
        )
        if code in {b"??", b"!!"}:
            boundaries.append((code, path))
        index += 1
    _require(
        len(boundaries) <= PRODUCTION_CAS_MAX_PATHS
        and len(set(boundaries)) == len(boundaries),
        "production Git status boundary inventory is invalid",
    )
    return sorted(boundaries)


def _framed_digest(records: list[tuple[bytes, ...]]) -> str:
    digest = hashlib.sha256()
    for record in sorted(records):
        digest.update(len(record).to_bytes(4, "big"))
        for field in record:
            digest.update(len(field).to_bytes(8, "big"))
            digest.update(field)
    return "sha256:" + digest.hexdigest()


def _metadata_fields(metadata: os.stat_result) -> tuple[bytes, ...]:
    return tuple(
        str(value).encode("ascii")
        for value in (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_nlink,
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
    )


def _git_authority_directory_metadata_fields(
    metadata: os.stat_result,
) -> tuple[bytes, ...]:
    """Return stable identity fields for an enumerated Git directory.

    Git may create and remove an optional lock file during an otherwise
    read-only command.  That changes the directory size and timestamps without
    changing any selected authority file.  The inventory still enumerates the
    directory twice and uses the complete metadata for its in-flight CAS, while
    the sealed baseline records only stable directory identity and ownership.
    Regular authority files continue to bind their complete metadata and
    content digest.
    """

    return tuple(
        str(value).encode("ascii")
        for value in (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_nlink,
            metadata.st_uid,
            metadata.st_gid,
        )
    ) + (b"-", b"-", b"-")


def _same_metadata(
    left: os.stat_result,
    right: os.stat_result,
) -> bool:
    return _metadata_fields(left) == _metadata_fields(right)


def _hash_regular_file(
    directory_fd: int,
    name: bytes,
    expected: os.stat_result,
    *,
    remaining_bytes: int,
    max_file_bytes: int = PRODUCTION_CAS_MAX_FILE_BYTES,
) -> tuple[bytes, int]:
    _require(
        0 <= expected.st_size <= max_file_bytes
        and expected.st_size <= remaining_bytes,
        "production worktree content exceeds the read-only CAS byte budget",
    )
    flags = (
        os.O_RDONLY
        | os.O_CLOEXEC
        | os.O_NOFOLLOW
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise AcceptanceHarnessError(
            "production worktree file could not be opened safely"
        ) from exc
    content = hashlib.sha256()
    count = 0
    try:
        before = os.fstat(descriptor)
        _require(
            stat.S_ISREG(before.st_mode)
            and before.st_nlink == 1
            and _same_metadata(before, expected),
            "production worktree file identity changed before hashing",
        )
        while True:
            try:
                chunk = os.read(descriptor, 1024 * 1024)
            except InterruptedError:
                continue
            if not chunk:
                break
            count += len(chunk)
            _require(
                count <= expected.st_size
                and count <= remaining_bytes,
                "production worktree content exceeded its bounded size",
            )
            content.update(chunk)
        after = os.fstat(descriptor)
        _require(
            count == expected.st_size
            and _same_metadata(after, expected),
            "production worktree file changed while it was hashed",
        )
    finally:
        os.close(descriptor)
    return content.hexdigest().encode("ascii"), count


def _production_worktree_inventory(
    *,
    tracked: set[bytes],
    ignored: set[bytes],
    untracked: set[bytes],
) -> dict[str, Any]:
    classified_files = tracked | ignored | untracked
    _require(
        len(classified_files) <= PRODUCTION_CAS_MAX_PATHS
        and not (tracked & ignored)
        and not (tracked & untracked)
        and not (ignored & untracked),
        "production Git path classifications overlap or exceed their bound",
    )
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        root_fd = os.open(PRODUCTION_REPO_ROOT, flags)
    except OSError as exc:
        raise AcceptanceHarnessError(
            "production repository root could not be opened safely"
        ) from exc
    records: list[tuple[bytes, ...]] = []
    observed_files: set[bytes] = set()
    content_bytes = 0
    content_bytes_by_class = {
        b"tracked": 0,
        b"ignored": 0,
        b"untracked": 0,
    }

    def walk(directory_fd: int, prefix: bytes, depth: int) -> None:
        nonlocal content_bytes
        _require(
            depth <= PRODUCTION_CAS_MAX_DEPTH,
            "production worktree directory depth exceeds its bound",
        )
        before_directory = os.fstat(directory_fd)
        _require(
            stat.S_ISDIR(before_directory.st_mode),
            "production worktree traversal escaped a directory",
        )
        try:
            with os.scandir(directory_fd) as iterator:
                names = sorted(os.fsencode(entry.name) for entry in iterator)
        except OSError as exc:
            raise AcceptanceHarnessError(
                "production worktree directory could not be enumerated safely"
            ) from exc
        _require(
            len(records) + len(names) <= PRODUCTION_CAS_MAX_ENTRIES,
            "production worktree inventory exceeds its entry bound",
        )
        for name in names:
            if not prefix and name == b".git":
                continue
            _require(
                name not in {b"", b".", b".."}
                and b"/" not in name
                and b"\0" not in name,
                "production worktree contains an unsafe directory entry",
            )
            relative = name if not prefix else prefix + b"/" + name
            _normalize_repo_path(relative, name="filesystem inventory")
            try:
                metadata = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise AcceptanceHarnessError(
                    "production worktree entry disappeared during inventory"
                ) from exc
            if stat.S_ISDIR(metadata.st_mode):
                record_type = b"directory"
                content_digest = b"-"
                try:
                    child_fd = os.open(
                        name,
                        flags,
                        dir_fd=directory_fd,
                    )
                except OSError as exc:
                    raise AcceptanceHarnessError(
                        "production worktree directory could not be opened safely"
                    ) from exc
                try:
                    _require(
                        _same_metadata(os.fstat(child_fd), metadata),
                        "production worktree directory identity changed",
                    )
                    walk(child_fd, relative, depth + 1)
                    _require(
                        _same_metadata(os.fstat(child_fd), metadata),
                        "production worktree directory changed during inventory",
                    )
                finally:
                    os.close(child_fd)
            else:
                _require(
                    relative in classified_files,
                    "production worktree contains a Git-unclassified leaf",
                )
                observed_files.add(relative)
                classification = (
                    b"tracked"
                    if relative in tracked
                    else b"ignored"
                    if relative in ignored
                    else b"untracked"
                )
                if stat.S_ISREG(metadata.st_mode):
                    record_type = b"regular"
                    content_digest, hashed = _hash_regular_file(
                        directory_fd,
                        name,
                        metadata,
                        remaining_bytes=(
                            PRODUCTION_CAS_MAX_TOTAL_BYTES - content_bytes
                        ),
                    )
                    content_bytes += hashed
                    content_bytes_by_class[classification] += hashed
                elif stat.S_ISLNK(metadata.st_mode):
                    record_type = b"symlink"
                    try:
                        target = os.readlink(name, dir_fd=directory_fd)
                        after = os.stat(
                            name,
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                    except OSError as exc:
                        raise AcceptanceHarnessError(
                            "production worktree symlink changed during inventory"
                        ) from exc
                    _require(
                        isinstance(target, bytes)
                        and metadata.st_nlink == 1
                        and _same_metadata(after, metadata),
                        "production worktree symlink changed during inventory",
                    )
                    content_digest = hashlib.sha256(target).hexdigest().encode(
                        "ascii"
                    )
                else:
                    raise AcceptanceHarnessError(
                        "production worktree contains a forbidden special leaf"
                    )
                records.append(
                    (
                        relative,
                        record_type,
                        classification,
                        *_metadata_fields(metadata),
                        content_digest,
                    )
                )
                continue
            records.append(
                (
                    relative,
                    record_type,
                    b"directory",
                    *_metadata_fields(metadata),
                    content_digest,
                )
            )
        _require(
            _same_metadata(os.fstat(directory_fd), before_directory),
            "production worktree directory changed during inventory",
        )

    try:
        root_metadata = os.fstat(root_fd)
        walk(root_fd, b"", 0)
        _require(
            observed_files == classified_files,
            "production Git path inventory differs from the filesystem",
        )
        try:
            path_metadata = os.stat(
                PRODUCTION_REPO_ROOT,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise AcceptanceHarnessError(
                "production repository root changed during inventory"
            ) from exc
        _require(
            _same_metadata(path_metadata, root_metadata),
            "production repository root changed during inventory",
        )
    finally:
        os.close(root_fd)
    return {
        "root_metadata": root_metadata,
        "entry_count": len(records),
        "content_bytes": content_bytes,
        "tracked_content_bytes": content_bytes_by_class[b"tracked"],
        "ignored_content_bytes": content_bytes_by_class[b"ignored"],
        "untracked_content_bytes": content_bytes_by_class[b"untracked"],
        "inventory_sha256": _framed_digest(records),
    }


def _nul_record_digest(
    payload: bytes,
    *,
    name: str,
) -> tuple[str, list[bytes]]:
    _require(
        len(payload) <= 4 * 1024 * 1024
        and (not payload or payload.endswith(b"\0")),
        f"production {name} canonical output is invalid",
    )
    records = payload[:-1].split(b"\0") if payload else []
    _require(
        all(record for record in records)
        and len(records) <= PRODUCTION_GIT_AUTHORITY_MAX_ENTRIES,
        f"production {name} canonical output is invalid",
    )
    return _framed_digest([(record,) for record in records]), sorted(records)


def _production_git_authority_inventory() -> dict[str, Any]:
    git_path = PRODUCTION_REPO_ROOT / ".git"
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        git_fd = os.open(git_path, flags)
    except OSError as exc:
        raise AcceptanceHarnessError(
            "production Git authority directory is unsafe"
        ) from exc
    records: list[tuple[bytes, ...]] = []
    content_bytes = 0

    def add_regular(
        directory_fd: int,
        name: bytes,
        relative: bytes,
        metadata: os.stat_result,
    ) -> None:
        nonlocal content_bytes
        digest, hashed = _hash_regular_file(
            directory_fd,
            name,
            metadata,
            remaining_bytes=(
                PRODUCTION_GIT_AUTHORITY_MAX_TOTAL_BYTES - content_bytes
            ),
            max_file_bytes=PRODUCTION_GIT_AUTHORITY_MAX_FILE_BYTES,
        )
        content_bytes += hashed
        records.append(
            (
                relative,
                b"regular",
                *_metadata_fields(metadata),
                digest,
            )
        )

    def walk_selected_directory(
        directory_fd: int,
        prefix: bytes,
        depth: int,
    ) -> None:
        _require(
            depth <= PRODUCTION_CAS_MAX_DEPTH,
            "production Git authority depth exceeds its bound",
        )
        before = os.fstat(directory_fd)
        _require(
            stat.S_ISDIR(before.st_mode),
            "production Git authority traversal escaped a directory",
        )
        try:
            with os.scandir(directory_fd) as iterator:
                names = sorted(os.fsencode(entry.name) for entry in iterator)
        except OSError as exc:
            raise AcceptanceHarnessError(
                "production Git authority directory could not be enumerated"
            ) from exc
        _require(
            len(records) + len(names)
            <= PRODUCTION_GIT_AUTHORITY_MAX_ENTRIES,
            "production Git authority inventory exceeds its entry bound",
        )
        for name in names:
            _require(
                name not in {b"", b".", b".."}
                and b"/" not in name
                and b"\0" not in name,
                "production Git authority contains an unsafe entry",
            )
            relative = prefix + b"/" + name
            _normalize_repo_path(relative, name="Git authority")
            try:
                metadata = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise AcceptanceHarnessError(
                    "production Git authority entry disappeared"
                ) from exc
            if stat.S_ISREG(metadata.st_mode):
                add_regular(directory_fd, name, relative, metadata)
            elif stat.S_ISDIR(metadata.st_mode):
                try:
                    child_fd = os.open(
                        name,
                        flags,
                        dir_fd=directory_fd,
                    )
                except OSError as exc:
                    raise AcceptanceHarnessError(
                        "production Git authority directory is unsafe"
                    ) from exc
                try:
                    _require(
                        _same_metadata(os.fstat(child_fd), metadata),
                        "production Git authority directory identity changed",
                    )
                    walk_selected_directory(
                        child_fd,
                        relative,
                        depth + 1,
                    )
                    _require(
                        _same_metadata(os.fstat(child_fd), metadata),
                        "production Git authority directory changed",
                    )
                finally:
                    os.close(child_fd)
                records.append(
                    (
                        relative,
                        b"directory",
                        *_git_authority_directory_metadata_fields(metadata),
                        b"-",
                    )
                )
            else:
                raise AcceptanceHarnessError(
                    "production Git authority contains a symlink or special leaf"
                )
        _require(
            _same_metadata(os.fstat(directory_fd), before),
            "production Git authority directory changed",
        )

    try:
        root_metadata = os.fstat(git_fd)
        for name, required in (
            (b"HEAD", True),
            (b"config", True),
            (b"index", True),
            (b"packed-refs", False),
        ):
            try:
                metadata = os.stat(
                    name,
                    dir_fd=git_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                _require(
                    not required,
                    "production Git authority is missing a required file",
                )
                records.append((name, b"absent"))
                continue
            except OSError as exc:
                raise AcceptanceHarnessError(
                    "production Git authority file is unavailable"
                ) from exc
            _require(
                stat.S_ISREG(metadata.st_mode),
                "production Git authority file is not regular",
            )
            add_regular(git_fd, name, name, metadata)
        # ``info/exclude``, ``info/attributes`` and sparse-checkout metadata
        # can change how the worktree is classified or interpreted.  Treat
        # the complete info tree as raw Git authority alongside refs/logs.
        for name in (b"refs", b"logs", b"info"):
            try:
                metadata = os.stat(
                    name,
                    dir_fd=git_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                records.append((name, b"absent"))
                continue
            except OSError as exc:
                raise AcceptanceHarnessError(
                    "production Git authority directory is unavailable"
                ) from exc
            _require(
                stat.S_ISDIR(metadata.st_mode),
                "production Git authority path is not a directory",
            )
            try:
                child_fd = os.open(name, flags, dir_fd=git_fd)
            except OSError as exc:
                raise AcceptanceHarnessError(
                    "production Git authority directory is unsafe"
                ) from exc
            try:
                _require(
                    _same_metadata(os.fstat(child_fd), metadata),
                    "production Git authority directory identity changed",
                )
                walk_selected_directory(child_fd, name, 1)
                _require(
                    _same_metadata(os.fstat(child_fd), metadata),
                    "production Git authority directory changed",
                )
            finally:
                os.close(child_fd)
            records.append(
                (
                    name,
                    b"directory",
                    *_git_authority_directory_metadata_fields(metadata),
                    b"-",
                )
            )
        _require(
            _same_metadata(os.fstat(git_fd), root_metadata),
            "production Git authority root changed",
        )
        try:
            path_metadata = os.stat(git_path, follow_symlinks=False)
        except OSError as exc:
            raise AcceptanceHarnessError(
                "production Git authority root changed"
            ) from exc
        _require(
            _same_metadata(path_metadata, root_metadata),
            "production Git authority root changed",
        )
    finally:
        os.close(git_fd)
    records.append(
        (
            b".",
            b"git-directory",
            *_git_authority_directory_metadata_fields(root_metadata),
            b"-",
        )
    )
    return {
        "entry_count": len(records),
        "content_bytes": content_bytes,
        "inventory_sha256": _framed_digest(records),
    }


def _production_repo_snapshot() -> dict[str, Any]:
    # Inspect the raw on-disk Git control plane before invoking Git at all.
    # This prevents a newly-added local include/fsmonitor/filter from being
    # interpreted while we are still trying to discover that it exists.
    initial_git_authority = _production_git_authority_inventory()
    _require(
        initial_git_authority == PRODUCTION_BASELINE_RAW_GIT_AUTHORITY,
        "production raw Git authority differs from the fixed baseline",
    )
    environment = _safe_command_environment(
        extra={
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1",
            "HOME": "/nonexistent/nexpoly-gpu-acceptance-home",
            "XDG_CONFIG_HOME": "/nonexistent/nexpoly-gpu-acceptance-xdg",
        }
    )

    def git_bytes(*arguments: str, timeout: float = 30.0) -> bytes:
        try:
            return subprocess.run(
                (
                    "git",
                    *PRODUCTION_GIT_CONFIG_OVERRIDES,
                    "-C",
                    str(PRODUCTION_REPO_ROOT),
                    *arguments,
                ),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                env=environment,
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise AcceptanceHarnessError(
                "production repository read-only Git CAS failed"
            ) from exc

    def collect_git_state() -> dict[str, bytes]:
        return {
            "head": git_bytes("rev-parse", "HEAD", timeout=10.0),
            "tree": git_bytes("rev-parse", "HEAD^{tree}", timeout=10.0),
            "git_dir": git_bytes(
                "rev-parse",
                "--absolute-git-dir",
                timeout=10.0,
            ),
            "head_ref": git_bytes(
                "symbolic-ref",
                "-q",
                "HEAD",
                timeout=10.0,
            ),
            "status": git_bytes(
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignored=matching",
                "--ignore-submodules=none",
            ),
            "tracked": git_bytes("ls-files", "--cached", "-z"),
            "ignored": git_bytes(
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
            ),
            "untracked": git_bytes(
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
            ),
            "config": git_bytes("config", "--local", "-z", "--list"),
            "origin": git_bytes(
                "config",
                "--local",
                "-z",
                "--get-all",
                "remote.origin.url",
            ),
            "refs": git_bytes(
                "for-each-ref",
                (
                    "--format=%(refname)%09%(objectname)%09"
                    "%(objecttype)%09%(symref)"
                ),
            ),
        }

    initial_git_state = collect_git_state()
    try:
        head = initial_git_state["head"].decode("ascii").strip()
        tree = initial_git_state["tree"].decode("ascii").strip()
        git_dir = initial_git_state["git_dir"].decode("utf-8").strip()
    except UnicodeError as exc:
        raise AcceptanceHarnessError(
            "production repository Git identity is invalid"
        ) from exc
    _require(
        head == PRODUCTION_BASELINE_SHA
        and tree == PRODUCTION_BASELINE_TREE
        and git_dir == str(PRODUCTION_REPO_ROOT / ".git")
        and initial_git_state["head_ref"] == b"refs/heads/main\n",
        "production repository differs from the fixed Git baseline",
    )
    status = initial_git_state["status"]
    boundaries = _status_boundaries(status)
    tracked = set(
        _git_nul_paths(
            initial_git_state["tracked"],
            "tracked",
        )
    )
    ignored = set(
        _git_nul_paths(
            initial_git_state["ignored"],
            "ignored",
        )
    )
    untracked = set(
        _git_nul_paths(
            initial_git_state["untracked"],
            "untracked",
        )
    )
    _require(
        len(tracked) <= PRODUCTION_CAS_MAX_PATHS
        and len(ignored) <= PRODUCTION_CAS_MAX_PATHS
        and len(untracked) <= PRODUCTION_CAS_MAX_PATHS,
        "production Git path inventory exceeds its bound",
    )
    _require(
        not untracked
        and all(code == b"!!" for code, _path in boundaries),
        "production repository has non-ignored untracked content",
    )
    for code, paths in ((b"!!", ignored), (b"??", untracked)):
        matching_boundaries = [
            boundary
            for boundary_code, boundary in boundaries
            if boundary_code == code
        ]
        _require(
            all(
                any(
                    path == boundary
                    or path.startswith(boundary + b"/")
                    for boundary in matching_boundaries
                )
                for path in paths
            ),
            "production Git status boundaries do not cover every path",
        )
    inventory = _production_worktree_inventory(
        tracked=tracked,
        ignored=ignored,
        untracked=untracked,
    )
    final_git_authority = _production_git_authority_inventory()
    _require(
        final_git_authority == initial_git_authority,
        "production raw Git authority changed during the read-only CAS",
    )
    config_sha256, _config_records = _nul_record_digest(
        initial_git_state["config"],
        name="local config",
    )
    origin_sha256, origin_records = _nul_record_digest(
        initial_git_state["origin"],
        name="origin URL",
    )
    _require(
        origin_records == [PRODUCTION_BASELINE_ORIGIN.encode("utf-8")],
        "production origin differs from the fixed HTTPS baseline",
    )
    refs_payload = initial_git_state["refs"]
    _require(
        not refs_payload or refs_payload.endswith(b"\n"),
        "production ref inventory is truncated",
    )
    ref_records = refs_payload.splitlines()
    for record in ref_records:
        fields = record.split(b"\t")
        _require(
            len(fields) == 4
            and fields[0].startswith(b"refs/")
            and len(fields[1]) == 40
            and all(
                byte in b"0123456789abcdef"
                for byte in fields[1]
            )
            and fields[2] in {b"commit", b"tag", b"tree", b"blob"}
            and (not fields[3] or fields[3].startswith(b"refs/")),
            "production ref inventory is invalid",
        )
    _require(
        len(ref_records) <= PRODUCTION_GIT_AUTHORITY_MAX_ENTRIES
        and len(set(ref_records)) == len(ref_records),
        "production ref inventory exceeds its bound or is duplicated",
    )
    refs_sha256 = _framed_digest([(record,) for record in ref_records])
    final_git_state = collect_git_state()
    _require(
        final_git_state == initial_git_state,
        "production Git control state changed during the read-only CAS",
    )
    metadata = inventory["root_metadata"]
    boundary_records = [
        (code, path)
        for code, path in boundaries
    ]
    snapshot = {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mtime_ns": metadata.st_mtime_ns,
        "head": head,
        "tree": tree,
        "status_sha256": _sha256_bytes(status),
        "status_boundary_count": len(boundaries),
        "tracked_path_count": len(tracked),
        "ignored_path_count": len(ignored),
        "untracked_path_count": len(untracked),
        "inventory_entry_count": inventory["entry_count"],
        "content_bytes": inventory["content_bytes"],
        "tracked_content_bytes": inventory["tracked_content_bytes"],
        "ignored_content_bytes": inventory["ignored_content_bytes"],
        "untracked_content_bytes": inventory["untracked_content_bytes"],
        "boundary_sha256": _framed_digest(boundary_records),
        "inventory_sha256": inventory["inventory_sha256"],
        "git_authority_entry_count": initial_git_authority["entry_count"],
        "git_authority_content_bytes": initial_git_authority["content_bytes"],
        "git_authority_sha256": initial_git_authority["inventory_sha256"],
        "git_config_sha256": config_sha256,
        "git_origin_url_count": len(origin_records),
        "git_origin_sha256": origin_sha256,
        "git_ref_count": len(ref_records),
        "git_refs_sha256": refs_sha256,
        "git_head_ref_sha256": _sha256_bytes(
            initial_git_state["head_ref"]
        ),
    }
    _require(
        snapshot == PRODUCTION_BASELINE_SNAPSHOT,
        "production repository snapshot differs from the complete fixed baseline",
    )
    return snapshot


class Gpu2AuditMonitor:
    """Continuously sample production GPU2 across the full live interval."""

    def __init__(
        self,
        baseline: dict[str, Any],
        *,
        interval_seconds: float = GPU2_AUDIT_INTERVAL_SECONDS,
        sampler: Any = snapshot_gpu2,
    ) -> None:
        self.baseline = baseline
        self.interval_seconds = interval_seconds
        self.sampler = sampler
        self.samples: list[dict[str, Any]] = [baseline]
        self.errors: list[str] = []
        self.drift_detected = False
        self.started_at = dt.datetime.now(dt.UTC)
        self.sampled_at: list[dt.datetime] = [self.started_at]
        self.finished_at: dt.datetime | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._sample_loop,
            name="nexpoly-gpu2-acceptance-audit",
            daemon=False,
        )

    def start(self) -> None:
        self._thread.start()

    def _sample_once(self) -> None:
        try:
            sample = self.sampler()
        except Exception as exc:  # noqa: BLE001 - monitoring boundary
            self.errors.append(f"{type(exc).__name__}: {exc}")
            self._stop.set()
            return
        self.samples.append(sample)
        self.sampled_at.append(dt.datetime.now(dt.UTC))
        if sample != self.baseline:
            self.drift_detected = True
            self._stop.set()

    def _sample_loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._sample_once()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join(timeout=5.0)
        _require(not self._thread.is_alive(), "GPU2 monitor thread did not stop")
        self._sample_once()
        self.finished_at = dt.datetime.now(dt.UTC)
        _require(not self.errors, "GPU2 continuous audit failed to sample")
        _require(
            not self.drift_detected
            and len(self.samples) >= 2
            and all(sample == self.baseline for sample in self.samples),
            "physical GPU2 changed during continuous acceptance audit",
        )
        return {
            "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
            "finished_at": self.finished_at.isoformat().replace("+00:00", "Z"),
            "interval_ms": int(self.interval_seconds * 1000),
            "sample_count": len(self.samples),
            "samples": self.samples,
            "sampled_at": [
                value.isoformat().replace("+00:00", "Z")
                for value in self.sampled_at
            ],
            "samples_sha256": acceptance_contract.canonical_json_digest(
                self.samples
            ),
            "drift_detected": False,
        }


def _worker_health(runtime_root: Path) -> dict[str, Any]:
    socket_path = runtime_root / "monomer-dft-worker-socket/worker.sock"
    result = _run(
        "curl",
        "--silent",
        "--show-error",
        "--fail",
        "--max-time",
        "5",
        "--unix-socket",
        str(socket_path),
        "http://monomer-dft-worker/health",
        timeout=10.0,
        env=_safe_command_environment(),
    )
    try:
        health = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AcceptanceHarnessError("fresh Worker health is invalid") from exc
    _require(
        isinstance(health, dict)
        and health.get("status") == "ok"
        and health.get("runtime_ready") is True
        and health.get("draining") is False
        and re.fullmatch(r"[0-9a-f]{32}", str(health.get("worker_instance_id")))
        is not None,
        "fresh Worker is not ready",
    )
    return health


def _read_worker_process(runtime_root: Path) -> dict[str, Any]:
    pid_file = runtime_root / "monomer-dft-worker.pid"
    _require(
        pid_file.is_file()
        and not pid_file.is_symlink()
        and stat.S_IMODE(pid_file.stat().st_mode) == 0o600,
        "fresh Worker PID evidence is unavailable",
    )
    fields = pid_file.read_text(encoding="ascii").split()
    _require(
        len(fields) == 2 and all(field.isdecimal() for field in fields),
        "fresh Worker PID evidence is malformed",
    )
    pid, expected_ticks = map(int, fields)
    identity = _process_identity(pid)
    _require(
        identity["process_start_ticks"] == expected_ticks
        and Path(identity["cwd"]).resolve()
        == (REPO_ROOT / "workers/monomer_dft_worker").resolve(),
        "fresh Worker process escaped exact F",
    )
    return identity


def _compose_evidence(
    *,
    project_name: str,
    authority_sha: str,
    run_kind: str,
    authority_images: dict[str, Any] | None,
) -> dict[str, Any]:
    environment = _local_docker_environment()
    ids = _run(
        "docker",
        "ps",
        "--filter",
        f"label=com.docker.compose.project={project_name}",
        "--format",
        "{{.ID}}",
        timeout=10.0,
        env=environment,
    ).stdout.split()
    _require(bool(ids), "fresh Compose project has no running containers")
    try:
        inspected = json.loads(
            _run(
                "docker",
                "inspect",
                *ids,
                timeout=30.0,
                env=environment,
            ).stdout
        )
    except json.JSONDecodeError as exc:
        raise AcceptanceHarnessError("fresh Compose inventory is invalid") from exc
    _require(isinstance(inspected, list), "fresh Compose inventory is not an array")
    by_service: dict[str, dict[str, Any]] = {}
    for item in inspected:
        _require(isinstance(item, dict), "fresh Compose container is invalid")
        config = item.get("Config")
        state = item.get("State")
        labels = config.get("Labels") if isinstance(config, dict) else None
        _require(
            isinstance(labels, dict)
            and labels.get("com.docker.compose.project") == project_name
            and isinstance(state, dict)
            and state.get("Running") is True,
            "fresh Compose label/state differs",
        )
        service = labels.get("com.docker.compose.service")
        if service not in {"backend", "frontend", "postgres"}:
            continue
        source_revision = labels.get("org.opencontainers.image.revision")
        if source_revision is None and isinstance(config, dict):
            source_revision = (config.get("Labels") or {}).get(
                "org.opencontainers.image.revision"
            )
        role = "web" if service == "frontend" else service
        _require(role not in by_service, "fresh Compose service is duplicated")
        by_service[role] = {
            "container_id": item.get("Id"),
            "image_id": item.get("Image"),
            "source_revision": source_revision if role != "postgres" else None,
            "started_at": state.get("StartedAt"),
        }
    _require(
        set(by_service) == {"backend", "web", "postgres"}
        and by_service["backend"]["source_revision"] == authority_sha
        and by_service["web"]["source_revision"] == authority_sha,
        "fresh Compose images do not bind exact F",
    )
    if run_kind == "final-main":
        _require(
            isinstance(authority_images, dict),
            "final-main image evidence is missing",
        )
        for role in ("backend", "web"):
            expected = authority_images[role]
            _validate_published_platform_manifest(
                expected=expected,
                environment=environment,
            )
            try:
                image_inventory = json.loads(
                    _run(
                        "docker",
                        "image",
                        "inspect",
                        expected["digest_ref"],
                        timeout=30.0,
                        env=environment,
                    ).stdout
                )
            except json.JSONDecodeError as exc:
                raise AcceptanceHarnessError(
                    f"local {role} OCI image inventory is invalid"
                ) from exc
            _require(
                isinstance(image_inventory, list)
                and len(image_inventory) == 1
                and isinstance(image_inventory[0], dict),
                f"local {role} OCI image inventory is ambiguous",
            )
            image = image_inventory[0]
            image_config = image.get("Config")
            image_labels = (
                image_config.get("Labels")
                if isinstance(image_config, dict)
                else None
            )
            descriptor = image.get("Descriptor")
            repo_digests = image.get("RepoDigests")
            _require(
                by_service[role]["image_id"] == expected["image_id"]
                and image.get("Id") == expected["image_id"]
                and isinstance(repo_digests, list)
                and expected["digest_ref"] in repo_digests
                and isinstance(descriptor, dict)
                and descriptor.get("digest") == expected["index_digest"]
                and isinstance(image_labels, dict)
                and image_labels.get("org.opencontainers.image.revision")
                == expected["revision"]
                and image_labels.get("org.opencontainers.image.source")
                == expected["source"]
                and image_labels.get("org.opencontainers.image.version")
                == expected["version"],
                f"running {role} container differs from exact published F OCI evidence",
            )
            by_service[role].update(
                {
                    "digest_ref": expected["digest_ref"],
                    "index_digest": expected["index_digest"],
                    "platform_digest": expected["platform_digest"],
                    "source": expected["source"],
                    "version": expected["version"],
                }
            )
    return {"project_name": project_name, **by_service}


class FreshAcceptanceControl:
    """Own one fresh Broker/MPS/Worker/Compose lifecycle, fail-closed."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        run_directory: Path,
        authority_sha: str,
        authority_tree: str,
        gpu3_mode: str,
        stack_timeout: float,
        run_kind: str,
        authority_images: dict[str, Any] | None,
    ) -> None:
        self.runtime_root = runtime_root
        self.run_directory = run_directory
        self.authority_sha = authority_sha
        self.authority_tree = authority_tree
        self.gpu3_mode = gpu3_mode
        self.stack_timeout = stack_timeout
        self.run_kind = run_kind
        self.authority_images = authority_images
        suffix = f"{authority_sha[:8]}_{uuid.uuid4().hex[:12]}"
        self.project_name = f"nexpoly_dft_fresh_{suffix}"[:58]
        self.candidate_image_tags = (
            _candidate_image_tags(
                project_name=self.project_name,
                authority_sha=authority_sha,
            )
            if run_kind == "candidate-tree"
            else {}
        )
        self.ordinary_dev_images_before: dict[str, str | None] | None = None
        self.candidate_images_absent_before = False
        self.gpu_root = runtime_root / "gpu-resource"
        self.gpu_root_identity: dict[str, int] | None = None
        self.gpu_root_descriptor = -1
        self.reservations_descriptor = -1
        self.authority_process_id = os.getpid()
        self.mps_descriptors: dict[int, dict[str, int]] = {}
        self.broker_socket = self.gpu_root / "broker.sock"
        self.broker_state = run_directory / "broker-state.json"
        self.broker_process: subprocess.Popen[bytes] | None = None
        self.broker_log_handle: Any = None
        self.broker_process_evidence: dict[str, Any] | None = None
        self.broker_instance_id: str | None = None
        self.initial_leases: list[Any] | None = None
        self.final_leases: list[Any] | None = None
        self.stack_attempted = False
        self.stack_started = False
        self.worker_evidence: dict[str, Any] | None = None
        self.container_evidence: dict[str, Any] | None = None
        self.mps_started: list[int] = []
        self.mps_attempted: list[int] = []
        self.cleanup_evidence: dict[str, Any] | None = None

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self._close_runtime_descriptors()

    def _close_runtime_descriptors(self) -> None:
        for descriptors in self.mps_descriptors.values():
            for descriptor in descriptors.values():
                if descriptor >= 0:
                    with contextlib.suppress(OSError):
                        os.close(descriptor)
        self.mps_descriptors.clear()
        if self.reservations_descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(self.reservations_descriptor)
            self.reservations_descriptor = -1
        if self.gpu_root_descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(self.gpu_root_descriptor)
            self.gpu_root_descriptor = -1

    def _require_private_gpu_root(self, *, create: bool) -> None:
        """Create/open the development GPU root without following symlinks."""

        _require(
            self.gpu_root.parent == self.runtime_root
            and self.gpu_root.name == "gpu-resource",
            "acceptance GPU runtime root escaped the private runtime root",
        )
        runtime_descriptor = _open_absolute_directory_chain(
            self.runtime_root,
            "acceptance private runtime root",
        )
        gpu_descriptor = -1
        try:
            runtime_metadata = os.fstat(runtime_descriptor)
            _require(
                stat.S_ISDIR(runtime_metadata.st_mode)
                and runtime_metadata.st_uid == os.geteuid()
                and stat.S_IMODE(runtime_metadata.st_mode) == 0o700,
                "acceptance private runtime root is not owner-private",
            )
            if create:
                try:
                    os.mkdir(
                        os.fsencode(self.gpu_root.name),
                        mode=0o700,
                        dir_fd=runtime_descriptor,
                    )
                except FileExistsError:
                    pass
            try:
                gpu_descriptor = os.open(
                    os.fsencode(self.gpu_root.name),
                    (
                        os.O_RDONLY
                        | os.O_CLOEXEC
                        | os.O_DIRECTORY
                        | os.O_NOFOLLOW
                    ),
                    dir_fd=runtime_descriptor,
                )
            except OSError as exc:
                raise AcceptanceHarnessError(
                    "acceptance GPU runtime root is unavailable or unsafe"
                ) from exc
            gpu_metadata = os.fstat(gpu_descriptor)
            identity = _directory_identity_snapshot(gpu_metadata)
            _require(
                stat.S_ISDIR(gpu_metadata.st_mode)
                and gpu_metadata.st_uid == os.geteuid()
                and stat.S_IMODE(gpu_metadata.st_mode) == 0o700,
                "acceptance GPU runtime root is not owner-private",
            )
            if self.gpu_root_identity is None:
                self.gpu_root_identity = identity
                self.gpu_root_descriptor = gpu_descriptor
                gpu_descriptor = -1
            else:
                _require(
                    self.gpu_root_descriptor >= 0
                    and identity == self.gpu_root_identity
                    and _directory_identity_snapshot(
                        os.fstat(self.gpu_root_descriptor)
                    )
                    == self.gpu_root_identity,
                    "acceptance GPU runtime root identity changed",
                )
        finally:
            if gpu_descriptor >= 0:
                os.close(gpu_descriptor)
            os.close(runtime_descriptor)

    def _gpu_authority_path(self, *parts: str) -> Path:
        _require(
            self.gpu_root_descriptor >= 0
            and self.gpu_root_identity is not None
            and _directory_identity_snapshot(
                os.fstat(self.gpu_root_descriptor)
            )
            == self.gpu_root_identity,
            "acceptance GPU runtime descriptor authority changed",
        )
        _require(
            os.getpid() == self.authority_process_id,
            "acceptance GPU authority escaped its harness process",
        )
        path = Path(
            f"/proc/{self.authority_process_id}/fd/"
            f"{self.gpu_root_descriptor}"
        )
        return path.joinpath(*parts)

    def _gpu_child_absent(self, name: str, message: str) -> None:
        _require(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)
            is not None,
            "acceptance GPU child name is unsafe",
        )
        try:
            os.stat(
                os.fsencode(name),
                dir_fd=self.gpu_root_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        except OSError as exc:
            raise AcceptanceHarnessError(message) from exc
        raise AcceptanceHarnessError(message)

    @staticmethod
    def _private_directory_descriptor(
        parent_descriptor: int,
        name: str,
        *,
        create: bool,
    ) -> int:
        if create:
            try:
                os.mkdir(
                    os.fsencode(name),
                    mode=0o700,
                    dir_fd=parent_descriptor,
                )
                os.fsync(parent_descriptor)
            except FileExistsError:
                pass
        try:
            descriptor = os.open(
                os.fsencode(name),
                (
                    os.O_RDONLY
                    | os.O_CLOEXEC
                    | os.O_DIRECTORY
                    | os.O_NOFOLLOW
                ),
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise AcceptanceHarnessError(
                "acceptance MPS directory authority is unsafe"
            ) from exc
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            os.close(descriptor)
            raise AcceptanceHarnessError(
                "acceptance MPS directory is not owner-private"
            )
        return descriptor

    def _prepare_private_mps_tree(self, index: int) -> None:
        _require(index in {1, 3}, "acceptance MPS index is unsafe")
        _require(
            index not in self.mps_descriptors,
            f"acceptance GPU{index} MPS directory already has authority",
        )
        self._require_private_gpu_root(create=False)
        self._gpu_child_absent(
            f"mps-{index}",
            f"acceptance refuses preexisting GPU{index} MPS state",
        )
        slot_descriptor = self._private_directory_descriptor(
            self.gpu_root_descriptor,
            f"mps-{index}",
            create=True,
        )
        pipe_descriptor = -1
        log_descriptor = -1
        try:
            pipe_descriptor = self._private_directory_descriptor(
                slot_descriptor,
                "pipe",
                create=True,
            )
            log_descriptor = self._private_directory_descriptor(
                slot_descriptor,
                "log",
                create=True,
            )
            self.mps_descriptors[index] = {
                "slot": slot_descriptor,
                "pipe": pipe_descriptor,
                "log": log_descriptor,
            }
            slot_descriptor = -1
            pipe_descriptor = -1
            log_descriptor = -1
        finally:
            for descriptor in (
                log_descriptor,
                pipe_descriptor,
                slot_descriptor,
            ):
                if descriptor >= 0:
                    os.close(descriptor)

    def _mps_authority_path(self, index: int, kind: str) -> Path:
        descriptors = self.mps_descriptors.get(index)
        _require(
            isinstance(descriptors, dict)
            and kind in {"slot", "pipe", "log"}
            and descriptors.get(kind, -1) >= 0,
            "acceptance MPS descriptor authority is unavailable",
        )
        descriptor = descriptors[kind]
        metadata = os.fstat(descriptor)
        _require(
            stat.S_ISDIR(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and stat.S_IMODE(metadata.st_mode) == 0o700,
            "acceptance MPS descriptor authority changed",
        )
        return Path(
            f"/proc/{self.authority_process_id}/fd/{descriptor}"
        )

    def _mps_pass_fds(self, index: int) -> tuple[int, ...]:
        descriptors = self.mps_descriptors[index]
        return (
            self.gpu_root_descriptor,
            self.reservations_descriptor,
            descriptors["slot"],
            descriptors["pipe"],
            descriptors["log"],
        )

    def _remove_private_mps_tree(self, index: int) -> None:
        """Remove only the exact fresh MPS tree held by this controller."""

        descriptors = self.mps_descriptors.get(index)
        _require(
            isinstance(descriptors, dict),
            f"GPU{index} MPS cleanup lacks descriptor authority",
        )
        pipe_descriptor = descriptors["pipe"]
        log_descriptor = descriptors["log"]
        slot_descriptor = descriptors["slot"]
        _require(
            os.listdir(pipe_descriptor) == [],
            f"GPU{index} MPS pipe directory is not empty after stop",
        )
        log_entries = sorted(os.listdir(log_descriptor))
        _require(
            len(log_entries) <= 32,
            f"GPU{index} MPS log inventory exceeds its cleanup bound",
        )
        for name in log_entries:
            _require(
                isinstance(name, str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)
                is not None,
                f"GPU{index} MPS log entry name is unsafe",
            )
            metadata = os.stat(
                os.fsencode(name),
                dir_fd=log_descriptor,
                follow_symlinks=False,
            )
            _require(
                stat.S_ISREG(metadata.st_mode)
                and metadata.st_uid == os.geteuid()
                and metadata.st_nlink == 1
                and metadata.st_size <= 16 * 1024 * 1024,
                f"GPU{index} MPS log entry is unsafe",
            )
            os.unlink(os.fsencode(name), dir_fd=log_descriptor)
        os.fsync(log_descriptor)
        _require(
            os.listdir(log_descriptor) == []
            and sorted(os.listdir(slot_descriptor)) == ["log", "pipe"],
            f"GPU{index} MPS directory inventory differs during cleanup",
        )
        os.rmdir(b"pipe", dir_fd=slot_descriptor)
        os.rmdir(b"log", dir_fd=slot_descriptor)
        os.fsync(slot_descriptor)
        _require(
            os.listdir(slot_descriptor) == [],
            f"GPU{index} MPS slot is not empty during cleanup",
        )
        os.rmdir(
            os.fsencode(f"mps-{index}"),
            dir_fd=self.gpu_root_descriptor,
        )
        os.fsync(self.gpu_root_descriptor)
        self._gpu_child_absent(
            f"mps-{index}",
            f"GPU{index} MPS directory survived cleanup",
        )
        for descriptor in descriptors.values():
            os.close(descriptor)
        del self.mps_descriptors[index]

    def _collect_private_mps_control_artifacts(self, index: int) -> None:
        """Collect only bounded NVIDIA IPC residue after an idle `quit`."""

        descriptors = self.mps_descriptors.get(index)
        _require(
            isinstance(descriptors, dict),
            f"GPU{index} MPS cleanup lacks descriptor authority",
        )
        pipe_descriptor = descriptors["pipe"]
        entries = sorted(os.listdir(pipe_descriptor))
        allowed = {"control", "control_lock", "control_privileged"}
        _require(
            set(entries) <= allowed,
            f"GPU{index} MPS pipe retained unexpected entries after stop",
        )
        for name in entries:
            metadata = os.stat(
                os.fsencode(name),
                dir_fd=pipe_descriptor,
                follow_symlinks=False,
            )
            mode = stat.S_IMODE(metadata.st_mode)
            common_safe = (
                metadata.st_uid == os.geteuid()
                and metadata.st_gid == os.getegid()
                and metadata.st_nlink == 1
                and metadata.st_size <= 4096
            )
            if name == "control":
                safe = (
                    common_safe
                    and (
                        stat.S_ISFIFO(metadata.st_mode)
                        or stat.S_ISSOCK(metadata.st_mode)
                    )
                    and mode in {0o600, 0o640, 0o644, 0o660, 0o666}
                )
            elif name == "control_privileged":
                safe = (
                    common_safe
                    and stat.S_ISSOCK(metadata.st_mode)
                    and mode == 0o700
                )
            else:
                safe = (
                    common_safe
                    and stat.S_ISREG(metadata.st_mode)
                    and mode in {0o600, 0o640, 0o644, 0o660, 0o666}
                )
            _require(
                safe,
                f"GPU{index} MPS pipe residue is unsafe: {name}",
            )
            os.unlink(os.fsencode(name), dir_fd=pipe_descriptor)
        os.fsync(pipe_descriptor)
        _require(
            os.listdir(pipe_descriptor) == [],
            f"GPU{index} MPS pipe residue survived exact cleanup",
        )

    def _collect_private_broker_socket(self) -> None:
        """Remove the exact fresh socket only after its Broker has exited."""

        _require(
            self.broker_process is not None
            and self.broker_process.poll() is not None,
            "fresh Broker socket cannot be collected while its process is live",
        )
        self._require_private_gpu_root(create=False)
        try:
            metadata = os.stat(
                b"broker.sock",
                dir_fd=self.gpu_root_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        _require(
            stat.S_ISSOCK(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and metadata.st_gid == os.getegid()
            and metadata.st_nlink == 1
            and stat.S_IMODE(metadata.st_mode) == 0o600,
            "fresh Broker socket residue is unsafe",
        )
        os.unlink(b"broker.sock", dir_fd=self.gpu_root_descriptor)
        os.fsync(self.gpu_root_descriptor)
        self._gpu_child_absent(
            "broker.sock",
            "fresh Broker socket survived exact cleanup",
        )

    def _mps_environment(self, index: int) -> dict[str, str]:
        self._require_private_gpu_root(create=False)
        _require(
            self.reservations_descriptor >= 0,
            "acceptance reservation descriptor authority is unavailable",
        )
        return _safe_command_environment(
            extra={
                "NEXPOLY_GPU_STATE_ROOT": str(self._gpu_authority_path()),
                "NEXPOLY_GPU_EXTERNAL_RESERVATIONS": str(
                    self._reservations_authority_path()
                ),
                "NEXPOLY_GPU_BROKER_SOCKET": str(
                    self._gpu_authority_path("broker.sock")
                ),
                "NEXPOLY_GPU_MPS_SLOT_DIRECTORY": str(
                    self._mps_authority_path(index, "slot")
                ),
                "NEXPOLY_GPU_MPS_PIPE_DIRECTORY": str(
                    self._mps_authority_path(index, "pipe")
                ),
                "NEXPOLY_GPU_MPS_LOG_DIRECTORY": str(
                    self._mps_authority_path(index, "log")
                ),
                "NEXPOLY_GPU_MPS_DESCRIPTOR_AUTHORITY": "1",
                "NEXPOLY_GPU_MPS_AUTHORITY_PID": str(
                    self.authority_process_id
                ),
                "NEXPOLY_GPU_MPS_AUTHORITY_START_TICKS": str(
                    _read_proc_start_ticks(self.authority_process_id)
                ),
                "NEXPOLY_GPU_MPS_EXPECTED_ROOT": str(self.gpu_root),
                "NEXPOLY_GPU_MPS_REQUIRE_DEFAULT_MODE": "1",
            }
        )

    def _reservations_authority_path(self) -> Path:
        _require(
            self.reservations_descriptor >= 0,
            "acceptance reservation descriptor authority is unavailable",
        )
        metadata = os.fstat(self.reservations_descriptor)
        _require(
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and metadata.st_nlink == 1
            and stat.S_IMODE(metadata.st_mode) == 0o600,
            "acceptance reservation descriptor authority changed",
        )
        return Path(
            f"/proc/{self.authority_process_id}/fd/"
            f"{self.reservations_descriptor}"
        )

    @staticmethod
    def _descriptor_identity(descriptor: int) -> str:
        metadata = os.fstat(descriptor)
        return f"{metadata.st_dev}:{metadata.st_ino}"

    def _formal_gpu_authority_environment(self) -> dict[str, str]:
        self._require_private_gpu_root(create=False)
        _require(
            self.reservations_descriptor >= 0
            and 1 in self.mps_descriptors,
            "formal GPU descriptor authority is incomplete",
        )
        source_digest = _sha256_file(
            REPO_ROOT / "ops/config/gpu-external-reservations.json"
        ).removeprefix("sha256:")
        _require(
            _sha256_bytes(
                os.pread(
                    self.reservations_descriptor,
                    1024 * 1024 + 1,
                    0,
                )
            ).removeprefix("sha256:")
            == source_digest,
            "formal GPU reservation descriptor differs from exact F",
        )
        environment = {
            "NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY": "1",
            "NEXPOLY_DFT_GPU_AUTHORITY_PID": str(
                self.authority_process_id
            ),
            "NEXPOLY_DFT_GPU_AUTHORITY_START_TICKS": str(
                _read_proc_start_ticks(self.authority_process_id)
            ),
            "NEXPOLY_DFT_GPU_AUTHORITY_ROOT": str(
                self._gpu_authority_path()
            ),
            "NEXPOLY_DFT_GPU_AUTHORITY_ROOT_IDENTITY": (
                self._descriptor_identity(self.gpu_root_descriptor)
            ),
            "NEXPOLY_DFT_GPU_RESERVATIONS_AUTHORITY": str(
                self._reservations_authority_path()
            ),
            "NEXPOLY_DFT_GPU_RESERVATIONS_IDENTITY": (
                self._descriptor_identity(self.reservations_descriptor)
            ),
            "NEXPOLY_DFT_GPU_RESERVATIONS_SHA256": source_digest,
            "NEXPOLY_DFT_GPU1_MPS_PIPE_AUTHORITY": str(
                self._mps_authority_path(1, "pipe")
            ),
            "NEXPOLY_DFT_GPU1_MPS_PIPE_IDENTITY": (
                self._descriptor_identity(
                    self.mps_descriptors[1]["pipe"]
                )
            ),
        }
        if 3 in self.mps_descriptors:
            environment.update(
                {
                    "NEXPOLY_DFT_GPU3_MPS_PIPE_AUTHORITY": str(
                        self._mps_authority_path(3, "pipe")
                    ),
                    "NEXPOLY_DFT_GPU3_MPS_PIPE_IDENTITY": (
                        self._descriptor_identity(
                            self.mps_descriptors[3]["pipe"]
                        )
                    ),
                }
            )
        return environment

    def _require_absent(self) -> None:
        # This must precede every Docker/Broker/MPS observation.  The runtime
        # tree is ignored by Git, so repository cleanliness cannot establish
        # that a stale child is a real owner-private directory.
        self._require_private_gpu_root(create=True)
        self._gpu_child_absent(
            "broker.sock",
            "acceptance refuses preexisting Broker state",
        )
        for index in (1, 3):
            self._gpu_child_absent(
                f"mps-{index}",
                f"acceptance refuses preexisting GPU{index} MPS state",
            )
        _require(not _stack_running(), "acceptance refuses a preexisting Worker")
        docker_environment = _local_docker_environment()
        self.ordinary_dev_images_before = _docker_image_tag_snapshot(
            list(ORDINARY_DEV_IMAGE_TAGS)
        )
        candidate_snapshot = _docker_image_tag_snapshot(
            list(self.candidate_image_tags.values())
        )
        _require(
            all(value is None for value in candidate_snapshot.values()),
            "acceptance candidate image tag already exists",
        )
        self.candidate_images_absent_before = True
        containers = _run(
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={self.project_name}",
            timeout=10.0,
            env=docker_environment,
        ).stdout.split()
        volume = _run(
            "docker",
            "volume",
            "inspect",
            f"{self.project_name}_monomer_dft_postgres_data",
            timeout=10.0,
            check=False,
            env=docker_environment,
        )
        network = _run(
            "docker",
            "network",
            "inspect",
            f"{self.project_name}_default",
            timeout=10.0,
            check=False,
            env=docker_environment,
        )
        _require(
            not containers
            and volume.returncode != 0
            and network.returncode != 0,
            "acceptance Compose project, volume, or network is not fresh",
        )
        _require(
            not (self.runtime_root / "monomer-dft-worker.pid").exists(),
            "acceptance refuses preexisting Broker/Worker state",
        )

    def _start_broker(self) -> None:
        self._require_private_gpu_root(create=False)
        _require(
            self.reservations_descriptor < 0,
            "runtime external reservation authority already exists",
        )
        reservations_source = REPO_ROOT / "ops/config/gpu-external-reservations.json"
        source_bytes = reservations_source.read_bytes()
        reservations_descriptor = -1
        try:
            reservations_descriptor = os.open(
                b"external-reservations.json",
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=self.gpu_root_descriptor,
            )
        except FileNotFoundError:
            reservations_descriptor = os.open(
                b"external-reservations.json",
                (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW
                ),
                0o600,
                dir_fd=self.gpu_root_descriptor,
            )
            try:
                os.write(reservations_descriptor, source_bytes)
                os.fsync(reservations_descriptor)
                os.fsync(self.gpu_root_descriptor)
            finally:
                os.close(reservations_descriptor)
                reservations_descriptor = -1
            reservations_descriptor = os.open(
                b"external-reservations.json",
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=self.gpu_root_descriptor,
            )
        except OSError as exc:
            raise AcceptanceHarnessError(
                "runtime external reservation policy is unsafe"
            ) from exc
        if reservations_descriptor >= 0:
            try:
                metadata = os.fstat(reservations_descriptor)
                _require(
                    stat.S_ISREG(metadata.st_mode)
                    and metadata.st_uid == os.geteuid()
                    and metadata.st_nlink == 1
                    and stat.S_IMODE(metadata.st_mode) == 0o600
                    and os.pread(
                        reservations_descriptor,
                        len(source_bytes) + 1,
                        0,
                    )
                    == source_bytes,
                    "runtime external reservation policy differs from exact F",
                )
            except BaseException:
                os.close(reservations_descriptor)
                raise
            self.reservations_descriptor = reservations_descriptor
        authority_broker_socket = self._gpu_authority_path("broker.sock")
        broker_process_root = Path(
            f"/proc/self/fd/{self.gpu_root_descriptor}"
        )
        broker_process_reservations = Path(
            f"/proc/self/fd/{self.reservations_descriptor}"
        )
        command = (
            sys.executable,
            "-E",
            "-s",
            "-B",
            "-m",
            "ops.gpu_broker.server",
            "--socket",
            str(broker_process_root / "broker.sock"),
            "--state",
            str(self.broker_state),
            "--policy",
            str(REPO_ROOT / "ops/config/gpu-broker-policy.json"),
            "--external-reservations",
            str(broker_process_reservations),
            "--mps-state-root",
            str(broker_process_root),
        )
        broker_log_path = self.run_directory / "fresh-broker.log"
        broker_log_descriptor = os.open(
            broker_log_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        self.broker_log_handle = os.fdopen(
            broker_log_descriptor, "ab", buffering=0
        )
        self.broker_process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=_safe_command_environment(),
            stdin=subprocess.DEVNULL,
            stdout=self.broker_log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            pass_fds=(
                self.gpu_root_descriptor,
                self.reservations_descriptor,
            ),
        )
        deadline = time.monotonic() + 15.0
        last_error: Exception | None = None
        from gpu_resource import GpuBrokerClient

        while time.monotonic() < deadline:
            if self.broker_process.poll() is not None:
                break
            try:
                status = GpuBrokerClient(authority_broker_socket).status()
                self.broker_instance_id = status.get("broker_instance_id")
                self.initial_leases = status.get("leases")
                _require(
                    isinstance(self.broker_instance_id, str)
                    and self.initial_leases == []
                    and status.get("draining") is False,
                    "fresh Broker did not start empty",
                )
                self.broker_process_evidence = _process_identity(
                    self.broker_process.pid
                )
                _require(
                    Path(self.broker_process_evidence["cwd"]).resolve()
                    == REPO_ROOT.resolve(),
                    "fresh Broker escaped exact F",
                )
                return
            except Exception as exc:  # noqa: BLE001 - bounded startup retry
                last_error = exc
                time.sleep(0.1)
        stderr = b""
        if self.broker_process.poll() is not None:
            try:
                stderr = broker_log_path.read_bytes()[-2000:]
            except OSError:
                pass
        raise AcceptanceHarnessError(
            "fresh Broker failed to start: "
            + stderr.decode("utf-8", errors="replace")
        ) from last_error

    def _start_mps(self, index: int) -> None:
        _require(index in {1, 3} and index != 2, "GPU2 MPS is forbidden")
        self._require_private_gpu_root(create=False)
        self._prepare_private_mps_tree(index)
        self.mps_attempted.append(index)
        result = _run(
            str(SCRIPT_ROOT / "gpu_mps_control.sh"),
            "start",
            str(index),
            timeout=30.0,
            check=False,
            env=self._mps_environment(index),
            pass_fds=self._mps_pass_fds(index),
        )
        _require(
            result.returncode == 0,
            f"fresh GPU{index} MPS failed: {result.stderr[-1000:]}",
        )
        try:
            control_metadata = os.stat(
                b"control",
                dir_fd=self.mps_descriptors[index]["pipe"],
                follow_symlinks=False,
            )
        except OSError as exc:
            raise AcceptanceHarnessError(
                f"fresh GPU{index} MPS control channel is unavailable"
            ) from exc
        _require(
            (
                stat.S_ISFIFO(control_metadata.st_mode)
                or stat.S_ISSOCK(control_metadata.st_mode)
            )
            and control_metadata.st_uid == os.geteuid(),
            f"fresh GPU{index} MPS control channel is unsafe",
        )
        self.mps_started.append(index)

    def start(self) -> dict[str, str]:
        self._require_absent()
        self._start_broker()
        self._start_mps(1)
        if self.gpu3_mode == "actual":
            self._start_mps(3)
        self.stack_attempted = True
        _stack_command(
            "start",
            self.stack_timeout,
            project_name=self.project_name,
            authority_sha=self.authority_sha,
            run_kind=self.run_kind,
            authority_images=self.authority_images,
            gpu_authority_environment=(
                self._formal_gpu_authority_environment()
            ),
        )
        self.stack_started = True
        health = _worker_health(self.runtime_root)
        process = _read_worker_process(self.runtime_root)
        self.worker_evidence = {
            "instance_id": health["worker_instance_id"],
            "process": process,
            "authority_sha": self.authority_sha,
            "fresh": True,
        }
        self.container_evidence = _compose_evidence(
            project_name=self.project_name,
            authority_sha=self.authority_sha,
            run_kind=self.run_kind,
            authority_images=self.authority_images,
        )
        resolved = {
            "MONOMER_DFT_PYTHON": sys.executable,
            "MONOMER_DFT_GPU_BROKER_UDS": str(
                self._gpu_authority_path("broker.sock")
            ),
            "MONOMER_DFT_GPU_MPS_PIPE_ROOT": str(
                self._gpu_authority_path()
            ),
            "NEXPOLY_DFT_GPU1_MPS_PIPE_AUTHORITY": str(
                self._mps_authority_path(1, "pipe")
            ),
        }
        if 3 in self.mps_descriptors:
            resolved["NEXPOLY_DFT_GPU3_MPS_PIPE_AUTHORITY"] = str(
                self._mps_authority_path(3, "pipe")
            )
        return resolved

    def ensure_gpu3_mps(self) -> None:
        if 3 not in self.mps_started:
            self._start_mps(3)

    def cleanup(self) -> tuple[dict[str, Any] | None, list[str]]:
        errors: list[str] = []
        worker_stopped = False
        containers_removed = False
        volume_removed = False
        network_removed = False
        broker_drained = False
        broker_stopped = False
        leases_empty = False
        candidate_image_tags = sorted(self.candidate_image_tags.values())
        candidate_images_removed = not candidate_image_tags
        ordinary_dev_images_after: dict[str, str | None] | None = None
        ordinary_dev_images_unchanged = False
        stopped: list[int] = []
        if self.stack_attempted:
            try:
                _stack_command(
                    "stop",
                    self.stack_timeout,
                    project_name=self.project_name,
                    authority_sha=self.authority_sha,
                    run_kind=self.run_kind,
                    authority_images=self.authority_images,
                    gpu_authority_environment=(
                        self._formal_gpu_authority_environment()
                    ),
                )
                worker_stopped = not _stack_running()
                environment = _local_docker_environment()
                remaining = _run(
                    "docker",
                    "ps",
                    "-aq",
                    "--filter",
                    f"label=com.docker.compose.project={self.project_name}",
                    timeout=10.0,
                    env=environment,
                ).stdout.split()
                containers_removed = not remaining
                if containers_removed:
                    volume_name = (
                        f"{self.project_name}_monomer_dft_postgres_data"
                    )
                    volume_inventory = _run(
                        "docker",
                        "volume",
                        "inspect",
                        volume_name,
                        timeout=10.0,
                        check=False,
                        env=environment,
                    )
                    if volume_inventory.returncode == 0:
                        removal = _run(
                            "docker",
                            "volume",
                            "rm",
                            volume_name,
                            timeout=30.0,
                            check=False,
                            env=environment,
                        )
                        _require(
                            removal.returncode == 0,
                            "fresh Compose PostgreSQL volume removal failed",
                        )
                    volume_removed = (
                        _run(
                            "docker",
                            "volume",
                            "inspect",
                            volume_name,
                            timeout=10.0,
                            check=False,
                            env=environment,
                        ).returncode
                        != 0
                    )
                    network_removed = (
                        _run(
                            "docker",
                            "network",
                            "inspect",
                            f"{self.project_name}_default",
                            timeout=10.0,
                            check=False,
                            env=environment,
                        ).returncode
                        != 0
                    )
                    _require(
                        volume_removed and network_removed,
                        "fresh Compose volume or network survived cleanup",
                    )
            except Exception as exc:  # noqa: BLE001 - collect all cleanup failures
                errors.append(f"stack cleanup: {type(exc).__name__}: {exc}")
        try:
            environment = _local_docker_environment()
            candidate_snapshot = _docker_image_tag_snapshot(
                candidate_image_tags
            )
            for tag, image_id in candidate_snapshot.items():
                if image_id is None:
                    continue
                removal = _run(
                    "docker",
                    "image",
                    "rm",
                    tag,
                    timeout=30.0,
                    check=False,
                    env=environment,
                )
                _require(
                    removal.returncode == 0,
                    "candidate image tag removal failed",
                )
            candidate_images_removed = all(
                value is None
                for value in _docker_image_tag_snapshot(
                    candidate_image_tags
                ).values()
            )
            _require(
                candidate_images_removed,
                "candidate image tag survived cleanup",
            )
            ordinary_dev_images_after = _docker_image_tag_snapshot(
                list(ORDINARY_DEV_IMAGE_TAGS)
            )
            ordinary_dev_images_unchanged = (
                self.ordinary_dev_images_before is not None
                and ordinary_dev_images_after
                == self.ordinary_dev_images_before
            )
            _require(
                ordinary_dev_images_unchanged,
                "ordinary development image tags changed during acceptance",
            )
        except Exception as exc:  # noqa: BLE001 - retain cleanup evidence
            errors.append(f"image cleanup: {type(exc).__name__}: {exc}")
        if self.broker_process is not None and self.broker_process.poll() is None:
            try:
                from gpu_resource import GpuBrokerClient

                if self.gpu_root_identity is not None:
                    self._require_private_gpu_root(create=False)
                client = GpuBrokerClient(
                    self._gpu_authority_path("broker.sock")
                )
                status = client.set_draining(True)
                broker_drained = status.get("draining") is True
                self.final_leases = status.get("leases")
                leases_empty = self.final_leases == []
                _require(leases_empty, "fresh Broker still has leases during cleanup")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Broker drain: {type(exc).__name__}: {exc}")
        mps_cleanup_safe = broker_drained and leases_empty
        mps_cleanup_complete = mps_cleanup_safe
        if mps_cleanup_safe:
            for index in reversed(self.mps_attempted):
                try:
                    if self.gpu_root_identity is not None:
                        self._require_private_gpu_root(create=False)
                    descriptors = self.mps_descriptors.get(index)
                    _require(
                        isinstance(descriptors, dict),
                        f"GPU{index} MPS descriptor authority is unavailable",
                    )
                    control_path = (
                        self._mps_authority_path(index, "pipe") / "control"
                    )
                    if not control_path.exists() and not control_path.is_symlink():
                        # A failed start that never created a control channel
                        # has no daemon to collect.
                        self._remove_private_mps_tree(index)
                        continue
                    result = _run(
                        str(SCRIPT_ROOT / "gpu_mps_control.sh"),
                        "stop",
                        str(index),
                        timeout=30.0,
                        check=False,
                        env=self._mps_environment(index),
                        pass_fds=self._mps_pass_fds(index),
                    )
                    _require(
                        result.returncode == 0,
                        f"GPU{index} MPS stop failed: {result.stderr[-1000:]}",
                    )
                    self._collect_private_mps_control_artifacts(index)
                    self._remove_private_mps_tree(index)
                    stopped.append(index)
                except Exception as exc:  # noqa: BLE001
                    mps_cleanup_complete = False
                    errors.append(f"GPU{index} MPS cleanup: {type(exc).__name__}: {exc}")
        elif self.mps_attempted:
            errors.append("MPS cleanup refused because Broker drain/lease proof failed")
        if self.broker_process is not None:
            try:
                if (
                    self.broker_process.poll() is None
                    and mps_cleanup_complete
                ):
                    self.broker_process.terminate()
                    try:
                        self.broker_process.wait(timeout=10.0)
                    except subprocess.TimeoutExpired:
                        self.broker_process.kill()
                        self.broker_process.wait(timeout=5.0)
                if self.broker_process.poll() is None:
                    errors.append(
                        "fresh Broker intentionally preserved because lease/MPS "
                        "cleanup was not proven"
                    )
                else:
                    self._collect_private_broker_socket()
                    broker_stopped = (
                        not self._gpu_authority_path("broker.sock").exists()
                        and not self._gpu_authority_path(
                            "broker.sock"
                        ).is_symlink()
                    )
                    _require(broker_stopped, "fresh Broker was not collected")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Broker process cleanup: {type(exc).__name__}: {exc}")
            finally:
                if self.broker_log_handle is not None:
                    self.broker_log_handle.close()
                    self.broker_log_handle = None
        self.cleanup_evidence = {
            "worker_stopped": worker_stopped,
            "containers_removed": containers_removed,
            "volume_removed": volume_removed,
            "network_removed": network_removed,
            "broker_drained": broker_drained,
            "broker_stopped": broker_stopped,
            "mps_indices_stopped": sorted(stopped),
            "leases_empty": leases_empty,
            "candidate_image_tags": candidate_image_tags,
            "candidate_image_tags_sha256": (
                acceptance_contract.canonical_json_digest(
                    candidate_image_tags
                )
            ),
            "candidate_images_absent_before": (
                self.candidate_images_absent_before
            ),
            "candidate_images_removed": candidate_images_removed,
            "ordinary_dev_images_before_sha256": (
                acceptance_contract.canonical_json_digest(
                    self.ordinary_dev_images_before
                )
            ),
            "ordinary_dev_images_after_sha256": (
                acceptance_contract.canonical_json_digest(
                    ordinary_dev_images_after
                )
            ),
            "ordinary_dev_images_unchanged": ordinary_dev_images_unchanged,
        }
        if (
            self.broker_process_evidence is None
            or self.broker_instance_id is None
            or self.initial_leases is None
            or self.final_leases is None
            or self.worker_evidence is None
            or self.container_evidence is None
        ):
            self._close_runtime_descriptors()
            return None, errors
        evidence = {
            "mode": "fresh_exact_f",
            "image_mode": (
                "published_exact"
                if self.run_kind == "final-main"
                else "candidate_local"
            ),
            "project_name": self.project_name,
            "authority": {
                "sha": self.authority_sha,
                "tree": self.authority_tree,
            },
            "broker": {
                "instance_id": self.broker_instance_id,
                "process": self.broker_process_evidence,
                "initial_leases": self.initial_leases,
                "final_leases": self.final_leases,
                "socket_sha256": acceptance_contract.canonical_json_digest(
                    {
                        "path": str(self.broker_socket),
                        "instance_id": self.broker_instance_id,
                    }
                ),
            },
            "worker": self.worker_evidence,
            "containers": self.container_evidence,
            "cleanup": self.cleanup_evidence,
        }
        self._close_runtime_descriptors()
        return evidence, errors


def _prepare_formal_smoke_runtime(
    controller: FreshAcceptanceControl,
) -> dict[str, Any]:
    """Run smoke only after installing the controller's exact FD authority."""

    os.environ.update(
        {
            "NEXPOLY_DFT_FORMAL_ACCEPTANCE": "1",
            "NEXPOLY_DFT_PROJECT_NAME": controller.project_name,
            "NEXPOLY_DFT_AUTHORITY_SHA": controller.authority_sha,
            **controller._formal_gpu_authority_environment(),
        }
    )
    result = smoke_runtime.prepare_runtime(REPO_ROOT)
    _require(
        result["broker_enabled"] is True
        and result["formal_gpu_authority"] is True,
        "real acceptance requires the formal descriptor-bound Host Broker",
    )
    return result


def run_acceptance(args: argparse.Namespace) -> Path:
    validate_git_authority(args.authority_sha, args.authority_tree)
    validate_bridge_authority(
        bridge_sha=args.bridge_sha,
        bridge_tree=args.bridge_tree,
        authority_sha=args.authority_sha,
    )
    _require_backend_url(args.backend_url)
    _require(
        args.stack_mode == "manage",
        "real acceptance requires a harness-owned fresh stack",
    )
    runtime_root = _safe_runtime_root()
    images: dict[str, Any] | None = None
    if args.run_kind == "final-main":
        _require(args.images is not None, "final-main requires authority OCI images")
        images = _load_json(args.images, "authority OCI image evidence")
        _validate_authority_images_input(
            images,
            authority_sha=args.authority_sha,
        )
    else:
        _require(
            args.run_kind == "candidate-tree" and args.images is None,
            "candidate-tree prevalidation must not claim final OCI images",
        )
    production_before = _production_repo_snapshot()
    before_gpu2 = snapshot_gpu2()

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    run_directory = runtime_root / "runs" / f"gpu-acceptance-{stamp}-{os.getpid()}"
    run_directory.mkdir(mode=0o700)
    monitor = Gpu2AuditMonitor(before_gpu2)
    monitor.start()
    controller = FreshAcceptanceControl(
        runtime_root=runtime_root,
        run_directory=run_directory,
        authority_sha=args.authority_sha,
        authority_tree=args.authority_tree,
        gpu3_mode=args.gpu3_mode,
        stack_timeout=args.stack_timeout,
        run_kind=args.run_kind,
        authority_images=images,
    )
    preflight_result: dict[str, Any] | None = None
    e2e: dict[str, Any] | None = None
    science: dict[str, Any] | None = None
    gpu3_identity: dict[str, Any] | None = None
    primary_error: BaseException | None = None
    try:
        resolved = controller.start()
        preflight_result = _prepare_formal_smoke_runtime(controller)
        backend_evidence = run_backend_e2e(
            base_url=BACKEND_BASE_URL,
            job_root=runtime_root / "monomer-dft-worker-runs",
            timeout_seconds=args.job_timeout,
        )
        e2e = backend_evidence["e2e"]
        science = backend_evidence["science"]
        _require(
            controller.worker_evidence is not None
            and controller.broker_instance_id is not None
            and science["worker_instance_id"]
            == controller.worker_evidence["instance_id"]
            and science["broker_instance_id"] == controller.broker_instance_id,
            "Backend science did not use the fresh exact-F Worker/Broker",
        )

        claim_before = _docker_gpu3_claim()
        requested_mode = args.gpu3_mode
        if requested_mode == "externally_fenced":
            _require(
                claim_before is not None,
                "GPU3 externally_fenced mode lacks a Docker claim",
            )
        if requested_mode == "actual":
            _require(
                claim_before is None,
                "GPU3 actual mode conflicts with a Docker claim",
            )
        if claim_before is not None:
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
                resolved=resolved,
            )
            claim_after = _docker_gpu3_claim()
            _require(
                claim_after is not None,
                "GPU3 Docker claim disappeared during rejection proof",
            )
            claim = _bind_gpu3_claim_cas(claim_before, claim_after)
            rejection = _finalize_gpu3_rejection(
                rejection,
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
            controller.ensure_gpu3_mps()
            gpu3_direct, gpu3_lease = run_leased_direct(
                resolved=resolved,
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
                "result": gpu3_direct,
                "lease": gpu3_lease,
                "evidence_sha256": acceptance_contract.canonical_json_digest(
                    {"result": gpu3_direct, "lease": gpu3_lease}
                ),
            }
            # This is an exact Broker-leased overflow calculation, not a
            # Backend/UDS job. Keep the Backend provenance truthful.
            e2e["gpu_indices"] = [1]
            e2e["overflow_test_status"] = "passed"
    except BaseException as exc:  # noqa: BLE001 - cleanup must still run
        primary_error = exc

    control_plane: dict[str, Any] | None = None
    cleanup_errors: list[str] = []
    try:
        control_plane, cleanup_errors = controller.cleanup()
    except Exception as exc:  # noqa: BLE001 - retain primary error and evidence
        cleanup_errors.append(f"controller cleanup: {type(exc).__name__}: {exc}")

    gpu2_audit: dict[str, Any] | None = None
    try:
        gpu2_audit = monitor.stop()
    except Exception as exc:  # noqa: BLE001
        cleanup_errors.append(f"GPU2 audit finalization: {type(exc).__name__}: {exc}")

    production_after: dict[str, Any] | None = None
    try:
        production_after = _production_repo_snapshot()
        _require(
            production_after == production_before,
            "production repository changed during development acceptance",
        )
        validate_git_authority(args.authority_sha, args.authority_tree)
        validate_bridge_authority(
            bridge_sha=args.bridge_sha,
            bridge_tree=args.bridge_tree,
            authority_sha=args.authority_sha,
        )
    except Exception as exc:  # noqa: BLE001
        cleanup_errors.append(f"final CAS: {type(exc).__name__}: {exc}")

    if (
        primary_error is not None
        or cleanup_errors
        or control_plane is None
        or gpu2_audit is None
        or production_after is None
        or preflight_result is None
        or e2e is None
        or science is None
        or gpu3_identity is None
    ):
        failure = {
            "schema_version": 1,
            "status": "failed",
            "captured_at": dt.datetime.now(dt.UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "primary_error": (
                None
                if primary_error is None
                else {
                    "type": type(primary_error).__name__,
                    "message": str(primary_error),
                }
            ),
            "cleanup_errors": cleanup_errors,
            "control_plane": control_plane,
            "gpu2_audit": gpu2_audit,
            "production_cas": {
                "before": production_before,
                "after": production_after,
            },
        }
        try:
            _write_private_json(
                run_directory / "gpu-acceptance-failure.json", failure
            )
        except Exception as write_error:  # noqa: BLE001
            cleanup_errors.append(
                f"failure evidence: {type(write_error).__name__}: {write_error}"
            )
        messages = []
        if primary_error is not None:
            messages.append(f"{type(primary_error).__name__}: {primary_error}")
        messages.extend(cleanup_errors)
        raise AcceptanceHarnessError(
            "GPU acceptance failed closed: " + "; ".join(messages)
        ) from primary_error

    after_gpu2 = gpu2_audit["samples"][-1]
    report_payload: dict[str, Any] = {
            "schema_version": 1,
            "status": (
                "passed" if args.run_kind == "final-main" else "prevalidated"
            ),
            "run_kind": args.run_kind,
            "captured_at": dt.datetime.now(dt.UTC)
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
            "images": (
                images
                if images is not None
                else {
                    "mode": "candidate-local",
                    "warning": (
                        "not production-valid; rerun final-main after immutable "
                        "main images are published"
                    ),
                }
            ),
            "runtime": _runtime_evidence(preflight_result),
            "control_plane": control_plane,
            "production_cas": {
                "before": production_before,
                "after": production_after,
                "unchanged": True,
            },
            "coverage": {
                "broker_science": science,
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
                        acceptance_contract.canonical_json_digest(science)
                    ),
                },
                "2": {
                    "index": 2,
                    "uuid": GPU_UUIDS["2"],
                    "mode": "unchanged",
                    "cuda_started": False,
                    "before": before_gpu2,
                    "after": after_gpu2,
                    "audit": gpu2_audit,
                    "processes_unchanged": True,
                    "memory_unchanged": True,
                },
                "3": gpu3_identity,
            },
        }
    report = acceptance_contract.seal_report(report_payload)
    if args.run_kind == "candidate-tree":
        output = run_directory / "gpu-acceptance-candidate-tree.json"
        _write_private_json(output, report)
        return output
    assert images is not None
    acceptance_contract.validate_report(
        report,
        authority={"sha": args.authority_sha, "tree": args.authority_tree},
        bridge={"sha": args.bridge_sha, "tree": args.bridge_tree},
        authority_images=images,
        runtime_contract=runtime_contract.RUNTIME_CONTRACT,
        runtime_contract_sha256=runtime_contract.RUNTIME_CONTRACT_SHA256,
        observed_at=dt.datetime.now(dt.UTC),
    )
    output = run_directory / "gpu-acceptance-report.json"
    _write_private_json(output, report)
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authority-sha")
    parser.add_argument("--authority-tree")
    parser.add_argument("--bridge-sha")
    parser.add_argument("--bridge-tree")
    parser.add_argument("--images", type=Path)
    parser.add_argument(
        "--run-kind",
        choices=("candidate-tree", "final-main"),
        default="final-main",
    )
    parser.add_argument(
        "--gpu3-mode",
        choices=("auto", "actual", "externally_fenced"),
        default="auto",
    )
    parser.add_argument(
        "--stack-mode",
        choices=("manage",),
        default="manage",
    )
    parser.add_argument(
        "--backend-url",
        choices=(BACKEND_BASE_URL,),
        default=BACKEND_BASE_URL,
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
    )
    if any(item is None for item in required) or (
        args.run_kind == "final-main" and args.images is None
    ) or (
        args.run_kind == "candidate-tree" and args.images is not None
    ):
        print(
            "authority/bridge SHA/tree are required; final-main requires "
            "--images and candidate-tree forbids it",
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

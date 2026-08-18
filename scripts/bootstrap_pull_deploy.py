#!/usr/bin/python3 -I
"""Install the immutable pull router and initial content-addressed controls.

Bootstrap is the only operation allowed to install ``runtime/bin``.  It does
not start or stop services, contact PostgreSQL, change Git HEAD, create a
Worker slot, or write credentials.  Ordinary deployments install new control
releases without ever overwriting the router or an existing release.
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import ctypes
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import subprocess
import sys
import types
from typing import Iterable
import urllib.error
import urllib.parse
import urllib.request

sys.dont_write_bytecode = True

PRODUCTION_ROOT = Path("/data/lzq/gith/nexpoly")
RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
WORKER_UNIT_PATH = Path(
    "/home/devuser/.config/systemd/user/nexpoly-monomer-md-worker.service"
)
DFT_WORKER_UNIT_PATH = Path(
    "/home/devuser/.config/systemd/user/nexpoly-monomer-dft-worker.service"
)
SCRIPT_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_ROOT.parent
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
TAKEOVER_OPERATION_RE = re.compile(r"takeover-[a-z0-9][a-z0-9-]{7,79}\Z")
ADOPTION_OPERATION_RE = re.compile(r"adopt-[a-z0-9][a-z0-9._-]{7,119}\Z")
REPOSITORY_SSH_URL = "git@github.com:lzq390/ZhijuPoly.git"
REPOSITORY_API_ROOT = "https://api.github.com/repos/lzq390/ZhijuPoly"
SYSTEM_CA_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
GIT_EXTERNAL_STORAGE_MARKERS = (
    Path(".git/commondir"),
    Path(".git/info/grafts"),
    Path(".git/objects/info/alternates"),
    Path(".git/objects/info/http-alternates"),
)

DIRECTORIES = {
    "bin": 0o700,
    "config": 0o700,
    "config/docker": 0o700,
    "state": 0o700,
    "state/prepared": 0o700,
    "state/control-handoffs": 0o700,
    "state/worker-slots": 0o700,
    "state/contract-operations": 0o700,
    "state/contract-verification-databases": 0o700,
    "state/maintenance": 0o700,
    "state/maintenance/0005-polytao-alias": 0o700,
    "state/monomer-md-worker-socket": 0o700,
    "state/monomer-md-worker-runs": 0o700,
    "state/gpu-resource": 0o700,
    "state/adoption-transactions": 0o700,
    "audit": 0o700,
    "audit/production-readiness": 0o700,
    "audit/mutable-data": 0o700,
    "audit/bootstrap-worker-unit": 0o700,
    "audit/contracts": 0o700,
    "audit/contracts/0012": 0o700,
    "audit/maintenance": 0o700,
    "audit/maintenance/0005-polytao-alias": 0o700,
    "audit/adoption": 0o700,
    "backups": 0o700,
    "backups/bootstrap-worker-unit": 0o700,
    "backups/contracts": 0o700,
    "backups/contracts/0012": 0o700,
    "backups/maintenance": 0o700,
    "backups/maintenance/0005-polytao-alias": 0o700,
    "wheel-cache": 0o700,
    "worker-venvs": 0o700,
    "control-releases": 0o700,
}

IMMUTABLE_FILES = {
    "control_runtime_selector.py": (SCRIPT_ROOT / "control_runtime_selector.py", 0o700),
    "nexpoly-pull-deploy": (SCRIPT_ROOT / "nexpoly-pull-deploy", 0o700),
    "nexpoly-postgres-media-evidence": (
        SCRIPT_ROOT / "nexpoly-postgres-media-evidence",
        0o700,
    ),
    "nexpoly-production-readiness": (
        SCRIPT_ROOT / "nexpoly-production-readiness",
        0o700,
    ),
    "nexpoly-pull-contract-0012": (
        SCRIPT_ROOT / "nexpoly-pull-contract-0012",
        0o700,
    ),
    "nexpoly-reconcile-production-0005-polytao-alias": (
        SCRIPT_ROOT / "nexpoly-reconcile-production-0005-polytao-alias",
        0o700,
    ),
}

BOOTSTRAP_TRANSACTION_SCHEMA_VERSION = 1
BOOTSTRAP_TRANSACTION_RELATIVE_DIRECTORY = Path(
    "state/legacy-takeover/bootstrap-children"
)
BOOTSTRAP_PHASES = (
    "intent",
    "runtime-layout-intent",
    "runtime-layout-ready",
    "checkout-intent",
    "checkout-ready",
    "worker-unit-intent",
    "worker-unit-ready",
    "immutable-controls-intent",
    "immutable-controls-ready",
    "control-release-intent",
    "control-release-ready",
    "authority-commit-intent",
    "completed",
)

ADOPTION_TRANSACTION_SCHEMA_VERSION = 2
ADOPTION_PHASES = (
    "intent",
    "layout-ready",
    "controls-ready",
    "baseline-ready",
    "authority-commit-intent",
    "completed",
)
ADOPTED_DEPLOYMENT_SCHEMA_VERSION = 1
ADOPTION_AUTHORITY_KIND = "manual-runtime-adoption"
ADOPTION_DEPLOY_LOCK_DISPOSITION = "permanent-control-layout"
ADOPTION_JOURNAL_TEMP_MAX_BYTES = 16 * 1024 * 1024
ADOPTION_JOURNAL_TEMP_LIMIT = 32
ADOPTED_DFT_GPU_UUID = "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe"
ADOPTED_DFT_RUNTIME_SYMLINKS = {
    "venv/bin/python": "/usr/bin/python3.12",
    "venv/bin/python3": "python",
    "venv/bin/python3.12": "python",
    "venv/lib64": "lib",
}
ADOPTION_TRANSACTION_RELATIVE_DIRECTORY = Path("state/adoption-transactions")
ADOPTED_DEPLOYMENT_RELATIVE_PATH = Path("state/adopted-deployment.json")
ADOPTION_ALIAS_MARKER_RELATIVE_PATH = Path(
    "state/maintenance/0005-polytao-alias/operation.json"
)


class BootstrapError(RuntimeError):
    pass


def digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _control_runtime(*, source_sha: str, allow_test: bool) -> object:
    """Execute only bytes already bound to the reviewed Git commit.

    Importing the live worktree here would create a validation/import TOCTOU.
    Compiling the byte-identical Git-object payload keeps bootstrap independent
    of mutable Python files after source identity has been established.
    """

    payload = _read_reviewed_source(
        "scripts/control_runtime_selector.py",
        source_sha=source_sha,
        allow_test=allow_test,
    )
    module = types.ModuleType("nexpoly_bootstrap_control_runtime")
    module.__file__ = f"git:{source_sha}:scripts/control_runtime_selector.py"
    try:
        exec(compile(payload, module.__file__, "exec"), module.__dict__)
    except BaseException as exc:
        raise BootstrapError("cannot load reviewed immutable control runtime") from exc
    return module


def _legacy_takeover_evidence(
    *,
    source_sha: str,
    allow_test: bool,
    installed_runtime_root: Path | None = None,
) -> object:
    """Load the takeover validator from the exact reviewed F commit."""

    reviewed = _read_reviewed_source(
        "scripts/legacy_takeover_evidence.py",
        source_sha=source_sha,
        allow_test=allow_test,
    )
    if installed_runtime_root is None:
        payload = reviewed
        module_path = f"git:{source_sha}:scripts/legacy_takeover_evidence.py"
    else:
        path = (
            installed_runtime_root
            / "legacy-takeover/bin/legacy_takeover_evidence.py"
        )
        try:
            metadata = path.lstat()
            payload = path.read_bytes()
        except OSError as exc:
            raise BootstrapError(
                "installed takeover evidence validator is unavailable"
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or payload != reviewed
        ):
            raise BootstrapError(
                "installed takeover evidence validator differs from reviewed F"
            )
        module_path = str(path)
    module = types.ModuleType("nexpoly_bootstrap_legacy_takeover_evidence")
    module.__file__ = module_path
    try:
        exec(compile(payload, module_path, "exec"), module.__dict__)
    except BaseException as exc:
        raise BootstrapError(
            "reviewed legacy takeover validator cannot be loaded"
        ) from exc
    return module


def _takeover_git_identity(
    production_repository: dict[str, object],
) -> dict[str, str]:
    head = production_repository.get("head")
    tree = production_repository.get("tree")
    if (
        production_repository.get("branch") != "main"
        or not isinstance(head, str)
        or SHA_RE.fullmatch(head) is None
        or not isinstance(tree, str)
        or SHA_RE.fullmatch(tree) is None
    ):
        raise BootstrapError(
            "production repository cannot bind legacy takeover"
        )
    return {
        "branch": "refs/heads/main",
        "head_sha": head,
        "head_tree": tree,
        "local_main_sha": head,
    }


def _completed_legacy_takeover(
    runtime_root: Path,
    operation_id: str,
    *,
    source_sha: str,
    source_tree: str,
    production_repository: dict[str, object],
    allow_test: bool,
) -> dict[str, object]:
    """Require source-pinned, completed, exact pre-stopped takeover evidence."""

    validator = _legacy_takeover_evidence(
        source_sha=source_sha,
        allow_test=allow_test,
    )
    try:
        binding = validator.validate_completed(
            runtime_root,
            operation_id,
            source_sha,
            source_tree,
            expected_git_identity=_takeover_git_identity(
                production_repository
            ),
        )
    except Exception as exc:
        raise BootstrapError(
            "completed exact legacy takeover authority is required"
        ) from exc
    if (
        not isinstance(binding, dict)
        or binding.get("operation_id") != operation_id
        or binding.get("authority_sha") != source_sha
        or binding.get("authority_tree") != source_tree
        or not isinstance(binding.get("binding_sha256"), str)
        or not str(binding["binding_sha256"]).startswith("sha256:")
    ):
        raise BootstrapError("legacy takeover binding is invalid")
    return dict(binding)


def _github_token(runtime_root: Path) -> str:
    path = runtime_root / "config/github-api-token"
    try:
        metadata = path.lstat()
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise BootstrapError("GitHub API token is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or not token
        or any(character.isspace() for character in token)
    ):
        raise BootstrapError("GitHub API token is unsafe or malformed")
    return token


def _request_github_json(url: str, token: str) -> dict[str, object]:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "api.github.com"
        or not parsed.path.startswith("/repos/lzq390/ZhijuPoly/")
    ):
        raise BootstrapError("GitHub evidence URL is outside the pinned repository")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "nexpoly-bootstrap-pull-deploy/1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            return None

    try:
        ca_metadata = SYSTEM_CA_BUNDLE.lstat()
        if (
            not stat.S_ISREG(ca_metadata.st_mode)
            or SYSTEM_CA_BUNDLE.is_symlink()
            or ca_metadata.st_uid != 0
            or ca_metadata.st_mode & 0o022
        ):
            raise BootstrapError("system CA bundle identity is unsafe")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_verify_locations(cafile=str(SYSTEM_CA_BUNDLE))
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
            NoRedirect(),
        )
        with opener.open(request, timeout=30) as response:
            if response.geturl() != url:
                raise BootstrapError("GitHub evidence request was redirected")
            payload = response.read(2 * 1024 * 1024 + 1)
    except BootstrapError:
        raise
    except (OSError, urllib.error.URLError) as exc:
        raise BootstrapError("GitHub delivery evidence is unavailable") from exc
    if len(payload) > 2 * 1024 * 1024:
        raise BootstrapError("GitHub delivery evidence is too large")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BootstrapError("GitHub delivery evidence is malformed") from exc
    if not isinstance(document, dict):
        raise BootstrapError("GitHub delivery evidence is not an object")
    return document


def _delivery_gate(
    production_root: Path,
    runtime_root: Path,
    source_sha: str,
    *,
    allow_test: bool,
    sealed: dict[str, object] | None = None,
) -> dict[str, object]:
    required = set(
        _required_ci_jobs(
            source_sha=source_sha,
            allow_test=allow_test,
        )
    )
    if allow_test:
        evidence = {
            "remote_main": source_sha,
            "ci": {
                "head_sha": source_sha,
                "conclusion": "success",
                "required_jobs": sorted(required),
            },
        }
        if sealed is not None and sealed != evidence:
            raise BootstrapError("sealed test delivery evidence differs")
        return evidence
    del production_root
    token = _github_token(runtime_root)
    remote_ref = _request_github_json(
        f"{REPOSITORY_API_ROOT}/git/ref/heads/main", token
    )
    remote_object = remote_ref.get("object")
    remote_main = (
        remote_object.get("sha") if isinstance(remote_object, dict) else None
    )
    if (
        remote_ref.get("ref") != "refs/heads/main"
        or not isinstance(remote_object, dict)
        or remote_object.get("type") != "commit"
        or not isinstance(remote_main, str)
        or SHA_RE.fullmatch(remote_main) is None
    ):
        raise BootstrapError("protected remote main evidence is malformed")
    if remote_main != source_sha:
        raise BootstrapError("bootstrap source is not the current protected remote main")
    sealed_ci = sealed.get("ci") if isinstance(sealed, dict) else None
    if sealed is not None:
        if (
            set(sealed) != {"remote_main", "ci"}
            or sealed.get("remote_main") != source_sha
            or not isinstance(sealed_ci, dict)
            or not isinstance(sealed_ci.get("workflow_run_id"), int)
            or isinstance(sealed_ci.get("workflow_run_id"), bool)
            or not isinstance(sealed_ci.get("run_attempt"), int)
            or isinstance(sealed_ci.get("run_attempt"), bool)
            or sealed_ci["run_attempt"] <= 0
        ):
            raise BootstrapError("sealed bootstrap delivery evidence is invalid")
        run_id = sealed_ci["workflow_run_id"]
        run_attempt = sealed_ci["run_attempt"]
    else:
        runs = _request_github_json(
            f"{REPOSITORY_API_ROOT}/actions/runs?branch=main&head_sha={source_sha}"
            "&event=push&per_page=20",
            token,
        ).get("workflow_runs")
        if not isinstance(runs, list):
            raise BootstrapError("GitHub workflow evidence has no run list")
        candidates = [
            value
            for value in runs
            if isinstance(value, dict)
            and value.get("head_sha") == source_sha
            and value.get("head_branch") == "main"
            and value.get("event") == "push"
            and value.get("status") == "completed"
            and value.get("conclusion") == "success"
            and value.get("path") == ".github/workflows/ci.yml"
            and isinstance(value.get("id"), int)
            and not isinstance(value.get("id"), bool)
        ]
        if not candidates:
            raise BootstrapError(
                "target main has no successful completed CI workflow run"
            )
        selected = max(
            candidates,
            key=lambda value: (value.get("run_attempt", 0), value["id"]),
        )
        run_id = selected["id"]
        run_attempt = selected.get("run_attempt", 1)
        if (
            not isinstance(run_attempt, int)
            or isinstance(run_attempt, bool)
            or run_attempt <= 0
        ):
            raise BootstrapError("selected CI workflow attempt is invalid")
    # A workflow rerun retains the run ID and increments run_attempt.  Always
    # bind both the workflow and jobs to the exact reviewed attempt so a crash
    # recovery cannot drift to a newer rerun of the same SHA/run ID.
    run = _request_github_json(
        f"{REPOSITORY_API_ROOT}/actions/runs/{run_id}/attempts/{run_attempt}",
        token,
    )
    if not (
        run.get("head_sha") == source_sha
        and run.get("head_branch") == "main"
        and run.get("event") == "push"
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("path") == ".github/workflows/ci.yml"
        and isinstance(run.get("id"), int)
        and not isinstance(run.get("id"), bool)
        and run.get("id") == run_id
        and run.get("run_attempt") == run_attempt
    ):
        raise BootstrapError("sealed CI workflow run no longer verifies")
    jobs = _request_github_json(
        f"{REPOSITORY_API_ROOT}/actions/runs/{run_id}/attempts/{run_attempt}/jobs"
        "?per_page=100",
        token,
    ).get("jobs")
    if not isinstance(jobs, list):
        raise BootstrapError("GitHub workflow evidence has no job list")
    successful = {
        job.get("name")
        for job in jobs
        if isinstance(job, dict) and job.get("conclusion") == "success"
    }
    if not required.issubset(successful):
        raise BootstrapError("target CI lacks required successful jobs")
    ci = {
        "workflow_run_id": run_id,
        "run_attempt": run_attempt,
        "head_sha": source_sha,
        "head_branch": "main",
        "event": "push",
        "path": ".github/workflows/ci.yml",
        "conclusion": "success",
        "required_jobs": sorted(required),
    }
    evidence = {"remote_main": remote_main, "ci": ci}
    if sealed is not None and evidence != sealed:
        raise BootstrapError("sealed bootstrap delivery evidence changed")
    return evidence


def _required_ci_jobs(*, source_sha: str, allow_test: bool) -> tuple[str, ...]:
    """Read the sole required-job contract from exact F authority bytes."""

    payload = _read_reviewed_source(
        "scripts/bridge_deploy_core.py",
        source_sha=source_sha,
        allow_test=allow_test,
    )
    module = types.ModuleType("nexpoly_bootstrap_bridge_ci_contract")
    module.__file__ = f"git:{source_sha}:scripts/bridge_deploy_core.py"
    try:
        exec(compile(payload, module.__file__, "exec"), module.__dict__)
    except BaseException as exc:
        raise BootstrapError("F bridge CI contract cannot be loaded") from exc
    raw = getattr(module, "REQUIRED_CI_JOBS", None)
    if (
        not isinstance(raw, (set, frozenset))
        or not raw
        or len(raw) > 32
        or any(not isinstance(value, str) or not value for value in raw)
    ):
        raise BootstrapError("F bridge CI contract is invalid")
    jobs = tuple(sorted(raw))
    if len(jobs) != len(raw):
        raise BootstrapError("F bridge CI contract contains duplicate jobs")
    return jobs


def _safe_source(path: Path) -> bytes:
    try:
        metadata = path.lstat()
        payload = path.read_bytes()
    except OSError as exc:
        raise BootstrapError(f"bootstrap source file is missing: {path}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
    ):
        raise BootstrapError(f"bootstrap source file is unsafe: {path}")
    return payload


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _inode_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _durability_barrier(
    path: Path, *, payload: bytes | None, mode: int, directory: bool = False
) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (
            before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != mode
            or (directory and not stat.S_ISDIR(before.st_mode))
            or (not directory and not stat.S_ISREG(before.st_mode))
        ):
            raise BootstrapError(f"durability barrier identity is unsafe: {path}")
        observed = b""
        if not directory:
            if before.st_size > 64 * 1024 * 1024:
                raise BootstrapError(f"durability barrier file is too large: {path}")
            chunks = bytearray()
            while len(chunks) < before.st_size:
                chunk = os.read(descriptor, before.st_size - len(chunks))
                if not chunk:
                    break
                chunks.extend(chunk)
            observed = bytes(chunks)
            if len(observed) != before.st_size or (
                payload is not None and observed != payload
            ):
                raise BootstrapError(f"durability barrier payload differs: {path}")
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        if (
            _inode_identity(before) != _inode_identity(after)
            or _inode_identity(path.lstat()) != _inode_identity(after)
        ):
            raise BootstrapError(f"durability barrier path changed: {path}")
        _fsync_directory(path.parent)
        if _inode_identity(path.lstat()) != _inode_identity(after):
            raise BootstrapError(f"durability barrier path changed: {path}")
        return observed
    finally:
        os.close(descriptor)


def _rename_noreplace(source: Path, destination: Path) -> None:
    if source.parent != destination.parent:
        raise BootstrapError("no-clobber publication must remain in one parent")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise BootstrapError("renameat2 is required for no-clobber publication")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,  # RENAME_NOREPLACE
    )
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(error, os.strerror(error), str(destination))
        raise BootstrapError(
            f"no-clobber publication failed: {os.strerror(error)}"
        )
    _fsync_directory(destination.parent)


def _rename_exchange(first: Path, second: Path) -> None:
    if first.parent != second.parent:
        raise BootstrapError("authority exchange must remain in one parent")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise BootstrapError("renameat2 is required for authority exchange")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(first),
        -100,
        os.fsencode(second),
        2,  # RENAME_EXCHANGE
    )
    if result != 0:
        error = ctypes.get_errno()
        raise BootstrapError(f"authority exchange failed: {os.strerror(error)}")
    _fsync_directory(first.parent)


def _atomic_file(path: Path, payload: bytes, mode: int) -> None:
    temporary = path.parent / f".{path.name}.{os.urandom(12).hex()}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        _fsync_directory(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def _atomic_json(path: Path, document: dict[str, object]) -> None:
    payload = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8") + b"\n"
    _atomic_file(path, payload, 0o600)


def _load_private_json(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
        payload = path.read_bytes()
    except OSError as exc:
        raise BootstrapError(f"private bootstrap record is unavailable: {path}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise BootstrapError(f"private bootstrap record is unsafe: {path}")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"private bootstrap record is invalid: {path}") from exc
    if not isinstance(value, dict):
        raise BootstrapError(f"private bootstrap record is invalid: {path}")
    return value


def _canonical_json_digest(value: object) -> str:
    return digest(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    )


def _adopted_dft_runtime_inventory(root: Path) -> str:
    """Seal the exact legacy runtime while excluding mutable Warp payloads.

    The manually-built fc05 uv environment contains cache-backed hard links
    and one owner-contained 0666 lock file.  Those legacy identities are
    recorded verbatim for later CAS instead of applying the stricter target
    runtime policy (which requires single-link regular files).
    """

    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise BootstrapError("adopted monomer DFT runtime is unavailable") from exc
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root.is_symlink()
        or root_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(root_metadata.st_mode) != 0o700
    ):
        raise BootstrapError("adopted monomer DFT runtime root is unsafe")

    def immutable_paths(directory: Path) -> Iterable[Path]:
        try:
            with os.scandir(directory) as stream:
                entries = sorted(stream, key=lambda entry: entry.name)
        except OSError as exc:
            raise BootstrapError(
                "adopted monomer DFT runtime changed during inventory"
            ) from exc
        for entry in entries:
            path = directory / entry.name
            yield path
            relative = path.relative_to(root).as_posix()
            if relative == "warp-cache":
                continue
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                raise BootstrapError(
                    "adopted monomer DFT runtime changed during inventory"
                ) from exc
            if is_directory:
                yield from immutable_paths(path)

    records: list[dict[str, object]] = [
        {
            "path": ".",
            "kind": "directory",
            "uid": root_metadata.st_uid,
            "mode": stat.S_IMODE(root_metadata.st_mode),
            "nlink": root_metadata.st_nlink,
        }
    ]
    observed_links: set[str] = set()
    warp_root_seen = False
    for path in immutable_paths(root):
        relative = path.relative_to(root).as_posix()
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise BootstrapError(
                "adopted monomer DFT runtime changed during inventory"
            ) from exc
        mode = stat.S_IMODE(metadata.st_mode)
        if metadata.st_uid != os.geteuid() or metadata.st_nlink < 1:
            raise BootstrapError("adopted monomer DFT runtime ownership is unsafe")
        if relative == "warp-cache":
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or path.is_symlink()
                or mode != 0o700
            ):
                raise BootstrapError(
                    "adopted monomer DFT mutable Warp cache root is unsafe"
                )
            warp_root_seen = True
            records.append(
                {
                    "path": relative,
                    "kind": "mutable-directory",
                    "uid": metadata.st_uid,
                    "mode": mode,
                }
            )
            continue
        if stat.S_ISLNK(metadata.st_mode):
            try:
                target = os.readlink(path)
                after = path.lstat()
            except OSError as exc:
                raise BootstrapError(
                    "adopted monomer DFT runtime symlink changed"
                ) from exc
            if (
                ADOPTED_DFT_RUNTIME_SYMLINKS.get(relative) != target
                or (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_uid,
                    metadata.st_nlink,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                )
                != (
                    after.st_dev,
                    after.st_ino,
                    after.st_mode,
                    after.st_uid,
                    after.st_nlink,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
            ):
                raise BootstrapError(
                    "adopted monomer DFT runtime contains an unknown symlink"
                )
            observed_links.add(relative)
            records.append(
                {
                    "path": relative,
                    "kind": "symlink",
                    "uid": metadata.st_uid,
                    "mode": mode,
                    "nlink": metadata.st_nlink,
                    "target": target,
                }
            )
            continue
        if stat.S_ISDIR(metadata.st_mode):
            if mode & 0o022:
                raise BootstrapError("adopted monomer DFT runtime mode is unsafe")
            records.append(
                {
                    "path": relative,
                    "kind": "directory",
                    "uid": metadata.st_uid,
                    "mode": mode,
                    "nlink": metadata.st_nlink,
                }
            )
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise BootstrapError("adopted monomer DFT runtime contains a special file")
        if (relative != "venv/.lock" and mode & 0o022) or (
            relative == "venv/.lock" and mode != 0o666
        ):
            raise BootstrapError("adopted monomer DFT runtime mode is unsafe")
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as exc:
            raise BootstrapError(
                "adopted monomer DFT runtime file is unavailable"
            ) from exc
        try:
            before = os.fstat(descriptor)
            file_digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                file_digest.update(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or identity
            != (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_uid,
                metadata.st_nlink,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
            or identity
            != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_uid,
                after.st_nlink,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        ):
            raise BootstrapError(
                "adopted monomer DFT runtime file changed during inventory"
            )
        records.append(
            {
                "path": relative,
                "kind": "file",
                "uid": before.st_uid,
                "mode": stat.S_IMODE(before.st_mode),
                "nlink": before.st_nlink,
                "size": before.st_size,
                "sha256": "sha256:" + file_digest.hexdigest(),
            }
        )
    if observed_links != set(ADOPTED_DFT_RUNTIME_SYMLINKS) or not warp_root_seen:
        raise BootstrapError("adopted monomer DFT runtime layout is incomplete")
    return _canonical_json_digest(records)


def _dft_worker_unit_path(production_root: Path, *, allow_test: bool) -> Path:
    if allow_test:
        return (
            production_root.parent
            / "systemd/user/nexpoly-monomer-dft-worker.service"
        )
    return DFT_WORKER_UNIT_PATH


def _require_adoption_operation_id(value: object) -> str:
    if not isinstance(value, str) or ADOPTION_OPERATION_RE.fullmatch(value) is None:
        raise BootstrapError("adoption operation ID is invalid")
    return value


def _adoption_transaction_path(
    runtime_root: Path, *, operation_id: str
) -> Path:
    operation_id = _require_adoption_operation_id(operation_id)
    return (
        runtime_root
        / ADOPTION_TRANSACTION_RELATIVE_DIRECTORY
        / f"{operation_id}.json"
    )


def _adoption_transactions(
    runtime_root: Path,
) -> dict[str, dict[str, object]]:
    """Load every durable adoption journal without creating the journal root."""

    directory = runtime_root / ADOPTION_TRANSACTION_RELATIVE_DIRECTORY
    if not (directory.exists() or directory.is_symlink()):
        return {}
    try:
        metadata = directory.lstat()
        entries = sorted(directory.iterdir(), key=lambda value: value.name)
    except OSError as exc:
        raise BootstrapError("adoption transaction inventory is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or directory.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise BootstrapError("adoption transaction directory is unsafe")
    transactions: dict[str, dict[str, object]] = {}
    safe_temporary_count = 0
    quarantines: list[tuple[str, Path]] = []
    for path in entries:
        name = path.name
        quarantine_match = re.fullmatch(
            r"\.(adopt-[a-z0-9][a-z0-9._-]{7,119})\.abort-quarantine",
            name,
        )
        if quarantine_match is not None:
            quarantines.append((quarantine_match.group(1), path))
            continue
        temporary_operation: str | None = None
        if name.startswith(".") and name.endswith(".tmp"):
            journal_and_token = name[1:-4]
            journal_name, separator, token = journal_and_token.rpartition(".")
            if (
                separator
                and journal_name.endswith(".json")
                and re.fullmatch(r"[0-9a-f]{24}", token) is not None
            ):
                candidate = journal_name.removesuffix(".json")
                if ADOPTION_OPERATION_RE.fullmatch(candidate) is not None:
                    temporary_operation = candidate
        if temporary_operation is not None:
            try:
                temporary = path.lstat()
            except OSError as exc:
                raise BootstrapError(
                    "adoption transaction staging is unavailable"
                ) from exc
            safe_temporary_count += 1
            if (
                safe_temporary_count > ADOPTION_JOURNAL_TEMP_LIMIT
                or not stat.S_ISREG(temporary.st_mode)
                or path.is_symlink()
                or temporary.st_uid != os.geteuid()
                or stat.S_IMODE(temporary.st_mode) != 0o600
                or temporary.st_nlink != 1
                or temporary.st_dev != metadata.st_dev
                or temporary.st_size > ADOPTION_JOURNAL_TEMP_MAX_BYTES
            ):
                raise BootstrapError("adoption transaction staging is unsafe")
            # Random atomic staging contains no authority.  Retain it while
            # ignoring only this strict private shape.
            continue
        if path.suffix != ".json":
            raise BootstrapError("adoption transaction inventory contains an unknown entry")
        operation_id = path.stem
        _require_adoption_operation_id(operation_id)
        transaction = _validate_adoption_transaction(
            _load_private_json(path), path=path
        )
        if transaction["operation_id"] != operation_id:
            raise BootstrapError("adoption transaction filename differs from its identity")
        transactions[operation_id] = transaction
    for operation_id, path in quarantines:
        transaction = transactions.get(operation_id)
        evidence = transaction.get("step_evidence") if transaction else None
        plan = evidence.get("abort_quarantine") if isinstance(evidence, dict) else None
        observed = path.lstat()
        if (
            not isinstance(plan, dict)
            or plan.get("root") != str(path)
            or not stat.S_ISDIR(observed.st_mode)
            or path.is_symlink()
            or observed.st_uid != os.geteuid()
            or stat.S_IMODE(observed.st_mode) != 0o700
        ):
            raise BootstrapError("adoption quarantine lacks durable authority")
    return transactions


def _assert_exclusive_adoption_transaction(
    runtime_root: Path, *, operation_id: str
) -> dict[str, object] | None:
    """Reject a second live adoption authority, even when its ID differs."""

    transactions = _adoption_transactions(runtime_root)
    foreign = [
        value
        for name, value in transactions.items()
        if name != operation_id and value.get("status") != "aborted"
    ]
    if foreign:
        raise BootstrapError("another manual runtime adoption transaction exists")
    return transactions.get(operation_id)


def _regular_file_record(
    path: Path,
    *,
    label: str,
    maximum: int = 16 * 1024 * 1024,
    allowed_modes: set[int] | None = None,
) -> tuple[dict[str, object], bytes]:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise BootstrapError(f"{label} is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or not 1 <= before.st_size <= maximum
            or (
                allowed_modes is not None
                and stat.S_IMODE(before.st_mode) not in allowed_modes
            )
        ):
            raise BootstrapError(f"{label} has an unsafe identity")
        payload = bytearray()
        while len(payload) < before.st_size:
            chunk = os.read(descriptor, min(1024 * 1024, before.st_size - len(payload)))
            if not chunk:
                raise BootstrapError(f"{label} changed while being read")
            payload.extend(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_mode,
        before.st_uid,
    )
    if identity != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_mode,
        after.st_uid,
    ):
        raise BootstrapError(f"{label} changed while being read")
    content = bytes(payload)
    return (
        {
            "path": str(path),
            "sha256": digest(content),
            "size": len(content),
            "mode": format(stat.S_IMODE(before.st_mode), "04o"),
        },
        content,
    )


def _json_file_record(
    path: Path,
    *,
    label: str,
    maximum: int = 16 * 1024 * 1024,
) -> tuple[dict[str, object], dict[str, object]]:
    record, payload = _regular_file_record(
        path,
        label=label,
        maximum=maximum,
        allowed_modes={0o600},
    )
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise BootstrapError(f"{label} is not a JSON object")
    return record, value


def _literal_environment_values(
    path: Path, *, label: str, names: set[str]
) -> tuple[dict[str, object], dict[str, str]]:
    record, payload = _regular_file_record(
        path,
        label=label,
        maximum=1024 * 1024,
        allowed_modes={0o600},
    )
    values: dict[str, str] = {}
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise BootstrapError(f"{label} is not UTF-8") from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise BootstrapError(f"{label} contains a malformed assignment")
        name, value = line.split("=", 1)
        if name in values:
            raise BootstrapError(f"{label} contains a duplicate assignment")
        if name in names:
            values[name] = value
    if set(values) != names:
        raise BootstrapError(f"{label} lacks required adoption values")
    return record, values


ADOPTED_DFT_PROCESS_ENVIRONMENT = {
    "MONOMER_DFT_GPU_BROKER_ENABLED": "0",
    "MONOMER_DFT_GPU_GUARD_STATE": str(RUNTIME_ROOT / "state/gpu2-guard.json"),
    "MONOMER_DFT_MAX_CONCURRENT_JOBS": "1",
    "MONOMER_DFT_MAX_QUEUED_JOBS": "8",
    "NEXPOLY_DFT_GPU_DEVICE": "2",
    "NEXPOLY_DFT_OVERFLOW_GPU_DEVICES": "",
}


def _bounded_process_environment(pid: int) -> dict[str, str]:
    path = Path(f"/proc/{pid}/environ")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
    except OSError as exc:
        raise BootstrapError("monomer DFT process environment is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid():
            raise BootstrapError("monomer DFT process environment is unsafe")
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > 1024 * 1024:
                raise BootstrapError("monomer DFT process environment is too large")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
    ):
        raise BootstrapError("monomer DFT process changed during environment read")
    values: dict[str, str] = {}
    try:
        entries = bytes(payload).split(b"\0")
        for raw in entries:
            if not raw:
                continue
            name, separator, value = raw.partition(b"=")
            if not separator:
                raise BootstrapError("monomer DFT process environment is malformed")
            decoded_name = name.decode("utf-8")
            decoded_value = value.decode("utf-8")
            if decoded_name in values:
                raise BootstrapError(
                    "monomer DFT process environment contains a duplicate"
                )
            values[decoded_name] = decoded_value
    except UnicodeError as exc:
        raise BootstrapError("monomer DFT process environment is not UTF-8") from exc
    return values


def _assert_adopted_dft_unit_semantics(
    payload: bytes,
    *,
    main_pid: int,
    allow_test: bool,
) -> None:
    try:
        lines = [
            raw.strip()
            for raw in payload.decode("utf-8").splitlines()
            if raw.strip() and not raw.lstrip().startswith(("#", ";"))
        ]
    except UnicodeError as exc:
        raise BootstrapError("monomer DFT unit is not UTF-8") from exc
    required_lines = {
        f"EnvironmentFile={RUNTIME_ROOT}/config/monomer-dft-runtime.env",
        'Environment="MONOMER_DFT_GPU_GUARD_STATE='
        f'{RUNTIME_ROOT}/state/gpu2-guard.json"',
        'Environment="NEXPOLY_DFT_GPU_DEVICE=2"',
        'Environment="NEXPOLY_DFT_OVERFLOW_GPU_DEVICES="',
        'Environment="MONOMER_DFT_GPU_BROKER_ENABLED=0"',
        'Environment="MONOMER_DFT_MAX_CONCURRENT_JOBS=1"',
        'Environment="MONOMER_DFT_MAX_QUEUED_JOBS=8"',
        f"ExecStartPre=/usr/bin/python3 -I -B {PRODUCTION_ROOT}/scripts/"
        "gpu2_guard.py --require-ready",
        f"ExecStart={PRODUCTION_ROOT}/workers/monomer_dft_worker/"
        "run_host_worker.sh",
    }
    if any(lines.count(value) != 1 for value in required_lines):
        raise BootstrapError("monomer DFT unit lacks the exact legacy GPU2 contract")
    guard_assignments = [
        value
        for value in lines
        if "NEXPOLY_DFT_GPU_GUARD_MODE=" in value
    ]
    if guard_assignments not in (
        [],
        ['Environment="NEXPOLY_DFT_GPU_GUARD_MODE=enforce"'],
    ):
        raise BootstrapError("monomer DFT unit is not fail-closed enforce mode")
    if allow_test:
        environment = dict(ADOPTED_DFT_PROCESS_ENVIRONMENT)
    else:
        environment = _bounded_process_environment(main_pid)
    if any(
        environment.get(name) != expected
        for name, expected in ADOPTED_DFT_PROCESS_ENVIRONMENT.items()
    ):
        raise BootstrapError("monomer DFT process does not use the legacy GPU2 contract")
    guard_mode = environment.get("NEXPOLY_DFT_GPU_GUARD_MODE")
    if guard_mode not in {None, "enforce"}:
        raise BootstrapError("monomer DFT process is not fail-closed enforce mode")
    cuda_visible = environment.get("CUDA_VISIBLE_DEVICES")
    if cuda_visible not in {None, "2"}:
        raise BootstrapError("monomer DFT process CUDA visibility differs from GPU2")


def _adoption_systemd_identity(
    path: Path,
    *,
    unit_name: str,
    expected_sha256: str | None,
    allow_test: bool,
    require_dft_semantics: bool = False,
) -> dict[str, object]:
    record, payload = _regular_file_record(
        path,
        label=f"{unit_name} unit",
        maximum=1024 * 1024,
        allowed_modes={0o600, 0o664},
    )
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_sha256) is None
        or record["sha256"] != expected_sha256
    ):
        raise BootstrapError(f"{unit_name} unit differs from explicit confirmation")
    if allow_test:
        fields = {
            "LoadState": "loaded",
            "FragmentPath": str(path),
            "DropInPaths": "",
            "NeedDaemonReload": "no",
            "UnitFileState": "enabled",
            "ActiveState": "active",
            "SubState": "running",
            "MainPID": "1001",
            "InvocationID": "1" * 32,
        }
    else:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                unit_name,
                "--property=LoadState",
                "--property=FragmentPath",
                "--property=DropInPaths",
                "--property=NeedDaemonReload",
                "--property=UnitFileState",
                "--property=ActiveState",
                "--property=SubState",
                "--property=MainPID",
                "--property=InvocationID",
            ],
            env=_systemd_environment(allow_test=False),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        fields = dict(
            line.split("=", 1)
            for line in result.stdout.splitlines()
            if "=" in line
        )
    required = {
        "LoadState",
        "FragmentPath",
        "DropInPaths",
        "NeedDaemonReload",
        "UnitFileState",
        "ActiveState",
        "SubState",
        "MainPID",
        "InvocationID",
    }
    try:
        main_pid = int(fields.get("MainPID", ""))
    except ValueError as exc:
        raise BootstrapError(f"{unit_name} process identity is malformed") from exc
    invocation = fields.get("InvocationID")
    if (
        set(fields) != required
        or fields["LoadState"] != "loaded"
        or fields["FragmentPath"] != str(path)
        or fields["DropInPaths"] != ""
        or fields["NeedDaemonReload"] != "no"
        or fields["UnitFileState"] not in {"enabled", "static"}
        or fields["ActiveState"] != "active"
        or fields["SubState"] != "running"
        or main_pid <= 0
        or not isinstance(invocation, str)
        or re.fullmatch(r"[0-9a-f]{32}", invocation) is None
    ):
        raise BootstrapError(f"{unit_name} is not the active unchanged instance")
    if require_dft_semantics:
        _assert_adopted_dft_unit_semantics(
            payload,
            main_pid=main_pid,
            allow_test=allow_test,
        )
    return {
        **record,
        "systemd_state": {
            key: fields[key]
            for key in (
                "LoadState",
                "FragmentPath",
                "DropInPaths",
                "NeedDaemonReload",
                "UnitFileState",
                "ActiveState",
                "SubState",
            )
        },
        "process_identity": {
            "main_pid": main_pid,
            "invocation_id": invocation,
        },
    }


def _adoption_worker_health(
    runtime_root: Path, *, worker: str, allow_test: bool, live_sha: str
) -> dict[str, object]:
    if allow_test:
        return {
            "status": "ok",
            "runtime_ready": True,
            "active_jobs": 0,
            "queued_jobs": 0,
            "worker_instance_id": f"test-{worker}",
            "release_sha": live_sha,
            "gpu_guard_status": "ready" if worker == "monomer-dft" else None,
            "gpu_contention_observed": False,
            "degradation_reason": None,
        }
    socket_path = (
        runtime_root / f"state/{worker}-worker-socket/worker.sock"
    )
    try:
        parent = socket_path.parent.lstat()
        metadata = socket_path.lstat()
    except OSError as exc:
        raise BootstrapError(f"{worker} Worker socket is unavailable") from exc
    if (
        not stat.S_ISDIR(parent.st_mode)
        or socket_path.parent.is_symlink()
        or parent.st_uid != os.geteuid()
        or parent.st_mode & 0o077
        or not stat.S_ISSOCK(metadata.st_mode)
        or socket_path.is_symlink()
        or metadata.st_uid != os.geteuid()
    ):
        raise BootstrapError(f"{worker} Worker socket is unsafe")
    result = subprocess.run(
        [
            "/usr/bin/curl",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "10",
            "--unix-socket",
            str(socket_path),
            "http://worker/health",
        ],
        env={"PATH": "/usr/bin:/bin", "HOME": "/home/devuser"},
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"{worker} Worker health is invalid JSON") from exc
    active = document.get("active_jobs") if isinstance(document, dict) else None
    queued = (
        document.get("queued_jobs", 0) if isinstance(document, dict) else None
    )
    instance = (
        document.get("worker_instance_id") if isinstance(document, dict) else None
    )
    if (
        not isinstance(document, dict)
        or not isinstance(active, int)
        or isinstance(active, bool)
        or active != 0
        or not isinstance(queued, int)
        or isinstance(queued, bool)
        or queued != 0
        or not isinstance(instance, str)
        or not instance
    ):
        raise BootstrapError(f"{worker} Worker is not an idle adoption baseline")
    observed_release = document.get("release_sha", document.get("source_sha"))
    if observed_release != live_sha:
        raise BootstrapError(f"{worker} Worker source differs from live checkout")
    runtime = document.get("runtime")
    if worker == "monomer-md":
        if document.get("status") != "ok" or document.get("runtime_ready") is not True:
            raise BootstrapError("monomer-md Worker is not runtime ready")
        degradation_reason = None
        guard_status = None
        contention_observed = False
    else:
        runtime_guard_status = (
            runtime.get("guard_status") if isinstance(runtime, dict) else None
        )
        guard_status = document.get("gpu_guard_status", runtime_guard_status)
        contention_observed = (
            document.get("gpu_contention_observed") is True
            or (
                "gpu_contention_observed" not in document
                and guard_status == "quarantined"
            )
        )
        if document.get("status") == "ok":
            if document.get("runtime_ready") is not True:
                raise BootstrapError("monomer-dft Worker readiness is inconsistent")
            degradation_reason = None
        elif (
            document.get("status") == "degraded"
            and document.get("runtime_ready") is False
            and document.get("accepting_jobs") is False
            and document.get("gpu_guard_mode", "enforce") == "enforce"
            and guard_status == "quarantined"
            and contention_observed
            and isinstance(runtime, dict)
            and runtime.get("fatal") is False
            and runtime.get("fatal_reason") is None
            and runtime.get("guard_status") == "quarantined"
        ):
            degradation_reason = "gpu-guard-quarantined"
        else:
            raise BootstrapError(
                "monomer-dft Worker degradation is not the admitted GPU guard quarantine"
            )
    return {
        "status": document.get("status"),
        "runtime_ready": document.get("runtime_ready") is True,
        "active_jobs": active,
        "queued_jobs": queued,
        "worker_instance_id": instance,
        "release_sha": observed_release,
        "gpu_guard_status": guard_status,
        "gpu_contention_observed": contention_observed,
        "degradation_reason": degradation_reason,
    }


def _adoption_asset_identity(
    runtime_root: Path, *, allow_test: bool
) -> dict[str, object]:
    pointer = runtime_root / "state/current-assets"
    if allow_test and not (pointer.exists() or pointer.is_symlink()):
        return {
            "pointer": str(pointer),
            "root": str(runtime_root.parent / ("a" * 64)),
            "manifest_sha256": "sha256:" + "a" * 64,
        }
    try:
        metadata = pointer.lstat()
        raw_target = Path(os.readlink(pointer))
    except OSError as exc:
        raise BootstrapError("active asset pointer is unavailable") from exc
    if (
        not stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
    ):
        raise BootstrapError("active asset pointer is unsafe")
    target = raw_target if raw_target.is_absolute() else pointer.parent / raw_target
    target = target.absolute()
    try:
        target_metadata = target.lstat()
    except OSError as exc:
        raise BootstrapError("active asset release is unavailable") from exc
    if (
        not stat.S_ISDIR(target_metadata.st_mode)
        or target.is_symlink()
        or target_metadata.st_uid != os.geteuid()
        or target_metadata.st_mode & 0o022
    ):
        raise BootstrapError("active asset release is unsafe")
    manifest_record, _payload = _regular_file_record(
        target / "ASSET-MANIFEST.json",
        label="active asset manifest",
        maximum=16 * 1024 * 1024,
        allowed_modes={0o600, 0o400, 0o644},
    )
    if not allow_test and target.name != str(manifest_record["sha256"]).removeprefix(
        "sha256:"
    ):
        raise BootstrapError("active asset release differs from its manifest")
    return {
        "pointer": str(pointer),
        "root": str(target),
        "manifest_sha256": manifest_record["sha256"],
    }


def _adoption_dft_projection(
    production_root: Path,
    runtime_root: Path,
    *,
    live_sha: str,
    live_tree: str,
    unit: dict[str, object],
    allow_test: bool,
) -> dict[str, object]:
    env_path = runtime_root / "config/monomer-dft-runtime.env"
    names = {
        "MONOMER_DFT_RELEASE_SHA",
        "MONOMER_DFT_RUNTIME_CONTRACT_SHA256",
        "MONOMER_DFT_PYTHON",
        "AIMNET_CACHE_DIR",
        "WARP_CACHE_PATH",
    }
    if allow_test and not env_path.exists():
        release_root = runtime_root / "worker-venvs/dft" / live_sha
        model_names = (
            "aimnet2-pd_0.pt",
            "aimnet2_2025_b973c_d3_0.pt",
            "aimnet2_b973c_d3_0.pt",
            "aimnet2_rxn_0.pt",
            "aimnet2_wb97m_d3_0.pt",
            "aimnet2nse_wb97m_0.pt",
        )
        return {
            "runtime": {
                "root": str(release_root),
                "runtime_manifest_path": str(release_root / "runtime.json"),
                "runtime_manifest_sha256": "sha256:" + "2" * 64,
                "release_sha": live_sha,
                "source_tree": live_tree,
                "python": str(release_root / "venv/bin/python"),
                "requirements_lock_sha256": "sha256:" + "3" * 64,
                "aimnet_source_lock_sha256": "sha256:" + "4" * 64,
                "runtime_inventory_sha256": "sha256:" + "1" * 64,
                "models": {
                    name: "sha256:" + format(index + 5, "x") * 64
                    for index, name in enumerate(model_names)
                },
            },
            "runtime_env": {
                "path": str(env_path),
                "sha256": "sha256:" + "b" * 64,
                "values": {
                    "MONOMER_DFT_RELEASE_SHA": live_sha,
                    "MONOMER_DFT_RUNTIME_CONTRACT_SHA256": "sha256:" + "5" * 64,
                    "MONOMER_DFT_PYTHON": str(release_root / "venv/bin/python"),
                    "AIMNET_CACHE_DIR": str(release_root / "aimnet-cache"),
                    "WARP_CACHE_PATH": str(release_root / "warp-cache"),
                },
            },
            "systemd_unit": {
                "target_path": unit["path"],
                "sha256": unit["sha256"],
                "systemd_state": unit["systemd_state"],
                "process_identity": unit["process_identity"],
                "control_release_id": None,
                "launcher_path": str(
                    production_root
                    / "workers/monomer_dft_worker/run_host_worker.sh"
                ),
                "launcher_sha256": "sha256:" + "c" * 64,
            },
            "gpu": {
                "index": "2",
                "uuid": ADOPTED_DFT_GPU_UUID,
                "guard_mode": "enforce",
                "guard_state_path": str(runtime_root / "state/gpu2-guard.json"),
                "guard_schema_version": 1,
                "guard_status": "ready",
                "contention_observed": False,
            },
        }
    env_record, values = _literal_environment_values(
        env_path,
        label="monomer DFT runtime environment",
        names=names,
    )
    if (
        values["MONOMER_DFT_RELEASE_SHA"] != live_sha
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            values["MONOMER_DFT_RUNTIME_CONTRACT_SHA256"],
        )
        is None
    ):
        raise BootstrapError("monomer DFT runtime environment source differs")
    python = Path(values["MONOMER_DFT_PYTHON"])
    model_root = Path(values["AIMNET_CACHE_DIR"])
    warp_root = Path(values["WARP_CACHE_PATH"])
    if (
        not python.is_absolute()
        or not model_root.is_absolute()
        or not warp_root.is_absolute()
    ):
        raise BootstrapError("monomer DFT runtime paths are not absolute")
    release_root = model_root.parent
    if (
        release_root
        != runtime_root / "worker-venvs/dft" / live_sha
        or python != release_root / "venv/bin/python"
        or warp_root != release_root / "warp-cache"
    ):
        raise BootstrapError("monomer DFT runtime paths leave the sealed release")
    manifest_record, runtime_manifest = _json_file_record(
        release_root / "runtime.json",
        label="monomer DFT runtime manifest",
    )
    if (
        runtime_manifest.get("schema_version") != 1
        or runtime_manifest.get("release") != live_sha
        or runtime_manifest.get("source_tree") != live_tree
    ):
        raise BootstrapError("monomer DFT runtime manifest source differs")
    requirements = runtime_manifest.get("requirements_lock_sha256")
    aimnet_source = runtime_manifest.get("aimnet_source_lock_sha256")
    if (
        not isinstance(requirements, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", requirements) is None
        or not isinstance(aimnet_source, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", aimnet_source) is None
    ):
        raise BootstrapError("monomer DFT runtime lock identity is invalid")
    checkpoint_names = (
        "aimnet2-pd_0.pt",
        "aimnet2_2025_b973c_d3_0.pt",
        "aimnet2_b973c_d3_0.pt",
        "aimnet2_rxn_0.pt",
        "aimnet2_wb97m_d3_0.pt",
        "aimnet2nse_wb97m_0.pt",
    )
    models: dict[str, str] = {}
    for name in checkpoint_names:
        record, _payload = _regular_file_record(
            model_root / name,
            label=f"monomer DFT checkpoint {name}",
            maximum=2 * 1024 * 1024 * 1024,
            allowed_modes={0o600, 0o400, 0o644},
        )
        models[name] = str(record["sha256"])
    guard_path = runtime_root / "state/gpu2-guard.json"
    if allow_test and not guard_path.exists():
        guard = {
            "schema_version": 1,
            "gpu_index": "2",
            "gpu_uuid": ADOPTED_DFT_GPU_UUID,
            "status": "ready",
        }
    else:
        _guard_record, guard = _json_file_record(
            guard_path,
            label="GPU2 guard observation",
        )
    gpu_uuid = guard.get("gpu_uuid")
    guard_status = guard.get("status")
    if (
        guard.get("schema_version") != 1
        or guard.get("gpu_index") != "2"
        or gpu_uuid != ADOPTED_DFT_GPU_UUID
        or guard_status not in {"ready", "quarantined"}
    ):
        raise BootstrapError("GPU2 guard identity is invalid")
    if not allow_test:
        observed_at = guard.get("observed_at")
        try:
            observed = dt.datetime.fromisoformat(
                str(observed_at).replace("Z", "+00:00")
            )
            age = (dt.datetime.now(dt.timezone.utc) - observed).total_seconds()
        except (TypeError, ValueError) as exc:
            raise BootstrapError("GPU2 guard observation timestamp is invalid") from exc
        unknown = guard.get("unknown_processes")
        if (
            observed.tzinfo is None
            or not -5 <= age <= 150
            or not isinstance(unknown, list)
            or (guard_status == "ready") != (len(unknown) == 0)
        ):
            raise BootstrapError("GPU2 guard observation is stale or inconsistent")
    launcher_record, _payload = _regular_file_record(
        production_root / "workers/monomer_dft_worker/run_host_worker.sh",
        label="monomer DFT live launcher",
        maximum=1024 * 1024,
        allowed_modes={0o700, 0o755},
    )
    runtime_inventory_sha256 = _adopted_dft_runtime_inventory(release_root)
    return {
        "runtime": {
            "root": str(release_root),
            "runtime_manifest_path": str(release_root / "runtime.json"),
            "runtime_manifest_sha256": manifest_record["sha256"],
            "release_sha": live_sha,
            "source_tree": live_tree,
            "python": str(python),
            "requirements_lock_sha256": requirements,
            "aimnet_source_lock_sha256": aimnet_source,
            "runtime_inventory_sha256": runtime_inventory_sha256,
            "models": models,
        },
        "runtime_env": {
            "path": str(env_path),
            "sha256": env_record["sha256"],
            "values": values,
        },
        "systemd_unit": {
            "target_path": unit["path"],
            "sha256": unit["sha256"],
            "systemd_state": unit["systemd_state"],
            "process_identity": unit["process_identity"],
            "control_release_id": None,
            "launcher_path": str(
                production_root
                / "workers/monomer_dft_worker/run_host_worker.sh"
            ),
            "launcher_sha256": launcher_record["sha256"],
        },
        "gpu": {
            "index": "2",
            "uuid": gpu_uuid,
            "guard_mode": "enforce",
            "guard_state_path": str(guard_path),
            "guard_schema_version": 1,
            "guard_status": guard_status,
            "contention_observed": guard_status == "quarantined",
        },
    }


def _adoption_migration_records(
    *, source_sha: str, allow_test: bool
) -> list[dict[str, object]]:
    payload = _read_reviewed_source(
        "backend/migrations/postgres/manifest.json",
        source_sha=source_sha,
        allow_test=allow_test,
    )
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BootstrapError("reviewed migration manifest is invalid") from exc
    migrations = document.get("migrations") if isinstance(document, dict) else None
    if not isinstance(migrations, list):
        raise BootstrapError("reviewed migration manifest lacks migrations")
    records: list[dict[str, object]] = []
    for value in migrations:
        if not isinstance(value, dict):
            raise BootstrapError("reviewed migration record is invalid")
        records.append(dict(value))
        if value.get("version") == "0013_monomer_dft_jobs":
            break
    if (
        len(records) != 13
        or records[-1].get("version") != "0013_monomer_dft_jobs"
        or any(
            not isinstance(value.get("checksum"), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(value["checksum"])) is None
            for value in records
        )
    ):
        raise BootstrapError("reviewed post-0013 migration authority is invalid")
    return records


def _adoption_container_and_database_evidence(
    runtime_root: Path,
    *,
    source_sha: str,
    source_tree: str,
    migrations: list[dict[str, object]],
    allow_test: bool,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    deploy_env = runtime_root / "config/deploy.env"
    if allow_test:
        env_record = _adoption_optional_test_file_record(
            deploy_env,
            label="production deploy environment",
            allow_test=True,
        )
        images = {
            "backend": {
                "digest_ref": "example.invalid/backend@sha256:" + "1" * 64,
                "container_id": "1" * 64,
                "image_id": "sha256:" + "2" * 64,
                "started_at": "2026-01-01T00:00:00Z",
                "restart_count": 0,
            },
            "web": {
                "digest_ref": "example.invalid/web@sha256:" + "3" * 64,
                "container_id": "3" * 64,
                "image_id": "sha256:" + "4" * 64,
                "started_at": "2026-01-01T00:00:00Z",
                "restart_count": 0,
            },
        }
        postgres = {
            "container_id": "5" * 64,
            "image_id": "sha256:" + "6" * 64,
            "configured_image": "postgres:16-alpine",
            "system_identifier": "7659245354718314530",
        }
        database = {
            "system_identifier": postgres["system_identifier"],
            "ledger": [
                {"version": value["version"], "checksum": value["checksum"]}
                for value in migrations
            ],
            "postgres_major": 16,
        }
        return env_record, images, {"runtime": postgres, **database}
    env_record, values = _literal_environment_values(
        deploy_env,
        label="production deploy environment",
        names={
            "NEXPOLY_BACKEND_IMAGE",
            "NEXPOLY_WEB_IMAGE",
            "NEXPOLY_POSTGRES_USER",
            "NEXPOLY_POSTGRES_DB",
        },
    )
    environment = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/home/devuser",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    containers: dict[str, dict[str, object]] = {}
    raw_inspect: dict[str, dict[str, object]] = {}
    for service, output_name in (
        ("backend", "backend"),
        ("nginx", "web"),
        ("lab-postgres", "postgres"),
    ):
        listed = subprocess.run(
            [
                "docker",
                "ps",
                "--no-trunc",
                "--filter",
                "label=com.docker.compose.project=nexpoly",
                "--filter",
                f"label=com.docker.compose.service={service}",
                "--format",
                "{{.ID}}",
            ],
            env=environment,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        ids = [value for value in listed.stdout.splitlines() if value]
        if len(ids) != 1:
            raise BootstrapError(f"production {service} container identity is ambiguous")
        inspected = subprocess.run(
            ["docker", "container", "inspect", ids[0]],
            env=environment,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            container = json.loads(inspected.stdout)[0]
            state = container["State"]
            config = container["Config"]
            labels = config["Labels"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise BootstrapError(f"production {service} container evidence is invalid") from exc
        if (
            container.get("Id") != ids[0]
            or state.get("Running") is not True
            or labels.get("com.docker.compose.project") != "nexpoly"
            or labels.get("com.docker.compose.service") != service
            or not isinstance(container.get("Image"), str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", container["Image"]) is None
            or not isinstance(container.get("RestartCount"), int)
            or isinstance(container.get("RestartCount"), bool)
            or container["RestartCount"] < 0
        ):
            raise BootstrapError(f"production {service} container identity differs")
        if output_name in {"backend", "web"} and (
            labels.get("org.opencontainers.image.revision") != source_sha
            or (
                output_name == "backend"
                and labels.get("com.nexpoly.source.tree") != source_tree
            )
        ):
            raise BootstrapError(f"production {service} image source differs")
        containers[output_name] = {
            "container_id": container["Id"],
            "image_id": container["Image"],
            "started_at": state.get("StartedAt"),
            "restart_count": container["RestartCount"],
        }
        raw_inspect[output_name] = container
    image_evidence: dict[str, object] = {}
    for output_name, variable in (
        ("backend", "NEXPOLY_BACKEND_IMAGE"),
        ("web", "NEXPOLY_WEB_IMAGE"),
    ):
        digest_ref = values[variable]
        if re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", digest_ref) is None:
            raise BootstrapError(f"production {output_name} image is not digest-pinned")
        configured = raw_inspect[output_name].get("Config")
        if not isinstance(configured, dict) or configured.get("Image") != digest_ref:
            raise BootstrapError(f"production {output_name} configured image differs")
        image_evidence[output_name] = {
            "digest_ref": digest_ref,
            **containers[output_name],
        }
    postgres_container = raw_inspect["postgres"]
    postgres_config = postgres_container.get("Config")
    configured_postgres = (
        postgres_config.get("Image") if isinstance(postgres_config, dict) else None
    )
    query = (
        "SELECT json_build_object("
        "'system_identifier',(SELECT system_identifier::text FROM pg_control_system()),"
        "'server_version_num',current_setting('server_version_num'),"
        "'ledger',(SELECT COALESCE(json_agg(json_build_object('version',version,"
        "'checksum',checksum) ORDER BY version),'[]'::json) FROM governance.schema_migrations)"
        ")::text"
    )
    result = subprocess.run(
        [
            "docker",
            "exec",
            str(postgres_container["Id"]),
            "psql",
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "--set",
            "ON_ERROR_STOP=1",
            "--username",
            values["NEXPOLY_POSTGRES_USER"],
            "--dbname",
            values["NEXPOLY_POSTGRES_DB"],
            "--command",
            query,
        ],
        env=environment,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        database = json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        raise BootstrapError("production database adoption evidence is invalid") from exc
    expected_ledger = [
        {"version": value["version"], "checksum": value["checksum"]}
        for value in migrations
    ]
    if (
        not isinstance(database, dict)
        or not isinstance(database.get("system_identifier"), str)
        or not database["system_identifier"].isdigit()
        or not str(database.get("server_version_num", "")).startswith("16")
        or database.get("ledger") != expected_ledger
    ):
        raise BootstrapError("production database is not the exact post-0013 baseline")
    postgres_evidence = {
        "runtime": {
            **containers["postgres"],
            "configured_image": configured_postgres,
        },
        "system_identifier": database["system_identifier"],
        "postgres_major": 16,
        "ledger": expected_ledger,
    }
    return env_record, image_evidence, postgres_evidence


def _adoption_manual_evidence(
    runtime_root: Path, *, allow_test: bool
) -> dict[str, object]:
    paths = {
        "manual_deployment_report": runtime_root
        / "manual-operations/manual-deploy-20260727t095913z/DEPLOYMENT-REPORT.md",
        "post_0013_database": runtime_root
        / "manual-operations/manual-deploy-20260727t095913z/evidence/database-post-0013.txt",
        "isolated_restore": runtime_root
        / "manual-operations/manual-deploy-20260727t095913z/evidence/isolated-restore.txt",
        "formal_dft_release": runtime_root
        / "manual-operations/formal-dft-20260729t012508z-8d6a580/release-completion-report.json",
    }
    if allow_test and not all(path.exists() for path in paths.values()):
        return {
            name: {
                "path": str(path),
                "sha256": "sha256:" + format(index + 1, "x") * 64,
                "size": 1,
                "mode": "0600",
            }
            for index, (name, path) in enumerate(paths.items())
        }
    records: dict[str, object] = {}
    for name, path in paths.items():
        record, _payload = _regular_file_record(
            path,
            label=name.replace("_", " "),
            maximum=64 * 1024 * 1024,
            allowed_modes={0o600},
        )
        records[name] = record
    return records


def _adoption_optional_test_file_record(
    path: Path, *, label: str, allow_test: bool
) -> dict[str, object]:
    if allow_test and not path.exists():
        return {
            "path": str(path),
            "sha256": "sha256:" + "e" * 64,
            "size": 1,
            "mode": "0600",
        }
    record, _payload = _regular_file_record(
        path,
        label=label,
        maximum=16 * 1024 * 1024,
        allowed_modes={0o600},
    )
    return record


def _collect_adoption_evidence(
    production_root: Path,
    runtime_root: Path,
    *,
    operation_id: str,
    bootstrap_source_sha: str,
    bootstrap_source_tree: str,
    live_sha: str,
    expected_md_unit_sha256: str | None,
    expected_dft_unit_sha256: str | None,
    allow_test: bool,
) -> dict[str, object]:
    operation_id = _require_adoption_operation_id(operation_id)
    if SHA_RE.fullmatch(live_sha) is None:
        raise BootstrapError("adopted live SHA is invalid")
    repository = _production_repository_identity(
        production_root,
        bootstrap_source_sha,
        allow_test=allow_test,
    )
    live_tree = repository.get("tree")
    if repository.get("head") != live_sha or not isinstance(live_tree, str):
        raise BootstrapError("production checkout differs from the requested live SHA")
    md_unit = _adoption_systemd_identity(
        _worker_unit_path(production_root, allow_test=allow_test),
        unit_name="nexpoly-monomer-md-worker.service",
        expected_sha256=expected_md_unit_sha256,
        allow_test=allow_test,
    )
    dft_unit = _adoption_systemd_identity(
        _dft_worker_unit_path(production_root, allow_test=allow_test),
        unit_name="nexpoly-monomer-dft-worker.service",
        expected_sha256=expected_dft_unit_sha256,
        allow_test=allow_test,
        require_dft_semantics=True,
    )
    active_slot_path = runtime_root / "state/monomer-md-active-slot.json"
    if allow_test and not active_slot_path.exists():
        active_slot_record = {
            "path": str(active_slot_path),
            "sha256": "sha256:" + "6" * 64,
            "size": 1,
            "mode": "0600",
        }
        slot_record_path = runtime_root / "state/worker-slots/md-a.json"
        slot_file_record = {
            "path": str(slot_record_path),
            "sha256": "sha256:" + "8" * 64,
            "size": 1,
            "mode": "0600",
        }
        slot_record = {
            "schema_version": 2,
            "status": "ready",
            "component": "monomer-md",
            "slot": "a",
            "source_sha": live_sha,
            "source_tree": live_tree,
        }
        slot_record_identity_sha256 = _canonical_json_digest(slot_record)
        active_slot = {
            "schema_version": 1,
            "component": "monomer-md",
            "slot": "a",
            "source_sha": live_sha,
            "source_tree": live_tree,
            "worker_lock_sha256": "sha256:" + "7" * 64,
            "slot_record_sha256": slot_record_identity_sha256,
            "operation_id": "manual-md-test-adoption",
            "activated_at": "2026-01-01T00:00:00Z",
        }
    else:
        active_slot_record, active_slot = _json_file_record(
            active_slot_path,
            label="active monomer MD slot",
        )
        slot = active_slot.get("slot")
        if (
            active_slot.get("schema_version") != 1
            or active_slot.get("component") != "monomer-md"
            or slot not in {"a", "b"}
            or active_slot.get("source_sha") != live_sha
            or active_slot.get("source_tree") != live_tree
        ):
            raise BootstrapError("active monomer MD slot differs from live source")
        slot_record_path = runtime_root / f"state/worker-slots/md-{slot}.json"
        slot_file_record, slot_record = _json_file_record(
            slot_record_path,
            label="active monomer MD slot record",
        )
        # The active pointer binds the semantic worker-slot record, while the
        # adoption evidence below independently seals its exact file bytes.
        # ``_atomic_json`` appends a newline, so these digests intentionally
        # differ for the production record.
        slot_record_identity_sha256 = _canonical_json_digest(slot_record)
        if (
            slot_record.get("schema_version") != 2
            or slot_record.get("status") != "ready"
            or slot_record.get("component") != "monomer-md"
            or slot_record.get("slot") != slot
            or slot_record.get("source_sha") != live_sha
            or slot_record.get("source_tree") != live_tree
            or active_slot.get("slot_record_sha256")
            != slot_record_identity_sha256
        ):
            raise BootstrapError("active monomer MD slot record differs")
    worker_env = _adoption_optional_test_file_record(
        runtime_root / "config/worker.env",
        label="monomer MD Worker environment",
        allow_test=allow_test,
    )
    md_launcher_path = production_root / "workers/monomer_md_worker/run_host_worker.sh"
    if allow_test and not md_launcher_path.exists():
        md_launcher_sha256 = "sha256:" + "9" * 64
    else:
        launcher_record, _payload = _regular_file_record(
            md_launcher_path,
            label="monomer MD live launcher",
            maximum=1024 * 1024,
            allowed_modes={0o700, 0o755},
        )
        md_launcher_sha256 = str(launcher_record["sha256"])
    md_health = _adoption_worker_health(
        runtime_root,
        worker="monomer-md",
        allow_test=allow_test,
        live_sha=live_sha,
    )
    dft_health = _adoption_worker_health(
        runtime_root,
        worker="monomer-dft",
        allow_test=allow_test,
        live_sha=live_sha,
    )
    monomer_dft = _adoption_dft_projection(
        production_root,
        runtime_root,
        live_sha=live_sha,
        live_tree=live_tree,
        unit=dft_unit,
        allow_test=allow_test,
    )
    gpu = monomer_dft["gpu"]
    if (
        not isinstance(gpu, dict)
        or dft_health.get("gpu_guard_status") != gpu.get("guard_status")
        or dft_health.get("gpu_contention_observed")
        != gpu.get("contention_observed")
    ):
        raise BootstrapError("monomer DFT health differs from fresh GPU guard evidence")
    migrations = _adoption_migration_records(
        source_sha=bootstrap_source_sha,
        allow_test=allow_test,
    )
    deploy_env, images, database = _adoption_container_and_database_evidence(
        runtime_root,
        source_sha=live_sha,
        source_tree=live_tree,
        migrations=migrations,
        allow_test=allow_test,
    )
    app_env = _adoption_optional_test_file_record(
        runtime_root / "config/app.env",
        label="production application environment",
        allow_test=allow_test,
    )
    asset = _adoption_asset_identity(runtime_root, allow_test=allow_test)
    manual = _adoption_manual_evidence(runtime_root, allow_test=allow_test)
    ledger = database.get("ledger")
    if not isinstance(ledger, list):
        raise BootstrapError("adopted database ledger is invalid")
    return {
        "schema_version": 1,
        "authority_kind": ADOPTION_AUTHORITY_KIND,
        "operation_id": operation_id,
        "bootstrap_source": {
            "sha": bootstrap_source_sha,
            "tree": bootstrap_source_tree,
        },
        "live_repository": repository,
        "production_config": {
            "deploy_env": deploy_env,
            "app_env": app_env,
        },
        "images": images,
        "database": database,
        "asset_identity": asset,
        "migrations": migrations,
        "maintenance": {
            "kind": "adopted-maintenance-provenance",
            "alias_0005": "completed-without-formal-marker",
            "contract_0012": "completed-without-formal-marker",
            "ledger_endpoint": "post-0013",
            "ledger_sha256": _canonical_json_digest(ledger),
            "manual_evidence": manual,
        },
        "monomer_md": {
            "active_slot_path": str(active_slot_path),
            "active_slot_file_sha256": active_slot_record["sha256"],
            "active_slot": active_slot,
            "slot_record_path": str(slot_record_path),
            "slot_record_file_sha256": slot_file_record["sha256"],
            "slot_record": slot_record,
            "worker_env": worker_env,
            "systemd_unit": {
                "target_path": md_unit["path"],
                "sha256": md_unit["sha256"],
                "systemd_state": md_unit["systemd_state"],
                "process_identity": md_unit["process_identity"],
                "control_release_id": None,
                "launcher_path": str(md_launcher_path),
                "launcher_sha256": md_launcher_sha256,
            },
            "health": md_health,
        },
        "monomer_dft": {**monomer_dft, "health": dft_health},
    }


def _adoption_tree_identity(
    path: Path,
    *,
    excluded_paths: set[Path] | None = None,
    linked_destinations: set[Path] | None = None,
) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BootstrapError(f"adoption-owned tree is unavailable: {path}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise BootstrapError(f"adoption-owned tree is unsafe: {path}")
    records: list[dict[str, object]] = []
    for entry in sorted(path.rglob("*")):
        if excluded_paths is not None and entry in excluded_paths:
            continue
        relative = entry.relative_to(path).as_posix()
        entry_metadata = entry.lstat()
        if entry.is_symlink() or entry_metadata.st_uid != os.geteuid():
            raise BootstrapError(f"adoption-owned tree contains an unsafe entry: {path}")
        if stat.S_ISDIR(entry_metadata.st_mode):
            kind = "directory"
            sha256 = None
        elif stat.S_ISREG(entry_metadata.st_mode):
            if entry_metadata.st_nlink != 1 and not (
                linked_destinations is not None
                and entry in linked_destinations
                and entry_metadata.st_nlink == 2
            ):
                raise BootstrapError(
                    f"adoption-owned tree contains an unsafe hard link: {path}"
                )
            kind = "file"
            sha256 = digest(entry.read_bytes())
        else:
            raise BootstrapError(f"adoption-owned tree contains a special entry: {path}")
        records.append(
            {
                "relative_path": relative,
                "kind": kind,
                "mode": format(stat.S_IMODE(entry_metadata.st_mode), "04o"),
                "sha256": sha256,
            }
        )
    return {
        "path": str(path),
        "kind": "tree",
        "identity_sha256": _canonical_json_digest(records),
    }


def _adoption_file_ownership(path: Path) -> dict[str, object]:
    record, _payload = _regular_file_record(
        path,
        label=f"adoption-owned file {path.name}",
        maximum=64 * 1024 * 1024,
        allowed_modes={0o600, 0o700},
    )
    return {
        "path": str(path),
        "kind": "file",
        "sha256": record["sha256"],
        "mode": record["mode"],
    }


def _adoption_file_plan(
    path: Path, payload: bytes, mode: int
) -> dict[str, object]:
    if mode not in {0o600, 0o700}:
        raise BootstrapError("adoption file plan mode is invalid")
    return {
        "path": str(path),
        "kind": "file",
        "sha256": digest(payload),
        "mode": format(mode, "04o"),
    }


def _adoption_install_staging_plan(
    path: Path,
    destination: Path,
    payload: bytes,
    mode: int,
    *,
    operation_id: str,
    purpose: str = "install",
) -> dict[str, object]:
    suffix = "complete.tmp" if purpose == "cas" else "tmp"
    expected = destination.parent / f".{destination.name}.{operation_id}.{suffix}"
    if (
        _require_adoption_operation_id(operation_id) != operation_id
        or purpose not in {"install", "cas"}
        or path != expected
        or mode not in {0o600, 0o700}
    ):
        raise BootstrapError("adoption install staging plan is invalid")
    return {
        "path": str(path),
        "kind": "install-staging",
        "destination": str(destination),
        "sha256": digest(payload),
        "mode": format(mode, "04o"),
        "operation_id": operation_id,
        "purpose": purpose,
        "initially_absent": True,
    }


def _adoption_install_temporary_path(
    path: Path, *, operation_id: str
) -> Path:
    operation_id = _require_adoption_operation_id(operation_id)
    return path.parent / f".{path.name}.{operation_id}.tmp"


def _validate_adoption_ownership(
    value: object, *, label: str
) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or value.get("kind")
        not in {"file", "tree", "directory", "staging-tree", "install-staging"}
        or not isinstance(value.get("path"), str)
        or not Path(value["path"]).is_absolute()
    ):
        raise BootstrapError(f"{label} ownership is invalid")
    kind = value["kind"]
    if kind == "file":
        if (
            set(value) != {"path", "kind", "sha256", "mode"}
            or not isinstance(value.get("sha256"), str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", value["sha256"]) is None
            or value.get("mode") not in {"0600", "0700"}
        ):
            raise BootstrapError(f"{label} file ownership is invalid")
    elif kind == "install-staging":
        destination = value.get("destination")
        operation_id = value.get("operation_id")
        purpose = value.get("purpose")
        suffix = "complete.tmp" if purpose == "cas" else "tmp"
        if (
            set(value)
            != {
                "path", "kind", "destination", "sha256", "mode",
                "operation_id", "purpose", "initially_absent",
            }
            or not isinstance(destination, str)
            or not Path(destination).is_absolute()
            or not isinstance(operation_id, str)
            or _require_adoption_operation_id(operation_id) != operation_id
            or purpose not in {"install", "cas"}
            or value.get("initially_absent") is not True
            or value.get("mode") not in {"0600", "0700"}
            or re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("sha256")))
            is None
            or Path(str(value["path"]))
            != Path(destination).parent
            / f".{Path(destination).name}.{operation_id}.{suffix}"
        ):
            raise BootstrapError(f"{label} install staging ownership is invalid")
    elif kind == "tree":
        if (
            set(value) != {"path", "kind", "identity_sha256"}
            or not isinstance(value.get("identity_sha256"), str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", value["identity_sha256"])
            is None
        ):
            raise BootstrapError(f"{label} tree ownership is invalid")
    elif kind == "directory":
        if (
            set(value) != {"path", "kind", "mode"}
            or value.get("mode") != "0700"
        ):
            raise BootstrapError(f"{label} directory ownership is invalid")
    else:
        files = value.get("files")
        if (
            set(value)
            != {"path", "kind", "owner_sha256", "owner", "files"}
            or not isinstance(value.get("owner_sha256"), str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", value["owner_sha256"])
            is None
            or not isinstance(value.get("owner"), dict)
            or _canonical_json_digest(value["owner"]) != value["owner_sha256"]
            or not isinstance(files, dict)
            or not files
        ):
            raise BootstrapError(f"{label} staging-tree ownership is invalid")
        for name, record in files.items():
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z0-9._-]+", name) is None
                or not isinstance(record, dict)
                or set(record) != {"sha256", "mode", "size"}
                or not isinstance(record.get("sha256"), str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", record["sha256"])
                is None
                or record.get("mode") not in {"0600", "0700"}
                or not isinstance(record.get("size"), int)
                or not 0 < record["size"] <= 64 * 1024 * 1024
            ):
                raise BootstrapError(
                    f"{label} staging-tree file authority is invalid"
                )
    return dict(value)


def _validate_adoption_staging_tree(
    path: Path,
    ownership: dict[str, object],
    *,
    allow_missing_entries: bool = False,
    allow_partial_entries: bool = False,
) -> dict[str, object]:
    expected = _validate_adoption_ownership(
        ownership, label="adoption staging-tree"
    )
    if expected["kind"] != "staging-tree" or str(path) != expected["path"]:
        raise BootstrapError("adoption staging-tree authority differs")
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise BootstrapError("adoption staging-tree root changed before abort")
    owner = expected["owner"]
    files = expected["files"]
    assert isinstance(owner, dict) and isinstance(files, dict)
    owner_payload = json.dumps(
        owner, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8") + b"\n"
    entries = {entry.name: entry for entry in path.iterdir()}
    owner_present = ".owner.json" in entries

    def safe_partial(entry: Path, *, mode: int, maximum: int) -> bool:
        metadata = entry.lstat()
        return (
            stat.S_ISREG(metadata.st_mode)
            and not entry.is_symlink()
            and metadata.st_uid == os.geteuid()
            and metadata.st_nlink == 1
            and stat.S_IMODE(metadata.st_mode) == mode
            and metadata.st_size <= maximum
        )

    if owner_present:
        owner_path = path / ".owner.json"
        try:
            owner_record, observed_owner_payload = _regular_file_record(
                owner_path,
                label="adoption staging-tree owner",
                maximum=1024 * 1024,
                allowed_modes={0o600},
            )
            owner_exact = (
                owner_record["sha256"] == digest(owner_payload)
                and observed_owner_payload == owner_payload
                and _load_private_json(owner_path) == owner
            )
        except BootstrapError:
            owner_exact = False
        if not owner_exact and not (
            allow_partial_entries
            and safe_partial(owner_path, mode=0o600, maximum=len(owner_payload))
        ):
            raise BootstrapError(
                "adoption staging-tree owner changed before abort"
            )
    for name, entry in entries.items():
        if name == ".owner.json":
            continue
        expected_record: object | None = files.get(name)
        if not isinstance(expected_record, dict):
            raise BootstrapError(
                "adoption staging-tree contains an unplanned entry"
            )
        expected_mode = int(str(expected_record["mode"]), 8)
        try:
            record, _payload = _regular_file_record(
                entry,
                label="adoption staging-tree file",
                maximum=64 * 1024 * 1024,
                allowed_modes={expected_mode},
            )
            file_exact = record["sha256"] == expected_record["sha256"]
        except BootstrapError:
            file_exact = False
        if not file_exact and not (
            allow_partial_entries
            and safe_partial(
                entry,
                mode=expected_mode,
                maximum=int(expected_record["size"]),
            )
        ):
            raise BootstrapError(
                "adoption staging-tree file changed before abort"
            )
    final_names = set(files)
    actual_final_names = set(entries) & final_names
    if not owner_present and entries and not allow_missing_entries:
        if set(entries) != final_names or actual_final_names != final_names:
            raise BootstrapError(
                "ownerless adoption staging-tree is not complete"
            )
    return expected


def _validate_adoption_transaction(
    document: dict[str, object], *, path: Path
) -> dict[str, object]:
    required = {
        "schema_version",
        "status",
        "phase",
        "operation_id",
        "identity",
        "identity_sha256",
        "created_at",
        "updated_at",
        "planned_paths",
        "created_paths",
        "step_plans",
        "step_evidence",
    }
    optional = {"aborted_at"}
    identity = document.get("identity")
    planned = document.get("planned_paths")
    created = document.get("created_paths")
    step_plans = document.get("step_plans")
    phase = document.get("phase")
    status = document.get("status")
    operation_id = document.get("operation_id")
    if (
        not isinstance(document, dict)
        or not required.issubset(document)
        or not set(document).issubset(required | optional)
        or document.get("schema_version") != ADOPTION_TRANSACTION_SCHEMA_VERSION
        or not isinstance(operation_id, str)
        or _require_adoption_operation_id(operation_id) != operation_id
        or path != _adoption_transaction_path(path.parents[2], operation_id=operation_id)
        or not isinstance(identity, dict)
        or _canonical_json_digest(identity) != document.get("identity_sha256")
        or identity.get("deploy_lock_disposition")
        != ADOPTION_DEPLOY_LOCK_DISPOSITION
        or not isinstance(identity.get("deploy_lock_created"), bool)
        or not isinstance(planned, list)
        or not isinstance(created, list)
        or not isinstance(step_plans, dict)
        or not set(step_plans).issubset({"layout", "controls", "baseline", "authority"})
        or not isinstance(document.get("step_evidence"), dict)
        or not isinstance(document.get("created_at"), str)
        or not isinstance(document.get("updated_at"), str)
    ):
        raise BootstrapError("adoption transaction has an invalid shape")
    if status in {"in-progress", "completed"}:
        if phase not in ADOPTION_PHASES or (status == "completed") != (
            phase == "completed"
        ):
            raise BootstrapError("adoption transaction phase is invalid")
        if "aborted_at" in document:
            raise BootstrapError("active adoption transaction contains abort state")
    elif status == "aborted":
        if phase != "aborted" or not isinstance(document.get("aborted_at"), str):
            raise BootstrapError("aborted adoption transaction is invalid")
    else:
        raise BootstrapError("adoption transaction status is invalid")
    planned_by_path: dict[str, dict[str, object]] = {}
    for raw in planned:
        value = _validate_adoption_ownership(raw, label="planned adoption")
        path_value = str(value["path"])
        if path_value in planned_by_path:
            raise BootstrapError("planned adoption ownership is duplicated")
        planned_by_path[path_value] = value
    created_by_path: dict[str, dict[str, object]] = {}
    for raw in created:
        value = _validate_adoption_ownership(raw, label="committed adoption")
        path_value = str(value["path"])
        if (
            path_value in created_by_path
            or planned_by_path.get(path_value) != value
        ):
            raise BootstrapError("committed adoption ownership was not planned")
        created_by_path[path_value] = value
    for name, raw_plan in step_plans.items():
        if (
            not isinstance(raw_plan, dict)
            or set(raw_plan) != {"evidence", "paths"}
            or not isinstance(raw_plan.get("paths"), list)
        ):
            raise BootstrapError(f"adoption {name} plan is invalid")
        for raw in raw_plan["paths"]:
            value = _validate_adoption_ownership(
                raw, label=f"adoption {name} plan"
            )
            if planned_by_path.get(str(value["path"])) != value:
                raise BootstrapError(f"adoption {name} plan is not durable")
    return dict(document)


def _write_adoption_transaction(
    path: Path, transaction: dict[str, object]
) -> dict[str, object]:
    value = {
        **transaction,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    _atomic_json(path, value)
    return _validate_adoption_transaction(_load_private_json(path), path=path)


def _reseal_adoption_transaction(
    path: Path, transaction: dict[str, object]
) -> dict[str, object]:
    """Durably republish an already visible adoption journal unchanged."""

    _atomic_json(path, transaction)
    sealed = _validate_adoption_transaction(_load_private_json(path), path=path)
    if sealed != transaction:
        raise BootstrapError("resealed adoption transaction differs")
    return sealed


def _record_adoption_plan(
    path: Path,
    transaction: dict[str, object],
    *,
    name: str,
    evidence: object,
    planned_paths: list[dict[str, object]],
) -> dict[str, object]:
    if name not in {"layout", "controls", "baseline", "authority"}:
        raise BootstrapError("adoption plan name is invalid")
    normalized = [
        _validate_adoption_ownership(value, label=f"adoption {name} plan")
        for value in planned_paths
    ]
    plan = {"evidence": evidence, "paths": normalized}
    step_plans = transaction.get("step_plans")
    existing_paths = transaction.get("planned_paths")
    if not isinstance(step_plans, dict) or not isinstance(existing_paths, list):
        raise BootstrapError("adoption planned ownership journal is invalid")
    if name in step_plans:
        if step_plans[name] != plan:
            raise BootstrapError(f"existing adoption {name} plan differs")
        return transaction
    by_path = {
        str(value["path"]): value
        for value in existing_paths
        if isinstance(value, dict) and isinstance(value.get("path"), str)
    }
    for value in normalized:
        path_value = str(value["path"])
        if path_value in by_path and by_path[path_value] != value:
            raise BootstrapError("adoption planned ownership conflicts")
        by_path[path_value] = value
    return _write_adoption_transaction(
        path,
        {
            **transaction,
            "planned_paths": list(by_path.values()),
            "step_plans": {**step_plans, name: plan},
        },
    )


def _advance_adoption_transaction(
    path: Path,
    transaction: dict[str, object],
    *,
    phase: str,
    evidence_name: str,
    evidence: object,
    created_paths: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    current = str(transaction.get("phase"))
    if phase not in ADOPTION_PHASES:
        raise BootstrapError("requested adoption phase is invalid")
    current_index = ADOPTION_PHASES.index(current)
    target_index = ADOPTION_PHASES.index(phase)
    stored = transaction["step_evidence"]
    if not isinstance(stored, dict):
        raise BootstrapError("adoption transaction evidence is invalid")
    if current_index >= target_index:
        if stored.get(evidence_name) != evidence:
            raise BootstrapError("completed adoption step evidence differs")
        return transaction
    if target_index != current_index + 1:
        raise BootstrapError("adoption phase transition is not sequential")
    paths = list(transaction["created_paths"])
    for value in created_paths or []:
        if value not in paths:
            paths.append(value)
    return _write_adoption_transaction(
        path,
        {
            **transaction,
            "phase": phase,
            "status": "completed" if phase == "completed" else "in-progress",
            "created_paths": paths,
            "step_evidence": {**stored, evidence_name: evidence},
        },
    )


def _ensure_adoption_lock(runtime_root: Path) -> tuple[Path, bool]:
    try:
        root = runtime_root.lstat()
        state = (runtime_root / "state").lstat()
    except OSError as exc:
        raise BootstrapError(
            "manual adoption requires the existing private runtime/state layout"
        ) from exc
    if (
        not stat.S_ISDIR(root.st_mode)
        or runtime_root.is_symlink()
        or root.st_uid != os.geteuid()
        or stat.S_IMODE(root.st_mode) != 0o700
        or not stat.S_ISDIR(state.st_mode)
        or (runtime_root / "state").is_symlink()
        or state.st_uid != os.geteuid()
        or stat.S_IMODE(state.st_mode) != 0o700
    ):
        raise BootstrapError("manual adoption runtime/state layout is unsafe")
    lock = runtime_root / "state/deploy.lock"
    created = False
    if not (lock.exists() or lock.is_symlink()):
        descriptor = os.open(
            lock,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(lock.parent)
        created = True
    _durability_barrier(lock, payload=b"", mode=0o600)
    return lock, created


def _ensure_adoption_layout(
    runtime_root: Path,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    created: list[dict[str, object]] = []
    evidence: dict[str, object] = {}
    for relative, requested_mode in DIRECTORIES.items():
        path = runtime_root / relative
        if not (path.exists() or path.is_symlink()):
            path.mkdir(parents=False, mode=requested_mode)
            _fsync_directory(path.parent)
            created.append(
                {
                    "path": str(path),
                    "kind": "directory",
                    "mode": format(requested_mode, "04o"),
                }
            )
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022
        ):
            raise BootstrapError(f"adoption runtime directory is unsafe: {path}")
        _durability_barrier(
            path,
            payload=None,
            mode=stat.S_IMODE(metadata.st_mode),
            directory=True,
        )
        evidence[relative] = {
            "path": str(path),
            "mode": format(stat.S_IMODE(metadata.st_mode), "04o"),
        }
    return evidence, created


def _new_adoption_transaction(
    runtime_root: Path,
    *,
    operation_id: str,
    identity: dict[str, object],
) -> tuple[Path, dict[str, object]]:
    directory = runtime_root / ADOPTION_TRANSACTION_RELATIVE_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = directory.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or directory.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise BootstrapError("adoption transaction directory is unsafe")
    _fsync_directory(directory.parent)
    _fsync_directory(directory)
    path = _adoption_transaction_path(runtime_root, operation_id=operation_id)
    if path.exists() or path.is_symlink():
        transaction = _validate_adoption_transaction(
            _load_private_json(path), path=path
        )
        if transaction["identity"] != identity:
            raise BootstrapError("existing adoption transaction identity differs")
        return path, transaction
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    transaction = {
        "schema_version": ADOPTION_TRANSACTION_SCHEMA_VERSION,
        "status": "in-progress",
        "phase": "intent",
        "operation_id": operation_id,
        "identity": identity,
        "identity_sha256": _canonical_json_digest(identity),
        "created_at": now,
        "updated_at": now,
        "planned_paths": [],
        "created_paths": [],
        "step_plans": {},
        "step_evidence": {"intent": identity},
    }
    _atomic_json(path, transaction)
    return path, _validate_adoption_transaction(
        _load_private_json(path), path=path
    )


def _adoption_initial_presence(runtime_root: Path) -> dict[str, object]:
    releases = runtime_root / "control-releases"
    release_names = (
        sorted(entry.name for entry in releases.iterdir())
        if releases.is_dir() and not releases.is_symlink()
        else []
    )
    paths = {
        str(runtime_root / relative): (
            (runtime_root / relative).exists()
            or (runtime_root / relative).is_symlink()
        )
        for relative in DIRECTORIES
    }
    for name in IMMUTABLE_FILES:
        path = runtime_root / "bin" / name
        paths[str(path)] = path.exists() or path.is_symlink()
    for relative in (
        ADOPTED_DEPLOYMENT_RELATIVE_PATH,
        Path("state/bootstrap-control.json"),
        Path("state/active-control.json"),
    ):
        path = runtime_root / relative
        paths[str(path)] = path.exists() or path.is_symlink()
    return {"paths": paths, "control_release_names": release_names}


def _adoption_preflight(
    runtime_root: Path,
    *,
    operation_id: str,
    permit_transaction: bool,
) -> None:
    transaction = _assert_exclusive_adoption_transaction(
        runtime_root, operation_id=operation_id
    )
    transaction_present = transaction is not None
    if transaction_present and not permit_transaction:
        raise BootstrapError("manual adoption operation already has a transaction")
    if transaction_present:
        assert transaction is not None
        if transaction["status"] == "aborted":
            raise BootstrapError("manual adoption operation was already aborted")
    for path in (
        runtime_root / "state/deploy-in-progress.json",
        runtime_root / "state/contract-0012-in-progress.json",
        runtime_root / ADOPTION_ALIAS_MARKER_RELATIVE_PATH,
    ):
        if path.exists() or path.is_symlink():
            raise BootstrapError("manual adoption is blocked by governed operation state")
    bootstrap_path = runtime_root / "state/bootstrap-control.json"
    current_path = runtime_root / "state/current-deployment.json"
    if current_path.exists() or current_path.is_symlink():
        raise BootstrapError(
            "manual adoption requires current deployment state to be absent"
        )
    if bootstrap_path.exists() or bootstrap_path.is_symlink():
        record = _load_private_json(bootstrap_path)
        if not (
            permit_transaction
            and record.get("schema_version") == 3
            and record.get("authority_kind") == ADOPTION_AUTHORITY_KIND
            and record.get("operation_id") == operation_id
            ):
            raise BootstrapError("production already has a bootstrap authority")
    if not transaction_present:
        for path in (
            runtime_root / "state/active-control.json",
            runtime_root / ADOPTED_DEPLOYMENT_RELATIVE_PATH,
        ):
            if path.exists() or path.is_symlink():
                raise BootstrapError(
                    "manual adoption found a pre-existing control destination"
                )
        bin_root = runtime_root / "bin"
        if bin_root.is_dir() and not bin_root.is_symlink() and any(
            bin_root.iterdir()
        ):
            raise BootstrapError("manual adoption found pre-existing runtime controls")
        releases = runtime_root / "control-releases"
        if releases.is_dir() and not releases.is_symlink() and any(
            releases.iterdir()
        ):
            raise BootstrapError("manual adoption found pre-existing control releases")


def _adopted_deployment_state(
    *,
    evidence: dict[str, object],
    evidence_sha256: str,
    active: dict[str, object],
    adopted_at: str,
) -> dict[str, object]:
    repository = evidence["live_repository"]
    bootstrap_source = evidence["bootstrap_source"]
    if not isinstance(repository, dict) or not isinstance(bootstrap_source, dict):
        raise BootstrapError("adoption evidence lacks repository authority")
    return {
        "schema_version": ADOPTED_DEPLOYMENT_SCHEMA_VERSION,
        "status": "adopted",
        "authority_kind": ADOPTION_AUTHORITY_KIND,
        "operation_id": evidence["operation_id"],
        "source_sha": repository["head"],
        "source_tree": repository["tree"],
        "bootstrap_source_sha": bootstrap_source["sha"],
        "bootstrap_source_tree": bootstrap_source["tree"],
        "active_control": active,
        "adoption_evidence": evidence,
        "adoption_evidence_sha256": evidence_sha256,
        "images": evidence["images"],
        "production_config": evidence["production_config"],
        "asset_identity": evidence["asset_identity"],
        "migrations": evidence["migrations"],
        "database": evidence["database"],
        "maintenance": evidence["maintenance"],
        "monomer_md": evidence["monomer_md"],
        "monomer_dft": evidence["monomer_dft"],
        "adopted_at": adopted_at,
    }


def _assert_adoption_evidence_unchanged(
    expected: dict[str, object],
    production_root: Path,
    runtime_root: Path,
    *,
    operation_id: str,
    bootstrap_source_sha: str,
    bootstrap_source_tree: str,
    live_sha: str,
    md_unit_sha256: str,
    dft_unit_sha256: str,
    allow_test: bool,
) -> None:
    observed = _collect_adoption_evidence(
        production_root,
        runtime_root,
        operation_id=operation_id,
        bootstrap_source_sha=bootstrap_source_sha,
        bootstrap_source_tree=bootstrap_source_tree,
        live_sha=live_sha,
        expected_md_unit_sha256=md_unit_sha256,
        expected_dft_unit_sha256=dft_unit_sha256,
        allow_test=allow_test,
    )
    if observed != expected:
        raise BootstrapError("manual runtime adoption evidence changed")


def _apply_manual_runtime_adoption(
    args: argparse.Namespace,
    *,
    production_root: Path,
    runtime_root: Path,
    source_sha: str,
    source_tree: str,
    source_readiness: dict[str, object],
    delivery_gate: dict[str, object],
    evidence: dict[str, object],
    evidence_sha256: str,
    immutable_payloads: dict[str, tuple[bytes, int]],
    control: object,
    allow_test: bool,
) -> dict[str, object]:
    if args.confirm_evidence_sha256 != evidence_sha256:
        raise BootstrapError("adoption evidence differs from explicit confirmation")
    repository = evidence.get("live_repository")
    if (
        not isinstance(repository, dict)
        or args.confirm_source_tree != repository.get("tree")
    ):
        raise BootstrapError("adopted live tree differs from explicit confirmation")
    operation_id = _require_adoption_operation_id(args.operation_id)
    _adoption_preflight(
        runtime_root,
        operation_id=operation_id,
        permit_transaction=True,
    )
    lock_path, lock_created = _ensure_adoption_lock(runtime_root)
    with _open_deploy_lock(lock_path) as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BootstrapError("another deployment holds deploy.lock") from exc
        # The first preflight happens before the lock path can be created and
        # acquired.  Repeat it under the shared deployment lock so a governed
        # operation that completed in that gap cannot be adopted as though the
        # original no-authority baseline still existed.
        _adoption_preflight(
            runtime_root,
            operation_id=operation_id,
            permit_transaction=True,
        )
        transaction_path = _adoption_transaction_path(
            runtime_root, operation_id=operation_id
        )
        recovered_transaction = False
        if transaction_path.exists() or transaction_path.is_symlink():
            transaction = _validate_adoption_transaction(
                _load_private_json(transaction_path), path=transaction_path
            )
            recovered_transaction = True
            stored_identity = transaction.get("identity")
            if not isinstance(stored_identity, dict):
                raise BootstrapError("adoption transaction identity is invalid")
            initial_presence = stored_identity.get("initial_presence")
            expected_fields = {
                "authority_kind": ADOPTION_AUTHORITY_KIND,
                "operation_id": operation_id,
                "bootstrap_source_sha": source_sha,
                "bootstrap_source_tree": source_tree,
                "live_source_sha": args.live_sha,
                "live_source_tree": repository["tree"],
                "evidence_sha256": evidence_sha256,
                "source_readiness_sha256": _canonical_json_digest(source_readiness),
                "delivery_gate_sha256": _canonical_json_digest(delivery_gate),
                "md_unit_sha256": args.confirm_md_unit_sha256,
                "dft_unit_sha256": args.confirm_dft_unit_sha256,
                "deploy_lock_disposition": ADOPTION_DEPLOY_LOCK_DISPOSITION,
            }
            if any(
                stored_identity.get(name) != value
                for name, value in expected_fields.items()
            ):
                raise BootstrapError("adoption transaction authority changed")
        else:
            initial_presence = _adoption_initial_presence(runtime_root)
            identity = {
                "authority_kind": ADOPTION_AUTHORITY_KIND,
                "operation_id": operation_id,
                "bootstrap_source_sha": source_sha,
                "bootstrap_source_tree": source_tree,
                "live_source_sha": args.live_sha,
                "live_source_tree": repository["tree"],
                "evidence_sha256": evidence_sha256,
                "source_readiness_sha256": _canonical_json_digest(source_readiness),
                "delivery_gate_sha256": _canonical_json_digest(delivery_gate),
                "md_unit_sha256": args.confirm_md_unit_sha256,
                "dft_unit_sha256": args.confirm_dft_unit_sha256,
                "initial_presence": initial_presence,
                "deploy_lock_created": lock_created,
                "deploy_lock_disposition": ADOPTION_DEPLOY_LOCK_DISPOSITION,
            }
            transaction_path, transaction = _new_adoption_transaction(
                runtime_root,
                operation_id=operation_id,
                identity=identity,
            )
        if not isinstance(initial_presence, dict):
            raise BootstrapError("adoption initial path authority is invalid")
        initial_paths = initial_presence.get("paths")
        initial_releases = initial_presence.get("control_release_names")
        if not isinstance(initial_paths, dict) or not isinstance(initial_releases, list):
            raise BootstrapError("adoption initial path inventory is invalid")
        _assert_adoption_evidence_unchanged(
            evidence,
            production_root,
            runtime_root,
            operation_id=operation_id,
            bootstrap_source_sha=source_sha,
            bootstrap_source_tree=source_tree,
            live_sha=args.live_sha,
            md_unit_sha256=args.confirm_md_unit_sha256,
            dft_unit_sha256=args.confirm_dft_unit_sha256,
            allow_test=allow_test,
        )
        if recovered_transaction:
            transaction = _reseal_adoption_transaction(
                transaction_path, transaction
            )
        layout_planned: list[dict[str, object]] = []
        for relative, mode in DIRECTORIES.items():
            path = runtime_root / relative
            if (
                Path(relative) != ADOPTION_TRANSACTION_RELATIVE_DIRECTORY
                and initial_paths.get(str(path)) is False
            ):
                layout_planned.append(
                    {
                        "path": str(path),
                        "kind": "directory",
                        "mode": format(mode, "04o"),
                    }
                )
        transaction = _record_adoption_plan(
            transaction_path,
            transaction,
            name="layout",
            evidence={"directories": layout_planned},
            planned_paths=layout_planned,
        )
        layout_evidence, _layout_created_now = _ensure_adoption_layout(runtime_root)
        transaction = _advance_adoption_transaction(
            transaction_path,
            transaction,
            phase="layout-ready",
            evidence_name="runtime_layout",
            evidence=layout_evidence,
            created_paths=layout_planned,
        )
        _assert_adoption_evidence_unchanged(
            evidence,
            production_root,
            runtime_root,
            operation_id=operation_id,
            bootstrap_source_sha=source_sha,
            bootstrap_source_tree=source_tree,
            live_sha=args.live_sha,
            md_unit_sha256=args.confirm_md_unit_sha256,
            dft_unit_sha256=args.confirm_dft_unit_sha256,
            allow_test=allow_test,
        )
        if any(str(value).startswith(".bootstrap-") for value in initial_releases):
            raise BootstrapError("manual adoption found pre-existing control staging")
        control_plan = _plan_control_release(
            runtime_root,
            control=control,
            source_sha=source_sha,
            source_tree=source_tree,
            allow_test=allow_test,
            prepared_at=str(transaction["created_at"]),
        )
        candidate = control_plan["candidate"]
        active = control_plan["active"]
        release_id = control_plan["release_id"]
        if (
            not isinstance(candidate, dict)
            or not isinstance(active, dict)
            or not isinstance(release_id, str)
        ):
            raise BootstrapError("planned adoption controls are invalid")
        control_planned: list[dict[str, object]] = []
        bin_temporary_paths: dict[str, Path] = {}
        bin_temporary_authorities: dict[str, dict[str, object]] = {}
        controls_planned = "controls" in transaction["step_plans"]
        staging_path = control_plan.get("staging_path")
        if (
            not controls_planned
            and isinstance(staging_path, Path)
            and (staging_path.exists() or staging_path.is_symlink())
        ):
            raise BootstrapError(
                "control staging existed before durable intent"
            )
        immutable_digests: dict[str, str] = {}
        for name, (payload, mode) in immutable_payloads.items():
            path = runtime_root / "bin" / name
            immutable_digests[name] = digest(payload)
            if initial_paths.get(str(path)) is False:
                temporary = _adoption_install_temporary_path(
                    path, operation_id=operation_id
                )
                bin_temporary_paths[name] = temporary
                temporary_authority = _adoption_install_staging_plan(
                    temporary,
                    path,
                    payload,
                    mode,
                    operation_id=operation_id,
                )
                if not controls_planned and (
                    temporary.exists() or temporary.is_symlink()
                ):
                    raise BootstrapError(
                        "immutable staging existed before durable intent"
                    )
                bin_temporary_authorities[name] = temporary_authority
                control_planned.extend(
                    (
                        _adoption_file_plan(path, payload, mode),
                        temporary_authority,
                    )
                )
        if release_id not in initial_releases:
            staging_ownership = control_plan.get("staging_tree_ownership")
            release_ownership = control_plan.get("release_tree_ownership")
            if not isinstance(staging_ownership, dict) or not isinstance(
                release_ownership, dict
            ):
                raise BootstrapError("planned control-release ownership is invalid")
            control_planned.extend((staging_ownership, release_ownership))
        controls_plan_evidence = {
            "immutable_files": immutable_digests,
            "candidate_control": candidate,
            "active_control": active,
            "release_tree": control_plan["release_tree_ownership"],
        }
        transaction = _record_adoption_plan(
            transaction_path,
            transaction,
            name="controls",
            evidence=controls_plan_evidence,
            planned_paths=control_planned,
        )
        installed: dict[str, str] = {}
        control_created: list[dict[str, object]] = []
        for name, (payload, mode) in immutable_payloads.items():
            path = runtime_root / "bin" / name
            installed[name] = _install_exact(
                path,
                payload,
                mode,
                temporary_path=bin_temporary_paths.get(name),
                temporary_authority=bin_temporary_authorities.get(name),
                reject_unowned_staging=True,
            )
            if initial_paths.get(str(path)) is False:
                control_created.append(_adoption_file_plan(path, payload, mode))
        actual_bin = {entry.name for entry in (runtime_root / "bin").iterdir()}
        if actual_bin != set(IMMUTABLE_FILES):
            raise BootstrapError("runtime/bin contains non-adoption controls")
        candidate, active = _build_control_release(
            runtime_root,
            control=control,
            source_sha=source_sha,
            source_tree=source_tree,
            allow_test=allow_test,
            prepared_at=str(transaction["created_at"]),
            plan=control_plan,
            staging_authorized=True,
        )
        release_path = runtime_root / "control-releases" / str(candidate["release_id"])
        if str(candidate["release_id"]) not in initial_releases:
            release_ownership = control_plan["release_tree_ownership"]
            if _adoption_tree_identity(release_path) != release_ownership:
                raise BootstrapError("installed control-release tree differs from plan")
            control_created.append(release_ownership)
        controls_evidence = {
            "immutable_files": installed,
            "candidate_control": candidate,
            "active_control": active,
        }
        transaction = _advance_adoption_transaction(
            transaction_path,
            transaction,
            phase="controls-ready",
            evidence_name="controls",
            evidence=controls_evidence,
            created_paths=control_created,
        )
        _assert_adoption_evidence_unchanged(
            evidence,
            production_root,
            runtime_root,
            operation_id=operation_id,
            bootstrap_source_sha=source_sha,
            bootstrap_source_tree=source_tree,
            live_sha=args.live_sha,
            md_unit_sha256=args.confirm_md_unit_sha256,
            dft_unit_sha256=args.confirm_dft_unit_sha256,
            allow_test=allow_test,
        )
        adopted_state = _adopted_deployment_state(
            evidence=evidence,
            evidence_sha256=evidence_sha256,
            active=active,
            adopted_at=str(transaction["created_at"]),
        )
        adopted_path = runtime_root / ADOPTED_DEPLOYMENT_RELATIVE_PATH
        adopted_payload = json.dumps(
            adopted_state,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8") + b"\n"
        baseline_planned: list[dict[str, object]] = []
        adopted_temporary: Path | None = None
        adopted_temporary_authority: dict[str, object] | None = None
        if initial_paths.get(str(adopted_path)) is False:
            adopted_temporary = _adoption_install_temporary_path(
                adopted_path, operation_id=operation_id
            )
            adopted_temporary_authority = _adoption_install_staging_plan(
                adopted_temporary,
                adopted_path,
                adopted_payload,
                0o600,
                operation_id=operation_id,
            )
            if "baseline" not in transaction["step_plans"] and (
                adopted_temporary.exists() or adopted_temporary.is_symlink()
            ):
                raise BootstrapError(
                    "immutable staging existed before durable intent"
                )
            baseline_planned.extend(
                (
                    _adoption_file_plan(adopted_path, adopted_payload, 0o600),
                    adopted_temporary_authority,
                )
            )
        baseline_evidence = {
            "adopted_deployment_sha256": _canonical_json_digest(adopted_state),
            "adopted_deployment": adopted_state,
        }
        transaction = _record_adoption_plan(
            transaction_path,
            transaction,
            name="baseline",
            evidence=baseline_evidence,
            planned_paths=baseline_planned,
        )
        _install_exact(
            adopted_path,
            adopted_payload,
            0o600,
            temporary_path=adopted_temporary,
            temporary_authority=adopted_temporary_authority,
            reject_unowned_staging=True,
        )
        if _load_private_json(adopted_path) != adopted_state:
            raise BootstrapError("existing adopted deployment state differs")
        baseline_created = [
            _adoption_file_plan(adopted_path, adopted_payload, 0o600)
        ] if initial_paths.get(str(adopted_path)) is False else []
        transaction = _advance_adoption_transaction(
            transaction_path,
            transaction,
            phase="baseline-ready",
            evidence_name="baseline",
            evidence=baseline_evidence,
            created_paths=baseline_created,
        )
        _assert_adoption_evidence_unchanged(
            evidence,
            production_root,
            runtime_root,
            operation_id=operation_id,
            bootstrap_source_sha=source_sha,
            bootstrap_source_tree=source_tree,
            live_sha=args.live_sha,
            md_unit_sha256=args.confirm_md_unit_sha256,
            dft_unit_sha256=args.confirm_dft_unit_sha256,
            allow_test=allow_test,
        )
        bootstrap_path = runtime_root / "state/bootstrap-control.json"
        active_path = runtime_root / "state/active-control.json"
        bootstrap_base = {
            "schema_version": 3,
            "authority_kind": ADOPTION_AUTHORITY_KIND,
            "operation_id": operation_id,
            "source_sha": source_sha,
            "source_tree": source_tree,
            "source_readiness": source_readiness,
            "source_readiness_sha256": _canonical_json_digest(source_readiness),
            "delivery_gate": delivery_gate,
            "production_repository": repository,
            "adoption": evidence,
            "adoption_evidence_sha256": evidence_sha256,
            "adopted_deployment": adopted_state,
            "adopted_deployment_sha256": _canonical_json_digest(adopted_state),
            "immutable_files": installed,
            "candidate_control": candidate,
            "active_control": active,
        }
        prepared_bootstrap = {**bootstrap_base, "status": "prepared"}
        completed_bootstrap = {**bootstrap_base, "status": "completed"}
        json_payload = lambda value: json.dumps(  # noqa: E731
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8") + b"\n"
        bootstrap_payload = json_payload(prepared_bootstrap)
        completed_payload = json_payload(completed_bootstrap)
        active_payload = json_payload(active)
        bootstrap_temporary = _adoption_install_temporary_path(
            bootstrap_path, operation_id=operation_id
        )
        active_temporary = _adoption_install_temporary_path(
            active_path, operation_id=operation_id
        )
        cas_temporary = bootstrap_path.parent / (
            f".{bootstrap_path.name}.{operation_id}.complete.tmp"
        )
        bootstrap_staging = _adoption_install_staging_plan(
            bootstrap_temporary, bootstrap_path, bootstrap_payload, 0o600,
            operation_id=operation_id,
        )
        active_staging = _adoption_install_staging_plan(
            active_temporary, active_path, active_payload, 0o600,
            operation_id=operation_id,
        )
        cas_staging = _adoption_install_staging_plan(
            cas_temporary, bootstrap_path, completed_payload, 0o600,
            operation_id=operation_id, purpose="cas",
        )
        authority_intent = {
            "bootstrap_schema_version": 3,
            "active_control": active,
            "adopted_deployment_sha256": _canonical_json_digest(adopted_state),
        }
        if transaction["phase"] == "baseline-ready":
            for path in (bootstrap_path, active_path):
                if path.exists() or path.is_symlink():
                    raise BootstrapError(
                        "adoption authority destination appeared before commit intent"
                    )
            if "authority" not in transaction["step_plans"] and any(
                path.exists() or path.is_symlink()
                for path in (bootstrap_temporary, active_temporary, cas_temporary)
            ):
                raise BootstrapError(
                    "adoption authority staging appeared before durable intent"
                )
        else:
            if bootstrap_path.exists() or bootstrap_path.is_symlink():
                current_bootstrap = _load_private_json(bootstrap_path)
                if current_bootstrap not in (
                    prepared_bootstrap,
                    completed_bootstrap,
                ):
                    raise BootstrapError(
                        "existing adoption bootstrap authority differs"
                    )
            if active_path.exists() or active_path.is_symlink():
                if _load_private_json(active_path) != active:
                    raise BootstrapError(
                        "existing adoption active control differs"
                    )
        transaction = _record_adoption_plan(
            transaction_path,
            transaction,
            name="authority",
            evidence=authority_intent,
            planned_paths=[bootstrap_staging, active_staging, cas_staging],
        )
        transaction = _advance_adoption_transaction(
            transaction_path,
            transaction,
            phase="authority-commit-intent",
            evidence_name="authority_intent",
            evidence=authority_intent,
        )
        if bootstrap_path.exists() or bootstrap_path.is_symlink():
            current_bootstrap = _load_private_json(bootstrap_path)
            if current_bootstrap not in (prepared_bootstrap, completed_bootstrap):
                raise BootstrapError("existing adoption bootstrap authority differs")
            if current_bootstrap == prepared_bootstrap:
                _install_exact(
                    bootstrap_path,
                    bootstrap_payload,
                    0o600,
                    temporary_path=bootstrap_temporary,
                    temporary_authority=bootstrap_staging,
                    authorized_staging_siblings=((cas_temporary, cas_staging),),
                    reject_unowned_staging=True,
                )
        else:
            _install_exact(
                bootstrap_path,
                bootstrap_payload,
                0o600,
                temporary_path=bootstrap_temporary,
                temporary_authority=bootstrap_staging,
                reject_unowned_staging=True,
            )
        _install_exact(
            active_path,
            active_payload,
            0o600,
            temporary_path=active_temporary,
            temporary_authority=active_staging,
            reject_unowned_staging=True,
        )
        # Reconcile unconditionally.  An exchange response can be lost after
        # the final already became completed, leaving the old prepared inode
        # under the exact journal-authorized CAS staging name.
        _cas_replace_exact_file(
            bootstrap_path,
            expected_payload=bootstrap_payload,
            replacement_payload=completed_payload,
            mode=0o600,
            temporary_path=cas_temporary,
            temporary_authority=cas_staging,
        )
        loaded_active, _manifest, _release = control.load_active_control(runtime_root)
        if loaded_active != active:
            raise BootstrapError("adoption active control did not commit")
        transaction = _advance_adoption_transaction(
            transaction_path,
            transaction,
            phase="completed",
            evidence_name="authority",
            evidence={
                "bootstrap_control_sha256": digest(bootstrap_path.read_bytes()),
                "active_control_sha256": digest(active_path.read_bytes()),
                "adopted_deployment_sha256": digest(adopted_path.read_bytes()),
            },
        )
    return {
        "status": "adopted",
        "operation_id": operation_id,
        "evidence_sha256": evidence_sha256,
        "adopted_deployment": adopted_state,
        "active_control": active,
        "adoption_transaction": transaction,
    }


def _planned_install_link_residues(
    ownership_by_path: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    residues: list[dict[str, object]] = []
    for temporary_authority in ownership_by_path.values():
        if (
            temporary_authority.get("kind") != "install-staging"
            or temporary_authority.get("purpose") != "install"
        ):
            continue
        temporary = Path(str(temporary_authority["path"]))
        destination = Path(str(temporary_authority["destination"]))
        if not (temporary.exists() or temporary.is_symlink()) or not (
            destination.exists() or destination.is_symlink()
        ):
            continue
        destination_ownership = ownership_by_path.get(str(destination))
        if (
            not isinstance(destination_ownership, dict)
            or destination_ownership.get("kind") != "file"
            or destination_ownership.get("sha256")
            != temporary_authority.get("sha256")
            or destination_ownership.get("mode")
            != temporary_authority.get("mode")
        ):
            raise BootstrapError(
                "linked install destination lacks exact durable ownership"
            )
        record, payload = _regular_file_record(
            destination,
            label="linked adoption install destination",
            maximum=64 * 1024 * 1024,
            allowed_modes={int(str(destination_ownership["mode"]), 8)},
        )
        if (
            record["sha256"] != destination_ownership["sha256"]
            or record["mode"] != destination_ownership["mode"]
        ):
            raise BootstrapError("linked adoption install payload changed")
        identity = _authorized_install_link_identity(
            destination,
            temporary,
            payload=payload,
            mode=int(str(destination_ownership["mode"]), 8),
            temporary_authority=temporary_authority,
            remove_temporary=False,
        )
        residues.append(
            {
                "destination": str(destination),
                "temporary": str(temporary),
                "destination_after_quarantine": None,
                "destination_ownership": destination_ownership,
                "temporary_authority": temporary_authority,
                "identity": identity,
            }
        )
    return residues


def _quarantine_identity(
    path: Path,
    *,
    excluded_paths: set[Path] | None = None,
    linked_destinations: set[Path] | None = None,
) -> dict[str, object]:
    metadata = path.lstat()
    if path.is_symlink() or metadata.st_uid != os.geteuid():
        raise BootstrapError("adoption quarantine source is unsafe")
    if stat.S_ISDIR(metadata.st_mode):
        tree = _adoption_tree_identity(
            path,
            excluded_paths=excluded_paths,
            linked_destinations=linked_destinations,
        )
        return {
            "kind": "tree",
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "identity_sha256": tree["identity_sha256"],
        }
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise BootstrapError("adoption quarantine source is unsafe")
    mode = stat.S_IMODE(metadata.st_mode)
    payload = _durability_barrier(path, payload=None, mode=mode)
    rebound = path.lstat()
    if (
        _inode_identity(metadata) != _inode_identity(rebound)
        or rebound.st_nlink != 1
    ):
        raise BootstrapError("adoption quarantine source changed")
    return {
        "kind": "file",
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "sha256": digest(payload),
        "mode": format(mode, "04o"),
        "size": len(payload),
    }


def _rename_noreplace_between(
    source: Path,
    destination: Path,
    *,
    expected_identity: object,
) -> None:
    if (
        not isinstance(expected_identity, dict)
        or not isinstance(expected_identity.get("device"), int)
        or not isinstance(expected_identity.get("inode"), int)
    ):
        raise BootstrapError("adoption quarantine inode authority is invalid")
    parent_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    source_parent = os.open(source.parent, parent_flags)
    target_parent = os.open(destination.parent, parent_flags)
    source_descriptor = -1
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        os.close(source_parent)
        os.close(target_parent)
        raise BootstrapError("renameat2 is required for adoption quarantine")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    try:
        def object_identity(value: os.stat_result) -> tuple[int, ...]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_uid,
                value.st_nlink,
                value.st_size,
                value.st_mtime_ns,
            )

        for descriptor, path in (
            (source_parent, source.parent),
            (target_parent, destination.parent),
        ):
            pinned_parent = os.fstat(descriptor)
            bound_parent = path.lstat()
            if (
                not stat.S_ISDIR(pinned_parent.st_mode)
                or pinned_parent.st_uid != os.geteuid()
                or (pinned_parent.st_dev, pinned_parent.st_ino)
                != (bound_parent.st_dev, bound_parent.st_ino)
            ):
                raise BootstrapError("adoption quarantine parent changed")
        source_descriptor = os.open(
            source.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=source_parent,
        )
        held = os.fstat(source_descriptor)
        bound = os.stat(source.name, dir_fd=source_parent, follow_symlinks=False)
        if (
            (held.st_dev, held.st_ino)
            != (expected_identity["device"], expected_identity["inode"])
            or _inode_identity(bound) != _inode_identity(held)
        ):
            raise BootstrapError("adoption quarantine source binding changed")
        result = renameat2(
            source_parent,
            os.fsencode(source.name),
            target_parent,
            os.fsencode(destination.name),
            1,
        )
        if result != 0:
            error = ctypes.get_errno()
            raise BootstrapError(
                f"adoption quarantine publication failed: {os.strerror(error)}"
            )
        moved = os.stat(
            destination.name, dir_fd=target_parent, follow_symlinks=False
        )
        held_after = os.fstat(source_descriptor)
        if (
            object_identity(held_after) != object_identity(held)
            or _inode_identity(moved) != _inode_identity(held_after)
        ):
            still_moved = os.stat(
                destination.name,
                dir_fd=target_parent,
                follow_symlinks=False,
            )
            if _inode_identity(still_moved) == _inode_identity(moved):
                restored = renameat2(
                    target_parent,
                    os.fsencode(destination.name),
                    source_parent,
                    os.fsencode(source.name),
                    1,
                )
                if restored == 0:
                    os.fsync(source_parent)
                    os.fsync(target_parent)
                    restored_source = os.stat(
                        source.name,
                        dir_fd=source_parent,
                        follow_symlinks=False,
                    )
                    if object_identity(restored_source) != object_identity(moved):
                        raise BootstrapError(
                            "adoption quarantine restore binding changed"
                        )
            raise BootstrapError("adoption quarantine target binding changed")
        try:
            os.stat(source.name, dir_fd=source_parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise BootstrapError("adoption quarantine source remained published")
        os.fsync(source_parent)
        if target_parent != source_parent:
            os.fsync(target_parent)
        rebound = os.stat(
            destination.name, dir_fd=target_parent, follow_symlinks=False
        )
        if _inode_identity(rebound) != _inode_identity(held_after):
            raise BootstrapError("adoption quarantine target changed after fsync")
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        os.close(source_parent)
        os.close(target_parent)


def _adoption_quarantine_plan(
    ownership_by_path: dict[str, dict[str, object]],
    *,
    transaction_path: Path,
    linked_install_residues: list[dict[str, object]],
) -> dict[str, object]:
    operation_id = transaction_path.stem
    quarantine = transaction_path.parent / f".{operation_id}.abort-quarantine"
    if quarantine.exists() or quarantine.is_symlink():
        raise BootstrapError("adoption quarantine existed before durable intent")
    linked_temporaries = {
        Path(str(value["temporary"])) for value in linked_install_residues
    }
    linked_destinations = {
        Path(str(value["destination"])) for value in linked_install_residues
    }
    present = [
        (Path(path), ownership)
        for path, ownership in ownership_by_path.items()
        if Path(path) not in linked_temporaries
        and (Path(path).exists() or Path(path).is_symlink())
    ]
    roots = [
        (path, ownership)
        for path, ownership in present
        if not any(path != parent and path.is_relative_to(parent) for parent, _ in present)
    ]
    entries: list[dict[str, object]] = []
    root_targets: dict[Path, Path] = {}
    identity_by_destination = {
        Path(str(value["destination"])): value["identity"]
        for value in linked_install_residues
    }
    for index, (path, _ownership) in enumerate(roots):
        target = quarantine / f"{index:04d}"
        root_targets[path] = target
        if path in identity_by_destination:
            linked_identity = identity_by_destination[path]
            if not isinstance(linked_identity, dict):
                raise BootstrapError("linked install quarantine identity is invalid")
            identity = {"kind": "file", **linked_identity}
        else:
            excluded = {
                temporary
                for temporary in linked_temporaries
                if temporary.is_relative_to(path)
            }
            linked = {
                destination
                for destination in linked_destinations
                if destination.is_relative_to(path)
            }
            identity = _quarantine_identity(
                path,
                excluded_paths=excluded or None,
                linked_destinations=linked or None,
            )
        entries.append(
            {
                "source": str(path),
                "target": str(target),
                "identity": identity,
            }
        )
    planned_residues: list[dict[str, object]] = []
    for residue in linked_install_residues:
        destination = Path(str(residue["destination"]))
        containing = [
            root for root in root_targets if destination.is_relative_to(root)
        ]
        if len(containing) != 1:
            raise BootstrapError("linked install quarantine root is ambiguous")
        root = containing[0]
        target = root_targets[root]
        after = target if destination == root else target / destination.relative_to(root)
        planned_residues.append(
            {**residue, "destination_after_quarantine": str(after)}
        )
    return {
        "schema_version": 2,
        "root": str(quarantine),
        "entries": entries,
        "linked_install_residues": planned_residues,
    }


def _resume_linked_install_residue(raw: object) -> None:
    if (
        not isinstance(raw, dict)
        or set(raw)
        != {
            "destination",
            "temporary",
            "destination_after_quarantine",
            "destination_ownership",
            "temporary_authority",
            "identity",
        }
    ):
        raise BootstrapError("linked install quarantine journal is invalid")
    destination = Path(str(raw["destination"]))
    temporary = Path(str(raw["temporary"]))
    after = Path(str(raw["destination_after_quarantine"]))
    destination_ownership = _validate_adoption_ownership(
        raw["destination_ownership"], label="linked install destination"
    )
    temporary_authority = _validate_adoption_ownership(
        raw["temporary_authority"], label="linked install temporary"
    )
    identity = raw["identity"]
    if (
        destination_ownership.get("kind") != "file"
        or destination_ownership.get("path") != str(destination)
        or temporary_authority.get("kind") != "install-staging"
        or temporary_authority.get("purpose") != "install"
        or temporary_authority.get("path") != str(temporary)
        or temporary_authority.get("destination") != str(destination)
        or temporary_authority.get("sha256")
        != destination_ownership.get("sha256")
        or temporary_authority.get("mode") != destination_ownership.get("mode")
        or not isinstance(identity, dict)
        or set(identity) != {"device", "inode", "sha256", "mode", "size"}
        or not isinstance(identity.get("device"), int)
        or not isinstance(identity.get("inode"), int)
        or identity.get("sha256") != destination_ownership.get("sha256")
        or identity.get("mode") != destination_ownership.get("mode")
        or not isinstance(identity.get("size"), int)
    ):
        raise BootstrapError("linked install quarantine authority differs")
    mode = int(str(destination_ownership["mode"]), 8)
    temporary_present = temporary.exists() or temporary.is_symlink()
    destination_present = destination.exists() or destination.is_symlink()
    after_present = after.exists() or after.is_symlink()
    if destination_present and after_present:
        raise BootstrapError("linked install destination has two live bindings")
    if temporary_present:
        if not destination_present or after_present:
            raise BootstrapError("linked install pair changed before collapse")
        record, payload = _regular_file_record(
            destination,
            label="linked install destination",
            maximum=64 * 1024 * 1024,
            allowed_modes={mode},
        )
        if (
            record["sha256"] != destination_ownership["sha256"]
            or len(payload) != identity["size"]
        ):
            raise BootstrapError("linked install destination payload changed")
        observed = _authorized_install_link_identity(
            destination,
            temporary,
            payload=payload,
            mode=mode,
            temporary_authority=temporary_authority,
            remove_temporary=True,
        )
        if observed != identity:
            raise BootstrapError("linked install inode identity changed")
        return
    candidates = [
        value
        for value, present in (
            (destination, destination_present),
            (after, after_present),
        )
        if present
    ]
    if len(candidates) != 1:
        raise BootstrapError("collapsed linked install destination disappeared")
    current = candidates[0]
    metadata = current.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or current.is_symlink()
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or metadata.st_dev != identity["device"]
        or metadata.st_ino != identity["inode"]
    ):
        raise BootstrapError("collapsed linked install inode changed")
    payload = _durability_barrier(current, payload=None, mode=mode)
    rebound = current.lstat()
    if (
        digest(payload) != identity["sha256"]
        or len(payload) != identity["size"]
        or rebound.st_nlink != 1
        or (rebound.st_dev, rebound.st_ino)
        != (identity["device"], identity["inode"])
    ):
        raise BootstrapError("collapsed linked install payload changed")


def _resume_adoption_quarantine(plan: object, *, transaction_path: Path) -> None:
    if (
        not isinstance(plan, dict)
        or set(plan)
        != {"schema_version", "root", "entries", "linked_install_residues"}
        or plan.get("schema_version") != 2
        or not isinstance(plan.get("entries"), list)
        or not isinstance(plan.get("linked_install_residues"), list)
    ):
        raise BootstrapError("adoption quarantine journal is invalid")
    root = Path(str(plan["root"]))
    if root != transaction_path.parent / f".{transaction_path.stem}.abort-quarantine":
        raise BootstrapError("adoption quarantine root differs")
    if not (root.exists() or root.is_symlink()):
        root.mkdir(mode=0o700)
        _fsync_directory(root.parent)
    _durability_barrier(root, payload=None, mode=0o700, directory=True)
    runtime_root = transaction_path.parents[2]
    entry_bindings: list[tuple[Path, Path]] = []
    for index, raw_entry in enumerate(plan["entries"]):
        if not isinstance(raw_entry, dict):
            raise BootstrapError("adoption quarantine entry is invalid")
        entry_bindings.append(
            (Path(str(raw_entry.get("source"))), root / f"{index:04d}")
        )
    seen_linked_paths: set[Path] = set()
    for raw_residue in plan["linked_install_residues"]:
        if not isinstance(raw_residue, dict):
            raise BootstrapError("linked install quarantine journal is invalid")
        destination = Path(str(raw_residue.get("destination")))
        temporary = Path(str(raw_residue.get("temporary")))
        after = Path(str(raw_residue.get("destination_after_quarantine")))
        containing = [
            (source, target)
            for source, target in entry_bindings
            if destination.is_relative_to(source)
        ]
        if (
            len(containing) != 1
            or destination in seen_linked_paths
            or temporary in seen_linked_paths
            or not destination.is_relative_to(runtime_root)
            or not temporary.is_relative_to(runtime_root)
        ):
            raise BootstrapError("linked install quarantine path escapes authority")
        source_root, target_root = containing[0]
        expected_after = (
            target_root
            if destination == source_root
            else target_root / destination.relative_to(source_root)
        )
        if after != expected_after:
            raise BootstrapError("linked install quarantine target differs")
        seen_linked_paths.update((destination, temporary))
        _resume_linked_install_residue(raw_residue)
    seen_sources: set[Path] = set()
    seen_targets: set[Path] = set()
    for index, raw in enumerate(plan["entries"]):
        if (
            not isinstance(raw, dict)
            or set(raw) != {"source", "target", "identity"}
        ):
            raise BootstrapError("adoption quarantine entry is invalid")
        source = Path(str(raw["source"]))
        target = Path(str(raw["target"]))
        if (
            target != root / f"{index:04d}"
            or not source.is_absolute()
            or not source.is_relative_to(runtime_root)
            or source in seen_sources
            or target in seen_targets
        ):
            raise BootstrapError("adoption quarantine entry escapes authority")
        seen_sources.add(source)
        seen_targets.add(target)
        source_present = source.exists() or source.is_symlink()
        target_present = target.exists() or target.is_symlink()
        if source_present and target_present:
            raise BootstrapError("adoption quarantine has two live bindings")
        if source_present:
            if _quarantine_identity(source) != raw["identity"]:
                raise BootstrapError("adoption quarantine source changed")
            _rename_noreplace_between(
                source, target, expected_identity=raw["identity"]
            )
        elif not target_present:
            raise BootstrapError("adoption quarantine entry disappeared")
        if _quarantine_identity(target) != raw["identity"]:
            raise BootstrapError("adoption quarantine target changed")
        for parent in (source.parent, root):
            parent_metadata = parent.lstat()
            _durability_barrier(
                parent,
                payload=None,
                mode=stat.S_IMODE(parent_metadata.st_mode),
                directory=True,
            )


def _abort_manual_runtime_adoption(
    args: argparse.Namespace,
    *,
    runtime_root: Path,
) -> dict[str, object]:
    operation_id = _require_adoption_operation_id(args.operation_id)
    transaction_path = _adoption_transaction_path(
        runtime_root, operation_id=operation_id
    )
    transaction = _validate_adoption_transaction(
        _load_private_json(transaction_path), path=transaction_path
    )
    identity = transaction.get("identity")
    if (
        not isinstance(identity, dict)
        or identity.get("operation_id") != operation_id
        or identity.get("bootstrap_source_sha") != args.sha
        or identity.get("live_source_sha") != args.live_sha
        or identity.get("live_source_tree") != args.confirm_source_tree
        or identity.get("evidence_sha256") != args.confirm_evidence_sha256
        or identity.get("md_unit_sha256") != args.confirm_md_unit_sha256
        or identity.get("dft_unit_sha256") != args.confirm_dft_unit_sha256
    ):
        raise BootstrapError("adoption abort confirmation differs from transaction")
    lock_path, _created = _ensure_adoption_lock(runtime_root)
    with _open_deploy_lock(lock_path) as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BootstrapError("another deployment holds deploy.lock") from exc
        current_transaction = _assert_exclusive_adoption_transaction(
            runtime_root, operation_id=operation_id
        )
        if current_transaction is None:
            raise BootstrapError("adoption transaction disappeared before abort")
        transaction = current_transaction
        if transaction.get("identity") != identity:
            raise BootstrapError("adoption transaction changed before abort")
        if transaction["status"] == "aborted":
            transaction = _reseal_adoption_transaction(
                transaction_path, transaction
            )
            evidence = transaction.get("step_evidence")
            quarantine = (
                evidence.get("abort_quarantine")
                if isinstance(evidence, dict)
                else None
            )
            _resume_adoption_quarantine(
                quarantine, transaction_path=transaction_path
            )
            return {"status": "already-aborted", "operation_id": operation_id}
        if transaction["phase"] in {"authority-commit-intent", "completed"}:
            raise BootstrapError(
                "adoption abort is forbidden after authority commit intent"
            )
        for path in (
            runtime_root / "state/bootstrap-control.json",
            runtime_root / "state/active-control.json",
            runtime_root / "state/current-deployment.json",
        ):
            if path.exists() or path.is_symlink():
                raise BootstrapError(
                    "adoption abort found committed or foreign deployment authority"
                )
        ownership_by_path: dict[str, dict[str, object]] = {}
        for raw in [
            *list(transaction["planned_paths"]),
            *list(transaction["created_paths"]),
        ]:
            ownership = _validate_adoption_ownership(
                raw, label="adoption abort"
            )
            path_value = str(ownership["path"])
            if (
                path_value in ownership_by_path
                and ownership_by_path[path_value] != ownership
            ):
                raise BootstrapError("adoption abort ownership conflicts")
            ownership_by_path[path_value] = ownership
        ordered_ownership = reversed(list(ownership_by_path.values()))
        owned_paths = {Path(value) for value in ownership_by_path}
        linked_install_residues = _planned_install_link_residues(
            ownership_by_path
        )
        linked_destinations = {
            Path(str(value["destination"])) for value in linked_install_residues
        }
        linked_temporaries = {
            Path(str(value["temporary"])) for value in linked_install_residues
        }
        # Validate the entire cleanup set before deleting its first byte.  This
        # makes an existing drift a zero-delete failure rather than a partial
        # teardown discovered halfway through the reverse traversal.
        for ownership in list(ordered_ownership):
            path = Path(str(ownership["path"]))
            if not path.is_absolute() or not path.is_relative_to(runtime_root):
                raise BootstrapError("adoption abort ownership leaves runtime root")
            if ownership["kind"] == "file":
                if not (path.exists() or path.is_symlink()):
                    continue
                if path in linked_destinations:
                    continue
                current = _adoption_file_ownership(path)
                if current != ownership:
                    raise BootstrapError("adoption-owned file changed before abort")
            elif ownership["kind"] == "install-staging":
                if not (path.exists() or path.is_symlink()):
                    continue
                if path in linked_temporaries:
                    continue
                identity_record = _quarantine_identity(path)
                if (
                    identity_record.get("kind") != "file"
                    or identity_record.get("mode") != ownership["mode"]
                    or int(identity_record.get("size", -1)) > 64 * 1024 * 1024
                ):
                    raise BootstrapError("adoption staging changed before abort")
            elif ownership["kind"] == "tree":
                if not (path.exists() or path.is_symlink()):
                    continue
                current = _adoption_tree_identity(path)
                if current != ownership:
                    raise BootstrapError("adoption-owned tree changed before abort")
            elif ownership["kind"] == "staging-tree":
                if not (path.exists() or path.is_symlink()):
                    continue
                _validate_adoption_staging_tree(
                    path,
                    ownership,
                    allow_missing_entries=True,
                    allow_partial_entries=True,
                )
            else:
                if not (path.exists() or path.is_symlink()):
                    continue
                metadata = path.lstat()
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or path.is_symlink()
                    or metadata.st_uid != os.geteuid()
                    or format(stat.S_IMODE(metadata.st_mode), "04o")
                    != ownership["mode"]
                ):
                    raise BootstrapError("adoption-owned directory changed before abort")
                if any(entry not in owned_paths for entry in path.iterdir()):
                    raise BootstrapError(
                        f"adoption-owned directory is not empty during abort: {path}"
                    )
        evidence = transaction["step_evidence"]
        if not isinstance(evidence, dict):
            raise BootstrapError("adoption transaction evidence is invalid")
        quarantine = evidence.get("abort_quarantine")
        if quarantine is None:
            quarantine = _adoption_quarantine_plan(
                ownership_by_path,
                transaction_path=transaction_path,
                linked_install_residues=linked_install_residues,
            )
            transaction = _write_adoption_transaction(
                transaction_path,
                {
                    **transaction,
                    "step_evidence": {**evidence, "abort_quarantine": quarantine},
                },
            )
        transaction = _reseal_adoption_transaction(transaction_path, transaction)
        _resume_adoption_quarantine(
            quarantine, transaction_path=transaction_path
        )
        lock_metadata = lock_path.lstat()
        if (
            identity.get("deploy_lock_disposition")
            != ADOPTION_DEPLOY_LOCK_DISPOSITION
            or not stat.S_ISREG(lock_metadata.st_mode)
            or lock_path.is_symlink()
            or lock_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(lock_metadata.st_mode) != 0o600
        ):
            raise BootstrapError("permanent adoption deploy lock is unsafe")
        terminal = _write_adoption_transaction(
            transaction_path,
            {
                **transaction,
                "status": "aborted",
                "phase": "aborted",
                "aborted_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        )
    return {
        "status": "aborted",
        "operation_id": operation_id,
        "adoption_transaction": terminal,
    }


def _bootstrap_transaction_path(
    runtime_root: Path,
    *,
    operation_id: str,
    source_sha: str,
) -> Path:
    if TAKEOVER_OPERATION_RE.fullmatch(operation_id) is None:
        raise BootstrapError("legacy takeover operation ID is invalid")
    if SHA_RE.fullmatch(source_sha) is None:
        raise BootstrapError("bootstrap transaction source SHA is invalid")
    return (
        runtime_root
        / BOOTSTRAP_TRANSACTION_RELATIVE_DIRECTORY
        / f"{operation_id}-{source_sha}.json"
    )


def _ensure_bootstrap_transaction_directory(runtime_root: Path) -> Path:
    """Create only the journal path that legacy control restore never removes."""

    for relative in (
        Path("state"),
        Path("state/legacy-takeover"),
        BOOTSTRAP_TRANSACTION_RELATIVE_DIRECTORY,
    ):
        path = runtime_root / relative
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise BootstrapError(
                f"bootstrap transaction directory is unavailable: {path}"
            ) from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise BootstrapError(
                f"bootstrap transaction directory is unsafe: {path}"
            )
        # A mkdir is not durable merely because its child can be opened.  Seal
        # every component and the directory entry in its parent, including on
        # replay after a parent-fsync response was lost.
        _durability_barrier(
            path,
            payload=None,
            mode=0o700,
            directory=True,
        )
    return runtime_root / BOOTSTRAP_TRANSACTION_RELATIVE_DIRECTORY


def _reseal_bootstrap_transaction(path: Path) -> dict[str, object]:
    """Durably bind an existing journal before trusting its mutation intent."""

    payload = _durability_barrier(path, payload=None, mode=0o600)
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BootstrapError("bootstrap child transaction is invalid JSON") from exc
    if not isinstance(document, dict):
        raise BootstrapError("bootstrap child transaction is not a JSON object")
    transaction = _validate_bootstrap_transaction(document, path=path)
    canonical = json.dumps(
        transaction,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8") + b"\n"
    if payload != canonical:
        raise BootstrapError("bootstrap child transaction is not canonical")
    return transaction


def _validate_bootstrap_transaction(
    document: dict[str, object],
    *,
    path: Path,
) -> dict[str, object]:
    required = {
        "schema_version",
        "status",
        "phase",
        "operation_id",
        "source_sha",
        "source_tree",
        "identity",
        "identity_sha256",
        "prepared_at",
        "created_at",
        "updated_at",
        "step_evidence",
    }
    optional = {
        "abort_authority",
        "restored_terminal_sha256",
        "aborted_at",
    }
    if (
        not required.issubset(document)
        or not set(document).issubset(required | optional)
        or document.get("schema_version")
        != BOOTSTRAP_TRANSACTION_SCHEMA_VERSION
        or document.get("status")
        not in {"in-progress", "completed", "aborting", "aborted"}
        or not isinstance(document.get("identity"), dict)
        or _canonical_json_digest(document["identity"])
        != document.get("identity_sha256")
        or not isinstance(document.get("step_evidence"), dict)
    ):
        raise BootstrapError("bootstrap child transaction has an invalid shape")
    operation_id = document.get("operation_id")
    source_sha = document.get("source_sha")
    source_tree = document.get("source_tree")
    if (
        not isinstance(operation_id, str)
        or TAKEOVER_OPERATION_RE.fullmatch(operation_id) is None
        or not isinstance(source_sha, str)
        or SHA_RE.fullmatch(source_sha) is None
        or not isinstance(source_tree, str)
        or SHA_RE.fullmatch(source_tree) is None
        or path
        != _bootstrap_transaction_path(
            path.parents[3],
            operation_id=operation_id,
            source_sha=source_sha,
        )
    ):
        raise BootstrapError("bootstrap child transaction identity is invalid")
    status = document["status"]
    phase = document["phase"]
    if status in {"in-progress", "completed"}:
        if phase not in BOOTSTRAP_PHASES:
            raise BootstrapError("bootstrap child transaction phase is invalid")
        if (status == "completed") != (phase == "completed"):
            raise BootstrapError(
                "bootstrap child transaction completion is inconsistent"
            )
        if any(name in document for name in optional):
            raise BootstrapError(
                "active bootstrap transaction contains abort authority"
            )
    else:
        if phase not in {"abort-intent", "aborted"}:
            raise BootstrapError("bootstrap abort phase is invalid")
        abort_authority = document.get("abort_authority")
        if not isinstance(abort_authority, dict):
            raise BootstrapError("bootstrap abort authority is missing")
        if (status == "aborted") != (phase == "aborted"):
            raise BootstrapError("bootstrap abort completion is inconsistent")
        restored = document.get("restored_terminal_sha256")
        if status == "aborted":
            if (
                not isinstance(restored, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", restored) is None
                or not isinstance(document.get("aborted_at"), str)
            ):
                raise BootstrapError(
                    "bootstrap abort terminal authority is invalid"
                )
        elif restored is not None or "aborted_at" in document:
            raise BootstrapError(
                "nonterminal bootstrap abort has terminal authority"
            )
    return document


def _load_or_create_bootstrap_transaction(
    runtime_root: Path,
    *,
    operation_id: str,
    source_sha: str,
    source_tree: str,
    identity: dict[str, object],
) -> tuple[Path, dict[str, object]]:
    path = _bootstrap_transaction_path(
        runtime_root,
        operation_id=operation_id,
        source_sha=source_sha,
    )
    if path.exists() or path.is_symlink():
        # Reseal the already-visible intent before any resumable build or
        # publication is allowed to mutate operation-owned staging.
        transaction = _reseal_bootstrap_transaction(path)
        _ensure_bootstrap_transaction_directory(runtime_root)
        transaction = _reseal_bootstrap_transaction(path)
        if (
            transaction["operation_id"] != operation_id
            or transaction["source_sha"] != source_sha
            or transaction["source_tree"] != source_tree
            or transaction["identity"] != identity
            or transaction["identity_sha256"]
            != _canonical_json_digest(identity)
        ):
            raise BootstrapError(
                "existing bootstrap child transaction has different authority"
            )
        if transaction["status"] in {"aborting", "aborted"}:
            raise BootstrapError(
                "aborted bootstrap transaction cannot be resumed"
            )
        return path, transaction
    directory = _ensure_bootstrap_transaction_directory(runtime_root)
    prepared_at = dt.datetime.now(dt.timezone.utc).isoformat()
    transaction = {
        "schema_version": BOOTSTRAP_TRANSACTION_SCHEMA_VERSION,
        "status": "in-progress",
        "phase": "intent",
        "operation_id": operation_id,
        "source_sha": source_sha,
        "source_tree": source_tree,
        "identity": identity,
        "identity_sha256": _canonical_json_digest(identity),
        "prepared_at": prepared_at,
        "created_at": prepared_at,
        "updated_at": prepared_at,
        "step_evidence": {},
    }
    _atomic_json(path, transaction)
    _fsync_directory(directory)
    return path, _reseal_bootstrap_transaction(path)


def _begin_bootstrap_step(
    path: Path,
    transaction: dict[str, object],
    *,
    previous_phase: str,
    intent_phase: str,
) -> dict[str, object]:
    transaction = _reseal_bootstrap_transaction(path)
    if transaction["status"] == "completed":
        return transaction
    if transaction["status"] != "in-progress":
        raise BootstrapError("bootstrap transaction is not resumable")
    current = str(transaction["phase"])
    current_index = BOOTSTRAP_PHASES.index(current)
    intent_index = BOOTSTRAP_PHASES.index(intent_phase)
    if current_index >= intent_index:
        return transaction
    if current != previous_phase or intent_index != current_index + 1:
        raise BootstrapError("bootstrap transaction phase is discontinuous")
    transaction["phase"] = intent_phase
    transaction["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    _atomic_json(path, transaction)
    return _reseal_bootstrap_transaction(path)


def _complete_bootstrap_step(
    path: Path,
    transaction: dict[str, object],
    *,
    intent_phase: str,
    ready_phase: str,
    evidence_name: str,
    evidence: object,
) -> dict[str, object]:
    transaction = _reseal_bootstrap_transaction(path)
    step_evidence = dict(transaction["step_evidence"])
    if transaction["status"] == "completed" or (
        transaction["phase"] in BOOTSTRAP_PHASES
        and BOOTSTRAP_PHASES.index(str(transaction["phase"]))
        >= BOOTSTRAP_PHASES.index(ready_phase)
    ):
        if step_evidence.get(evidence_name) != evidence:
            raise BootstrapError(
                f"bootstrap {evidence_name} evidence changed on resume"
            )
        return transaction
    if (
        transaction["status"] != "in-progress"
        or transaction["phase"] != intent_phase
    ):
        raise BootstrapError("bootstrap transaction step cannot commit")
    step_evidence[evidence_name] = evidence
    transaction["step_evidence"] = step_evidence
    transaction["phase"] = ready_phase
    transaction["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    _atomic_json(path, transaction)
    return _reseal_bootstrap_transaction(path)


def _complete_bootstrap_transaction(
    path: Path,
    *,
    evidence: object,
) -> dict[str, object]:
    transaction = _reseal_bootstrap_transaction(path)
    step_evidence = dict(transaction["step_evidence"])
    if transaction["status"] == "completed":
        if step_evidence.get("authority_commit") != evidence:
            raise BootstrapError(
                "bootstrap authority commit evidence changed on resume"
            )
        return transaction
    if (
        transaction["status"] != "in-progress"
        or transaction["phase"] != "authority-commit-intent"
    ):
        raise BootstrapError("bootstrap authority transaction cannot commit")
    step_evidence["authority_commit"] = evidence
    transaction["step_evidence"] = step_evidence
    transaction["phase"] = "completed"
    transaction["status"] = "completed"
    transaction["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    _atomic_json(path, transaction)
    return _reseal_bootstrap_transaction(path)


def _sealed_bootstrap_delivery_gate(
    runtime_root: Path, *, source_sha: str, source_tree: str
) -> dict[str, object] | None:
    path = runtime_root / "state/bootstrap-control.json"
    if not path.exists() and not path.is_symlink():
        return None
    record = _load_private_json(path)
    gate = record.get("delivery_gate")
    if (
        record.get("schema_version") not in {2, 3}
        or record.get("status") not in {"prepared", "completed"}
        or record.get("source_sha") != source_sha
        or record.get("source_tree") != source_tree
        or not isinstance(gate, dict)
    ):
        raise BootstrapError("existing bootstrap delivery authority is invalid")
    return dict(gate)


def _write_authorized_staging(path: Path, payload: bytes, mode: int) -> None:
    exists = path.exists() or path.is_symlink()
    flags = (
        os.O_WRONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    flags |= 0 if exists else os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, mode)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_nlink != 1
            or before.st_size > len(payload)
            or _inode_identity(path.lstat()) != _inode_identity(before)
        ):
            raise BootstrapError("authorized staging inode is unsafe")
        os.ftruncate(descriptor, 0)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise BootstrapError("authorized staging write failed")
            written += count
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        if (
            after.st_size != len(payload)
            or _inode_identity(path.lstat()) != _inode_identity(after)
        ):
            raise BootstrapError("authorized staging inode changed")
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _read_held_file(descriptor: int, size: int) -> bytes:
    if size > 64 * 1024 * 1024:
        raise BootstrapError("authorized install inode is too large")
    payload = bytearray()
    while len(payload) < size:
        chunk = os.pread(
            descriptor,
            min(1024 * 1024, size - len(payload)),
            len(payload),
        )
        if not chunk:
            raise BootstrapError("authorized install inode changed while reading")
        payload.extend(chunk)
    return bytes(payload)


def _authorized_install_link_identity(
    destination: Path,
    temporary: Path,
    *,
    payload: bytes,
    mode: int,
    temporary_authority: dict[str, object],
    remove_temporary: bool,
) -> dict[str, object]:
    """Validate and optionally collapse the exact journal-owned link pair."""

    authority = _validate_adoption_ownership(
        temporary_authority, label="linked install staging"
    )
    if (
        destination.parent != temporary.parent
        or authority.get("kind") != "install-staging"
        or authority.get("purpose") != "install"
        or authority.get("path") != str(temporary)
        or authority.get("destination") != str(destination)
        or authority.get("sha256") != digest(payload)
        or authority.get("mode") != format(mode, "04o")
    ):
        raise BootstrapError("linked install staging authority differs")
    parent_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    parent_descriptor = os.open(destination.parent, parent_flags)
    destination_descriptor = -1
    temporary_descriptor = -1
    try:
        held_parent = os.fstat(parent_descriptor)
        bound_parent = destination.parent.lstat()
        if (
            not stat.S_ISDIR(held_parent.st_mode)
            or held_parent.st_uid != os.geteuid()
            or (held_parent.st_dev, held_parent.st_ino)
            != (bound_parent.st_dev, bound_parent.st_ino)
        ):
            raise BootstrapError("linked install parent changed")
        destination_descriptor = os.open(
            destination.name, file_flags, dir_fd=parent_descriptor
        )
        temporary_descriptor = os.open(
            temporary.name, file_flags, dir_fd=parent_descriptor
        )
        held_destination = os.fstat(destination_descriptor)
        held_temporary = os.fstat(temporary_descriptor)
        bound_destination = os.stat(
            destination.name, dir_fd=parent_descriptor, follow_symlinks=False
        )
        bound_temporary = os.stat(
            temporary.name, dir_fd=parent_descriptor, follow_symlinks=False
        )
        if (
            not stat.S_ISREG(held_destination.st_mode)
            or held_destination.st_uid != os.geteuid()
            or stat.S_IMODE(held_destination.st_mode) != mode
            or held_destination.st_nlink != 2
            or (held_destination.st_dev, held_destination.st_ino)
            != (held_temporary.st_dev, held_temporary.st_ino)
            or _inode_identity(bound_destination)
            != _inode_identity(held_destination)
            or _inode_identity(bound_temporary) != _inode_identity(held_temporary)
            or _read_held_file(destination_descriptor, held_destination.st_size)
            != payload
        ):
            raise BootstrapError("linked install inode differs from durable authority")
        os.fsync(destination_descriptor)
        os.fsync(parent_descriptor)
        sealed_destination = os.fstat(destination_descriptor)
        sealed_temporary = os.fstat(temporary_descriptor)
        if (
            _inode_identity(sealed_destination)
            != _inode_identity(held_destination)
            or _inode_identity(sealed_temporary) != _inode_identity(held_temporary)
            or _inode_identity(
                os.stat(
                    destination.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            )
            != _inode_identity(sealed_destination)
            or _inode_identity(
                os.stat(
                    temporary.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            )
            != _inode_identity(sealed_temporary)
        ):
            raise BootstrapError("linked install bindings changed before collapse")
        identity = {
            "device": sealed_destination.st_dev,
            "inode": sealed_destination.st_ino,
            "sha256": digest(payload),
            "mode": format(mode, "04o"),
            "size": len(payload),
        }
        if remove_temporary:
            os.unlink(temporary.name, dir_fd=parent_descriptor)
            collapsed = os.fstat(destination_descriptor)
            rebound = os.stat(
                destination.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                collapsed.st_nlink != 1
                or (collapsed.st_dev, collapsed.st_ino)
                != (identity["device"], identity["inode"])
                or _inode_identity(rebound) != _inode_identity(collapsed)
            ):
                raise BootstrapError("linked install collapse changed destination")
            try:
                os.stat(
                    temporary.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise BootstrapError("linked install temporary remained published")
            os.fsync(destination_descriptor)
            os.fsync(parent_descriptor)
            final = os.fstat(destination_descriptor)
            if (
                final.st_nlink != 1
                or _read_held_file(destination_descriptor, final.st_size)
                != payload
                or _inode_identity(
                    os.stat(
                        destination.name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                )
                != _inode_identity(final)
            ):
                raise BootstrapError("linked install collapse is not durable")
        return identity
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        os.close(parent_descriptor)


def _remove_exact_single_link(
    path: Path, *, payload: bytes, mode: int, label: str
) -> None:
    parent_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    parent_descriptor = os.open(path.parent, parent_flags)
    descriptor = -1
    try:
        parent = os.fstat(parent_descriptor)
        bound_parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or (parent.st_dev, parent.st_ino)
            != (bound_parent.st_dev, bound_parent.st_ino)
        ):
            raise BootstrapError(f"{label} parent changed")
        descriptor = os.open(path.name, file_flags, dir_fd=parent_descriptor)
        held = os.fstat(descriptor)
        bound = os.stat(
            path.name, dir_fd=parent_descriptor, follow_symlinks=False
        )
        if (
            not stat.S_ISREG(held.st_mode)
            or held.st_uid != os.geteuid()
            or stat.S_IMODE(held.st_mode) != mode
            or held.st_nlink != 1
            or _inode_identity(bound) != _inode_identity(held)
            or _read_held_file(descriptor, held.st_size) != payload
        ):
            raise BootstrapError(f"{label} differs")
        os.fsync(descriptor)
        os.fsync(parent_descriptor)
        sealed = os.fstat(descriptor)
        if (
            _inode_identity(sealed) != _inode_identity(held)
            or _inode_identity(
                os.stat(
                    path.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            )
            != _inode_identity(sealed)
        ):
            raise BootstrapError(f"{label} binding changed")
        os.unlink(path.name, dir_fd=parent_descriptor)
        os.fsync(descriptor)
        os.fsync(parent_descriptor)
        try:
            os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise BootstrapError(f"{label} remained published")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def _install_exact(
    path: Path,
    payload: bytes,
    mode: int,
    *,
    temporary_path: Path | None = None,
    temporary_authority: dict[str, object] | None = None,
    authorized_staging_siblings: tuple[
        tuple[Path, dict[str, object]], ...
    ] = (),
    reject_unowned_staging: bool = False,
) -> str:
    # A prior crash can leave only a private staging name, or both a complete
    # hard-linked destination and its staging name.  It must never leave a
    # truncated final path.  The parent is deploy-user-owned mode 0700.
    staging = list(path.parent.glob(f".{path.name}.*.tmp"))
    authorized_siblings: set[Path] = set()
    for sibling, raw_authority in authorized_staging_siblings:
        sibling_authority = _validate_adoption_ownership(
            raw_authority, label="immutable install sibling staging"
        )
        if (
            sibling.parent != path.parent
            or sibling_authority.get("kind") != "install-staging"
            or sibling_authority.get("purpose") != "cas"
            or sibling_authority.get("path") != str(sibling)
            or sibling_authority.get("destination") != str(path)
        ):
            raise BootstrapError("immutable install sibling authority differs")
        authorized_siblings.add(sibling)
    if temporary_path is None:
        if any(value not in authorized_siblings for value in staging):
            raise BootstrapError("immutable install has unowned staging files")
    else:
        authority = _validate_adoption_ownership(
            temporary_authority, label="immutable install staging"
        )
        if (
            temporary_path.parent != path.parent
            or not temporary_path.name.startswith(f".{path.name}.")
            or not temporary_path.name.endswith(".tmp")
            or any(
                value != temporary_path and value not in authorized_siblings
                for value in staging
            )
            or authority.get("kind") != "install-staging"
            or authority.get("purpose") != "install"
            or authority.get("path") != str(temporary_path)
            or authority.get("destination") != str(path)
            or authority.get("sha256") != digest(payload)
            or authority.get("mode") != format(mode, "04o")
        ):
            raise BootstrapError("immutable install has foreign staging files")
    if path.exists() or path.is_symlink():
        try:
            existing = _durability_barrier(path, payload=payload, mode=mode)
        except (OSError, BootstrapError) as exc:
            raise BootstrapError(
                f"refusing to overwrite a different immutable file: {path}"
            ) from exc
        if temporary_path is not None and (
            temporary_path.exists() or temporary_path.is_symlink()
        ):
            assert temporary_authority is not None
            _authorized_install_link_identity(
                path,
                temporary_path,
                payload=payload,
                mode=mode,
                temporary_authority=temporary_authority,
                remove_temporary=True,
            )
            _durability_barrier(path, payload=payload, mode=mode)
        return digest(existing)
    temporary = temporary_path or (
        path.parent / f".{path.name}.{os.urandom(12).hex()}.tmp"
    )
    random_inode: tuple[int, int] | None = None

    def cleanup_random_inode() -> None:
        if random_inode is None:
            return
        try:
            current = temporary.lstat()
            if (current.st_dev, current.st_ino) == random_inode:
                temporary.unlink()
                _fsync_directory(path.parent)
        except OSError:
            pass

    if temporary_path is not None:
        _write_authorized_staging(temporary, payload, mode)
    elif not (temporary.exists() or temporary.is_symlink()):
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        try:
            created = os.fstat(descriptor)
            random_inode = (created.st_dev, created.st_ino)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fchmod(stream.fileno(), mode)
                os.fsync(stream.fileno())
        except BaseException:
            cleanup_random_inode()
            raise
    try:
        # link(2) is an atomic no-replace publication.  A concurrent or stale
        # destination cannot be overwritten as os.replace() would do.
        os.link(temporary, path, follow_symlinks=False)
        if temporary_path is not None:
            assert temporary_authority is not None
            _authorized_install_link_identity(
                path,
                temporary,
                payload=payload,
                mode=mode,
                temporary_authority=temporary_authority,
                remove_temporary=True,
            )
        else:
            _durability_barrier(path, payload=payload, mode=mode)
            temporary.unlink()
            _fsync_directory(path.parent)
        _durability_barrier(path, payload=payload, mode=mode)
    except BaseException:
        cleanup_random_inode()
        raise
    return digest(payload)


def _cas_replace_exact_file(
    path: Path,
    *,
    expected_payload: bytes,
    replacement_payload: bytes,
    mode: int,
    temporary_path: Path,
    temporary_authority: dict[str, object],
) -> None:
    """Exchange exact expected bytes without overwriting a raced destination."""

    expected = _adoption_file_plan(path, expected_payload, mode)
    replacement = _adoption_file_plan(path, replacement_payload, mode)
    temporary_expected = _adoption_file_plan(
        temporary_path, expected_payload, mode
    )
    authority = _validate_adoption_ownership(
        temporary_authority, label="authority CAS staging"
    )
    if (
        authority.get("kind") != "install-staging"
        or authority.get("purpose") != "cas"
        or authority.get("path") != str(temporary_path)
        or authority.get("destination") != str(path)
        or authority.get("sha256") != digest(replacement_payload)
        or authority.get("mode") != format(mode, "04o")
        or any(
            value != temporary_path
            for value in path.parent.glob(f".{path.name}.*.tmp")
        )
    ):
        raise BootstrapError("authority CAS staging authority differs")
    current = _adoption_file_ownership(path)
    if current == replacement:
        _durability_barrier(path, payload=replacement_payload, mode=mode)
        if temporary_path.exists() or temporary_path.is_symlink():
            _remove_exact_single_link(
                temporary_path,
                payload=expected_payload,
                mode=mode,
                label="authority CAS residue",
            )
            _durability_barrier(path, payload=replacement_payload, mode=mode)
        return
    if current != expected:
        raise BootstrapError("authority CAS precondition differs")
    _write_authorized_staging(temporary_path, replacement_payload, mode)
    pinned = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        pinned_metadata = os.fstat(pinned)
        pinned_payload = bytearray()
        while len(pinned_payload) < pinned_metadata.st_size:
            chunk = os.read(
                pinned,
                min(1024 * 1024, pinned_metadata.st_size - len(pinned_payload)),
            )
            if not chunk:
                raise BootstrapError("authority CAS precondition changed")
            pinned_payload.extend(chunk)
        if (
            not stat.S_ISREG(pinned_metadata.st_mode)
            or pinned_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(pinned_metadata.st_mode) != mode
            or bytes(pinned_payload) != expected_payload
        ):
            raise BootstrapError("authority CAS precondition differs")
        _rename_exchange(path, temporary_path)
        swapped = temporary_path.lstat()
        if (
            swapped.st_dev,
            swapped.st_ino,
            swapped.st_mode,
            swapped.st_uid,
            swapped.st_size,
        ) != (
            pinned_metadata.st_dev,
            pinned_metadata.st_ino,
            pinned_metadata.st_mode,
            pinned_metadata.st_uid,
            pinned_metadata.st_size,
        ):
            _rename_exchange(path, temporary_path)
            raise BootstrapError("authority CAS destination changed before exchange")
    finally:
        os.close(pinned)
    if _adoption_file_ownership(temporary_path) != temporary_expected:
        _rename_exchange(path, temporary_path)
        raise BootstrapError("authority CAS exchanged unexpected bytes")
    _remove_exact_single_link(
        temporary_path,
        payload=expected_payload,
        mode=mode,
        label="authority CAS exchanged residue",
    )
    _durability_barrier(path, payload=replacement_payload, mode=mode)


def _git_layout(root: Path) -> tuple[Path, Path]:
    root = root.absolute()
    marker = root / ".git"
    try:
        metadata = marker.lstat()
    except OSError as exc:
        raise BootstrapError(f"Git metadata marker is unavailable: {marker}") from exc
    if stat.S_ISDIR(metadata.st_mode) and not marker.is_symlink():
        git_dir = marker
    elif stat.S_ISREG(metadata.st_mode) and not marker.is_symlink():
        try:
            payload = marker.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise BootstrapError("Git worktree marker is unreadable") from exc
        if len(payload) > 4096 or not payload.startswith("gitdir: "):
            raise BootstrapError("Git worktree marker is invalid")
        raw = payload.removeprefix("gitdir: ").strip()
        candidate = Path(raw)
        git_dir = candidate if candidate.is_absolute() else marker.parent / candidate
        git_dir = git_dir.resolve(strict=True)
    else:
        raise BootstrapError("Git metadata marker is unsafe")
    try:
        git_metadata = git_dir.lstat()
    except OSError as exc:
        raise BootstrapError("resolved Git directory is unavailable") from exc
    if (
        metadata.st_uid != os.geteuid()
        or not stat.S_ISDIR(git_metadata.st_mode)
        or git_dir.is_symlink()
        or git_metadata.st_uid != os.geteuid()
    ):
        raise BootstrapError("Git metadata is not deploy-user controlled")
    return git_dir.absolute(), root


def _git_environment(root: Path, *, home: str) -> dict[str, str]:
    git_dir, work_tree = _git_layout(root)
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": home,
        "GIT_DIR": str(git_dir),
        "GIT_WORK_TREE": str(work_tree),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_ASKPASS": "/bin/false",
        "GIT_SSH_COMMAND": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
        "GIT_PAGER": "cat",
        "GIT_EDITOR": "/bin/false",
    }


def _git_command(root: Path, *arguments: str) -> list[str]:
    return [
        "/usr/bin/git",
        "-c",
        "credential.helper=",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "protocol.allow=never",
        "-c",
        f"core.worktree={root.absolute()}",
        *arguments,
    ]


def _validate_local_git_config(
    payload: bytes,
    *,
    label: str,
    allow_runtime_overrides: bool,
) -> None:
    """Reject local Git policy that can execute code or redirect identity."""

    if len(payload) > 1024 * 1024:
        raise BootstrapError(f"{label} is unexpectedly large")
    parser = configparser.RawConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(payload.decode("utf-8"))
    except (UnicodeError, configparser.Error) as exc:
        raise BootstrapError(f"{label} is malformed") from exc
    allowed: dict[str, set[str]] = {
        "core": {
            "repositoryformatversion",
            "filemode",
            "bare",
            "logallrefupdates",
            "ignorecase",
            "precomposeunicode",
        },
        'remote "origin"': {"url", "fetch", "tagopt"},
        # VS Code records only a ref name here; Git does not execute it.
        'branch "main"': {"remote", "merge", "vscode-merge-base"},
        "user": {"name", "email"},
    }
    if allow_runtime_overrides:
        # The legacy production checkout may contain these values. Every
        # takeover Git call overrides them before the files are sealed.
        allowed["core"].update({"worktree", "fsmonitor", "untrackedcache"})
    for section in parser.sections():
        normalized = section.lower()
        permitted = allowed.get(normalized)
        if permitted is None:
            raise BootstrapError(f"{label} contains unsupported section {section}")
        keys = {key.lower() for key, _value in parser.items(section, raw=True)}
        if not keys.issubset(permitted):
            raise BootstrapError(f"{label} contains executable or unsupported policy")


def _validate_git_attributes(payload: bytes, *, label: str) -> None:
    if len(payload) > 1024 * 1024:
        raise BootstrapError(f"{label} is unexpectedly large")
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise BootstrapError(f"{label} is malformed") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = stripped.split()
        if any(
            token.lower().lstrip("-!").startswith("filter")
            for token in tokens[1:]
        ):
            raise BootstrapError(f"{label} contains an executable clean filter")


def _git_policy_paths(root: Path) -> list[Path]:
    git = root / ".git"
    paths = [git / "config"]
    for optional in (git / "config.worktree", git / "info/attributes"):
        if optional.exists() or optional.is_symlink():
            paths.append(optional)
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        if current == git:
            names[:] = []
            continue
        if ".git" in names:
            names.remove(".git")
        if ".gitattributes" in files:
            paths.append(current / ".gitattributes")
    return paths


def _assert_standalone_git_storage(root: Path) -> None:
    """Reject Git object/config authority outside the reviewed clone boundary."""

    for relative in GIT_EXTERNAL_STORAGE_MARKERS:
        path = root / relative
        if path.exists() or path.is_symlink():
            raise BootstrapError(
                f"Git repository uses forbidden external storage: {relative}"
            )
    objects = root / ".git/objects"
    try:
        objects_metadata = objects.lstat()
    except OSError as exc:
        raise BootstrapError("Git object database is unavailable") from exc
    if (
        not stat.S_ISDIR(objects_metadata.st_mode)
        or objects.is_symlink()
        or objects_metadata.st_uid != os.geteuid()
    ):
        raise BootstrapError("Git object database is unsafe")
    for directory, names, files in os.walk(objects, followlinks=False):
        current = Path(directory)
        metadata = current.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or current.is_symlink()
            or metadata.st_uid != os.geteuid()
        ):
            raise BootstrapError(f"Git object directory is unsafe: {current}")
        for name in (*names, *files):
            child = current / name
            metadata = child.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or not (
                    stat.S_ISDIR(metadata.st_mode)
                    or stat.S_ISREG(metadata.st_mode)
                )
            ):
                raise BootstrapError(f"Git object entry is unsafe: {child}")
            if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
                raise BootstrapError(
                    f"Git object database is hard-linked outside the clone: {child}"
                )


def _verify_git_object_database(root: Path) -> None:
    try:
        subprocess.run(
            _git_command(
                root,
                "fsck",
                "--full",
                "--strict",
                "--no-reflogs",
                "--no-dangling",
            ),
            cwd=root,
            env=_git_environment(root, home=os.environ.get("HOME", "")),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BootstrapError("Git object database failed strict verification") from exc


def bootstrap_source_readiness(
    root: Path,
    *,
    expected_sha: str | None = None,
) -> dict[str, object]:
    """Prove that ``root`` is a standalone, immutable bootstrap authority.

    This check is deliberately read-only.  In particular, it does not fetch,
    repack, expire reflogs, run maintenance, or remove unreachable objects.
    A clone with unreachable objects is rejected so a later bootstrap cannot
    silently depend on reflog-only or otherwise unreviewed bytes.
    """

    root = root.absolute()
    _assert_private_bootstrap_source(root)
    shallow = root / ".git/shallow"
    if shallow.exists() or shallow.is_symlink():
        raise BootstrapError("bootstrap source clone must not be shallow")
    _verify_git_object_database(root)
    environment = _git_environment(root, home=os.environ.get("HOME", ""))

    def git(*arguments: str, text: bool = True) -> str | bytes:
        result = subprocess.run(
            _git_command(root, *arguments),
            cwd=root,
            env=environment,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
        )
        return result.stdout

    try:
        branch = str(git("symbolic-ref", "--short", "HEAD")).strip()
        remote_names = str(git("remote")).splitlines()
        origin_fetch_urls = str(
            git("remote", "get-url", "--all", "origin")
        ).splitlines()
        origin_push_urls = str(
            git("remote", "get-url", "--push", "--all", "origin")
        ).splitlines()
        source_sha = str(git("rev-parse", "HEAD")).strip()
        source_tree = str(git("rev-parse", "HEAD^{tree}")).strip()
        local_main = str(git("rev-parse", "refs/heads/main")).strip()
        origin_main = str(
            git("rev-parse", "refs/remotes/origin/main")
        ).strip()
        replace_refs = str(
            git(
                "for-each-ref",
                "--format=%(refname)",
                "refs/replace/",
            )
        ).splitlines()
        index_entries = bytes(
            git("ls-files", "--sparse", "-v", "-z", text=False)
        ).split(b"\0")
        dirty = str(
            git(
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignore-submodules=none",
            )
        )
        ignored = bytes(
            git(
                "ls-files",
                "-z",
                "--others",
                "--ignored",
                "--exclude-standard",
                text=False,
            )
        )
        unreachable = subprocess.run(
            _git_command(
                root,
                "fsck",
                "--full",
                "--strict",
                "--no-reflogs",
                "--unreachable",
            ),
            cwd=root,
            env=environment,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BootstrapError("cannot establish bootstrap source readiness") from exc
    if branch != "main" or local_main != source_sha or origin_main != source_sha:
        raise BootstrapError("bootstrap source must be the exact local main checkout")
    if (
        remote_names != ["origin"]
        or origin_fetch_urls != [REPOSITORY_SSH_URL]
        or origin_push_urls != [REPOSITORY_SSH_URL]
    ):
        raise BootstrapError(
            "bootstrap source must use one canonical deploy-key SSH remote"
        )
    if (
        SHA_RE.fullmatch(source_sha) is None
        or SHA_RE.fullmatch(source_tree) is None
        or expected_sha is not None
        and source_sha != expected_sha
    ):
        raise BootstrapError("bootstrap source commit identity differs")
    if dirty:
        raise BootstrapError("bootstrap source contains tracked or untracked changes")
    if ignored:
        raise BootstrapError("bootstrap source contains ignored paths")
    if replace_refs:
        raise BootstrapError("bootstrap source contains Git replacement refs")
    special_index_entries = [
        entry
        for entry in index_entries
        if entry and not entry.startswith(b"H ")
    ]
    if special_index_entries:
        raise BootstrapError(
            "bootstrap source index contains sparse or hidden entries"
        )
    unreachable_output = unreachable.stdout + unreachable.stderr
    if unreachable_output.strip():
        raise BootstrapError(
            "bootstrap source contains dangling or unreachable Git objects"
        )
    return {
        "schema_version": 2,
        "ready": True,
        "source_root": str(root),
        "source_sha": source_sha,
        "source_tree": source_tree,
        "branch": branch,
        "origin": origin_fetch_urls[0],
        "remote_names": remote_names,
        "origin_fetch_urls": origin_fetch_urls,
        "origin_push_urls": origin_push_urls,
        "origin_main_sha": origin_main,
        "standalone_object_database": True,
        "shallow": False,
        "dirty_entries": 0,
        "ignored_entries": 0,
        "unreachable_objects": 0,
        "replace_refs": 0,
        "special_index_entries": 0,
        "sparse_index": False,
        "owner_private": True,
        "group_or_world_writable": False,
    }


def _read_git_policy(
    path: Path,
    *,
    root: Path,
    allow_runtime_overrides: bool = False,
) -> tuple[bytes, int]:
    try:
        metadata = path.lstat()
        payload = path.read_bytes()
    except OSError as exc:
        raise BootstrapError(f"Git policy file is unavailable: {path}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or not path.resolve().is_relative_to(root.resolve())
    ):
        raise BootstrapError(f"Git policy file is unsafe: {path}")
    if path.name in {"config", "config.worktree"} and path.parent == root / ".git":
        _validate_local_git_config(
            payload,
            label=str(path),
            allow_runtime_overrides=allow_runtime_overrides,
        )
        return payload, 0o600
    _validate_git_attributes(payload, label=str(path))
    return payload, stat.S_IMODE(metadata.st_mode) & ~0o022


def _assert_private_bootstrap_source(root: Path) -> None:
    """Prove the standalone reviewed clone is safe before the first Git call."""

    if any(
        _paths_overlap(root, protected)
        for protected in (PRODUCTION_ROOT, RUNTIME_ROOT)
    ):
        raise BootstrapError(
            "bootstrap source clone must be independent of production/runtime roots"
        )
    try:
        parent = root.parent.lstat()
        root_metadata = root.lstat()
        git_metadata = (root / ".git").lstat()
    except OSError as exc:
        raise BootstrapError("bootstrap source clone is unavailable") from exc
    if (
        not stat.S_ISDIR(parent.st_mode)
        or root.parent.is_symlink()
        or parent.st_uid != os.geteuid()
        or parent.st_mode & 0o077
        or not stat.S_ISDIR(root_metadata.st_mode)
        or root.is_symlink()
        or root_metadata.st_uid != os.geteuid()
        or root_metadata.st_mode & 0o077
        or not stat.S_ISDIR(git_metadata.st_mode)
        or (root / ".git").is_symlink()
        or git_metadata.st_uid != os.geteuid()
        or git_metadata.st_mode & 0o077
    ):
        raise BootstrapError(
            "bootstrap source must be an owner-controlled standalone private clone"
        )
    _assert_standalone_git_storage(root)
    for directory, names, files in os.walk(root, followlinks=False):
        current = Path(directory)
        metadata = current.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or current.is_symlink()
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise BootstrapError(f"bootstrap source directory is unsafe: {current}")
        for name in (*names, *files):
            child = current / name
            metadata = child.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077
                or not (
                    stat.S_ISDIR(metadata.st_mode)
                    or stat.S_ISREG(metadata.st_mode)
                )
            ):
                raise BootstrapError(f"bootstrap source entry is unsafe: {child}")
    for path in _git_policy_paths(root):
        _read_git_policy(path, root=root)


def _git(*arguments: str, text: bool = True) -> str | bytes:
    environment = _git_environment(
        REPOSITORY_ROOT,
        home=os.environ.get("HOME", "/home/devuser"),
    )
    result = subprocess.run(
        _git_command(REPOSITORY_ROOT, *arguments),
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
    )
    return result.stdout


def _source_identity(*, allow_test: bool) -> tuple[str, str]:
    if allow_test:
        source_sha = os.environ.get("NEXPOLY_BOOTSTRAP_SOURCE_SHA", "1" * 40)
        source_tree = os.environ.get("NEXPOLY_BOOTSTRAP_SOURCE_TREE", "2" * 40)
        if SHA_RE.fullmatch(source_sha) is None or SHA_RE.fullmatch(source_tree) is None:
            raise BootstrapError("test bootstrap source identity is invalid")
        return source_sha, source_tree
    try:
        source_sha = str(_git("rev-parse", "HEAD")).strip()
        source_tree = str(_git("rev-parse", "HEAD^{tree}")).strip()
        dirty = str(_git("status", "--porcelain=v1", "--untracked-files=all"))
        ignored = bytes(
            _git(
                "ls-files",
                "-z",
                "--others",
                "--ignored",
                "--exclude-standard",
                text=False,
            )
        )
    except subprocess.SubprocessError as exc:
        raise BootstrapError("cannot establish reviewed bootstrap source identity") from exc
    if (
        SHA_RE.fullmatch(source_sha) is None
        or SHA_RE.fullmatch(source_tree) is None
        or dirty
        or ignored
    ):
        raise BootstrapError(
            "bootstrap source checkout must be clean, unignored and commit-pinned"
        )
    return source_sha, source_tree


def _read_reviewed_source(relative: str, *, source_sha: str, allow_test: bool) -> bytes:
    path = REPOSITORY_ROOT / relative
    payload = _safe_source(path)
    if not allow_test:
        try:
            reviewed = bytes(_git("show", f"{source_sha}:{relative}", text=False))
        except subprocess.SubprocessError as exc:
            raise BootstrapError(f"reviewed control source is unavailable: {relative}") from exc
        if reviewed != payload:
            raise BootstrapError(f"bootstrap source differs from reviewed Git object: {relative}")
    return payload


def _plan_control_release(
    runtime_root: Path,
    *,
    control: object,
    source_sha: str,
    source_tree: str,
    allow_test: bool,
    prepared_at: str,
) -> dict[str, object]:
    if not isinstance(prepared_at, str) or not prepared_at:
        raise BootstrapError("bootstrap control prepared timestamp is invalid")
    source_payload = _read_reviewed_source(
        "scripts/control-release.json", source_sha=source_sha, allow_test=allow_test
    )
    try:
        source_manifest = control.parse_source_manifest(source_payload)
    except Exception as exc:
        raise BootstrapError("bootstrap control manifest is invalid") from exc
    payloads: dict[str, bytes] = {}
    identities: dict[str, dict[str, object]] = {}
    for record in source_manifest["files"]:
        payload = _read_reviewed_source(
            record["source"], source_sha=source_sha, allow_test=allow_test
        )
        payloads[record["name"]] = payload
        identities[record["name"]] = {
            "sha256": digest(payload),
            "size": len(payload),
            "mode": record["mode"],
        }
    identity = {
        "schema_version": control.CONTROL_MANIFEST_SCHEMA_VERSION,
        "protocol_version": control.PROTOCOL_VERSION,
        "source_sha": source_sha,
        "source_tree": source_tree,
        "compatibility": source_manifest["compatibility"],
        "entrypoints": source_manifest["entrypoints"],
        "files": identities,
    }
    release_id = control.release_identity(identity)
    manifest = {**identity, "release_id": release_id}
    control.validate_control_manifest(manifest)
    manifest_payload = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8") + b"\n"
    release_parent = runtime_root / "control-releases"
    release = release_parent / release_id
    staging = release_parent / f".bootstrap-{release_id}"
    staging_owner = {
        "schema_version": 1,
        "source_sha": source_sha,
        "source_tree": source_tree,
        "release_id": release_id,
        "manifest_sha256": digest(manifest_payload),
    }
    operation_id = "bootstrap-controls-" + source_sha[:16]
    candidate = {
        "schema_version": control.CONTROL_CANDIDATE_SCHEMA_VERSION,
        "protocol_version": control.PROTOCOL_VERSION,
        "component": "deployment-controls",
        "release_id": release_id,
        "source_sha": source_sha,
        "source_tree": source_tree,
        "manifest_sha256": digest(manifest_payload),
        "operation_id": operation_id,
        "prepared_at": prepared_at,
    }
    control.validate_candidate_record(candidate)
    active = {
        "schema_version": control.ACTIVE_CONTROL_SCHEMA_VERSION,
        "protocol_version": control.PROTOCOL_VERSION,
        "component": "deployment-controls",
        "generation": 1,
        "release_id": release_id,
        "source_sha": source_sha,
        "source_tree": source_tree,
        "manifest_sha256": digest(manifest_payload),
        "operation_id": operation_id,
        "previous_release_id": None,
        "activated_at": prepared_at,
    }
    control.validate_active_control_record(active)
    expected_files = {
        **{
            name: {
                "sha256": digest(payload),
                "mode": "0700",
                "size": len(payload),
            }
            for name, payload in payloads.items()
        },
        control.CONTROL_MANIFEST_NAME: {
            "sha256": digest(manifest_payload),
            "mode": "0600",
            "size": len(manifest_payload),
        },
    }
    tree_records = [
        {
            "relative_path": name,
            "kind": "file",
            "mode": record["mode"],
            "sha256": record["sha256"],
        }
        for name, record in sorted(expected_files.items())
    ]
    return {
        "payloads": payloads,
        "manifest": manifest,
        "manifest_payload": manifest_payload,
        "release_id": release_id,
        "release_path": release,
        "staging_path": staging,
        "staging_owner": staging_owner,
        "expected_files": expected_files,
        "release_tree_ownership": {
            "path": str(release),
            "kind": "tree",
            "identity_sha256": _canonical_json_digest(tree_records),
        },
        "staging_tree_ownership": {
            "path": str(staging),
            "kind": "staging-tree",
            "owner_sha256": _canonical_json_digest(staging_owner),
            "owner": staging_owner,
            "files": expected_files,
        },
        "candidate": candidate,
        "active": active,
    }


def _build_control_release(
    runtime_root: Path,
    *,
    control: object,
    source_sha: str,
    source_tree: str,
    allow_test: bool,
    prepared_at: str | None = None,
    plan: dict[str, object] | None = None,
    staging_authorized: bool = False,
) -> tuple[dict[str, object], dict[str, object]]:
    if prepared_at is None:
        prepared_at = dt.datetime.now(dt.timezone.utc).isoformat()
    if plan is None:
        plan = _plan_control_release(
            runtime_root,
            control=control,
            source_sha=source_sha,
            source_tree=source_tree,
            allow_test=allow_test,
            prepared_at=prepared_at,
        )
    payloads = plan.get("payloads")
    manifest = plan.get("manifest")
    manifest_payload = plan.get("manifest_payload")
    release_id = plan.get("release_id")
    release = plan.get("release_path")
    staging = plan.get("staging_path")
    staging_owner = plan.get("staging_owner")
    staging_ownership = plan.get("staging_tree_ownership")
    candidate = plan.get("candidate")
    active = plan.get("active")
    if (
        not isinstance(payloads, dict)
        or any(
            not isinstance(name, str) or not isinstance(payload, bytes)
            for name, payload in payloads.items()
        )
        or not isinstance(manifest, dict)
        or not isinstance(manifest_payload, bytes)
        or not isinstance(release_id, str)
        or not isinstance(release, Path)
        or not isinstance(staging, Path)
        or not isinstance(staging_owner, dict)
        or not isinstance(staging_ownership, dict)
        or not isinstance(candidate, dict)
        or not isinstance(active, dict)
        or candidate.get("prepared_at") != prepared_at
    ):
        raise BootstrapError("bootstrap control release plan is invalid")
    release_parent = release.parent

    def inspect_staging() -> str:
        _validate_adoption_staging_tree(
            staging,
            staging_ownership,
            allow_missing_entries=staging_authorized,
            allow_partial_entries=staging_authorized,
        )
        entries = {entry.name: entry for entry in staging.iterdir()}
        if ".owner.json" in entries:
            return "owned"
        if not entries:
            return "empty"
        if set(entries) != set(staging_ownership["files"]):
            if staging_authorized:
                return "owned"
            raise BootstrapError("control-release staging is incomplete")
        for name, entry in entries.items():
            expected_record = staging_ownership["files"][name]
            try:
                observed = _adoption_file_ownership(entry)
                exact = (
                    observed["sha256"] == expected_record["sha256"]
                    and observed["mode"] == expected_record["mode"]
                )
            except BootstrapError:
                exact = False
            if not exact:
                if staging_authorized:
                    return "owned"
                raise BootstrapError("control-release staging file is incomplete")
        return "complete"

    def cleanup_staging() -> None:
        if not staging_authorized:
            raise BootstrapError("control staging lacks durable cleanup authority")
        _validate_adoption_staging_tree(
            staging,
            staging_ownership,
            allow_missing_entries=True,
            allow_partial_entries=True,
        )
        for entry in sorted(staging.iterdir()):
            entry.unlink()
            _fsync_directory(staging)
        staging.rmdir()
        _fsync_directory(release_parent)

    foreign_staging = [
        path
        for path in release_parent.glob(".bootstrap-*")
        if path != staging
    ]
    if foreign_staging:
        raise BootstrapError(
            "control-releases contains foreign bootstrap staging"
        )
    staging_state = (
        inspect_staging()
        if staging.exists() or staging.is_symlink()
        else None
    )
    if release.exists() or release.is_symlink():
        try:
            existing, root = control.load_control_release(runtime_root, release_id)
        except Exception as exc:
            raise BootstrapError("existing initial control release is invalid") from exc
        if existing != manifest or root != release:
            raise BootstrapError("existing initial control release differs")
        _durability_barrier(release, payload=None, mode=0o700, directory=True)
        if staging_state is not None:
            if staging_state not in {"owned", "empty", "complete"}:
                raise BootstrapError(
                    "bootstrap control-release residue is invalid"
                )
            cleanup_staging()
    else:
        if staging_state == "complete":
            _rename_noreplace(staging, release)
            control.load_control_release(runtime_root, release_id)
            staging_state = None
        if not release.exists() and not release.is_symlink():
            if not staging_authorized:
                raise BootstrapError(
                    "control staging lacks durable write authority"
                )
            if not (staging.exists() or staging.is_symlink()):
                _fsync_directory(release_parent)
                staging.mkdir(mode=0o700)
                _fsync_directory(release_parent)
            owner_payload = json.dumps(
                staging_owner,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8") + b"\n"
            _write_authorized_staging(
                staging / ".owner.json", owner_payload, 0o600
            )
            for name, payload in payloads.items():
                _write_authorized_staging(staging / name, payload, 0o700)
            _write_authorized_staging(
                staging / control.CONTROL_MANIFEST_NAME,
                manifest_payload,
                0o600,
            )
            (staging / ".owner.json").unlink()
            _fsync_directory(staging)
            _rename_noreplace(staging, release)
        control.load_control_release(runtime_root, release_id)
        _durability_barrier(release, payload=None, mode=0o700, directory=True)
    return candidate, active


def _harden_checkout(root: Path) -> None:
    try:
        root_metadata = root.lstat()
        git = root / ".git"
        git_metadata = git.lstat()
    except OSError as exc:
        raise BootstrapError("production checkout or .git is missing") from exc
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root.is_symlink()
        or not stat.S_ISDIR(git_metadata.st_mode)
        or git.is_symlink()
        or root_metadata.st_uid != os.geteuid()
        or git_metadata.st_uid != os.geteuid()
    ):
        raise BootstrapError("production checkout must be owner-controlled and non-symlink")
    _assert_standalone_git_storage(root)
    os.chmod(root, stat.S_IMODE(root_metadata.st_mode) & ~0o022)
    # Remove directory write permission first so another group member cannot
    # introduce a new nested .gitattributes while the Git control plane is
    # being sealed. Existing writer FDs to policy files are detached below by
    # atomic inode replacement.
    for directory, names, _files in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        if current == git:
            names[:] = []
            continue
        metadata = current.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or current.is_symlink()
            or metadata.st_uid != os.geteuid()
        ):
            raise BootstrapError(f"unsafe production directory: {current}")
        os.chmod(current, stat.S_IMODE(metadata.st_mode) & ~0o022)
        for name in names:
            child = current / name
            metadata = child.lstat()
            if child == git:
                continue
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or child.is_symlink()
                or metadata.st_uid != os.geteuid()
            ):
                raise BootstrapError(f"unsafe production directory: {child}")
            os.chmod(child, stat.S_IMODE(metadata.st_mode) & ~0o022)
    # Local Git config is executable policy (for example core.fsmonitor).
    # Remove group/world write before invoking Git at all.
    for directory, names, files in os.walk(git, followlinks=False):
        current = Path(directory)
        metadata = current.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or current.is_symlink()
            or metadata.st_uid != os.geteuid()
        ):
            raise BootstrapError(f"unsafe production Git directory: {current}")
        os.chmod(current, 0o700)
        for name in (*names, *files):
            child = current / name
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise BootstrapError(f"unsafe production Git entry: {child}")
            if stat.S_ISDIR(metadata.st_mode):
                os.chmod(child, 0o700)
            elif stat.S_ISREG(metadata.st_mode):
                os.chmod(child, 0o600)
            else:
                raise BootstrapError(f"special production Git entry: {child}")
    policy_payloads = {
        path: _read_git_policy(
            path,
            root=root,
            allow_runtime_overrides=True,
        )
        for path in _git_policy_paths(root)
    }
    for path, (payload, mode) in policy_payloads.items():
        _atomic_file(path, payload, mode)
    _verify_git_object_database(root)
    tracked = subprocess.run(
        _git_command(root, "ls-files", "-z", "--cached"),
        cwd=root,
        env=_git_environment(root, home=os.environ.get("HOME", "")),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    for raw in tracked.split(b"\0"):
        if not raw:
            continue
        relative = Path(raw.decode("utf-8"))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise BootstrapError("tracked production path escapes checkout")
        current = root
        for component in relative.parts[:-1]:
            current = current / component
            metadata = current.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or current.is_symlink() or metadata.st_uid != os.geteuid():
                raise BootstrapError(f"unsafe tracked parent: {current}")
            os.chmod(current, stat.S_IMODE(metadata.st_mode) & ~0o022)
        path = root / relative
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise BootstrapError(f"unsafe tracked production file: {path}")
        os.chmod(path, stat.S_IMODE(metadata.st_mode) & ~0o022)


def _initialize_runtime_root(root: Path) -> None:
    parent = root.parent
    metadata = parent.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or parent.is_symlink() or metadata.st_uid != os.geteuid():
        raise BootstrapError("runtime parent directory is unsafe")
    if root.exists() or root.is_symlink():
        metadata = root.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or root.is_symlink() or metadata.st_uid != os.geteuid():
            raise BootstrapError("runtime root is unsafe")
    else:
        root.mkdir(mode=0o700)
    os.chmod(root, 0o700)


def _open_deploy_lock(path: Path):  # type: ignore[no-untyped-def]
    try:
        root_metadata = path.parent.parent.lstat()
    except OSError as exc:
        raise BootstrapError(
            "pre-takeover installer must create the shared deploy.lock"
        ) from exc
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or path.parent.parent.is_symlink()
        or root_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(root_metadata.st_mode) != 0o700
    ):
        raise BootstrapError("runtime root is unsafe before deploy.lock")
    try:
        state_metadata = path.parent.lstat()
    except OSError as exc:
        raise BootstrapError(
            "pre-takeover installer must create the shared deploy.lock"
        ) from exc
    if (
        not stat.S_ISDIR(state_metadata.st_mode)
        or path.parent.is_symlink()
        or state_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(state_metadata.st_mode) != 0o700
    ):
        raise BootstrapError("runtime root is unsafe before deploy.lock")
    flags = (
        os.O_RDWR
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BootstrapError(
            "pre-takeover installer must create the shared deploy.lock"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise BootstrapError("deploy.lock is unsafe")
        stream = os.fdopen(descriptor, "a+", encoding="utf-8")
        descriptor = -1
        return stream
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _path_inventory(path: Path) -> dict[str, object]:
    if not path.exists() and not path.is_symlink():
        return {"path": str(path), "present": False}
    metadata = path.lstat()
    result: dict[str, object] = {
        "path": str(path),
        "present": True,
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "mode": format(stat.S_IMODE(metadata.st_mode), "04o"),
        "symlink": path.is_symlink(),
        "kind": (
            "directory"
            if stat.S_ISDIR(metadata.st_mode)
            else "file"
            if stat.S_ISREG(metadata.st_mode)
            else "other"
        ),
    }
    if stat.S_ISREG(metadata.st_mode) and not path.is_symlink():
        result["sha256"] = digest(path.read_bytes())
    return result


def _worker_unit_path(production_root: Path, *, allow_test: bool) -> Path:
    if allow_test:
        return production_root.parent / "systemd/user/nexpoly-monomer-md-worker.service"
    return WORKER_UNIT_PATH


def _paths_overlap(left: Path, right: Path) -> bool:
    left = left.resolve(strict=False)
    right = right.resolve(strict=False)
    return left == right or left in right.parents or right in left.parents


def _systemd_environment(*, allow_test: bool) -> dict[str, str]:
    if allow_test:
        return {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "")}
    uid = os.geteuid()
    if uid != 1001:
        raise BootstrapError("production bootstrap requires deploy UID 1001")
    runtime = Path(f"/run/user/{uid}")
    bus = runtime / "bus"
    try:
        runtime_metadata = runtime.lstat()
        bus_metadata = bus.lstat()
    except OSError as exc:
        raise BootstrapError("deploy-user systemd bus is unavailable") from exc
    if (
        not stat.S_ISDIR(runtime_metadata.st_mode)
        or runtime.is_symlink()
        or runtime_metadata.st_uid != uid
        or runtime_metadata.st_mode & 0o022
        or not stat.S_ISSOCK(bus_metadata.st_mode)
        or bus.is_symlink()
        or bus_metadata.st_uid != uid
    ):
        raise BootstrapError("deploy-user systemd bus identity is unsafe")
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/devuser",
        "XDG_RUNTIME_DIR": str(runtime),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={bus}",
    }


def _worker_unit_state(path: Path, *, allow_test: bool) -> dict[str, str]:
    present = path.exists() or path.is_symlink()
    if allow_test:
        return {
            "LoadState": "loaded" if present else "not-found",
            "FragmentPath": str(path) if present else "",
            "DropInPaths": "",
            "NeedDaemonReload": "no",
            "UnitFileState": "enabled" if present else "",
        }
    result = subprocess.run(
        [
            "systemctl",
            "--user",
            "show",
            path.name,
            "--property=LoadState",
            "--property=FragmentPath",
            "--property=DropInPaths",
            "--property=NeedDaemonReload",
            "--property=UnitFileState",
        ],
        env=_systemd_environment(allow_test=allow_test),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    fields = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    expected = {
        "LoadState",
        "FragmentPath",
        "DropInPaths",
        "NeedDaemonReload",
        "UnitFileState",
    }
    if set(fields) != expected:
        raise BootstrapError("Worker systemd inventory has an invalid shape")
    return fields


def _daemon_reload_worker_unit(*, allow_test: bool) -> None:
    if allow_test:
        return
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        env=_systemd_environment(allow_test=False),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _worker_unit_snapshot(
    path: Path, *, allow_test: bool
) -> tuple[bytes, os.stat_result, dict[str, str]] | None:
    if not (path.exists() or path.is_symlink()):
        return None
    try:
        parent = path.parent.lstat()
    except OSError as exc:
        raise BootstrapError("Worker unit parent cannot be inventoried") from exc
    if (
        not stat.S_ISDIR(parent.st_mode)
        or path.parent.is_symlink()
        or parent.st_uid != os.geteuid()
        or parent.st_mode & 0o022
    ):
        raise BootstrapError("existing Worker unit parent is unsafe")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise BootstrapError("existing Worker unit cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or not 1 <= metadata.st_size <= 1024 * 1024
        ):
            raise BootstrapError("existing Worker unit is unsafe")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise BootstrapError("existing Worker unit was truncated while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    return payload, metadata, _worker_unit_state(path, allow_test=allow_test)


def _preflight_worker_unit(
    runtime_root: Path,
    path: Path,
    *,
    expected_sha256: str | None,
    allow_test: bool,
) -> dict[str, object]:
    if expected_sha256 is not None and re.fullmatch(
        r"sha256:[0-9a-f]{64}", expected_sha256
    ) is None:
        raise BootstrapError("confirmed Worker unit SHA-256 is invalid")
    snapshot = _worker_unit_snapshot(path, allow_test=allow_test)
    if snapshot is None:
        if not allow_test or expected_sha256 is not None:
            raise BootstrapError("production Worker unit must be present")
        return {"present": False, "path": str(path)}
    payload, metadata, unit_state = snapshot
    unit_sha256 = digest(payload)
    if expected_sha256 != unit_sha256:
        raise BootstrapError("existing Worker unit differs from its explicit confirmation")
    expected_state = {
        "LoadState": "loaded",
        "FragmentPath": str(path),
        "DropInPaths": "",
        "NeedDaemonReload": "no",
        "UnitFileState": "enabled",
    }
    intent_path = runtime_root / "audit/bootstrap-worker-unit/takeover-intent.json"
    completed_path = runtime_root / "audit/bootstrap-worker-unit/takeover.json"
    intent_present = intent_path.exists() or intent_path.is_symlink()
    completed_present = completed_path.exists() or completed_path.is_symlink()
    mode = stat.S_IMODE(metadata.st_mode)
    reload_pending_state = {
        **expected_state,
        "NeedDaemonReload": "yes",
    }
    if unit_state != expected_state and not (
        unit_state == reload_pending_state
        and mode == 0o600
        and intent_present
        and not completed_present
    ):
        raise BootstrapError(
            "existing Worker unit is not the enabled no-drop-in baseline"
        )
    if mode == 0o664 and completed_present:
        raise BootstrapError("completed Worker unit takeover regressed to legacy mode")
    if mode == 0o600 and not (intent_present or completed_present):
        raise BootstrapError("mode-0600 Worker unit lacks bootstrap takeover authority")
    if mode not in {0o664, 0o600}:
        raise BootstrapError("existing Worker unit mode is not an allowed takeover state")
    return {
        "present": True,
        "path": str(path),
        "sha256": unit_sha256,
        "mode": format(mode, "04o"),
        "systemd_state": unit_state,
        "intent_present": intent_present,
        "completed_present": completed_present,
    }


def _take_over_worker_unit(
    runtime_root: Path,
    path: Path,
    *,
    expected_sha256: str | None,
    allow_test: bool,
) -> dict[str, object]:
    before = _preflight_worker_unit(
        runtime_root,
        path,
        expected_sha256=expected_sha256,
        allow_test=allow_test,
    )
    if before.get("present") is not True:
        return before
    snapshot = _worker_unit_snapshot(path, allow_test=allow_test)
    if snapshot is None:
        raise BootstrapError("Worker unit disappeared after preflight")
    payload, metadata, expected_state = snapshot
    if expected_state.get("NeedDaemonReload") == "yes":
        expected_state = {
            **expected_state,
            "NeedDaemonReload": "no",
        }
    unit_sha256 = digest(payload)
    audit_root = runtime_root / "audit/bootstrap-worker-unit"
    backup_root = runtime_root / "backups/bootstrap-worker-unit"
    backup = backup_root / f"legacy-{unit_sha256.removeprefix('sha256:')}.service"
    if backup.exists() or backup.is_symlink():
        backup_metadata = backup.lstat()
        if (
            not stat.S_ISREG(backup_metadata.st_mode)
            or backup.is_symlink()
            or backup_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(backup_metadata.st_mode) != 0o600
            or backup.read_bytes() != payload
        ):
            raise BootstrapError("existing Worker unit bootstrap backup differs")
    else:
        _atomic_file(backup, payload, 0o600)
    intent_path = audit_root / "takeover-intent.json"
    intent = {
        "schema_version": 1,
        "status": "intent",
        "path": str(path),
        "sha256": unit_sha256,
        "original_mode": "0664",
        "backup_path": str(backup),
        "backup_sha256": digest(backup.read_bytes()),
        "expected_result_mode": "0600",
        "systemd_state": expected_state,
    }
    if intent_path.exists() or intent_path.is_symlink():
        if _load_private_json(intent_path) != intent:
            raise BootstrapError("existing Worker unit takeover intent differs")
    else:
        if stat.S_IMODE(metadata.st_mode) != 0o664:
            raise BootstrapError("new Worker unit takeover did not start at mode 0664")
        _atomic_json(intent_path, intent)
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.geteuid()
            or current.st_ino != metadata.st_ino
            or current.st_dev != metadata.st_dev
        ):
            raise BootstrapError("Worker unit changed before permission takeover")
        verified = bytearray()
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            verified.extend(chunk)
            if len(verified) > 1024 * 1024:
                raise BootstrapError("Worker unit is unexpectedly large")
        if bytes(verified) != payload or digest(bytes(verified)) != unit_sha256:
            raise BootstrapError("Worker unit content changed before permission takeover")
    finally:
        os.close(descriptor)
    # Publish a separately created private inode directly over the legacy
    # pathname.  Never chmod the legacy inode first: a crash in that window
    # would make the path look private while an already-open group-writer FD
    # could still mutate the canonical unit. After os.replace, every legacy FD
    # is detached from the pathname systemd will load.
    _atomic_file(path, payload, 0o600)
    _daemon_reload_worker_unit(allow_test=allow_test)
    after = _worker_unit_snapshot(path, allow_test=allow_test)
    if after is None:
        raise BootstrapError("Worker unit permission takeover did not verify")
    after_payload, after_metadata, after_state = after
    if (
        digest(after_payload) != unit_sha256
        or stat.S_IMODE(after_metadata.st_mode) != 0o600
        or after_state != expected_state
    ):
        raise BootstrapError("Worker unit permission takeover did not verify")
    record = {
        "schema_version": 1,
        "status": "completed",
        "path": str(path),
        "sha256": unit_sha256,
        "original_mode": "0664",
        "backup_path": str(backup),
        "backup_sha256": digest(backup.read_bytes()),
        "result_mode": "0600",
        "systemd_state": expected_state,
        "intent_sha256": digest(intent_path.read_bytes()),
    }
    record_path = audit_root / "takeover.json"
    if record_path.exists() or record_path.is_symlink():
        if _load_private_json(record_path) != record:
            raise BootstrapError("existing Worker unit takeover authority differs")
    else:
        _atomic_json(record_path, record)
    return {**record, "present": True}


def _production_repository_identity(
    root: Path, target_sha: str, *, allow_test: bool
) -> dict[str, object]:
    if allow_test:
        return {
            "branch": "main",
            "origin": "git@github.com:lzq390/ZhijuPoly.git",
            "head": "0" * 40,
            "tree": "0" * 40,
            "target": target_sha,
            "fast_forward": True,
            "ignored_entries": 0,
        }
    environment = _git_environment(root, home="/home/devuser")

    def git(*arguments: str, text: bool = True, check: bool = True) -> str | bytes:
        result = subprocess.run(
            _git_command(root, *arguments),
            cwd=root,
            env=environment,
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
        )
        return result.stdout

    try:
        branch = str(git("symbolic-ref", "--short", "HEAD")).strip()
        origin = str(git("remote", "get-url", "origin")).strip()
        head = str(git("rev-parse", "HEAD")).strip()
        tree = str(git("rev-parse", "HEAD^{tree}")).strip()
        local_main = str(git("rev-parse", "refs/heads/main")).strip()
        dirty = str(git("status", "--porcelain=v1", "--untracked-files=all"))
        ignored_payload = bytes(
            git(
                "ls-files",
                "-z",
                "--others",
                "--ignored",
                "--exclude-standard",
                text=False,
            )
        )
        source_environment = _git_environment(
            REPOSITORY_ROOT, home="/home/devuser"
        )
        source_has_previous = subprocess.run(
            _git_command(REPOSITORY_ROOT, "cat-file", "-e", f"{head}^{{commit}}"),
            cwd=REPOSITORY_ROOT,
            env=source_environment,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        ancestor = subprocess.run(
            _git_command(
                REPOSITORY_ROOT,
                "merge-base",
                "--is-ancestor",
                head,
                target_sha,
            ),
            cwd=REPOSITORY_ROOT,
            env=source_environment,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BootstrapError("production Git identity cannot be established") from exc
    ignored = [entry for entry in ignored_payload.split(b"\0") if entry]
    if (
        branch != "main"
        or origin != REPOSITORY_SSH_URL
        or SHA_RE.fullmatch(head) is None
        or SHA_RE.fullmatch(tree) is None
        or local_main != head
        or dirty
        or ignored
        or source_has_previous.returncode != 0
        or ancestor.returncode != 0
    ):
        raise BootstrapError(
            "production checkout is not a clean canonical fast-forward main baseline"
        )
    return {
        "branch": branch,
        "origin": origin,
        "head": head,
        "tree": tree,
        "target": target_sha,
        "fast_forward": True,
        "ignored_entries": 0,
    }


def _run_bootstrap_legacy_restore(
    command: list[str],
    *,
    deploy_lock_fd: int,
    allow_test: bool,
) -> subprocess.CompletedProcess[str]:
    environment = {
        "HOME": os.environ.get("HOME", "") if allow_test else "/home/devuser",
        "USER": "devuser",
        "LOGNAME": "devuser",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        pass_fds=(deploy_lock_fd,),
        timeout=1800,
    )


def _abort_bootstrap_transaction(
    args: argparse.Namespace,
    *,
    production_root: Path,
    runtime_root: Path,
    worker_unit_path: Path,
    allow_test: bool,
) -> dict[str, object]:
    if (
        args.confirm_production_root != str(production_root)
        or args.confirm_runtime_root != str(runtime_root)
        or args.confirm_source_tree is None
    ):
        raise BootstrapError(
            "bootstrap abort requires exact production, runtime and tree confirmations"
        )
    if not allow_test and (
        production_root != PRODUCTION_ROOT
        or runtime_root != RUNTIME_ROOT
        or production_root.resolve() != PRODUCTION_ROOT
        or runtime_root.resolve() != RUNTIME_ROOT
    ):
        raise BootstrapError("bootstrap abort requires the fixed production roots")
    path = _bootstrap_transaction_path(
        runtime_root,
        operation_id=args.legacy_takeover_operation_id,
        source_sha=args.sha,
    )
    transaction = _reseal_bootstrap_transaction(path)
    if (
        transaction["source_tree"] != args.confirm_source_tree
        or transaction["operation_id"] != args.legacy_takeover_operation_id
    ):
        raise BootstrapError("bootstrap abort confirmation differs from transaction")
    lock = runtime_root / "state/deploy.lock"
    with _open_deploy_lock(lock) as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BootstrapError("another deployment holds deploy.lock") from exc
        locked = _reseal_bootstrap_transaction(path)
        if locked != transaction:
            raise BootstrapError(
                "bootstrap child transaction changed while acquiring deploy.lock"
            )
        transaction = locked
        if transaction["status"] == "completed":
            raise BootstrapError(
                "completed bootstrap must be recovered by the governed controller"
            )
        if transaction["status"] == "aborted":
            return {
                "action": "abort-bootstrap",
                "status": "already-aborted",
                "transaction": transaction,
            }
        evidence = _legacy_takeover_evidence(
            source_sha=str(transaction["source_sha"]),
            allow_test=allow_test,
            installed_runtime_root=runtime_root,
        )
        identity = transaction["identity"]
        assert isinstance(identity, dict)
        takeover_binding = identity.get("legacy_takeover")
        if not isinstance(takeover_binding, dict):
            raise BootstrapError(
                "bootstrap transaction lacks legacy takeover authority"
            )
        try:
            install_manifest = evidence.validate_install_manifest(
                runtime_root,
                str(transaction["source_sha"]),
                str(transaction["source_tree"]),
            )
            install_manifest_sha256 = evidence.sha256_file(
                runtime_root / "legacy-takeover/INSTALL-MANIFEST.json"
            )
        except Exception as exc:
            raise BootstrapError(
                "installed exact legacy restore closure is unavailable"
            ) from exc
        if (
            install_manifest.get("authority_sha")
            != transaction["source_sha"]
            or install_manifest.get("authority_tree")
            != transaction["source_tree"]
            or install_manifest_sha256
            != takeover_binding.get("install_manifest_sha256")
        ):
            raise BootstrapError(
                "installed legacy restore closure differs from bootstrap authority"
            )
        if transaction["status"] == "in-progress":
            try:
                control = evidence.snapshot_current_control_layout(runtime_root)
                permissions = evidence.snapshot_current_checkout_permissions(
                    runtime_root,
                    str(transaction["operation_id"]),
                )
            except Exception as exc:
                raise BootstrapError(
                    "cannot seal partial bootstrap state for abort"
                ) from exc
            expected_worker = identity.get("worker_unit")
            if not isinstance(expected_worker, dict):
                raise BootstrapError(
                    "bootstrap transaction lacks Worker unit authority"
                )
            worker = _worker_unit_snapshot(
                worker_unit_path,
                allow_test=allow_test,
            )
            if expected_worker.get("present") is True:
                if worker is None:
                    raise BootstrapError(
                        "bootstrap Worker unit disappeared before abort"
                    )
                worker_payload, worker_metadata, _worker_state = worker
                worker_sha256: str | None = digest(worker_payload)
                if (
                    worker_sha256 != expected_worker.get("sha256")
                    or stat.S_IMODE(worker_metadata.st_mode)
                    not in {0o600, 0o664}
                ):
                    raise BootstrapError(
                        "bootstrap Worker unit changed before abort"
                    )
            else:
                if worker is not None:
                    raise BootstrapError(
                        "unexpected Worker unit appeared before abort"
                    )
                worker_sha256 = None
            started_at = dt.datetime.now(dt.timezone.utc).isoformat()
            abort_authority = {
                "operation_id": transaction["operation_id"],
                "transaction_identity_sha256": transaction[
                    "identity_sha256"
                ],
                "control_layout_sha256": control["sha256"],
                "checkout_permissions_sha256": permissions["sha256"],
                "worker_unit_sha256": worker_sha256,
                "started_at": started_at,
            }
            transaction["status"] = "aborting"
            transaction["phase"] = "abort-intent"
            transaction["abort_authority"] = abort_authority
            transaction["updated_at"] = started_at
            _atomic_json(path, transaction)
            transaction = _reseal_bootstrap_transaction(path)
        abort_authority = transaction["abort_authority"]
        assert isinstance(abort_authority, dict)
        launcher = (
            runtime_root
            / "legacy-takeover/bin/nexpoly-legacy-takeover"
        )
        command = [
            str(launcher),
            "restore",
            "--operation-id",
            str(transaction["operation_id"]),
            "--expected-control-layout-sha256",
            str(abort_authority["control_layout_sha256"]),
            "--expected-checkout-permissions-sha256",
            str(abort_authority["checkout_permissions_sha256"]),
            "--parent-deploy-lock-fd",
            str(stream.fileno()),
        ]
        worker_sha256 = abort_authority.get("worker_unit_sha256")
        if worker_sha256 is not None:
            command.extend(
                ["--expected-worker-unit-sha256", str(worker_sha256)]
            )
        try:
            completed = _run_bootstrap_legacy_restore(
                command,
                deploy_lock_fd=stream.fileno(),
                allow_test=allow_test,
            )
            response = json.loads(completed.stdout)
            restored = evidence.validate_status_document(
                response,
                str(transaction["operation_id"]),
            )
        except Exception as exc:
            raise BootstrapError(
                "exact legacy takeover restore did not complete bootstrap abort"
            ) from exc
        if (
            restored.get("active") is not False
            or restored.get("restore_phase") != "restored"
            or restored.get("control_layout_replacement_sha256")
            != abort_authority["control_layout_sha256"]
            or restored.get("checkout_permissions_replacement_sha256")
            != abort_authority["checkout_permissions_sha256"]
            or not isinstance(
                restored.get("restored_terminal_sha256"),
                str,
            )
            or re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                str(restored.get("restored_terminal_sha256")),
            )
            is None
        ):
            raise BootstrapError(
                "legacy takeover restore terminal differs from bootstrap abort"
            )
        finished_at = dt.datetime.now(dt.timezone.utc).isoformat()
        transaction["status"] = "aborted"
        transaction["phase"] = "aborted"
        transaction["restored_terminal_sha256"] = restored[
            "restored_terminal_sha256"
        ]
        transaction["aborted_at"] = finished_at
        transaction["updated_at"] = finished_at
        _atomic_json(path, transaction)
        transaction = _reseal_bootstrap_transaction(path)
        return {
            "action": "abort-bootstrap",
            "status": "aborted",
            "transaction": transaction,
        }


def _test_bootstrap_source_readiness(
    source_sha: str, source_tree: str
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "ready": True,
        "source_root": str(REPOSITORY_ROOT),
        "source_sha": source_sha,
        "source_tree": source_tree,
        "branch": "main",
        "origin": REPOSITORY_SSH_URL,
        "remote_names": ["origin"],
        "origin_fetch_urls": [REPOSITORY_SSH_URL],
        "origin_push_urls": [REPOSITORY_SSH_URL],
        "origin_main_sha": source_sha,
        "standalone_object_database": True,
        "shallow": False,
        "dirty_entries": 0,
        "ignored_entries": 0,
        "unreachable_objects": 0,
        "replace_refs": 0,
        "special_index_entries": 0,
        "sparse_index": False,
        "owner_private": True,
        "group_or_world_writable": False,
    }


def _require_mutating_adoption_roots(
    args: argparse.Namespace,
    *,
    production_root: Path,
    runtime_root: Path,
    allow_test: bool,
) -> None:
    if allow_test:
        return
    if (
        production_root != PRODUCTION_ROOT
        or runtime_root != RUNTIME_ROOT
        or production_root.resolve() != PRODUCTION_ROOT
        or runtime_root.resolve() != RUNTIME_ROOT
    ):
        raise BootstrapError(
            "mutating manual adoption requires exact production/runtime roots"
        )
    if (
        args.confirm_production_root != str(PRODUCTION_ROOT)
        or args.confirm_runtime_root != str(RUNTIME_ROOT)
    ):
        raise BootstrapError(
            "mutating manual adoption requires exact confirmed roots"
        )


def _manual_adoption_main(
    args: argparse.Namespace, *, allow_test: bool
) -> int:
    production_root = Path(args.production_root).absolute()
    runtime_root = Path(args.runtime_root).absolute()
    operation_id = _require_adoption_operation_id(args.operation_id)
    if not isinstance(args.live_sha, str) or SHA_RE.fullmatch(args.live_sha) is None:
        raise BootstrapError("--live-sha is required for manual adoption")
    test_units = (
        _worker_unit_path(production_root, allow_test=True),
        _dft_worker_unit_path(production_root, allow_test=True),
    )
    if allow_test and (
        any(
            _paths_overlap(candidate, protected)
            for candidate in (production_root, runtime_root)
            for protected in (PRODUCTION_ROOT, RUNTIME_ROOT)
        )
        or any(_paths_overlap(unit, WORKER_UNIT_PATH) for unit in test_units)
        or any(_paths_overlap(unit, DFT_WORKER_UNIT_PATH) for unit in test_units)
    ):
        raise BootstrapError("test mode is forbidden for production roots")
    if args.adopt_abort:
        _require_mutating_adoption_roots(
            args,
            production_root=production_root,
            runtime_root=runtime_root,
            allow_test=allow_test,
        )
        if (
            not args.confirm_evidence_sha256
            or not args.confirm_source_tree
            or not args.confirm_md_unit_sha256
            or not args.confirm_dft_unit_sha256
        ):
            raise BootstrapError("adoption abort requires exact confirmations")
        result = _abort_manual_runtime_adoption(
            args,
            runtime_root=runtime_root,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if allow_test:
        source_sha, source_tree = _source_identity(allow_test=True)
        source_readiness = _test_bootstrap_source_readiness(
            source_sha, source_tree
        )
    else:
        source_readiness = bootstrap_source_readiness(
            REPOSITORY_ROOT,
            expected_sha=args.sha,
        )
        source_sha = str(source_readiness["source_sha"])
        source_tree = str(source_readiness["source_tree"])
    if args.sha != source_sha:
        raise BootstrapError(
            "requested adoption SHA differs from the clean source checkout"
        )
    if args.adopt_apply:
        _require_mutating_adoption_roots(
            args,
            production_root=production_root,
            runtime_root=runtime_root,
            allow_test=allow_test,
        )
        for name, value in (
            ("evidence", args.confirm_evidence_sha256),
            ("MD unit", args.confirm_md_unit_sha256),
            ("DFT unit", args.confirm_dft_unit_sha256),
        ):
            if (
                not isinstance(value, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
            ):
                raise BootstrapError(f"confirmed adoption {name} digest is invalid")
    _adoption_preflight(
        runtime_root,
        operation_id=operation_id,
        permit_transaction=args.adopt_apply,
    )
    sealed_gate = _sealed_bootstrap_delivery_gate(
        runtime_root,
        source_sha=source_sha,
        source_tree=source_tree,
    )
    delivery_gate = _delivery_gate(
        production_root,
        runtime_root,
        source_sha,
        allow_test=allow_test,
        sealed=sealed_gate,
    )
    evidence = _collect_adoption_evidence(
        production_root,
        runtime_root,
        operation_id=operation_id,
        bootstrap_source_sha=source_sha,
        bootstrap_source_tree=source_tree,
        live_sha=args.live_sha,
        expected_md_unit_sha256=(
            args.confirm_md_unit_sha256 if args.adopt_apply else None
        ),
        expected_dft_unit_sha256=(
            args.confirm_dft_unit_sha256 if args.adopt_apply else None
        ),
        allow_test=allow_test,
    )
    evidence_sha256 = _canonical_json_digest(evidence)
    md_unit_sha256 = evidence["monomer_md"]["systemd_unit"]["sha256"]  # type: ignore[index]
    dft_unit_sha256 = evidence["monomer_dft"]["systemd_unit"]["sha256"]  # type: ignore[index]
    plan = {
        "action": "manual-runtime-adoption",
        "apply": args.adopt_apply,
        "operation_id": operation_id,
        "production_root": str(production_root),
        "runtime_root": str(runtime_root),
        "bootstrap_source_sha": source_sha,
        "bootstrap_source_tree": source_tree,
        "live_source_sha": args.live_sha,
        "live_source_tree": evidence["live_repository"]["tree"],  # type: ignore[index]
        "source_readiness": source_readiness,
        "delivery_gate": delivery_gate,
        "deploy_lock_disposition": ADOPTION_DEPLOY_LOCK_DISPOSITION,
        "evidence": evidence,
        "evidence_sha256": evidence_sha256,
        "confirmations": {
            "production_root": str(production_root),
            "runtime_root": str(runtime_root),
            "source_tree": evidence["live_repository"]["tree"],  # type: ignore[index]
            "evidence_sha256": evidence_sha256,
            "md_unit_sha256": md_unit_sha256,
            "dft_unit_sha256": dft_unit_sha256,
        },
        "excluded_actions": [
            "change Git HEAD or fetch",
            "start, stop, reload, or restart services",
            "change PostgreSQL or its migration ledger",
            "change container or image state",
            "change Worker units, environments, slots, or runtimes",
            "change asset pointers",
            "forge legacy takeover or maintenance markers",
        ],
    }
    if args.adopt_plan:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    immutable_payloads = {
        name: (
            _read_reviewed_source(
                source.relative_to(REPOSITORY_ROOT).as_posix(),
                source_sha=source_sha,
                allow_test=allow_test,
            ),
            mode,
        )
        for name, (source, mode) in IMMUTABLE_FILES.items()
    }
    control = _control_runtime(source_sha=source_sha, allow_test=allow_test)
    result = _apply_manual_runtime_adoption(
        args,
        production_root=production_root,
        runtime_root=runtime_root,
        source_sha=source_sha,
        source_tree=source_tree,
        source_readiness=source_readiness,
        delivery_gate=delivery_gate,
        evidence=evidence,
        evidence_sha256=evidence_sha256,
        immutable_payloads=immutable_payloads,
        control=control,
        allow_test=allow_test,
    )
    print(json.dumps({**plan, **result}, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument(
        "--check-source-readiness",
        action="store_true",
        help="only validate a standalone bootstrap source and print JSON",
    )
    parser.add_argument(
        "--source-root",
        help="source clone inspected by --check-source-readiness",
    )
    parser.add_argument("--production-root", default=str(PRODUCTION_ROOT))
    parser.add_argument("--runtime-root", default=str(RUNTIME_ROOT))
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument(
        "--abort",
        action="store_true",
        help="restore the exact legacy state for an interrupted bootstrap",
    )
    action.add_argument(
        "--adopt-plan",
        action="store_true",
        help="read-only plan for one-time manual production adoption",
    )
    action.add_argument(
        "--adopt-apply",
        action="store_true",
        help="install only control authority around an unchanged manual runtime",
    )
    action.add_argument(
        "--adopt-abort",
        action="store_true",
        help="CAS-remove adoption-owned controls before authority commit",
    )
    parser.add_argument("--confirm-production-root")
    parser.add_argument("--confirm-runtime-root")
    parser.add_argument("--confirm-source-tree")
    parser.add_argument("--confirm-worker-unit-sha256")
    parser.add_argument("--operation-id")
    parser.add_argument("--live-sha")
    parser.add_argument("--confirm-evidence-sha256")
    parser.add_argument("--confirm-md-unit-sha256")
    parser.add_argument("--confirm-dft-unit-sha256")
    parser.add_argument(
        "--legacy-takeover-operation-id",
        help=(
            "exact completed pre-stopped takeover operation consumed by "
            "bootstrap"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    allow_test = os.environ.get("NEXPOLY_ALLOW_TEST_ROOT") == "1"
    if not allow_test and not sys.flags.isolated:
        print(
            "bootstrap-pull-deploy: error: bootstrap requires isolated Python startup",
            file=sys.stderr,
        )
        return 2
    args = build_parser().parse_args(argv)
    if args.check_source_readiness:
        if (
            args.apply
            or args.abort
            or args.adopt_plan
            or args.adopt_apply
            or args.adopt_abort
        ):
            print(
                "bootstrap-pull-deploy: error: source readiness is read-only",
                file=sys.stderr,
            )
            return 2
        try:
            report = bootstrap_source_readiness(
                Path(args.source_root).absolute()
                if args.source_root
                else REPOSITORY_ROOT,
                expected_sha=args.sha,
            )
        except (BootstrapError, OSError, subprocess.SubprocessError) as exc:
            print(f"bootstrap-pull-deploy: error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    if args.source_root is not None:
        print(
            "bootstrap-pull-deploy: error: --source-root requires --check-source-readiness",
            file=sys.stderr,
        )
        return 2
    if args.adopt_plan or args.adopt_apply or args.adopt_abort:
        try:
            return _manual_adoption_main(args, allow_test=allow_test)
        except (
            BootstrapError,
            OSError,
            UnicodeError,
            subprocess.SubprocessError,
        ) as exc:
            print(f"bootstrap-pull-deploy: error: {exc}", file=sys.stderr)
            return 2
    if not args.legacy_takeover_operation_id:
        print(
            "bootstrap-pull-deploy: error: "
            "--legacy-takeover-operation-id is required",
            file=sys.stderr,
        )
        return 2
    production_root = Path(args.production_root).absolute()
    runtime_root = Path(args.runtime_root).absolute()
    test_unit = _worker_unit_path(production_root, allow_test=True)
    if allow_test and (
        any(
            _paths_overlap(candidate, protected)
            for candidate in (production_root, runtime_root)
            for protected in (PRODUCTION_ROOT, RUNTIME_ROOT)
        )
        or _paths_overlap(test_unit, WORKER_UNIT_PATH)
    ):
        print(
            "bootstrap-pull-deploy: error: test mode is forbidden for production roots",
            file=sys.stderr,
        )
        return 2
    if args.abort:
        try:
            result = _abort_bootstrap_transaction(
                args,
                production_root=production_root,
                runtime_root=runtime_root,
                worker_unit_path=_worker_unit_path(
                    production_root,
                    allow_test=allow_test,
                ),
                allow_test=allow_test,
            )
        except (
            BootstrapError,
            OSError,
            UnicodeError,
            subprocess.SubprocessError,
        ) as exc:
            print(f"bootstrap-pull-deploy: error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    try:
        if not allow_test:
            source_readiness = bootstrap_source_readiness(
                REPOSITORY_ROOT,
                expected_sha=args.sha,
            )
            source_sha = str(source_readiness["source_sha"])
            source_tree = str(source_readiness["source_tree"])
        else:
            source_sha, source_tree = _source_identity(allow_test=True)
            source_readiness = {
                "schema_version": 2,
                "ready": True,
                "source_root": str(REPOSITORY_ROOT),
                "source_sha": source_sha,
                "source_tree": source_tree,
                "branch": "main",
                "origin": REPOSITORY_SSH_URL,
                "remote_names": ["origin"],
                "origin_fetch_urls": [REPOSITORY_SSH_URL],
                "origin_push_urls": [REPOSITORY_SSH_URL],
                "origin_main_sha": source_sha,
                "standalone_object_database": True,
                "shallow": False,
                "dirty_entries": 0,
                "ignored_entries": 0,
                "unreachable_objects": 0,
                "replace_refs": 0,
                "special_index_entries": 0,
                "sparse_index": False,
                "owner_private": True,
                "group_or_world_writable": False,
            }
        if args.sha != source_sha:
            raise BootstrapError(
                "requested bootstrap SHA differs from the clean source checkout"
            )
        source_readiness_sha256 = digest(
            json.dumps(
                source_readiness,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        )
        if args.apply and not allow_test and (
            production_root != PRODUCTION_ROOT
            or runtime_root != RUNTIME_ROOT
            or production_root.resolve() != PRODUCTION_ROOT
            or (runtime_root.exists() and runtime_root.resolve() != RUNTIME_ROOT)
            or args.confirm_production_root != str(PRODUCTION_ROOT)
            or args.confirm_runtime_root != str(RUNTIME_ROOT)
            or args.confirm_source_tree != source_tree
        ):
            raise BootstrapError(
                "--apply requires exact confirmed production roots"
            )
        if args.apply and allow_test and (
            args.confirm_production_root != str(production_root)
            or args.confirm_runtime_root != str(runtime_root)
            or args.confirm_source_tree != source_tree
        ):
            raise BootstrapError("test apply requires matching confirmations")
        control = _control_runtime(
            source_sha=source_sha,
            allow_test=allow_test,
        )
        sealed_delivery_gate = _sealed_bootstrap_delivery_gate(
            runtime_root,
            source_sha=source_sha,
            source_tree=source_tree,
        )
        delivery_gate = _delivery_gate(
            production_root,
            runtime_root,
            source_sha,
            allow_test=allow_test,
            sealed=sealed_delivery_gate,
        )
        immutable_payloads = {
            name: (
                _read_reviewed_source(
                    source.relative_to(REPOSITORY_ROOT).as_posix(),
                    source_sha=source_sha,
                    allow_test=allow_test,
                ),
                mode,
            )
            for name, (source, mode) in IMMUTABLE_FILES.items()
        }
        permission_inventory = {
            "checkout": _path_inventory(production_root),
            "git": _path_inventory(production_root / ".git"),
        }
        production_repository = _production_repository_identity(
            production_root, source_sha, allow_test=allow_test
        )
        legacy_takeover = _completed_legacy_takeover(
            runtime_root,
            args.legacy_takeover_operation_id,
            source_sha=source_sha,
            source_tree=source_tree,
            production_repository=production_repository,
            allow_test=allow_test,
        )
    except (BootstrapError, OSError, subprocess.SubprocessError) as exc:
        print(f"bootstrap-pull-deploy: error: {exc}", file=sys.stderr)
        return 2
    worker_unit_path = _worker_unit_path(production_root, allow_test=allow_test)
    try:
        worker_unit_state = _worker_unit_state(
            worker_unit_path, allow_test=allow_test
        )
    except (BootstrapError, OSError, subprocess.SubprocessError) as exc:
        print(f"bootstrap-pull-deploy: error: {exc}", file=sys.stderr)
        return 2
    plan: dict[str, object] = {
        "action": "bootstrap-pull-deploy",
        "apply": args.apply,
        "production_root": str(production_root),
        "runtime_root": str(runtime_root),
        "source_sha": source_sha,
        "source_tree": source_tree,
        "source_readiness": source_readiness,
        "source_readiness_sha256": source_readiness_sha256,
        "legacy_takeover": legacy_takeover,
        "delivery_gate": delivery_gate,
        "production_repository": production_repository,
        "directories": [str(runtime_root / relative) for relative in DIRECTORIES],
        "immutable_files": {
            name: {"source": str(IMMUTABLE_FILES[name][0]), "mode": mode, "sha256": digest(payload)}
            for name, (payload, mode) in immutable_payloads.items()
        },
        "permission_takeover": {
            "checkout": "owner, group/other non-writable",
            "git": "0700 directories and 0600 files",
            "current_observed_production_modes": "must be reviewed by operator before --apply",
            "inventory": {
                **permission_inventory,
                "monomer_md_unit": _path_inventory(
                    worker_unit_path
                ),
                "monomer_md_unit_state": worker_unit_state,
            },
        },
        "excluded_actions": [
            "change Git HEAD or fetch",
            "start or stop services",
            "read or change PostgreSQL",
            "write credentials or environment files",
            "create an active Worker slot",
            "upgrade immutable selector after bootstrap",
        ],
    }
    if not args.apply:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    try:
        initial_unit_preflight = _preflight_worker_unit(
            runtime_root,
            worker_unit_path,
            expected_sha256=args.confirm_worker_unit_sha256,
            allow_test=allow_test,
        )
        os.umask(0o077)
        lock = runtime_root / "state/deploy.lock"
        with _open_deploy_lock(lock) as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BootstrapError("another deployment holds deploy.lock") from exc
            if allow_test:
                locked_readiness = source_readiness
            else:
                locked_readiness = bootstrap_source_readiness(
                    REPOSITORY_ROOT,
                    expected_sha=source_sha,
                )
            if locked_readiness != source_readiness:
                raise BootstrapError(
                    "bootstrap source readiness changed before installation"
                )
            if digest(
                json.dumps(
                    locked_readiness,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("utf-8")
            ) != source_readiness_sha256:
                raise BootstrapError(
                    "bootstrap source readiness digest changed before installation"
                )
            locked_takeover = _completed_legacy_takeover(
                runtime_root,
                args.legacy_takeover_operation_id,
                source_sha=source_sha,
                source_tree=source_tree,
                production_repository=production_repository,
                allow_test=allow_test,
            )
            if locked_takeover != legacy_takeover:
                raise BootstrapError(
                    "legacy takeover authority changed before installation"
                )
            locked_unit = _preflight_worker_unit(
                runtime_root,
                worker_unit_path,
                expected_sha256=args.confirm_worker_unit_sha256,
                allow_test=allow_test,
            )
            if locked_unit != initial_unit_preflight:
                raise BootstrapError("Worker unit changed before bootstrap intent")
            transaction_identity = {
                "source_readiness_sha256": source_readiness_sha256,
                "legacy_takeover": legacy_takeover,
                "delivery_gate": delivery_gate,
                "production_repository": production_repository,
                "checkout_permissions_authority_sha256": legacy_takeover[
                    "checkout_permissions_sha256"
                ],
                "worker_unit": {
                    "present": initial_unit_preflight.get("present"),
                    "path": initial_unit_preflight.get("path"),
                    "sha256": initial_unit_preflight.get("sha256"),
                },
                "directories": {
                    relative: format(mode, "04o")
                    for relative, mode in DIRECTORIES.items()
                },
                "immutable_files": {
                    name: {
                        "sha256": digest(payload),
                        "mode": format(mode, "04o"),
                    }
                    for name, (payload, mode) in immutable_payloads.items()
                },
            }
            transaction_path, transaction = (
                _load_or_create_bootstrap_transaction(
                    runtime_root,
                    operation_id=args.legacy_takeover_operation_id,
                    source_sha=source_sha,
                    source_tree=source_tree,
                    identity=transaction_identity,
                )
            )
            transaction = _begin_bootstrap_step(
                transaction_path,
                transaction,
                previous_phase="intent",
                intent_phase="runtime-layout-intent",
            )
            _initialize_runtime_root(runtime_root)
            for relative, mode in DIRECTORIES.items():
                path = runtime_root / relative
                path.mkdir(parents=True, exist_ok=True, mode=mode)
                if (
                    path.is_symlink()
                    or not path.is_dir()
                    or path.stat().st_uid != os.geteuid()
                ):
                    raise BootstrapError(f"runtime directory is unsafe: {path}")
                os.chmod(path, mode)
            layout_evidence = {
                relative: {
                    "path": str(runtime_root / relative),
                    "mode": format(
                        stat.S_IMODE((runtime_root / relative).lstat().st_mode),
                        "04o",
                    ),
                }
                for relative in DIRECTORIES
            }
            transaction = _complete_bootstrap_step(
                transaction_path,
                transaction,
                intent_phase="runtime-layout-intent",
                ready_phase="runtime-layout-ready",
                evidence_name="runtime_layout",
                evidence=layout_evidence,
            )
            locked_gate = _delivery_gate(
                production_root,
                runtime_root,
                source_sha,
                allow_test=allow_test,
                sealed=delivery_gate,
            )
            if locked_gate != delivery_gate:
                raise BootstrapError("protected-main delivery evidence changed before install")
            transaction = _begin_bootstrap_step(
                transaction_path,
                transaction,
                previous_phase="runtime-layout-ready",
                intent_phase="checkout-intent",
            )
            _harden_checkout(production_root)
            locked_repository = _production_repository_identity(
                production_root, source_sha, allow_test=allow_test
            )
            if locked_repository != production_repository:
                raise BootstrapError("production Git identity changed before install")
            locked_takeover = _completed_legacy_takeover(
                runtime_root,
                args.legacy_takeover_operation_id,
                source_sha=source_sha,
                source_tree=source_tree,
                production_repository=locked_repository,
                allow_test=allow_test,
            )
            if locked_takeover != legacy_takeover:
                raise BootstrapError(
                    "legacy takeover authority changed during installation"
                )
            checkout_evidence = {
                "repository": locked_repository,
                "checkout": _path_inventory(production_root),
                "git": _path_inventory(production_root / ".git"),
            }
            transaction = _complete_bootstrap_step(
                transaction_path,
                transaction,
                intent_phase="checkout-intent",
                ready_phase="checkout-ready",
                evidence_name="checkout",
                evidence=checkout_evidence,
            )
            locked_unit_after_checkout = _preflight_worker_unit(
                runtime_root,
                worker_unit_path,
                expected_sha256=args.confirm_worker_unit_sha256,
                allow_test=allow_test,
            )
            if locked_unit_after_checkout != initial_unit_preflight:
                raise BootstrapError("Worker unit changed before takeover")
            transaction = _begin_bootstrap_step(
                transaction_path,
                transaction,
                previous_phase="checkout-ready",
                intent_phase="worker-unit-intent",
            )
            plan["worker_unit_takeover"] = _take_over_worker_unit(
                runtime_root,
                worker_unit_path,
                expected_sha256=args.confirm_worker_unit_sha256,
                allow_test=allow_test,
            )
            transaction = _complete_bootstrap_step(
                transaction_path,
                transaction,
                intent_phase="worker-unit-intent",
                ready_phase="worker-unit-ready",
                evidence_name="worker_unit",
                evidence=plan["worker_unit_takeover"],
            )
            transaction = _begin_bootstrap_step(
                transaction_path,
                transaction,
                previous_phase="worker-unit-ready",
                intent_phase="immutable-controls-intent",
            )
            installed = {
                name: _install_exact(runtime_root / "bin" / name, payload, mode)
                for name, (payload, mode) in immutable_payloads.items()
            }
            actual_bin = {entry.name for entry in (runtime_root / "bin").iterdir()}
            if actual_bin != set(IMMUTABLE_FILES):
                raise BootstrapError("runtime/bin contains non-immutable or missing controls")
            transaction = _complete_bootstrap_step(
                transaction_path,
                transaction,
                intent_phase="immutable-controls-intent",
                ready_phase="immutable-controls-ready",
                evidence_name="immutable_controls",
                evidence=installed,
            )
            control_plan = _plan_control_release(
                runtime_root,
                control=control,
                source_sha=source_sha,
                source_tree=source_tree,
                allow_test=allow_test,
                prepared_at=str(transaction["prepared_at"]),
            )
            legacy_staging = control_plan["staging_path"]
            if (
                transaction["phase"] == "immutable-controls-ready"
                and isinstance(legacy_staging, Path)
                and (legacy_staging.exists() or legacy_staging.is_symlink())
            ):
                raise BootstrapError(
                    "control staging existed before durable legacy intent"
                )
            transaction = _begin_bootstrap_step(
                transaction_path,
                transaction,
                previous_phase="immutable-controls-ready",
                intent_phase="control-release-intent",
            )
            candidate, active = _build_control_release(
                runtime_root,
                control=control,
                source_sha=source_sha,
                source_tree=source_tree,
                allow_test=allow_test,
                prepared_at=str(transaction["prepared_at"]),
                plan=control_plan,
                staging_authorized=True,
            )
            transaction = _complete_bootstrap_step(
                transaction_path,
                transaction,
                intent_phase="control-release-intent",
                ready_phase="control-release-ready",
                evidence_name="control_release",
                evidence={
                    "candidate": candidate,
                    "active": active,
                },
            )
            final_readiness = (
                source_readiness
                if allow_test
                else bootstrap_source_readiness(
                    REPOSITORY_ROOT,
                    expected_sha=source_sha,
                )
            )
            final_gate = _delivery_gate(
                production_root,
                runtime_root,
                source_sha,
                allow_test=allow_test,
                sealed=delivery_gate,
            )
            final_repository = _production_repository_identity(
                production_root, source_sha, allow_test=allow_test
            )
            final_takeover = _completed_legacy_takeover(
                runtime_root,
                args.legacy_takeover_operation_id,
                source_sha=source_sha,
                source_tree=source_tree,
                production_repository=final_repository,
                allow_test=allow_test,
            )
            final_unit = _preflight_worker_unit(
                runtime_root,
                worker_unit_path,
                expected_sha256=args.confirm_worker_unit_sha256,
                allow_test=allow_test,
            )
            if (
                final_readiness != source_readiness
                or final_gate != delivery_gate
                or final_repository != production_repository
                or final_takeover != legacy_takeover
                or final_unit.get("present") is True
                and (
                    final_unit.get("mode") != "0600"
                    or final_unit.get("completed_present") is not True
                    or final_unit.get("sha256")
                    != args.confirm_worker_unit_sha256
                )
            ):
                raise BootstrapError(
                    "bootstrap authority changed before active-control commit"
                )
            transaction = _begin_bootstrap_step(
                transaction_path,
                transaction,
                previous_phase="control-release-ready",
                intent_phase="authority-commit-intent",
            )
            active_path = runtime_root / "state/active-control.json"
            bootstrap_record_path = runtime_root / "state/bootstrap-control.json"
            existing_bootstrap_record: dict[str, object] | None = None
            if bootstrap_record_path.exists() or bootstrap_record_path.is_symlink():
                existing_bootstrap_record = _load_private_json(
                    bootstrap_record_path
                )
                if existing_bootstrap_record.get("status") not in {
                    "prepared",
                    "completed",
                }:
                    raise BootstrapError("existing bootstrap authority status is invalid")
                if (
                    existing_bootstrap_record.get("status") == "completed"
                    and not (active_path.exists() or active_path.is_symlink())
                ):
                    raise BootstrapError(
                        "completed bootstrap record has no active control authority"
                    )
                stored_candidate = existing_bootstrap_record.get("candidate_control")
                stored_active = existing_bootstrap_record.get("active_control")
                if not isinstance(stored_candidate, dict) or not isinstance(
                    stored_active, dict
                ):
                    raise BootstrapError(
                        "existing bootstrap authority lacks initial controls"
                    )
                comparable_candidate = {
                    key: value
                    for key, value in candidate.items()
                    if key != "prepared_at"
                }
                comparable_stored = {
                    key: value
                    for key, value in stored_candidate.items()
                    if key != "prepared_at"
                }
                if comparable_candidate != comparable_stored:
                    raise BootstrapError(
                        "existing bootstrap candidate authority differs"
                    )
                candidate = stored_candidate
                comparable_active = {
                    key: value for key, value in active.items() if key != "activated_at"
                }
                comparable_stored_active = {
                    key: value
                    for key, value in stored_active.items()
                    if key != "activated_at"
                }
                if comparable_active != comparable_stored_active:
                    raise BootstrapError(
                        "existing bootstrap active authority differs"
                    )
                active = stored_active
            bootstrap_record_base = {
                "schema_version": 2,
                "source_sha": source_sha,
                "source_tree": source_tree,
                "source_readiness": source_readiness,
                "source_readiness_sha256": source_readiness_sha256,
                "legacy_takeover": legacy_takeover,
                "delivery_gate": delivery_gate,
                "production_repository": production_repository,
                "immutable_files": installed,
                "worker_unit_takeover": plan["worker_unit_takeover"],
                "candidate_control": candidate,
                "active_control": active,
            }
            if existing_bootstrap_record is None:
                _atomic_json(
                    bootstrap_record_path,
                    {**bootstrap_record_base, "status": "prepared"},
                )
            else:
                expected_existing = {
                    **bootstrap_record_base,
                    "status": existing_bootstrap_record["status"],
                }
                if existing_bootstrap_record != expected_existing:
                    raise BootstrapError("existing bootstrap authority differs")
            if active_path.exists() or active_path.is_symlink():
                existing = _load_private_json(active_path)
                # Timestamp is part of authority; a rerun must preserve the
                # exact first successful bootstrap record.
                comparable = {
                    key: value for key, value in existing.items() if key != "activated_at"
                }
                target = {key: value for key, value in active.items() if key != "activated_at"}
                if comparable != target:
                    raise BootstrapError("existing active control authority differs")
                active = existing
            else:
                _atomic_json(active_path, active)
            validated_active = control.validate_active_control_record(
                _load_private_json(active_path)
            )
            manifest, release_root = control.load_control_release(
                runtime_root, validated_active["release_id"]
            )
            if (
                validated_active != active
                or control.sha256_file(
                    release_root / control.CONTROL_MANIFEST_NAME
                )
                != active["manifest_sha256"]
                or any(
                    active[key] != manifest[key]
                    for key in ("source_sha", "source_tree", "release_id")
                )
            ):
                raise BootstrapError("initial active control authority did not commit")
            bootstrap_record = {**bootstrap_record_base, "status": "completed"}
            if existing_bootstrap_record != bootstrap_record:
                _atomic_json(bootstrap_record_path, bootstrap_record)
            loaded_active, _manifest, _root = control.load_active_control(runtime_root)
            if loaded_active != active:
                raise BootstrapError(
                    "completed bootstrap authority did not enable initial controls"
                )
            transaction = _complete_bootstrap_transaction(
                transaction_path,
                evidence={
                    "bootstrap_control_sha256": digest(
                        bootstrap_record_path.read_bytes()
                    ),
                    "active_control_sha256": digest(active_path.read_bytes()),
                    "active_control": active,
                },
            )
        plan["status"] = "initialized"
        plan["bootstrap_transaction"] = transaction
        plan["installed_sha256"] = installed
        plan["candidate_control"] = candidate
        plan["active_control"] = active
    except (BootstrapError, OSError, UnicodeError, subprocess.SubprocessError) as exc:
        print(f"bootstrap-pull-deploy: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

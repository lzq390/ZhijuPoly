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
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import stat
import subprocess
import sys
import types
import urllib.error
import urllib.parse
import urllib.request

sys.dont_write_bytecode = True

PRODUCTION_ROOT = Path("/data/lzq/gith/nexpoly")
RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
WORKER_UNIT_PATH = Path(
    "/home/devuser/.config/systemd/user/nexpoly-monomer-md-worker.service"
)
SCRIPT_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_ROOT.parent
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
REPOSITORY_SSH_URL = "git@github.com:lzq390/ZhijuPoly.git"
REPOSITORY_API_ROOT = "https://api.github.com/repos/lzq390/ZhijuPoly"
SYSTEM_CA_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
GIT_EXTERNAL_STORAGE_MARKERS = (
    Path(".git/commondir"),
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
    "state/monomer-md-worker-socket": 0o700,
    "state/monomer-md-worker-runs": 0o700,
    "state/gpu-resource": 0o700,
    "audit": 0o700,
    "audit/bootstrap-worker-unit": 0o700,
    "audit/contracts/0012": 0o700,
    "backups": 0o700,
    "backups/bootstrap-worker-unit": 0o700,
    "backups/contracts/0012": 0o700,
    "wheel-cache": 0o700,
    "worker-venvs": 0o700,
    "control-releases": 0o700,
}

IMMUTABLE_FILES = {
    "control_runtime_selector.py": (SCRIPT_ROOT / "control_runtime_selector.py", 0o700),
    "nexpoly-pull-deploy": (SCRIPT_ROOT / "nexpoly-pull-deploy", 0o700),
    "nexpoly-pull-contract-0012": (
        SCRIPT_ROOT / "nexpoly-pull-contract-0012",
        0o700,
    ),
}


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
    if allow_test:
        evidence = {
            "remote_main": source_sha,
            "ci": {
                "head_sha": source_sha,
                "conclusion": "success",
                "required_jobs": [
                    "Publish and smoke immutable main images",
                    "ci-gate",
                ],
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
    required = {"ci-gate", "Publish and smoke immutable main images"}
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
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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


def _sealed_bootstrap_delivery_gate(
    runtime_root: Path, *, source_sha: str, source_tree: str
) -> dict[str, object] | None:
    path = runtime_root / "state/bootstrap-control.json"
    if not path.exists() and not path.is_symlink():
        return None
    record = _load_private_json(path)
    gate = record.get("delivery_gate")
    if (
        record.get("schema_version") != 1
        or record.get("status") not in {"prepared", "completed"}
        or record.get("source_sha") != source_sha
        or record.get("source_tree") != source_tree
        or not isinstance(gate, dict)
    ):
        raise BootstrapError("existing bootstrap delivery authority is invalid")
    return dict(gate)


def _install_exact(path: Path, payload: bytes, mode: int) -> str:
    # A prior crash can leave only a private staging name, or both a complete
    # hard-linked destination and its staging name.  It must never leave a
    # truncated final path.  The parent is deploy-user-owned mode 0700.
    for temporary in path.parent.glob(f".{path.name}.*.tmp"):
        metadata = temporary.lstat()
        if (
            temporary.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
        ):
            raise BootstrapError(f"immutable install staging file is unsafe: {temporary}")
        temporary.unlink()
    _fsync_directory(path.parent)
    if path.exists() or path.is_symlink():
        try:
            metadata = path.lstat()
            existing = path.read_bytes()
        except OSError as exc:
            raise BootstrapError(f"installed immutable file is unsafe: {path}") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != mode
            or existing != payload
        ):
            raise BootstrapError(
                f"refusing to overwrite a different immutable file: {path}"
            )
        return digest(existing)
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
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        # link(2) is an atomic no-replace publication.  A concurrent or stale
        # destination cannot be overwritten as os.replace() would do.
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        _fsync_directory(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    return digest(payload)


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
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_PAGER": "cat",
        "GIT_EDITOR": "/bin/false",
    }


def _git_command(root: Path, *arguments: str) -> list[str]:
    return [
        "git",
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
        f"core.worktree={root.absolute()}",
        *arguments,
    ]


def _validate_local_git_config(payload: bytes, *, label: str) -> None:
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
            # These three are explicitly overridden on every Git invocation.
            "worktree",
            "fsmonitor",
            "untrackedcache",
        },
        'remote "origin"': {"url", "fetch", "tagopt"},
        # VS Code records only a ref name here; Git does not execute it.
        'branch "main"': {"remote", "merge", "vscode-merge-base"},
        "user": {"name", "email"},
    }
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


def _read_git_policy(path: Path, *, root: Path) -> tuple[bytes, int]:
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
        _validate_local_git_config(payload, label=str(path))
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
        or parent.st_mode & 0o022
        or not stat.S_ISDIR(root_metadata.st_mode)
        or root.is_symlink()
        or root_metadata.st_uid != os.geteuid()
        or root_metadata.st_mode & 0o022
        or not stat.S_ISDIR(git_metadata.st_mode)
        or (root / ".git").is_symlink()
        or git_metadata.st_uid != os.geteuid()
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
            or metadata.st_mode & 0o022
        ):
            raise BootstrapError(f"bootstrap source directory is unsafe: {current}")
        for name in (*names, *files):
            child = current / name
            metadata = child.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o022
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


def _build_control_release(
    runtime_root: Path,
    *,
    control: object,
    source_sha: str,
    source_tree: str,
    allow_test: bool,
) -> tuple[dict[str, object], dict[str, object]]:
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
    if release.exists() or release.is_symlink():
        try:
            existing, root = control.load_control_release(runtime_root, release_id)
        except Exception as exc:
            raise BootstrapError("existing initial control release is invalid") from exc
        if existing != manifest or root != release:
            raise BootstrapError("existing initial control release differs")
    else:
        staging = release_parent / f".bootstrap-{os.urandom(12).hex()}"
        staging.mkdir(mode=0o700)
        try:
            for name, payload in payloads.items():
                _atomic_file(staging / name, payload, 0o700)
            _atomic_file(staging / control.CONTROL_MANIFEST_NAME, manifest_payload, 0o600)
            _fsync_directory(staging)
            os.rename(staging, release)
            _fsync_directory(release_parent)
        except BaseException:
            if staging.exists() and not staging.is_symlink():
                import shutil

                shutil.rmtree(staging)
            raise
        control.load_control_release(runtime_root, release_id)
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
        "prepared_at": dt.datetime.now(dt.timezone.utc).isoformat(),
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
        "activated_at": candidate["prepared_at"],
    }
    control.validate_active_control_record(active)
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
        path: _read_git_policy(path, root=root)
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
    flags = (
        os.O_RDWR
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    created = False
    try:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise BootstrapError("deploy.lock is unsafe")
        if created:
            os.fsync(descriptor)
            _fsync_directory(path.parent)
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
    if unit_state != expected_state:
        raise BootstrapError("existing Worker unit is not the enabled no-drop-in baseline")
    intent_path = runtime_root / "audit/bootstrap-worker-unit/takeover-intent.json"
    completed_path = runtime_root / "audit/bootstrap-worker-unit/takeover.json"
    intent_present = intent_path.exists() or intent_path.is_symlink()
    completed_present = completed_path.exists() or completed_path.is_symlink()
    mode = stat.S_IMODE(metadata.st_mode)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--production-root", default=str(PRODUCTION_ROOT))
    parser.add_argument("--runtime-root", default=str(RUNTIME_ROOT))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-production-root")
    parser.add_argument("--confirm-runtime-root")
    parser.add_argument("--confirm-source-tree")
    parser.add_argument("--confirm-worker-unit-sha256")
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
    try:
        if not allow_test:
            _assert_private_bootstrap_source(REPOSITORY_ROOT)
            _verify_git_object_database(REPOSITORY_ROOT)
        source_sha, source_tree = _source_identity(allow_test=allow_test)
        if args.sha != source_sha:
            raise BootstrapError(
                "requested bootstrap SHA differs from the clean source checkout"
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
        if allow_test:
            production_repository = _production_repository_identity(
                production_root, source_sha, allow_test=allow_test
            )
        else:
            production_repository = {
                "validation": "deferred-until-confirmed-apply",
                "expected_branch": "main",
                "expected_origin": REPOSITORY_SSH_URL,
                "target": source_sha,
            }
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
        _initialize_runtime_root(runtime_root)
        for relative, mode in DIRECTORIES.items():
            path = runtime_root / relative
            path.mkdir(parents=True, exist_ok=True, mode=mode)
            if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.geteuid():
                raise BootstrapError(f"runtime directory is unsafe: {path}")
            os.chmod(path, mode)
        lock = runtime_root / "state/deploy.lock"
        with _open_deploy_lock(lock) as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BootstrapError("another deployment holds deploy.lock") from exc
            locked_source = _source_identity(allow_test=allow_test)
            if locked_source != (source_sha, source_tree):
                raise BootstrapError("bootstrap source changed before installation")
            locked_gate = _delivery_gate(
                production_root,
                runtime_root,
                source_sha,
                allow_test=allow_test,
                sealed=delivery_gate,
            )
            if locked_gate != delivery_gate:
                raise BootstrapError("protected-main delivery evidence changed before install")
            _harden_checkout(production_root)
            locked_repository = _production_repository_identity(
                production_root, source_sha, allow_test=allow_test
            )
            if production_repository.get("validation") == (
                "deferred-until-confirmed-apply"
            ):
                production_repository = locked_repository
                plan["production_repository"] = locked_repository
            elif locked_repository != production_repository:
                raise BootstrapError("production Git identity changed before install")
            locked_unit = _preflight_worker_unit(
                runtime_root,
                worker_unit_path,
                expected_sha256=args.confirm_worker_unit_sha256,
                allow_test=allow_test,
            )
            if locked_unit != initial_unit_preflight:
                raise BootstrapError("Worker unit changed before takeover")
            plan["worker_unit_takeover"] = _take_over_worker_unit(
                runtime_root,
                worker_unit_path,
                expected_sha256=args.confirm_worker_unit_sha256,
                allow_test=allow_test,
            )
            installed = {
                name: _install_exact(runtime_root / "bin" / name, payload, mode)
                for name, (payload, mode) in immutable_payloads.items()
            }
            actual_bin = {entry.name for entry in (runtime_root / "bin").iterdir()}
            if actual_bin != set(IMMUTABLE_FILES):
                raise BootstrapError("runtime/bin contains non-immutable or missing controls")
            candidate, active = _build_control_release(
                runtime_root,
                control=control,
                source_sha=source_sha,
                source_tree=source_tree,
                allow_test=allow_test,
            )
            final_source = _source_identity(allow_test=allow_test)
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
            final_unit = _preflight_worker_unit(
                runtime_root,
                worker_unit_path,
                expected_sha256=args.confirm_worker_unit_sha256,
                allow_test=allow_test,
            )
            if (
                final_source != (source_sha, source_tree)
                or final_gate != delivery_gate
                or final_repository != production_repository
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
                "schema_version": 1,
                "source_sha": source_sha,
                "source_tree": source_tree,
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
        plan["status"] = "initialized"
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

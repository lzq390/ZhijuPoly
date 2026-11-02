"""Offline production identity checks shared by the PMD service entry points."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess

BWRAP = "/usr/bin/bwrap"

PRODUCTION_RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
PRODUCTION_SOCKET_PARENT = Path(f"/run/user/{os.getuid()}")
PRODUCTION_FIELDS = {"environment", "operation_id", "source_sha", "source_root", "socket_root"}


def reject_production_from_dev(runtime=None):
    if (os.environ.get("POLYMER_MD_ENVIRONMENT", "dev") not in {"", "dev", "disabled"}
            or runtime is not None and Path(runtime).is_relative_to(PRODUCTION_RUNTIME_ROOT)):
        raise ValueError("development entry point cannot use a production environment or runtime")


def validate_production_binding(values):
    if (values.get("environment") != "prod" or not isinstance(values.get("operation_id"), str)
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", values["operation_id"]) is None):
        raise ValueError("production configuration requires an explicit environment and operation ID")
    if not isinstance(values.get("source_sha"), str) or re.fullmatch(r"[0-9a-f]{40}", values["source_sha"]) is None:
        raise ValueError("production configuration requires a full source SHA")
    runtime, source, sockets = (Path(values[key]) for key in ("runtime_root", "source_root", "socket_root"))
    if runtime != PRODUCTION_RUNTIME_ROOT / "state" / values["operation_id"] / "pmd":
        raise ValueError("production PMD runtime must use its operation's independent state directory")
    if sockets.parent != PRODUCTION_SOCKET_PARENT or not sockets.name.startswith("nexpoly-"):
        raise ValueError("production sockets require a separate short owned runtime directory")
    if source != runtime.parent / "releases" / values["source_sha"]:
        raise ValueError("production frozen source must use the same operation's releases/full-SHA directory")


def verify_source_binding(values, files):
    """Bind each frozen source byte to an existing, clean, full Git commit."""
    source, sha = values["source_root"], values["source_sha"]
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_OPTIONAL_LOCKS"] = "0"

    def git(*args):
        try:
            return subprocess.check_output(["/usr/bin/git", "-C", source, *args], env=environment,
                                           stderr=subprocess.DEVNULL, timeout=30)
        except (subprocess.SubprocessError, OSError) as exc:
            raise ValueError("production frozen source cannot be verified") from exc

    if (git("rev-parse", "--verify", "HEAD").decode().strip() != sha
            or git("status", "--porcelain=v1", "--untracked-files=all")):
        raise ValueError("production source must be a clean checkout of the configured full SHA")
    for item in files:
        if hashlib.sha256(git("show", f"{sha}:{item['path']}")).hexdigest() != item["sha256"]:
            raise ValueError("production frozen release differs from the configured source commit")


def sandbox_worker_command(values, arguments):
    if not Path(BWRAP).is_file() or not os.access(BWRAP, os.X_OK):
        raise ValueError("production polymer MD requires bubblewrap for enforced read-only dependencies")
    # The host's user systemd ignores ReadOnlyPaths. Bubblewrap was independently
    # verified to preserve the listening FD and user-systemd execution scopes.
    # Verify the actual mount view once more inside the namespace before exec.
    check = ("import os,sys; paths=sys.argv[1:5]; "
             "assert all(os.statvfs(p).f_flag & os.ST_RDONLY for p in paths), 'production dependencies are not read-only'; "
             "assert not os.statvfs(sys.argv[5]).f_flag & os.ST_RDONLY, 'production runtime is not writable'; "
             "os.execve(sys.argv[6],sys.argv[6:],os.environ)")
    command = [BWRAP, "--unshare-user", "--die-with-parent", "--ro-bind", "/", "/",
               "--bind", values["runtime_root"], values["runtime_root"],
               "--bind", values["socket_root"], values["socket_root"], "--proc", "/proc", "--dev", "/dev",
               "/usr/bin/python3", "-I", "-B", "-c", check,
               values["source_root"], values["release_root"], values["runner_python"], values["worker_python"],
               values["runtime_root"], *arguments]
    return BWRAP, command

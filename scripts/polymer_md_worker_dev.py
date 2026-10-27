#!/usr/bin/env python3
"""Run or control one private, frozen polymer MD development Worker.

Provisioning and systemd resource limits belong to the operator. This entry
point accepts only an external JSON configuration; it never creates an engine,
copies jobs, manages services, or reads a database credential.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workers.polymer_md_worker.app.release_contract import verify_release

FIELDS = {"schema_version", "runtime_root", "release_root", "runtime_manifest",
          "runner_python", "worker_python", "job_root", "socket_path",
          "start_authorization_url", "start_authorization_token_file"}
PATH_FIELDS = FIELDS - {"schema_version", "start_authorization_url"}
CPU_COUNT, MEMORY_BYTES = 16, 32 * 1024**3


def absolute_path(value):
    if (not isinstance(value, str) or not value or any(ord(c) < 32 for c in value)
            or not Path(value).is_absolute() or value.startswith("//") or str(Path(value)) != value
            or ".." in Path(value).parts):
        raise ValueError("configuration paths must be canonical absolute paths")
    path = Path(value)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("configuration paths must not contain symbolic links")
    return path


def owned(path, mode, *, directory=False):
    absolute_path(str(path))
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != mode or (not directory and info.st_nlink != 1)):
        raise ValueError("private paths must be owned ordinary directories (0700) or files (0600)")
    return info


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON configuration field")
        result[key] = value
    return result


def read_private_json(path):
    if owned(path, 0o600).st_size > 32 * 1024 * 1024:
        raise ValueError("private JSON is too large")
    return json.loads(path.read_text(), object_pairs_hook=_unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid JSON number")))


@dataclass(frozen=True)
class Configuration:
    values: dict
    token: str

    def path(self, key):
        return Path(self.values[key])


def load_configuration(path):
    path = absolute_path(str(path))
    if owned(path, 0o600).st_size > 16 * 1024:
        raise ValueError("Worker configuration is too large")
    values = read_private_json(path)
    if (not isinstance(values, dict) or set(values) != FIELDS
            or type(values["schema_version"]) is not int or values["schema_version"] != 1):
        raise ValueError("Worker configuration fields or schema version are invalid")
    paths = {key: absolute_path(values[key]) for key in PATH_FIELDS}
    runtime, release = paths["runtime_root"], paths["release_root"]
    legacy = Path.home() / ".local/share/nexpoly/polymer-md-dev"
    if (runtime.is_relative_to(ROOT) or ROOT.is_relative_to(runtime)
            or runtime.is_relative_to(legacy) or legacy.is_relative_to(runtime)
            or runtime.is_relative_to(release) or release.is_relative_to(runtime)):
        raise ValueError("Worker runtime must be external and separate from source, release and legacy runtime")
    owned(runtime, 0o700, directory=True)
    if paths["job_root"] != runtime / "jobs" or paths["socket_path"] != runtime / "socket/worker.sock":
        raise ValueError("Worker jobs and socket must use this runtime's private layout")
    for key in ("runtime_manifest", "start_authorization_token_file"):
        if not paths[key].is_relative_to(runtime):
            raise ValueError("Worker manifest and authorization token must belong to its private runtime")
    if not path.is_relative_to(runtime):
        raise ValueError("Worker configuration must belong to its private runtime")
    for target in (path.parent, paths["runtime_manifest"].parent, paths["start_authorization_token_file"].parent,
                   paths["job_root"], paths["socket_path"].parent):
        for parent in (target, *target.parents):
            if parent.is_relative_to(runtime):
                owned(parent, 0o700, directory=True)
    if len(os.fsencode(paths["socket_path"])) > 107:
        raise ValueError("Worker Unix socket path is too long")
    for key in ("runner_python", "worker_python"):
        info = paths[key].lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or not os.access(paths[key], os.X_OK):
            raise ValueError("Worker and runner Python must be owned ordinary executables")
    owned(paths["runtime_manifest"], 0o600)
    token_path = paths["start_authorization_token_file"]
    if owned(token_path, 0o600).st_size > 4096:
        raise ValueError("Worker authorization token is invalid")
    token = token_path.read_text().strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token) is None:
        raise ValueError("Worker authorization token is invalid")
    url = values["start_authorization_url"]
    if not isinstance(url, str) or url != url.strip() or any(ord(c) < 32 for c in url):
        raise ValueError("Worker authorization URL must be a private loopback HTTP endpoint")
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path != "/internal/polymer-md" or not parsed.port):
        raise ValueError("Worker authorization URL must be a private loopback HTTP endpoint")
    return Configuration(values, token)


def verify_configuration(config):
    release = config.path("release_root")
    owned(release, 0o700, directory=True)
    # The inventory contract ignores __pycache__, but Python can load cached
    # bytecode even when bytecode writing is disabled. Accept source only.
    if any(path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"} for path in release.rglob("*")):
        raise ValueError("Worker release must not contain compiled Python caches")
    manifest = verify_release(release)
    if manifest["source_digest"] != release.name:
        raise ValueError("Worker release directory differs from its content identity")
    for target in (release / "release.json", *(release / item["path"] for item in manifest["files"])):
        info = target.lstat()
        if info.st_uid != os.getuid() or info.st_mode & 0o222:
            raise ValueError("Worker release files must be owned and immutable")
    names = {item["path"] for item in manifest["files"]}
    module_path = "workers/polymer_md_worker/app/runtime.py"
    if not {module_path, "workers/polymer_md_worker/app/main.py"} <= names:
        raise ValueError("Worker release lacks its runtime or HTTP entry point")
    # The selected release's adapter fingerprint must be used, even when this
    # operator entry point is running from a newer checkout.
    spec = importlib.util.spec_from_file_location("_polymer_md_frozen_runtime", release / module_path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    runtime = module.probe_runtime(config.path("runtime_manifest"))
    if runtime.get("patch_version") != "nexpoly-md-10":
        raise ValueError("Worker requires the sealed md10 runtime")
    runner = str(config.path("runner_python"))
    if runtime.get("python") != runner or not any(item.get("path") == runner for item in runtime.get("verified_files", [])):
        raise ValueError("Worker runner Python differs from its sealed runtime")
    return manifest["source_digest"], runtime


def clean_environment(config):
    allowed = {"HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
               "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment.update(PATH="/usr/bin:/bin", PYTHONPATH=str(config.path("release_root")),
        PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", CUDA_VISIBLE_DEVICES="", NVIDIA_VISIBLE_DEVICES="none",
        OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1",
        POLYMER_MD_JOB_ROOT=str(config.path("job_root")), POLYMER_MD_ARTIFACT_ROOT=str(config.path("job_root")),
        POLYMER_MD_WORKER_UDS=str(config.path("socket_path")), POLYMER_MD_SOCKET_DIR=str(config.path("socket_path").parent),
        POLYMER_MD_RUNTIME_MANIFEST=str(config.path("runtime_manifest")),
        POLYMER_MD_RUNNER_PYTHON=str(config.path("runner_python")), POLYMER_MD_START_DRAINED="true",
        POLYMER_MD_START_AUTHORIZATION_URL=config.values["start_authorization_url"],
        POLYMER_MD_START_AUTHORIZATION_TOKEN=config.token)
    return environment


def _socket_info(path):
    absolute_path(str(path))
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("Worker socket must be an owned private Unix socket (0600)")
    return info


def run_worker(config, runtime):
    if runtime.get("ready") is not True:
        raise ValueError("Worker sealed runtime is not ready")
    lock_path = config.path("runtime_root") / ".worker-launch.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    listener = None
    bound_identity = None
    path = config.path("socket_path")
    try:
        owned(lock_path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists() or path.is_symlink():
            old = _socket_info(path)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(1)
                try:
                    probe.connect(str(path))
                except ConnectionRefusedError:
                    current = _socket_info(path)
                    if (current.st_dev, current.st_ino) != (old.st_dev, old.st_ino):
                        raise ValueError("Worker socket changed during startup")
                    path.unlink()
                else:
                    raise ValueError("another Worker already owns this private socket")
        os.umask(0o077)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        path.chmod(0o600)
        bound_identity = _socket_info(path)
        listener.listen(128)
        listener.set_inheritable(True)
        os.set_inheritable(descriptor, True)
        python = str(config.path("worker_python"))
        os.chdir(config.path("release_root"))
        os.execve(python, [python, "-m", "uvicorn", "workers.polymer_md_worker.app.main:app",
                         "--fd", str(listener.fileno()), "--workers", "1", "--no-access-log"], clean_environment(config))
    finally:
        if listener is not None:
            listener.close()
        os.close(descriptor)
        if bound_identity is not None and path.exists():
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (bound_identity.st_dev, bound_identity.st_ino):
                path.unlink()


def _request(connection, method, endpoint):
    connection.request(method, endpoint, headers={"Host": "polymer-md"})
    response = connection.getresponse()
    body = response.read(32 * 1024 * 1024 + 1)
    if response.status != 200 or len(body) > 32 * 1024 * 1024:
        raise ValueError("Worker control response is unsuccessful or too large")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("Worker health response is invalid")
    return value


def _assert_identity(health, digest, runtime, instance=None):
    remote_runtime = health.get("runtime")
    if (health.get("protocol_version") != 1 or health.get("worker_release_digest") != digest
            or not isinstance(health.get("worker_instance_id"), str) or not health["worker_instance_id"]
            or (instance is not None and health["worker_instance_id"] != instance)
            or not isinstance(remote_runtime, dict)
            or remote_runtime.get("runtime_fingerprint") != runtime.get("runtime_fingerprint")):
        raise ValueError("Worker release, runtime or instance identity differs from configuration")


def healthy_for_resume(health, runtime):
    return (runtime.get("ready") is True and all(health.get(key) is True for key in
            ("runtime_ready", "analysis_ready", "scope_ready"))
            and health.get("start_authorization_version") == 1
            and not any(health.get(key) for key in ("recovering", "fatal_error", "monitor_degraded", "stopping"))
            and health.get("cpu_count") == CPU_COUNT and health.get("memory_bytes") == MEMORY_BYTES
            and health.get("gpu_count") == 0 and health.get("max_running_jobs") == 1)


def summary(health):
    keys = ("protocol_version", "worker_instance_id", "worker_release_digest", "ready", "accepting_jobs",
            "runtime_ready", "analysis_ready", "scope_ready", "draining", "recovering", "monitor_degraded",
            "queued_jobs", "running_jobs", "max_running_jobs", "cpu_count", "memory_bytes", "gpu_count")
    result = {key: health.get(key) for key in keys}
    result["runtime"] = {key: health.get("runtime", {}).get(key) for key in ("patch_version", "runtime_fingerprint", "ready")}
    return result


def control(config, action, digest, runtime):
    _socket_info(config.path("socket_path"))
    connection = http.client.HTTPConnection("polymer-md", timeout=30)
    # Reuse exactly this accepted Unix connection. A server replacement must
    # fail the command rather than silently reconnecting before a mutation.
    connection.auto_open = 0
    connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.sock.settimeout(30)
        connection.sock.connect(str(config.path("socket_path")))
        health = _request(connection, "GET", "/health")
        _assert_identity(health, digest, runtime)
        if action != "check":
            if action == "resume" and not healthy_for_resume(health, runtime):
                raise ValueError("Worker must have ready runtime, scope and analysis before resume")
            result = _request(connection, "POST", "/" + action)
            _assert_identity(result, digest, runtime, health["worker_instance_id"])
            if (action == "drain" and (result.get("draining") is not True or result.get("accepting_jobs") is not False)
                    or action == "resume" and (result.get("ready") is not True or result.get("draining") is not False)):
                raise ValueError("Worker did not confirm the requested admission state")
            health = result
        print(json.dumps(summary(health), sort_keys=True))
        return 0 if action == "drain" or healthy_for_resume(health, runtime) else 1
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run-worker", "check", "drain", "resume"))
    parser.add_argument("--config", required=True, type=Path, help="absolute private JSON configuration (schema_version=1)")
    args = parser.parse_args(argv)
    config = load_configuration(args.config)
    digest, runtime = verify_configuration(config)
    if args.action == "run-worker":
        run_worker(config, runtime)
        return 0
    return control(config, args.action, digest, runtime)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, http.client.HTTPException) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        raise SystemExit(1)

"""Validated development GPU1 cohort configuration and exact early bindings.

No GPU authority is created here. The existing controller remains the sole
Broker/MPS descriptor owner; this module selects its private lifecycle paths.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
FROZEN_RUNTIME_KEYS = frozenset({"NEXPOLY_ASSET_ROOT", "BYTEFF2_ROOT", "BYTEFF2_OPENMM_DIR",
    "MONOMER_MD_DEV_WORKER_PYTHON", "MONOMER_MD_DEV_WORKER_BASE_PYTHON",
    "MONOMER_MD_DEV_WORKER_BASE_PYTHON_IDENTITY_SHA256", "MONOMER_MD_DEV_WORKER_WHEELHOUSE",
    "MONOMER_MD_DEV_BYTEFF2_OPENMM_DIR", "MONOMER_MD_DEV_WORKER_ENABLED"})
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from gpu_resource.transient_scope import ScopeResourceLimits


class CohortError(RuntimeError):
    pass


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def private_bytes(path: Path, *, maximum: int = 65536,
                  expected_identity: tuple[int, int] | None = None) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if expected_identity is not None and (before.st_dev, before.st_ino) != expected_identity:
            raise CohortError("cohort configuration file identity differs")
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_gid != os.getegid() or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size > maximum):
            raise CohortError("cohort record is not an owner-private bounded regular file")
        body = os.read(descriptor, maximum + 1)
        after = os.fstat(descriptor)
        current = path.lstat()
        if (len(body) > maximum or len(body) != before.st_size
                or (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
                != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)
                or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)):
            raise CohortError("cohort record changed while it was read")
        return body
    finally:
        os.close(descriptor)


def private_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(private_bytes(path))
    except (ValueError, UnicodeError) as exc:
        raise CohortError("cohort record JSON is invalid") from exc
    if not isinstance(value, dict):
        raise CohortError("cohort record must be an object")
    return value


def private_directory(path: Path, *, create: bool = False) -> None:
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    metadata = path.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or stat.S_IMODE(metadata.st_mode) != 0o700):
        raise CohortError("cohort directory identity or permissions differ")


def write_once(path: Path, value: dict[str, Any]) -> None:
    body = canonical_json(value)
    if path.exists() or path.is_symlink():
        if private_bytes(path) != body:
            raise CohortError("existing cohort binding differs")
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".cohort-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = -1
        # link is an atomic, no-replace publication; never overwrite a binding.
        os.link(temporary, path, follow_symlinks=False)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.unlink(temporary)


@dataclass(frozen=True)
class DevGpuCohort:
    path: Path
    sha256: str
    device: int
    inode: int
    repo_root: Path
    runtime_root: Path
    cohort_id: str
    source_sha: str
    source_tree: str
    compose_project: str
    frontend_port: int
    backend_port: int
    auth_env_file: Path | None
    md_resources: ScopeResourceLimits
    dft_resources: ScopeResourceLimits
    backend_resources: ScopeResourceLimits
    prior_backend: dict[str, str] | None
    systemd_inventory_provider: str = "legacy"

    @classmethod
    def load(cls, path: Path, sha256: str, *, repo_root: Path = REPO_ROOT,
             expected_identity: tuple[int, int] | None = None) -> DevGpuCohort:
        if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
            raise CohortError("cohort requires an exact SHA256")
        if not path.is_absolute() or str(path) != os.path.normpath(str(path)):
            raise CohortError("cohort path must be canonical and absolute")
        if repo_root == Path("/data/lzq/gith/nexpoly"):
            raise CohortError("native development cohort is forbidden in the production repository")
        private_directory(repo_root / ".runtime")
        private_directory(repo_root / ".runtime/cohorts")
        relative = path.relative_to(repo_root / ".runtime/cohorts")
        if (len(relative.parts) != 2 or relative.parts[1] != "cohort.json"
                or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", relative.parts[0]) is None):
            raise CohortError("cohort path is outside the fixed configuration layout")
        private_directory(path.parent)
        metadata = path.lstat()
        identity = (metadata.st_dev, metadata.st_ino)
        if expected_identity is not None and identity != expected_identity:
            raise CohortError("cohort configuration file was replaced")
        body = private_bytes(path, expected_identity=identity)
        if hashlib.sha256(body).hexdigest() != sha256:
            raise CohortError("cohort configuration digest differs")
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise CohortError("cohort configuration JSON is invalid") from exc
        keys = {"schema_version", "cohort_id", "repo_root", "source_sha", "source_tree",
                "compose_project", "frontend_port", "backend_port", "auth_env_file",
                "md_resources", "dft_resources", "backend_resources", "prior_backend"}
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] not in {1, 2}:
            raise CohortError("cohort configuration schema differs")
        provider = "legacy"
        if value["schema_version"] == 2:
            keys.add("systemd_inventory_provider")
            provider = value.get("systemd_inventory_provider")
            if provider != "root-unix-v1":
                raise CohortError("cohort systemd inventory provider differs")
        if set(value) != keys:
            raise CohortError("cohort configuration schema differs")
        cohort_id = value["cohort_id"]
        if not isinstance(cohort_id, str) or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", cohort_id) is None:
            raise CohortError("cohort id is invalid")
        runtime_root = repo_root / ".runtime/cohorts" / cohort_id
        if path != runtime_root / "cohort.json" or value["repo_root"] != str(repo_root):
            raise CohortError("cohort configuration belongs to another path or source root")
        private_directory(runtime_root)
        for key in ("source_sha", "source_tree"):
            if not isinstance(value[key], str) or re.fullmatch(r"[0-9a-f]{40}", value[key]) is None:
                raise CohortError("cohort source identity is invalid")
        project = value["compose_project"]
        if not isinstance(project, str) or re.fullmatch(r"nexpoly_(?:dev_cohort|accept)_[a-z0-9_]{1,63}", project) is None:
            raise CohortError("cohort Compose project must be a private development project")
        for key in ("frontend_port", "backend_port"):
            if type(value[key]) is not int or not 1024 <= value[key] <= 65535:
                raise CohortError("cohort loopback port is invalid")
        if value["frontend_port"] == value["backend_port"]:
            raise CohortError("cohort ports must differ")
        resources = []
        resource_keys = {"cpu_quota_percent", "memory_max_bytes", "memory_swap_max_bytes", "tasks_max"}
        for key in ("md_resources", "dft_resources", "backend_resources"):
            if not isinstance(value[key], dict) or set(value[key]) != resource_keys:
                raise CohortError("cohort scope resources require all fixed fields")
            try:
                profile = ScopeResourceLimits(**value[key])
            except (RuntimeError, TypeError, ValueError) as exc:
                raise CohortError("cohort scope resource profile is invalid") from exc
            if not profile.configured:
                raise CohortError("cohort scope resources must be configured")
            resources.append(profile)
        auth = value["auth_env_file"]
        auth_path = None
        if auth is not None:
            if not isinstance(auth, str) or "\n" in auth or "\r" in auth:
                raise CohortError("cohort auth environment path is invalid")
            auth_path = Path(auth)
            if (not auth_path.is_absolute() or str(auth_path) != os.path.normpath(str(auth_path))
                    or not (auth_path == repo_root / ".env.dev.auth" or auth_path.is_relative_to(runtime_root))):
                raise CohortError("cohort auth environment is outside its private configuration")
            if auth_path.name not in {".env.dev.auth", "backend.auth.env"}:
                raise CohortError("cohort auth environment must use its native role filename")
            # Validate metadata without reading credentials or DSNs.
            metadata = auth_path.lstat()
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                    or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600
                    or auth_path.resolve() != auth_path):
                raise CohortError("cohort auth environment is not owner-private")
        prior = value["prior_backend"]
        if prior is not None:
            if (not isinstance(prior, dict) or set(prior) != {"container_id", "image_id"}
                    or re.fullmatch(r"[0-9a-f]{64}", str(prior.get("container_id"))) is None
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", str(prior.get("image_id"))) is None):
                raise CohortError("prior private Backend requires exact container and image identity")
        return cls(path, sha256, *identity, repo_root, runtime_root, cohort_id,
                   value["source_sha"], value["source_tree"], project,
                   value["frontend_port"], value["backend_port"], auth_path,
                   resources[0], resources[1], resources[2], prior, provider)

    @property
    def backend_image(self) -> str:
        return f"nexpoly-dev-cohort-{self.cohort_id}:{self.source_sha}"

    def path_for(self, name: str) -> Path:
        return self.runtime_root / name

    def require_source(self, source_sha: str, source_tree: str) -> None:
        if (source_sha, source_tree) != (self.source_sha, self.source_tree):
            raise CohortError("cohort source differs from the startup checkout")

    def verify(self) -> None:
        if DevGpuCohort.load(self.path, self.sha256, repo_root=self.repo_root,
                            expected_identity=(self.device, self.inode)) != self:
            raise CohortError("cohort configuration identity changed")

    def environment(self) -> dict[str, str]:
        result = {
            "NEXPOLY_DEV_GPU_COHORT_CONFIG": str(self.path),
            "NEXPOLY_DEV_GPU_COHORT_SHA256": self.sha256,
            "NEXPOLY_DEV_GPU_COHORT_DEVICE": str(self.device),
            "NEXPOLY_DEV_GPU_COHORT_INODE": str(self.inode),
            "NEXPOLY_DEV_GPU_COHORT_SOURCE_SHA": self.source_sha,
            "NEXPOLY_DEV_GPU_COHORT_SOURCE_TREE": self.source_tree,
            "NEXPOLY_DEV_GPU_COHORT_RUNTIME_ROOT": str(self.runtime_root),
            "NEXPOLY_DEV_COMPOSE_PROJECT": self.compose_project,
            "NEXPOLY_DEV_BACKEND_IMAGE": self.backend_image,
            "NEXPOLY_DEV_GPU_COHORT_COMPOSE_FILE": str(self.path_for("cohort-compose.yml")),
            "NEXPOLY_DEV_FRONTEND_PORT": str(self.frontend_port),
            "NEXPOLY_DEV_BACKEND_PORT": str(self.backend_port),
            "NEXPOLY_DEV_GPU_LAUNCHER_ENABLED": "false",
            "NEXPOLY_DEV_USER_ISOLATION_ENABLED": "true" if self.auth_env_file else "false",
            "NEXPOLY_DEV_AUTH_ENV_FILE": str(self.auth_env_file) if self.auth_env_file else "",
            "NEXPOLY_GPU_STATE_ROOT": str(self.path_for("gpu-resource")),
            "MONOMER_MD_DEV_WORKER_SOCKET_DIR": str(self.path_for("md-socket")),
            "MONOMER_MD_DEV_WORKER_JOB_ROOT": str(self.path_for("md-runs")),
            "MONOMER_MD_DEV_CANARY_STATE_DIR": str(self.path_for("md-canaries")),
            "MONOMER_DFT_DEV_WORKER_SOCKET_DIR": str(self.path_for("dft-socket")),
            "MONOMER_DFT_DEV_DOWNLOAD_SPOOL_DIR": str(self.path_for("dft-spool")),
            "MONOMER_DFT_WORKER_UDS": str(self.path_for("dft-socket/worker.sock")),
            "MONOMER_DFT_JOB_ROOT": str(self.path_for("dft-runs")),
            "MONOMER_DFT_GPU_BROKER_UDS": str(self.path_for("gpu-resource/broker.sock")),
            "MONOMER_DFT_GPU_MPS_PIPE_ROOT": str(self.path_for("gpu-resource")),
            "MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS": str(self.path_for("gpu-resource/external-reservations.json")),
            "MONOMER_DFT_GPU_BROKER_ENABLED": "1",
            "MONOMER_DFT_STANDALONE_GPU_SMOKE": "0",
            "MONOMER_DFT_DEPLOYMENT": "dev",
        }
        for component, profile in (("MD", self.md_resources), ("DFT", self.dft_resources)):
            for suffix, number in (("CPU_QUOTA_PERCENT", profile.cpu_quota_percent),
                                   ("MEMORY_MAX_BYTES", profile.memory_max_bytes),
                                   ("MEMORY_SWAP_MAX_BYTES", profile.memory_swap_max_bytes),
                                   ("TASKS_MAX", profile.tasks_max)):
                result[f"MONOMER_{component}_GPU_SCOPE_{suffix}"] = str(number)
        return result

    def systemd_configuration_options(self) -> dict:
        self.verify()
        if self.systemd_inventory_provider == "legacy":
            return {}
        if self.systemd_inventory_provider != "root-unix-v1":
            raise CohortError("cohort systemd inventory provider differs")
        from functools import partial
        from gpu_resource.systemd_inventory import configuration_client
        return {"system_configuration_factory": partial(configuration_client,
                    expected_source_sha=self.source_sha, expected_source_tree=self.source_tree)}

    def compose_bytes(self) -> bytes:
        # Only these fixed, secret-free native selections are added. The
        # original development network/security contract is reused exactly.
        return ("services:\n  backend:\n    image: " + json.dumps(self.backend_image)
                + "\n    container_name: " + json.dumps(self.compose_project + "-backend")
                + "\n    labels:\n      com.nexpoly.dev.cohort-sha256: " + json.dumps(self.sha256)
                + "\n      com.nexpoly.dev.cohort-operation-id: ${NEXPOLY_DEV_GPU_COHORT_OPERATION_ID:-unprepared}"
                + "\n    cpus: " + str(self.backend_resources.cpu_quota_percent / 100)
                + "\n    mem_limit: " + str(self.backend_resources.memory_max_bytes)
                + "\n    memswap_limit: " + str(self.backend_resources.memory_max_bytes + self.backend_resources.memory_swap_max_bytes)
                + "\n    pids_limit: " + str(self.backend_resources.tasks_max)
                + "\n    command: [\"/bin/sh\", \"-ec\", \"cd /app/backend/app; exec uvicorn app.main:app --app-dir /app/backend --host 0.0.0.0 --port 8000 --workers 1 --timeout-graceful-shutdown 40\"]"
                + "\n    volumes: !override\n"
                + "      - ${NEXPOLY_ASSET_ROOT:?set pinned asset release}/backend-data:/app/backend/data:ro\n"
                + "      - ${NEXPOLY_ASSET_ROOT:?set pinned asset release}/database:/app/database:ro\n"
                + "      - ${NEXPOLY_ASSET_ROOT:?set pinned asset release}/model:/app/model:ro\n"
                + "".join("      - type: bind\n        source: " + json.dumps(str(self.path_for(name)))
                          + "\n        target: " + json.dumps(target)
                          + "\n        read_only: " + str(read_only).lower()
                          + "\n        bind:\n          create_host_path: false\n"
                          for name, target, read_only in self.runtime_mounts())
                + "\nnetworks:\n  default: !override\n    external: true\n    name: nexpoly_dev_default\n").encode()

    def require_prepared(self) -> None:
        if private_bytes(self.path_for("cohort-compose.yml")) != self.compose_bytes():
            raise CohortError("cohort Compose selection differs from its frozen configuration")
        for name, _target, _read_only in self.runtime_mounts():
            private_directory(self.path_for(name))

    @staticmethod
    def runtime_mounts() -> tuple[tuple[str, str, bool], ...]:
        return (("batch-state", "/app/.runtime/monomer-polymerization-batch", False),
                ("md-socket", "/app/monomer-md-worker", False),
                ("md-canaries", "/app/monomer-md-canaries", False),
                ("dft-socket", "/app/monomer-dft-worker", True),
                ("dft-spool", "/app/.runtime/monomer-dft-download-spool", False))

    def prepare(self) -> None:
        for name in ("md-socket", "md-runs", "md-canaries", "dft-socket", "dft-runs",
                      "dft-spool", "home", "tmp", "xdg-cache", "gpu-session", "batch-state"):
            private_directory(self.path_for(name), create=True)
        path = self.path_for("cohort-compose.yml")
        if path.exists() or path.is_symlink():
            self.require_prepared()
        else:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(self.compose_bytes())
                stream.flush()
                os.fsync(stream.fileno())

    def reservations(self, source: bytes) -> bytes:
        value = json.loads(source)
        registration = value.get("managed_docker_claims", {}).get("backend-dev")
        if (not isinstance(registration, dict) or registration.get("compose_project") != "nexpoly_dev"
                or registration.get("component") != "backend" or registration.get("environment") != "dev"
                or registration.get("compose_service") != "backend"
                or registration.get("gpu_uuids") != ["GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771"]):
            raise CohortError("native backend registration differs from the fixed development contract")
        registration["compose_project"] = self.compose_project
        return canonical_json(value)

    def binding(self, session_id: str) -> dict[str, Any]:
        if re.fullmatch(r"[0-9a-f]{32}", session_id) is None:
            raise CohortError("cohort binding requires an exact session")
        return {"schema_version": 1, "cohort_sha256": self.sha256,
                "cohort_device": self.device, "cohort_inode": self.inode,
                "source_sha": self.source_sha, "source_tree": self.source_tree,
                "session_id": session_id}

    def backend_record_path(self, session_id: str) -> Path:
        self.binding(session_id)
        return self.path_for(f"backend-candidate-{session_id}.json")

    def backend_container_id(self, session_id: str) -> str | None:
        self.verify()
        path = self.backend_record_path(session_id)
        if not path.exists() and not path.is_symlink():
            return None
        value = private_json(path)
        baseline = self.binding(session_id)
        keys = {*baseline, "container_id", "image_id", "config_hash", "compose_project", "compose_service", "operation_id"}
        if (set(value) != keys or any(value.get(k) != v for k, v in baseline.items())
                or value["compose_project"] != self.compose_project or value["compose_service"] != "backend"
                or re.fullmatch(r"[0-9a-f]{64}", str(value["container_id"])) is None):
            raise CohortError("early Backend record is foreign")
        return value["container_id"]


def process_identity(pid: int) -> tuple[int, list[str]]:
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    fields = raw[raw.rfind(")") + 2:].split()
    if fields[0] == "Z":
        raise CohortError("cohort process has exited")
    argv = [item.decode() for item in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if item]
    return int(fields[19]), argv


def dft_binding(cohort: DevGpuCohort, session_id: str, pid: int, start_ticks: int) -> dict[str, Any]:
    ticks, argv = process_identity(pid)
    runner = str(cohort.repo_root / "workers/monomer_dft_worker/run_host_worker.sh")
    expected_python = str(cohort.repo_root / ".runtime/venvs/monomer-dft-worker/bin/python")
    if ticks != start_ticks or len(argv) != 2 or argv[1] != runner or Path(argv[0]).name != "bash":
        raise CohortError("DFT supervisor PID/start/full argv differs")
    if Path(os.readlink(f"/proc/{pid}/exe")).resolve() != Path("/bin/bash").resolve():
        raise CohortError("DFT supervisor executable differs")
    observed = {}
    wanted = {"NEXPOLY_DEV_GPU_SESSION_ID": session_id,
              "NEXPOLY_DEV_GPU_COHORT_CONFIG": str(cohort.path),
              "NEXPOLY_DEV_GPU_COHORT_SHA256": cohort.sha256,
              "NEXPOLY_DEV_GPU_COHORT_DEVICE": str(cohort.device),
              "NEXPOLY_DEV_GPU_COHORT_INODE": str(cohort.inode),
              "MONOMER_DFT_WORKER_UDS": str(cohort.path_for("dft-socket/worker.sock")),
              "MONOMER_DFT_PYTHON": expected_python}
    for entry in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
        key, separator, value = entry.partition(b"=")
        if separator and key.decode(errors="ignore") in wanted:
            observed[key.decode()] = value.decode()
    if observed != wanted or process_identity(pid) != (ticks, argv):
        raise CohortError("DFT supervisor cohort environment differs")
    return {**cohort.binding(session_id), "pid": pid, "start_ticks": ticks,
            "argv": argv, "python": expected_python, "socket": wanted["MONOMER_DFT_WORKER_UDS"]}


def docker_identity(container_id: str) -> dict[str, Any] | None:
    if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
        raise CohortError("Backend requires a full Docker container ID")
    template = '{"id":{{json .Id}},"image":{{json .Image}},"name":{{json .Name}},"running":{{json .State.Running}},"devices":{{json .HostConfig.DeviceRequests}},"labels":{{json .Config.Labels}}}'
    completed = subprocess.run(("docker", "inspect", "--format", template, container_id),
                               capture_output=True, text=True, timeout=15)
    if completed.returncode:
        if "No such object" in completed.stderr or "No such container" in completed.stderr:
            return None
        raise CohortError("Backend Docker identity could not be inspected")
    value = json.loads(completed.stdout)
    if value.get("id") != container_id:
        raise CohortError("Backend container identity differs")
    return value


def backend_binding(cohort: DevGpuCohort, session_id: str, container_id: str,
                    config_hash: str, identity: dict[str, Any]) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{64}", config_hash) is None:
        raise CohortError("Backend config hash is invalid")
    labels = identity.get("labels") or {}
    expected = {"com.docker.compose.project": cohort.compose_project,
                "com.docker.compose.service": "backend",
                "com.nexpoly.gpu.registration": "backend-dev",
                "com.nexpoly.gpu.component": "backend",
                "com.nexpoly.gpu.environment": "dev",
                "com.nexpoly.dev.cohort-sha256": cohort.sha256,
                "com.nexpoly.gpu.session-id": session_id,
                "com.nexpoly.dev.config-hash": config_hash}
    operation_id = labels.get("com.nexpoly.dev.cohort-operation-id")
    if (identity.get("id") != container_id or any(labels.get(k) != v for k, v in expected.items())
            or re.fullmatch(r"[0-9a-f]{32}", str(operation_id)) is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}", str(identity.get("image"))) is None):
        raise CohortError("Backend early binding labels/image/identity differ")
    return {**cohort.binding(session_id), "container_id": container_id,
            "image_id": identity["image"], "config_hash": config_hash,
            "compose_project": cohort.compose_project, "compose_service": "backend", "operation_id": operation_id}


def backend_service_ids(cohort: DevGpuCohort) -> set[str]:
    identifiers: set[str] = set()
    filters = (("label=com.docker.compose.project=" + cohort.compose_project,
                "label=com.docker.compose.service=backend"),
               ("name=" + cohort.compose_project + "-backend",))
    for selection in filters:
        command = ["docker", "ps", "--all", "--quiet", "--no-trunc"]
        for item in selection:
            command.extend(("--filter", item))
        completed = subprocess.run(command, capture_output=True, text=True, check=True, timeout=15)
        for identifier in completed.stdout.splitlines():
            if re.fullmatch(r"[0-9a-f]{64}", identifier) is None:
                raise CohortError("Backend service inventory contains an invalid identity")
            identity = docker_identity(identifier)
            if identity is None:
                raise CohortError("Backend service changed during inventory")
            if identity.get("name") != "/" + cohort.compose_project + "-backend":
                if len(selection) == 2:
                    raise CohortError("Backend service has an unexpected name")
                continue
            identifiers.add(identifier)
    if len(identifiers) > 1:
        raise CohortError("Backend service has multiple current identities")
    return identifiers


def collect_pending_backend(cohort: DevGpuCohort, session_id: str | None) -> None:
    path = cohort.path_for("backend-pending.json")
    if not path.exists() and not path.is_symlink():
        return
    pending = private_json(path)
    fixed = {"schema_version": 1, "cohort_sha256": cohort.sha256,
             "cohort_device": cohort.device, "cohort_inode": cohort.inode,
             "source_sha": cohort.source_sha, "source_tree": cohort.source_tree}
    keys = {*fixed, "kind", "session_id", "operation_id", "config_hash", "writer_pid", "writer_start_ticks"}
    if (set(pending) != keys or any(pending.get(k) != v for k, v in fixed.items())
            or pending["session_id"] != session_id
            or pending["kind"] not in {"cpu", "gpu"}
            or any(type(pending[k]) is not int or pending[k] <= 0
                   for k in ("writer_pid", "writer_start_ticks"))
            or (session_id is not None and re.fullmatch(r"[0-9a-f]{32}", str(session_id)) is None)
            or re.fullmatch(r"[0-9a-f]{32}", str(pending["operation_id"])) is None
            or re.fullmatch(r"[0-9a-f]{64}", str(pending["config_hash"])) is None):
        raise CohortError("pending Backend operation belongs to another authority")
    try:
        ticks, _ = process_identity(pending["writer_pid"])
    except (FileNotFoundError, CohortError):
        ticks = None
    if ticks == pending["writer_start_ticks"]:
        raise CohortError("pending Backend operation writer is still active")
    for identifier in backend_service_ids(cohort):
        identity = docker_identity(identifier)
        labels = identity.get("labels") or {}
        expected = {"com.nexpoly.dev.cohort-sha256": cohort.sha256,
                    "com.nexpoly.dev.cohort-operation-id": pending["operation_id"],
                    "com.nexpoly.dev.config-hash": pending["config_hash"]}
        if any(labels.get(k) != v for k, v in expected.items()):
            # An older recorded baseline or a replacement is never collected
            # merely because a failed operation is pending.
            continue
        cohort.verify()
        if docker_identity(identifier) != identity:
            raise CohortError("pending Backend identity changed before collection")
        subprocess.run(("docker", "rm", "--force", identifier), check=True,
                       capture_output=True, timeout=60)
    if private_json(path) != pending:
        raise CohortError("pending Backend operation changed before collection")
    path.unlink()


def compose_command(cohort: DevGpuCohort, kind: str) -> list[str]:
    command = ["docker", "compose", "-p", cohort.compose_project,
               "-f", str(cohort.repo_root / "docker-compose.yml"),
               "-f", str(cohort.repo_root / "docker-compose.dev.yml")]
    if cohort.auth_env_file is not None:
        command.extend(("-f", str(cohort.repo_root / "docker-compose.user-isolation-dev.yml")))
    command.extend(("-f", str(cohort.path_for("cohort-compose.yml"))))
    if kind == "gpu":
        command.extend(("-f", str(cohort.repo_root / "docker-compose.dev-gpu-session.yml")))
    command.extend(("--env-file", str(cohort.repo_root / ".env.dev")))
    if cohort.auth_env_file is not None:
        command.extend(("--env-file", str(cohort.auth_env_file)))
    return command


def cpu_snapshot(cohort: DevGpuCohort) -> dict[str, Any]:
    cohort.verify()
    record = private_json(cohort.path_for("backend-cpu-snapshot.json"))
    fixed = {"schema_version": 1, "cohort_sha256": cohort.sha256,
             "cohort_device": cohort.device, "cohort_inode": cohort.inode,
             "source_sha": cohort.source_sha, "source_tree": cohort.source_tree}
    keys = {*fixed, "image_id", "config_hash", "dependency_lock", "build_config", "md_lock", "dft_lock", "runtime_environment", "compose"}
    if (set(record) != keys or any(record.get(k) != v for k, v in fixed.items())
            or re.fullmatch(r"sha256:[0-9a-f]{64}", str(record["image_id"])) is None
            or re.fullmatch(r"[0-9a-f]{64}", str(record["config_hash"])) is None
            or any(re.fullmatch(r"sha256:[0-9a-f]{64}", str(record[k])) is None
                   for k in ("dependency_lock", "build_config", "md_lock", "dft_lock"))
            or not isinstance(record["compose"], dict)):
        raise CohortError("frozen CPU configuration belongs to another authority")
    runtime = record["runtime_environment"]
    if (not isinstance(runtime, dict) or set(runtime) - FROZEN_RUNTIME_KEYS
            or not {"NEXPOLY_ASSET_ROOT", "BYTEFF2_ROOT"} <= runtime.keys()
            or any(not isinstance(value, str) or len(value) > 4096
                   or any(ord(ch) < 32 or ord(ch) == 127 for ch in value) for value in runtime.values())):
        raise CohortError("frozen runtime paths contain unsupported fields")
    service = record["compose"].get("services", {}).get("backend", {})
    mounts = {mount.get("target"): mount for mount in service.get("volumes", [])}
    for name, target, read_only in cohort.runtime_mounts():
        mount = mounts.get(target)
        if (not mount or mount.get("type") != "bind" or mount.get("source") != str(cohort.path_for(name))
                or mount.get("read_only") is not read_only
                or mount.get("bind", {}).get("create_host_path") is not False):
            raise CohortError("frozen CPU mount differs from its owned native path")
    normalized = json.loads(json.dumps(service))
    labels = normalized.get("labels") or {}
    labels.pop("com.nexpoly.dev.config-hash", None)
    labels.pop("com.nexpoly.dev.cohort-operation-id", None)
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if (digest != record["config_hash"] or service.get("image") != cohort.backend_image
            or service.get("container_name") != cohort.compose_project + "-backend"
            or service.get("deploy", {}).get("resources", {}).get("reservations", {}).get("devices")
            or service.get("gpus") or service.get("devices")
            or any(mount.get("target") == "/app/backend" for mount in service.get("volumes", []))
            or "--reload" in str(service.get("command", ""))):
        raise CohortError("frozen CPU configuration drifted or declares GPU devices")
    return record


def freeze_cpu(cohort: DevGpuCohort) -> dict[str, Any]:
    """Seal the native CPU config before a GPU session; never print its Env."""
    path = cohort.path_for("backend-cpu-snapshot.json")
    if path.exists() or path.is_symlink():
        return cpu_snapshot(cohort)
    source = []
    for ref in ("HEAD", "HEAD^{tree}"):
        result = subprocess.run(("git", "-C", str(cohort.repo_root), "rev-parse", "--verify", ref),
                                check=True, capture_output=True, text=True, timeout=10)
        source.append(result.stdout.strip())
    cohort.require_source(*source)
    rendered = subprocess.run((*compose_command(cohort, "cpu"), "config", "--format", "json"),
                              cwd=cohort.repo_root, check=True, capture_output=True, text=True, timeout=30)
    config = json.loads(rendered.stdout)
    service = config["services"]["backend"]
    # Keep only the selected service and its native network/volume definitions.
    # Its effective environment stays opaque in an owner-private record.
    normalized = json.loads(json.dumps(service))
    labels = normalized.get("labels") or {}
    labels.pop("com.nexpoly.dev.config-hash", None)
    labels.pop("com.nexpoly.dev.cohort-operation-id", None)
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    image = subprocess.run(("docker", "image", "inspect", "--format",
                            '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}} '
                            '{{index .Config.Labels "com.nexpoly.source.tree"}} '
                            '{{index .Config.Labels "com.nexpoly.backend.dependency-lock"}} '
                            '{{index .Config.Labels "com.nexpoly.backend.build-config"}}', cohort.backend_image),
                           check=True, capture_output=True, text=True, timeout=15).stdout.split()
    if len(image) != 5 or image[1:3] != source:
        raise CohortError("CPU snapshot image source differs from its frozen cohort")
    sealed = {"services": {"backend": service}}
    for key in ("networks", "volumes"):
        if key in config:
            sealed[key] = config[key]
    record = {"schema_version": 1, "cohort_sha256": cohort.sha256,
              "cohort_device": cohort.device, "cohort_inode": cohort.inode,
              "source_sha": cohort.source_sha, "source_tree": cohort.source_tree,
              "image_id": image[0], "config_hash": digest, "dependency_lock": image[3],
              "build_config": image[4], "compose": sealed,
              "runtime_environment": {key: os.environ[key] for key in FROZEN_RUNTIME_KEYS if key in os.environ}}
    for name in ("md", "dft"):
        lock_ref = cohort.source_sha + ":workers/monomer_" + name + "_worker/requirements.lock"
        lock = subprocess.run(("git", "-C", str(cohort.repo_root), "show", lock_ref),
                              check=True, capture_output=True, timeout=10).stdout
        record[name + "_lock"] = "sha256:" + hashlib.sha256(lock).hexdigest()
    cohort.verify()
    write_once(path, record)
    return cpu_snapshot(cohort)


def cpu_environment(cohort: DevGpuCohort) -> dict[str, str]:
    record = cpu_snapshot(cohort)
    return {**record["runtime_environment"],
            "CURRENT_SOURCE_REVISION": cohort.source_sha, "CURRENT_SOURCE_TREE": cohort.source_tree,
            "NEXPOLY_BUILD_REVISION": cohort.source_sha, "NEXPOLY_BUILD_SOURCE_TREE": cohort.source_tree,
            "BACKEND_DEPENDENCY_LOCK_SHA256": record["dependency_lock"],
            "BACKEND_BUILD_CONFIG_SHA256": record["build_config"],
            "NEXPOLY_BACKEND_DEPENDENCY_LOCK_SHA256": record["dependency_lock"],
            "NEXPOLY_BACKEND_BUILD_CONFIG_SHA256": record["build_config"],
            "NEXPOLY_DEV_FROZEN_CPU_IMAGE_ID": record["image_id"],
            "NEXPOLY_DEV_FROZEN_CPU_CONFIG_HASH": record["config_hash"],
            "NEXPOLY_DEV_FROZEN_MD_LOCK": record["md_lock"],
            "NEXPOLY_DEV_FROZEN_DFT_LOCK": record["dft_lock"]}


def literal_compose_values(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("$", "$$")
    if isinstance(value, list):
        return [literal_compose_values(item) for item in value]
    if isinstance(value, dict):
        return {key: literal_compose_values(item) for key, item in value.items()}
    return value


def recreate_backend(cohort: DevGpuCohort, *, kind: str, config_hash: str,
                     session_id: str | None) -> str:
    """Replace only a recorded ID; Compose may create/reuse but never replace.

    One private ctl lock covers the entire native operation. A fresh operation
    label proves which container was created before its ID is bound and started.
    """
    if kind not in {"cpu", "gpu"} or re.fullmatch(r"[0-9a-f]{64}", config_hash) is None:
        raise CohortError("Backend recreation requires its fixed mode and config hash")
    if session_id is not None:
        cohort.binding(session_id)
    if kind == "gpu" and session_id is None:
        raise CohortError("GPU Backend creation requires a real controller session")
    lock_path = cohort.path_for("backend-lifecycle.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise CohortError("Backend lifecycle lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CohortError("another Backend lifecycle operation is in progress") from exc
        cohort.verify()
        cohort.require_prepared()
        collect_pending_backend(cohort, session_id)
        state_path = cohort.path_for("backend-service.json")
        previous = private_json(state_path) if state_path.exists() or state_path.is_symlink() else None
        baseline = {"schema_version": 1, "cohort_sha256": cohort.sha256,
                    "cohort_device": cohort.device, "cohort_inode": cohort.inode,
                    "source_sha": cohort.source_sha, "source_tree": cohort.source_tree}
        if previous is not None:
            keys = {*baseline, "container_id", "image_id", "config_hash", "kind", "session_id", "operation_id"}
            if (set(previous) != keys or any(previous.get(k) != v for k, v in baseline.items())
                    or previous["kind"] not in {"cpu", "gpu", "retired"}
                    or re.fullmatch(r"[0-9a-f]{64}", str(previous["container_id"])) is None
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", str(previous["image_id"])) is None
                    or re.fullmatch(r"[0-9a-f]{64}", str(previous["config_hash"])) is None
                    or re.fullmatch(r"[0-9a-f]{32}", str(previous["operation_id"])) is None
                    or (previous["session_id"] is not None
                        and re.fullmatch(r"[0-9a-f]{32}", str(previous["session_id"])) is None)):
                raise CohortError("Backend lifecycle record is foreign")
            if previous["kind"] == "gpu" and previous["session_id"] != session_id:
                raise CohortError("cannot replace another live GPU session Backend")
        current = backend_service_ids(cohort)
        operation_id = uuid4().hex
        writer_ticks, _ = process_identity(os.getpid())
        pending = {**baseline, "kind": kind, "session_id": session_id,
                   "operation_id": operation_id, "config_hash": config_hash,
                   "writer_pid": os.getpid(), "writer_start_ticks": writer_ticks}
        # Publish intent before any removal/create. Failed native operations
        # can later collect only their nonce-bound candidate, under this lock.
        # Validate all current ownership before publishing the intent.
        if current:
            identifier = next(iter(current))
            identity = docker_identity(identifier)
            labels = identity.get("labels") or {}
            if previous is not None:
                expected_labels = {"com.nexpoly.dev.cohort-sha256": cohort.sha256,
                                   "com.nexpoly.dev.cohort-operation-id": previous["operation_id"],
                                   "com.nexpoly.dev.config-hash": previous["config_hash"]}
                if (identifier != previous["container_id"] or identity["image"] != previous["image_id"]
                        or any(labels.get(k) != v for k, v in expected_labels.items())):
                    raise CohortError("Backend service was replaced; refusing recreation")
            elif (cohort.prior_backend is None or identifier != cohort.prior_backend["container_id"]
                  or identity["image"] != cohort.prior_backend["image_id"] or identity.get("devices")):
                raise CohortError("preexisting Backend is not the pinned private CPU baseline")
            cohort.verify()
            if docker_identity(identifier) != identity or backend_service_ids(cohort) != current:
                raise CohortError("Backend identity changed before exact removal")
            if previous is not None and previous["kind"] == "retired":
                require_teardown_complete(cohort)
                if identity.get("running") is not False:
                    raise CohortError("retired Backend resumed outside its controller")
        frozen_snapshot = freeze_cpu(cohort)
        frozen_cpu = frozen_snapshot
        if kind != "cpu":
            frozen_cpu = None
        if frozen_cpu is not None and config_hash != frozen_cpu["config_hash"]:
            raise CohortError("CPU restore must use its frozen native configuration hash")
        write_once(cohort.path_for("backend-pending.json"), pending)
        if current:
            subprocess.run(("docker", "rm", "--force", identifier), check=True,
                           capture_output=True, timeout=60)
        environment = dict(os.environ, NEXPOLY_DEV_GPU_COHORT_OPERATION_ID=operation_id)
        frozen_path = None
        if frozen_cpu is not None:
            effective = json.loads(json.dumps(frozen_cpu["compose"]))
            service = effective["services"]["backend"]
            service.pop("depends_on", None)
            service.pop("build", None)
            service["image"] = frozen_cpu["image_id"]
            service["labels"]["com.nexpoly.dev.cohort-operation-id"] = operation_id
            service["labels"]["com.nexpoly.dev.config-hash"] = config_hash
            frozen_path = cohort.path_for("backend-cpu-create-" + operation_id + ".json")
            write_once(frozen_path, literal_compose_values(effective))
            command = ["docker", "compose", "-p", cohort.compose_project, "-f", str(frozen_path)]
        else:
            command = compose_command(cohort, kind)
        command.extend(("create", "--no-deps", "--no-recreate", "--no-build", "--pull", "never", "backend"))
        subprocess.run(command, cwd=cohort.repo_root, env=environment, check=True,
                       capture_output=True, timeout=120)
        current = backend_service_ids(cohort)
        if len(current) != 1:
            raise CohortError("native Backend creation did not publish one exact candidate")
        identifier = next(iter(current))
        identity = docker_identity(identifier)
        labels = identity.get("labels") or {}
        expected = {"com.docker.compose.project": cohort.compose_project,
                    "com.docker.compose.service": "backend",
                    "com.nexpoly.dev.cohort-sha256": cohort.sha256,
                    "com.nexpoly.dev.cohort-operation-id": operation_id,
                    "com.nexpoly.dev.config-hash": config_hash}
        if any(labels.get(k) != v for k, v in expected.items()):
            raise CohortError("Backend creation selected an unrelated replacement")
        if kind == "cpu" and identity.get("devices"):
            raise CohortError("CPU restore cannot declare any GPU")
        if identity["image"] != frozen_snapshot["image_id"]:
            raise CohortError("Backend creation changed its frozen image identity")
        if kind == "gpu":
            record = backend_binding(cohort, session_id, identifier, config_hash, identity)
            write_once(cohort.backend_record_path(session_id), record)
        state = {**baseline, "container_id": identifier, "image_id": identity["image"],
                 "config_hash": config_hash, "kind": kind, "session_id": session_id,
                 "operation_id": operation_id}
        # This mutable pointer is changed under the private lock after both
        # immutable configuration and old container identity were verified.
        temporary_fd, temporary = tempfile.mkstemp(prefix=".backend-service-", dir=cohort.runtime_root)
        try:
            with os.fdopen(temporary_fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(canonical_json(state))
                stream.flush()
                os.fsync(stream.fileno())
            if (private_json(state_path) if state_path.exists() else None) != previous:
                raise CohortError("Backend lifecycle pointer changed before publication")
            cohort.verify()
            os.replace(temporary, state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        if docker_identity(identifier) != identity:
            raise CohortError("Backend candidate changed before exact start")
        cohort.require_prepared()
        subprocess.run(("docker", "start", identifier), check=True, capture_output=True, timeout=60)
        if private_json(cohort.path_for("backend-pending.json")) != pending:
            raise CohortError("Backend operation identity changed before completion")
        cohort.path_for("backend-pending.json").unlink()
        if frozen_path is not None:
            frozen_path.unlink()
        return identifier
    finally:
        os.close(descriptor)


def stop_backend(cohort: DevGpuCohort, session_id: str) -> None:
    descriptor = os.open(cohort.path_for("backend-lifecycle.lock"),
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise CohortError("Backend lifecycle lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CohortError("another Backend lifecycle operation is in progress") from exc
        cohort.verify()
        collect_pending_backend(cohort, session_id)
        identifier = cohort.backend_container_id(session_id)
        if identifier is None:
            return
        record = private_json(cohort.backend_record_path(session_id))
        identity = docker_identity(identifier)
        if identity is None:
            return
        if backend_binding(cohort, session_id, identifier, record["config_hash"], identity) != record:
            raise CohortError("Backend candidate identity changed before exact stop")
        cohort.verify()
        subprocess.run(("docker", "stop", identifier), check=True,
                       capture_output=True, timeout=60)
    finally:
        os.close(descriptor)


def verify_cpu_backend(cohort: DevGpuCohort) -> str:
    frozen = cpu_snapshot(cohort)
    state = private_json(cohort.path_for("backend-service.json"))
    fixed = {"schema_version": 1, "cohort_sha256": cohort.sha256,
             "cohort_device": cohort.device, "cohort_inode": cohort.inode,
             "source_sha": cohort.source_sha, "source_tree": cohort.source_tree,
             "kind": "cpu", "image_id": frozen["image_id"], "config_hash": frozen["config_hash"]}
    keys = {*fixed, "container_id", "session_id", "operation_id"}
    if (set(state) != keys or any(state.get(k) != v for k, v in fixed.items())
            or re.fullmatch(r"[0-9a-f]{64}", str(state["container_id"])) is None
            or re.fullmatch(r"[0-9a-f]{32}", str(state["operation_id"])) is None):
        raise CohortError("CPU Backend lifecycle identity differs")
    identity = docker_identity(state["container_id"])
    expected = {"com.docker.compose.project": cohort.compose_project,
                "com.docker.compose.service": "backend", "com.nexpoly.dev.cohort-sha256": cohort.sha256,
                "com.nexpoly.dev.config-hash": frozen["config_hash"],
                "com.nexpoly.dev.cohort-operation-id": state["operation_id"]}
    if (identity is None or identity.get("image") != frozen["image_id"] or identity.get("devices")
            or any((identity.get("labels") or {}).get(k) != v for k, v in expected.items())
            or backend_service_ids(cohort) != {state["container_id"]}):
        raise CohortError("CPU Backend was replaced or its frozen identity changed")
    return state["container_id"]


def require_teardown_complete(cohort: DevGpuCohort) -> None:
    for name in ("gpu-session/controller.json", "gpu-resource/broker.sock", "gpu-resource/mps-1",
                 "md-runs/worker.pid", "md-socket/worker.sock", "monomer-dft-worker.pid",
                 "monomer-dft-worker.session.json", "dft-socket/worker.sock", "dft-process.json"):
        path = cohort.path_for(name)
        if path.exists() or path.is_symlink():
            raise CohortError("cohort teardown still retains an owned runtime identity")


def retire_backend(cohort: DevGpuCohort, session_id: str | None) -> None:
    """Record maintenance completion only after the native plane fully exited."""
    descriptor = os.open(cohort.path_for("backend-lifecycle.lock"),
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise CohortError("Backend lifecycle lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CohortError("another Backend lifecycle operation is in progress") from exc
        cohort.verify()
        state_path = cohort.path_for("backend-service.json")
        state = private_json(state_path)
        if session_id is None:
            if state.get("kind") == "cpu":
                verify_cpu_backend(cohort)
                require_teardown_complete(cohort)
                return
            session_id = state.get("session_id")
            cohort.binding(session_id or "")
        collect_pending_backend(cohort, session_id)
        record = private_json(cohort.backend_record_path(session_id))
        identifier = cohort.backend_container_id(session_id)
        identity = docker_identity(identifier)
        if identity is None or identity.get("running") is not False:
            raise CohortError("maintenance Backend has not exited")
        if backend_binding(cohort, session_id, identifier, record["config_hash"], identity) != record:
            raise CohortError("maintenance Backend identity differs")
        path = state_path
        expected = {k: record[k] for k in ("schema_version", "cohort_sha256", "cohort_device", "cohort_inode",
                   "source_sha", "source_tree", "session_id", "container_id", "image_id", "config_hash", "operation_id")}
        expected["kind"] = "gpu"
        retired = dict(expected, kind="retired")
        if state not in (expected, retired):
            raise CohortError("maintenance lifecycle pointer changed")
        require_teardown_complete(cohort)
        if state == retired:
            return
        descriptor_tmp, temporary = tempfile.mkstemp(prefix=".backend-retired-", dir=cohort.runtime_root)
        try:
            with os.fdopen(descriptor_tmp, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(canonical_json(retired))
                stream.flush()
                os.fsync(stream.fileno())
            cohort.verify()
            require_teardown_complete(cohort)
            if private_json(path) != state or docker_identity(identifier) != identity:
                raise CohortError("maintenance identity changed before retirement")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    finally:
        os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "environment", "freeze-cpu", "cpu-environment", "verify-cpu", "retire-backend", "recreate-backend", "bind-dft", "verify-dft", "collect-dft", "bind-backend", "stop-backend", "verify-backend"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--device", type=int)
    parser.add_argument("--inode", type=int)
    parser.add_argument("--kind", choices=("cpu", "gpu"))
    parser.add_argument("--session-id")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--start-ticks", type=int)
    parser.add_argument("--container-id")
    parser.add_argument("--config-hash")
    parser.add_argument("--print-config-hash", action="store_true")
    args = parser.parse_args(argv)
    try:
        if (args.device is None) != (args.inode is None):
            raise CohortError("configuration identity requires both device and inode")
        identity = (args.device, args.inode) if args.device is not None else None
        cohort = DevGpuCohort.load(args.config, args.sha256, expected_identity=identity)
        if args.command == "prepare":
            cohort.prepare()
            return 0
        cohort.require_prepared()
        if args.command == "environment":
            for key, value in cohort.environment().items():
                print(f"{key}={value}")
            return 0
        if args.command == "freeze-cpu":
            freeze_cpu(cohort)
            return 0
        if args.command == "cpu-environment":
            for key, value in cpu_environment(cohort).items():
                print(f"{key}={value}")
            return 0
        if args.command == "verify-cpu":
            print(verify_cpu_backend(cohort))
            return 0
        if args.command == "recreate-backend":
            print(recreate_backend(cohort, kind=args.kind, config_hash=args.config_hash or "", session_id=args.session_id))
            return 0
        if args.command == "stop-backend":
            stop_backend(cohort, args.session_id or "")
            return 0
        if args.command == "retire-backend":
            retire_backend(cohort, args.session_id)
            return 0
        baseline = cohort.binding(args.session_id or "")
        if args.command in {"bind-dft", "verify-dft", "collect-dft"}:
            path = cohort.path_for("dft-process.json")
            if args.command == "collect-dft":
                if not path.exists() and not path.is_symlink():
                    return 0
                record = private_json(path)
                expected_keys = {*baseline, "pid", "start_ticks", "argv", "python", "socket"}
                if set(record) != expected_keys or any(record.get(k) != v for k, v in baseline.items()):
                    raise CohortError("DFT binding is foreign")
                try:
                    ticks, _ = process_identity(record["pid"])
                except FileNotFoundError:
                    ticks = None
                except CohortError:
                    ticks = None
                if ticks == record["start_ticks"]:
                    raise CohortError("DFT binding is still active")
                if private_json(path) != record:
                    raise CohortError("DFT binding changed before collection")
                path.unlink()
                return 0
            if not args.pid or not args.start_ticks:
                raise CohortError("DFT binding requires exact PID/start")
            value = dft_binding(cohort, args.session_id, args.pid, args.start_ticks)
            if args.command == "bind-dft":
                write_once(path, value)
            elif private_json(path) != value:
                raise CohortError("DFT recorded argv/path/config identity differs")
        elif args.command == "bind-backend":
            identity = docker_identity(args.container_id or "")
            if identity is None:
                raise CohortError("Backend candidate disappeared before binding")
            value = backend_binding(cohort, args.session_id, args.container_id, args.config_hash or "", identity)
            write_once(cohort.backend_record_path(args.session_id), value)
        else:
            path = cohort.backend_record_path(args.session_id)
            if not path.exists() and not path.is_symlink():
                return 0
            record = private_json(path)
            expected_keys = {*baseline, "container_id", "image_id", "config_hash", "compose_project", "compose_service", "operation_id"}
            if set(record) != expected_keys or any(record.get(k) != v for k, v in baseline.items()):
                raise CohortError("Backend candidate binding is foreign")
            identity = docker_identity(record["container_id"])
            if identity is None:
                return 0
            if backend_binding(cohort, args.session_id, record["container_id"], record["config_hash"], identity) != record:
                raise CohortError("Backend candidate identity changed")
            cohort.verify()
            print(record["config_hash"] if args.print_config_hash else record["container_id"])
    except (CohortError, OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        print(f"development GPU cohort error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

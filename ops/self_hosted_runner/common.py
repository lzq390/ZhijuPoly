"""Shared, dependency-free configuration and durable state primitives."""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import tomllib

GIB = 1024 ** 3
REPOSITORY = "lzq390/ZhijuPoly"
LABEL = "nexpoly-ci-ephemeral"
PROFILE = "nexpoly-ci-ephemeral-v1"
DEFAULT_ROOT = Path("/data/lzq/github-runner")
DEFAULT_CONFIG = Path("/etc/nexpoly-runner/config.toml")
UUID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class RunnerError(RuntimeError):
    """An operational failure whose message is safe to display."""


def validate_uuid(value: str) -> str:
    if not isinstance(value, str) or not UUID_PATTERN.fullmatch(value):
        raise RunnerError("Invalid instance identifier")
    return value


def regular_file(path: Path, *, root_owned: bool = False) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise RunnerError(f"Required file unavailable: {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise RunnerError(f"Expected a regular file: {path}")
    if root_owned and (info.st_uid != 0 or info.st_mode & 0o022):
        raise RunnerError(f"File must be root-owned and not group/world writable: {path}")
    return info


def atomic_json(path: Path, value: object) -> None:
    """Commit state with a same-directory rename and directory fsync."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path, default: object = None) -> object:
    if not path.exists():
        return default
    regular_file(path)
    if path.stat().st_size > 1024 * 1024:
        raise RunnerError(f"State file exceeds size limit: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunnerError(f"Cannot read state: {path}") from exc


@dataclasses.dataclass(frozen=True)
class Config:
    root: Path = DEFAULT_ROOT
    state: Path = DEFAULT_ROOT / "state"
    repository: str = REPOSITORY
    label: str = LABEL
    name_prefix: str = "nexpoly-ci-"
    app_id: int = 0
    installation_id: int = 0
    private_key_file: Path = Path("/etc/nexpoly-runner/app.pem")
    vcpus: int = 8
    memory_mib: int = 32768
    disk_gib: int = 160
    proxy_port: int = 27892
    upstream_host: str = "127.0.0.1"
    upstream_port: int = 27891
    dns: str = "202.38.64.7"
    no_proxy: str = "localhost,127.0.0.1,::1,postgres,backend,lab-postgres"
    state_min_free_gib: int = 40
    host_min_free_gib: int = 100
    poll_seconds: int = 30
    boot_timeout_seconds: int = 600
    job_timeout_seconds: int = 6300
    shutdown_timeout_seconds: int = 120
    failure_limit: int = 3
    config_file: Path = DEFAULT_CONFIG

    @classmethod
    def load(cls, path: Path, *, privileged: bool = False) -> "Config":
        regular_file(path, root_owned=privileged)
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            values: dict[str, object] = {"config_file": path.absolute()}
            groups = {
                "paths": ("root", "state"),
                "github": ("repository", "app_id", "installation_id", "private_key_file"),
                "runner": ("label", "name_prefix", "vcpus", "memory_mib", "disk_gib"),
                "network": ("proxy_port", "upstream_host", "upstream_port", "dns", "no_proxy"),
                "policy": ("state_min_free_gib", "host_min_free_gib", "poll_seconds", "boot_timeout_seconds", "job_timeout_seconds", "shutdown_timeout_seconds", "failure_limit"),
            }
            for group, names in groups.items():
                section = data.get(group, {})
                if not isinstance(section, dict):
                    raise ValueError(group)
                for name in names:
                    if name in section:
                        values[name] = section[name]
            for name in ("root", "state", "private_key_file"):
                if name in values:
                    values[name] = Path(values[name])
            result = cls(**values)
            result.validate()
            return result
        except (TypeError, ValueError, KeyError) as exc:
            raise RunnerError("Invalid runner configuration") from exc

    def validate(self) -> None:
        fixed = {
            "root": DEFAULT_ROOT, "state": DEFAULT_ROOT / "state",
            "repository": REPOSITORY, "label": LABEL, "name_prefix": "nexpoly-ci-",
            "vcpus": 8, "memory_mib": 32768, "disk_gib": 160,
            "proxy_port": 27892, "upstream_host": "127.0.0.1", "upstream_port": 27891,
            "dns": "202.38.64.7",
            "no_proxy": "localhost,127.0.0.1,::1,postgres,backend,lab-postgres",
            "poll_seconds": 30, "failure_limit": 3,
        }
        for name, expected in fixed.items():
            if getattr(self, name) != expected:
                raise RunnerError(f"Configuration differs from reviewed deployment contract: {name}")
        for name in ("app_id", "installation_id"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise RunnerError(f"Invalid GitHub identifier: {name}")
        for name in ("state_min_free_gib", "host_min_free_gib", "poll_seconds", "boot_timeout_seconds", "job_timeout_seconds", "shutdown_timeout_seconds", "failure_limit"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise RunnerError(f"Invalid positive policy limit: {name}")
        if self.state_min_free_gib < 40 or self.host_min_free_gib < 100:
            raise RunnerError("Disk safety margins cannot be reduced")
        if self.job_timeout_seconds > 6300 or self.boot_timeout_seconds > 600 or self.shutdown_timeout_seconds > 120:
            raise RunnerError("VM timeout exceeds the reviewed maximum")
        if self.private_key_file != Path("/etc/nexpoly-runner/app.pem"):
            raise RunnerError("App private key must use the protected configuration directory")

    @property
    def host_proxy(self) -> str:
        return f"http://127.0.0.1:{self.proxy_port}"

    @property
    def controller_dir(self) -> Path:
        return self.state / "controller"

    @property
    def journal(self) -> Path:
        return self.controller_dir / "state.json"

    @property
    def drain_file(self) -> Path:
        return self.controller_dir / "drain.json"

    @property
    def manifest(self) -> Path:
        return self.state / "templates" / "current.json"

    def instance(self, instance_id: str) -> Path:
        return self.state / "instances" / validate_uuid(instance_id)

    def runtime(self, instance_id: str) -> Path:
        return Path("/run") / f"nexpoly-runner-vm-{validate_uuid(instance_id)}"

    def unit(self, instance_id: str) -> str:
        return f"nexpoly-runner-vm@{validate_uuid(instance_id)}.service"

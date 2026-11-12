#!/usr/bin/env python3
"""Read-only production checks, independent of release-time protection baselines.

The private v1 configuration pins production contracts, never process lifetimes.
Guard mode observes GPU ownership only; it cannot drain or signal a workload.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
import tempfile
import types
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request


TIMEOUT = 15
MAX_JSON_BYTES = 4 * 1024 * 1024
REQUIRED_MODELS = {"ocsr", "conditional_generation", "retrosynthesis", "polytao"}


class ObservationError(Exception):
    """A safe diagnostic code, without subprocess output or configuration data."""


def fail(code: str) -> None:
    raise ObservationError(code)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def timestamp() -> str:
    return now().strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_hash(path: str) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        fail("file_unreadable")


def run(*command: str) -> str:
    try:
        return subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=TIMEOUT).stdout
    except subprocess.TimeoutExpired:
        fail("command_timeout:" + command[0])
    except subprocess.CalledProcessError as exc:
        fail("command_failed:" + command[0] + ":" + str(exc.returncode))
    except OSError:
        fail("command_unavailable:" + command[0])


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail("duplicate_json_key")
        result[key] = value
    return result


def parse_json(raw: str | bytes) -> Any:
    try:
        return json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, TypeError):
        fail("invalid_json")


def private_json(path: Path) -> tuple[dict[str, Any], str]:
    """Open without following a symlink, validate the same fd that is read."""
    if not path.is_absolute():
        fail("private_path_not_absolute")
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or before.st_size > MAX_JSON_BYTES):
            fail("private_file_identity_invalid")
        raw = os.read(fd, MAX_JSON_BYTES + 1)
        after = os.fstat(fd)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                  value.st_mtime_ns, value.st_ctime_ns)
        if identity(before) != identity(after) or len(raw) != before.st_size:
            fail("private_file_changed")
    except OSError:
        fail("private_file_unreadable")
    finally:
        if fd is not None:
            os.close(fd)
    payload = parse_json(raw)
    if not isinstance(payload, dict):
        fail("private_json_not_object")
    return payload, hashlib.sha256(raw).hexdigest()


def string(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and "\x00" not in value


def absolute(value: Any) -> bool:
    return string(value) and Path(value).is_absolute()


def hex_digest(value: Any, length: int = 64) -> bool:
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{" + str(length) + "}", value) is not None


def local_url(url: Any) -> bool:
    if not string(url):
        return False
    value = urllib.parse.urlsplit(url)
    return (value.scheme == "http" and value.hostname == "127.0.0.1"
            and value.username is None and value.password is None and not value.fragment)


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    try:
        if type(config["version"]) is not int or config["version"] != 1:
            fail("config_version_invalid")
        if any(not hex_digest(config[key], 40) for key in ("source_sha", "source_tree", "backend_source_sha")):
            fail("config_source_identity_invalid")
        if config["gpu_index"] != "2" or not string(config["gpu_uuid"]) or not config["gpu_uuid"].startswith("GPU-"):
            fail("config_gpu_identity_invalid")
        if not all(absolute(config[key]) for key in ("state_path", "report_path")):
            fail("config_output_path_invalid")
        if config["state_path"] == config["report_path"]:
            fail("config_output_paths_collide")
        containers = config["containers"]
        if not isinstance(containers, list) or not containers:
            fail("config_containers_invalid")
        names: set[str] = set()
        for item in containers:
            name = item["name"]
            if not string(name) or name.startswith("-") or name in names or not string(item["image_id"]):
                fail("config_container_identity_invalid")
            names.add(name)
            for key in ("config_sha256", "host_config_sha256", "mounts_sha256"):
                if not hex_digest(item[key]):
                    fail("config_container_hash_invalid")
            for key in ("revision", "compose_project", "compose_service"):
                if item.get(key) is not None and not string(item[key]):
                    fail("config_container_label_invalid")
        if config["backend_container"] not in names:
            fail("config_backend_missing")
        unit_names: set[str] = set()
        if not isinstance(config["units"], list) or not config["units"]:
            fail("config_units_invalid")
        for item in config["units"]:
            if not string(item["name"]) or not item["name"].endswith(".service") or item["name"].startswith("-") or item["name"] in unit_names:
                fail("config_unit_identity_invalid")
            unit_names.add(item["name"])
            if not absolute(item["working_directory"]) or not hex_digest(item["fragment_sha256"]):
                fail("config_unit_contract_invalid")
            if not isinstance(item.get("dropins", {}), dict) or any(
                    not absolute(path) or not hex_digest(digest) for path, digest in item.get("dropins", {}).items()):
                fail("config_unit_dropins_invalid")
            if not isinstance(item.get("expected_properties", {}), dict) or any(
                    re.fullmatch("[A-Za-z][A-Za-z0-9]*", key) is None or not isinstance(value, str)
                    for key, value in item.get("expected_properties", {}).items()):
                fail("config_unit_properties_invalid")
        if not isinstance(config["allowed_gpu_units"], list) or not config["allowed_gpu_units"] or not set(config["allowed_gpu_units"]).issubset(unit_names):
            fail("config_gpu_units_invalid")
        if not isinstance(config["workers"], list) or not config["workers"]:
            fail("config_workers_invalid")
        for item in config["workers"]:
            if not string(item["name"]) or not absolute(item["socket"]) or not isinstance(item["expected"], dict) or not item["expected"]:
                fail("config_worker_invalid")
            if any(not string(key) for key in item["expected"]):
                fail("config_worker_expected_invalid")
            if "unit" in item and item["unit"] not in unit_names:
                fail("config_worker_unit_invalid")
            if "container" in item and item["container"] not in names:
                fail("config_worker_container_invalid")
            if "unit" in item and "container" in item:
                fail("config_worker_peer_ambiguous")
            if "private_socket_root" in item and not absolute(item["private_socket_root"]):
                fail("config_worker_socket_root_invalid")
        if not isinstance(config["required_models"], list) or set(config["required_models"]) != REQUIRED_MODELS:
            fail("config_required_models_invalid")
        for item in config.get("public_probes", []):
            if not string(item["name"]) or not local_url(item["url"]) or type(item.get("status", 200)) is not int:
                fail("config_public_probe_invalid")
        for item in config.get("isolation_probes", []):
            if not string(item["name"]) or item["unit"] not in unit_names:
                fail("config_isolation_probe_invalid")
            for key in ("helper", "worker_config"):
                if not absolute(item[key]) or not hex_digest(item[key + "_sha256"]):
                    fail("config_isolation_probe_file_invalid")
    except (KeyError, TypeError, ValueError):
        fail("config_schema_invalid")
    return config


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", delete=False) as stream:
            temporary = stream.name
            os.chmod(temporary, 0o600)
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        fail("atomic_write_failed")
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def container_identity(contract: dict[str, Any]) -> dict[str, Any]:
    payload = parse_json(run("docker", "inspect", contract["name"]))
    try:
        if not isinstance(payload, list) or len(payload) != 1:
            fail("container_inspect_ambiguous")
        item = payload[0]
        if item["Name"].lstrip("/") != contract["name"] or item["Image"] != contract["image_id"]:
            fail("container_identity_drift")
        state = item["State"]
        if state["Status"] != "running" or state.get("Running") is not True or int(state["Pid"]) <= 0:
            fail("container_not_running")
        if state.get("Health", {}).get("Status", "healthy") != "healthy":
            fail("container_unhealthy")
        labels = item["Config"].get("Labels") or {}
        for key, label in (("revision", "org.opencontainers.image.revision"),
                           ("compose_project", "com.docker.compose.project"),
                           ("compose_service", "com.docker.compose.service")):
            if contract.get(key) is not None and labels.get(label) != contract[key]:
                fail("container_label_drift:" + key)
        for key, section in (("config_sha256", "Config"), ("host_config_sha256", "HostConfig"),
                             ("mounts_sha256", "Mounts")):
            value = item[section]
            if section == "Mounts":
                value = sorted(value, key=lambda mount: json.dumps(mount, sort_keys=True, separators=(",", ":")))
            if canonical_hash(value) != contract[key]:
                fail("container_contract_drift:" + section)
        return {"id": item["Id"], "pid": int(state["Pid"])}
    except (KeyError, TypeError, ValueError):
        fail("container_inspect_invalid")


def unit_identity(contract: dict[str, Any]) -> dict[str, Any]:
    properties = "ActiveState,SubState,MainPID,ControlGroup,WorkingDirectory,FragmentPath,DropInPaths"
    if contract.get("expected_properties"):
        properties += "," + ",".join(sorted(contract["expected_properties"]))
    raw = run("systemctl", "--user", "show", contract["name"],
              "--property=" + properties)
    values = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    try:
        pid = int(values["MainPID"])
        group = values["ControlGroup"]
        if values["ActiveState"] != "active" or values["SubState"] != "running" or pid <= 0:
            fail("unit_not_running")
        if not group.startswith("/") or group == "/" or ".." in group.split("/"):
            fail("unit_cgroup_invalid")
        if values["WorkingDirectory"] != contract["working_directory"]:
            fail("unit_working_directory_drift")
        if file_hash(values["FragmentPath"]) != contract["fragment_sha256"]:
            fail("unit_fragment_drift")
        # All configured drop-ins must still be loaded. New drop-ins are drift too.
        dropins = contract.get("dropins", {})
        if set(values.get("DropInPaths", "").split()) != set(dropins):
            fail("unit_dropins_drift")
        for path, digest in dropins.items():
            if file_hash(path) != digest:
                fail("unit_dropin_drift")
        for key, expected in contract.get("expected_properties", {}).items():
            if values.get(key) != expected:
                fail("unit_property_drift:" + key)
        if not cgroup_member(pid, group):
            fail("unit_main_pid_cgroup_mismatch")
        return {"pid": pid, "cgroup": group}
    except (KeyError, TypeError, ValueError):
        fail("unit_show_invalid")


def process_cgroups(pid: int) -> list[str]:
    try:
        return [line.split(":", 2)[2] for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
                if line.count(":") >= 2]
    except FileNotFoundError:
        return []
    except OSError:
        fail("process_cgroup_unreadable")


def cgroup_member(pid: int, expected: str) -> bool:
    return any(value == expected or value.startswith(expected.rstrip("/") + "/")
               for value in process_cgroups(pid))


def socket_identity(path: str, *, private_socket_root: str | None = None) -> tuple[int, int]:
    value = Path(path)
    try:
        entry = value.lstat()
        if value.resolve() != value:
            fail("worker_socket_identity_invalid")
        allowed_modes = {0o600}
        if private_socket_root is not None:
            root = Path(private_socket_root)
            if not root.is_absolute() or value == root or not value.is_relative_to(root):
                fail("worker_socket_outside_private_root")
            # Historical GPU workers create 0666 sockets inside an inaccessible
            # 0700 cohort. Preserve that contract only when every containing
            # directory, including the cohort root, remains private and owned.
            current = value.parent
            while True:
                directory = current.lstat()
                if (current.resolve() != current or not stat.S_ISDIR(directory.st_mode)
                        or directory.st_uid != os.geteuid() or directory.st_gid != os.getegid()
                        or stat.S_IMODE(directory.st_mode) != 0o700):
                    fail("worker_socket_private_parent_invalid")
                if current == root:
                    break
                current = current.parent
            if entry.st_gid != os.getegid():
                fail("worker_socket_group_invalid")
            allowed_modes.add(0o666)
        if (value.resolve() != value or not stat.S_ISSOCK(entry.st_mode)
                or entry.st_uid != os.geteuid() or stat.S_IMODE(entry.st_mode) not in allowed_modes
                or entry.st_nlink != 1):
            fail("worker_socket_identity_invalid")
        return entry.st_dev, entry.st_ino
    except OSError:
        fail("worker_socket_unavailable")


def uds_health(path: str, *, expected_cgroup: str | None = None,
               expected_pids: set[int] | None = None,
               private_socket_root: str | None = None) -> dict[str, Any]:
    class UnixHTTPConnection(http.client.HTTPConnection):
        def connect(self) -> None:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(TIMEOUT)
            self.sock.connect(path)
    connection = UnixHTTPConnection("localhost", timeout=TIMEOUT)
    try:
        before = socket_identity(path, private_socket_root=private_socket_root)
        connection.connect()
        assert connection.sock is not None
        pid, uid, _ = struct.unpack("3i", connection.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        if pid <= 0 or uid != os.geteuid():
            fail("worker_peer_identity_invalid")
        if expected_cgroup is not None and not cgroup_member(pid, expected_cgroup):
            fail("worker_peer_cgroup_mismatch")
        if expected_pids is not None and pid not in expected_pids:
            fail("worker_peer_container_mismatch")
        connection.request("GET", "/health")
        response = connection.getresponse()
        if response.status != 200:
            fail("worker_health_http:" + str(response.status))
        raw = response.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            fail("worker_health_too_large")
        payload = parse_json(raw)
        if not isinstance(payload, dict):
            fail("worker_health_invalid")
        if socket_identity(path, private_socket_root=private_socket_root) != before:
            fail("worker_socket_changed")
        return payload
    except (OSError, http.client.HTTPException):
        fail("worker_health_unavailable")
    finally:
        connection.close()


def dotted(payload: dict[str, Any], path: str) -> Any:
    value: Any = payload
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            fail("health_field_missing:" + path)
        value = value[key]
    return value


def capacity(payload: dict[str, Any], active: str, maximum: str, waiting: str | None = None,
             max_waiting: str | None = None) -> None:
    for count_key, limit_key in ((active, maximum), (waiting, max_waiting)):
        if count_key is None or limit_key is None or count_key not in payload or limit_key not in payload:
            continue
        count = payload[count_key]
        limit = payload.get(limit_key)
        if type(count) is not int or type(limit) is not int or not 0 <= count <= limit:
            fail("capacity_invalid:" + count_key)


def worker_health(contract: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    before_unit = before_container = None
    peer_arguments: dict[str, Any] = {}
    if "private_socket_root" in contract:
        peer_arguments["private_socket_root"] = contract["private_socket_root"]
    if "unit" in contract:
        if config is None:
            fail("worker_peer_config_missing")
        unit = next(item for item in config["units"] if item["name"] == contract["unit"])
        before_unit = unit_identity(unit)
        peer_arguments["expected_cgroup"] = before_unit["cgroup"]
    if "container" in contract:
        if config is None:
            fail("worker_peer_config_missing")
        container = next(item for item in config["containers"] if item["name"] == contract["container"])
        before_container = container_identity(container)
        peer_arguments["expected_pids"] = backend_pids(contract["container"])
    payload = uds_health(contract["socket"], **peer_arguments)
    if before_unit is not None and unit_identity(unit) != before_unit:
        fail("worker_unit_changed")
    if before_container is not None and container_identity(container) != before_container:
        fail("worker_container_changed")
    for key, expected in contract["expected"].items():
        value = dotted(payload, key)
        if type(value) is not type(expected) or value != expected:
            fail("worker_expected_mismatch:" + key)
    if payload.get("runtime_ready", payload.get("ready")) is not True:
        fail("worker_not_ready")
    for key in ("draining", "recovering", "fatal"):
        if payload.get(key, False) is not False:
            fail("worker_unavailable:" + key)
    runtime = payload.get("runtime", {})
    if isinstance(runtime, dict) and runtime.get("fatal", False) is not False:
        fail("worker_runtime_fatal")
    capacity(payload, "active_jobs", "max_active_jobs")
    capacity(payload, "active_jobs", "max_concurrent_jobs", "queued_jobs", "max_queued_jobs")
    capacity(payload, "running_jobs", "max_running_jobs", "queued_jobs", "max_queued_jobs")
    capacity(payload, "active", "max_running_analyses", "queued", "max_pending_analyses")
    return {key: payload[key] for key in ("runtime_ready", "active_jobs", "queued_jobs", "accepting_jobs") if key in payload}


def backend_status(container: str, endpoint: str) -> dict[str, Any]:
    # No shell, import of application modules, or mutation endpoint is involved.
    if endpoint not in ("/internal/gpu/status", "/internal/deployment/status"):
        fail("backend_endpoint_forbidden")
    program = ("import urllib.request; "
               "r=urllib.request.urlopen('http://127.0.0.1:8000" + endpoint + "',timeout=10); "
               "print(r.read(4194305).decode())")
    raw = run("docker", "exec", container, "python", "-c", program)
    if len(raw) > MAX_JSON_BYTES:
        fail("backend_status_too_large")
    payload = parse_json(raw)
    if not isinstance(payload, dict):
        fail("backend_status_invalid")
    return payload


def gpu_health(config: dict[str, Any]) -> dict[str, Any]:
    payload = backend_status(config["backend_container"], "/internal/gpu/status")
    if payload.get("build_revision") != config["backend_source_sha"]:
        fail("backend_source_mismatch")
    if payload.get("status") != "ready" or payload.get("accepting_inferences") is not True:
        fail("gpu_runtime_not_ready")
    if type(payload.get("max_concurrent_inferences")) is not int or payload["max_concurrent_inferences"] != 1:
        fail("gpu_concurrency_contract_invalid")
    models = payload.get("models")
    if not isinstance(models, dict):
        fail("gpu_models_missing")
    for name in config["required_models"]:
        model = models.get(name)
        if not isinstance(model, dict) or any(model.get(key) is not True for key in ("enabled", "loaded", "ready")):
            fail("gpu_model_not_ready:" + name)
        if any(model.get(key) is not False for key in ("loading", "fatal")) or model.get("error") is not None:
            fail("gpu_model_error:" + name)
        for key in ("active_tasks", "waiting_tasks"):
            if type(model.get(key)) is not int or model[key] < 0:
                fail("gpu_model_capacity_invalid:" + name)
    capacity(payload, "active_inferences", "max_concurrent_inferences", "waiting_inferences", "max_waiting_inferences")
    return {"status": payload["status"], "active_inferences": payload.get("active_inferences"),
            "waiting_inferences": payload.get("waiting_inferences"),
            "historical_inference_errors": sorted(name for name in models
                                                  if models[name].get("last_inference_error"))}


def deployment_health(config: dict[str, Any]) -> dict[str, Any]:
    payload = backend_status(config["backend_container"], "/internal/deployment/status")
    if dotted(payload, "drain.enabled") is not False:
        fail("deployment_draining")
    if type(payload.get("active_total")) is not int or payload["active_total"] < 0:
        fail("deployment_active_count_invalid")
    return {"draining": False, "active_total": payload["active_total"]}


def gpu_processes(config: dict[str, Any]) -> list[dict[str, Any]]:
    identifier = run("nvidia-smi", "-i", config["gpu_index"], "--query-gpu=uuid", "--format=csv,noheader").strip()
    if identifier != config["gpu_uuid"]:
        fail("gpu_uuid_mismatch")
    raw = run("nvidia-smi", "-i", config["gpu_index"], "--query-compute-apps=pid,gpu_uuid,process_name",
              "--format=csv,noheader,nounits")
    result = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = [value.strip() for value in line.split(",", 2)]
        if len(parts) != 3 or not parts[0].isdigit() or int(parts[0]) <= 0 or parts[1] != identifier:
            fail("gpu_process_sample_invalid")
        # Names can contain command-line data; report only PID and GPU identity.
        result.append({"pid": int(parts[0]), "gpu_uuid": identifier})
    return result


def backend_pids(container: str) -> set[int]:
    raw = run("docker", "top", container, "-eo", "pid")
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "PID" or any(not line.strip().isdigit() for line in lines[1:]):
        fail("backend_process_list_invalid")
    return {int(line.strip()) for line in lines[1:]}


def guard(config: dict[str, Any], config_sha256: str) -> tuple[dict[str, Any], int]:
    payload: dict[str, Any] = {"schema_version": 1, "observed_at": timestamp(),
        "gpu_index": config["gpu_index"], "gpu_uuid": config["gpu_uuid"], "guard_mode": "observe",
        "source_sha": config["source_sha"], "source_tree": config["source_tree"],
        "backend_source_sha": config["backend_source_sha"], "observer_config_sha256": config_sha256,
        "status": "quarantined", "allowed_processes": [], "unknown_processes": [], "errors": []}
    try:
        backend = next(item for item in config["containers"] if item["name"] == config["backend_container"])
        before_backend = container_identity(backend)
        units = {item["name"]: item for item in config["units"] if item["name"] in config["allowed_gpu_units"]}
        before_units = {name: unit_identity(item) for name, item in units.items()}
        processes = gpu_processes(config)
        production_pids = backend_pids(config["backend_container"])
        allowed, unknown = [], []
        for process in processes:
            pid = process["pid"]
            groups = process_cgroups(pid)
            if not groups:
                # A process that ended after nvidia-smi is not an unknown owner.
                if not Path(f"/proc/{pid}").exists():
                    continue
                fail("gpu_process_cgroup_empty")
            authority = None
            if pid in production_pids:
                authority = "production-backend"
            else:
                for name, identity in before_units.items():
                    if any(group == identity["cgroup"] or group.startswith(identity["cgroup"] + "/") for group in groups):
                        authority = name
                        break
            item = dict(process)
            if authority:
                item["authority"] = authority
                allowed.append(item)
            else:
                unknown.append(item)
        if container_identity(backend) != before_backend:
            fail("backend_changed_during_sample")
        if {name: unit_identity(item) for name, item in units.items()} != before_units:
            fail("gpu_units_changed_during_sample")
        payload.update(allowed_processes=allowed, unknown_processes=unknown,
                       status="quarantined" if unknown else "ready",
                       authorities={"backend_container": before_backend["id"], "systemd_user_units": before_units})
    except Exception as exc:
        error = str(exc) if isinstance(exc, ObservationError) else "unexpected_observation_error:" + type(exc).__name__
        payload.update(status="quarantined", errors=[error], unknown_processes=[{"kind": "unresolved_observation", "error": error}])
    payload["observed_at"] = timestamp()
    atomic_write(Path(config["state_path"]), payload)
    return payload, 0 if payload["status"] == "ready" else 2


def guard_health(config: dict[str, Any], config_sha256: str | None = None) -> dict[str, Any]:
    payload, _ = private_json(Path(config["state_path"]))
    try:
        if (payload.get("schema_version") != 1 or payload.get("gpu_index") != config["gpu_index"]
                or payload.get("gpu_uuid") != config["gpu_uuid"] or payload.get("guard_mode") != "observe"):
            fail("guard_identity_invalid")
        if any(payload.get(key) != config[key] for key in ("source_sha", "source_tree", "backend_source_sha")):
            fail("guard_source_identity_invalid")
        if config_sha256 is not None and payload.get("observer_config_sha256") != config_sha256:
            fail("guard_config_identity_invalid")
        if not isinstance(payload.get("observed_at"), str) or re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", payload["observed_at"]) is None:
            fail("guard_timestamp_invalid")
        observed = dt.datetime.strptime(payload["observed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        if not 0 <= (now() - observed).total_seconds() <= 120:
            fail("guard_snapshot_stale")
        if payload.get("status") != "ready" or payload.get("unknown_processes") != [] or payload.get("errors") != []:
            fail("guard_snapshot_not_ready")
    except (KeyError, TypeError, ValueError):
        fail("guard_snapshot_invalid")
    return {"status": "ready", "observed_at": payload["observed_at"]}


def public_probe(contract: dict[str, Any]) -> dict[str, Any]:
    # Disable proxies and redirects so a configured local GET stays local.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
            return None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(contract["url"], timeout=TIMEOUT) as response:
            code = response.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except (OSError, urllib.error.URLError):
        fail("public_probe_unavailable")
    if code != contract.get("status", 200):
        fail("public_probe_http:" + str(code))
    return {"http_status": code}


def pinned_helper(path: str, digest: str) -> types.ModuleType:
    """Load precisely the reviewed helper bytes, without a hash/import TOCTOU."""
    fd = None
    try:
        value = Path(path)
        if value.resolve() != value:
            fail("isolation_helper_symlink")
        fd = os.open(value, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        entry = os.fstat(fd)
        if (not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.geteuid()
                or entry.st_mode & 0o022 or entry.st_nlink != 1 or entry.st_size > MAX_JSON_BYTES):
            fail("isolation_helper_identity_invalid")
        code = os.read(fd, MAX_JSON_BYTES + 1)
        if len(code) != entry.st_size or hashlib.sha256(code).hexdigest() != digest:
            fail("isolation_helper_hash_mismatch")
    except OSError:
        fail("isolation_helper_unreadable")
    finally:
        if fd is not None:
            os.close(fd)
    module = types.ModuleType("production_isolation_helper")
    module.__file__ = path
    exec(compile(code, path, "exec"), module.__dict__)
    return module


def isolation_probe(contract: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    worker_config, digest = private_json(Path(contract["worker_config"]))
    if digest != contract["worker_config_sha256"]:
        fail("isolation_worker_config_hash_mismatch")
    helper = pinned_helper(contract["helper"], contract["helper_sha256"])
    unit = next(item for item in config["units"] if item["name"] == contract["unit"])
    before = unit_identity(unit)
    try:
        proof = helper.inspect_runtime(worker_config, before["pid"], before["cgroup"])
    except helper.IsolationError as exc:
        code = str(exc)
        fail(code if re.fullmatch(r"pmd_isolation_[a-z_]+", code) else "isolation_probe_failed")
    if unit_identity(unit) != before:
        fail("isolation_unit_changed")
    if not isinstance(proof, dict):
        fail("isolation_proof_invalid")
    return proof


def verify(config: dict[str, Any], *, include_runtime: bool = True) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    def check(name: str, operation: Callable[[], Any]) -> None:
        try:
            checks[name] = {"status": "ok", "details": operation()}
        except Exception as exc:
            code = str(exc) if isinstance(exc, ObservationError) else "unexpected_check_error:" + type(exc).__name__
            checks[name] = {"status": "failed", "error": code}
    for contract in config["containers"]:
        check("container:" + contract["name"], lambda item=contract: container_identity(item))
    for contract in config["units"]:
        check("unit:" + contract["name"], lambda item=contract: unit_identity(item))
    for contract in config["workers"]:
        check("worker:" + contract["name"], lambda item=contract: worker_health(item, config))
    for contract in config.get("isolation_probes", []):
        check("isolation:" + contract["name"], lambda item=contract: isolation_probe(item, config))
    if include_runtime:
        check("gpu_runtime", lambda: gpu_health(config))
        check("deployment", lambda: deployment_health(config))
        for contract in config.get("public_probes", []):
            check("public:" + contract["name"], lambda item=contract: public_probe(item))
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=("verify", "guard", "monitor"))
    args = parser.parse_args(argv)
    try:
        config, digest = private_json(args.config)
        validate_config(config)
        if args.mode == "guard":
            payload, code = guard(config, digest)
            print(json.dumps(payload, sort_keys=True))
            return code
        checks = verify(config, include_runtime=args.mode == "monitor")
        if args.mode == "monitor":
            try:
                checks["gpu_guard"] = {"status": "ok", "details": guard_health(config, digest)}
            except Exception as exc:
                code = str(exc) if isinstance(exc, ObservationError) else "unexpected_check_error:" + type(exc).__name__
                checks["gpu_guard"] = {"status": "failed", "error": code}
        failed = [name for name, check in checks.items() if check["status"] != "ok"]
        report = {"schema_version": 1, "checked_at": timestamp(), "status": "failed" if failed else "ok",
                  "mode": args.mode, "checks": checks, "failed_checks": failed,
                  "observer_config_sha256": digest}
        report["observer_script_sha256"] = file_hash(__file__)
        if args.mode == "monitor":
            atomic_write(Path(config["report_path"]), report)
        print(json.dumps(report, sort_keys=True))
        return 2 if failed else 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, ObservationError) else "unexpected_error:" + type(exc).__name__
        print(json.dumps({"status": "failed", "mode": args.mode, "error": code, "checked_at": timestamp()}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

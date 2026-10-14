"""Bounded, redacted system-manager configuration protocol (Linux only).

No environment value, path to an EnvironmentFile, or secret-derived digest is
part of the wire schema. A capture is a single-connection capability consumed
by validate, after the caller has completed its independent live-process CAS.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
import time
from uuid import uuid4

INSTALL_ROOT = Path("/usr/local/lib/nexpoly-gpu-systemd-inventory")
SOCKET_PATH = Path("/run/nexpoly-gpu-systemd-inventory/inventory.sock")
SERVICE = "nexpoly-gpu-systemd-inventory.service"
SERVICE_CGROUP = "/system.slice/" + SERVICE
ENTRY = INSTALL_ROOT / "ops/gpu_broker/systemd_provider.py"
EXEC_ARGV = ("/usr/bin/python3", "-I", "-S", "-B", str(ENTRY), "serve")
TARGET_GPU = 1
TARGET_UUID = "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771"
CALLER_UID = CALLER_GID = 1001
MAX_REQUEST = 4096
MAX_RESPONSE = 128 * 1024
MAX_UNITS = 256
MAX_FILES = 256
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_FILE_BYTES = 16 * 1024 * 1024
MAX_COMMAND_BYTES = 2 * 1024 * 1024
MAX_SECONDS = 10.0
PACKAGE_FILES = (
    "ops/__init__.py", "ops/gpu_broker/__init__.py",
    "ops/gpu_broker/broker.py", "ops/gpu_broker/server.py",
    "ops/gpu_broker/systemd_provider.py", "gpu_resource/__init__.py",
    "gpu_resource/client.py", "gpu_resource/transient_scope.py",
    "gpu_resource/systemd_inventory.py",
)
ERROR_CODES = frozenset({"unavailable", "invalid_request", "identity_changed",
                         "expired", "unsafe_file", "outside_target", "limit"})


class InventoryError(RuntimeError):
    """Only fixed public error codes; no underlying exception text."""
    def __init__(self, code="unavailable"):
        self.code = code if code in ERROR_CODES else "unavailable"
        super().__init__("systemd configuration provider: " + self.code)


def strict_json(body: bytes, maximum: int) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InventoryError("invalid_request")
            result[key] = value
        return result
    if not body or len(body) > maximum:
        raise InventoryError("limit")
    try:
        value = json.loads(body, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(InventoryError("invalid_request")))
    except (ValueError, UnicodeError, RecursionError):
        raise InventoryError("invalid_request") from None
    if not isinstance(value, dict):
        raise InventoryError("invalid_request")
    return value


def encode(value: dict, maximum: int) -> bytes:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(body) > maximum:
        raise InventoryError("limit")
    return body


def remaining(deadline: float, monotonic=time.monotonic) -> float:
    seconds = deadline - monotonic()
    if seconds <= 0:
        raise InventoryError("expired")
    return seconds


def peer_credentials(connection) -> tuple[int, int, int]:
    return struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))


def receive(connection, maximum: int, deadline: float, *, sender=None, monotonic=time.monotonic) -> dict:
    connection.settimeout(remaining(deadline, monotonic))
    body, ancillary, flags, _address = connection.recvmsg(maximum + 1,
        socket.CMSG_SPACE(12) + socket.CMSG_SPACE(16 * 4))
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            for offset in range(0, len(data) - len(data) % 4, 4):
                os.close(struct.unpack("i", data[offset:offset + 4])[0])
    if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(body) > maximum:
        raise InventoryError("limit")
    if len(ancillary) != 1:
        raise InventoryError("identity_changed")
    level, kind, data = ancillary[0]
    if level != socket.SOL_SOCKET or kind != socket.SCM_CREDENTIALS or len(data) != 12:
        raise InventoryError("identity_changed")
    actual = struct.unpack("3i", data)
    if sender is not None and actual != sender:
        raise InventoryError("identity_changed")
    remaining(deadline, monotonic)
    return strict_json(body, maximum)


def send(connection, value: dict, maximum: int, deadline: float, *, monotonic=time.monotonic):
    connection.settimeout(remaining(deadline, monotonic))
    body = encode(value, maximum)
    if connection.send(body) != len(body):
        raise InventoryError("unavailable")
    remaining(deadline, monotonic)


def request(value: dict, operation: str, nonce: str, capture_id=None):
    wanted = {"protocol", "operation", "target_gpu", "nonce"}
    if operation == "validate":
        wanted.add("capture_id")
    if (set(value) != wanted or type(value["protocol"]) is not int or value["protocol"] != 1
            or value["operation"] != operation or type(value["target_gpu"]) is not int
            or value["target_gpu"] != TARGET_GPU or value["nonce"] != nonce
            or re.fullmatch(r"[0-9a-f]{32}", str(nonce)) is None
            or (operation == "validate" and value["capture_id"] != capture_id)):
        raise InventoryError("invalid_request")


def process_identity(pid: int) -> tuple[int, str]:
    try:
        before = Path(f"/proc/{pid}/stat").read_text()
        ticks = int(before[before.rindex(")") + 2:].split()[19])
        rows = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        groups = [row[3:] for row in rows if row.startswith("0::")]
        after = Path(f"/proc/{pid}/stat").read_text()
        if (ticks <= 0 or before[before.rindex(")") + 2:].split()[19]
                != after[after.rindex(")") + 2:].split()[19] or len(groups) != 1
                or not groups[0].startswith("/") or len(groups[0]) > 4096):
            raise InventoryError("identity_changed")
        return ticks, groups[0]
    except (OSError, ValueError, IndexError):
        raise InventoryError("identity_changed") from None


def metadata_identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_nlink)


def trusted_directory(path: Path):
    """Root-owned, non-writable and no symlink in any path component."""
    result = []
    for parent in (*reversed(path.parents), path):
        value = parent.lstat()
        if not stat.S_ISDIR(value.st_mode) or value.st_uid != 0 or value.st_mode & 0o022:
            raise InventoryError("unsafe_file")
        result.append((str(parent), metadata_identity(value)))
    return tuple(result)


def trusted_bytes(path: Path, maximum: int):
    parents = trusted_directory(path.parent)
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_nlink != 1
            or before.st_mode & 0o022 or before.st_size > maximum):
        raise InventoryError("unsafe_file")
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        if metadata_identity(opened) != metadata_identity(before):
            raise InventoryError("identity_changed")
        body = os.pread(fd, maximum + 1, 0)
        if (len(body) != before.st_size or metadata_identity(os.fstat(fd)) != metadata_identity(before)
                or metadata_identity(path.lstat()) != metadata_identity(before)
                or trusted_directory(path.parent) != parents):
            raise InventoryError("identity_changed")
        return body, (metadata_identity(before), parents)
    finally:
        os.close(fd)


def verify_package():
    """Public, root-protected reviewed source manifest; not an env-file hash."""
    package_layout()
    raw, identity = trusted_bytes(INSTALL_ROOT / "manifest.json", 16384)
    manifest = strict_json(raw, 16384)
    if (set(manifest) != {"schema_version", "source_sha", "source_tree", "files"}
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or any(re.fullmatch(r"[0-9a-f]{40}", str(manifest[k])) is None for k in ("source_sha", "source_tree"))
            or not isinstance(manifest["files"], dict) or set(manifest["files"]) != set(PACKAGE_FILES)):
        raise InventoryError("unsafe_file")
    identities = []
    for relative in PACKAGE_FILES:
        body, file_identity = trusted_bytes(INSTALL_ROOT / relative, MAX_FILE_BYTES)
        if hashlib.sha256(body).hexdigest() != manifest["files"][relative]:
            raise InventoryError("unsafe_file")
        identities.append((relative, file_identity))
    package_layout()
    return manifest, (identity, tuple(identities))


def package_layout():
    # Source loaders can otherwise select .pyc or native extensions even when
    # all .py hashes match. A closed directory layout also prevents shadowing
    # standard-library names via the installed import root.
    layout = {}
    for relative in (*PACKAGE_FILES, "manifest.json"):
        path = Path(relative)
        for parent in (Path("."), *reversed(path.parent.parents), path.parent):
            if parent == path:
                continue
            layout.setdefault(str(parent), set())
        layout.setdefault(str(path.parent), set()).add(path.name)
        while path.parent != Path("."):
            path = path.parent
            layout.setdefault(str(path.parent), set()).add(path.name)
    for relative, expected in layout.items():
        directory = INSTALL_ROOT / relative
        trusted_directory(directory)
        names = set()
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name not in expected:
                    raise InventoryError("unsafe_file")
                names.add(entry.name)
        if names != expected:
            raise InventoryError("unsafe_file")


def interpreter_authority():
    declared = Path(EXEC_ARGV[0])
    parents = trusted_directory(declared.parent)
    metadata = declared.lstat()
    link = None
    if stat.S_ISLNK(metadata.st_mode):
        if metadata.st_uid != 0:
            raise InventoryError("unsafe_file")
        link = os.readlink(declared)
        target = Path(os.path.normpath(link if link.startswith("/") else str(declared.parent / link)))
    else:
        target = declared
    target_parents = trusted_directory(target.parent)
    opened = target.lstat()
    if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != 0 or opened.st_mode & 0o022
            or not opened.st_mode & stat.S_IXUSR):
        raise InventoryError("unsafe_file")
    if metadata_identity(declared.lstat()) != metadata_identity(metadata) or (link is not None and os.readlink(declared) != link):
        raise InventoryError("identity_changed")
    return (opened.st_dev, opened.st_ino), (metadata_identity(metadata), parents, link,
            str(target), metadata_identity(opened), target_parents)


def actual_argv(pid):
    with Path(f"/proc/{pid}/cmdline").open("rb") as stream:
        raw = stream.read(16385)
    if len(raw) > 16384 or not raw.endswith(b"\0"):
        raise InventoryError("identity_changed")
    try:
        return tuple(item.decode("utf-8") for item in raw[:-1].split(b"\0"))
    except UnicodeError:
        raise InventoryError("identity_changed") from None


def provider_runtime():
    """Root's own executable proof; no ptrace permission is given to UID1001."""
    pid = os.getpid()
    before = process_identity(pid)
    executable, authority = interpreter_authority()
    running = Path(f"/proc/{pid}/exe").stat()
    if (before[1] != SERVICE_CGROUP or actual_argv(pid) != EXEC_ARGV
            or (running.st_dev, running.st_ino) != executable
            or interpreter_authority() != (executable, authority) or process_identity(pid) != before):
        raise InventoryError("identity_changed")
    public = {"pid": pid, "start_ticks": before[0], "executable_device": executable[0], "executable_inode": executable[1]}
    return public, authority


@dataclass(frozen=True)
class UnitProjection:
    unit: str
    active_state: str
    sub_state: str
    invocation_id: str
    main_pid: str
    control_group: str
    user: str
    static_gpu1: bool
    statically_relevant: bool

    @classmethod
    def parse(cls, value):
        fields = set(cls.__dataclass_fields__)
        if (not isinstance(value, dict) or set(value) != fields
                or any(not isinstance(value[k], str) or len(value[k]) > 4096 or "\0" in value[k]
                       or "\n" in value[k] for k in fields - {"static_gpu1", "statically_relevant"})
                or re.fullmatch(r"[A-Za-z0-9_.:@\\-]+\.service", value["unit"]) is None
                or value["unit"].startswith("-")
                or value["active_state"] not in {"active", "activating", "reloading", "deactivating"}
                or re.fullmatch(r"[A-Za-z0-9_.:@-]+", value["sub_state"]) is None
                or re.fullmatch(r"(?:[0-9a-f]{32})?", value["invocation_id"]) is None
                or re.fullmatch(r"(?:0|[1-9][0-9]{0,9})", value["main_pid"]) is None
                or (value["control_group"] and not value["control_group"].startswith("/"))
                or type(value["static_gpu1"]) is not bool or type(value["statically_relevant"]) is not bool
                or (value["static_gpu1"] and not value["statically_relevant"])):
            raise InventoryError("invalid_request")
        return cls(**value)


def _provider_identity(peer, deadline):
    pid, uid, gid = peer
    if pid <= 0 or uid != 0 or gid != CALLER_GID:
        raise InventoryError("identity_changed")
    identity = process_identity(pid)
    if identity[1] != SERVICE_CGROUP:
        raise InventoryError("identity_changed")
    command = ["/usr/bin/systemctl", "show", "--property=Id", "--property=MainPID",
               "--property=InvocationID", "--property=ActiveState", "--property=SubState",
               "--property=User", "--property=Group", "--property=ControlGroup",
               "--property=ExecStart", "--", SERVICE]
    completed = subprocess.run(command, capture_output=True, text=True, check=False,
                               timeout=remaining(deadline), env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    if completed.returncode or len(completed.stdout.encode()) > 16384:
        raise InventoryError("unavailable")
    props = {}
    for row in completed.stdout.splitlines():
        key, sep, value = row.partition("=")
        if not sep or key in props:
            raise InventoryError("identity_changed")
        props[key] = value
    wanted = {"Id": SERVICE, "MainPID": str(pid), "ActiveState": "active", "SubState": "running",
              "User": "root", "Group": str(CALLER_GID), "ControlGroup": SERVICE_CGROUP}
    expected_exec = "path=/usr/bin/python3 ; argv[]=" + " ".join(EXEC_ARGV) + " ; ignore_errors=no ;"
    if (set(props) != {*wanted, "InvocationID", "ExecStart"}
            or any(props[k] != v for k, v in wanted.items())
            or re.fullmatch(r"[0-9a-f]{32}", props["InvocationID"]) is None
            or not props["ExecStart"].startswith("{ " + expected_exec)
            or props["ExecStart"].count("path=") != 1
            or actual_argv(pid) != EXEC_ARGV or process_identity(pid) != identity):
        raise InventoryError("identity_changed")
    return identity, props["InvocationID"], props["ExecStart"], interpreter_authority()


class SystemdConfigurationClient:
    def __init__(self, *, expected_source_sha, expected_source_tree, deadline=None, monotonic=time.monotonic):
        if any(re.fullmatch(r"[0-9a-f]{40}", str(value)) is None for value in (expected_source_sha, expected_source_tree)):
            raise InventoryError("identity_changed")
        self.expected_source = expected_source_sha, expected_source_tree
        self.monotonic = monotonic
        self.deadline = min(deadline, monotonic() + MAX_SECONDS) if deadline is not None else monotonic() + MAX_SECONDS
        self.connection = None
        self.nonce = uuid4().hex
        self.capture_id = None
        self.units = ()
        self.consumed = False

    def __enter__(self):
        try:
            if os.geteuid() != CALLER_UID or os.getegid() != CALLER_GID:
                raise InventoryError("identity_changed")
            self.caller = (os.getpid(), os.geteuid(), os.getegid())
            self.caller_identity = process_identity(self.caller[0])
            self.package = verify_package()
            manifest = self.package[0]
            if (manifest["source_sha"], manifest["source_tree"]) != self.expected_source:
                raise InventoryError("identity_changed")
            self.socket_parents = trusted_directory(SOCKET_PATH.parent)
            metadata = SOCKET_PATH.lstat()
            if (not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != 0
                    or metadata.st_gid != CALLER_GID or stat.S_IMODE(metadata.st_mode) != 0o660):
                raise InventoryError("unsafe_file")
            self.socket_identity = metadata_identity(metadata)
            self.connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            self.connection.settimeout(remaining(self.deadline, self.monotonic))
            self.connection.connect(str(SOCKET_PATH))
            self.peer = peer_credentials(self.connection)
            self.provider = _provider_identity(self.peer, self.deadline)
            self._check()
            send(self.connection, {"protocol": 1, "operation": "capture", "target_gpu": TARGET_GPU,
                                   "nonce": self.nonce}, MAX_REQUEST, self.deadline, monotonic=self.monotonic)
            response = self._response("capture")
            if set(response) != {"protocol", "operation", "target_gpu", "nonce", "ok", "caller_pid", "capture_id", "units", "provider"}:
                raise InventoryError("invalid_request")
            if re.fullmatch(r"[0-9a-f]{32}", str(response["capture_id"])) is None:
                raise InventoryError("invalid_request")
            if not isinstance(response["units"], list) or len(response["units"]) > MAX_UNITS:
                raise InventoryError("limit")
            self.units = tuple(UnitProjection.parse(item) for item in response["units"])
            names = [unit.unit for unit in self.units]
            if names != sorted(set(names)):
                raise InventoryError("invalid_request")
            self.capture_id = response["capture_id"]
            self._check()
            return self
        except Exception:
            self.close()
            raise

    def _response(self, operation):
        value = receive(self.connection, MAX_RESPONSE, self.deadline, sender=self.peer, monotonic=self.monotonic)
        if (value.get("protocol") != 1 or type(value.get("protocol")) is not int
                or value.get("operation") != operation or value.get("target_gpu") != TARGET_GPU
                or type(value.get("target_gpu")) is not int or value.get("nonce") != self.nonce
                or value.get("caller_pid") != self.caller[0] or type(value.get("caller_pid")) is not int
                or type(value.get("ok")) is not bool):
            raise InventoryError("identity_changed")
        if not value["ok"]:
            if set(value) != {"protocol", "operation", "target_gpu", "nonce", "ok", "caller_pid", "error"}:
                raise InventoryError("invalid_request")
            raise InventoryError(value.get("error"))
        expected = {"pid": self.peer[0], "start_ticks": self.provider[0][0],
                    "executable_device": self.provider[3][0][0], "executable_inode": self.provider[3][0][1]}
        observed = value.get("provider")
        if (not isinstance(observed, dict) or set(observed) != set(expected)
                or any(type(observed[key]) is not int or observed[key] != wanted for key, wanted in expected.items())):
            raise InventoryError("identity_changed")
        return value

    def _check(self):
        remaining(self.deadline, self.monotonic)
        if (os.getpid() != self.caller[0] or process_identity(self.caller[0]) != self.caller_identity
                or peer_credentials(self.connection) != self.peer
                or _provider_identity(self.peer, self.deadline) != self.provider
                or metadata_identity(SOCKET_PATH.lstat()) != self.socket_identity
                or trusted_directory(SOCKET_PATH.parent) != self.socket_parents
                or verify_package() != self.package):
            raise InventoryError("identity_changed")

    def validate(self):
        if self.consumed or self.connection is None or self.capture_id is None:
            raise InventoryError("invalid_request")
        self.consumed = True
        self._check()
        send(self.connection, {"protocol": 1, "operation": "validate", "target_gpu": TARGET_GPU,
                              "nonce": self.nonce, "capture_id": self.capture_id},
             MAX_REQUEST, self.deadline, monotonic=self.monotonic)
        response = self._response("validate")
        if (set(response) != {"protocol", "operation", "target_gpu", "nonce", "ok", "caller_pid", "capture_id", "provider"}
                or response["capture_id"] != self.capture_id):
            raise InventoryError("invalid_request")
        self._check()

    def close(self):
        self.consumed = True
        self.units = ()
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __exit__(self, *_):
        self.close()


@contextmanager
def configuration_client(*, expected_source_sha, expected_source_tree, deadline=None, monotonic=time.monotonic):
    try:
        with SystemdConfigurationClient(expected_source_sha=expected_source_sha,
                                       expected_source_tree=expected_source_tree,
                                       deadline=deadline, monotonic=monotonic) as client:
            yield client
    except (InventoryError, OSError, ValueError, subprocess.SubprocessError):
        # Native callers may log this exception; never retain secret-bearing
        # systemctl stderr/tracebacks as a chained exception.
        from ops.gpu_broker.broker import BrokerError
        raise BrokerError("gpu_claim_inventory_unavailable", "systemd configuration provider unavailable or changed") from None

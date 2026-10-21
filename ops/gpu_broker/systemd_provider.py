"""Proposed Root service. Development/review does not install or run it.

The entry point imports only the standard library until a fixed, Root-owned
installation manifest and every package path have been verified. No checkout,
user-site, PATH command, request path, or caller environment is an authority.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import socket
import stat
import subprocess
import sys
import time
from uuid import uuid4

_INSTALL = Path("/usr/local/lib/nexpoly-gpu-systemd-inventory")
_FILES = (
    "ops/__init__.py", "ops/gpu_broker/__init__.py", "ops/gpu_broker/broker.py",
    "ops/gpu_broker/server.py", "ops/gpu_broker/systemd_provider.py",
    "gpu_resource/__init__.py", "gpu_resource/client.py",
    "gpu_resource/transient_scope.py", "gpu_resource/systemd_inventory.py",
)


def _bootstrap():
    if (os.geteuid() != 0 or os.getegid() != 1001 or not sys.flags.isolated or not sys.flags.no_site
            or not sys.flags.dont_write_bytecode
            or sys.pycache_prefix is not None
            or Path(__file__) != _INSTALL / "ops/gpu_broker/systemd_provider.py"
            or sys.argv != [str(_INSTALL / "ops/gpu_broker/systemd_provider.py"), "serve"]):
        raise RuntimeError("unavailable")
    # Before importing any package module, forbid caches, extensions and any
    # non-whitelisted entry. -B alone does not prevent reading stale bytecode.
    layout = {".": {"manifest.json", "ops", "gpu_resource"},
              "ops": {"__init__.py", "gpu_broker"},
              "ops/gpu_broker": {"__init__.py", "broker.py", "server.py", "systemd_provider.py"},
              "gpu_resource": {"__init__.py", "client.py", "transient_scope.py", "systemd_inventory.py"}}
    for relative, expected in layout.items():
        directory = _INSTALL / relative
        for parent in (*reversed(directory.parents), directory):
            value = parent.lstat()
            if not stat.S_ISDIR(value.st_mode) or value.st_uid != 0 or value.st_mode & 0o022:
                raise RuntimeError("unavailable")
        seen = set()
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name not in expected:
                    raise RuntimeError("unavailable")
                seen.add(entry.name)
        if seen != expected:
            raise RuntimeError("unavailable")
    read_identities = {}
    def public_bytes(path, maximum):
        def identity(value):
            return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid,
                    value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_nlink)
        parents = []
        for parent in (*reversed(path.parent.parents), path.parent):
            st = parent.lstat()
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
                raise RuntimeError("unavailable")
            parents.append((str(parent), identity(st)))
        st = path.lstat()
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022
                or st.st_nlink != 1 or st.st_size > maximum):
            raise RuntimeError("unavailable")
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            raw = os.pread(fd, maximum + 1, 0)
            after = os.fstat(fd)
            if (identity(st) != identity(before) or identity(before) != identity(after)
                    or identity(path.lstat()) != identity(st) or len(raw) != st.st_size):
                raise RuntimeError("unavailable")
            read_identities[str(path)] = (identity(before), tuple(parents))
            return raw
        finally:
            os.close(fd)
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise RuntimeError("unavailable")
            result[key] = value
        return result
    manifest = json.loads(public_bytes(_INSTALL / "manifest.json", 16384), object_pairs_hook=unique)
    if (not isinstance(manifest, dict) or set(manifest) != {"schema_version", "source_sha", "source_tree", "files"}
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or any(re.fullmatch(r"[0-9a-f]{40}", str(manifest[k])) is None for k in ("source_sha", "source_tree"))
            or not isinstance(manifest["files"], dict) or set(manifest["files"]) != set(_FILES)):
        raise RuntimeError("unavailable")
    for relative in _FILES:
        if hashlib.sha256(public_bytes(_INSTALL / relative, 1024 * 1024)).hexdigest() != manifest["files"][relative]:
            raise RuntimeError("unavailable")
    # Only this verified installation is added; isolated standard-library paths
    # remain the other import roots. Never import from a UID1001 checkout.
    sys.path.insert(0, str(_INSTALL))
    from gpu_resource import systemd_inventory as protocol
    expected_identity = (read_identities[str(_INSTALL / "manifest.json")],
        tuple((relative, read_identities[str(_INSTALL / relative)]) for relative in _FILES))
    native, boot_package = _load_verified_native(protocol, manifest, expected_identity)
    boot_runtime = protocol.provider_runtime()
    return protocol, native, boot_package, boot_runtime


def _import_native():
    from ops.gpu_broker import server
    return server


def _load_verified_native(protocol, manifest, expected_identity):
    boot_package = protocol.verify_package()
    if boot_package != (manifest, expected_identity):
        raise RuntimeError("unavailable")
    native = _import_native()
    if protocol.verify_package() != boot_package:
        raise RuntimeError("unavailable")
    return native, boot_package


class FixedSystemctl:
    """Read-only fixed argv with bounded stdout and a hard request deadline."""
    def __init__(self, protocol, deadline, *, monotonic=time.monotonic):
        self.p = protocol
        self.deadline = deadline
        self.monotonic = monotonic

    def __call__(self, command, **_kwargs):
        p = self.p
        if not isinstance(command, (list, tuple)) or not command or command[0] != "systemctl":
            raise p.InventoryError("invalid_request")
        arguments = list(command[1:])
        if arguments == ["list-units", "--type=service", "--state=active,activating,reloading,deactivating", "--no-legend", "--plain"]:
            pass
        elif arguments == ["show-environment"]:
            pass
        elif arguments and arguments[0] == "show":
            props = ["--property=" + key for key in (
                "Id", "ActiveState", "SubState", "InvocationID", "MainPID", "ControlGroup", "User",
                "Environment", "EnvironmentFiles", "PassEnvironment", "UnsetEnvironment")]
            units = arguments[len(props) + 1:]
            if (arguments[1:len(props) + 1] != props or not units or len(units) > p.MAX_UNITS
                    or any(not isinstance(unit, str) or len(unit) > 255 or unit.startswith("-")
                           or re.fullmatch(r"[A-Za-z0-9_.:@\\-]+\.service", unit) is None for unit in units)
                    or units != sorted(set(units))):
                raise p.InventoryError("invalid_request")
            arguments = ["show", *props, "--", *units]
        else:
            raise p.InventoryError("invalid_request")
        child = subprocess.Popen(["/usr/bin/systemctl", *arguments], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                 close_fds=True, cwd="/", env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        output = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while True:
                    seconds = p.remaining(self.deadline, self.monotonic)
                    if not selector.select(seconds):
                        raise p.InventoryError("expired")
                    chunk = os.read(child.stdout.fileno(), min(65536, p.MAX_COMMAND_BYTES + 1 - len(output)))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > p.MAX_COMMAND_BYTES:
                        raise p.InventoryError("limit")
            code = child.wait(timeout=p.remaining(self.deadline, self.monotonic))
            if code != 0:
                raise p.InventoryError("unavailable")
            return subprocess.CompletedProcess(command, code, output.decode("utf-8"), "")
        except (OSError, UnicodeError, subprocess.SubprocessError):
            raise p.InventoryError("unavailable") from None
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=1)
            child.stdout.close()


def file_configuration(path: Path, optional: bool, p, maximum=None):
    """Complete identity/content CAS with no caller-controlled paths.

    Path is derived exclusively from Root system-manager metadata. Stronger
    than the legacy UID1001 reader: every ancestor and final file must be
    Root-owned and non-writable. Unsupported paths fail closed. Compatibility
    links are allowed only at the last component with a secured target chain.
    """
    if (not path.is_absolute() or str(path) != os.path.normpath(str(path))
            or str(path).startswith("//") or len(str(path)) > 4096):
        raise p.InventoryError("unsafe_file")
    parents = p.trusted_directory(path.parent)
    try:
        declared = path.lstat()
    except FileNotFoundError:
        if not optional:
            raise p.InventoryError("unsafe_file") from None
        if p.trusted_directory(path.parent) != parents:
            raise p.InventoryError("identity_changed")
        return {}, (str(path), True, parents, None), 0
    link = None
    target = path
    if stat.S_ISLNK(declared.st_mode):
        if declared.st_uid != 0:
            raise p.InventoryError("unsafe_file")
        link = os.readlink(path)
        # Resolve only this compatibility link; trusted_directory below
        # rejects any symlink in the resolved target's ancestor chain.
        target = Path(os.path.normpath(str(path.parent / link))) if not link.startswith("/") else Path(os.path.normpath(link))
    maximum = p.MAX_FILE_BYTES if maximum is None else min(maximum, p.MAX_FILE_BYTES)
    raw, target_identity = p.trusted_bytes(target, maximum)
    if (p.metadata_identity(path.lstat()) != p.metadata_identity(declared)
            or p.trusted_directory(path.parent) != parents
            or (link is not None and os.readlink(path) != link)):
        raise p.InventoryError("identity_changed")
    configured = {}
    try:
        for row in raw.decode("utf-8").splitlines():
            row = row.strip()
            if not row or row.startswith(("#", ";")):
                continue
            for field in shlex.split(row, comments=True):
                name, sep, value = field.partition("=")
                if not sep or not name:
                    raise p.InventoryError("unsafe_file")
                configured[name] = value
    except (ValueError, UnicodeError):
        raise p.InventoryError("unsafe_file") from None
    # Hash and paths never leave Root process memory, never appear in logs.
    fingerprint = (str(path), False, parents, p.metadata_identity(declared), link,
                   str(target), target_identity, hashlib.sha256(raw).digest())
    return configured, fingerprint, len(raw)


@dataclass(frozen=True)
class ConfigurationSnapshot:
    authorities: tuple
    manager: dict | None
    files: tuple
    units: tuple


def capture_configuration(p, native, deadline, *, run=None, read_file=None, monotonic=time.monotonic):
    """Root configuration only; no /proc environ, NVIDIA, Docker or GPU IO."""
    run = run if run is not None else FixedSystemctl(p, deadline, monotonic=monotonic)
    read_file = read_file if read_file is not None else lambda path, optional, maximum: file_configuration(path, optional, p, maximum)
    authorities = native._query_systemd_unit_authorities("system", run=run)
    if len(authorities) > p.MAX_UNITS:
        raise p.InventoryError("limit")
    if native.EXPECTED_GPU_UUIDS.get(p.TARGET_GPU) != p.TARGET_UUID:
        raise p.InventoryError("outside_target")
    manager = None
    fingerprints = []
    projections = []
    total_bytes = 0
    relevant = {"CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES"}
    for authority in authorities:
        p.remaining(deadline, monotonic)
        if authority.active_state in {"activating", "reloading", "deactivating"} and authority.environment_files:
            raise p.InventoryError("unavailable")
        environment = native._parse_systemd_environment(authority.environment, declaration="Environment", require_values=True)
        passed = native._parse_systemd_environment(authority.pass_environment, declaration="PassEnvironment", require_values=False)
        unset = native._parse_systemd_environment(authority.unset_environment, declaration="UnsetEnvironment", require_values=False)
        pass_names = {entry.split("=", 1)[0] for entry in passed}
        if pass_names & relevant and manager is None:
            manager = native._query_systemd_manager_environment(["systemctl"], "system", run=run)
        configured = {name: manager[name] for name in pass_names if manager is not None and name in manager}
        configured.update(entry.split("=", 1) for entry in environment)
        for declaration in authority.environment_files:
            if not declaration:
                continue
            tokens = shlex.split(declaration)
            if len(tokens) != 2 or tokens[1] not in {"(ignore_errors=yes)", "(ignore_errors=no)"}:
                raise p.InventoryError("unavailable")
            raw_path = tokens[0]
            optional = tokens[1] == "(ignore_errors=yes)"
            if raw_path.startswith("-/"):
                if not optional:
                    raise p.InventoryError("unavailable")
                raw_path = raw_path[1:]
            if len(fingerprints) >= p.MAX_FILES:
                raise p.InventoryError("limit")
            values, fingerprint, size = read_file(Path(raw_path), optional, p.MAX_TOTAL_FILE_BYTES - total_bytes)
            total_bytes += size
            if total_bytes > p.MAX_TOTAL_FILE_BYTES:
                raise p.InventoryError("limit")
            configured.update(values)
            fingerprints.append(fingerprint)
        for entry in unset:
            key, sep, value = entry.partition("=")
            if not sep or configured.get(key) == value:
                configured.pop(key, None)
        statically_relevant = bool(set(configured) & relevant or pass_names & relevant)
        devices = set()
        for name in relevant:
            value = configured.get(name)
            if value is None or value.strip().lower() in {"", "none", "void"}:
                continue
            if value.strip().lower() == "all":
                devices.update(native.EXPECTED_GPU_UUIDS.values())
            else:
                devices.update(native._resolve_gpu_claim_tokens(value.strip().split(",")))
        static_gpu1 = p.TARGET_UUID in devices
        # No weaker projection of a mixed declaration into a GPU1-only claim.
        if static_gpu1 and devices != {p.TARGET_UUID}:
            raise p.InventoryError("outside_target")
        projection = p.UnitProjection.parse({key: getattr(authority, key) for key in (
            "unit", "active_state", "sub_state", "invocation_id", "main_pid", "control_group", "user")}
            | {"static_gpu1": static_gpu1, "statically_relevant": statically_relevant})
        projections.append(projection)
    p.remaining(deadline, monotonic)
    return ConfigurationSnapshot(authorities, manager, tuple(fingerprints), tuple(projections))


class ConnectionSession:
    """Exactly capture then validate; no cross-connection handle or cache."""
    def __init__(self, connection, p, native, *, monotonic=time.monotonic,
                 identity=None, capture=None, verify=None, boot_package, boot_runtime, runtime=None):
        self.connection, self.p, self.native = connection, p, native
        self.monotonic = monotonic
        self.identity = identity or p.process_identity
        self.capture = capture or (lambda deadline: capture_configuration(p, native, deadline, monotonic=monotonic))
        self.verify = verify or p.verify_package
        self.boot_package = boot_package
        self.boot_runtime = boot_runtime
        self.runtime = runtime or p.provider_runtime
        self.snapshot = None

    def run(self):
        p = self.p
        deadline = self.monotonic() + p.MAX_SECONDS
        peer = p.peer_credentials(self.connection)
        operation, nonce = "capture", "0" * 32
        try:
            if peer[1:] != (p.CALLER_UID, p.CALLER_GID) or peer[0] <= 0:
                raise p.InventoryError("identity_changed")
            caller_identity = self.identity(peer[0])
            def check():
                p.remaining(deadline, self.monotonic)
                if (p.peer_credentials(self.connection) != peer or self.identity(peer[0]) != caller_identity
                        or self.verify() != self.boot_package or self.runtime() != self.boot_runtime):
                    raise p.InventoryError("identity_changed")
            check()
            value = p.receive(self.connection, p.MAX_REQUEST, deadline, sender=peer, monotonic=self.monotonic)
            nonce = value.get("nonce")
            p.request(value, "capture", nonce)
            check()
            self.snapshot = self.capture(deadline)
            if self.capture(deadline) != self.snapshot:
                raise p.InventoryError("identity_changed")
            check()
            capture_id = uuid4().hex
            p.send(self.connection, {"protocol": 1, "operation": "capture", "target_gpu": p.TARGET_GPU,
                   "nonce": nonce, "ok": True, "caller_pid": peer[0], "capture_id": capture_id,
                   "units": [asdict(unit) for unit in self.snapshot.units], "provider": self.boot_runtime[0]},
                   p.MAX_RESPONSE, deadline, monotonic=self.monotonic)
            operation = "validate"
            value = p.receive(self.connection, p.MAX_REQUEST, deadline, sender=peer, monotonic=self.monotonic)
            # Consume even on an invalid validate attempt. Never expose a second
            # success, retryable handle, snapshot id, or secret-derived hash.
            expected, self.snapshot = self.snapshot, None
            p.request(value, "validate", nonce, capture_id)
            check()
            if self.capture(deadline) != expected:
                raise p.InventoryError("identity_changed")
            check()
            p.send(self.connection, {"protocol": 1, "operation": "validate", "target_gpu": p.TARGET_GPU,
                   "nonce": nonce, "ok": True, "caller_pid": peer[0], "capture_id": capture_id, "provider": self.boot_runtime[0]},
                   # Public runtime evidence only, proved internally by Root.
                   p.MAX_RESPONSE, deadline, monotonic=self.monotonic)
        except Exception as exc:
            code = exc.code if isinstance(exc, p.InventoryError) else "unavailable"
            # Fixed response only. Never print exc, traceback, raw unit or env.
            try:
                if re.fullmatch(r"[0-9a-f]{32}", str(nonce)) is None:
                    nonce = "0" * 32
                p.send(self.connection, {"protocol": 1, "operation": operation, "target_gpu": p.TARGET_GPU,
                       "nonce": nonce, "ok": False, "caller_pid": peer[0], "error": code},
                       p.MAX_RESPONSE, deadline, monotonic=self.monotonic)
            except Exception:
                pass
        finally:
            self.snapshot = None
            self.connection.close()


def serve(p, native, boot_package, boot_runtime):
    # RuntimeDirectory is created by the fixed service. Never replace a socket
    # or use a caller's directory. Concurrency is one, request life <=10s.
    if p.verify_package() != boot_package or p.provider_runtime() != boot_runtime:
        raise p.InventoryError("identity_changed")
    p.trusted_directory(p.SOCKET_PATH.parent)
    if p.SOCKET_PATH.exists() or p.SOCKET_PATH.is_symlink():
        raise p.InventoryError("unsafe_file")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    try:
        os.umask(0o077)
        listener.bind(str(p.SOCKET_PATH))
        # Service Group=1001 creates the socket with its intended group;
        # no chown capability or filesystem permission relaxation is needed.
        metadata = p.SOCKET_PATH.lstat()
        if metadata.st_uid != 0 or metadata.st_gid != p.CALLER_GID:
            raise p.InventoryError("identity_changed")
        os.chmod(p.SOCKET_PATH, 0o660)
        socket_identity = p.metadata_identity(p.SOCKET_PATH.lstat())
        listener.listen(1)
        while True:
            connection, _ = listener.accept()
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            ConnectionSession(connection, p, native, boot_package=boot_package, boot_runtime=boot_runtime).run()
    finally:
        listener.close()
        # Only remove the exact socket created by this process; service-owned
        # RuntimeDirectory is removed by systemd after stop.
        if "socket_identity" in locals() and p.metadata_identity(p.SOCKET_PATH.lstat()) == socket_identity:
            p.SOCKET_PATH.unlink()


def main():
    try:
        p, native, boot_package, boot_runtime = _bootstrap()
        serve(p, native, boot_package, boot_runtime)
    except Exception:
        print("systemd configuration provider unavailable", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

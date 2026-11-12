#!/usr/bin/env python3
"""Expose the existing monitoring API to the 9001 frontend over a private socket.

Mount /run/user/<uid>/nexpoly-server-monitoring read-only into the frontend
container. This script manages only its two bridge units; it never starts,
restarts, or enables the existing monitoring API.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import subprocess
import tempfile


MARKER = "# Managed by scripts/server_monitoring_dev_bridge.py"
API_UNIT = "nexpoly-server-monitoring-api.service"
UNITS = {
    "socket": "nexpoly-server-monitoring-dev-bridge.socket",
    "service": "nexpoly-server-monitoring-dev-bridge.service",
}
PROXY = Path("/usr/lib/systemd/systemd-socket-proxyd")
RUNTIME_ROOT = Path(f"/run/user/{os.getuid()}")
RUNTIME = RUNTIME_ROOT / "nexpoly-server-monitoring"
SOCKET = RUNTIME / "api.sock"


def user_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    environment["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{os.getuid()}/bus"
    return environment


def systemctl(*arguments: str, check: bool = True, capture_output: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *arguments], env=user_environment(),
        check=check, capture_output=capture_output, text=True,
    )


def quoted(value: Path | str) -> str:
    value = str(value)
    if any(character in value for character in "\n\r\0"):
        raise ValueError("Invalid systemd argument")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def no_symlink_parents(path: Path) -> None:
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid()):
            raise RuntimeError(f"Unsafe directory ancestor: {parent}")


def owned_directory(path: Path, *, create: bool = False, private: bool = False) -> None:
    no_symlink_parents(path)
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(f"Directory must be owned by the current user and not a symlink: {path}")
    mode = stat.S_IMODE(info.st_mode)
    if (private and mode != 0o700) or mode & 0o022:
        raise RuntimeError(f"Unsafe directory permissions: {path}; expected {'0700' if private else 'no group/other writes'}")


def check_socket(*, required: bool = False) -> None:
    try:
        info = SOCKET.lstat()
    except FileNotFoundError:
        if required:
            raise RuntimeError(f"Bridge did not create its socket: {SOCKET}") from None
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError(f"Socket must be owned by the current user, mode 0600, and not a symlink: {SOCKET}")


def check_runtime(*, create: bool = False) -> None:
    owned_directory(RUNTIME_ROOT, private=True)
    owned_directory(RUNTIME, create=create, private=True)
    check_socket()


def unit_directory(*, create: bool = False) -> Path:
    directory = Path.home()
    owned_directory(directory)
    for component in (".config", "systemd", "user"):
        directory /= component
        owned_directory(directory, create=create)
    return directory


def bridge_unit(role: str) -> str:
    common = (
        f"{MARKER}\n[Unit]\nDescription=NexPoly 9001 monitoring API Unix socket bridge\n"
        f"After={API_UNIT}\n"
    )
    if role == "socket":
        return common + (
            f"[Socket]\nListenStream={SOCKET}\nService={UNITS['service']}\n"
            "SocketMode=0600\nDirectoryMode=0700\nRemoveOnStop=yes\n"
        )
    if role == "service":
        return common + (
            f"Requires={UNITS['socket']}\nAfter={UNITS['socket']}\n"
            "[Service]\nType=simple\nUMask=0077\nNoNewPrivileges=yes\n"
            f"ExecStart={quoted(PROXY)} 127.0.0.1:5319\n"
            "Restart=on-failure\nRestartSec=3\nTimeoutStopSec=10\n"
        )
    raise ValueError(f"Unknown bridge unit role: {role}")


def check_unit(path: Path, *, required: bool = False, expected: str | None = None) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if required:
            raise RuntimeError("Bridge units are missing; run prepare first") from None
        return
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise RuntimeError(f"Refusing to use or replace an unsafe unit: {path}")
    content = path.read_text()
    if not content.startswith(MARKER + "\n"):
        raise RuntimeError(f"Refusing to use or replace an unrelated unit: {path}")
    if expected is not None and content != expected:
        raise RuntimeError(f"Bridge unit differs from its expected configuration; run prepare first: {path}")


def require_managed_units(*, exact: bool = False) -> None:
    directory = unit_directory()
    for role, name in UNITS.items():
        target = directory / name
        check_unit(target, required=True, expected=bridge_unit(role) if exact else None)
        result = systemctl(
            "show", name, "--property=LoadState,FragmentPath,DropInPaths", capture_output=True,
        )
        properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if (properties.get("LoadState") != "loaded" or properties.get("FragmentPath") != str(target)
                or properties.get("DropInPaths") != ""):
            raise RuntimeError(f"Refusing to control an overridden or unloaded bridge unit: {name}")


def prepare() -> None:
    if not PROXY.is_file() or not os.access(PROXY, os.X_OK):
        raise RuntimeError(f"The systemd socket proxy executable is missing: {PROXY}")
    directory = unit_directory(create=True)
    # Check both destinations before changing either unit.
    for name in UNITS.values():
        check_unit(directory / name)
    check_runtime(create=True)
    for role, name in UNITS.items():
        target = directory / name
        descriptor, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=directory)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(bridge_unit(role))
            check_unit(target)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    systemctl("daemon-reload")
    print(f"Prepared bridge units and private directory {RUNTIME}; nothing started or enabled.")


def start() -> None:
    check_runtime()
    require_managed_units(exact=True)
    result = systemctl("is-active", "--quiet", API_UNIT, check=False)
    if result.returncode:
        raise RuntimeError("The existing monitoring API must already be active; it was not started or restarted")
    systemctl("start", UNITS["socket"])
    check_socket(required=True)
    print(f"Bridge listening on {SOCKET}; mount {RUNTIME} read-only into the 9001 frontend.")


def stop() -> None:
    check_runtime()
    require_managed_units()
    systemctl("stop", UNITS["socket"], UNITS["service"])
    print("Stopped only the development bridge socket and service.")


def status() -> None:
    print(f"Bridge socket: {SOCKET}", flush=True)
    systemctl("status", "--no-pager", UNITS["socket"], UNITS["service"], check=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "start", "stop", "status"))
    arguments = parser.parse_args()
    try:
        {"prepare": prepare, "start": start, "stop": stop, "status": status}[arguments.action]()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()

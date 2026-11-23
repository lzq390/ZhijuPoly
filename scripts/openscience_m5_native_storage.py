"""Expose only the four pinned M5 guest filesystems to the native development Docker engine."""

import argparse
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import time
import urllib.request

import openscience_per_user_dev as manager

OPS = Path(__file__).resolve().parents[1] / "ops/openscience-per-user-dev"
ALIAS = "m5-quota-guest"
FMT = "nexpoly-m5-nfs-v1"
GUEST = "/opt/nexpoly-m5-nfs"


def private_dir(path):
    path.mkdir(mode=0o700, exist_ok=True)
    if (
        path.is_symlink()
        or path.stat().st_uid != os.getuid()
        or path.stat().st_mode & 0o077
    ):
        raise ValueError(f"Private operator directory required: {path}")
    return path


def ssh_config_text(work, port):
    return (
        f"Host m5-quota-guest\n  HostName 127.0.0.1\n  Port {port}\n  User root\n"
        f'  IdentityFile "{work}/key"\n  UserKnownHostsFile "{work}/known_hosts"\n'
        "  StrictHostKeyChecking yes\n  GlobalKnownHostsFile /dev/null\n  IdentitiesOnly yes\n"
        "  HostKeyAlgorithms ssh-ed25519\n  BatchMode yes\n  ConnectTimeout 15\n"
    )


def ssh(vm):
    config = vm / "ssh.conf"
    if (
        config.is_symlink()
        or config.stat().st_uid != os.getuid()
        or config.stat().st_mode & 0o077
    ):
        raise ValueError("Expected private SSH configuration from the M5 VM launcher")
    machine = json.loads((vm / "vm.json").read_bytes())
    port = machine["ssh_port"]
    if (
        machine["format"] != "nexpoly-m5-isolated-browser-v1"
        or type(port) is not int
        or not 1024 <= port <= 65535
        or port == 9011
        or config.read_text() != ssh_config_text(vm, port)
    ):
        raise ValueError("Expected the pinned M5 VM's loopback SSH endpoint")
    return [
        "ssh",
        "-F",
        str(config),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
    ]


def guest(vm, command, data=None):
    return subprocess.check_output([*ssh(vm), ALIAS, command], input=data, timeout=180)


def install(vm, package_dir):
    pins = OPS / "nfs-ubuntu-packages.json"
    data = io.BytesIO()
    with TemporaryDirectory() as tmp, tarfile.open(fileobj=data, mode="w:gz") as tar:
        tar.add(OPS / "guest-nfs-exports.py", "guest.py")
        tar.add(pins, "packages.json")
        for source, expected in json.loads(pins.read_bytes()).items():
            name = Path(source).name
            path = package_dir / name if package_dir else Path(tmp) / name
            if not package_dir:
                with urllib.request.urlopen(
                    "https://archive.ubuntu.com/ubuntu/" + source, timeout=30
                ) as response:
                    path.write_bytes(response.read(4 * 1024 * 1024))
            if (
                path.is_symlink()
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected
            ):
                raise ValueError(f"Unexpected Ubuntu package: {name}")
            tar.add(path, "debs/" + name, recursive=False)
    return json.loads(
        guest(
            vm,
            f"umask 077 && install -d -m0700 {GUEST} && tar --no-same-owner -C {GUEST} -xzf - && python3 -IB {GUEST}/guest.py install",
            data.getvalue(),
        )
    )


def mount_options(manifest, row):
    return {
        "type": "nfs",
        "device": ":" + row["pseudo"] + "/workspace",
        "o": f"addr=127.0.0.1,port={manifest['port']},nfsvers=4.1,proto=tcp,resvport,hard,timeo=30,retrans=2",
    }


def volume_labels(manifest, slot, row):
    return {
        "org.nexpoly.m5.native": manifest["run_id"],
        "org.nexpoly.m5.slot": slot,
        "org.nexpoly.m5.filesystem-uuid": row["filesystem_uuid"],
    }


def prepare(vm, package_dir):
    path = vm / "native/manifest.json"
    if path.exists():
        current = read(vm)
        rows = json.loads(
            guest(
                vm,
                f"systemctl start m5-native-nfs.service && python3 -IB {GUEST}/guest.py status",
            )
        )
    else:
        rows = install(vm, package_dir)
    if set(rows) != {"src-a", "src-b", "dst-a", "dst-b"}:
        raise ValueError("Unexpected guest quota exports")
    if path.exists():
        run_id = current["run_id"]
        port = current["port"]
    else:
        run_id = secrets.token_hex(6)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
    for slot, row in rows.items():
        row["volume"] = f"nexpoly-m5-{run_id}-{slot}"
    result = {
        "format": FMT,
        "run_id": run_id,
        "ssh_config": str(vm / "ssh.conf"),
        "ssh_alias": ALIAS,
        "port": port,
        "workspaces": rows,
    }
    if path.exists() and result != current:
        raise ValueError("Retained manifest differs from actual guest bindings")
    manager.write_atomic(
        path, json.dumps(result, sort_keys=True, indent=2).encode() + b"\n", 0o600
    )
    return result


def read(vm):
    path = vm / "native/manifest.json"
    if (
        path.is_symlink()
        or path.stat().st_mode & 0o077
        or path.stat().st_uid != os.getuid()
    ):
        raise ValueError("Unexpected native storage manifest permissions")
    data = json.loads(path.read_bytes())
    if (
        data["format"] != FMT
        or data["ssh_config"] != str(vm / "ssh.conf")
        or data["ssh_alias"] != ALIAS
    ):
        raise ValueError("Unexpected M5 native storage manifest")
    if len(data["run_id"]) != 12 or any(
        c not in "0123456789abcdef" for c in data["run_id"]
    ):
        raise ValueError("Invalid native storage run identifier")
    if not 1024 <= data["port"] <= 65535 or data["port"] == 9011:
        raise ValueError("Invalid native NFS port")
    return data


def volumes(config):
    manager.local_engine()
    for slot, row in config["workspaces"].items():
        opts = mount_options(config, row)
        labels = volume_labels(config, slot, row)
        name = row["volume"]
        existing = manager.docker("volume", "inspect", name, missing=True)
        if existing is None:
            args = ["volume", "create", "--driver", "local"]
            for key, val in opts.items():
                args.extend(("--opt", key + "=" + val))
            for key, val in labels.items():
                args.extend(("--label", key + "=" + val))
            manager.docker(*args, name)
        data = json.loads(manager.docker("volume", "inspect", name))[0]
        if (
            data["Driver"] != "local"
            or data["Options"] != opts
            or data["Labels"] != labels
        ):
            raise ValueError("Volume identity differs: " + name)


def socket_dir(config):
    return private_dir(
        Path("/tmp") / f"nexpoly-m5-native-{os.getuid()}-{config['run_id']}"
    )


def control(config, verb):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        s.connect(str(socket_dir(config) / "control"))
        s.sendall(verb.encode() + b"\n")
        if s.recv(80) != b"ok\n":
            raise ValueError("Unexpected storage relay response")


async def copy(source, target):
    try:
        while block := await source.read(256 * 1024):
            target.write(block)
            await target.drain()
    finally:
        target.close()


async def relay(vm, config):
    # NFS AUTH_SYS is available only to the host kernel or trusted CAP_NET_BIND_SERVICE holders.
    setting = Path("/proc/sys/net/ipv4/ip_unprivileged_port_start")
    if int(setting.read_text()) < 1024:
        raise ValueError("Privileged source ports required")
    directory = socket_dir(config)
    stopped = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stopped.set)

    async def client(reader, writer):
        host, port = writer.get_extra_info("peername")[:2]
        if host != "127.0.0.1" or port >= 1024 or int(setting.read_text()) < 1024:
            writer.close()
            return
        try:
            remote, sink = await asyncio.open_unix_connection(directory / "nfs")
        except OSError:
            writer.close()
            return
        await asyncio.gather(
            copy(reader, sink), copy(remote, writer), return_exceptions=True
        )

    async def command(reader, writer):
        line = await reader.readline()
        if line in (b"ping\n", b"stop\n"):
            writer.write(b"ok\n")
            await writer.drain()
            if line == b"stop\n":
                stopped.set()
        writer.close()

    server = await asyncio.start_server(client, "127.0.0.1", config["port"])
    control_path = directory / "control"
    control_path.unlink(missing_ok=True)
    (directory / "nfs").unlink(missing_ok=True)
    child = await asyncio.create_subprocess_exec(
        *ssh(vm),
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "StreamLocalBindUnlink=yes",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=3",
        "-N",
        "-L",
        f"{directory}/nfs:127.0.0.1:2049",
        ALIAS,
    )
    try:
        for _ in range(200):
            if child.returncode is not None:
                raise RuntimeError("SSH storage forwarding exited")
            if (directory / "nfs").is_socket():
                break
            await asyncio.sleep(0.1)
        else:
            raise TimeoutError("SSH storage forward did not become available")
        channel = await asyncio.start_unix_server(command, path=str(control_path))
        async with server, channel:
            tasks = [
                asyncio.create_task(child.wait()),
                asyncio.create_task(stopped.wait()),
            ]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in tasks:
                task.cancel()
    finally:
        server.close()
        if child.returncode is None:
            child.terminate()
        await child.wait()
        control_path.unlink(missing_ok=True)
        (directory / "nfs").unlink(missing_ok=True)


def start(vm, data):
    try:
        control(data, "ping")
        return
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    with (vm / "native/relay.log").open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, __file__, "--vm", str(vm), "relay"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
    for _ in range(200):
        if process.poll() is not None:
            raise RuntimeError("Storage relay exited; see its private log")
        try:
            control(data, "ping")
            return
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.15)
    raise TimeoutError("Native storage relay unavailable")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm", type=Path, required=True)
    parser.add_argument(
        "--packages-dir",
        type=Path,
        help="Previously downloaded pinned Ubuntu .deb files",
    )
    parser.add_argument(
        "action", choices=("prepare", "volumes", "start", "stop", "relay")
    )
    args = parser.parse_args()
    os.umask(0o077)
    vm = private_dir(args.vm.resolve(strict=True))
    private_dir(vm / "native")
    if args.action == "prepare":
        prepare(vm, args.packages_dir)
    config = read(vm)
    if args.action in ("prepare", "start"):
        start(vm, config)
    elif args.action == "relay":
        asyncio.run(relay(vm, config))
        return
    elif args.action == "volumes":
        volumes(config)
    elif args.action == "stop":
        manager.local_engine()
        for row in config["workspaces"].values():
            if manager.docker("ps", "-aq", "--filter", "volume=" + row["volume"]):
                raise ValueError(
                    "Recycle native workspace containers before stopping the relay"
                )
        control(config, "stop")
    print(
        json.dumps(
            {
                "action": args.action,
                "manifest": str(vm / "native/manifest.json"),
                "port": config["port"],
            }
        )
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Root bootstrap and unprivileged QEMU entry point for one disposable CI VM.

``doctor`` and ``render-config`` are read-only. Installation is deliberately an
explicit root action; this module never invokes sudo or the host Docker daemon.
Only the dedicated ``inet nexpoly_runner`` nftables table is managed here.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import copy
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid

CONFIG = Path("/etc/nexpoly-runner/config.toml")
INSTALL = Path("/usr/local/libexec/nexpoly-runner")
CONTROLLER_LOCK = Path("/run/nexpoly-runner-controller/controller.lock")
PACKAGE = Path(__file__).resolve().parent
VM_USER = "nexpoly-vm"
PROXY_USER = "nexpoly-proxy"
NFT_TABLE = "nexpoly_runner"
GIB = 1024 ** 3
STORAGE_BYTES = 200 * GIB
IO_BYTES = 100 * 1024 ** 2
REQUIRED_FILES = ("common.py", "github_api.py", "controller.py", "host.py", "template.py", "config.example.toml",
                  "install.sh", "proxy/squid.conf", "proxy/domains.txt", "apparmor/nexpoly-runner-vm",
                  "systemd/nexpoly-runner-vm@.service", "systemd/nexpoly-runner-proxy.service",
                  "systemd/nexpoly-runner-firewall.service", "systemd/nexpoly-runner-controller.service",
                  "systemd/nexpoly-runner-storage.service", "guest/bootstrap.py", "guest/build.py",
                  "guest/smoke.py", "guest/job-event.py", "guest/job-started.sh", "guest/job-completed.sh",
                  "guest/nexpoly-ci-bootstrap.service", "guest/software-lock.json")
PACKAGES = ("qemu-system-x86", "qemu-utils", "cloud-image-utils", "nftables", "squid", "apparmor-utils",
            "gpg", "gpgv", "ubuntu-cloudimage-keyring", "openssl")
SAFE_PATH = re.compile(r"/[A-Za-z0-9_./-]+\Z")


class HostError(RuntimeError):
    pass


def root_only() -> None:
    if os.geteuid() != 0:
        raise HostError("This operation requires an administrator sudo session; no changes made.")


def run(args: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, input=input_text, text=True, capture_output=True, check=False,
                            env={**os.environ, "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"})
    if check and result.returncode:
        raise HostError(f"{args[0]} failed ({result.returncode}): {result.stderr.strip()[:2000]}")
    return result


def read_config(path: Path) -> dict:
    with path.open("rb") as stream:
        config = tomllib.load(stream)
    root = Path(config["paths"]["root"])
    state = Path(config["paths"]["state"])
    for value in (str(root), str(state)):
        if not SAFE_PATH.fullmatch(value) or ".." in Path(value).parts:
            raise HostError("Paths must be absolute, normalized and contain only safe path characters")
    if root.parent != Path("/data/lzq") or state != root / "state":
        raise HostError("The storage root must be an immediate child of /data/lzq, with state=root/state")
    network = config["network"]
    if network["upstream_host"] != "127.0.0.1":
        raise HostError("The existing parent proxy must remain on 127.0.0.1")
    dns = ipaddress.ip_address(network["dns"])
    if dns.version != 4 or not dns.is_global:
        raise HostError("Configure one explicit, globally addressed IPv4 DNS resolver")
    ports = (network["upstream_port"], network["proxy_port"])
    if any(type(port) is not int or not 1024 <= port <= 65535 for port in ports) or ports[0] == ports[1]:
        raise HostError("Proxy ports must be distinct integer unprivileged ports")
    runner = config["runner"]
    for key, minimum, maximum in (("vcpus", 1, 8), ("memory_mib", 4096, 32768), ("disk_gib", 32, 160)):
        if type(runner[key]) is not int or not minimum <= runner[key] <= maximum:
            raise HostError(f"runner.{key} exceeds this host's reviewed resource budget")
    fixed = {("paths", "root"): "/data/lzq/github-runner", ("paths", "state"): "/data/lzq/github-runner/state",
             ("runner", "vcpus"): 8, ("runner", "memory_mib"): 32768, ("runner", "disk_gib"): 160,
             ("network", "upstream_port"): 27891, ("network", "proxy_port"): 27892, ("network", "dns"): "202.38.64.7"}
    for (section, key), expected in fixed.items():
        if config[section][key] != expected:
            raise HostError(f"{section}.{key} differs from the reviewed deployment contract")
    for key, minimum in (("state_min_free_gib", 40), ("host_min_free_gib", 100)):
        if type(config["policy"][key]) is not int or config["policy"][key] < minimum:
            raise HostError("Disk safety margins cannot be reduced")
    return config


def reject_symlinks(path: Path) -> None:
    """Never follow a substituted data/config parent when running as root."""
    for item in (path, *path.parents):
        if item.is_symlink():
            raise HostError(f"Symlink is not allowed in managed path: {item}")


def atomic_write(path: Path, content: str | bytes, mode: int = 0o644) -> None:
    reject_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content.encode() if isinstance(content, str) else content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def host_addresses() -> list[str]:
    result = run(["ip", "-j", "address", "show"])
    addresses = {"127.0.0.1/32", "114.214.255.0/24"}
    for interface in json.loads(result.stdout):
        for entry in interface.get("addr_info", []):
            address = ipaddress.ip_address(entry["local"])
            # Include the entire connected subnet, including globally addressed
            # LANs. IPv6 is blocked for QEMU at the UID firewall as well.
            addresses.add(str(ipaddress.ip_network(f"{address}/{entry['prefixlen']}", strict=False)))
    return sorted(addresses)


def render_nft(config: dict, vm_uid: int, proxy_uid: int) -> str:
    if any(type(uid) is not int or uid <= 0 for uid in (vm_uid, proxy_uid)) or vm_uid == proxy_uid:
        raise HostError("Dedicated non-root, distinct QEMU and proxy UIDs are required")
    n = config["network"]
    return f"""table inet {NFT_TABLE} {{
  chain output {{
    type filter hook output priority 0; policy accept;
    meta skuid {vm_uid} jump vm_egress
    meta skuid {proxy_uid} jump proxy_egress
  }}
  chain vm_egress {{
    meta nfproto ipv6 counter drop
    ip daddr 127.0.0.1 tcp dport {n['proxy_port']} counter accept
    ip daddr {n['dns']} udp dport 53 counter accept
    ip daddr {n['dns']} tcp dport 53 counter accept
    counter drop
  }}
  chain proxy_egress {{
    meta nfproto ipv6 counter drop
    ct state established,related counter accept
    ip daddr 127.0.0.1 tcp dport {n['upstream_port']} counter accept
    ip daddr {n['dns']} udp dport 53 counter accept
    ip daddr {n['dns']} tcp dport 53 counter accept
    counter drop
  }}
}}
"""


def render_assets(config: dict, vm_uid: int, proxy_uid: int, addresses: list[str], package: Path = PACKAGE) -> dict[str, str]:
    """Return install destination -> contents; never write or invoke services."""
    substitutions = {
        "@ROOT@": config["paths"]["root"],
        "@STATE@": config["paths"]["state"],
        "@CPU_QUOTA@": str(config["runner"]["vcpus"] * 100),
        "@PROXY_PORT@": str(config["network"]["proxy_port"]),
        "@UPSTREAM_PORT@": str(config["network"]["upstream_port"]),
        "@DNS@": config["network"]["dns"],
        "@HOST_ADDRESSES@": " ".join(str(ipaddress.ip_network(value, strict=False)) for value in addresses),
    }
    if not addresses:
        raise HostError("Host address deny set must not be empty")
    rendered = {}
    for path in sorted((package / "systemd").glob("*.service")):
        rendered[f"/etc/systemd/system/{path.name}"] = path.read_text()
    rendered["/etc/apparmor.d/nexpoly-runner-vm"] = (package / "apparmor/nexpoly-runner-vm").read_text()
    rendered["/etc/nexpoly-runner/squid.conf"] = (package / "proxy/squid.conf").read_text()
    rendered["/etc/nexpoly-runner/proxy-domains.txt"] = (package / "proxy/domains.txt").read_text()
    for path, content in rendered.items():
        for key, value in substitutions.items():
            content = content.replace(key, value)
        if re.search(r"@[A-Z_]+@", content):
            raise HostError(f"Unresolved asset placeholder: {path}")
        rendered[path] = content
    rendered["/etc/nexpoly-runner/egress.nft"] = render_nft(config, vm_uid, proxy_uid)
    rendered["/usr/local/bin/nexpoly-runnerctl"] = (
        '#!/bin/sh\nexec /usr/bin/python3 -I -B /usr/local/libexec/nexpoly-runner/controller.py "$@"\n'
    )
    rendered["/etc/systemd/journald@nexpoly-runner.conf"] = (
        "[Journal]\nStorage=persistent\nSystemMaxUse=1G\nSystemMaxFileSize=16M\n"
        "RuntimeMaxUse=64M\nMaxRetentionSec=7day\nRateLimitIntervalSec=30s\nRateLimitBurst=1000\n"
        "ForwardToSyslog=no\nForwardToKMsg=no\nForwardToConsole=no\n"
    )
    rendered["/etc/systemd/system/systemd-journald@nexpoly-runner.service.d/storage.conf"] = (
        f"[Unit]\nRequiresMountsFor={config['paths']['state']}\n\n[Service]\nLogsDirectory=\n"
        f"BindPaths={config['paths']['state']}/logs/journal:/var/log/journal\nLimitCORE=0\n"
    )
    # Clean existing build logs even if no future template update is requested.
    # 'e' never creates a directory when the dedicated filesystem is unmounted.
    rendered["/etc/tmpfiles.d/nexpoly-runner.conf"] = (
        f"e {config['paths']['state']}/logs/template-builds - - - m:7d\n"
    )
    return rendered


def firewall_apply(config: dict) -> None:
    root_only()
    content = render_nft(config, pwd.getpwnam(VM_USER).pw_uid, pwd.getpwnam(PROXY_USER).pw_uid)
    exists = run(["nft", "list", "table", "inet", NFT_TABLE], check=False).returncode == 0
    # Deletion and replacement are one nft transaction. Never flush another
    # table or transiently leave a live QEMU without its outbound restriction.
    transaction = (f"delete table inet {NFT_TABLE}\n" if exists else "") + content
    run(["nft", "--check", "--file", "-"], input_text=transaction)
    run(["nft", "--file", "-"], input_text=transaction)


def normalize_firewall_expression(expression: dict) -> dict:
    """Normalize only documented numeric/symbolic forms of reviewed values."""
    value = copy.deepcopy(expression)
    if set(value) != {"match"} or not isinstance(value["match"], dict):
        return value
    match = value["match"]
    if (match.get("op") == "==" and match.get("left") == {"meta": {"key": "nfproto"}}
            and type(match.get("right")) is int and match["right"] == 10):
        match["right"] = "ipv6"  # Linux NFPROTO_IPV6; nft --numeric prints 10.
    if match.get("op") == "in" and match.get("left") == {"ct": {"key": "state"}}:
        right = match.get("right")
        if isinstance(right, dict) and set(right) == {"set"}:
            right = right["set"]
        if isinstance(right, list):
            names = []
            for state in right:
                if type(state) is int and state in (2, 4):
                    names.append({2: "established", 4: "related"}[state])
                elif isinstance(state, str) and state in ("established", "related"):
                    names.append(state)
                else:
                    return value  # Unknown states must remain a mismatch.
            if len(names) == 2 and set(names) == {"established", "related"}:
                match["right"] = {"set": ["established", "related"]}
    return value


def firewall_report(config: dict) -> dict:
    """Compare policy semantics, retaining diagnostics for rejected live rules."""
    def fail(reason):
        return {"ok": False, "reason": reason}
    try:
        result = run(["nft", "--numeric", "--json", "list", "table", "inet", NFT_TABLE], check=False)
        if result.returncode:
            return fail("Cannot read the dedicated nft table; administrator access is required")
        active = json.loads(result.stdout)["nftables"]
        if not isinstance(active, list):
            return fail("Invalid nft JSON object list")
        for item in active:
            if not isinstance(item, dict) or len(item) != 1 or not set(item) <= {"metainfo", "table", "chain", "rule"}:
                return fail("Unexpected nft object outside the reviewed table/chain/rule policy")
            kind, obj = next(iter(item.items()))
            if not isinstance(obj, dict):
                return fail("Invalid nft object")
            if kind != "metainfo":
                if obj.get("family") != "inet":
                    return fail("Dedicated nft object has an unexpected family")
                table_name = obj.get("name" if kind == "table" else "table")
                if table_name != NFT_TABLE:
                    return fail("Dedicated nft object belongs to an unexpected table")
        tables = [item["table"] for item in active if "table" in item]
        if len(tables) != 1 or tables[0].get("flags", []) != []:
            return fail("Expected one active dedicated nft table without flags")
        rules = [item["rule"] for item in active if "rule" in item]
        chain_records = [item["chain"] for item in active if "chain" in item]
        chains = {record["name"]: record for record in chain_records}
        if len(chain_records) != len(chains):
            return fail("Duplicate nft chain records")
        if set(chains) != {"output", "vm_egress", "proxy_egress"}:
            return fail("Dedicated nft chain names differ from the approved policy")
        if chains["output"].get("hook") != "output" or chains["output"].get("policy") != "accept" or chains["output"].get("type") != "filter" or chains["output"].get("prio") != 0:
            return fail("Output chain hook, type, priority or policy differs")
        if any(key in chains["output"] for key in ("dev", "devices")):
            return fail("Output chain must not be limited to a network device")
        if any(chains[name].get("flags", []) != [] for name in chains):
            return fail("Unexpected nft chain flags")
        if any(any(key in chains[name] for key in ("hook", "type", "policy", "prio", "dev", "devices"))
               for name in ("vm_egress", "proxy_egress")):
            return fail("Egress chains must be regular, unhooked chains")
        # Parse nft's canonical JSON for the desired rules without modifying the
        # active ruleset. --check + --echo does not expose canonical rule JSON on
        # every Ubuntu nft version, so compare explicit expressions instead.
        vm_uid, proxy_uid = pwd.getpwnam(VM_USER).pw_uid, pwd.getpwnam(PROXY_USER).pw_uid
        n = config["network"]
        def match(left, right):
            return {"match": {"op": "==", "left": left, "right": right}}
        def meta(key): return {"meta": {"key": key}}
        def payload(protocol, field): return {"payload": {"protocol": protocol, "field": field}}
        def destination(address, protocol, port):
            return [match(payload("ip", "daddr"), address), match(payload(protocol, "dport"), port), {"accept": None}]
        expected = {
            "output": [[match(meta("skuid"), vm_uid), {"jump": {"target": "vm_egress"}}],
                       [match(meta("skuid"), proxy_uid), {"jump": {"target": "proxy_egress"}}]],
            "vm_egress": [[match(meta("nfproto"), "ipv6"), {"drop": None}],
                          destination("127.0.0.1", "tcp", n["proxy_port"]),
                          destination(n["dns"], "udp", 53), destination(n["dns"], "tcp", 53), [{"drop": None}]],
            "proxy_egress": [[match(meta("nfproto"), "ipv6"), {"drop": None}],
                             [{"match": {"op": "in", "left": {"ct": {"key": "state"}}, "right": {"set": ["established", "related"]}}}, {"accept": None}],
                             destination("127.0.0.1", "tcp", n["upstream_port"]),
                             destination(n["dns"], "udp", 53), destination(n["dns"], "tcp", 53), [{"drop": None}]],
        }
        actual = {chain: [] for chain in expected}
        for rule in rules:
            expressions = []
            if rule.get("chain") not in actual or not isinstance(rule.get("expr"), list):
                return fail("Unexpected rule chain or expression list")
            for expression in rule["expr"]:
                if not isinstance(expression, dict):
                    return fail("Invalid nft rule expression")
                if "counter" in expression:
                    counter = expression["counter"]
                    if (set(expression) != {"counter"} or not isinstance(counter, dict)
                            or not set(counter) <= {"packets", "bytes"}
                            or any(type(v) is not int or v < 0 for v in counter.values())):
                        return fail("Unexpected counter expression")
                    continue
                expressions.append(normalize_firewall_expression(expression))
            actual[rule["chain"]].append(expressions)
        for chain in expected:
            if len(actual[chain]) != len(expected[chain]):
                return fail(f"{chain}: rule count differs from the approved policy")
            for index, (observed, approved) in enumerate(zip(actual[chain], expected[chain]), 1):
                if observed != approved:
                    return fail(f"{chain}: rule {index} differs from the approved expression sequence")
        return {"ok": True, "reason": "Dedicated UID egress policy matches"}
    except (OSError, KeyError, ValueError, TypeError):
        return fail("Cannot parse or inspect the dedicated nft policy")


def firewall_check(config: dict) -> bool:
    return firewall_report(config)["ok"]


def validate_instance(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise HostError("Instance ID must be a 32-character lowercase hexadecimal UUID")
    return value


def qemu_command(config: dict, instance: str, *, check_files: bool = True) -> list[str]:
    instance = validate_instance(instance)
    folder = Path(config["paths"]["state"]) / "instances" / instance
    runtime = Path("/run") / f"nexpoly-runner-vm-{instance}"
    if check_files:
        for path in (folder / "disk.qcow2", folder / "seed.iso", runtime):
            reject_symlinks(path)
            if not path.exists():
                raise HostError(f"Missing VM input: {path}")
        for path in (folder / "disk.qcow2", folder / "seed.iso"):
            if not path.is_file():
                raise HostError(f"VM input is not a regular file: {path}")
        if os.geteuid() != pwd.getpwnam(VM_USER).pw_uid:
            raise HostError("QEMU must run under its dedicated unprivileged systemd account")
    args = ["/usr/bin/qemu-system-x86_64", "-no-user-config", "-nodefaults",
            "-machine", "q35,accel=kvm", "-cpu", "host", "-smp", str(config["runner"]["vcpus"]),
            "-m", str(config["runner"]["memory_mib"]), "-display", "none", "-monitor", "none",
            "-no-reboot", "-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
            "-drive", f"file={folder}/disk.qcow2,if=virtio,format=qcow2,cache=none,discard=unmap,bps_rd={IO_BYTES},bps_wr={IO_BYTES}",
            "-drive", f"file={folder}/seed.iso,if=virtio,format=raw,readonly=on",
            "-netdev", "user,id=ci,ipv6=off", "-device", "virtio-net-pci,netdev=ci",
            "-qmp", f"unix:{runtime}/qmp.sock,server=on,wait=off",
            "-chardev", f"socket,id=console,path={runtime}/serial.sock,server=on,wait=off",
            "-serial", "chardev:console", "-device", "virtio-serial-pci"]
    for name in ("bootstrap", "events"):
        args.extend(["-chardev", f"socket,id={name},path={runtime}/{name}.sock,server=on,wait=off",
                     "-device", f"virtserialport,chardev={name},name=org.nexpoly.runner.{name}"])
    return args


def open_directory_no_follow(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise HostError("Directory must be an absolute normalized path")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        return directory
    except BaseException:
        os.close(directory)
        raise


def snapshot_package(source: Path, target: Path) -> None:
    """Copy a complete package into private root staging before installing it."""
    def copy_directory(directory: int, destination: Path) -> None:
        destination.mkdir(mode=0o755)
        for name in sorted(os.listdir(directory)):
            if name == "__pycache__" or name.endswith(".pyc"):
                continue
            if Path(name).suffix in {".pem", ".key"}:
                raise HostError("Credentials must not be included in the staged control package")
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    copy_directory(child, destination / name)
                finally:
                    os.close(child)
            elif stat.S_ISREG(metadata.st_mode):
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(descriptor, "rb") as stream:
                    actual = os.fstat(stream.fileno())
                    if not stat.S_ISREG(actual.st_mode) or actual.st_size > 8 * 1024 ** 2:
                        raise HostError("Staged package contains a nonregular or oversized file")
                    content = stream.read(8 * 1024 ** 2 + 1)
                    if len(content) > 8 * 1024 ** 2:
                        raise HostError("Staged package file changed or is oversized")
                (destination / name).write_bytes(content)
            else:
                raise HostError(f"Unsupported staged package entry: {name}")
    directory = open_directory_no_follow(source)
    try:
        copy_directory(directory, target)
    finally:
        os.close(directory)
    for relative in REQUIRED_FILES:
        if not (target / relative).is_file():
            raise HostError(f"Incomplete staged package: {relative}")
    if not (target / "guest").is_dir() or not list((target / "guest").iterdir()):
        raise HostError("Incomplete staged package: guest/")
    # Recheck the private snapshot, then parse every Python module without
    # importing code or producing bytecode in the staged source.
    for path in target.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise HostError("Unsafe entry appeared while taking the package snapshot")
        os.chmod(path, 0o755 if path.is_dir() or path.suffix == ".sh" else 0o644)
        if path.suffix == ".py":
            ast.parse(path.read_bytes(), filename=str(path))


def mount_unit(config: dict) -> tuple[str, str]:
    root, state = Path(config["paths"]["root"]), Path(config["paths"]["state"])
    name = run(["systemd-escape", "--path", "--suffix=mount", str(state)]).stdout.strip()
    return name, f"""[Unit]
Description=NexPoly bounded 200 GiB CI storage
RequiresMountsFor={root}
Before=nexpoly-runner-proxy.service

[Mount]
What={root}/storage.ext4
Where={state}
Type=ext4
Options=loop,nodev,nosuid,noexec,nodiscard,X-fstrim.notrim
TimeoutSec=60

[Install]
WantedBy=multi-user.target
"""


def ensure_storage(config: dict) -> None:
    root, state = Path(config["paths"]["root"]), Path(config["paths"]["state"])
    image = root / "storage.ext4"
    for path in (root, state, image):
        reject_symlinks(path)
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    state.mkdir(mode=0o755, exist_ok=True)
    if os.path.ismount(state):
        if not storage_check(config, require_allocation=False, require_guard=False):
            raise HostError("Mounted state filesystem has an unexpected source, ownership, size or mount flags; refusing to change it")
        # A valid loop filesystem can lose reservation through inode-table
        # zeroing or FITRIM. Repair only after proving its complete identity.
        protect_storage(config)
    # /data/lzq is owned by the trusted server administrator. The runner's own
    # subtree is root-owned; the guest UID never gains directory replacement.
    for directory in (root, state):
        os.chown(directory, 0, 0)
        os.chmod(directory, 0o755)
    if not image.exists():
        minimum = int(config["policy"]["host_min_free_gib"]) * GIB
        if shutil.disk_usage(root).free < STORAGE_BYTES + minimum:
            raise HostError("Not enough /data capacity to reserve 200 GiB and retain the host free-space floor")
        if any(state.iterdir()):
            raise HostError("Refusing to hide a nonempty existing state directory with a new filesystem")
        # Create under a private name; a failed mkfs never leaves a valid-looking
        # production backing file for a later installer to reuse.
        temporary = root / f".storage-{uuid.uuid4()}.ext4"
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                os.posix_fallocate(stream.fileno(), 0, STORAGE_BYTES)
            run(["mkfs.ext4", "-q", "-m", "0", "-L", "nexpoly-ci", "-E",
                 "nodiscard,lazy_itable_init=0,lazy_journal_init=0", str(temporary)])
            reserve_storage(temporary, config)
            os.rename(temporary, image)
        finally:
            if temporary.exists():
                temporary.unlink()
    else:
        metadata = image.stat()
        if not image.is_file() or metadata.st_size != STORAGE_BYTES or metadata.st_uid != 0 or metadata.st_mode & 0o077:
            raise HostError("Existing storage.ext4 is not the expected root-owned 200 GiB backing file")
        if run(["blkid", "-s", "TYPE", "-o", "value", str(image)]).stdout.strip() != "ext4":
            raise HostError("Existing backing image is not ext4; refusing to reformat it")
    unit_name, content = mount_unit(config)
    atomic_write(Path("/etc/systemd/system") / unit_name, content)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", unit_name])
    protect_storage(config)
    if not storage_check(config):
        raise HostError("Dedicated state filesystem failed source/options validation")
    vm = pwd.getpwnam(VM_USER)
    for name in ("templates", "instances", "logs", "controller"):
        path = state / name
        path.mkdir(exist_ok=True, mode=0o750)
        os.chmod(path, 0o750 if name in {"templates", "instances"} else 0o700)
        os.chown(path, 0, vm.pw_gid if name in {"templates", "instances"} else 0)
    (state / "logs/journal").mkdir(mode=0o700, exist_ok=True)


def storage_check(config: dict, *, require_allocation: bool = True, require_guard: bool = True) -> bool:
    root, state = Path(config["paths"]["root"]), Path(config["paths"]["state"])
    image = root / "storage.ext4"
    try:
        reject_symlinks(image)
        reject_symlinks(state)
        metadata = image.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != STORAGE_BYTES or metadata.st_uid != 0 or metadata.st_mode & 0o077:
            return False
        if require_allocation and metadata.st_blocks * 512 < STORAGE_BYTES:
            return False
        mounted = run(["findmnt", "--json", "--output", "TARGET,SOURCE,FSTYPE,OPTIONS", "--mountpoint", str(state)], check=False)
        if mounted.returncode:
            return False
        filesystems = json.loads(mounted.stdout)["filesystems"]
        if len(filesystems) != 1:
            return False
        filesystem = filesystems[0]
        source = filesystem["source"]
        if filesystem["target"] != str(state) or filesystem["fstype"] != "ext4" or not re.fullmatch(r"/dev/loop[0-9]+", source):
            return False
        if not {"rw", "nodev", "nosuid", "noexec"} <= set(filesystem["options"].split(",")):
            return False
        loop = Path("/sys/block") / Path(source).name / "loop"
        if Path((loop / "backing_file").read_text().strip()) != image:
            return False
        if (loop / "offset").read_text().strip() != "0" or (loop / "sizelimit").read_text().strip() != "0":
            return False
        if require_guard and (loop.parent / "queue/discard_max_bytes").read_text().strip() != "0":
            return False
        return os.stat(state).st_dev != os.stat(root).st_dev
    except (OSError, HostError, KeyError, ValueError, TypeError):
        return False


def reserve_storage(image: Path, config: dict) -> None:
    """Restore reservation without overwriting file bytes, truncating or mkfs."""
    root_only()
    reject_symlinks(image)
    descriptor = os.open(image, os.O_RDWR | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077
                or info.st_size != STORAGE_BYTES or info.st_nlink != 1):
            raise HostError("Refusing allocation repair on an unexpected storage file")
        missing = max(0, STORAGE_BYTES - info.st_blocks * 512)
        if not missing:
            return
        floor = config["policy"]["host_min_free_gib"] * GIB
        if shutil.disk_usage(image.parent).free < missing + floor:
            raise HostError(f"Storage reservation is short by {missing} bytes; restoring it would cross the host free-space floor")
        os.posix_fallocate(descriptor, 0, STORAGE_BYTES)
        os.fsync(descriptor)
        if os.fstat(descriptor).st_blocks * 512 < STORAGE_BYTES:
            raise HostError("Could not restore the full 200 GiB storage reservation")
        if shutil.disk_usage(image.parent).free < floor:
            raise HostError("Host free-space floor was crossed by concurrent disk use during reservation repair")
    finally:
        os.close(descriptor)


def protect_storage(config: dict) -> None:
    """Guard only the verified runner loop device, including after every boot."""
    root_only()
    if not storage_check(config, require_allocation=False, require_guard=False):
        raise HostError("Cannot protect storage: mounted filesystem identity does not match the runner backing file")
    state = Path(config["paths"]["state"])
    source = run(["findmnt", "--noheadings", "--output", "SOURCE", "--mountpoint", str(state)]).stdout.strip()
    if not re.fullmatch(r"/dev/loop[0-9]+", source):
        raise HostError("Expected exactly one dedicated loop device")
    loop = Path("/sys/block") / Path(source).name
    image = Path(config["paths"]["root"]) / "storage.ext4"
    if Path((loop / "loop/backing_file").read_text().strip()) != image:
        raise HostError("Loop backing identity changed before reservation protection")
    # Linux loop maps discard and ordinary WRITE_ZEROES to hole punching.
    # Setting this queue limit to zero rejects those operations; zeroing falls
    # back to normal writes. Do not alter the host NVMe queue or global fstrim.
    guard = loop / "queue/discard_max_bytes"
    guard.write_text("0\n")
    if guard.read_text().strip() != "0":
        raise HostError("Cannot disable hole-punching on the dedicated loop device")
    reserve_storage(image, config)
    if not storage_check(config):
        raise HostError("Dedicated storage reservation or queue guard failed verification")


def apparmor_check() -> bool:
    try:
        profiles = Path("/sys/kernel/security/apparmor/profiles").read_text().splitlines()
        return "nexpoly-runner-vm (enforce)" in profiles
    except OSError:
        return False


def install_packages() -> None:
    installed = run(["dpkg-query", "-W", "-f=${Status}", "squid"], check=False)
    squid_exists = installed.returncode == 0 and "install ok installed" in installed.stdout
    if not squid_exists:
        # Mask before apt's postinst can auto-start the distribution's default
        # listener. This is unrelated to our separately named proxy unit.
        run(["systemctl", "mask", "squid.service"])
    environment = {**os.environ, "DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "l"}
    # sudo usually strips proxy environment variables. Bootstrap uses the
    # existing reviewed parent: the restricted CI proxy is not installed yet.
    apt = ["apt-get", "-o", "Acquire::http::Proxy=http://127.0.0.1:27891",
           "-o", "Acquire::https::Proxy=http://127.0.0.1:27891"]
    for command in ([*apt, "update"], [*apt, "install", "-y", "--no-install-recommends", *PACKAGES]):
        completed = subprocess.run(command, env=environment, check=False)
        if completed.returncode:
            raise HostError("Package installation failed; runner activation was not attempted")


def ensure_users() -> None:
    for user in (VM_USER, PROXY_USER):
        try:
            entry = pwd.getpwnam(user)
        except KeyError:
            run(["useradd", "--system", "--user-group", "--no-create-home", "--home-dir", "/nonexistent",
                 "--shell", "/usr/sbin/nologin", user])
            entry = pwd.getpwnam(user)
        if entry.pw_uid == 0 or entry.pw_shell not in {"/usr/sbin/nologin", "/sbin/nologin", "/bin/false"}:
            raise HostError(f"Existing account {user} is not a locked service account")
        groups = set(run(["id", "-nG", user]).stdout.split())
        if groups - {user, "kvm" if user == VM_USER else user}:
            raise HostError(f"Existing {user} has unexpected supplementary groups")
    run(["usermod", "-a", "-G", "kvm", VM_USER])


def activate_proxy() -> None:
    """Apply config and unit changes once, for first installs and reinstalls."""
    unit = "nexpoly-runner-proxy.service"
    # Installation already refuses live controller/VM instances. Restarting
    # here is safe and also applies systemd unit changes. Do not enable --now
    # followed immediately by a signal-based reload of a newly starting Squid.
    run(["systemctl", "enable", unit])
    run(["systemctl", "restart", unit])
    # Type=simple reports active before Squid binds its listener. Wait only for
    # that local startup step; the controller separately verifies real HTTPS
    # through the parent proxy and must still fail on an upstream outage.
    deadline = time.monotonic() + 30
    while True:
        run(["systemctl", "is-active", "--quiet", unit])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HostError("Dedicated proxy listener 127.0.0.1:27892 did not become ready within 30 seconds")
        try:
            # Both address and port are fixed by the reviewed config contract.
            with socket.create_connection(("127.0.0.1", 27892), timeout=min(0.5, remaining)):
                return
        except (ConnectionRefusedError, TimeoutError):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HostError("Dedicated proxy listener 127.0.0.1:27892 did not become ready within 30 seconds") from None
            time.sleep(min(0.25, remaining))
        except OSError:
            raise HostError("Cannot verify dedicated proxy listener 127.0.0.1:27892 readiness") from None


def install(source: Path, config_path: Path, *, with_packages: bool = False) -> None:
    root_only()
    # Template downloads precede the first VM, and serve may be between VMs.
    # Hold the same lock for the entire installation so neither can race a
    # proxy restart, storage repair, package install or executable replacement.
    with installation_lock():
        _install(source, config_path, with_packages=with_packages)


@contextlib.contextmanager
def installation_lock():
    reject_symlinks(CONTROLLER_LOCK.parent)
    CONTROLLER_LOCK.parent.mkdir(mode=0o700, exist_ok=True)
    directory = os.open(CONTROLLER_LOCK.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        info = os.fstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
            raise HostError("Runner lock directory must be root-owned with mode 0700")
        descriptor = os.open(CONTROLLER_LOCK.name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise HostError("Runner lock must be a root-owned regular file with mode 0600 and one link")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise HostError("A controller, template update or installer holds the runner lock. "
                            "Drain and stop the controller, or wait for/cancel the template update "
                            "before reinstalling; no installation changes made.") from error
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def _install(source: Path, config_path: Path, *, with_packages: bool = False) -> None:
    source = source.absolute()
    reject_symlinks(CONFIG)
    # Reinstalling from a fresh staging package must not reset application IDs
    # or policy limits to example defaults. Operators update the installed file
    # with sudoedit; only the first installation imports --config.
    if CONFIG.exists():
        metadata = CONFIG.stat()
        if metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise HostError("Installed configuration must be root-owned and not group/world writable")
        config_path = CONFIG
    config = read_config(config_path)
    if run(["uname", "-m"]).stdout.strip() != "x86_64":
        raise HostError("This implementation requires the reviewed x86_64 host")
    os_release = Path("/etc/os-release").read_text()
    if 'ID=ubuntu' not in os_release or 'VERSION_ID="24.04"' not in os_release:
        raise HostError("This installer is qualified for Ubuntu 24.04 only")
    active = run(["systemctl", "list-units", "--no-legend", "--state=active,activating,deactivating", "nexpoly-runner-vm@*.service"], check=False)
    if active.stdout.strip() or run(["systemctl", "is-active", "--quiet", "nexpoly-runner-controller.service"], check=False).returncode == 0:
        raise HostError("Drain and stop the controller and all VM instances before reinstalling")
    # Nothing from a user-writable staging directory is installed as an active
    # root entry point before this complete, private snapshot has been verified.
    INSTALL.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".nexpoly-runner-stage-", dir=INSTALL.parent) as staging:
        snapshot = Path(staging) / "package"
        snapshot_package(source, snapshot)
        if with_packages:
            install_packages()
        required = ("qemu-system-x86_64", "qemu-img", "cloud-localds", "nft", "squid", "apparmor_parser", "mkfs.ext4", "systemd-escape", "gpg", "gpgv", "openssl")
        missing = [program for program in required if not shutil.which(program)]
        if missing:
            raise HostError("Missing host packages; rerun with --install-packages: " + ", ".join(missing))
        if not Path("/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg").is_file():
            raise HostError("Missing Ubuntu cloud-image signing keyring; install ubuntu-cloudimage-keyring")
        ensure_users()
        assets = render_assets(config, pwd.getpwnam(VM_USER).pw_uid, pwd.getpwnam(PROXY_USER).pw_uid, host_addresses(), snapshot)
        # Keep an existing valid configuration byte-for-byte, including operator
        # application IDs. This file never contains the application private key.
        if config_path != CONFIG:
            atomic_write(CONFIG, config_path.read_bytes())
        else:
            reject_symlinks(CONFIG)
        ensure_storage(config)
        backup = INSTALL.with_name("nexpoly-runner.previous")
        if backup.exists():
            reject_symlinks(backup)
            shutil.rmtree(backup)
        reject_symlinks(INSTALL)
        if INSTALL.exists():
            os.rename(INSTALL, backup)
        os.rename(snapshot, INSTALL)
        os.chmod(INSTALL, 0o755)
        for name, content in assets.items():
            atomic_write(Path(name), content, 0o755 if name.startswith("/usr/local/bin/") else 0o644)
        run(["apparmor_parser", "-r", "/etc/apparmor.d/nexpoly-runner-vm"])
        run(["aa-enforce", "/etc/apparmor.d/nexpoly-runner-vm"])
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "enable", "--now", "nexpoly-runner-storage.service"])
        run(["systemctl", "enable", "--now", "nexpoly-runner-firewall.service"])
        firewall_apply(config)
        activate_proxy()
    print("Host components installed. Installation did not enable or start the controller; complete template and App acceptance before activation.")


def doctor(config: dict) -> dict:
    checks = []
    def record(name: str, ok: bool, detail: str):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
    for program in ("qemu-system-x86_64", "qemu-img", "cloud-localds", "nft", "squid", "apparmor_parser"):
        record(program, shutil.which(program) is not None, shutil.which(program) or "administrator package installation required")
    record("kvm", os.access("/dev/kvm", os.R_OK | os.W_OK), "/dev/kvm access is required for nexpoly-vm, not for the interactive user")
    for user in (VM_USER, PROXY_USER):
        try:
            entry = pwd.getpwnam(user)
            record(user, entry.pw_uid != 0, f"uid={entry.pw_uid}")
        except KeyError:
            record(user, False, "dedicated account not installed")
    root = Path(config["paths"]["root"])
    parent = root if root.exists() else root.parent
    usage = shutil.disk_usage(parent)
    existing = (root / "storage.ext4").is_file()
    needed = (0 if existing else STORAGE_BYTES) + config["policy"]["host_min_free_gib"] * GIB
    record("host_disk", usage.free >= needed, f"{usage.free // GIB} GiB free; need {needed // GIB} GiB")
    record("dedicated_storage", existing, "fixed 200 GiB backing file; a missing file is not created by doctor")
    record("storage_mount", storage_check(config), "expected loop backing, mount flags, ownership and size")
    record("apparmor", apparmor_check(), "nexpoly-runner-vm must be loaded in enforce mode")
    try:
        record("firewall", firewall_check(config), "exact dedicated nft UID egress rules; root is required to inspect")
    except (OSError, KeyError):
        record("firewall", False, "nft or dedicated service accounts not installed")
    record("root", os.geteuid() == 0, "read-only diagnosis; administrator access is required for installation")
    try:
        with socket.create_connection((config["network"]["upstream_host"], config["network"]["upstream_port"]), timeout=2):
            record("parent_proxy", True, "loopback TCP listener reachable; end-to-end connectivity still needs acceptance")
    except OSError as error:
        record("parent_proxy", False, type(error).__name__)
    return {"non_mutating": True, "ready": all(check["ok"] for check in checks), "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for command in ("doctor", "render-config", "firewall-apply", "firewall-check", "storage-check", "storage-protect", "run-vm", "install"):
        sub = subs.add_parser(command)
        sub.add_argument("--config", type=Path, default=CONFIG if CONFIG.exists() else PACKAGE / "config.example.toml")
        if command == "doctor":
            sub.add_argument("--non-mutating", action="store_true", help="doctor is always read-only")
        if command == "render-config":
            sub.add_argument("--vm-uid", type=int, default=64020, help="preview placeholder, resolved during installation")
            sub.add_argument("--proxy-uid", type=int, default=64021, help="preview placeholder, resolved during installation")
        if command == "run-vm":
            sub.add_argument("--instance", required=True)
        if command == "install":
            sub.add_argument("--source", type=Path, required=True)
            sub.add_argument("--install-packages", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = read_config(args.config)
        if args.command == "doctor":
            report = doctor(config)
            print(json.dumps(report, indent=2))
            return 0 if report["ready"] else 1
        if args.command == "render-config":
            print(json.dumps(render_assets(config, args.vm_uid, args.proxy_uid, host_addresses()), indent=2))
        elif args.command == "firewall-apply":
            firewall_apply(config)
        elif args.command == "firewall-check":
            report = firewall_report(config)
            print(json.dumps(report))
            return 0 if report["ok"] else 1
        elif args.command == "storage-check":
            return 0 if storage_check(config) else 1
        elif args.command == "storage-protect":
            protect_storage(config)
        elif args.command == "run-vm":
            command = qemu_command(config, args.instance)
            os.execv(command[0], command)
        elif args.command == "install":
            install(args.source, args.config, with_packages=args.install_packages)
    except (HostError, OSError, KeyError, ValueError, SyntaxError) as error:
        print(f"nexpoly-runner host: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

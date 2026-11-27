"""Connect the current-host quota candidate, inspect its first restore, or check a quiesced web backup."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import time
from uuid import uuid4

import start_openscience_m5_web_vm as launcher
from verify_openscience_m5_workspace_web import provision_code
from openscience_m5_backup import available_bytes, offline_registry, require_space
import openscience_m5_native_storage as native
from openscience_m5_storage_ssh import StorageGuest
from openscience_per_user_dev import docker, write_atomic


def inspect(vm):
    config = json.loads((vm / "vm.json").read_bytes())
    if "backup_dir" not in config:
        raise ValueError("Use the independent deploy-m6 storage VM on two physical disks")
    layout = launcher.checked_layout(vm, config)
    guest = StorageGuest(vm / "ssh.conf")
    result = json.loads(guest.run("cat", "/var/lib/nexpoly-candidate-storage.json", capture_output=True).stdout)
    if result["status"] != "passed" or result["format"] != "nexpoly-current-host-storage-v1":
        raise ValueError("Incomplete guest verification")
    result.update(host_disks=config["host_disks"], storage_vm_base_sha256=launcher.offline.image_lock()["sha256"],
                  obtained_at=datetime.now(timezone.utc).isoformat(), physical_hosts=1,
                  host_free_bytes={"primary": available_bytes(vm), "backup": available_bytes(config["backup_dir"])},
                  virtual_disk_bytes={serial: size for serial, (path, size) in layout.items()})
    return result, guest


def connect(vm, guest, packages):
    if (vm / "native/manifest.json").exists():
        unused_volumes(native.read(vm), must_exist=False)
    provision_code(guest)
    native.private_dir(vm / "native")
    info = native.prepare(vm, packages)
    native.volumes(info)
    native.start(vm, info)
    for phase in ("src", "dst"):
        bindings = []
        for role in "ab":
            row = info["workspaces"][phase + "-" + role]
            bindings.append({key: row[key] for key in ("environment", "user_id", "workspace", "uid", "generation",
                                                        "mount", "filesystem_uuid")})
        write_atomic(vm / "native" / (phase + "-bindings.json"), json.dumps(bindings).encode(), 0o600)


def unused_volumes(info, *, must_exist):
    for row in info["workspaces"].values():
        labels = docker("volume", "ls", "-q", "--filter", "label=org.nexpoly.m5.filesystem-uuid=" + row["filesystem_uuid"])
        aliases = labels.splitlines()
        if must_exist and row["volume"] not in aliases:
            raise ValueError("A registered storage volume is missing")
        if any(docker("ps", "-aq", "--filter", "volume=" + name) for name in aliases):
            raise ValueError("Recycle all source and destination accounts before storage maintenance")


@contextmanager
def paused_relay(vm, info):
    try:
        native.control(info, "ping")
    except (ConnectionRefusedError, FileNotFoundError):
        active = False
    else:
        active = True
        native.control(info, "stop")
    try:
        # Keep the guest inaccessible through the NFS forward throughout the checkpoint.
        with socket.socket() as guard:
            guard.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            end = time.monotonic() + 10
            while True:
                try:
                    guard.bind(("127.0.0.1", info["port"]))
                    guard.listen(1)
                    break
                except OSError:
                    if time.monotonic() >= end:
                        raise
                    time.sleep(0.2)
            yield
    finally:
        if active:
            native.start(vm, info)


def checkpoint(vm, registry, report, guest):
    info = native.read(vm)
    require_space(report["host_free_bytes"]["backup"], launcher.CANDIDATE_BACKUP_BYTES, 16 * launcher.offline.GIB)
    with offline_registry(registry) as db:
        db.check_format()
        endpoints = db.data.get("nfs_endpoints", {})
        keys = {"deploy-m6/" + row["owner"]["user_id"] for row in report["accounts"].values()}
        if db.data.get("format") not in (4, 5) or set(endpoints) != keys or set(db.data.get("host_media", {})) != keys:
            raise ValueError("Expected the registered candidate source pair")
        claim = db.data["native_claim"]
        if claim["phase"] != "src":
            raise ValueError("Expected the claimed candidate source")
        for role in "ab":
            row = info["workspaces"]["src-" + role]
            owner = db.data["candidate_owners"][role]
            key = "deploy-m6/" + owner["user_id"]
            if (any(report["accounts"][role]["owner"].get(k) != val for k, val in owner.items())
                    or claim["filesystem_uuids"][role] != row["filesystem_uuid"]
                    or Path(db.data["host_media"][key]["file"]) != vm / ("m5-" + role + ".raw")
                    or endpoints[key]["volume"] != row["volume"]):
                raise ValueError("Registered source owner or volume differs")
        guest.run("python3", "-IB", "/opt/nexpoly/ops/openscience-per-user-dev/quota_nfs.py", "check-claim",
                  "src", claim["deployment"], claim["registry_sha256"], *(claim["filesystem_uuids"][r] for r in "ab"),
                  capture_output=True)
        unused_volumes(info, must_exist=True)
        program = (native.OPS / "quota-candidate-checkpoint.py").read_bytes()
        with paused_relay(vm, info):
            output = guest.run("python3", "-IB", "-", uuid4().hex, input=program, capture_output=True, timeout=600)
        report["web_checkpoint"] = json.loads(output.stdout)
        for role, row in report["web_checkpoint"]["accounts"].items():
            owner = db.data["candidate_owners"][role]
            row["source_generation"] = db.data["workspaces"]["deploy-m6/" + owner["user_id"]]["generation"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("inspect")
    sub.add_parser("connect").add_argument("--packages-dir", type=Path)
    sub.add_parser("checkpoint").add_argument("--registry", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        parser.error("Use a new evidence file")
    os.umask(0o077)
    vm = args.vm.resolve(strict=True)
    report, guest = inspect(vm)
    with launcher.storage_lock(vm):
        if args.action == "connect":
            connect(vm, guest, args.packages_dir)
        elif args.action == "checkpoint":
            checkpoint(vm, args.registry, report, guest)
    with args.output.open("x") as out:
        json.dump(report, out, indent=2, sort_keys=True)
        out.write("\n")


if __name__ == "__main__":
    main()

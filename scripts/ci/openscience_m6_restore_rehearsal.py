"""Restore synthetic A/B workspaces on a second offline guest using only copied backup media and pinned manifests."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

import verify_openscience_m5_quota as vm
from openscience_m5_backup import publish


REPORT = vm.ROOT / "docs/verification/openscience-m6-ac22-20261124.json"
BOOT = """#!/bin/sh
set -eu
trap 'sync; systemctl --no-block poweroff' EXIT
udevadm settle --timeout=30
mkdir -p /inputs
mount -o ro,nosuid,nodev,noexec /dev/disk/by-label/M6RESTORE /inputs
exec > /dev/virtio-ports/nexpoly.restore.result 2>&1
test "$(uname -r)" = "6.8.0-147-generic"
for m in quota_tree quota_v2; do
    test -d "/sys/module/$m" || insmod "/inputs/$m.ko"
done
python3 -B /inputs/openscience_m6_restore_rehearsal_guest.py
"""


def sparse(path, size):
    with path.open("xb") as stream:
        stream.truncate(size)


def drive(path, kind, name, readonly=False):
    spec = f"file={path},format={kind},if=none,id={name}"
    return ["-drive", spec + (",readonly=on" if readonly else ""),
            "-device", f"virtio-blk-pci,drive={name},serial={name}"]


def boot_guest(work, base, transfer, phase, users, pins=None, native_fixtures=None, export=False):
    directory = work / phase
    directory.mkdir()
    inputs = directory / "inputs"
    shutil.copytree(work / "common", inputs)
    machine = str(uuid4())
    spec = dict(phase=phase, users=users, machine_uuid=machine, export=export)
    if native_fixtures:
        spec["native_pins"] = {}
        for role in users:
            archive = inputs / (role + ".tar")
            shutil.copyfile(native_fixtures / archive.name, archive)
            spec["native_pins"][role] = vm.file_digest(archive)
    if pins is not None:
        spec["pins"] = pins
    (inputs / "run.json").write_text(json.dumps(spec))
    vm.run("genisoimage", "-quiet", "-R", "-V", "M6RESTORE", "-o", str(directory / "inputs.iso"), str(inputs))
    (directory / "user-data").write_text(BOOT)
    (directory / "meta-data").write_text(f"instance-id: {machine}\nlocal-hostname: m6-restore-{phase}\n")
    (directory / "network-config").write_text("version: 2\nethernets: {}\n")
    vm.run("cloud-localds", "--network-config", str(directory / "network-config"), str(directory / "seed.iso"),
           str(directory / "user-data"), str(directory / "meta-data"))
    vm.run("qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", str(base),
           str(directory / "system.qcow2"))
    args = ["qemu-system-x86_64", "-no-user-config", "-nodefaults", "-machine", "q35,accel=tcg",
            "-uuid", machine, "-cpu", "max", "-smp", "2", "-m", "1536",
            "-kernel", str(work / "kernel"), "-initrd", str(work / "initrd"),
            "-append", "root=LABEL=cloudimg-rootfs ro console=ttyS0 systemd.mask=systemd-networkd-wait-online.service",
            "-display", "none", "-monitor", "none", "-no-reboot", "-nic", "none", "-serial", "file:console.log",
            "-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
            "-chardev", "file,id=results,path=results.log", "-device", "virtio-serial-pci",
            "-device", "virtserialport,chardev=results,name=nexpoly.restore.result",
            *drive("system.qcow2", "qcow2", "m6-system"), *drive("seed.iso", "raw", "m6-seed", True),
            *drive("inputs.iso", "raw", "m6-input", True), *drive(transfer, "raw", "m6-transfer", phase == "dst")]
    for role in users:
        name = f"m6-{phase}-{role}"
        sparse(directory / name, vm.DISK_BYTES)
        args.extend(drive(name, "raw", name))
    start = monotonic()
    with (directory / "qemu.log").open("wb") as log:
        subprocess.run(args, cwd=directory, stdin=subprocess.DEVNULL, stdout=log, stderr=log, check=True, timeout=420)
    lines = (directory / "results.log").read_text(errors="replace").splitlines()
    marker = "M6_RESTORE_RESULT="
    found = [line[len(marker):] for line in lines if line.startswith(marker)]
    if len(found) != 1:
        raise ValueError(f"{phase} guest did not finish: " + "\n".join(lines[-12:]))
    receipt = dict(**json.loads(found[0]), machine_uuid=machine, elapsed_seconds=round(monotonic() - start, 2))
    if native_fixtures:
        receipt["native_fixtures_sha256"] = spec["native_pins"]
    return receipt


def drill(work, base, modules, native_fixtures=None, restored_packages=None):
    if vm.file_digest(base) != vm.image_lock()["sha256"]:
        raise ValueError("Unexpected cloud base image")
    if shutil.disk_usage(work).free < 32 * vm.GIB + 2 * vm.DISK_BYTES + 4 * vm.GIB:
        raise ValueError("Insufficient temporary disk space including the development storage reserve")
    common = work / "common"
    common.mkdir()
    vm.guest_kernel(base, work, common, modules)
    for name in ("openscience_m5_archive.py", "openscience_m5_backup.py", "openscience_m5_quota_bundle.py",
                 "openscience_per_user_dev.py"):
        shutil.copyfile(vm.ROOT / "scripts" / name, common / name)
    ops = vm.ROOT / "ops/openscience-per-user-dev"
    shutil.copyfile(ops / "quota-guest.py", common / "quota_guest.py")
    shutil.copyfile(ops / "quota.py", common / "quota.py")
    guest_name = "openscience_m6_restore_rehearsal_guest.py"
    shutil.copyfile(Path(__file__).with_name(guest_name), common / guest_name)
    rows = json.loads(REPORT.read_bytes())["runtime"]["dev-m1"]
    users = {role: {field: rows["test-user-" + role][field] for field in ("environment", "user_id", "workspace", "uid", "gid")}
             for role in ("a", "b")}

    transfer = work / "source-backup.raw"
    sparse(transfer, 96 * 1024**2)
    src = boot_guest(work, base, transfer, "src", users, native_fixtures=native_fixtures)
    pins = {role: row["manifest"] for role, row in src["accounts"].items()}
    # Source is fully powered off. Only a copy of its independent backup device crosses guests.
    shutil.rmtree(work / "src")
    copied = work / "recovery-backup.raw"
    shutil.copyfile(transfer, copied)
    media_hash = vm.file_digest(transfer)
    if vm.file_digest(copied) != media_hash:
        raise ValueError("Transferred medium checksum mismatch")
    transfer.unlink()
    dst = boot_guest(work, base, copied, "dst", users, pins, export=bool(restored_packages))
    if (vm.file_digest(copied) != media_hash or dst["boot_id"] == src["boot_id"]
            or dst["machine_uuid"] == src["machine_uuid"]):
        raise ValueError("Backup medium changed or destination did not boot independently")
    uuids = set()
    for role in users:
        source, target = src["accounts"][role], dst["accounts"][role]
        uuids.update([source["fs_uuid"], target["fs_uuid"]])
        if any(source[field] != target[field] for field in ("inventory", "facts", "uid", "hard_bytes")):
            raise ValueError("Restored workspace differs from source for " + role)
    a, b = (src["accounts"][role]["facts"] for role in users)
    if len(uuids) != 4 or any(a[name]["sha256"] == b[name]["sha256"] for name in a if "reserved_ranges" not in a[name]):
        raise ValueError("Workspaces or filesystems are not distinct")
    if restored_packages:
        # QEMU has already powered off and closed all disks. debugfs defaults to read-only.
        with TemporaryDirectory(dir=restored_packages.parent) as tmp:
            stage = Path(tmp) / "verified"
            stage.mkdir(mode=0o700)
            for role in users:
                archive = stage / (role + ".tar")
                subprocess.run(["debugfs", "-R", f"dump /m6-export.tar {archive.name}",
                                str(work / "dst" / ("m6-dst-" + role))], cwd=stage, check=True, capture_output=True)
                if vm.file_digest(archive) != dst["accounts"][role]["export_sha256"]:
                    raise ValueError("Offline destination export differs from the recovered workspace archive")
            publish(stage, restored_packages)
    return {"status": "passed", "environment": "dev-m1 synthetic identities", "physical_hosts": 1,
            "image_sha256": vm.image_lock()["sha256"], "kernel": vm.KERNEL, "accelerator": "tcg",
            "source_disks_deleted_before_target_boot": not (work / "src").exists() and not transfer.exists(),
            "transfer_sha256": media_hash, "users": users, "source": src, "destination": dst}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-image", type=Path, default=vm.default_image())
    parser.add_argument("--quota-modules", type=Path, default=vm.DEFAULT_MODULES)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-fixtures", type=Path, help="Read a.tar and b.tar only into the source guest")
    parser.add_argument("--restored-packages", type=Path, help="New directory for verified offline destination TARs")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new evidence file")
    if bool(args.native_fixtures) != bool(args.restored_packages):
        parser.error("Use --native-fixtures and --restored-packages together")
    if args.restored_packages and args.restored_packages.exists():
        parser.error("Use a new restored package directory")
    os.umask(0o077)
    started = datetime.now(timezone.utc).isoformat()
    with TemporaryDirectory(prefix="openscience-m6-two-guests-") as tmp:
        result = drill(Path(tmp), args.base_image.resolve(), args.quota_modules, args.native_fixtures,
                       args.restored_packages)
    result.update(start=started, end=datetime.now(timezone.utc).isoformat(), temporary_disks_removed=True)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")

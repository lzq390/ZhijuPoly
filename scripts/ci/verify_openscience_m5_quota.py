"""Run the dev-m1 A/B storage acceptance in a disposable, offline Linux guest."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
GIB = 1024**3
DATA_DISKS = ("m5-a", "m5-b", "m5-restore-a", "m5-restore-b")
BACKUP_DISK = "m5-backup"
DISK_BYTES = 3 * GIB
BACKUP_BYTES = 256 * 1024**2
MARKER = "M5_QUOTA_RESULT="
SOFTWARE = ROOT / "ops/self_hosted_runner/guest/software-lock.json"
KERNEL = "6.8.0-147-generic"
MODULES_PACKAGE_SHA256 = (
    "b3f16d16b54796497884b27810b0942243bdc6ef26da1ce821e948ece39a9492"
)
MODULE_HASHES = {
    "quota_tree.ko.zst": "6d5206a8ae384b03ec41b11e3be5a4a13ff30cbaefe4a6d2ce62bf6fc062b216",
    "quota_v2.ko.zst": "a54b39dc4719e7a2dd407acfa0ff913da311b374761ef5e25eff0c8f607436bd",
}
DEFAULT_MODULES = Path.home() / ".cache/nexpoly/openscience-m5" / KERNEL

# This script is NoCloud user-data for a fresh, private COW system disk.
# The guest sees only new virtual disks and a read-only copy of the verified input files.
BOOT = """#!/bin/sh
set -eu
trap 'sync; systemctl --no-block poweroff' EXIT
udevadm settle --timeout=30
mkdir -p /inputs /backup
mount -o ro,nosuid,nodev,noexec /dev/disk/by-label/M5INPUT /inputs
exec > /dev/virtio-ports/nexpoly.quota.result 2>&1
test "$(uname -r)" = "6.8.0-147-generic"
for module in quota_tree quota_v2; do
    if ! test -d "/sys/module/$module"; then insmod "/inputs/$module.ko"; fi
    test -d "/sys/module/$module"
done
mkfs.ext4 -q -F -m 0 /dev/disk/by-id/virtio-m5-backup
mount -o nodev,nosuid,noexec /dev/disk/by-id/virtio-m5-backup /backup
chmod 0700 /backup
export PYTHONDONTWRITEBYTECODE=1
python3 -B /inputs/guest.py
"""


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(*args):
    return subprocess.run(
        args, capture_output=True, check=True, text=True
    ).stdout.strip()


def image_lock():
    return json.loads(SOFTWARE.read_text())["cloud_image"]


def default_image():
    name = "ubuntu-" + image_lock()["sha256"] + ".qcow2"
    return Path("/data/lzq/github-runner/state/downloads") / name


def guest_kernel(base, work, inputs, modules):
    # Partition 16 of this locked Ubuntu image is a standalone ext4 /boot. Read it
    # using userspace image readers; direct boot lets an offline guest skip network-online.
    boot_image = work / "boot.raw"
    filesystem = {
        "driver": "raw",
        "offset": 227328 * 512,
        "size": 1869825 * 512,
        "file": {
            "driver": "qcow2",
            "file": {"driver": "file", "filename": str(base.resolve())},
        },
    }
    run(
        "qemu-img",
        "convert",
        "-O",
        "raw",
        "json:" + json.dumps(filesystem),
        str(boot_image),
    )
    for item, name in (("vmlinuz", "kernel"), ("initrd.img", "initrd")):
        subprocess.run(
            ["debugfs", "-R", f"dump /{item}-{KERNEL} {name}", str(boot_image)],
            cwd=work,
            check=True,
            capture_output=True,
        )
        if (work / name).stat().st_size == 0:
            raise ValueError(f"Missing {item} from the pinned guest image")
    boot_image.unlink()
    for name, expected in MODULE_HASHES.items():
        module = modules / name
        if not module.is_file() or file_digest(module) != expected:
            raise ValueError(
                f"Supply the pinned Ubuntu linux-modules-extra-{KERNEL} copy of {name} in {modules}"
            )
        with (inputs / Path(name).stem).open("xb") as output:
            subprocess.run(["zstd", "-dc", str(module)], stdout=output, check=True)


def private_drives(base, work, inputs, modules, data_disks=None):
    # Supplying explicit disk formats and a known backing image also prevents probing unrelated files.
    if not base.is_file() or file_digest(base) != image_lock()["sha256"]:
        raise ValueError(
            "Ubuntu cloud base image differs from the repository software lock"
        )
    guest_kernel(base, work, inputs, modules)
    disk = work / "system.qcow2"
    run(
        "qemu-img",
        "create",
        "-q",
        "-f",
        "qcow2",
        "-F",
        "qcow2",
        "-b",
        str(base.resolve()),
        str(disk),
    )
    sizes = (
        {name: DISK_BYTES for name in DATA_DISKS} if data_disks is None else data_disks
    )
    for serial, size in {**sizes, BACKUP_DISK: BACKUP_BYTES}.items():
        with (work / (serial + ".raw")).open("xb") as f:
            f.truncate(size)

    (work / "user-data").write_text(BOOT)
    (work / "meta-data").write_text(
        "instance-id: nexpoly-m5-offline\nlocal-hostname: m5-quota\n"
    )
    (work / "network-config").write_text("version: 2\nethernets: {}\n")
    run(
        "cloud-localds",
        "--network-config",
        str(work / "network-config"),
        str(work / "seed.iso"),
        str(work / "user-data"),
        str(work / "meta-data"),
    )
    run(
        "genisoimage",
        "-quiet",
        "-R",
        "-V",
        "M5INPUT",
        "-o",
        str(work / "inputs.iso"),
        str(inputs),
    )


def run_vm(base, work, inputs, modules=DEFAULT_MODULES, *, data_disks=None):
    private_drives(base, work, inputs, modules, data_disks)
    args = [
        "qemu-system-x86_64",
        "-no-user-config",
        "-nodefaults",
        "-machine",
        "q35,accel=tcg",
        "-cpu",
        "max",
        "-smp",
        "2",
        "-m",
        "1536",
        "-kernel",
        "kernel",
        "-initrd",
        "initrd",
        "-append",
        "root=LABEL=cloudimg-rootfs ro console=ttyS0 systemd.mask=systemd-networkd-wait-online.service",
        "-display",
        "none",
        "-monitor",
        "none",
        "-no-reboot",
        "-nic",
        "none",
        "-serial",
        "file:console.log",
        "-sandbox",
        "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
        "-chardev",
        "file,id=results,path=results.log",
        "-device",
        "virtio-serial-pci",
        "-device",
        "virtserialport,chardev=results,name=nexpoly.quota.result",
        "-drive",
        "file=system.qcow2,format=qcow2,if=none,id=system",
        "-device",
        "virtio-blk-pci,drive=system,bootindex=1",
        "-drive",
        "file=seed.iso,format=raw,if=virtio,readonly=on",
        "-drive",
        "file=inputs.iso,format=raw,if=virtio,readonly=on",
    ]
    for serial in (*(DATA_DISKS if data_disks is None else data_disks), BACKUP_DISK):
        args += [
            "-drive",
            f"if=none,file={serial}.raw,format=raw,id={serial}",
            "-device",
            f"virtio-blk-pci,drive={serial},serial={serial}",
        ]
    # The caller owns the executable and work directory; there is no host block device,
    # socket, forwarded port or host directory exposed to the guest.
    start = monotonic()
    with (work / "qemu.log").open("wb") as log:
        subprocess.run(
            args,
            cwd=work,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            check=True,
            timeout=600,
        )
    text = (work / "results.log").read_text(errors="replace")
    results = [
        line.removeprefix(MARKER)
        for line in text.splitlines()
        if line.startswith(MARKER)
    ]
    if len(results) != 1:
        raise ValueError(
            "Guest failed to return exactly one quota result. "
            + (
                "Guest error: " + text.strip().splitlines()[-1][:360]
                if text.strip()
                else "No result on guest channel."
            )
        )
    payload = json.loads(results[0])
    if payload.get("status") != "passed":
        raise ValueError(
            "Guest quota acceptance failed: "
            + str(payload.get("error", "see guest result"))[:360]
        )
    return {
        "guest": payload,
        "vm": {
            "image_sha256": image_lock()["sha256"],
            "image_version": image_lock()["version"],
            "linux_release": KERNEL,
            "ubuntu_modules_extra_package_sha256": MODULES_PACKAGE_SHA256,
            "guest_quota_modules_sha256": MODULE_HASHES,
            "accelerator": "tcg",
            "guest_memory_mib": 1536,
            "virtual_cpus": 2,
            "network_adapters": 0,
            "host_directory_shares": 0,
            **(
                {"test_device_bytes": DISK_BYTES}
                if data_disks is None
                else {"data_disks_bytes": data_disks}
            ),
            "backup_device_bytes": BACKUP_BYTES,
            "elapsed_seconds": round(monotonic() - start, 2),
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--packages", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--base-image", type=Path, default=default_image())
    parser.add_argument(
        "--quota-modules",
        type=Path,
        default=DEFAULT_MODULES,
        help="Directory containing the pinned Ubuntu quota_tree.ko.zst and quota_v2.ko.zst",
    )
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new evidence file")
    from openscience_m5_quota_inputs import retained_state, stage_inputs

    os.umask(0o077)
    start = datetime.now(timezone.utc).isoformat()
    originals = retained_state(args.registry)
    with TemporaryDirectory(prefix="nexpoly-m5-quota-") as tmp:
        work = Path(tmp)
        # Preserve the development volume floor if all guest test drives grow to their maximum.
        if (
            shutil.disk_usage(work).free
            < 32 * GIB + 4 * DISK_BYTES + BACKUP_BYTES + 4 * GIB
        ):
            raise ValueError(
                "Insufficient scratch filesystem space for M5 guest and reserved free space"
            )
        inputs = work / "inputs"
        inputs.mkdir()
        staged = stage_inputs(inputs, args.registry, args.packages, args.evidence)
        code = ROOT / "ops/openscience-per-user-dev"
        for source, name in (
            (code / "quota-guest.py", "guest.py"),
            (code / "quota.py", "quota.py"),
        ):
            shutil.copyfile(source, inputs / name)
        for name in (
            "openscience_m5_archive.py",
            "openscience_m5_backup.py",
            "openscience_per_user_dev.py",
            "openscience_m5_quota_bundle.py",
        ):
            shutil.copyfile(ROOT / "scripts" / name, inputs / name)
        observed = run_vm(args.base_image, work, inputs, args.quota_modules)
    if originals != retained_state(args.registry):
        raise ValueError(
            "Retained dev-m1 registry or private volume identity changed during the quota drill"
        )
    report = {
        "status": "passed",
        "environment": "dev-m1",
        "start": start,
        "end": datetime.now(timezone.utc).isoformat(),
        **observed,
        "originals": {"before": originals, "unchanged": True},
        "temporary_vm_and_volumes_removed": True,
    }
    if staged is not None:
        report["inputs"] = staged
    with args.output.open("x") as output:
        json.dump(report, output, indent=2)
        output.write("\n")


if __name__ == "__main__":
    main()

"""Start the isolated ext4 storage VM for the native M5 workspace browser drill."""

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess

import verify_openscience_m5_quota as offline
from openscience_m5_quota_inputs import stage_inputs
from openscience_m5_native_storage import ssh_config_text

BOOT = """#!/bin/sh
set -eu
exec >/var/log/m5-bootstrap.log 2>&1
trap 'echo "bootstrap exit=$?" >/dev/ttyS0' EXIT
udevadm settle
mkdir /inputs
mount -o ro,nosuid,nodev,noexec /dev/disk/by-label/M5INPUT /inputs
install -m 600 /inputs/ssh_host_key /etc/ssh/m5_host_key
install -m 600 /inputs/client.pub /root/.ssh/authorized_keys
printf 'HostKey /etc/ssh/m5_host_key\nPermitRootLogin prohibit-password\nPasswordAuthentication no\n' >/etc/ssh/sshd_config.d/00-m5.conf
systemctl restart ssh.socket
systemctl restart ssh.service
ip -br a >/dev/ttyS0
mkdir -p /opt/nexpoly /backup
mountpoint -q /backup && exit 1
test -b /dev/disk/by-id/virtio-m5-backup
test -z "$(blkid /dev/disk/by-id/virtio-m5-backup || true)"
mkfs.ext4 -q -m 0 /dev/disk/by-id/virtio-m5-backup
mount -o nodev,nosuid,noexec /dev/disk/by-id/virtio-m5-backup /backup
chmod 700 /backup
for m in quota_tree quota_v2; do test -d /sys/module/$m || insmod /inputs/$m.ko; done
PYTHONDONTWRITEBYTECODE=1 python3 -B /inputs/provision.py
python3 -IB /inputs/quota-vm-storage.py install
systemctl start m5-storage.service
"""

PROVISION = """import json
from pathlib import Path
import quota_guest as drill

rows = drill.input_bindings()
volumes = drill.mount_volumes(drill.blank_volumes(), [])
result = {}
for role, row in rows.items():
    mount = volumes['src', role]
    policy = drill.quota.install(mount, row)
    workspace = mount / 'workspace'
    workspace.mkdir()
    with (drill.INPUTS / role / 'workspace.tar').open('rb') as handle:
        inventory = drill.archive.process_archive(handle, row['uid'], row['workspace'], workspace)
    if inventory != row['inventory'] or inventory != drill.inventory(workspace, row):
        raise ValueError('Source backup readback mismatch')
    result[role] = {'quota':policy, 'inventory': inventory}
Path('/run/m5-provision.json').write_text(json.dumps(result, sort_keys=True))
"""


def build(work, registry, packages, evidence, base, modules):
    if (
        not base.is_file()
        or offline.file_digest(base) != offline.image_lock()["sha256"]
    ):
        raise ValueError("Guest base image must match the repository lock")
    # Bound maximum growth from the root overlay and each of the five sparse disks.
    if shutil.disk_usage(work).free < 66 * offline.GIB:
        raise ValueError("Need 66 GiB scratch space including the 32 GiB host reserve")
    inputs = work / "inputs"
    inputs.mkdir()
    stage_inputs(inputs, registry, packages, evidence)
    offline.guest_kernel(base, work, inputs, modules)
    ops = offline.ROOT / "ops/openscience-per-user-dev"
    for src, dst in [
        (ops / "quota.py", "quota.py"),
        (ops / "quota-vm-storage.py", "quota-vm-storage.py"),
        (ops / "quota-guest.py", "quota_guest.py"),
    ]:
        shutil.copyfile(src, inputs / dst)
    for name in (
        "openscience_m5_archive.py",
        "openscience_m5_quota_bundle.py",
        "openscience_m5_backup.py",
        "openscience_per_user_dev.py",
    ):
        shutil.copyfile(offline.ROOT / "scripts" / name, inputs / name)
    (inputs / "provision.py").write_text(PROVISION)
    for target in (work / "key", inputs / "ssh_host_key"):
        offline.run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(target))
    shutil.copyfile(work / "key.pub", inputs / "client.pub")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    host_key = (inputs / "ssh_host_key.pub").read_text().split()
    (work / "known_hosts").write_text(
        f"[127.0.0.1]:{port} {host_key[0]} {host_key[1]}\n"
    )
    (work / "ssh.conf").write_text(ssh_config_text(work, port))
    offline.run(
        "qemu-img",
        "create",
        "-q",
        "-f",
        "qcow2",
        "-F",
        "qcow2",
        "-b",
        str(base.resolve()),
        str(work / "system.qcow2"),
        "20G",
    )
    for serial in (*offline.DATA_DISKS, offline.BACKUP_DISK):
        with (work / (serial + ".raw")).open("xb") as handle:
            handle.truncate(
                offline.BACKUP_BYTES
                if serial == offline.BACKUP_DISK
                else offline.DISK_BYTES
            )
    (work / "user-data").write_text(BOOT)
    (work / "meta-data").write_text(
        "instance-id: nexpoly-m5-web\nlocal-hostname: m5-quota-web\n"
    )
    (work / "network-config").write_text(
        "version: 2\nethernets:\n  m5:\n    match: {macaddress: '52:54:00:37:a5:17'}\n"
        "    addresses: [10.0.2.15/24]\n"
    )
    offline.run(
        "cloud-localds",
        "--network-config",
        str(work / "network-config"),
        str(work / "seed.iso"),
        str(work / "user-data"),
        str(work / "meta-data"),
    )
    offline.run(
        "genisoimage",
        "-quiet",
        "-R",
        "-V",
        "M5INPUT",
        "-o",
        str(work / "inputs.iso"),
        str(inputs),
    )
    pin_existing(work, base, port)
    launch(work, port)


def pin_existing(work, base, port):
    data = {
        "format": "nexpoly-m5-isolated-browser-v1",
        "ssh_port": port,
        "base": str(base.resolve()),
        "media": {
            name: offline.file_digest(work / name)
            for name in ("kernel", "initrd", "seed.iso", "inputs.iso")
        },
    }
    with (work / "vm.json").open("x") as pin:
        json.dump(data, pin, sort_keys=True)


def resume(work):
    if (
        work.is_symlink()
        or work.stat().st_uid != os.getuid()
        or work.stat().st_mode & 0o077
    ):
        raise ValueError("Expected an operator-owned private VM directory")
    config = json.loads((work / "vm.json").read_bytes())
    if (
        config["format"] != "nexpoly-m5-isolated-browser-v1"
        or not 1024 <= config["ssh_port"] <= 65535
    ):
        raise ValueError("Unrecognized retained VM")
    if config["ssh_port"] == 9011:
        raise ValueError("Reserved port")
    if set(config["media"]) != {"kernel", "initrd", "seed.iso", "inputs.iso"}:
        raise ValueError("Incomplete boot media pins")
    for name, expected in config["media"].items():
        path = work / name
        if (
            path.is_symlink()
            or not path.is_file()
            or offline.file_digest(path) != expected
        ):
            raise ValueError(f"Boot media changed: {name}")
    base = Path(config["base"])
    disk = work / "system.qcow2"
    if (
        disk.is_symlink()
        or not disk.is_file()
        or offline.file_digest(base) != offline.image_lock()["sha256"]
    ):
        raise ValueError("Unexpected M5 system disk or base")
    info = json.loads(offline.run("qemu-img", "info", "--output=json", str(disk)))
    if (
        info["format"] != "qcow2"
        or Path(info["full-backing-filename"]) != base
        or info["backing-filename-format"] != "qcow2"
    ):
        raise ValueError("The retained system overlay has an unexpected backing file")
    for serial in (*offline.DATA_DISKS, offline.BACKUP_DISK):
        path = work / (serial + ".raw")
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size
            != (
                offline.BACKUP_BYTES
                if serial == offline.BACKUP_DISK
                else offline.DISK_BYTES
            )
        ):
            raise ValueError(f"Unexpected retained M5 disk: {serial}")
    launch(work, config["ssh_port"])


def launch(work, port):
    water = (work / "waterpool").exists()
    if water:
        from prepare_openscience_m5_water_pool import verify_layout

        verify_layout(work)
    with socket.socket() as listener:
        # Match a restarting TCP listener: a completed loopback SSH connection
        # may still be in TIME_WAIT after the previous QEMU has exited.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
    # The guest gets a single loopback-only SSH path, no external connectivity, host share or device passthrough.
    argv = [
        "qemu-system-x86_64",
        "-no-user-config",
        "-nodefaults",
        "-machine",
        "q35,accel=tcg",
        "-cpu",
        "max",
        "-smp",
        "4",
        "-m",
        "8192",
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
        "-serial",
        "file:console.log",
        "-sandbox",
        "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
        "-nic",
        f"user,model=virtio-net-pci,mac=52:54:00:37:a5:17,restrict=on,hostfwd=tcp:127.0.0.1:{port}-10.0.2.15:22",
        "-drive",
        (
            "file=waterpool/system.qcow2,format=qcow2,if=virtio"
            if water
            else "file=system.qcow2,format=qcow2,if=virtio"
        ),
        "-drive",
        "file=seed.iso,format=raw,if=virtio,readonly=on",
        "-drive",
        "file=inputs.iso,format=raw,if=virtio,readonly=on",
    ]
    for serial in (*offline.DATA_DISKS, offline.BACKUP_DISK):
        argv.extend(
            [
                "-drive",
                (
                    f"if=none,file=waterpool/{serial}.qcow2,format=qcow2,id={serial}"
                    if water
                    else f"if=none,file={serial}.raw,format=raw,id={serial}"
                ),
                "-device",
                f"virtio-blk-pci,drive={serial},serial={serial}",
            ]
        )
    if water:
        argv.extend(
            (
                "-drive",
                "if=none,file=waterpool/pool.raw,format=raw,id=m5-waterpool,discard=unmap",
                "-device",
                "virtio-blk-pci,drive=m5-waterpool,serial=m5-waterpool",
            )
        )
    with (work / "qemu.log").open("ab") as log:
        process = subprocess.Popen(
            argv,
            cwd=work,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    try:
        code = process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise RuntimeError(
            f"QEMU exited during launch ({code}), see {work / 'qemu.log'}"
        )
    print(
        json.dumps(
            {
                "ssh_config": str(work / "ssh.conf"),
                "qemu_pid": process.pid,
                "guest_ready_file": "/run/m5-storage-ready",
            },
            sort_keys=True,
        ),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Boot the existing private VM after a clean guest shutdown",
    )
    for key in ("registry", "packages", "evidence"):
        parser.add_argument("--" + key, type=Path)
    parser.add_argument("--base-image", type=Path, default=offline.default_image())
    parser.add_argument("--quota-modules", type=Path, default=offline.DEFAULT_MODULES)
    args = parser.parse_args()
    os.umask(0o077)
    work = args.work.absolute()
    if args.resume:
        resume(work)
        return
    if not all((args.registry, args.packages, args.evidence)):
        parser.error("New VM creation requires registry, packages and evidence")
    work.mkdir(mode=0o700)
    build(
        work,
        args.registry,
        args.packages,
        args.evidence,
        args.base_image,
        args.quota_modules,
    )


if __name__ == "__main__":
    main()

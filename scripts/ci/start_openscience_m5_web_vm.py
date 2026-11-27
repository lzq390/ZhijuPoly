"""Start the isolated ext4 storage VM for the native M5 workspace browser drill."""

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess

import verify_openscience_m5_quota as offline
from openscience_m5_backup import available_bytes, private_file, require_space, separate_disks
from openscience_m5_quota_inputs import stage_inputs
from openscience_m5_native_storage import ssh_config_text
from openscience_per_user_dev import validate_candidate_owners, write_atomic
from prepare_openscience_m5_water_pool import DEDICATED, registered_pool, storage_lock

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


CANDIDATE_BACKUP_BYTES = 5 * offline.GIB
SECONDARY_DISKS = {"m5-restore-a", "m5-restore-b", offline.BACKUP_DISK}


def device(path):
    identity = path.stat().st_dev
    return f"{os.major(identity)}:{os.minor(identity)}"


def secondary(path, primary):
    info = path.lstat()
    if (not path.is_absolute() or path.resolve(strict=True) != path or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("Use a private, canonical operator-owned backup directory")
    return separate_disks(device(primary), device(path))


def disk_layout(work, archive, *, restore_target=False):
    layout = {}
    for serial in offline.DATA_DISKS:
        directory = archive if archive and serial in SECONDARY_DISKS and not restore_target else work
        layout[serial] = (directory / (serial + ".raw"), offline.DISK_BYTES)
    layout[offline.BACKUP_DISK] = ((archive or work) / (offline.BACKUP_DISK + ".raw"),
                                   CANDIDATE_BACKUP_BYTES if archive else offline.BACKUP_BYTES)
    return layout


def build(work, registry, packages, evidence, base, modules, owners=None, archive=None, *, restore_target=False):
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
    if owners:
        with private_file(owners) as handle:
            rows = validate_candidate_owners(json.load(handle))
        archive.mkdir(mode=0o700)
        secondary(archive, work)
        secondary_bytes = CANDIDATE_BACKUP_BYTES + (0 if restore_target else 2 * offline.DISK_BYTES)
        require_space(available_bytes(archive), secondary_bytes, 16 * offline.GIB)
        spec = {"version": 1, "environment": "deploy-m6", "hard_limit_bytes": 2_000_000_000,
                "users": {r: {**row, "registered_generation": 0} for r, row in rows.items()}}
        if restore_target:
            spec["restore_target"] = True
        (inputs / "spec.json").write_text(json.dumps(spec, sort_keys=True))
    else:
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
    if owners:
        shutil.copyfile(ops / "quota-candidate-provision.py", inputs / "provision.py")
    else:
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
    for path, size in disk_layout(work, archive, restore_target=restore_target).values():
        with path.open("xb") as handle:
            handle.truncate(size)
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
    pin_existing(work, base, port, archive, restore_target=restore_target)
    launch(work, port)


def pin_existing(work, base, port, archive=None, *, restore_target=False):
    data = {
        "format": "nexpoly-m5-isolated-browser-v1",
        "storage": DEDICATED,
        "ssh_port": port,
        "base": str(base.resolve()),
        "media": {
            name: offline.file_digest(work / name)
            for name in ("kernel", "initrd", "seed.iso", "inputs.iso")
        },
    }
    if restore_target:
        data["restore_target"] = True
    if archive:
        data["backup_dir"] = str(archive)
        disks = secondary(archive, work)
        data["host_disks"] = {role: row for role, row in zip(("primary", "backup"), disks)}
        data["disk_inodes"] = {k: path.stat().st_ino for k, (path, size) in disk_layout(work, archive, restore_target=restore_target).items()}
    pin = work / "vm.json"
    with storage_lock(work):
        if pin.exists() or pin.is_symlink():
            raise FileExistsError(pin)
        write_atomic(pin, json.dumps(data, sort_keys=True).encode() + b"\n", 0o600)


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
    checked_layout(work, config)
    launch(work, config["ssh_port"])


def checked_layout(work, config):
    archive = Path(config["backup_dir"]) if "backup_dir" in config else None
    layout = disk_layout(work, archive, restore_target=config.get("restore_target") is True)
    if archive:
        disks = secondary(archive, work)
        if config["host_disks"] != dict(zip(("primary", "backup"), disks)):
            raise ValueError("Workspace and backup disks changed")
    for serial, (path, size) in layout.items():
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_size != size or (archive and
                (config["disk_inodes"][serial] != info.st_ino or info.st_nlink != 1 or info.st_uid != os.getuid()
                 or info.st_mode & 0o077))):
            raise ValueError(f"Unexpected retained M5 disk: {serial}")
    return layout


def launch(work, port):
    with storage_lock(work):
        launch_pinned(work, port, registered_pool(work) is not None)


def launch_pinned(work, port, water):
    config = json.loads((work / "vm.json").read_bytes())
    # The historical water pool stores its own overlays. Keep that layout apart from split-host storage.
    if water and "backup_dir" in config:
        raise ValueError("A candidate with split storage cannot use the synthetic water pool")
    layout = checked_layout(work, config) if "backup_dir" in config else disk_layout(work, None)
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
        "2048" if "backup_dir" in config else "8192",
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
    for serial, (path, _) in layout.items():
        escaped_path = str(path).replace(",", ",,")
        argv.extend(
            [
                "-drive",
                (
                    f"if=none,file=waterpool/{serial}.qcow2,format=qcow2,id={serial}"
                    if water
                    else f"if=none,file={escaped_path},format=raw,id={serial}"
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
    parser.add_argument("--owners", type=Path, help="Private deploy-m6 account registration for a fresh candidate")
    parser.add_argument("--backup-dir", type=Path, help="New private directory on a different physical disk")
    parser.add_argument("--restore-target", action="store_true",
                        help="Prepare fresh dst filesystems on the workspace disk for imported backups")
    for key in ("registry", "packages", "evidence"):
        parser.add_argument("--" + key, type=Path)
    parser.add_argument("--base-image", type=Path, default=offline.default_image())
    parser.add_argument("--quota-modules", type=Path, default=offline.DEFAULT_MODULES)
    args = parser.parse_args()
    if args.restore_target and (not args.owners or not args.backup_dir or args.resume):
        parser.error("--restore-target requires fresh candidate owners and separate backup storage")
    os.umask(0o077)
    work = args.work.absolute()
    if args.resume:
        resume(work)
        return
    legacy = (args.registry, args.packages, args.evidence)
    if args.owners or args.backup_dir:
        if not (args.owners and args.backup_dir) or any(legacy):
            parser.error("Use --owners and --backup-dir together on a fresh candidate")
    elif not all(legacy):
        parser.error("New VM creation requires registry, packages and evidence")
    work.mkdir(mode=0o700)
    build(
        work,
        args.registry,
        args.packages,
        args.evidence,
        args.base_image,
        args.quota_modules,
        args.owners,
        args.backup_dir.absolute() if args.backup_dir else None,
        restore_target=args.restore_target,
    )


if __name__ == "__main__":
    main()

"""Install and remount only the five formatted disks of the isolated M5 browser guest."""

import argparse
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from uuid import UUID

MOUNTS = Path("/mnt/openscience-m5-quota")
CONFIG = Path("/etc/nexpoly-m5-disks.json")
LIBRARY = Path("/usr/local/lib/nexpoly-m5-storage")
UNITS = Path("/etc/systemd/system")


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def volumes():
    yield "m5-backup", Path("/backup")
    for stage, prefix in (("src", "m5-"), ("dst", "m5-restore-")):
        for role in ("a", "b"):
            yield prefix + role, MOUNTS / f"{stage}-{role}"


def device(serial):
    node = Path("/dev/disk/by-id/virtio-" + serial).resolve(strict=True)
    if (
        not stat.S_ISBLK(node.stat().st_mode)
        or (Path("/sys/class/block") / node.name / "serial").read_text().strip()
        != serial
    ):
        raise ValueError(f"Expected the M5 block device {serial}")
    return node


def disk_uuid(node):
    return str(UUID(run("blkid", "-p", "-o", "value", "-s", "UUID", str(node))))


def mount(node, target, options):
    target.mkdir(exist_ok=True)
    if not target.is_mount():
        run("mount", "-o", options, str(node), str(target))
    if target.stat().st_dev != node.stat().st_rdev:
        raise ValueError(f"Unexpected device already mounted at {target}")


def install():
    pins = {}
    for serial, path in volumes():
        node = device(serial)
        if not path.is_mount() or path.stat().st_dev != node.stat().st_rdev:
            raise ValueError("Install requires the original guest mounts")
        pins[serial] = disk_uuid(node)
    if len(set(pins.values())) != len(pins):
        raise ValueError("The guest filesystems must be independent")
    if CONFIG.exists() and json.loads(CONFIG.read_text()) != pins:
        raise ValueError("Destination already pins a different set of disks")
    CONFIG.write_text(json.dumps(pins, sort_keys=True))
    CONFIG.chmod(0o600)
    LIBRARY.mkdir(exist_ok=True)
    destination = LIBRARY / "mount-storage.py"
    if Path(__file__).resolve() != destination:
        shutil.copyfile(__file__, destination)
    (UNITS / "m5-storage.service").write_text(
        "[Unit]\nDescription=Verify and mount retained M5 test disks\n"
        "After=local-fs.target\n[Service]\nType=oneshot\nRemainAfterExit=yes\n"
        f"ExecStart=/usr/bin/python3 -IB {destination} mount\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "m5-storage.service")


def remount():
    run("udevadm", "settle", "--timeout=30")
    pins = json.loads(CONFIG.read_text())
    if set(pins) != {serial for serial, _ in volumes()}:
        raise ValueError("Incomplete retained M5 disk binding")
    mounts = [(device(serial), target, serial) for serial, target in volumes()]
    for node, _, serial in mounts:
        if disk_uuid(node) != pins[serial]:
            raise ValueError(f"Retained M5 UUID differs for {serial}")
    mount(Path("/dev/disk/by-label/M5INPUT"), Path("/inputs"), "ro,nodev,nosuid,noexec")
    for module in ("quota_tree", "quota_v2"):
        if not (Path("/sys/module") / module).is_dir():
            run("insmod", f"/inputs/{module}.ko")
    for node, target, serial in mounts:
        mount(
            node,
            target,
            "nodev,nosuid,noexec" + ("" if serial == "m5-backup" else ",usrquota"),
        )
    # Recheck accounting and enforcement with the same owner-aware kernel probe as the manager.
    sys.path.insert(0, "/inputs")
    import quota

    spec = json.loads(Path("/inputs/spec.json").read_bytes())
    for stage in ("src", "dst"):
        for role, binding in spec["users"].items():
            effective = quota.read(MOUNTS / f"{stage}-{role}", binding)
            permitted = {spec["hard_limit_bytes"]}
            if stage == "dst" and not (MOUNTS / f"dst-{role}" / "workspace").exists():
                permitted.add(0)
            if effective["hard_bytes"] not in permitted:
                raise ValueError(f"Hard limit differs for {stage}-{role}")

    Path("/run/m5-storage-ready").touch()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "mount"))
    if parser.parse_args().action == "install":
        install()
    else:
        remount()

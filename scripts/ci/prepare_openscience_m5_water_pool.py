"""Stage the two parked dev-m1 quota images on one bounded, independently measured disk."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from uuid import UUID, uuid4

import verify_openscience_m5_quota as offline
from openscience_m5_native_storage import read as read_native
from openscience_m5_storage_ssh import StorageGuest

POOL_BYTES = 768 * 1024**2
POOL_SERIAL = "m5-waterpool"
DATA_DISKS = (*offline.DATA_DISKS, offline.BACKUP_DISK)
OPS = offline.ROOT / "ops/openscience-per-user-dev"


def ensure_idle(vm, config):
    for p in Path("/proc").glob("[0-9]*"):
        try:
            if (p / "cwd").resolve(strict=True) == vm and (
                p / "comm"
            ).read_text().strip().startswith("qemu-system"):
                raise ValueError(
                    "Park the retained storage VM before preparing the pool"
                )
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass
    ensure_no_holders(config)


def ensure_no_holders(config):
    for row in config["workspaces"].values():
        if offline.run(
            "docker", "ps", "-aq", "--filter", "volume=" + row["volume"]
        ).strip():
            raise ValueError(
                "Recycle all four dedicated M5 volumes before preparing the pool"
            )


def verify_layout(vm):
    """Validate the parked backing chain before activating its writable COW copies."""
    root = vm / "waterpool"
    cfg = json.loads((root / "pool.json").read_bytes())
    if (
        root.is_symlink()
        or root.stat().st_uid != os.getuid()
        or root.stat().st_mode & 0o077
        or cfg.get("format") != "nexpoly-dev-m1-waterpool-v1"
        or cfg.get("pool_bytes") != POOL_BYTES
        or set(cfg["sources"]) != {"a", "b"}
    ):
        raise ValueError("Unexpected development water-pool layout")
    UUID(cfg["filesystem_uuid"])
    for name in ("system", *DATA_DISKS):
        overlay = root / (name + ".qcow2")
        fmt = "qcow2" if name == "system" else "raw"
        source = vm / (name + "." + fmt)
        if overlay.is_symlink() or not overlay.is_file():
            raise ValueError("Missing retained water-pool overlay")
        entry = json.loads(
            offline.run("qemu-img", "info", "--output=json", str(overlay))
        )
        if (
            entry["format"] != "qcow2"
            or Path(entry["full-backing-filename"]) != source
            or entry["backing-filename-format"] != fmt
        ):
            raise ValueError("Unexpected source for water-pool overlay")
    disk = root / "pool.raw"
    if disk.is_symlink() or not disk.is_file() or disk.stat().st_size != POOL_BYTES:
        raise ValueError("Invalid dedicated water-pool disk")
    uuid = offline.run("blkid", "-p", "-s", "UUID", "-o", "value", str(disk)).strip()
    if uuid != cfg["filesystem_uuid"]:
        raise ValueError("Unexpected water-pool filesystem UUID")
    return cfg


def copy_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare(vm, evidence):
    cfg = read_native(vm)
    ensure_idle(vm, cfg)
    out = vm / "waterpool"
    if out.exists():
        return verify_layout(vm)
    base = json.loads(evidence.read_bytes())["storage_vm_cold_restart"]
    if (
        base["status"] != "passed"
        or not base["all_four_inventories_and_kernel_quotas_match"]
    ):
        raise ValueError("Verified retained A/B quota images are required")
    if shutil.disk_usage(vm).free < 33 * 1024**3:
        raise ValueError(
            "The development host needs room for the test pool in addition to its 32 GiB reserve"
        )
    with tempfile.TemporaryDirectory(dir=vm) as tmp:
        stage = Path(tmp)
        inner = stage / "files"
        inner.mkdir()
        result = {
            "format": "nexpoly-dev-m1-waterpool-v1",
            "filesystem_uuid": str(uuid4()),
            "pool_bytes": POOL_BYTES,
            "sources": {},
        }
        for role in ("a", "b"):
            row = cfg["workspaces"]["src-" + role]
            pin = base["workspaces"]["src-" + role]
            if (
                row["filesystem_uuid"] != pin["kernel"]["filesystem_uuid"]
                or row["uid"] != pin["root_uid"]
            ):
                raise ValueError(
                    "Retained source pin differs from the measured dev-m1 image"
                )
            source = vm / ("m5-" + role + ".raw")
            if source.is_symlink() or source.stat().st_size != offline.DISK_BYTES:
                raise ValueError("Unexpected retained image")
            # Hard links would modify source inode metadata. Copy only independently owned data.
            dst = inner / ("src-" + role + ".raw")
            subprocess.run(
                ["cp", "--sparse=always", "--reflink=auto", "--", source, dst],
                check=True,
            )
            pin_hash = copy_digest(source)
            if copy_digest(dst) != pin_hash:
                raise ValueError(
                    "The working ext4 image differs from its parked source"
                )
            result["sources"][role] = {
                "image_sha256": pin_hash,
                "uid": row["uid"],
                "filesystem_uuid": row["filesystem_uuid"],
                "inventory": pin["inventory"],
                "workspace": row["workspace"],
            }
        disk = stage / "pool.raw"
        with disk.open("xb") as stream:
            stream.truncate(POOL_BYTES)
        offline.run(
            "mkfs.ext4",
            "-q",
            "-F",
            "-b",
            "4096",
            "-m",
            "0",
            "-L",
            POOL_SERIAL,
            "-U",
            result["filesystem_uuid"],
            "-E",
            "lazy_itable_init=0,lazy_journal_init=0",
            "-d",
            str(inner),
            str(disk),
        )
        for name in ("system", *DATA_DISKS):
            fmt = "qcow2" if name == "system" else "raw"
            offline.run(
                "qemu-img",
                "create",
                "-q",
                "-f",
                "qcow2",
                "-F",
                fmt,
                "-b",
                str(vm / (name + "." + fmt)),
                str(stage / (name + ".qcow2")),
            )
        shutil.rmtree(inner)
        (stage / "pool.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n"
        )
        ensure_idle(vm, cfg)
        stage.rename(out)
    return verify_layout(vm)


def sync_helpers(reader):
    # Publish complete files so a running periodic inspector never imports partial source.
    upload = """import hashlib, os, sys
from pathlib import Path
sys.path.insert(0, '/opt/nexpoly/scripts')
from openscience_per_user_dev import write_atomic
p = Path('/opt/nexpoly/ops/openscience-per-user-dev') / sys.argv[1]
data = sys.stdin.buffer.read()
if hashlib.sha256(data).hexdigest() != sys.argv[2]:
    raise ValueError('Guest helper checksum differs')
write_atomic(p, data, 0o755)
"""
    for name in ("water_pool.py", "water_monitor.py"):
        data = (OPS / name).read_bytes()
        reader.run(
            "python3",
            "-IBc",
            upload,
            name,
            hashlib.sha256(data).hexdigest(),
            input=data,
        )


def install(vm):
    """Provision only our running test VM, after verifying the pool's exact UUID locally."""
    ensure_no_holders(read_native(vm))
    config = json.loads((vm / "waterpool/pool.json").read_bytes())
    reader = StorageGuest(vm / "ssh.conf")
    reader.run("test", "-b", "/dev/disk/by-id/virtio-" + POOL_SERIAL)
    target = "/opt/nexpoly/ops/openscience-per-user-dev/water_pool.py"
    sync_helpers(reader)
    reader.run("python3", "-IB", target, "install", input=json.dumps(config).encode())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "install", "sync"))
    parser.add_argument("--vm", type=Path, required=True)
    parser.add_argument(
        "--evidence",
        type=Path,
        default=offline.ROOT
        / "docs/verification/openscience-m5-workspace-quota-20261123.json",
    )
    args = parser.parse_args()
    os.umask(0o077)
    vm = args.vm.resolve(strict=True)
    if args.action == "prepare":
        print(json.dumps(prepare(vm, args.evidence)))
    elif args.action == "sync":
        sync_helpers(StorageGuest(vm / "ssh.conf"))
    else:
        install(vm)

"""Stage the two parked dev-m1 quota images on one bounded, independently measured disk."""

import argparse
from contextlib import contextmanager
import fcntl
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
from openscience_per_user_dev import write_atomic

POOL_BYTES = 768 * 1024**2
POOL_SERIAL = "m5-waterpool"
DATA_DISKS = (*offline.DATA_DISKS, offline.BACKUP_DISK)
OPS = offline.ROOT / "ops/openscience-per-user-dev"
DEDICATED = {"kind": "dedicated"}


@contextmanager
def storage_lock(vm):
    with (vm / "storage.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def pool_profile(layout):
    digest = hashlib.sha256(json.dumps(layout, sort_keys=True).encode()).hexdigest()
    return {"kind": "waterpool", "filesystem_uuid": layout["filesystem_uuid"], "layout_sha256": digest}


def registered_pool(vm):
    profile = json.loads((vm / "vm.json").read_bytes()).get("storage")
    path = vm / "waterpool"
    if profile == DEDICATED and not (path.exists() or path.is_symlink()):
        return None
    if not isinstance(profile, dict) or profile.get("kind") != "waterpool":
        raise ValueError("Storage layout is unpinned or migration is incomplete; verify the pool before resuming")
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Registered water-pool is missing")
    layout = verify_layout(vm)
    if profile != pool_profile(layout):
        raise ValueError("Registered water-pool differs from its pin")
    return layout


def sync_directory(path):
    fd = os.open(path, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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
    manifest = root / "pool.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("Missing water-pool manifest")
    cfg = json.loads(manifest.read_bytes())
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


def prepare(vm, evidence, legacy_pool_uuid=None):
    with storage_lock(vm):
        return prepare_locked(vm, evidence, legacy_pool_uuid)


def prepare_locked(vm, evidence, legacy_pool_uuid):
    cfg = read_native(vm)
    ensure_idle(vm, cfg)
    out = vm / "waterpool"
    machine = json.loads((vm / "vm.json").read_bytes())
    profile = machine.get("storage")
    if isinstance(profile, dict) and profile.get("kind") == "waterpool":
        return registered_pool(vm)
    if profile != DEDICATED and not (
        profile is None and legacy_pool_uuid and out.is_dir() and not out.is_symlink()
    ):
        raise ValueError("An unpinned retained VM requires its existing pool and independently recorded UUID")
    base = json.loads(evidence.read_bytes())["storage_vm_cold_restart"]
    if (
        base["status"] != "passed"
        or not base["all_four_inventories_and_kernel_quotas_match"]
    ):
        raise ValueError("Verified retained A/B quota images are required")

    def register_pool():
        layout = verify_layout(vm)
        if legacy_pool_uuid and layout["filesystem_uuid"] != legacy_pool_uuid:
            raise ValueError("Existing pool differs from the independently recorded UUID")
        for role in ("a", "b"):
            row = cfg["workspaces"]["src-" + role]
            pin = layout["sources"][role]
            previous = base["workspaces"]["src-" + role]
            if (any(pin[k] != row[k] for k in ("uid", "workspace", "filesystem_uuid"))
                    or pin["inventory"] != previous["inventory"]
                    or pin["image_sha256"] != copy_digest(vm / ("m5-" + role + ".raw"))):
                raise ValueError("Pool source differs from the retained workspace evidence")
        machine["storage"] = pool_profile(layout)
        write_atomic(vm / "vm.json", json.dumps(machine, sort_keys=True).encode() + b"\n", 0o600)
        return layout

    if out.exists() or out.is_symlink():
        return register_pool()
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
        for path in stage.iterdir():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        sync_directory(stage)
        stage.rename(out)
        sync_directory(vm)
    return register_pool()


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
    for name in ("water_pool.py", "water_alerts.py", "water_monitor.py"):
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
    parser.add_argument("--legacy-pool-uuid", help="Previously recorded filesystem UUID when adopting a pre-pin pool")
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
        print(json.dumps(prepare(vm, args.evidence, args.legacy_pool_uuid)))
    elif args.action == "sync":
        sync_helpers(StorageGuest(vm / "ssh.conf"))
    else:
        install(vm)

"""Prove the backing device of both dev-m1 workspace volumes and control their allocation gates."""

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import quota  # noqa: E402

POOL = Path("/mnt/nexpoly-m5-pool")
ROOTS = Path("/mnt/openscience-m5-quota")
SERIAL = "m5-waterpool"
CONFIG = Path("/etc/nexpoly-m5-water-pool.json")
INITIAL = Path("/etc/nexpoly-m5-water-pool-initial.json")
HELPER = Path("/opt/nexpoly/ops/openscience-per-user-dev/water_pool.py")
NFS = "m5-native-nfs.service"
UNIT = "m5-water-pool.service"
MIB = 1024**2
POOL_BYTES = 768 * MIB
RELEASE_FILE = "m5-watermark-release.txt"
RELEASE_BYTES = 3 * MIB
# Ordinary flushed bytes charge real blocks in the sparse loop image. Lines are bounded
# so the native text preview can display this test file.
RELEASE_LINE = b"M5 shared disk deletion fixture ".ljust(1023, b".") + b"\n"
PROBE_FILE = ".m5-watermark-allocation-probe"
PROBE_DATA = b"M5 global allocation probe\n"
GROUP_QUOTA, GROUP_FLAGS, LIMIT_MASK = 1, (1 << 2) | (1 << 3), 1 | (1 << 2)


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def check(condition, reason):
    if not condition:
        raise ValueError(reason)


def pool_device():
    node = Path("/dev/disk/by-id/virtio-" + SERIAL).resolve(strict=True)
    check(stat.S_ISBLK(node.stat().st_mode), "The dedicated water-pool disk is missing")
    serial = (Path("/sys/class/block") / node.name / "serial").read_text().strip()
    check(
        serial == SERIAL and int(run("blockdev", "--getsize64", node)) == POOL_BYTES,
        "Unexpected global development disk",
    )
    return node


def pins():
    cfg = json.loads(CONFIG.read_bytes())
    check(
        cfg["format"] == "nexpoly-dev-m1-waterpool-v1"
        and cfg["pool_bytes"] == POOL_BYTES
        and set(cfg["sources"]) == {"a", "b"},
        "Unexpected pool binding",
    )
    check(
        str(UUID(cfg["filesystem_uuid"])) == cfg["filesystem_uuid"], "Invalid pool UUID"
    )
    return cfg


def block_for(path):
    dev = path.stat().st_dev
    return Path("/sys/dev/block") / f"{os.major(dev)}:{os.minor(dev)}"


def group_call(node, command, gid, value):
    if (
        quota._quotactl(
            (command << 8) | GROUP_QUOTA, os.fsencode(node), gid, ctypes.byref(value)
        )
        == -1
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(node))


def group_state(node, uid):
    status = quota._QuotaStatusV1(version=1)
    group_call(node, quota.Q_XGETQSTATV, uid, status)
    check(
        status.version == 1 and status.flags & GROUP_FLAGS == GROUP_FLAGS,
        "Kernel group quota is inactive",
    )
    limits = quota._DiskQuota()
    group_call(node, quota.Q_GETQUOTA, uid, limits)
    check(
        not limits.block_soft
        and not limits.inode_soft
        and limits.block_hard in (0, 1)
        and limits.inode_hard in (0, 1),
        "Unexpected pre-existing group policy",
    )
    pair = (limits.block_hard, limits.inode_hard)
    mode = {(0, 0): "open", (1, 1): "block"}.get(pair, "partial")
    return {
        "group_gate": mode,
        "group_hard_kib": limits.block_hard,
        "group_hard_inodes": limits.inode_hard,
        "group_used_bytes": limits.used_bytes,
        "group_used_inodes": limits.used_inodes,
    }


def verify_pool(cfg):
    node = pool_device()
    check(
        POOL.is_mount() and POOL.stat().st_dev == node.stat().st_rdev,
        "The measured filesystem is not the pool",
    )
    check(
        run("blkid", "-p", "-s", "UUID", "-o", "value", node) == cfg["filesystem_uuid"]
        and run("blkid", "-p", "-s", "TYPE", "-o", "value", node) == "ext4",
        "Unexpected pool UUID or type",
    )
    return node


def topology():
    cfg = pins()
    verify_pool(cfg)
    users = json.loads(Path("/inputs/spec.json").read_bytes())["users"]
    result = {}
    for role, owner in users.items():
        pin = cfg["sources"][role]
        root = ROOTS / ("src-" + role)
        image = POOL / ("src-" + role + ".raw")
        check(
            image.is_file()
            and not image.is_symlink()
            and image.stat().st_dev == POOL.stat().st_dev
            and image.stat().st_nlink == 1
            and image.stat().st_size == 3 * 1024**3,
            "Missing sparse quota image on the shared development disk",
        )
        block = block_for(root)
        backing = Path((block / "loop/backing_file").read_text().strip())
        check(
            backing == image and backing.samefile(image),
            "Workspace is not backed by its pinned image in the observed pool",
        )
        node, fs = quota._filesystem(root, owner, "src")
        q = quota._on_device(node, owner, fs)
        check(
            (q["filesystem_uuid"], q["uid"], q["hard_bytes"])
            == (pin["filesystem_uuid"], pin["uid"], 2_000_000_000)
            and owner["workspace"] == pin["workspace"],
            "Workspace identity or user quota changed",
        )
        result[role] = (
            node,
            {
                "uid": q["uid"],
                "filesystem_uuid": q["filesystem_uuid"],
                "user_hard_bytes": q["hard_bytes"],
                **group_state(node, q["uid"]),
            },
        )
    check(
        set(result) == {"a", "b"} and result["a"][0] != result["b"][0],
        "Expected two distinct user filesystems",
    )
    return cfg, result


def observation(cfg, roles):
    vfs = os.statvfs(POOL)
    return {
        "filesystem_uuid": cfg["filesystem_uuid"],
        "filesystem_type": "ext4",
        "total_bytes": vfs.f_blocks * vfs.f_frsize,
        "available_bytes": vfs.f_bavail * vfs.f_frsize,
        "workspaces": {r: row for r, (_, row) in roles.items()},
    }


def sample():
    return observation(*topology())


def gate(mode):
    check(mode in ("block", "open"), "Unexpected global allocation gate")
    cfg, roles = topology()
    target = int(mode == "block")
    for node, row in roles.values():
        if row["group_gate"] != mode:
            limits = quota._DiskQuota(
                block_hard=target, inode_hard=target, valid=LIMIT_MASK
            )
            group_call(node, quota.Q_SETQUOTA, row["uid"], limits)
        row.update(group_state(node, row["uid"]))
        check(row["group_gate"] == mode, "Group allocation gate failed kernel readback")
    return observation(cfg, roles)


def initial_receipt(cfg):
    canonical = json.dumps(
        cfg["sources"], sort_keys=True, separators=(",", ":")
    ).encode()
    return {
        "format": "nexpoly-m5-verified-pool-inputs-v1",
        "filesystem_uuid": cfg["filesystem_uuid"],
        "source_pins_sha256": hashlib.sha256(canonical).hexdigest(),
        "workspaces": {
            role: {k: row[k] for k in ("workspace", "uid", "filesystem_uuid")}
            for role, row in cfg["sources"].items()
        },
    }


def verify_initial(cfg, inspect_tree, publish):
    # The receipt lives on the operator system disk. After it is present the
    # working files may change normally while their physical ownership stays pinned.
    receipt = initial_receipt(cfg)
    if INITIAL.exists() or INITIAL.is_symlink():
        fd = os.open(INITIAL, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            check(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_nlink == 1
                and info.st_dev != POOL.stat().st_dev
                and json.load(handle) == receipt,
                "Unexpected initial pool verification receipt",
            )
        return
    check(
        INITIAL.parent.stat().st_dev != POOL.stat().st_dev,
        "Verification receipt must live outside the guarded pool",
    )
    users = json.loads(Path("/inputs/spec.json").read_bytes())["users"]
    for role, owner in users.items():
        check(
            inspect_tree(ROOTS / ("src-" + role) / "workspace", owner)
            == cfg["sources"][role]["inventory"],
            "Initial shared-disk workspace tree differs from the confirmed retained source",
        )
    publish(INITIAL, json.dumps(receipt, sort_keys=True).encode() + b"\n", 0o600)


def verify_ready(cfg):
    sys.path.insert(0, "/opt/nexpoly/scripts")
    from openscience_per_user_dev import write_atomic
    from openscience_m5_quota_bundle import inventory

    observed = sample()
    verify_initial(cfg, inventory, write_atomic)
    return observed


def mount_pool():
    cfg = pins()
    node = pool_device()
    POOL.mkdir(mode=0o700, exist_ok=True)
    check(not POOL.is_symlink(), "Unexpected mountpoint")
    if not POOL.is_mount():
        run("mount", "-t", "ext4", "-o", "nodev,nosuid,noexec", node, POOL)
    verify_pool(cfg)
    POOL.chmod(0o700)
    for role, pin in cfg["sources"].items():
        root = ROOTS / ("src-" + role)
        image = POOL / ("src-" + role + ".raw")
        check(
            not image.is_symlink()
            and image.is_file()
            and image.stat().st_dev == POOL.stat().st_dev
            and image.stat().st_size == 3 * 1024**3,
            "Unexpected quota image",
        )
        if root.is_mount() and (block_for(root) / "loop/backing_file").exists():
            check(
                Path((block_for(root) / "loop/backing_file").read_text().strip())
                == image,
                "Another loop image already holds this mount",
            )
            continue
        check(
            run("blkid", "-p", "-s", "UUID", "-o", "value", image)
            == pin["filesystem_uuid"],
            "The working image must retain the registered workspace UUID",
        )
        if root.is_mount():
            prior = Path("/dev/disk/by-id/virtio-m5-" + role).resolve(strict=True)
            check(
                root.stat().st_dev == prior.stat().st_rdev,
                "Unexpected existing source mount",
            )
            run("umount", root)
        # mount(8) allocates an autoclear loop device for this image; discard propagates frees to the pool.
        run(
            "mount",
            "-t",
            "ext4",
            "-o",
            "loop,usrquota,grpquota,discard,nodev,nosuid,noexec",
            image,
            root,
        )
    verify_ready(cfg)


def install(cfg):
    disk = pool_device()
    check(
        run("blkid", "-p", "-s", "UUID", "-o", "value", disk) == cfg["filesystem_uuid"],
        "Refusing to install against a different disk",
    )
    if CONFIG.exists():
        check(json.loads(CONFIG.read_bytes()) == cfg, "Water-pool binding has changed")
    else:
        with CONFIG.open("x") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(cfg, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
    active = subprocess.call(["systemctl", "is-active", "--quiet", UNIT]) == 0
    exported = subprocess.call(["systemctl", "is-active", "--quiet", NFS]) == 0
    if active and exported:
        return verify_ready(cfg)
    if not active:
        run("systemctl", "stop", NFS)
        (Path("/etc/systemd/system") / UNIT).write_text(
            "[Unit]\nDescription=Mount both dev-m1 quota images from the verified shared disk\n"
            f"Requires=m5-storage.service\nAfter=m5-storage.service\nBefore={NFS}\n"
            "[Service]\nType=oneshot\nRemainAfterExit=yes\n"
            f"ExecStart=/usr/bin/python3 -IB {HELPER} mount\n[Install]\nWantedBy=multi-user.target\n"
        )
        dropin = Path("/etc/systemd/system") / (NFS + ".d")
        dropin.mkdir(exist_ok=True)
        (dropin / "water-pool.conf").write_text(
            f"[Unit]\nRequires={UNIT}\nAfter={UNIT}\n"
        )
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "--now", UNIT)
    observed = verify_ready(cfg)
    run("systemctl", "start", NFS)
    return observed


def pressure(target=None):
    cfg, roles = topology()
    private = POOL / ".m5-global-drill"
    private.mkdir(mode=0o700, exist_ok=True)
    info = private.lstat()
    check(
        stat.S_ISDIR(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o700
        and info.st_uid == 0,
        "Unexpected pressure directory",
    )
    path = private / "pressure.dat"
    if not path.exists() and target is None:
        return observation(cfg, roles)
    flags = os.O_NOFOLLOW | os.O_RDWR | (os.O_CREAT if target is not None else 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "r+b") as file:
        info = os.fstat(fd)
        check(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == 0
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_dev == POOL.stat().st_dev,
            "Unexpected pressure file",
        )
        fcntl.flock(fd, fcntl.LOCK_EX)
        if target is None:
            path.unlink()
        else:
            check(
                32 <= target <= 512,
                "Pressure target must leave 32–512 MiB on this dedicated test disk",
            )
            spare = observation(cfg, roles)["available_bytes"]
            change = spare - target * MIB
            size = max(0, info.st_size + change)
            if size > info.st_size:
                os.posix_fallocate(fd, info.st_size, size - info.st_size)
            else:
                file.truncate(size)
            os.fsync(fd)
    return observation(cfg, roles)


def ensure_fixture(path, data):
    try:
        with path.open("xb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
    except FileExistsError:
        pass
    meta = path.lstat()
    check(
        stat.S_ISREG(meta.st_mode)
        and stat.S_IMODE(meta.st_mode) == 0o600
        and (meta.st_uid, meta.st_gid) == (os.getuid(), os.getgid())
        and meta.st_nlink == 1
        and meta.st_size == len(data)
        and path.read_bytes() == data,
        "Unexpected pre-existing fixture",
    )


def fixtures(action, mode=None):
    sample()
    sys.path[:0] = ["/opt/nexpoly/scripts", "/inputs"]
    from quota_guest import run_as

    release_data = (
        RELEASE_LINE * (RELEASE_BYTES // len(RELEASE_LINE))
        if action in ("seed", "cleanup")
        else None
    )
    if action == "seed":
        # Discard unused extents in this verified test *copy* before the fixture is allocated,
        # so saving new data consumes physical blocks on the shared disk too.
        root = ROOTS / "src-a"
        if not (root / "workspace/files/projects/same-project" / RELEASE_FILE).exists():
            run("fstrim", root)

    def exercise(role, uid):
        folder = ROOTS / ("src-" + role) / "workspace/files/projects/same-project"
        probe = folder / PROBE_FILE
        release = folder / RELEASE_FILE
        if action == "seed":
            ensure_fixture(probe, PROBE_DATA)
            if role == "a":
                ensure_fixture(release, release_data)
                st = release.stat()
                check(
                    st.st_size == RELEASE_BYTES and st.st_blocks * 512 >= st.st_size,
                    "Release fixture is sparse",
                )
                return {
                    "seeded": True,
                    "uid": uid,
                    "release_bytes": st.st_size,
                    "release_allocated_bytes": st.st_blocks * 512,
                }
            return {"seeded": True, "uid": uid}
        if action == "cleanup":
            paths = [(probe, PROBE_DATA)]
            if role == "a":
                paths.append((release, release_data))
            for path, expected in paths:
                if path.exists():
                    ensure_fixture(path, expected)
                    path.unlink()
            return {"removed": True, "uid": uid}
        ensure_fixture(probe, PROBE_DATA)
        candidate = folder / (".m5-global-create-" + uuid4().hex)
        created = allocated = 0
        try:
            with candidate.open("xb") as file:
                file.write(b"x" * 4096)
                file.flush()
                os.fsync(file.fileno())
        except OSError as exc:
            created = exc.errno
        finally:
            candidate.unlink(missing_ok=True)
        with probe.open("r+b", buffering=0) as file:
            try:
                os.posix_fallocate(file.fileno(), 65536, 4096)
                os.fsync(file.fileno())
            except OSError as exc:
                allocated = exc.errno
            finally:
                file.truncate(len(PROBE_DATA))
                os.fsync(file.fileno())
        expected = errno.EDQUOT if mode == "block" else 0
        check(
            (created, allocated) == (expected, expected),
            f"Unexpected kernel result: {created}/{allocated}",
        )
        return {
            "uid": uid,
            "gid": os.getgid(),
            "create_errno": created,
            "allocate_existing_errno": allocated,
            "existing_fixture_readable": probe.read_bytes() == PROBE_DATA,
        }

    users = json.loads(Path("/inputs/spec.json").read_bytes())["users"]
    return {
        role: run_as(owner, exercise, role, owner["uid"])
        for role, owner in users.items()
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("sample", "mount", "install", "seed", "cleanup", "release"):
        commands.add_parser(name)
    for name in ("gate", "probe"):
        commands.add_parser(name).add_argument("mode", choices=("block", "open"))
    commands.add_parser("pressure").add_argument("available_mib", type=int)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == "install":
        result = install(json.load(sys.stdin))
    elif args.command == "mount":
        mount_pool()
        result = sample()
    elif args.command == "sample":
        result = sample()
    elif args.command == "gate":
        result = gate(args.mode)
    elif args.command in ("pressure", "release"):
        result = pressure(getattr(args, "available_mib", None))
    else:
        result = fixtures(args.command, getattr(args, "mode", None))
    print(json.dumps(result, sort_keys=True))

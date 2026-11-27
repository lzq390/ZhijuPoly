"""Manage byte hard limits for registered workspace owners on dedicated ext4 filesystems."""

import argparse
import ctypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from uuid import UUID


PILOT_HARD_BYTES = 2_000_000_000
KIB = 1024
FORMAT = "nexpoly-ext4-user-quota-v1"
# Linux UAPI, linux/quota.h and linux/dqblk_xfs.h. quotactl takes QCMD(command, USRQUOTA).
Q_GETQUOTA, Q_SETQUOTA = 0x800007, 0x800008
Q_XGETQSTATV = (ord("X") << 8) + 8
QIF_BLIMITS = 1
USER_ACCOUNTING, USER_ENFORCEMENT = 1, 2


class QuotaError(ValueError):
    pass


class _DiskQuota(ctypes.Structure):
    _fields_ = [(field, ctypes.c_uint64) for field in (
        "block_hard", "block_soft", "used_bytes", "inode_hard", "inode_soft",
        "used_inodes", "block_grace_expires", "inode_grace_expires",
    )] + [("valid", ctypes.c_uint32)]


class _QuotaStatusV1(ctypes.Structure):
    # Linux fs_quota_statv is 160 bytes. The remaining fields report quota file and grace statistics.
    _fields_ = [("version", ctypes.c_int8), ("pad", ctypes.c_uint8), ("flags", ctypes.c_uint16),
                ("statistics", ctypes.c_uint8 * 156)]


_quotactl = ctypes.CDLL(None, use_errno=True).quotactl
_quotactl.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
_quotactl.restype = ctypes.c_int


def _call(device, command, uid, data):
    if _quotactl(command << 8, os.fsencode(device), uid, ctypes.byref(data)) == -1:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(device))


def _identity(binding):
    if not isinstance(binding, dict):
        raise QuotaError("Expected a dev-m1 workspace binding")
    keys = ("environment", "user_id", "workspace", "uid")
    result = {key: binding.get(key) for key in keys}
    pilot = result["environment"] == "dev-m1" and result["user_id"] in ("test-user-a", "test-user-b")
    candidate = (result["environment"] == "deploy-m6" and isinstance(result["user_id"], str)
                 and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", result["user_id"]))
    if (not (pilot or candidate)
            or not isinstance(result["workspace"], str) or not re.fullmatch("[0-9a-f]{32}", result["workspace"])
            or type(result["uid"]) is not int or not 210000 <= result["uid"] < 100210000
            or binding.get("gid", result["uid"]) != result["uid"]):
        raise QuotaError("Invalid workspace quota binding")
    return result


def volume_label(binding, stage):
    owner = _identity(binding)
    if stage not in ("src", "dst"):
        raise QuotaError("Unknown workspace storage phase")
    if owner["environment"] == "dev-m1":
        return f"m5quota-{stage}-{owner['user_id'][-1]}"
    return f"nc{stage[0]}-{snapshot_digest(owner)[:12]}"


def _hard_kib(value):
    if type(value) is not int or value <= 0 or value > PILOT_HARD_BYTES or value % KIB:
        raise QuotaError("Expected a positive whole-KiB hard limit within the 2 GB development pilot")
    return value // KIB


def _unescape_mount(field):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), field)


def check_source_mount(source, mount):
    if (
        source.is_symlink()
        or not source.is_dir()
        or source.stat().st_dev != mount.stat().st_dev
    ):
        raise ValueError("Expected a workspace directory on the source quota volume")
    workspace = source.resolve()
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        path = _unescape_mount(line.split()[4])
        if Path(path).is_relative_to(workspace):
            raise ValueError("Unexpected nested mount inside the workspace")


def _filesystem(mount, owner, role=None):
    root = Path(mount).resolve(strict=True)
    if root == Path("/") or not root.is_dir():
        raise QuotaError("Expected a dedicated ext4 test volume mountpoint")
    st_dev = root.stat().st_dev
    major_minor = f"{os.major(st_dev)}:{os.minor(st_dev)}"
    # Exclude bind mounts of subdirectories and stale/covered mount entries.
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        fields = left.split()
        if _unescape_mount(fields[4]) == str(root):
            mounts.append((fields, right.split()))
    if len(mounts) != 1:
        raise QuotaError("Expected exactly one mount at the supplied test volume root")
    fields, filesystem = mounts[0]
    if (fields[2] != major_minor or fields[3] != "/" or filesystem[0] != "ext4"
            or "rw" not in fields[5].split(",")):
        raise QuotaError("Expected the writable root of a dedicated ext4 device")
    device = Path(_unescape_mount(filesystem[1])).resolve(strict=True)
    info = device.stat()
    if not stat.S_ISBLK(info.st_mode) or info.st_rdev != st_dev:
        raise QuotaError("Mounted filesystem and local block device differ")
    result = subprocess.run(["blkid", "-p", "-o", "export", str(device)], check=True,
                            capture_output=True, text=True, timeout=5)
    metadata = dict(line.split("=", 1) for line in result.stdout.splitlines())
    labels = {volume_label(owner, stage) for stage in ([role] if role else ["src", "dst"])}
    if metadata.get("TYPE") != "ext4" or metadata.get("LABEL") not in labels:
        raise QuotaError("Device label does not identify this owner's independent volume")
    fs_uuid = str(UUID(metadata["UUID"]))
    status = _QuotaStatusV1(version=1)
    _call(device, Q_XGETQSTATV, 0, status)
    accounting = bool(status.flags & USER_ACCOUNTING)
    enforced = bool(status.flags & USER_ENFORCEMENT)
    if status.version != 1 or not accounting or not enforced:
        raise QuotaError("Kernel user quota accounting and enforcement must both be enabled")
    return device, {"filesystem_type": "ext4", "filesystem_uuid": fs_uuid, "volume_label": metadata["LABEL"],
                    "accounting": accounting, "enforced": enforced}


def _on_device(device, owner, filesystem):
    quota = _DiskQuota()
    _call(device, Q_GETQUOTA, owner["uid"], quota)
    if quota.block_soft or quota.inode_soft or quota.inode_hard:
        raise QuotaError("Dedicated pilot volume already has an additional quota policy")
    return {**filesystem, "uid": owner["uid"], "used_bytes": quota.used_bytes,
            "hard_kib": quota.block_hard, "hard_bytes": quota.block_hard * KIB}


def read(mount, identity):
    owner = _identity(identity)
    device, fs = _filesystem(mount, owner)
    return _on_device(device, owner, fs)


def _apply(device, owner, fs, hard_bytes):
    blocks = _hard_kib(hard_bytes)
    current = _on_device(device, owner, fs)
    if current["hard_kib"] == blocks:
        return current
    if current["hard_kib"] or current["used_bytes"] > hard_bytes:
        raise QuotaError("Existing quota or usage conflicts with requested hard limit")
    quota = _DiskQuota(block_hard=blocks, valid=QIF_BLIMITS)
    _call(device, Q_SETQUOTA, owner["uid"], quota)
    effective = _on_device(device, owner, fs)
    if effective["hard_kib"] != blocks:
        raise QuotaError("Kernel did not apply requested user block limit")
    return effective


def install(mount, identity, hard_bytes=PILOT_HARD_BYTES):
    owner = _identity(identity)
    device, fs = _filesystem(mount, owner, "src")
    return _apply(device, owner, fs, hard_bytes)


def snapshot(mount, identity):
    owner = _identity(identity)
    observed = read(mount, owner)
    _hard_kib(observed["hard_bytes"])
    return {"format": FORMAT, "identity": owner, "source_filesystem_uuid": observed["filesystem_uuid"],
            "hard_bytes": observed["hard_bytes"]}


def snapshot_digest(snapshot):
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def restore(mount, identity, snapshot, expected_sha256):
    owner = _identity(identity)
    if (not isinstance(expected_sha256, str) or not re.fullmatch("[0-9a-f]{64}", expected_sha256)
            or not hmac.compare_digest(snapshot_digest(snapshot), expected_sha256)):
        raise QuotaError("Quota snapshot differs from the trusted archive pin")
    if (not isinstance(snapshot, dict) or set(snapshot) != {"format", "identity", "source_filesystem_uuid", "hard_bytes"}
            or snapshot["format"] != FORMAT or snapshot["identity"] != owner):
        raise QuotaError("Quota snapshot and requested workspace ownership differ")
    source_uuid = str(UUID(snapshot["source_filesystem_uuid"]))
    _hard_kib(snapshot["hard_bytes"])
    device, fs = _filesystem(mount, owner, "dst")
    if fs["filesystem_uuid"] == source_uuid:
        raise QuotaError("Restore requires an independently formatted destination filesystem")
    return _apply(device, owner, fs, snapshot["hard_bytes"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mount", required=True, type=Path)
    parser.add_argument("--binding", required=True, type=Path,
                        help="Trusted environment/user_id/workspace/uid binding JSON")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("read")
    commands.add_parser("snapshot")
    configure = commands.add_parser("install")
    configure.add_argument("--hard-bytes", type=int, default=PILOT_HARD_BYTES)
    recover = commands.add_parser("restore")
    recover.add_argument("--snapshot", required=True, type=Path)
    recover.add_argument("--snapshot-sha256", required=True)
    args = parser.parse_args()
    try:
        binding = json.loads(args.binding.read_bytes())
        if args.command == "install":
            result = install(args.mount, binding, args.hard_bytes)
        elif args.command == "restore":
            result = restore(args.mount, binding, json.loads(args.snapshot.read_bytes()), args.snapshot_sha256)
        elif args.command == "snapshot":
            result = snapshot(args.mount, binding)
        else:
            result = read(args.mount, binding)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, f"dev-m1 quota: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

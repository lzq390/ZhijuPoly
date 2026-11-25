"""Lease kernel allocation gates on the two claimed deploy-m6 source volumes."""

import argparse
import ctypes
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import UUID

OPS = Path(__file__).resolve().parent
sys.path[:0] = [str(OPS), str(OPS.parents[1] / "scripts")]
import quota  # noqa: E402
import quota_nfs  # noqa: E402
import water_pool as kernel  # noqa: E402
from openscience_per_user_dev import write_atomic  # noqa: E402

CONFIG = Path("/etc/nexpoly-deploy-m6-gate.json")
RUN = Path("/run/nexpoly-deploy-m6-gate")
SERVICE = "nexpoly-deploy-m6-gate.service"
LEASE_SECONDS = 20
Q_QUOTAON, QFMT_VFS_V1 = 0x800002, 4


def save(path, value, mode=0o600):
    write_atomic(path, json.dumps(value, sort_keys=True).encode(), mode)


def owners(claim):
    actual = quota_nfs.pinned_guest_claim(
        "src", claim["deployment"], claim["registry_sha256"], acquire=False
    )
    spec = json.loads(Path("/inputs/spec.json").read_bytes())
    rows = spec["users"]
    if (
        spec["environment"] != "deploy-m6"
        or actual != claim
        or set(rows) != {"a", "b"}
        or any(
            quota._identity(row)["environment"] != "deploy-m6" for row in rows.values()
        )
        or len({rows[r]["uid"] for r in rows}) != 2
    ):
        raise ValueError("Candidate gate requires the two claimed deploy-m6 owners")
    return rows


def volume(role, owner, wanted_uuid, enable=False):
    mount = Path(quota_nfs.GUEST_ROOT) / ("src-" + role)
    node, fs = quota._filesystem(mount, quota._identity(owner), "src")
    expected = Path("/dev/disk/by-id/virtio-m5-" + role)
    q = quota._on_device(node, owner, fs)
    if (
        not expected.samefile(node)
        or q["filesystem_uuid"] != wanted_uuid
        or q["hard_bytes"] != quota.PILOT_HARD_BYTES
        or (mount / "workspace").stat().st_gid != owner["uid"]
    ):
        raise ValueError(
            "Expected the registered source serial, filesystem, group and user quota"
        )
    if enable:
        status = quota._QuotaStatusV1(version=1)
        kernel.group_call(node, quota.Q_XGETQSTATV, owner["uid"], status)
        if status.flags & kernel.GROUP_FLAGS != kernel.GROUP_FLAGS:
            if status.version != 1 or not status.flags & (1 << 2):
                raise ValueError("The source volume lacks native ext4 group accounting")
            # ext4's internal group inode already accounts for all files. Enable
            # enforcement without replacing the existing quota inode or remounting.
            target = ctypes.c_char_p(os.fsencode(mount))
            if (
                quota._quotactl(
                    (Q_QUOTAON << 8) | kernel.GROUP_QUOTA,
                    os.fsencode(node),
                    QFMT_VFS_V1,
                    ctypes.cast(target, ctypes.c_void_p),
                )
                == -1
            ):
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error), str(mount))
    return node, dict(
        uid=q["uid"],
        filesystem_uuid=wanted_uuid,
        user_hard_bytes=q["hard_bytes"],
        **kernel.group_state(node, owner["uid"]),
    )


def apply(claim, mode, *, enable=False):
    rows = owners(claim)
    confirmed, errors = {}, {}
    for role, owner in rows.items():
        try:
            node, row = volume(role, owner, claim["filesystem_uuids"][role], enable)
            if mode != "inspect":
                kernel.set_group_gate(node, row, mode)
            confirmed[role] = row
        except kernel.VOLUME_ERRORS as exc:
            errors[role] = type(exc).__name__
            print(f"candidate gate {role}: {exc}", file=sys.stderr, flush=True)
    if errors and mode == "open":
        apply(claim, "block", enable=enable)
    return {"workspaces": confirmed, "errors": errors}


def guarded(claim, action):
    with (RUN / "lock").open("a") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        lease = RUN / "deadline"
        if action == "expiry":
            if lease.exists() and float(lease.read_text()) > time.monotonic():
                return {}
            action = "block"
        if action in ("open", "block"):
            # Never inherit an earlier lease across a partial-open failure.
            lease.unlink(missing_ok=True)
        result = apply(claim, action, enable=True)
        if result["errors"] and action == "open":
            raise ValueError("Candidate gates could not be opened together")
        if action == "open":
            write_atomic(lease, str(time.monotonic() + LEASE_SECONDS).encode(), 0o600)
        return result


def install(config):
    owners(config["claim"])
    if CONFIG.exists() and json.loads(CONFIG.read_bytes()) != config:
        raise ValueError(
            "Guest watchdog already belongs to another deployment or medium"
        )
    save(CONFIG, config)
    units = Path("/etc/systemd/system")
    script = Path(__file__).resolve()
    unit = f"""[Unit]
Description=Expire allocation leases for the deploy-m6 source volumes
Requires=m5-storage.service
After=m5-storage.service
Before=m5-native-nfs.service
[Service]
Type=notify
ExecStart=/usr/bin/python3 -IB {script} serve
ExecStopPost=/usr/bin/python3 -IB {script} stopped
Restart=on-failure
RestartSec=1
[Install]
WantedBy=multi-user.target
"""
    write_atomic(units / SERVICE, unit.encode(), 0o644)
    dep = units / "m5-native-nfs.service.d"
    dep.mkdir(exist_ok=True)
    write_atomic(
        dep / "deploy-m6-allocation.conf",
        f"[Unit]\nRequires={SERVICE}\nAfter={SERVICE}\n".encode(),
        0o644,
    )
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", SERVICE], check=True, timeout=65)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("install", "inspect", "open", "block", "serve", "stopped")
    )
    parser.add_argument("identity", nargs="*")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError("Expected candidate storage operator")
    os.umask(0o077)
    RUN.mkdir(mode=0o700, exist_ok=True)
    if args.action in ("serve", "stopped"):
        config = json.loads(CONFIG.read_bytes())
        claim = config["claim"]
        result = guarded(claim, "block")
        if result["errors"]:
            raise ValueError("Could not close both candidate gates")
        if args.action == "serve":
            address = os.environ["NOTIFY_SOCKET"]
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
                notify.sendto(
                    b"READY=1",
                    ("\0" + address[1:]) if address.startswith("@") else address,
                )
            while True:
                time.sleep(2)
                guarded(claim, "expiry")
        return
    deployment, root_hash, fs_uuid = args.identity
    claim = quota_nfs.pinned_guest_claim("src", deployment, root_hash, acquire=False)
    config = {"claim": claim, "host_filesystem_uuid": str(UUID(fs_uuid))}
    if args.action == "install":
        install(config)
    if config != json.loads(CONFIG.read_bytes()):
        raise ValueError("Different candidate or protected host filesystem")
    if args.action != "block":
        subprocess.run(["systemctl", "is-active", "--quiet", SERVICE], check=True)
    print(
        json.dumps(
            guarded(claim, "inspect" if args.action == "install" else args.action)
        )
    )


if __name__ == "__main__":
    main()

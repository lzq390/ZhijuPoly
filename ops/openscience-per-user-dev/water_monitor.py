"""Measure the shared M5 pool and apply one hysteretic write policy to both workspace volumes."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
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
sys.path.insert(0, str(OPS))
sys.path.insert(0, str(OPS.parents[1] / "scripts"))
from openscience_per_user_dev import write_atomic  # noqa: E402
import quota_nfs  # noqa: E402

ROOT = Path("/var/lib/nexpoly-m5-watermark")
SERVICE = "m5-global-disk.service"
CADENCE_SECONDS = 2
HISTORY = 64


def read(path):
    return json.loads(path.read_bytes())


def publish(path, data):
    write_atomic(path, json.dumps(data, sort_keys=True).encode() + b"\n", 0o600)


def policy_for(claim, filesystem_uuid, low_bytes, recover_bytes):
    if (
        claim["phase"] != "src"
        or str(UUID(filesystem_uuid)) != filesystem_uuid
        or type(low_bytes) is not int
        or type(recover_bytes) is not int
        or not 0 < low_bytes < recover_bytes
    ):
        raise ValueError(
            "Expected a source pool UUID and ordered positive byte thresholds"
        )
    return dict(
        claim=claim,
        filesystem_uuid=filesystem_uuid,
        low_bytes=low_bytes,
        recover_bytes=recover_bytes,
    )


def checked_sample(policy, sample):
    if (
        sample["filesystem_uuid"] != policy["filesystem_uuid"]
        or sample["filesystem_type"] != "ext4"
        or type(sample["available_bytes"]) is not int
        or type(sample["total_bytes"]) is not int
        or not 0 <= sample["available_bytes"] <= sample["total_bytes"]
        or policy["recover_bytes"] >= sample["total_bytes"]
        or set(sample["workspaces"]) != {"a", "b"}
        or any(
            sample["workspaces"][r]["filesystem_uuid"] != u
            for r, u in policy["claim"]["filesystem_uuids"].items()
        )
    ):
        raise ValueError(
            "Live pool or workspace volume differs from the registered policy"
        )
    return sample


@contextmanager
def locked(root):
    with (root / "monitor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def deliver(root, event):
    """The private operator feed is independent of status. Replay after a crash publishes the same ID."""
    if not event:
        return
    spool = root / "alerts"
    spool.mkdir(mode=0o700, exist_ok=True)
    seq = event["sequence"]
    dest = spool / f"{seq:012d}.json"
    if dest.exists():
        if read(dest) != event:
            raise ValueError(
                "Persisted alert receipt differs from its recorded transition"
            )
    else:
        # Only the exclusive monitor lock can publish here. Atomic write also fsyncs the directory.
        publish(dest, event)
        print(
            json.dumps({"M5_DISK_ALERT": event}, sort_keys=True),
            file=sys.stderr,
            flush=True,
        )
    if seq > HISTORY:
        (spool / f"{seq - HISTORY:012d}.json").unlink(missing_ok=True)


def low_water(policy, previous, free):
    # Require the recovery margin after a low reading or an unavailable device.
    return free <= policy["low_bytes"] or (
        previous in ("low", "unavailable") and free < policy["recover_bytes"]
    )


def observe(root, policy, pool, *, stopped=False):
    """Serialize monitor polls with synchronous write and start admission. The caller holds monitor.lock."""
    state_file = root / "state.json"
    previous = read(state_file) if state_file.exists() else {}
    if previous and previous["filesystem_uuid"] != policy["filesystem_uuid"]:
        raise ValueError("Watermark state belongs to a different filesystem")
    deliver(root, previous.get("last_transition"))
    old = previous.get("state")
    sample = None
    try:
        sample = checked_sample(policy, pool.sample())
        if stopped:
            raise ValueError("Disk monitor is inactive")
        low = low_water(policy, old, sample["available_bytes"])
        state = "low" if low else "normal"
        target = "block" if low else "open"
        if any(v["group_gate"] != target for v in sample["workspaces"].values()):
            sample = checked_sample(policy, pool.gate(target))
            # Background I/O can consume the recovered margin while the two
            # kernel limits are opening. Check the returned physical sample.
            if target == "open" and low_water(policy, old, sample["available_bytes"]):
                state, target = "low", "block"
                sample = checked_sample(policy, pool.gate(target))
        if any(v["group_gate"] != target for v in sample["workspaces"].values()):
            raise ValueError(
                "Both workspace block and inode caps must confirm the same gate"
            )
        reason = None
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        state, reason = (
            "unavailable",
            "monitor_inactive" if stopped else type(exc).__name__,
        )
        # Never report admission open after a partial quota update or wrong/missing device.
        # gate() revalidates physical devices before modifying either kernel quota.
        try:
            sample = checked_sample(policy, pool.sample())
            if any(v["group_gate"] != "block" for v in sample["workspaces"].values()):
                sample = checked_sample(policy, pool.gate("block"))
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
            subprocess.SubprocessError,
        ):
            sample = None
    now = datetime.now(timezone.utc).isoformat()
    last = previous.get("last_transition")
    seq = previous.get("sequence", 0)
    if old != state and (old is not None or state != "normal"):
        seq += 1
        last = dict(
            id=f"{policy['filesystem_uuid']}:{seq}",
            sequence=seq,
            previous=old,
            state=state,
            at=now,
            notification={
                "low": "firing",
                "normal": "resolved",
                "unavailable": "degraded",
            }[state],
            available_bytes=sample["available_bytes"] if sample else None,
            low_bytes=policy["low_bytes"],
            recover_bytes=policy["recover_bytes"],
            filesystem_uuid=policy["filesystem_uuid"],
            reason=reason,
        )
    current = dict(
        configured=True,
        state=state,
        write_allowed=state == "normal",
        sampled_at=now,
        poll_interval_seconds=CADENCE_SECONDS,
        sequence=seq,
        last_transition=last,
        reason=reason,
        filesystem_uuid=policy["filesystem_uuid"],
        low_bytes=policy["low_bytes"],
        recover_bytes=policy["recover_bytes"],
        total_bytes=sample["total_bytes"] if sample else None,
        available_bytes=sample["available_bytes"] if sample else None,
        workspaces=sample["workspaces"] if sample else {},
    )
    publish(state_file, current)
    deliver(root, last)
    return current


def stop_guard(root, policy, pool, *, shutting_down):
    # A broken/full control filesystem can itself stop the monitor. Close the
    # kernel gates before touching its status file or sending any notification.
    try:
        sample = checked_sample(policy, pool.sample())
        if any(v["group_gate"] != "block" for v in sample["workspaces"].values()):
            checked_sample(policy, pool.gate("block"))
    finally:
        # During an orderly VM shutdown systemd has already stopped the NFS
        # export. Keep the last observed disk condition and its resolved receipt.
        if not shutting_down:
            observe(root, policy, pool, stopped=True)


def owner(policy):
    claim = policy["claim"]
    observed = quota_nfs.pinned_guest_claim(
        "src", claim["deployment"], claim["registry_sha256"], acquire=False
    )
    if observed != claim:
        raise ValueError("Storage monitor has a different workspace owner")


def setup_service():
    units = Path("/etc/systemd/system")
    script = Path(__file__).resolve()
    unit = f"""[Unit]
Description=Monitor the global free space on the development OpenScience pool
Requires=m5-water-pool.service
After=m5-water-pool.service
Before=m5-native-nfs.service
[Service]
Type=notify
ExecStart=/usr/bin/python3 -IB {script} serve
ExecStopPost=/usr/bin/python3 -IB {script} stopped
Restart=on-failure
RestartSec=2
[Install]
WantedBy=multi-user.target
"""
    dep = units / "m5-native-nfs.service.d"
    dep.mkdir(exist_ok=True)
    write_atomic(units / SERVICE, unit.encode(), 0o644)
    write_atomic(
        dep / "global-disk.conf",
        f"[Unit]\nRequires={SERVICE}\nAfter={SERVICE}\n".encode(),
        0o644,
    )
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", SERVICE], check=True)


def main():
    import water_pool

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("configure", "preflight", "sample", "observed", "alerts"):
        p = commands.add_parser(name)
        p.add_argument("deployment")
        p.add_argument("registry_sha256")
        p.add_argument("filesystem_uuid")
        p.add_argument("low_bytes", type=int)
        p.add_argument("recover_bytes", type=int)
    commands.add_parser("serve")
    commands.add_parser("stopped")
    args = parser.parse_args()
    os.umask(0o077)
    if os.geteuid() != 0:
        raise PermissionError(
            "The storage monitor requires the private guest control plane"
        )
    cfg = ROOT / "policy.json"
    if args.action in ("serve", "stopped"):
        policy = read(cfg)
        owner(policy)
        if args.action == "stopped":
            lifecycle = subprocess.run(
                ["systemctl", "is-system-running"], capture_output=True, text=True
            )
            shutting_down = (
                os.environ.get("SERVICE_RESULT") == "success"
                and lifecycle.stdout.strip() == "stopping"
            )
            with locked(ROOT):
                stop_guard(ROOT, policy, water_pool, shutting_down=shutting_down)
            return
        first = True
        while True:
            with locked(ROOT):
                observation = observe(ROOT, policy, water_pool)
            if first and observation["state"] != "unavailable":
                addr = os.environ["NOTIFY_SOCKET"]
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
                    notify.sendto(
                        b"READY=1", ("\0" + addr[1:]) if addr.startswith("@") else addr
                    )
                first = False
            time.sleep(CADENCE_SECONDS)
    claim = quota_nfs.pinned_guest_claim(
        "src", args.deployment, args.registry_sha256, acquire=False
    )
    policy = policy_for(claim, args.filesystem_uuid, args.low_bytes, args.recover_bytes)
    if cfg.exists() and read(cfg) != policy:
        raise ValueError(
            "The shared pool already belongs to a different monitoring policy"
        )
    if args.action in ("preflight", "configure"):
        sample = checked_sample(policy, water_pool.sample())
        if args.action == "preflight":
            print(json.dumps(sample))
            return
        ROOT.mkdir(mode=0o700, exist_ok=True)
        if ROOT.is_symlink() or ROOT.stat().st_uid != 0 or ROOT.stat().st_mode & 0o077:
            raise PermissionError("Expected a private root-owned control directory")
        if ROOT.stat().st_dev == water_pool.POOL.stat().st_dev:
            raise ValueError(
                "Watermark state and alerts require storage outside the protected pool"
            )
        with locked(ROOT):
            if cfg.exists() and read(cfg) != policy:
                raise ValueError("Concurrent storage policy registration differs")
            publish(cfg, policy)
        setup_service()
    if read(cfg) != policy:
        raise ValueError("No matching installed global pool policy")
    with locked(ROOT):
        if args.action == "alerts":
            result = {
                "configured": True,
                "filesystem_uuid": policy["filesystem_uuid"],
                "alerts": [read(p) for p in sorted((ROOT / "alerts").glob("*.json"))],
            }
        elif args.action == "observed":
            result = read(ROOT / "state.json")
        else:
            active = (
                subprocess.run(
                    ["systemctl", "is-active", "--quiet", SERVICE]
                ).returncode
                == 0
            )
            result = observe(ROOT, policy, water_pool, stopped=not active)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

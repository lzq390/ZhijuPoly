"""Sample the deploy-m6 host backing filesystem and lease both guest quota gates."""

import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))
import quota_nfs  # noqa: E402
import water_monitor  # noqa: E402
import water_pool  # noqa: E402

GUEST = "/opt/nexpoly/ops/openscience-per-user-dev/candidate_gate.py"


class CandidatePool:
    def __init__(self, registry, policy):
        self.registry = registry
        self.policy = policy
        self.keys = registry.quota_keys()
        self.claim = registry.data["native_claim"]
        self.control = registry.root / "host-watermark"

    def space(self):
        # The kernel's mount ID, the by-UUID block device and *both* open image
        # inodes must agree. Never substitute the registry/guest/Docker free space.
        media = [self.registry.data["host_media"][key] for key in self.keys]
        if (
            media[0]["device"] != media[1]["device"]
            or media[0]["inode"] == media[1]["inode"]
            or any(
                self.registry.data["nfs_endpoints"][key]["slot"] != "src-" + role
                for role, key in zip("ab", self.keys)
            )
        ):
            raise ValueError(
                "Expected two distinct source images on the same measured filesystem"
            )
        device = (Path("/dev/disk/by-uuid") / self.policy["filesystem_uuid"]).stat()
        dev = media[0]["device"]
        types = {
            line.split(" - ", 1)[1].split()[0]
            for line in Path("/proc/self/mountinfo").read_text().splitlines()
            if line.split()[2] == dev
        }
        if (
            not stat.S_ISBLK(device.st_mode)
            or types != {"ext4"}
            or dev != f"{os.major(device.st_rdev)}:{os.minor(device.st_rdev)}"
            or self.registry.root.stat().st_dev == device.st_rdev
        ):
            raise ValueError("Unexpected host filesystem UUID, type or control volume")
        samples = [quota_nfs.host_space(pin) for pin in media]
        if (
            samples[0][1] != samples[1][1]
            or self.policy["recover_bytes"] >= samples[0][1]
        ):
            raise ValueError("Invalid target size or recovery threshold")
        return dict(
            filesystem_uuid=self.policy["filesystem_uuid"],
            filesystem_type="ext4",
            available_bytes=min(value[0] for value in samples),
            total_bytes=samples[0][1],
        )

    def guest(self, action):
        endpoint = self.registry.data["nfs_endpoints"][self.keys[0]]
        result = quota_nfs.guest_client(endpoint).run(
            "python3",
            "-IB",
            GUEST,
            action,
            self.claim["deployment"],
            self.claim["registry_sha256"],
            self.policy["filesystem_uuid"],
            capture_output=True,
            timeout=100 if action == "install" else 18,
        )
        return json.loads(result.stdout)

    def gate(self, mode, policy=None):
        # A failed host stat must still disallow allocation independently on each guest disk.
        if mode == "open":
            self.space()
        reply = self.guest(mode)
        sample = {**self.space(), "workspaces": reply["workspaces"]}
        if reply["errors"]:
            raise water_pool.IncompleteBlock(sample, reply["errors"])
        for role, row in reply["workspaces"].items():
            if (
                row["uid"] != self.registry.data["candidate_owners"][role]["uid"]
                or row["user_hard_bytes"] != 2_000_000_000
            ):
                raise ValueError("Different source UID or hard quota")
        return sample

    def sample(self):
        return self.gate("inspect")


def prepare_root(pool):
    root = pool.control
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise PermissionError("Expected a private candidate monitor directory")
    if info.st_dev != pool.registry.root.stat().st_dev:
        raise ValueError(
            "Monitor state and operator registry must share the control volume"
        )


def polling(root):
    # The updated supervisor owns this lock for the sampling thread's lifetime.
    # A stale file or a stopped/older supervisor must never renew a write lease.
    with (root / "host-watermark-poller.lock").open("a") as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def configure(registry, policy, alert_socket):
    current = registry.data.get("disk_watermark")
    if current and current["filesystem_uuid"] != policy["filesystem_uuid"]:
        raise ValueError("Different host filesystem is already registered")
    pool = CandidatePool(registry, policy)
    if not polling(registry.root):
        raise ValueError(
            "Start the updated candidate supervisor before registering the host monitor"
        )
    pool.space()
    prepare_root(pool)
    with water_monitor.locked(pool.control):
        if alert_socket is not None:
            water_monitor.water_alerts.register(
                pool.control,
                alert_socket,
                policy["filesystem_uuid"],
                deployment=pool.claim["deployment"],
            )
        ready = pool.control / "installed.json"
        initial = not current or not ready.exists()
        if initial:
            # Commit fail-closed host admission before enabling the guest watchdog.
            registry.data.update(format=5, disk_watermark=policy)
            registry.save()
            result = pool.guest("install")
            if result["errors"] or set(result["workspaces"]) != {"a", "b"}:
                raise ValueError("Both candidate watchdog gates must be available")
            water_monitor.publish(
                ready, dict(claim=pool.claim, filesystem_uuid=policy["filesystem_uuid"])
            )
        elif policy != current:
            registry.data["disk_watermark"] = policy
            registry.save()
        return observe(pool)


def observe(pool):
    return water_monitor.observe(
        pool.control,
        dict(pool.policy, claim=pool.claim, environment="deploy-m6"),
        pool,
        refresh=True,
        stopped=not polling(pool.registry.root),
    )


def command(registry, action, policy):
    pool = CandidatePool(registry, policy)
    with water_monitor.locked(pool.control):
        if water_monitor.read(pool.control / "installed.json") != dict(
            claim=pool.claim, filesystem_uuid=policy["filesystem_uuid"]
        ):
            raise ValueError("Missing candidate kernel watchdog binding")
        if action == "alerts":
            return water_monitor.alerts_status(pool.control)
        if action == "observed":
            return water_monitor.read(pool.control / "state.json")
        if action != "sample":
            raise ValueError("Unsupported host monitor operation")
        return observe(pool)


def poll(registry_root, stop):
    from openscience_per_user_dev import locked_registry

    with (registry_root / "host-watermark-poller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _poll(registry_root, stop, locked_registry)


def _poll(registry_root, stop, locked_registry):
    last_error = None
    while not stop.is_set():
        try:
            with locked_registry(
                registry_root, False, operation="check-write"
            ) as registry:
                if registry.data.get("disk_watermark"):
                    registry.disk_watermark()
            last_error = None
        except (
            OSError,
            ValueError,
            KeyError,
            RuntimeError,
            subprocess.SubprocessError,
        ) as exc:
            if str(exc) != last_error:
                print(
                    f"Candidate host watermark poll failed: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            last_error = str(exc)
        stop.wait(water_monitor.CADENCE_SECONDS)

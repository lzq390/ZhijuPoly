"""Local supervisor for the pinned per-user OpenScience development environment."""

import argparse
from contextlib import contextmanager
import fcntl
from functools import lru_cache
import hashlib
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from uuid import UUID, uuid4


BASE = "sha256:a828b3723cdf0ebe067339b88bad1ef9384ad596f50f1d448b7dfa45f28ec853"
PYTHON = "sha256:e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3"
VERSION = "53293fc-m1-v1"
PURPOSE = "openscience-per-user-development"
PREFIX = "org.nexpoly.m1."
FIXTURE = Path(__file__).resolve().parents[1] / "docs/verification/openscience-m0-fixture"
FIXTURE_HASHES = {"config.json": "8cb573057c482ecbc2b7c9efb189c7f084aaf90d810fc2bdefc737e256bd5554",
                  "fixture.py": "3bd6deddf2429e0e9ec0b176157e856bf55da8043beab4bc383fa3eff454c6f1"}
M2_TRANSPORT = Path(__file__).resolve().parents[1] / "ops/openscience-per-user-dev/m2_loopback.py"
M2_TRANSPORT_HASH = "2ee753da4cf136e41732a1a37f4df6d6d6e9b171942f8dd6b9e034854dc05415"
MOUNT = "/openscience"
TOKEN_DIR = "/run/nexpoly-m1"
CONTROL = "/run/nexpoly-m1-control"
BACKEND_MEMORY_MIB = 2048
FIXTURE_MEMORY_MIB = 96
PAIR_MEMORY_MIB = BACKEND_MEMORY_MIB + FIXTURE_MEMORY_MIB
VOLUME_RESERVE_MIB = 2048
QUOTA_BACKEND = "ext4-user-quota"
NFS_BACKEND = "nfs-ext4-user-quota"
PILOT_USERS = {"test-user-a", "test-user-b"}
DEFAULT_READY_SECONDS = 30
DEFAULT_CAPACITY = {"workspace_memory_mib": 2 * PAIR_MEMORY_MIB, "host_memory_floor_mib": 16384,
                    "volume_free_floor_mib": 32768}
RUNTIME = ["--pull=never", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
           "--log-opt", "max-size=2m", "--log-opt", "max-file=1"]
ENV = {"HOME": "/openscience/home", "XDG_CONFIG_HOME": "/openscience/config", "XDG_STATE_HOME": "/openscience/state",
       "XDG_CACHE_HOME": "/openscience/cache", "XDG_DATA_HOME": "/openscience/files/.runtime/data",
       "OPENSCIENCE_CONFIG": "/m1-config.json", "OPENSCIENCE_RECORDS_ROOT": "/openscience/records",
       "OPENSCIENCE_FILES_ROOT": "/openscience/files", "OPENSCIENCE_DISABLE_AUTOUPDATE": "1",
       "OPENSCIENCE_DISABLE_PROJECT_CONFIG": "1", "OPENSCIENCE_UMASK": "0077"}

# One helper invocation on a never-initialized, specifically labeled volume. The owner changes last.
INITIALIZE = """
import os, stat, sys
from pathlib import Path
root = Path('/data')
uid, identity = int(sys.argv[1]), sys.argv[2]
if root.stat().st_uid != 0:
    raise ValueError('volume has unexpected owner during initialization')
names = ['files', 'files/general', 'files/projects', 'records', 'home', 'state', 'config', 'cache']
marker = root / '.m1-owner'
allowed = {root / n for n in names} | {marker}
for child in root.rglob('*'):
    if child not in allowed or child.is_symlink():
        raise ValueError('unexpected content in uninitialized volume')
for name in names:
    directory = root / name
    directory.mkdir(exist_ok=True)
    directory.chmod(0o700)
if marker.exists() and marker.read_text() != identity:
    raise ValueError('workspace marker differs')
marker.write_text(identity)
marker.chmod(0o444)
for name in reversed(names):
    os.chown(root / name, uid, uid)
root.chmod(0o700)
os.chown(root, uid, uid)
"""
CHECK_VOLUME = """
import os, stat, sys
from pathlib import Path
root = Path('/data')
info = root.stat()
if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (os.getuid(), os.getgid(), 0o700):
    sys.exit(2)
fd = os.open(root / '.m1-owner', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
with os.fdopen(fd) as marker:
    if not stat.S_ISREG(os.fstat(marker.fileno()).st_mode) or marker.read(128) != sys.argv[1]:
        sys.exit(3)
"""
VOLUME_FREE_MIB = """
import os
fs = os.statvfs('/data')
print(fs.f_bavail * fs.f_frsize // (1024 * 1024))
"""
QUOTA_HEALTH = """
import http.client, json, sys
with open("/run/nexpoly-m1/token", encoding="ascii") as source:
    token = source.read(128).strip()
conn = http.client.HTTPConnection("127.0.0.1", 4096, timeout=5)
conn.request("GET", "/global/health", headers={"Authorization": "Bearer " + token})
reply = conn.getresponse()
sys.exit(0 if reply.status == 200 and json.loads(reply.read(4096)).get("healthy") is True else 1)
"""
QUOTA_HEALTH_CMD = "exec python3 -ISB -c " + shlex.quote(QUOTA_HEALTH) + " >/dev/null 2>&1"
READY = """
import json, sys, time, urllib.error, urllib.request
from pathlib import Path
token = Path('/run/nexpoly-m1/token').read_text().strip()
deadline = time.monotonic() + int(sys.argv[1])
last = 'no response'
while time.monotonic() < deadline:
    try:
        for path,key in [('/global/health', 'healthy'), ('/project/current','id')]:
            req = urllib.request.Request('http://127.0.0.1:4096' + path, headers={'Authorization':'Bearer ' + token})
            with urllib.request.urlopen(req, timeout=2) as response:
                if not json.load(response).get(key):
                    raise ValueError('not ready')
        break
    except (ValueError, OSError) as error:
        last = path + ' ' + ('HTTP ' + str(error.code) if isinstance(error, urllib.error.HTTPError) else type(error).__name__)
        time.sleep(0.15)
else:
    raise SystemExit('workspace readiness timed out: ' + last)
"""


class Problem(RuntimeError):
    pass


class CapacityRefusal(Problem):
    def __init__(self, reason, detail):
        super().__init__(f"workspace_capacity: {reason} ({detail})")


@lru_cache(maxsize=3)
def pilot_module(name):
    # The offline guest may stage modules beside the supervisor instead of the repository layout.
    here = Path(__file__).resolve()
    path = here.parents[1] / "ops/openscience-per-user-dev" / (name + ".py")
    if not path.is_file():
        path = here.with_name(name + ".py")
    spec = spec_from_file_location(name, path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def new_row(workspace, uid, generation=0):
    return {"id": workspace, "uid": uid, "generation": generation, "initialized": False,
            "last_activity": 0, "leases": [], "failed_starts": 0, "retry_after": 0}


def validated_readiness(seconds):
    if type(seconds) is not int or not DEFAULT_READY_SECONDS <= seconds <= 180:
        raise Problem("Quota readiness timeout must be between 30 and 180 seconds")
    return seconds


def validate_quota_binding(binding, *, local=True):
    keys = {"mount", "filesystem_uuid", "user_id", "environment", "workspace", "uid", "generation"}
    if (not isinstance(binding, dict) or set(binding) != keys or binding["environment"] != "dev-m1"
            or not isinstance(binding["user_id"], str) or binding["user_id"] not in PILOT_USERS
            or not isinstance(binding["workspace"], str)
            or not re.fullmatch("[0-9a-f]{32}", binding["workspace"]) or type(binding["uid"]) is not int
            or not 210000 <= binding["uid"] < 100210000 or type(binding["generation"]) is not int
            or binding["generation"] < 0):
        raise Problem("Expected a retained dev-m1 pilot binding and generation")
    mount, fs_uuid = binding["mount"], binding["filesystem_uuid"]
    if (not isinstance(mount, str) or not re.fullmatch(r"/[\w./-]+", mount, re.ASCII)
            or os.path.normpath(mount) != mount or (local and str(Path(mount).resolve(strict=True)) != mount)
            or not isinstance(fs_uuid, str)
            or str(UUID(fs_uuid)) != fs_uuid):
        raise Problem("Expected a canonical mount path and filesystem UUID")


def inspect_quota_volume(binding):
    validate_quota_binding(binding)
    mount = Path(binding["mount"])
    payload = mount / "workspace"
    quota = pilot_module("quota")
    observed = quota.read(mount, binding)
    if (observed["filesystem_uuid"] != binding["filesystem_uuid"] or observed["uid"] != binding["uid"]
            or observed["hard_bytes"] != quota.PILOT_HARD_BYTES or not observed["accounting"] or not observed["enforced"]):
        raise Problem("Mounted filesystem UUID, UID or effective hard quota differs from the pilot binding")
    quota.check_source_mount(payload, mount)
    info = payload.stat()
    if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (binding["uid"], binding["uid"], 0o700):
        raise Problem("Quota workspace directory differs from the registered UID or private mode")
    fd = os.open(payload / ".m1-owner", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as marker:
        meta = os.fstat(fd)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_dev != info.st_dev or meta.st_nlink != 1
                or (meta.st_uid, meta.st_gid, stat.S_IMODE(meta.st_mode)) != (0, 0, 0o444)
                or marker.read(33) != binding["workspace"].encode("ascii")):
            raise Problem("Quota workspace identity marker differs from the registered owner")
    return observed


def host_available_mib():
    meminfo = Path("/proc/meminfo").read_text()
    match = re.search(r"^MemAvailable:\s*(\d+) kB$", meminfo, re.MULTILINE)
    if not match:
        raise ValueError("MemAvailable is missing from /proc/meminfo")
    return int(match[1]) // 1024


def validated_capacity(limits):
    if (not isinstance(limits, dict) or limits.keys() != DEFAULT_CAPACITY.keys()
            or any(type(value) is not int or value <= 0 for value in limits.values())):
        raise Problem("Each of the three capacity settings must be a positive integer in MiB")
    return limits


def docker(*arguments, timeout=45, missing=False):
    result = subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        detail = result.stderr.strip()
        if missing and any(message in detail.lower() for message in ("no such container", "no such volume", "no such object")):
            return None
        raise Problem(f"Docker {arguments[0]} failed: {detail[-500:]}")
    return result.stdout.strip()


def local_engine():
    host = os.environ.get("DOCKER_HOST")
    if not host or os.environ.get("DOCKER_CONTEXT"):
        host = docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
    if not host.startswith("unix://"):
        raise Problem("M1 requires the local Linux Docker Engine over a Unix socket")


def software_image(image):
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise Problem("M1 requires a full local image ID")
    fmt = '{{json .RootFS.Layers}}'
    layers = json.loads(docker("image", "inspect", "--format", fmt, image))
    upstream = json.loads(docker("image", "inspect", "--format", fmt, BASE))
    labels = json.loads(docker("image", "inspect", "--format", "{{json .Config.Labels}}", image))
    if (layers[:len(upstream)] != upstream or len(layers) <= len(upstream)
            or labels.get("org.nexpoly.purpose") != PURPOSE or labels.get("org.nexpoly.template-version") != VERSION
            or labels.get("org.nexpoly.expected-base-image") != BASE):
        raise Problem("Image is not the pinned M1 development template")


def write_atomic(path, contents, mode):
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(contents)
            os.fchmod(target.fileno(), mode)
            target.flush()
            os.fsync(target.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(tmp).unlink(missing_ok=True)


class Registry:
    def __init__(self, root):
        self.root = root
        self.file = root / "registry.json"
        self.data = json.loads(self.file.read_text()) if self.file.exists() else None

    def save(self):
        write_atomic(self.file, json.dumps(self.data, sort_keys=True).encode() + b"\n", 0o600)

    def initialize(self, args):
        if not 1 <= args.max_active <= 2 or not 1 <= args.idle_seconds <= 86400:
            raise Problem("M1 supports 1–2 active instances and 1–86400 idle seconds")
        software_image(args.image)
        docker("image", "inspect", "--format", "{{.Id}}", PYTHON)
        settings = dict(image=args.image, template_version=VERSION, max_active=args.max_active,
                        idle_seconds=args.idle_seconds)
        if self.data:
            self.check_format()
            if any(self.data.get(k) != v for k, v in settings.items()):
                raise Problem("Development registry already initialized with different settings")
        else:
            existing = {p.name for p in self.root.iterdir()} - {"supervisor.lock"}
            if existing - FIXTURE_HASHES.keys():
                raise Problem("A new M1 registry requires a dedicated empty state directory")
            for name, expected in FIXTURE_HASHES.items():
                target = self.root / name
                if target.exists() and (target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != expected):
                    raise Problem("State directory contains an unrelated template file")
                data = (FIXTURE / name).read_bytes()
                if hashlib.sha256(data).hexdigest() != expected:
                    raise Problem("Unexpected synthetic model template input")
                write_atomic(self.root / name, data, 0o444)
            self.data = {**settings, "format": 1, "deployment": uuid4().hex, "workspaces": {},
                         "capacity": DEFAULT_CAPACITY.copy()}
            self.save()
        return {**{k: v for k, v in self.data.items() if k != "workspaces"}, "capacity": self.capacity()}

    def capacity(self):
        # Format-1 registries predate admission budgets. Apply the same defaults on first use.
        return validated_capacity(self.data.get("capacity", DEFAULT_CAPACITY))

    def readiness_seconds(self):
        if self.data and self.data.get("format") in (2, 3, 4, 5):
            return validated_readiness(self.data.get("readiness_seconds", DEFAULT_READY_SECONDS))
        return DEFAULT_READY_SECONDS

    def set_capacity(self, workspace_memory_mib, host_memory_floor_mib, volume_free_floor_mib):
        self.bindings()
        limits = validated_capacity(dict(workspace_memory_mib=workspace_memory_mib,
                                         host_memory_floor_mib=host_memory_floor_mib,
                                         volume_free_floor_mib=volume_free_floor_mib))
        self.data["capacity"] = limits
        self.save()
        return {"deployment": self.data["deployment"], "capacity": limits}

    def check_format(self, *, allow_unclaimed=False):
        if not self.data or self.data.get("format") not in (1, 2, 3, 4, 5):
            raise Problem("Initialize an M1 development registry first")
        # Older supervisors must refuse quota registries. A missing pin must never select the Docker backend.
        if self.data["format"] in (2, 3, 4, 5):
            expected = {"dev-m1/" + user for user in PILOT_USERS}
            pins = self.data.get("quota_storage")
            if not isinstance(pins, dict) or set(pins) != expected or set(self.data["workspaces"]) != expected:
                raise Problem("Quota registry identity bindings are incomplete")
            nfs = self.data.get("nfs_endpoints")
            if self.data["format"] in (3, 4, 5):
                if not isinstance(nfs, dict) or set(nfs) != expected or not all(nfs.values()):
                    raise Problem("NFS quota registry endpoint pins are incomplete")
                if self.data["format"] == 3 and not allow_unclaimed:
                    raise Problem("Legacy NFS registry requires exclusive registration; repeat register-quota-nfs while quiescent")
                if self.data["format"] in (4, 5):
                    module = pilot_module("quota_nfs")
                    phase = nfs["dev-m1/test-user-a"]["slot"][:3]
                    uuids = [pins["dev-m1/test-user-" + r]["filesystem_uuid"] for r in "ab"]
                    if self.data.get("native_claim") != module.claim_record(phase, *module.ownership(self), *uuids):
                        raise Problem("NFS owner claim differs from this registry or its location")
            elif nfs is not None:
                raise Problem("Unexpected NFS endpoints in a local ext4 registry")
        elif "quota_storage" in self.data:
            raise Problem("Unexpected quota storage in a format-1 registry")

        if self.data["format"] == 5:
            self.validate_watermark(self.data.get("disk_watermark"))
        elif "disk_watermark" in self.data:
            raise Problem("A disk watermark requires a guarded registry")

    def validate_watermark(self, policy):
        if (not isinstance(policy, dict) or set(policy) != {"filesystem_uuid", "low_bytes", "recover_bytes"}
                or self.data.get("native_claim", {}).get("phase") != "src"
                or not isinstance(policy["filesystem_uuid"], str) or str(UUID(policy["filesystem_uuid"])) != policy["filesystem_uuid"]
                or type(policy["low_bytes"]) is not int or type(policy["recover_bytes"]) is not int
                or not 0 < policy["low_bytes"] < policy["recover_bytes"]):
            raise Problem("Invalid global disk watermark policy")

    def pool_command(self, action, policy):
        nfs = pilot_module("quota_nfs")
        endpoint = self.data["nfs_endpoints"]["dev-m1/test-user-a"]
        claim = self.data["native_claim"]
        raw = nfs.guest_client(endpoint).run(
            "python3", "-IB", "/opt/nexpoly/ops/openscience-per-user-dev/water_monitor.py", action,
            claim["deployment"], claim["registry_sha256"], policy["filesystem_uuid"],
            policy["low_bytes"], policy["recover_bytes"], capture_output=True,
            timeout=120 if action == "configure" else 25,
        ).stdout
        row = json.loads(raw)
        if not isinstance(row, dict) or row.get("filesystem_uuid") != policy["filesystem_uuid"]:
            raise Problem("Unexpected global disk measurement")
        if action not in ("preflight", "alerts") and (
            row.get("configured") is not True or row.get("low_bytes") != policy["low_bytes"]
            or row.get("recover_bytes") != policy["recover_bytes"]
            or row.get("state") not in ("normal", "low", "unavailable")
            or row.get("write_allowed") is not (row["state"] == "normal")
        ):
            raise Problem("Invalid global disk guard observation")
        return row

    def configure_watermark(self, fs_uuid, low_mib, recover_mib):
        self.check_format()
        if self.data["format"] not in (4, 5):
            raise Problem("Only the claimed native development quota volumes support the shared pool")
        policy = dict(filesystem_uuid=fs_uuid, low_bytes=low_mib * 1024**2, recover_bytes=recover_mib * 1024**2)
        self.validate_watermark(policy)
        if self.data.get("disk_watermark", policy) != policy:
            raise Problem("A different global disk policy is already configured")
        if self.data["format"] != 5:
            if self.resident_ids() or any(docker("ps", "-aq", "--filter", "volume=" + v["volume"])
                                          for v in self.data["nfs_endpoints"].values()):
                raise Problem("Recycle and detach both workspaces before first registering the shared pool")
            self.pool_command("preflight", policy)
            # Pin the required guard before enabling the guest. A failed setup is
            # closed to admissions and can only be retried with the same policy.
            self.data.update(format=5, disk_watermark=policy)
            self.save()
        return self.pool_command("configure", policy)

    def disk_watermark(self, action="sample"):
        self.check_format()
        policy = self.data.get("disk_watermark")
        if policy is None:
            return {"configured": False, "write_allowed": True}
        try:
            return self.pool_command(action, policy)
        except (OSError, ValueError, TypeError, KeyError, Problem, subprocess.SubprocessError) as error:
            raise Problem("workspace_disk_monitor_unavailable") from error

    def check_disk_admission(self, *, start=False):
        if start and self.data.get("format") != 5 and "disk_watermark" not in self.data:
            return
        try:
            sample = self.disk_watermark()
            if not sample["write_allowed"]:
                raise Problem("workspace_disk_low_watermark" if sample["state"] == "low" else "workspace_disk_monitor_unavailable")
            return sample
        except Problem as error:
            if start:
                raise CapacityRefusal("global_disk", str(error)) from error
            raise

    def bindings(self):
        self.check_format()
        return [self.workspace(*key.split("/", 1)) for key in self.data["workspaces"]]

    def set_image(self, image):
        self.bindings()
        software_image(image)
        if image != self.data["image"]:
            selector = "label=" + PREFIX + "deployment=" + self.data["deployment"]
            if docker("ps", "-aq", "--filter", selector):
                raise Problem("template_in_use: recycle the registered backend and fixture containers first")
            if "quota_storage" in self.data:
                pilot_module("quota_web_files").prepare(self.root, image)
            self.data["image"] = image
            self.save()
        return {"deployment": self.data["deployment"], "image": image, "template_version": VERSION}

    def register_quota_storage(self, bindings, readiness_seconds=None, *, manifest=None, phase=None):
        if self.data and self.data.get("format") == 3 and manifest is not None:
            self.check_format(allow_unclaimed=True)
        else:
            self.bindings()
        seconds = self.readiness_seconds() if readiness_seconds is None else validated_readiness(readiness_seconds)
        if not isinstance(bindings, list) or len(bindings) != 2:
            raise Problem("Both retained dev-m1 quota bindings are required")
        for binding in bindings:
            validate_quota_binding(binding, local=manifest is None)
        if any(len({b[key] for b in bindings}) != 2 for key in ("uid", "user_id", "workspace", "mount", "filesystem_uuid")):
            raise Problem("Quota pilot requires two distinct users, UIDs, workspaces, mounts and filesystem UUIDs")
        pins = {b["environment"] + "/" + b["user_id"]: b for b in bindings}
        nfs = pilot_module("quota_nfs") if manifest is not None else None
        endpoints = nfs.endpoints(manifest, phase, bindings) if nfs else None
        if self.data["workspaces"] and (self.data.get("quota_storage") != pins or self.data.get("nfs_endpoints") != endpoints):
            raise Problem("Register quota storage on a fresh development registry")
        observed = {key: nfs.attest(endpoints[key], b, docker) if nfs else inspect_quota_volume(b) for key, b in pins.items()}
        claim = nfs.claim_pair(self, endpoints, pins, docker) if nfs else None
        pilot_module("quota_web_files").prepare(self.root, self.data["image"])
        first = "quota_storage" not in self.data
        if first:
            rows = {key: {**new_row(b["workspace"], b["uid"], b["generation"]), "initialized": True}
                    for key, b in pins.items()}
            self.data.update(format=4 if nfs else 2, quota_storage=pins, workspaces=rows)
            if nfs:
                self.data["nfs_endpoints"] = endpoints
        if first or self.readiness_seconds() != seconds or (claim and self.data.get("native_claim") != claim):
            if nfs:
                self.data.update(format=5 if "disk_watermark" in self.data else 4, native_claim=claim)
            self.data["readiness_seconds"] = seconds
            self.save()
        return {"deployment": self.data["deployment"], "storage_backend": NFS_BACKEND if nfs else QUOTA_BACKEND, "volumes": observed,
                "readiness_seconds": seconds, **({"native_claim": claim} if nfs else {})}

    def workspace(self, env, user, create=False):
        self.check_format()
        if not re.fullmatch(r"dev-[a-z0-9][a-z0-9-]{0,39}", env):
            raise Problem("Only explicitly named dev-* environments are supported")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}", user):
            raise Problem("Expected a stable user identifier")
        key = env + "/" + user
        rows = self.data["workspaces"]
        if key not in rows:
            if not create or "quota_storage" in self.data:
                raise Problem("Workspace has no registered binding")
            used = {row["uid"] for row in rows.values()}
            uid = 210000 + secrets.randbelow(100000000)
            while uid in used:
                uid = 210000 + secrets.randbelow(100000000)
            rows[key] = new_row(uuid4().hex, uid)
            self.save()
        return Workspace(self, env, user, rows[key])

    def resident_ids(self):
        # Include stopped, paused and partial pairs, including residents missing from the registry.
        output = docker("ps", "-a", "--filter", "label=" + PREFIX + "deployment=" + self.data["deployment"],
                        "--format", '{{.Label "org.nexpoly.m1.role"}}|{{.Label "org.nexpoly.m1.workspace"}}')
        residents = set()
        seen_roles = set()
        for line in output.splitlines():
            role, workspace = line.split("|")
            if role == "volume-check":  # Nonresident --rm helper, capped at 96 MiB.
                continue
            if (role not in {"backend", "fixture"} or not re.fullmatch(r"[0-9a-f]{32}", workspace)
                    or (workspace, role) in seen_roles):
                raise ValueError("Ambiguous resident Docker container ownership")
            seen_roles.add((workspace, role))
            residents.add(workspace)
        return residents

    def reconcile_residents(self, residents, candidate):
        stopped = {"exited", "dead", "created"}
        changed = False
        for binding in self.bindings():
            if binding.row["id"] == candidate or binding.row["id"] not in residents:
                continue
            backend = binding.resource("backend")
            if backend and backend["state"] not in stopped:
                continue
            fixture = binding.resource("fixture")
            if (backend or fixture) and (not fixture or fixture["state"] in stopped | {"running"}):
                binding.remove_pair()
                changed = True
        return self.resident_ids() if changed else residents

    def check_capacity(self, workspace, count, limits):
        if count > self.data["max_active"]:
            raise CapacityRefusal("instance_count", f"required={count}, limit={self.data['max_active']}")
        committed = count * PAIR_MEMORY_MIB
        budget = limits["workspace_memory_mib"]
        if committed > budget:
            raise CapacityRefusal("workspace_memory", f"required_mib={committed}, limit_mib={budget}")
        # Charge full limits, even for mostly idle workspaces that could still grow to their caps.
        available = host_available_mib()
        required = limits["host_memory_floor_mib"] + committed
        if available < required:
            raise CapacityRefusal("host_memory", f"available_mib={available}, required_mib={required}")
        available = workspace.volume_free_mib()
        required = limits["volume_free_floor_mib"] + count * VOLUME_RESERVE_MIB
        if available < required:
            raise CapacityRefusal("volume_free", f"available_mib={available}, required_mib={required}")

    def admit(self, workspace):
        self.check_disk_admission(start=True)
        try:
            limits = self.capacity()
            candidate = workspace.row["id"]
            residents = self.resident_ids()
            try:
                self.check_capacity(workspace, len(residents | {candidate}), limits)
            except CapacityRefusal:
                remaining = self.reconcile_residents(residents, candidate)
                if remaining == residents:
                    raise
                self.check_capacity(workspace, len(remaining | {candidate}), limits)
        except CapacityRefusal:
            raise
        except (Problem, OSError, ValueError, subprocess.SubprocessError) as error:
            raise CapacityRefusal("probe_unavailable", str(error)) from error


class Workspace:
    def __init__(self, registry, env, user, row):
        self.registry, self.env, self.user, self.row = registry, env, user, row
        self.deployment = registry.data["deployment"]
        self.base = f"nexpoly-m1-{self.deployment[:12]}-{row['id']}"
        self.volume = self.base + "-data"
        self.quota_binding = None
        self.nfs = None
        if "quota_storage" in registry.data:
            self.quota_binding = registry.data["quota_storage"].get(env + "/" + user)
            b = self.quota_binding
            if (not b or (b["environment"], b["user_id"], b["workspace"], b["uid"]) != (env, user, row["id"], row["uid"])
                    or not row["initialized"]):
                raise Problem("Registered quota storage and workspace identity differ")
            self.volume = "ext4:" + b["filesystem_uuid"]
            if registry.data["format"] in (3, 4, 5):
                self.nfs = registry.data["nfs_endpoints"][env + "/" + user]
                self.volume = self.nfs["volume"]
        self.backend, self.fixture = self.base + "-backend", self.base + "-fixture"
        self.uid = str(row["uid"])
        self.identity = self.uid + ":" + self.uid
        self.runtime = registry.root / row["id"]

    def labels(self, role):
        expected = {PREFIX + "deployment": self.deployment, PREFIX + "workspace": self.row["id"],
                    PREFIX + "environment": self.env, PREFIX + "uid": self.uid, PREFIX + "role": role}
        if role in ("fixture", "backend"):
            expected.update({PREFIX + "generation": str(self.row["generation"]), PREFIX + "resident": "yes",
                             PREFIX + "template": self.registry.data["template_version"]})
        return expected

    def label_flags(self, role):
        return [arg for name, value in self.labels(role).items() for arg in ["--label", name + "=" + value]]

    def resource(self, role):
        volume = role == "volume"
        if volume and self.quota_binding:
            self.quota_status()
            return self.volume
        kind = "volume" if volume else "container"
        name = {"volume": self.volume, "backend": self.backend, "fixture": self.fixture}[role]
        fmt = "{{json .Labels}}" if volume else '{{json .Config.Labels}}\n{{.Id}}\n{{.State.Status}}\n{{.Image}}'
        if role == "backend" and self.quota_binding:
            fmt += "\n{{.State.Pid}}"
        result = docker(kind, "inspect", "--format", fmt, name, missing=True)
        if result is None:
            return None
        fields = result.splitlines()
        labels = json.loads(fields[0]) or {}
        if any(labels.get(k) != v for k, v in self.labels(role).items()):
            raise Problem(f"Conflicting {kind} ownership or generation: {name}")
        if volume:
            driver = docker("volume", "inspect", "--format", "{{.Driver}} {{json .Options}}", name)
            if driver not in ("local null", "local {}"):
                raise Problem("Expected a private Docker local volume with default options")
            return name
        wanted = self.registry.data["image"] if role == "backend" else PYTHON
        if fields[3] != wanted:
            raise Problem("Unexpected workspace software image")
        result = {"id": fields[1], "state": fields[2]}
        if role == "backend" and self.quota_binding and result["state"] == "running":
            result["pid"] = int(fields[4])
        return result

    @property
    def secret_dir(self):
        return self.runtime / "secret"

    @property
    def control(self):
        return self.runtime / "control"

    @staticmethod
    def bind(path, destination):
        return f"type=bind,src={path},dst={destination},readonly"

    def data_mount(self, destination):
        if self.quota_binding and not self.nfs:
            return f"type=bind,src={self.quota_binding['mount']}/workspace,dst={destination},bind-propagation=rprivate"
        return f"type=volume,src={self.volume},dst={destination},volume-nocopy"

    def quota_status(self):
        if self.nfs:
            nfs = pilot_module("quota_nfs")
            return nfs.attest(self.nfs, self.quota_binding, docker, owner=self.registry.data["native_claim"])
        return inspect_quota_volume(self.quota_binding)

    def verify_live_quota_mount(self, backend):
        # Docker inspect retains the old source string even if a filesystem was replaced at that path.
        if backend and backend["state"] == "running":
            if self.nfs:
                nfs = pilot_module("quota_nfs")
                docker("exec", backend["id"], "python3", "-ISB", "-c", nfs.CLIENT_CHECK,
                       *nfs.client_args(self.nfs, self.quota_binding, MOUNT))
                return
            live = Path("/proc") / str(backend["pid"]) / "root" / MOUNT.lstrip("/")
            if not live.samefile(Path(self.quota_binding["mount"]) / "workspace"):
                raise Problem("Resident workspace is attached to a different filesystem or payload directory")

    def helper(self, script, *args, initialize=False, anonymous=False):
        flags = ["--cap-add=CHOWN", "--cap-add=FOWNER", "--cap-add=DAC_OVERRIDE"] if initialize else ["--user", self.identity]
        if self.quota_binding and initialize:
            raise Problem("A registered quota workspace must already be initialized")
        mount = "type=volume,dst=/data,volume-nocopy" if anonymous else self.data_mount("/data")
        if not initialize:
            mount += ",readonly"
        return docker("run", "--rm", *RUNTIME, *self.label_flags("volume-check"), "--network", "none",
                      "--memory", "96m", "--cpus", "0.5", "--pids-limit", "32", *flags,
                      "--mount", mount, PYTHON, "python", "-Bc", script, *args)

    def volume_free_mib(self):
        # Probe the actual named volume when present. For first admission sample an anonymous
        # volume on the same local driver before allocating the user's persistent volume.
        exists = self.resource("volume")
        if not exists and self.row["initialized"]:
            raise Problem("Persistent volume is missing; restore the registered volume before starting")
        # Dedicated ext4 filesystems already reserve private storage. Apply the deployment-wide
        # disk floor to the shared Docker driver, even when this user's quota is saturated.
        value = self.helper(VOLUME_FREE_MIB, anonymous=bool(self.quota_binding) or not exists)
        if not value.isascii() or not value.isdecimal():
            raise ValueError("Docker local volume probe returned invalid available MiB")
        return int(value)

    def verify_template_files(self):
        for filename, expected in FIXTURE_HASHES.items():
            path = self.registry.root / filename
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise Problem("Stored synthetic model template differs from its registered version")

    def prepare_volume(self):
        if self.quota_binding:
            self.quota_status()
            if self.nfs:
                nfs = pilot_module("quota_nfs")
                self.helper(nfs.CLIENT_CHECK, *nfs.client_args(self.nfs, self.quota_binding, "/data"))
            return
        if not self.resource("volume"):
            if self.row["initialized"]:
                raise Problem("Persistent volume is missing; restore the registered volume before starting")
            docker("volume", "create", *self.label_flags("volume"), self.volume)
            self.resource("volume")
        if not self.row["initialized"]:
            # A previous process can exit after finalizing the volume and before saving the registry.
            try:
                self.helper(CHECK_VOLUME, self.row["id"])
            except Problem:
                self.helper(INITIALIZE, self.uid, self.row["id"], initialize=True)
        self.helper(CHECK_VOLUME, self.row["id"])
        if not self.row["initialized"]:
            self.row["initialized"] = True
            self.registry.save()

    def transport_program(self):
        # A registry and its live containers may outlast a source checkout.
        path = self.registry.root / "m2-loopback-v1.py"
        if not path.exists() and not path.is_symlink():
            source = M2_TRANSPORT.read_bytes()
            if hashlib.sha256(source).hexdigest() != M2_TRANSPORT_HASH:
                raise Problem("Unexpected M2 loopback program")
            write_atomic(path, source, 0o444)
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != M2_TRANSPORT_HASH:
            raise Problem("Stored M2 loopback program differs from its registered version")
        return path

    def m3_root(self):
        root = os.environ.get("OPENSCIENCE_M3_OVERLAY")
        if not root or self.env != "dev-m1" or self.user not in ("test-user-a", "test-user-b"):
            return None
        return Path(root)

    def m3_overlay(self):
        quota_files = pilot_module("quota_web_files").mounts(self.registry.root) if self.quota_binding else {}
        base = self.m3_root()
        if base is None:
            return quota_files, {}
        code = base / "code"
        backend_overlay = {
            "/m3-code": code,
            "/m3-peer": base / self.row["id"],
            "/app/backend/cli/src/session/prompt.ts": code / "prompt.ts",
            "/app/backend/cli/src/server/server.ts": code / "server.ts",
        }
        for target, name in [("session/llm.ts", "llm.ts"), ("provider/provider.ts", "provider.ts")]:
            if (code / name).is_file():
                backend_overlay["/app/backend/cli/src/" + target] = code / name
        bridge = code / "capability-form-bridge.mjs"
        if bridge.exists():
            backend_overlay["/app/backend/cli/src/server/capability-form-bridge.mjs"] = bridge
        if backend_overlay.keys() & quota_files.keys():
            raise Problem("The development overlays claim the same backend mount")
        backend_overlay.update(quota_files)
        return backend_overlay, {"/m3-model.py": code / "model.py"}

    def retire_generation(self):
        if root := self.m3_root():
            (root / "generations" / self.user).unlink(missing_ok=True)

    def rotate_credential(self):
        self.retire_generation()
        token = secrets.token_hex(32).encode("ascii")
        write_atomic(self.secret_dir / "token", token, 0o444)
        if root := self.m3_root():
            write_atomic(root / "generations" / self.user, hashlib.sha256(token).hexdigest().encode("ascii"), 0o444)

    def backend_command(self):
        # File logging can spin on EDQUOT. Keep operational errors in Docker's bounded logs.
        log_flags = ["--print-logs", "--log-level", "ERROR"] if self.quota_binding else []
        return ["bun", "--conditions=browser", "/app/backend/cli/src/index.ts", *log_flags,
                "serve", "--hostname", "127.0.0.1", "--port", "4096", "--project-root",
                "/openscience/files/projects", "--general-workspace", "/openscience/files/general"]

    def create_pair(self):
        self.runtime.mkdir(mode=0o700, exist_ok=True)
        for path in (self.secret_dir, self.control):
            path.mkdir(mode=0o755, exist_ok=True)
            path.chmod(0o755)
        self.rotate_credential()
        (self.control / "draining").unlink(missing_ok=True)
        secret = self.bind(self.secret_dir, TOKEN_DIR)
        transport = self.runtime / "transport"
        transport.mkdir(exist_ok=True)
        transport.chmod(0o733)  # Only accessible through the deployment owner's private runtime directory.
        backend_overlay, fixture_overlay = self.m3_overlay()

        def mount_flags(bindings):
            flags = []
            for target, source in bindings.items():
                flags.extend(("--mount", self.bind(source, target)))
            return flags

        fixture_id = docker("run", "-d", *RUNTIME, *self.label_flags("fixture"), "--name", self.fixture,
                            "--network", "none", "--user", "65534:65534", "--memory", f"{FIXTURE_MEMORY_MIB}m",
                            "--memory-swap", f"{FIXTURE_MEMORY_MIB}m",
                            "--cpus", "0.5", "--pids-limit", "32", "--mount", secret, "--mount",
                            self.bind(self.registry.root / "fixture.py", "/fixture.py"), "--mount",
                            self.bind(self.transport_program(), "/m2-loopback.py"), "--mount",
                            f"type=bind,src={transport},dst=/m2-transport", *mount_flags(fixture_overlay), PYTHON,
                            "python", "-B", "/m3-model.py" if fixture_overlay else "/m2-loopback.py")
        settings = dict(ENV)
        if fixture_overlay:
            settings["OPENSCIENCE_CONFIG"] = "/m3-code/config.json"
        env = [arg for key, value in settings.items() for arg in ("--env", key + "=" + value)]
        health = []
        if self.quota_binding:
            # Docker records healthcheck argv and output. Only the probe process reads the rotating secret.
            health = ["--health-cmd", QUOTA_HEALTH_CMD,
                      "--health-interval", "30s", "--health-timeout", "10s", "--health-retries", "3",
                      "--health-start-period", f"{self.registry.readiness_seconds()}s"]
        return docker("run", "-d", *RUNTIME, *self.label_flags("backend"), "--name", self.backend, "--network",
                      "container:" + fixture_id, "--user", self.identity, *health, "--memory", f"{BACKEND_MEMORY_MIB}m",
                      "--memory-swap", f"{BACKEND_MEMORY_MIB}m",
                      "--cpus", "1", "--pids-limit", "256", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m,mode=1777",
                      "--mount", self.data_mount(MOUNT),
                      "--mount", secret, "--mount", self.bind(self.control, CONTROL),
                      "--mount", self.bind(self.registry.root / "config.json", "/m1-config.json"), *mount_flags(backend_overlay), *env,
                      self.registry.data["image"], *self.backend_command())

    def verify_pair(self, backend, fixture, *, require_current=True):
        def info(container):
            fmt = "{{json .Mounts}}\n{{.HostConfig.NetworkMode}}\n{{.Config.User}}\n{{json .HostConfig.PortBindings}}"
            verify_runtime = self.quota_binding and container is backend and require_current
            if verify_runtime:
                fmt += "\n{{json .Config.Cmd}}\n{{json .Config.Env}}\n{{json .Config.Healthcheck}}"
            mounts, network, user, ports, *extra = docker("container", "inspect", "--format", fmt, container["id"]).splitlines()
            if verify_runtime:
                cmd, env, health = map(json.loads, extra)
                if (cmd != self.backend_command() or any(item.startswith("BUN_JSC_") for item in env) or not health
                        or health.get("Test") != ["CMD-SHELL", QUOTA_HEALTH_CMD]):
                    raise Problem("Quota backend runtime settings changed; recycle this workspace and ensure it again")
            return {m["Destination"]: m for m in json.loads(mounts)}, network, user, json.loads(ports)
        mounts, net, user, ports = info(backend)
        fm, fnet, fuser, fports = info(fixture)
        if (net != "container:" + fixture["id"] or fnet != "none" or user != self.identity
                or fuser != "65534:65534" or ports or fports):
            raise Problem("Workspace network namespace, user or port bindings differ from the M1 template")
        backend_overlay, fixture_overlay = self.m3_overlay()
        fixture_template = {TOKEN_DIR, "/fixture.py", "/m2-loopback.py", "/m2-transport", *fixture_overlay}
        if (set(mounts) != {MOUNT, TOKEN_DIR, CONTROL, "/m1-config.json", *backend_overlay}
                or set(fm) not in ({TOKEN_DIR, "/fixture.py"}, fixture_template)
                or not mounts[MOUNT]["RW"]):
            raise Problem("Workspace private mounts differ from the M1 template")
        data = mounts[MOUNT]
        if self.quota_binding and not self.nfs:
            source = Path(self.quota_binding["mount"]) / "workspace"
            if (data.get("Type"), data.get("Source"), data.get("Propagation")) != ("bind", str(source), "rprivate"):
                raise Problem("Backend data mount differs from the registered ext4 volume")
        elif data.get("Type") != "volume" or data.get("Name") != self.volume:
            raise Problem("Workspace data volume differs from the M1 template")
        if self.quota_binding:
            self.verify_live_quota_mount(backend)
        bindings = [(mounts[TOKEN_DIR], self.secret_dir), (mounts[CONTROL], self.control),
                    (mounts["/m1-config.json"], self.registry.root / "config.json"),
                    (fm[TOKEN_DIR], self.secret_dir), (fm["/fixture.py"], self.registry.root / "fixture.py")]
        bindings.extend((mounts[dst], src) for dst, src in backend_overlay.items())
        bindings.extend((fm[dst], src) for dst, src in fixture_overlay.items())
        if "/m2-transport" in fm:
            channel = fm["/m2-transport"]
            if channel["Type"] != "bind" or not channel["RW"] or channel["Source"] != str(self.runtime / "transport"):
                raise Problem("Unexpected workspace Unix transport mount")
            bindings.append((fm["/m2-loopback.py"], self.transport_program()))
        if any(m["Type"] != "bind" or m["RW"] or m["Source"] != str(path) for m, path in bindings):
            raise Problem("Unexpected private template or secret binding")

    def ready(self, fixture, backend):
        seconds = self.registry.readiness_seconds()
        try:
            docker("exec", fixture["id"], "python", "-Bc", READY, str(seconds),
                   timeout=seconds + (15 if self.quota_binding else 5))
        except (Problem, subprocess.TimeoutExpired) as error:
            if not self.quota_binding:
                raise
            # Report only Docker's typed state and recognized error categories. Raw OpenScience
            # logs can contain prompts, filenames and credentials.
            details = {"generation": self.row["generation"], "readiness_seconds": seconds}
            probe = re.search(r"workspace readiness timed out: (/(?:global/health|project/current)) "
                              r"(HTTP [0-9]{3}|URLError|TimeoutError|ValueError|JSONDecodeError)\b", str(error))
            if probe:
                details.update(probe_path=probe[1], probe_result=probe[2])
            elif isinstance(error, subprocess.TimeoutExpired):
                details["probe_result"] = "docker_exec_timeout"
            try:
                state = json.loads(docker("container", "inspect", "--format", "{{json .State}}", backend["id"], timeout=10))
                if state["Status"] in ("running", "exited", "dead", "created", "paused", "restarting"):
                    details["backend_state"] = state["Status"]
                details.update(exit_code=int(state["ExitCode"]), oom_killed=bool(state["OOMKilled"]))
                logs = subprocess.run(["docker", "logs", "--tail", "30", backend["id"]], capture_output=True, timeout=10)
                signals = (b"SIGILL", b"Illegal instruction", b"SIGSEGV", b"SyntaxError", b"Cannot find module",
                           b"EROFS", b"EACCES", b"EDQUOT", b"ENOSPC", b"ENOENT", b"out of memory")
                details["backend_log_signals"] = [s.decode() for s in signals if s in logs.stdout or s in logs.stderr]
            except (Problem, OSError, ValueError, KeyError, subprocess.SubprocessError):
                details["diagnostics_incomplete"] = True
            self.row["last_readiness_failure"] = details
            self.registry.save()
            raise Problem("Workspace readiness failed: " + json.dumps(details, sort_keys=True)) from error

    def state(self, reason=None):
        backend = self.resource("backend")
        result = dict(deployment=self.deployment, environment=self.env, user_id=self.user, workspace=self.row["id"],
                      uid=self.row["uid"], gid=self.row["uid"], volume=self.volume, container=self.backend,
                      fixture=self.fixture, generation=self.row["generation"], template_version=VERSION,
                      state=backend["state"] if backend else "recycled", container_id=backend["id"] if backend else None)
        if self.quota_binding:
            result.update(storage_backend=NFS_BACKEND if self.nfs else QUOTA_BACKEND, quota=self.quota_status())
            self.verify_live_quota_mount(backend)
        if reason:
            result["reason"] = reason
        return result

    def remove_pair(self):
        items = [self.resource(role) for role in ("backend", "fixture")]
        self.retire_generation()
        for item in items:
            if item:
                if item["state"] == "running":
                    docker("stop", "--time", "10", item["id"])
                docker("rm", item["id"])
        if self.runtime.exists():
            shutil.rmtree(self.runtime)

    def ensure(self):
        self.verify_template_files()
        backend, fixture = self.resource("backend"), self.resource("fixture")
        if backend and fixture and backend["state"] == fixture["state"] == "running":
            self.verify_pair(backend, fixture)
            if not self.resource("volume") or not self.row["initialized"]:
                raise Problem("Active workspace storage is inconsistent with its registry")
            (self.control / "draining").unlink(missing_ok=True)
            self.ready(fixture, backend)
            return self.state()
        if self.row["retry_after"] > time.time():
            raise Problem("Workspace start backoff is active; retry shortly")
        stable = {"running", "exited", "dead", "created"}
        if any(item and item["state"] not in stable for item in (backend, fixture)):
            raise Problem("Workspace process state is unknown; retain its reservation")
        self.registry.admit(self)
        if backend and backend["state"] == "running":
            if not fixture or fixture["state"] not in ("dead", "exited"):
                raise Problem("Workspace network companion state is unknown; retain the active backend")
            self.verify_pair(backend, fixture, require_current=False)
            result = self.drain("recycle")
            if result["state"] != "recycled":
                raise Problem("Workspace companion recovery deferred: " + result.get("reason", "activity_unknown"))
            backend = fixture = None
        if backend or fixture:
            self.remove_pair()
        self.prepare_volume()
        self.row["generation"] += 1
        self.row["last_activity"] = time.time()
        self.row["leases"] = []
        self.registry.save()
        try:
            self.create_pair()
            backend, fixture = self.resource("backend"), self.resource("fixture")
            self.verify_pair(backend, fixture)
            self.ready(fixture, backend)
        except Exception:
            self.remove_pair()
            self.row["failed_starts"] += 1
            self.row["retry_after"] = time.time() + min(60, 2 ** min(6, self.row["failed_starts"]))
            self.registry.save()
            raise
        self.row.update(last_activity=time.time(), failed_starts=0, retry_after=0)
        self.registry.save()
        return self.state()

    def activity(self, backend, idle_only):
        if self.row["leases"]:
            return "busy"
        try:
            raw = docker("exec", backend["id"], "bun", "/app/backend/cli/src/server/nexpoly-m1-activity.mjs",
                         "--probe", timeout=12)
            data = json.loads(raw)
            if type(data["idle"]) is not bool:
                return "activity_unknown"
            if not data["idle"]:
                return str(data.get("reason") or "busy")
            stamp = data["last_activity_ms"]
            if stamp is not None and (type(stamp) not in (int, float) or not 0 <= stamp <= (time.time()+5)*1000):
                return "activity_unknown"
            last = max(self.row["last_activity"], (stamp or 0) / 1000)
            if idle_only and time.time() - last < self.registry.data["idle_seconds"]:
                return "idle_window"
            return None
        except (Problem, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            return "activity_unknown"

    def drain(self, command):
        backend, fixture = self.resource("backend"), self.resource("fixture")
        if backend and backend["state"] not in ("running", "exited", "dead", "created"):
            return self.state("activity_unknown")
        if not backend or backend["state"] != "running":
            if command == "restart":
                if self.quota_binding:
                    self.quota_status()
                return self.ensure()
            self.remove_pair()
            return self.state()
        if not fixture or fixture["state"] not in ("running", "exited", "dead"):
            return self.state("activity_unknown")
        if fixture["state"] != "running" and command == "restart":
            return self.ensure()
        if command == "restart":
            self.registry.check_disk_admission(start=True)
        self.verify_pair(backend, fixture, require_current=command == "restart")
        reason = self.activity(backend, command == "reap")
        if reason:
            return self.state(reason)
        flag = self.control / "draining"
        write_atomic(flag, b"draining\n", 0o444)
        try:
            reason = self.activity(backend, command == "reap")
            if reason:
                return self.state(reason)
            if command == "restart":
                self.retire_generation()
                docker("stop", "--time", "10", backend["id"])
                self.rotate_credential()
                docker("start", backend["id"])
                self.ready(fixture, backend)
                self.row["last_activity"] = time.time()
                self.registry.save()
            else:
                self.remove_pair()
        finally:
            flag.unlink(missing_ok=True)
        return self.state()

    def business(self, command, lease=None):
        if self.state()["state"] != "running":
            raise Problem("Workspace is not running")
        if command == "begin":
            lease = uuid4().hex
            self.row["leases"].append(lease)
        elif command == "end":
            if lease not in self.row["leases"]:
                raise Problem("Unknown lease for this workspace generation")
            self.row["leases"].remove(lease)
        self.row["last_activity"] = time.time()
        self.registry.save()
        result = self.state()
        if command == "begin":
            result["lease"] = lease
        return result


@contextmanager
def locked_registry(root, create):
    if root.is_symlink():
        raise Problem("Registry must reside in a private directory")
    root = root.resolve()
    if "," in str(root):
        raise Problem("Registry path cannot contain a Docker mount delimiter")
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700 or not root.is_dir():
        raise Problem("Registry must be a directory owned by this user with mode 0700")
    # Serialize local operator commands and capacity reservations, including a bounded startup or drain.
    with (root / "supervisor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield Registry(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path, help="Persistent local private registry directory")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Initialize the fixed offline two-user development template")
    init.add_argument("--image", required=True)
    init.add_argument("--max-active", type=int, default=2)
    init.add_argument("--idle-seconds", type=int, default=1800)
    upgrade = commands.add_parser("set-image", help="Change the pinned development image after recycling every instance")
    upgrade.add_argument("--image", required=True)
    capacity = commands.add_parser("set-capacity", help="Configure admission budgets for this development registry")
    capacity.add_argument("--workspace-memory-mib", required=True, type=int)
    capacity.add_argument("--host-memory-floor-mib", required=True, type=int)
    capacity.add_argument("--volume-free-floor-mib", required=True, type=int)
    quota = commands.add_parser("register-quota-storage", help="Attach both restored dev-m1 ext4 workspaces to a fresh registry")
    quota.add_argument("--bindings", type=Path, required=True)
    quota.add_argument("--readiness-seconds", type=int, help="Quota workspace startup probe deadline (30–180 seconds)")
    nfs = commands.add_parser("register-quota-nfs", help="Attach both preprovisioned NFS quota clone volumes to a fresh registry")
    nfs.add_argument("--manifest", type=Path, required=True)
    nfs.add_argument("--phase", required=True, choices=("src", "dst"))
    nfs.add_argument("--bindings", type=Path, required=True)
    nfs.add_argument("--readiness-seconds", type=int)
    water = commands.add_parser("set-disk-watermark", help="Register the shared ext4 pool and enable its storage monitor")
    water.add_argument("--filesystem-uuid", required=True)
    water.add_argument("--low-mib", required=True, type=int)
    water.add_argument("--recover-mib", required=True, type=int)
    status = commands.add_parser("disk-watermark", help="Measure the pool or read persisted operator evidence")
    source = status.add_mutually_exclusive_group()
    source.add_argument("--observed", action="store_true", help="Read the last observation without taking a new sample")
    source.add_argument("--alerts", action="store_true", help="Read the delivered transition notifications")
    commands.add_parser("list", help="List known development workspace bindings and their actual process state")
    for operation in ("ensure", "status", "restart", "recycle", "reap", "touch", "begin", "end", "check-write"):
        sub = commands.add_parser(operation)
        sub.add_argument("--environment", required=True)
        sub.add_argument("--user-id", required=True)
        if operation == "end":
            sub.add_argument("--lease", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        local_engine()
        with locked_registry(args.state_dir, args.command == "init") as registry:
            if args.command == "init":
                output = registry.initialize(args)
            elif args.command == "list":
                output = {"workspaces": [binding.state() for binding in registry.bindings()]}
            elif args.command == "set-image":
                output = registry.set_image(args.image)
            elif args.command == "register-quota-storage":
                output = registry.register_quota_storage(json.loads(args.bindings.read_bytes()), args.readiness_seconds)
            elif args.command == "register-quota-nfs":
                output = registry.register_quota_storage(json.loads(args.bindings.read_bytes()), args.readiness_seconds,
                                                        manifest=args.manifest, phase=args.phase)
            elif args.command == "set-disk-watermark":
                output = registry.configure_watermark(args.filesystem_uuid, args.low_mib, args.recover_mib)
            elif args.command == "disk-watermark":
                action = "observed" if args.observed else "alerts" if args.alerts else "sample"
                output = registry.disk_watermark(action)
            elif args.command == "set-capacity":
                output = registry.set_capacity(args.workspace_memory_mib, args.host_memory_floor_mib,
                                               args.volume_free_floor_mib)
            else:
                project = registry.workspace(args.environment, args.user_id, create=args.command == "ensure")
                if args.command == "ensure":
                    output = project.ensure()
                elif args.command == "check-write":
                    result = registry.check_disk_admission()
                    output = {"write_allowed": result["write_allowed"], "configured": result["configured"]}
                elif args.command == "status":
                    output = project.state()
                elif args.command in ("reap", "recycle", "restart"):
                    output = project.drain(args.command)
                else:
                    output = project.business(args.command, getattr(args, "lease", None))
        print(json.dumps(output))
    except (OSError, ValueError, subprocess.SubprocessError, Problem) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

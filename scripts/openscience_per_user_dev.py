"""Local M1 development supervisor. Its only upstream is a per-workspace synthetic model."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from uuid import uuid4


BASE = "sha256:a828b3723cdf0ebe067339b88bad1ef9384ad596f50f1d448b7dfa45f28ec853"
PYTHON = "sha256:e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3"
VERSION = "53293fc-m1-v1"
PURPOSE = "openscience-per-user-development"
PREFIX = "org.nexpoly.m1."
FIXTURE = Path(__file__).resolve().parents[1] / "docs/verification/openscience-m0-fixture"
FIXTURE_HASHES = {"config.json": "8cb573057c482ecbc2b7c9efb189c7f084aaf90d810fc2bdefc737e256bd5554",
                  "fixture.py": "3bd6deddf2429e0e9ec0b176157e856bf55da8043beab4bc383fa3eff454c6f1"}
MOUNT = "/openscience"
TOKEN_DIR = "/run/nexpoly-m1"
CONTROL = "/run/nexpoly-m1-control"
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
READY = """
import json, time, urllib.request
from pathlib import Path
token = Path('/run/nexpoly-m1/token').read_text().strip()
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    try:
        for path,key in [('/global/health', 'healthy'), ('/project/current','id')]:
            req = urllib.request.Request('http://127.0.0.1:4096' + path, headers={'Authorization':'Bearer ' + token})
            with urllib.request.urlopen(req, timeout=2) as response:
                if not json.load(response).get(key):
                    raise ValueError('not ready')
        break
    except (ValueError, OSError):
        time.sleep(0.15)
else:
    raise SystemExit('workspace readiness timed out')
"""


class Problem(RuntimeError):
    pass


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
            self.data = {**settings, "format": 1, "deployment": uuid4().hex, "workspaces": {}}
            self.save()
        return {k: v for k, v in self.data.items() if k != "workspaces"}

    def bindings(self):
        if not self.data or self.data.get("format") != 1:
            raise Problem("Initialize an M1 development registry first")
        return [self.workspace(*key.split("/", 1)) for key in self.data["workspaces"]]

    def set_image(self, image):
        self.bindings()
        software_image(image)
        if image != self.data["image"]:
            selector = "label=" + PREFIX + "deployment=" + self.data["deployment"]
            if docker("ps", "-aq", "--filter", selector):
                raise Problem("template_in_use: recycle the registered backend and fixture containers first")
            self.data["image"] = image
            self.save()
        return {"deployment": self.data["deployment"], "image": image, "template_version": VERSION}

    def workspace(self, env, user, create=False):
        if self.data is None or self.data.get("format") != 1:
            raise Problem("Initialize an M1 development registry first")
        if not re.fullmatch(r"dev-[a-z0-9][a-z0-9-]{0,39}", env):
            raise Problem("Only explicitly named dev-* environments are supported")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}", user):
            raise Problem("Expected a stable user identifier")
        key = env + "/" + user
        rows = self.data["workspaces"]
        if key not in rows:
            if not create:
                raise Problem("Workspace has no registered binding")
            used = {row["uid"] for row in rows.values()}
            uid = 210000 + secrets.randbelow(100000000)
            while uid in used:
                uid = 210000 + secrets.randbelow(100000000)
            rows[key] = {"id": uuid4().hex, "uid": uid, "generation": 0, "initialized": False,
                         "last_activity": 0, "leases": [], "failed_starts": 0, "retry_after": 0}
            self.save()
        return Workspace(self, env, user, rows[key])

    def admit(self, workspace):
        label = PREFIX + "deployment=" + self.data["deployment"]
        def residents():
            return set(docker("ps", "-a", "--filter", "label=" + label, "--filter", "label=" + PREFIX + "resident=yes",
                              "--format", '{{.Label "org.nexpoly.m1.workspace"}}').splitlines())

        active = residents()
        if workspace.row["id"] in active or len(active) < self.data["max_active"]:
            return
        # A killed supervisor may leave only its network companion or a terminated backend.
        # Reconcile only registered, stopped workspaces; keep unknown/paused or live processes reserved.
        for binding in self.bindings():
            if binding.row["id"] not in active:
                continue
            backend = binding.resource("backend")
            if not backend or backend["state"] in ("exited", "dead", "created"):
                binding.remove_pair()
        if len(residents()) >= self.data["max_active"]:
            raise Problem("workspace_capacity: all M1 development reservations are in use")


class Workspace:
    def __init__(self, registry, env, user, row):
        self.registry, self.env, self.user, self.row = registry, env, user, row
        self.deployment = registry.data["deployment"]
        self.base = f"nexpoly-m1-{self.deployment[:12]}-{row['id']}"
        self.volume = self.base + "-data"
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
        kind = "volume" if volume else "container"
        name = {"volume": self.volume, "backend": self.backend, "fixture": self.fixture}[role]
        fmt = "{{json .Labels}}" if volume else '{{json .Config.Labels}}\n{{.Id}}\n{{.State.Status}}\n{{.Image}}'
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
        return {"id": fields[1], "state": fields[2]}

    @property
    def secret_dir(self):
        return self.runtime / "secret"

    @property
    def control(self):
        return self.runtime / "control"

    @staticmethod
    def bind(path, destination):
        return f"type=bind,src={path},dst={destination},readonly"

    def helper(self, script, *args, initialize=False):
        flags = ["--cap-add=CHOWN", "--cap-add=FOWNER", "--cap-add=DAC_OVERRIDE"] if initialize else ["--user", self.identity]
        mount = f"type=volume,src={self.volume},dst=/data,volume-nocopy"
        if not initialize:
            mount += ",readonly"
        return docker("run", "--rm", *RUNTIME, *self.label_flags("volume-check"), "--network", "none",
                      "--memory", "96m", "--cpus", "0.5", "--pids-limit", "32", *flags,
                      "--mount", mount, PYTHON, "python", "-Bc", script, *args)

    def verify_template_files(self):
        for filename, expected in FIXTURE_HASHES.items():
            path = self.registry.root / filename
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise Problem("Stored synthetic model template differs from its registered version")

    def prepare_volume(self):
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

    def create_pair(self):
        self.runtime.mkdir(mode=0o700, exist_ok=True)
        for path in (self.secret_dir, self.control):
            path.mkdir(mode=0o755, exist_ok=True)
            path.chmod(0o755)
        write_atomic(self.secret_dir / "token", secrets.token_hex(32).encode(), 0o444)
        (self.control / "draining").unlink(missing_ok=True)
        secret = self.bind(self.secret_dir, TOKEN_DIR)
        fixture_id = docker("run", "-d", *RUNTIME, *self.label_flags("fixture"), "--name", self.fixture,
                            "--network", "none", "--user", "65534:65534", "--memory", "96m", "--memory-swap", "96m",
                            "--cpus", "0.5", "--pids-limit", "32", "--mount", secret, "--mount",
                            self.bind(self.registry.root / "fixture.py", "/fixture.py"), PYTHON,
                            "python", "-B", "/fixture.py", "serve")
        env = [arg for k, v in ENV.items() for arg in ["--env", k + "=" + v]]
        return docker("run", "-d", *RUNTIME, *self.label_flags("backend"), "--name", self.backend, "--network",
                      "container:" + fixture_id, "--user", self.identity, "--memory", "2g", "--memory-swap", "2g",
                      "--cpus", "1", "--pids-limit", "256", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m,mode=1777",
                      "--mount", f"type=volume,src={self.volume},dst={MOUNT},volume-nocopy",
                      "--mount", secret, "--mount", self.bind(self.control, CONTROL),
                      "--mount", self.bind(self.registry.root / "config.json", "/m1-config.json"), *env,
                      self.registry.data["image"], "bun", "--conditions=browser", "/app/backend/cli/src/index.ts",
                      "serve", "--hostname", "127.0.0.1", "--port", "4096", "--project-root",
                      "/openscience/files/projects", "--general-workspace", "/openscience/files/general")

    def verify_pair(self, backend, fixture):
        def info(container):
            fmt = "{{json .Mounts}}\n{{.HostConfig.NetworkMode}}\n{{.Config.User}}\n{{json .HostConfig.PortBindings}}"
            mounts, network, user, ports = docker("container", "inspect", "--format", fmt, container["id"]).splitlines()
            return {m["Destination"]: m for m in json.loads(mounts)}, network, user, json.loads(ports)
        mounts, net, user, ports = info(backend)
        fm, fnet, fuser, fports = info(fixture)
        if (net != "container:" + fixture["id"] or fnet != "none" or user != self.identity
                or fuser != "65534:65534" or ports or fports):
            raise Problem("Workspace network namespace, user or port bindings differ from the M1 template")
        if (set(mounts) != {MOUNT, TOKEN_DIR, CONTROL, "/m1-config.json"}
                or set(fm) != {TOKEN_DIR, "/fixture.py"}
                or mounts[MOUNT].get("Type") != "volume" or mounts[MOUNT].get("Name") != self.volume
                or not mounts[MOUNT]["RW"]):
            raise Problem("Workspace private mounts differ from the M1 template")
        bindings = [(mounts[TOKEN_DIR], self.secret_dir), (mounts[CONTROL], self.control),
                    (mounts["/m1-config.json"], self.registry.root / "config.json"),
                    (fm[TOKEN_DIR], self.secret_dir), (fm["/fixture.py"], self.registry.root / "fixture.py")]
        if any(m["Type"] != "bind" or m["RW"] or m["Source"] != str(path) for m, path in bindings):
            raise Problem("Unexpected private template or secret binding")

    def ready(self, fixture):
        docker("exec", fixture["id"], "python", "-Bc", READY, timeout=35)

    def state(self, reason=None):
        backend = self.resource("backend")
        result = dict(deployment=self.deployment, environment=self.env, user_id=self.user, workspace=self.row["id"],
                      uid=self.row["uid"], gid=self.row["uid"], volume=self.volume, container=self.backend,
                      fixture=self.fixture, generation=self.row["generation"], template_version=VERSION,
                      state=backend["state"] if backend else "recycled", container_id=backend["id"] if backend else None)
        if reason:
            result["reason"] = reason
        return result

    def remove_pair(self):
        items = [self.resource(role) for role in ("backend", "fixture")]
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
            self.ready(fixture)
            return self.state()
        if self.row["retry_after"] > time.time():
            raise Problem("Workspace start backoff is active; retry shortly")
        self.registry.admit(self)
        if backend and backend["state"] == "running":
            if not fixture or fixture["state"] not in ("dead", "exited"):
                raise Problem("Workspace network companion state is unknown; retain the active backend")
            self.verify_pair(backend, fixture)
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
            self.ready(fixture)
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
                return self.ensure()
            self.remove_pair()
            return self.state()
        if not fixture or fixture["state"] not in ("running", "exited", "dead"):
            return self.state("activity_unknown")
        if fixture["state"] != "running" and command == "restart":
            return self.ensure()
        self.verify_pair(backend, fixture)
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
                docker("stop", "--time", "10", backend["id"])
                write_atomic(self.secret_dir / "token", secrets.token_hex(32).encode(), 0o444)
                docker("start", backend["id"])
                self.ready(fixture)
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
    commands.add_parser("list", help="List known development workspace bindings and their actual process state")
    for operation in ("ensure", "status", "restart", "recycle", "reap", "touch", "begin", "end"):
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
            else:
                project = registry.workspace(args.environment, args.user_id, create=args.command == "ensure")
                if args.command == "ensure":
                    output = project.ensure()
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

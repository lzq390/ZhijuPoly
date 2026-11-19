"""Two fresh, simultaneous backends per round on a local Linux cgroup-v2 Docker host."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import sys
import time
from uuid import uuid4


HERE = Path(__file__).resolve().parent
BACKEND = "sha256:a828b3723cdf0ebe067339b88bad1ef9384ad596f50f1d448b7dfa45f28ec853"
PYTHON = "sha256:e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3"
USERS = {"a": 210001, "b": 210002}
MIB = 1024 ** 2
CST = timezone(timedelta(hours=8))

# Executed inside a disposable helper with only the fresh volume mounted.
PREPARE = """
import os
from pathlib import Path
import sys

root = Path('/m0')
uid = int(sys.argv[1])
for name in ('files/general', 'files/projects/m0-project', 'records', 'home', 'state', 'config', 'cache'):
    (root / name).mkdir(parents=True, exist_ok=True)
for directory in sorted(root.rglob('*'), key=lambda path: len(path.parts), reverse=True):
    os.chown(directory, uid, uid)
    directory.chmod(0o700)
os.chown(root, uid, uid)
root.chmod(0o700)
"""


def docker(*args, timeout=60):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"docker {args[0]} failed ({result.returncode}): {result.stderr[-1000:].strip()}")
    return result.stdout.strip()


def parallel(operation):
    with ThreadPoolExecutor(max_workers=len(USERS)) as pool:
        futures = {role: pool.submit(operation, role) for role in USERS}
        return {role: future.result() for role, future in futures.items()}


def bind(filename, target):
    return f"type=bind,src={HERE / filename},dst={target},readonly"


def volume_mount(volume, target="/m0"):
    return f"type=volume,src={volume},dst={target},volume-nocopy"


def cgroup_path(container_id):
    pid = docker("inspect", "--format", "{{.State.Pid}}", container_id)
    for line in (Path("/proc") / pid / "cgroup").read_text().splitlines():
        if line.startswith("0::/") and line != "0::/":
            return Path("/sys/fs/cgroup") / line[4:]
    raise RuntimeError("Local Linux cgroup v2 is required for per-container measurements")


def require_local_engine():
    host = os.environ.get("DOCKER_HOST")
    if os.environ.get("DOCKER_CONTEXT") or not host:
        host = docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
    if not host.startswith("unix://"):
        raise RuntimeError("A local Docker Engine Unix socket is required for bind mounts, clocks and cgroups")


def full_image_id(value):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise argparse.ArgumentTypeError("Pass a full local image ID: sha256: followed by 64 lowercase hex digits")
    return value


def verify_candidate(image):
    # Select only the few public image fields needed; never print the image's environment.
    fields = "{{json .RootFS.Layers}} {{json .Config.Entrypoint}}"
    def metadata(image_id):
        layers, entrypoint = docker("image", "inspect", "--format", fields, image_id).split(" ", 1)
        return json.loads(layers), json.loads(entrypoint)

    original, original_entry = metadata(BACKEND)
    derived, derived_entry = metadata(image)
    if len(derived) <= len(original) or derived[:len(original)] != original:
        raise RuntimeError("Candidate must extend the complete filesystem layers of the pinned M0 base image")
    if derived_entry != original_entry:
        raise RuntimeError("Candidate must use the same entrypoint as the pinned M0 base image")
    label = docker("image", "inspect", "--format", '{{index .Config.Labels "org.nexpoly.m0.candidate"}}', image)
    if label != "20261119":
        raise RuntimeError("Candidate must be a reviewed M0 build labeled org.nexpoly.m0.candidate=20261119")


@dataclass
class Sample:
    timestamp: float
    working: int
    current: int
    peak: int
    cpu_us: int
    pids: int
    oom_kills: int


def sample(root):
    def number(filename):
        return int((root / filename).read_text())

    def counters(filename):
        return dict(line.split() for line in (root / filename).read_text().splitlines())

    current = number("memory.current")
    inactive_file = int(counters("memory.stat")["inactive_file"])
    return Sample(time.monotonic(), current - inactive_file, current, number("memory.peak"),
                  int(counters("cpu.stat")["usage_usec"]), number("pids.current"),
                  int(counters("memory.events")["oom_kill"]))


def summarize(samples):
    first, last = samples[0], samples[-1]
    duration = last.timestamp - first.timestamp
    return {
        "n": len(samples), "window_s": round(duration, 3),
        "working_mib_median": round(statistics.median(s.working for s in samples) / MIB, 2),
        "working_mib_max": round(max(s.working for s in samples) / MIB, 2),
        "total_mib_last": round(last.current / MIB, 2), "lifetime_peak_mib": round(last.peak / MIB, 2),
        "cpu_pct_of_one_core": round((last.cpu_us - first.cpu_us) / (10000 * duration), 2),
        "pids_max": max(s.pids for s in samples), "oom_kills": last.oom_kills,
    }


def measure(paths, workload):
    readings = {role: [sample(path)] for role, path in paths.items()}

    def take():
        for role, path in paths.items():
            readings[role].append(sample(path))

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(workload)
        while not future.done():
            time.sleep(0.2)
            take()
        result = future.result()
    take()
    paired_peak = max(a.working + b.working for a, b in zip(readings["a"], readings["b"]))
    return {"samples": {role: summarize(rows) for role, rows in readings.items()},
            "pair_working_mib_max": round(paired_peak / MIB, 2), "result": result}


class Experiment:
    def __init__(self, mcp, candidate_image=None):
        self.run_id = "nexpoly-m0-" + uuid4().hex
        self.label = "org.nexpoly.m0.run=" + self.run_id
        self.mcp = mcp
        self.backend = candidate_image or BACKEND
        self.root = "/openscience" if candidate_image else "/m0"
        self.flags = ["--label", self.label, "--pull=never", "--read-only", "--cap-drop", "ALL",
                      "--security-opt", "no-new-privileges", "--log-opt", "max-size=2m", "--log-opt", "max-file=1"]
        self.fixtures = {}

    def resources(self):
        selector = "label=" + self.label
        return {"containers": docker("ps", "-aq", "--no-trunc", "--filter", selector).split(),
                "volumes": docker("volume", "ls", "-q", "--filter", selector).split()}

    def clear(self):
        # An unguessable label also covers setup helpers interrupted before their ID was returned.
        owned = self.resources()
        if owned["containers"]:
            docker("rm", "-f", *owned["containers"])
        if owned["volumes"]:
            docker("volume", "rm", *owned["volumes"])
        return self.resources()

    def start_fixture(self, role):
        return docker("run", "-d", *self.flags, "--name", f"{self.run_id}-fixture-{role}",
                      "--network", "none", "--user", "65534:65534", "--memory", "96m", "--memory-swap", "96m",
                      "--cpus", "0.5", "--pids-limit", "32", "--mount", bind("fixture.py", "/fixture.py"),
                      PYTHON, "python", "-B", "/fixture.py", "serve")

    def prepare_volume(self, pair, role):
        volume = f"{self.run_id}-{pair}-{role}-data"
        docker("volume", "create", "--label", self.label, volume)
        # Docker can return an already existing volume by name. Never mount it unless our label matches.
        if volume not in self.resources()["volumes"]:
            raise RuntimeError("Fresh volume ownership could not be confirmed")
        docker("run", "--rm", *self.flags, "--network", "none", "--memory", "96m", "--cpus", "0.5",
               "--cap-add", "CHOWN", "--cap-add", "FOWNER", "--mount", volume_mount(volume),
               PYTHON, "python", "-c", PREPARE, str(USERS[role]))
        return volume

    def launch(self, pair, role, volume):
        root = self.root
        env = {"HOME": f"{root}/home", "XDG_CONFIG_HOME": f"{root}/config", "XDG_STATE_HOME": f"{root}/state",
               "XDG_CACHE_HOME": f"{root}/cache", "XDG_DATA_HOME": f"{root}/files/.runtime/data",
               "OPENSCIENCE_CONFIG": "/synthetic-m0.json", "OPENSCIENCE_RECORDS_ROOT": f"{root}/records",
               "OPENSCIENCE_FILES_ROOT": f"{root}/files", "OPENSCIENCE_DISABLE_AUTOUPDATE": "1",
               "OPENSCIENCE_DISABLE_PROJECT_CONFIG": "1", "OPENSCIENCE_UMASK": "0077"}
        config = "config-mcp.json" if self.mcp else "config.json"
        args = ["create", *self.flags, "--name", f"{self.run_id}-{pair}-{role}",
                "--network", "container:" + self.fixtures[role], "--user", f"{USERS[role]}:{USERS[role]}",
                "--memory", "2g", "--memory-swap", "2g", "--cpus", "1", "--pids-limit", "256",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m,mode=1777", "--mount", volume_mount(volume, root),
                "--mount", bind(config, "/synthetic-m0.json")]
        for key, value in env.items():
            args.extend(["--env", f"{key}={value}"])
        args.extend([self.backend, "bun", "--conditions=browser", "/app/backend/cli/src/index.ts", "serve",
                     "--hostname", "127.0.0.1", "--port", "4096", "--project-root", f"{root}/files/projects",
                     "--general-workspace", f"{root}/files/general"])
        creating = time.monotonic()
        container_id = docker(*args)
        starting = time.monotonic()
        docker("start", container_id)
        return container_id, creating, starting

    def fixture_command(self, role, *args, timeout):
        response = docker("exec", self.fixtures[role], "python", "-B", "/fixture.py", *args, timeout=timeout)
        return json.loads(response)

    def probe(self, role):
        options = ["mcp"] if self.mcp else []
        return self.fixture_command(role, "probe", *options, timeout=50)

    def run_pair(self, pair, record):
        volumes = parallel(lambda role: self.prepare_volume(pair, role))
        with ThreadPoolExecutor(max_workers=2) as pool:
            probes = {role: pool.submit(self.probe, role) for role in USERS}
            time.sleep(0.3)
            launched = parallel(lambda role: self.launch(pair, role, volumes[role]))
            for role, future in probes.items():
                ready = future.result()
                _, creating, starting = launched[role]
                startup = {"create_ms": round(1000 * (starting - creating)),
                           "start_to_health_ms": round(1000 * (ready["health"] - starting)),
                           "create_to_workspace_ms": round(1000 * (ready["workspace"] - creating)),
                           "start_to_workspace_ms": round(1000 * (ready["workspace"] - starting))}
                if self.mcp:
                    startup["create_to_workspace_and_mcp_ms"] = round(1000 * (ready["full"] - creating))
                record["startup"][role] = startup
        paths = {role: cgroup_path(info[0]) for role, info in launched.items()}
        record["baseline"] = measure(paths, lambda: time.sleep(6))
        if pair == 3:
            def chat():
                options = [] if self.root == "/m0" else ["--files-root", f"{self.root}/files"]
                return parallel(lambda role: self.fixture_command(role, "chat", role, "2", *options, timeout=125))
            record["chat"] = measure(paths, chat)
            record["post_chat"] = measure(paths, lambda: time.sleep(10))
        record["allocated_bytes"] = {}
        for role, (container_id, _, _) in launched.items():
            docker("rm", "-f", container_id)
            usage = docker("run", "--rm", *self.flags, "--network", "none", "--memory", "96m", "--cpus", "0.5",
                           "--user", f"{USERS[role]}:{USERS[role]}", "--mount", volume_mount(volumes[role]) + ",readonly",
                           PYTHON, "du", "-s", "-B1", "/m0")
            record["allocated_bytes"][role] = int(usage.split()[0])
            docker("volume", "rm", volumes[role])


def now():
    return datetime.now(CST).isoformat(timespec="seconds")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Output directory for per-run JSON, e.g. /tmp/m0-results")
    parser.add_argument("--mcp", action="store_true", help="Include two synthetic MCP endpoints per actor")
    parser.add_argument("--candidate-image", type=full_image_id,
                        help="Full ID of a locally built M0 derivative; use /openscience as its private mount")
    args = parser.parse_args()
    if not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
        parser.error("Run on the local Linux Docker host with readable cgroup v2 counters")
    require_local_engine()
    if args.candidate_image:
        verify_candidate(args.candidate_image)
    args.output.mkdir(parents=True, exist_ok=True)
    experiment = Experiment(args.mcp, args.candidate_image)
    output = args.output / (experiment.run_id + ".json")
    data = {"run": experiment.run_id, "start": now(), "image": experiment.backend, "fixture_image": PYTHON,
            "synthetic_mcp_endpoints": 2 if args.mcp else 0, "pairs": [], "status": "running"}
    if args.candidate_image:
        data.update({"base_image": BACKEND, "mount_root": experiment.root})

    def save():
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output)

    def terminate(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    # Only clean up resources after confirming the generated run identifier is unused.
    if any(experiment.resources().values()):
        raise RuntimeError("Unexpected existing M0 run label")
    try:
        save()
        experiment.fixtures = parallel(experiment.start_fixture)
        for pair in range(1, 4):
            record = {"pair": pair, "startup": {}}
            data["pairs"].append(record)
            print(f"Starting pair {pair} at {now()}", flush=True)
            experiment.run_pair(pair, record)
            save()
        data["status"] = "passed"
    except (Exception, KeyboardInterrupt) as error:
        data["status"] = "failed"
        data["error"] = str(error) or type(error).__name__
    finally:
        data["end"] = now()
        try:
            data["leftover"] = experiment.clear()
            if any(data["leftover"].values()):
                raise RuntimeError("Run-labeled resources remain after cleanup")
        except (Exception, KeyboardInterrupt) as error:
            data["status"] = "failed"
            data["cleanup_error"] = str(error) or type(error).__name__
        save()
        print(f"Evidence: {output} ({data['status']})", flush=True)
    return int(data["status"] != "passed")


if __name__ == "__main__":
    sys.exit(main())

"""Measure cold admission, real tool calls, idle reclamation and overload on an isolated deploy-m6 clone."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time

from verify_openscience_m5_capacity import (
    COLD_TARGET_SECONDS,
    meminfo,
    nearest_rank,
    volume_fs,
)
from verify_openscience_per_user_dev import GENERAL, MANAGER, check, request, run

sys.path.insert(0, str(MANAGER.parent))
from openscience_m5_backup import BACKUP_FREE_FLOOR, available_bytes
from openscience_m5_storage_ssh import StorageGuest
from openscience_per_user_dev import PAIR_MEMORY_MIB

MIB = 1024**2
FLOOR = 32 * 1024 * MIB
VOLUME_RESERVE = 2 * 1024 * MIB
ROLES = ("a", "b")


def utc():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(path.read_bytes())


def supervisor(root, operation, user):
    payload = {"operation": operation, "environment": "deploy-m6", "user_id": user}
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(360)
        start = time.perf_counter()
        client.connect(str(root / "supervisor.sock"))
        client.sendall(json.dumps(payload).encode() + b"\n")
        with client.makefile("rb") as stream:
            row = json.loads(stream.readline(65536))
    return row, time.perf_counter() - start


def container_ids(deployment):
    listed = run(
        "docker",
        "ps",
        "-a",
        "--no-trunc",
        "--filter",
        "label=org.nexpoly.m1.deployment=" + deployment,
        "--format",
        '{{.ID}} {{.Label "org.nexpoly.m1.role"}}',
    )
    return [line.split()[0] for line in listed.splitlines() if line.split()[1] in ("backend", "fixture")]


def cgroup_files(cid):
    pid = run("docker", "inspect", "--format", "{{.State.Pid}}", cid)
    entries = (Path("/proc") / pid / "cgroup").read_text().splitlines()
    path = next(line[4:] for line in entries if line.startswith("0::/") and line != "0::/")
    return Path("/sys/fs/cgroup") / path


def counters(path, name):
    return {k: int(v) for k, v in (line.split() for line in (path / name).read_text().splitlines())}


class Samples:
    """Sample simultaneous memory independently of the per-cgroup kernel high-water marks."""

    def __init__(self, deployment, shared, disks):
        self.deployment = deployment
        self.disks = disks
        self.shared_ids = set(shared)
        self.paths = {}
        self.info = {}
        self.host_min = sys.maxsize
        self.disk_min = {k: sys.maxsize for k in disks}
        self.peak_total = self.peak_workspace = self.peak_working = self.samples = 0
        self.stop = threading.Event()
        self.error = None

    def sample(self):
        ids = self.shared_ids.union(
            run(
                "docker",
                "ps",
                "-q",
                "--no-trunc",
                "--filter",
                "label=org.nexpoly.m1.deployment=" + self.deployment,
            ).split()
        )
        total = work = working_set = 0
        for cid in sorted(ids):
            if cid not in self.paths:
                try:
                    label = run(
                        "docker",
                        "inspect",
                        "--format",
                        '{{index .Config.Labels "org.nexpoly.m1.role"}}',
                        cid,
                    )
                    if label == "volume-check":
                        continue
                    self.paths[cid] = cgroup_files(cid)
                except (FileNotFoundError, RuntimeError):
                    if cid in self.shared_ids or cid in container_ids(self.deployment):
                        raise
                    continue
                self.info[cid] = {
                    "role": "shared" if cid in self.shared_ids else label,
                    "peak_bytes": 0,
                    "oom": 0,
                    "oom_kill": 0,
                }
            path = self.paths[cid]
            try:
                current = int((path / "memory.current").read_text())
                peak = int((path / "memory.peak").read_text())
                inactive = counters(path, "memory.stat")["inactive_file"]
                events = counters(path, "memory.events")
            except FileNotFoundError:
                continue  # A reaped container's cgroup can vanish between the list and sample.
            row = self.info[cid]
            row["peak_bytes"] = max(row["peak_bytes"], peak)
            for event in ("oom", "oom_kill"):
                row[event] = max(row[event], events[event])
            total += current
            if cid not in self.shared_ids:
                work += current
                working_set += max(0, current - inactive)
        self.peak_total = max(self.peak_total, total)
        self.peak_workspace = max(self.peak_workspace, work)
        self.peak_working = max(self.peak_working, working_set)
        self.host_min = min(self.host_min, meminfo()["available_bytes"])
        for k, p in self.disks.items():
            self.disk_min[k] = min(self.disk_min[k], available_bytes(p))
        self.samples += 1

    def watch(self):
        try:
            while not self.stop.is_set():
                self.sample()
                self.stop.wait(0.65)
        except Exception as exc:
            self.error = exc

    @contextmanager
    def measuring(self, result):
        started = time.time_ns() // 1_000_000_000
        self.sample()
        thread = threading.Thread(target=self.watch)
        thread.start()
        try:
            yield
        finally:
            self.stop.set()
            thread.join()
            events = run(
                "docker",
                "events",
                "--since",
                str(started),
                "--until",
                str(int(time.time()) + 1),
                "--filter",
                "event=oom",
                "--filter",
                "label=org.nexpoly.m1.deployment=" + self.deployment,
                "--format",
                "{{.Actor.ID}}",
            ).split()
            result.update(
                samples=self.samples,
                nominal_interval_seconds=0.65,
                max_simultaneous_workspace_bytes=self.peak_workspace,
                max_simultaneous_workspace_working_set_bytes=self.peak_working,
                max_simultaneous_workspace_and_shared_container_bytes=self.peak_total,
                min_host_available_bytes=self.host_min,
                min_disk_available_bytes=self.disk_min,
                cgroups=self.info,
                workspace_docker_oom_events=events,
            )
            if self.error:
                raise self.error


def rows(root):
    return {
        r["user_id"]: r
        for r in json.loads(run(sys.executable, "-B", str(MANAGER), "--state-dir", str(root), "list"))[
            "workspaces"
        ]
    }


def auto_reap(root, deployment, seconds, ids):
    start = time.monotonic()
    while time.monotonic() - start < seconds + 150:
        if not container_ids(deployment):
            observed = rows(root)
            check(
                all(r["state"] == "recycled" and r["container_id"] is None for r in observed.values()),
                "Idle sweep left running instances",
            )
            return {
                "all_reclaimed_seconds_after_clients_closed": round(time.monotonic() - start, 3),
                "reclaimed_container_ids": sorted(ids),
                "recycled_accounts": len(observed),
            }
        time.sleep(1)
    raise TimeoutError("Automatic idle reclamation exceeded the sweep allowance")


def browser_tools(site, output, only=None):
    path = Path(__file__).with_name("verify_openscience_m6_capacity_tools.mjs")
    args = ["node", str(path), "--state", str(site), "--output", str(output)]
    if only:
        args += ["--only", only]
    subprocess.run(args, check=True, timeout=420)
    evidence = read(output)
    check(evidence["status"] == "passed", "Browser tool check failed")
    return evidence


def check_clone(site):
    root = site.parent
    check(
        root.name.startswith("nexpoly-m6-capacity-") and root.resolve() == root and site.name == "site",
        "Use the separately provisioned capacity site",
    )
    stack = read(site / "running.json")
    registry = Path(stack["registry"])
    check(
        registry == root / "ws"
        and registry.resolve() == registry
        and stack["environment"] == "deploy-m6"
        and stack["quota_files"]
        and stack["gateway"]
        and stack["parent"] == "https://app.capacity-m6.test:18960"
        and stack["agent"] == "https://agent.capacity-m6.test:18961",
        "Expected the independent deploy-m6 quota and gateway site",
    )
    data = read(registry / "registry.json")
    source_dir = Path.home() / ".local/state/nexpoly-m6-deploy-01"
    source = read(source_dir / "ws/registry.json")
    source_site = read(source_dir / "site/running.json")
    check(
        data["format"] == 5
        and data["deployment"] != source["deployment"]
        and stack["deployment"] != source_site["deployment"]
        and stack["gateway_image"] == source_site["gateway_image"]
        and data["image"] == source["image"]
        and data["capacity"] == source["capacity"]
        and data["readiness_seconds"] == source["readiness_seconds"]
        and data["disk_watermark"] == source["disk_watermark"]
        and data["max_active"] == source["max_active"] == 2,
        "Candidate limit or image differs from the live template",
    )
    check(
        len(stack["user_volumes"]) == 2 and 10 <= data["idle_seconds"] <= 1800,
        "Expected two capacity owners",
    )
    for field in ("user_id", "workspace", "uid"):
        check(
            {r[field] for r in data["candidate_owners"].values()}.isdisjoint(
                r[field] for r in source["candidate_owners"].values()
            ),
            "Unexpected preserved identity",
        )
    expected_volumes = {
        owner["user_id"]: data["nfs_endpoints"]["deploy-m6/" + owner["user_id"]]["volume"]
        for owner in data["candidate_owners"].values()
    }
    check(
        stack["user_volumes"] == expected_volumes
        and set(expected_volumes.values()).isdisjoint(r["volume"] for r in source["nfs_endpoints"].values())
        and data["native_claim"]["phase"] == "src"
        and set(data["native_claim"]["filesystem_uuids"].values()).isdisjoint(
            source["native_claim"]["filesystem_uuids"].values()
        ),
        "Unexpected preserved volume",
    )
    vm = Path("/data/lzq") / root.name / "vm"
    check(
        vm == vm.resolve()
        and all(Path(r["ssh_config"]).resolve() == vm / "ssh.conf" for r in data["nfs_endpoints"].values()),
        "Expected dedicated storage on the deployment media",
    )
    return stack, registry, data, vm


def backup_capacity(vm):
    guest = StorageGuest(vm / "ssh.conf")
    result = guest.run("df", "-B1", "--output=size,avail", "/backup", capture_output=True, text=True)
    total, free = map(int, result.stdout.strip().splitlines()[-1].split())
    return {"total_bytes": total, "available_bytes": free}


def memory_budget(root, policy):
    result = json.loads(
        run(
            sys.executable,
            "-B",
            str(MANAGER),
            "--state-dir",
            str(root),
            "set-capacity",
            "--workspace-memory-mib",
            str(policy["workspace_memory_mib"]),
            "--host-memory-floor-mib",
            str(policy["host_memory_floor_mib"]),
            "--volume-free-floor-mib",
            str(policy["volume_free_floor_mib"]),
        )
    )
    check(result["capacity"] == policy, "Admission policy readback differed")


def cold_both(root, owners):
    gate = threading.Barrier(len(owners))

    def cold(role):
        gate.wait(timeout=15)
        reply, elapsed = supervisor(root, "ensure", owners[role])
        check(
            reply["ok"] and reply["result"]["state"] == "running",
            "Cold admission failed: " + str(reply.get("error")),
        )
        return reply["result"], elapsed

    with ThreadPoolExecutor(max_workers=len(owners)) as pool:
        return dict(zip(owners, pool.map(cold, owners)))


def same_binding(result, data, role):
    row = data["candidate_owners"][role]
    key = "deploy-m6/" + row["user_id"]
    check(
        result["deployment"] == data["deployment"]
        and result["workspace"] == row["workspace"]
        and result["uid"] == row["uid"]
        and result["user_id"] == row["user_id"]
        and result["volume"] == data["nfs_endpoints"][key]["volume"]
        and result["quota"]["filesystem_uuid"] == data["native_claim"]["filesystem_uuids"][role]
        and result["quota"]["hard_bytes"] == 2_000_000_000
        and result["quota"]["enforced"],
        "Unexpected live workspace volume or quota",
    )


def overload(site, root, data, users, output):
    baseline = data["capacity"]
    constrained = {**baseline, "workspace_memory_mib": 2 * PAIR_MEMORY_MIB - 1}
    a, b = (users[r] for r in ROLES)
    first, _ = supervisor(root, "ensure", a)
    check(first["ok"], "First capacity account was not admitted")
    online = first["result"]
    same_binding(online, data, "a")
    ids = container_ids(data["deployment"])
    check(len(ids) == 2, "Expected one complete resident pair")
    try:
        memory_budget(root, constrained)
        before = time.perf_counter()
        refused, elapsed = supervisor(root, "ensure", b)
        check(
            not refused["ok"]
            and re.fullmatch(
                r"workspace_capacity: workspace_memory \(required_mib=4288, limit_mib=4287\)",
                refused["error"],
            ),
            "Second account did not reach the memory guard",
        )
        marker = {"path": "capacity-online-check.txt", "content": data["deployment"]}
        request(online, "/file/content", "PUT", marker, directory=GENERAL)
        check(
            request(online, "/file/content", directory=GENERAL, path=marker["path"])["content"]
            == marker["content"],
            "Online account failed private write/read during refusal",
        )
        probe = browser_tools(site, output, "a")
        after = rows(root)
        check(
            after[a]["container_id"] == online["container_id"]
            and after[b]["state"] == "recycled"
            and set(container_ids(data["deployment"])) == set(ids),
            "Rejection displaced an online account or allocated another pair",
        )
        return {
            "reason": refused["error"],
            "refusal_seconds": elapsed,
            "online_read_write": True,
            "same_online_backend": True,
            "resident_containers": len(ids),
            "online_browser": probe,
            "probe_elapsed_seconds": round(time.perf_counter() - before, 3),
        }
    finally:
        memory_budget(root, baseline)


def exercise(site, output, rounds, width, report):
    stack, root, data, vm = check_clone(site)
    users = {r: data["candidate_owners"][r]["user_id"] for r in ROLES}
    shared = {stack[k] for k in ("pg", "api", "gateway")}
    vm_cfg = read(vm / "vm.json")
    paths = {
        "docker": Path(run("docker", "info", "--format", "{{.DockerRootDir}}")),
        "target": vm,
        "backup_host": Path(vm_cfg["backup_dir"]),
    }
    previous = {}
    report.update(
        image=data["image"],
        workspace_deployment=data["deployment"],
        site_deployment=stack["deployment"],
        owners=users,
        active_under_test=width,
        idle_seconds=data["idle_seconds"],
        admission=data["capacity"],
        max_active=data["max_active"],
        target_ready_seconds=COLD_TARGET_SECONDS,
        backend_probe_timeout_seconds=data["readiness_seconds"],
        timing="simultaneous ensure dispatch through private supervisor to success (/global/health and /project/current), including queue",
        backup_guest_before=backup_capacity(vm),
        rounds=[],
    )
    for nr in range(1, rounds + 1):
        check(
            not container_ids(data["deployment"])
            and all(row["state"] == "recycled" for row in rows(root).values()),
            "Round requires fully cold workspaces",
        )
        item = {"round": nr, "started_at": utc(), "metrics": {}, "users": {}}
        report["rounds"].append(item)
        metrics = Samples(data["deployment"], shared, paths)
        with metrics.measuring(item["metrics"]):
            scheduled = users if width == 2 else {ROLES[(nr - 1) % 2]: users[ROLES[(nr - 1) % 2]]}
            active = cold_both(root, scheduled)
            print("Round", nr, "cold seconds", {role: row[1] for role, row in active.items()}, flush=True)
            for role, (row, elapsed) in active.items():
                same_binding(row, data, role)
                check(
                    row["container_id"] != previous.get(role),
                    "Previous backend survived idle collection",
                )
                previous[role] = row["container_id"]
                item["users"][role] = {
                    "user_id": users[role],
                    "container_id": row["container_id"],
                    "volume": row["volume"],
                    "uid": row["uid"],
                    "generation": row["generation"],
                    "submit_to_ready_seconds": elapsed,
                    "volume_fs": volume_fs(row),
                }
            ids = container_ids(data["deployment"])
            check(len(ids) == width * 2, "Every requested workspace pair must be resident")
            toolreport = browser_tools(
                site,
                output.with_name(output.stem + f"-tools-{nr}.json"),
                next(iter(scheduled)) if width == 1 else None,
            )
            item["browser"] = toolreport
            item["collection"] = auto_reap(root, data["deployment"], data["idle_seconds"], ids)
        item["ended_at"] = utc()
        captured = {cid: r for cid, r in metrics.info.items() if r["role"] in ("backend", "fixture")}
        check(
            set(captured) == set(ids) and all(row["peak_bytes"] > 0 for row in captured.values()),
            "Incomplete cgroup capture",
        )
        print("Finished capacity round", nr, flush=True)
    if width == 2:
        report["memory_overload"] = overload(
            site,
            root,
            data,
            users,
            output.with_name(output.stem + "-overload-tools.json"),
        )
        report["final_idle_reap"] = auto_reap(
            root,
            data["deployment"],
            data["idle_seconds"],
            container_ids(data["deployment"]),
        )
    report["backup_guest_after"] = backup_capacity(vm)
    report["final_policy"] = read(root / "registry.json")["capacity"]
    report["remaining_workspace_containers"] = container_ids(data["deployment"])
    times = [u["submit_to_ready_seconds"] for item in report["rounds"] for u in item["users"].values()]
    metrics = [item["metrics"] for item in report["rounds"]]
    free_floor = FLOOR + width * VOLUME_RESERVE
    report["summary"] = dict(
        cold_samples=len(times),
        p50_seconds=nearest_rank(times, 0.50),
        p95_seconds=nearest_rank(times, 0.95),
        max_simultaneous_workspaces_bytes=max(m["max_simultaneous_workspace_bytes"] for m in metrics),
        max_workspace_and_shared_bytes=max(
            m["max_simultaneous_workspace_and_shared_container_bytes"] for m in metrics
        ),
        oom_events=sum(r["oom"] for m in metrics for r in m["cgroups"].values() if r["role"] != "shared"),
        oom_kills=sum(r["oom_kill"] for m in metrics for r in m["cgroups"].values() if r["role"] != "shared"),
        docker_oom_events=sum(len(m["workspace_docker_oom_events"]) for m in metrics),
        shared_oom_observed=any(
            r["oom"] or r["oom_kill"] for m in metrics for r in m["cgroups"].values() if r["role"] == "shared"
        ),
        disk_floor_met=all(
            min(m["min_disk_available_bytes"][k] for k in ("docker", "target")) >= free_floor for m in metrics
        ),
    )
    summary = report["summary"]
    summary["host_memory_floor_met"] = (
        min(m["min_host_available_bytes"] for m in metrics)
        >= (data["capacity"]["host_memory_floor_mib"] + width * PAIR_MEMORY_MIB) * MIB
    )
    summary["backup_physical_reserve_met"] = (
        min(m["min_disk_available_bytes"]["backup_host"] for m in metrics) >= BACKUP_FREE_FLOOR + 5 * 1024 * MIB
    )
    summary["guest_backup_free_bytes"] = min(
        report[k]["available_bytes"] for k in ("backup_guest_before", "backup_guest_after")
    )
    check(
        report["final_policy"] == data["capacity"] and not report["remaining_workspace_containers"],
        "Final policy or idle state differs",
    )
    check(
        not any(
            summary[k]
            for k in (
                "oom_events",
                "oom_kills",
                "docker_oom_events",
                "shared_oom_observed",
            )
        ),
        "OOM during workload",
    )
    check(
        summary["disk_floor_met"]
        and summary["backup_physical_reserve_met"]
        and summary["guest_backup_free_bytes"] > 4_000_000_000 + 512 * MIB,
        "Insufficient disk headroom",
    )
    check(summary["host_memory_floor_met"], "Insufficient host memory headroom")
    report["status"] = "passed" if summary["p95_seconds"] <= COLD_TARGET_SECONDS else "p95_exceeded"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--active", type=int, default=2, choices=[1, 2])
    parser.add_argument("--rounds", type=int, default=2, choices=range(2, 6))
    args = parser.parse_args()
    report = {"started_at": utc(), "status": "failed"}
    with args.output.open("x") as file:
        try:
            exercise(args.state, args.output, args.rounds, args.active, report)
        finally:
            report["ended_at"] = utc()
            json.dump(report, file, indent=2)
            file.write("\n")
    return int(report["status"] != "passed")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    sys.exit(main())

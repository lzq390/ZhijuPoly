"""Run the M5 two-workspace admission and cold-start drill in a disposable local registry."""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

from verify_openscience_per_user_dev import (
    GENERAL,
    MANAGER,
    MODEL_REPLY,
    check,
    generated_resources,
    request,
    run,
    together,
)


IMAGE = "sha256:91c29f44773ef3e82ba7cdbd1a457cc5c0d7065975d4918ae47e21a3e6332859"
OWNERS = ("test-user-a", "test-user-b")
ENVIRONMENT = "dev-m5-capacity"
MIB = 1 << 20
PAIR_MEMORY_MIB = 2048 + 96
COLD_TARGET_SECONDS = 30
POLICY = {
    "workspace-memory-mib": 2 * PAIR_MEMORY_MIB,
    "host-memory-floor-mib": 16 * 1024,
    "volume-free-floor-mib": 32 * 1024,
}


def meminfo():
    fields = {
        line.split(":", 1)[0]: int(line.split()[1]) * 1024
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith(("MemTotal:", "MemAvailable:"))
    }
    return {"total_bytes": fields["MemTotal"], "available_bytes": fields["MemAvailable"]}


def cgroup(container):
    pid = run("docker", "inspect", "--format", "{{.State.Pid}}", container)
    entry = next(
        (
            line[4:]
            for line in (Path("/proc") / pid / "cgroup").read_text().splitlines()
            if line.startswith("0::/") and line != "0::/"
        ),
        None,
    )
    if entry is None:
        raise RuntimeError("A local cgroup v2 Linux Docker host is required")
    root = Path("/sys/fs/cgroup") / entry
    events = {
        k: int(v) for k, v in (line.split() for line in (root / "memory.events").read_text().splitlines())
    }
    return {
        "memory_current_bytes": int((root / "memory.current").read_text()),
        "memory_peak_bytes": int((root / "memory.peak").read_text()),
        "memory_limit_bytes": int((root / "memory.max").read_text()),
        "oom_events": events["oom"],
        "oom_kills": events["oom_kill"],
    }


def volume_fs(binding):
    # df and du use the actual /openscience mount, under the instance's real UID.
    fields = run("docker", "exec", binding["container"], "df", "-Pk", "/openscience").splitlines()[-1].split()
    return {
        "total_bytes": int(fields[-5]) * 1024,
        "available_bytes": int(fields[-3]) * 1024,
        "private_volume_allocated_bytes": int(
            run("docker", "exec", binding["container"], "du", "-sk", "/openscience").split()[0]
        )
        * 1024,
    }


def private_marker(binding, user, *, write=False):
    path = "capacity-drill.txt"
    if write:
        request(binding, "/file/content", "PUT", {"path": path, "content": user}, directory=GENERAL)
    data = request(binding, "/file/content", directory=GENERAL, path=path)
    check(data["content"] == user, "running instance returned an unexpected owner's marker")


def sample_pair(binding, user):
    private_marker(binding, user)
    session = request(binding, "/session", "POST", {"title": "M5 capacity sample"}, directory=GENERAL)
    reply = request(
        binding,
        f"/session/{session['id']}/message",
        "POST",
        {
            "model": {"providerID": "m0fixture", "modelID": "m0"},
            "parts": [{"type": "text", "text": "M5 synthetic sample"}],
        },
        directory=GENERAL,
    )
    check(any(p.get("text") == MODEL_REPLY for p in reply["parts"]), "synthetic model did not reply")
    disk = volume_fs(binding)
    backend, sidecar = cgroup(binding["container"]), cgroup(binding["fixture"])
    for metrics, limit in ((backend, 2048), (sidecar, 96)):
        check(
            metrics["memory_limit_bytes"] == limit * MIB,
            "cgroup cap does not match the admission reservation",
        )
        check(metrics["oom_events"] == metrics["oom_kills"] == 0, "cgroup reported an OOM")
    return {"backend": backend, "sidecar": sidecar, "disk": disk}


def nearest_rank(values, probability):
    return sorted(values)[math.ceil(probability * len(values)) - 1]


def drill(root, report):
    cli = [sys.executable, str(MANAGER), "--state-dir", str(root)]

    def manager(*args):
        return json.loads(run(*cli, *args))

    def action(verb, owner, env=ENVIRONMENT):
        return manager(verb, "--environment", env, "--user-id", owner)

    def configure(policy):
        options = [flag for name, value in policy.items() for flag in ("--" + name, str(value))]
        stored = manager("set-capacity", *options)["capacity"]
        check(
            stored == {key.replace("-", "_"): value for key, value in policy.items()},
            "manager did not persist the requested admission thresholds",
        )

    def cold(owner, env=ENVIRONMENT):
        start = time.perf_counter()
        row = action("ensure", owner, env)
        # ensure already verified /global/health and /project/current in the paired service.
        ready = time.perf_counter()
        check(row["state"] == "running", "ensure did not produce a running backend")
        check(request(row, "/global/health")["healthy"], "authorized API health failed")
        return row, math.ceil((ready - start) * 1000) / 1000

    a, b = OWNERS
    alternate = ENVIRONMENT + "-other"
    deployment = manager("init", "--image", IMAGE, "--idle-seconds", "1800", "--max-active", "2")[
        "deployment"
    ]

    def refuse(reason, owner, existing, policy, env=ENVIRONMENT):
        live_before = generated_resources(deployment)
        result = subprocess.run(
            [*cli, "ensure", "--environment", env, "--user-id", owner],
            capture_output=True,
            text=True,
            timeout=60,
        )
        check(result.returncode == 1, "expected refusal at " + reason)
        error = json.loads(result.stderr)["error"]
        parsed = re.match(r"^workspace_capacity: (\w+)\b", error)
        check(parsed and parsed[1] == reason, f"unexpected refusal for {reason}: {error}")
        observed = {
            name: int(value)
            for name, value in re.findall(
                r"\b(required_mib|available_mib|limit_mib|required|limit)=(\d+)", error
            )
        }
        required_field = "required" if reason == "instance_count" else "required_mib"
        available_field = {"instance_count": "limit", "workspace_memory": "limit_mib"}.get(
            reason, "available_mib"
        )
        check(
            observed[required_field] > observed[available_field],
            "measured usage did not cross the selected threshold",
        )
        live_after = generated_resources(deployment)
        check(
            all(set(live_before[k]) == set(live_after[k]) for k in live_before),
            "refusal allocated or removed resources",
        )
        served = action("ensure", a)
        check(
            served["container_id"] == existing["container_id"]
            and served["generation"] == existing["generation"],
            "admission pressure displaced the active workspace",
        )
        private_marker(served, a)
        return {
            "reason": reason,
            "policy_mib": policy,
            "manager_at_refusal": observed,
            "running_a_private_read_status": 200,
            "a_generation_preserved": True,
            "resources_unchanged": True,
        }

    try:
        configure(POLICY)
        first_a, new_a = cold(a)
        private_marker(first_a, a, write=True)
        mem, fs = meminfo(), volume_fs(first_a)
        report["host_before_refusal"] = mem
        report["volume_filesystem_before_refusal"] = fs
        for reason, option, threshold in (
            ("workspace_memory", "workspace-memory-mib", 2 * PAIR_MEMORY_MIB - 1),
            ("host_memory", "host-memory-floor-mib", math.ceil(mem["total_bytes"] / MIB) + 1),
            ("volume_free", "volume-free-floor-mib", math.ceil(fs["total_bytes"] / MIB) + 1),
        ):
            policy = {**POLICY, option: threshold}
            configure(policy)
            report["refusals"].append(refuse(reason, b, first_a, policy))
        configure(POLICY)
        first_b, new_b = cold(b)
        private_marker(first_b, b, write=True)
        report["new_volume_start_seconds"] = {a: new_a, b: new_b}
        report["refusals"].append(refuse("instance_count", a, first_a, POLICY, alternate))
        check(action("recycle", b)["state"] == "recycled", "B did not release its reservation")
        replacement, _ = cold(a, alternate)
        check(
            replacement["volume"] != first_a["volume"] and replacement["volume"] != first_b["volume"],
            "alternate binding reused a private volume",
        )
        private_marker(first_a, a)
        report["recycle_releases_slot"] = True
        check(action("recycle", a, alternate)["state"] == "recycled", "alternate did not recycle")
        check(action("recycle", a)["state"] == "recycled", "A did not recycle")

        for number in range(1, 4):
            # Both requests enter concurrently through the normal serialized manager admission path.
            bindings = together(OWNERS, cold)
            measured = together(OWNERS, lambda user: sample_pair(bindings[user][0], user))
            report["paired_rounds"].append(
                {
                    "round": number,
                    "host_after_workload": meminfo(),
                    "users": {
                        user: {"submit_to_ready_seconds": bindings[user][1], **measured[user]}
                        for user in OWNERS
                    },
                }
            )
            for user in OWNERS:
                check(action("recycle", user)["state"] == "recycled", "round did not end in a cold pair")
            print(f"Paired cold-start round {number}/3 complete", flush=True)
        samples = [r for pair in report["paired_rounds"] for r in pair["users"].values()]
        times = [r["submit_to_ready_seconds"] for r in samples]
        p95 = nearest_rank(times, 0.95)
        report["summary"] = {
            "samples": len(samples),
            "p50_submit_to_ready_seconds": nearest_rank(times, 0.5),
            "p95_submit_to_ready_seconds": p95,
            "cold_ready_target_seconds": COLD_TARGET_SECONDS,
            "two_user_p95_within_target": p95 <= COLD_TARGET_SECONDS,
            "max_backend_cgroup_peak_bytes": max(r["backend"]["memory_peak_bytes"] for r in samples),
            "max_sidecar_cgroup_peak_bytes": max(r["sidecar"]["memory_peak_bytes"] for r in samples),
            "max_user_volume_allocated_bytes": max(
                r["disk"]["private_volume_allocated_bytes"] for r in samples
            ),
            "oom_events": sum(r[k]["oom_events"] for r in samples for k in ("backend", "sidecar")),
            "oom_kills": sum(r[k]["oom_kills"] for r in samples for k in ("backend", "sidecar")),
        }
        check(p95 <= COLD_TARGET_SECONDS, "two-user cold-start P95 exceeded the 30-second target")
    finally:
        owned = generated_resources(deployment)
        if owned["containers"]:
            run("docker", "rm", "-f", *owned["containers"])
        if owned["volumes"]:
            run("docker", "volume", "rm", *owned["volumes"])
        remaining = generated_resources(deployment)
        report["remaining"] = {kind: len(items) for kind, items in remaining.items()}
        check(not any(remaining.values()), "drill left labeled resources behind")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New sanitized JSON report")
    args = parser.parse_args()
    report = {
        "status": "failed",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "image": IMAGE,
        "scope": "fresh disposable registry, two private volumes, per-workspace synthetic model",
        "timing": "concurrent operator ensure to API-ready including manager lock wait, nearest-rank quantiles",
        "policy_mib": POLICY,
        "max_active": 2,
        "reserved_per_pair_mib": PAIR_MEMORY_MIB,
        "refusals": [],
        "paired_rounds": [],
    }
    with args.output.open("x") as output:

        def interrupted(*_):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, interrupted)
        try:
            with TemporaryDirectory(prefix="openscience-m5-capacity-") as temp:
                drill(Path(temp), report)
            report["status"] = "passed"
        except (Exception, KeyboardInterrupt) as exc:
            print(str(exc) or type(exc).__name__, file=sys.stderr)
        finally:
            report["ended_at"] = datetime.now(timezone.utc).isoformat()
            json.dump(report, output, indent=2)
            output.write("\n")
    return int(report["status"] != "passed")


if __name__ == "__main__":
    sys.exit(main())

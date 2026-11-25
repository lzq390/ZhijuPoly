"""Measure the enrolled deploy-m6 target by moving only its thresholds around real free space."""

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from uuid import uuid4

CI = Path(__file__).resolve().parent
MANAGER = CI.parent / "openscience_per_user_dev.py"
MIB = 1024**2


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, timeout=150, **kwargs)


def read(path):
    return json.loads(path.read_bytes())


def workspace(root, user, operation):
    query = dict(environment="deploy-m6", user_id=user, operation=operation)
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(240)
        client.connect(str(root / "supervisor.sock"))
        client.sendall(json.dumps(query).encode() + b"\n")
        with client.makefile("rb") as stream:
            return json.loads(stream.readline(1024 * 1024))


def wait(browser, work, marker, seconds=420):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if (work / marker).exists():
            return
        require(
            browser.poll() is None and not (work / "failed").exists(),
            "Browser failed before " + marker,
        )
        time.sleep(0.5)
    raise TimeoutError(marker)


def sample_row(sample, state, deployment):
    require(
        sample["state"] == state
        and sample["write_allowed"] == (state == "normal")
        and sample["environment"] == "deploy-m6"
        and sample["deployment"] == deployment,
        "Candidate observation differs from requested mode",
    )
    require(
        set(sample["workspaces"]) == {"a", "b"}
        and all(
            row["group_gate"] == ("open" if state == "normal" else "block")
            and row["user_hard_bytes"] == 2_000_000_000
            for row in sample["workspaces"].values()
        ),
        "Both per-user limits and allocation gates must be confirmed",
    )
    return sample


def drill(state, output):
    stack = read(state / "running.json")
    root = Path(stack["registry"])
    db = read(root / "registry.json")
    policy = db["disk_watermark"]
    deploy = read(state / "candidate.json")["registry_deployment"]
    require(
        stack["environment"] == "deploy-m6"
        and stack["quota_files"]
        and db["format"] == 5
        and deploy == db["deployment"] == db["native_claim"]["deployment"]
        and db["native_claim"]["phase"] == "src"
        and set(db["candidate_owners"]) == {"a", "b"},
        "Use the independently registered deploy-m6 source pair",
    )
    target = {Path(row["file"]).stat().st_dev for row in db["host_media"].values()}
    require(
        target == {Path("/data").stat().st_dev} and root.stat().st_dev not in target,
        "Source images must be on /data and control state on the other disk",
    )
    manager = [sys.executable, "-B", str(MANAGER), "--state-dir", str(root)]
    setter = [
        *manager,
        "set-disk-watermark",
        "--filesystem-uuid",
        policy["filesystem_uuid"],
    ]
    original = [policy[k] // MIB for k in ("low_bytes", "recover_bytes")]
    require(
        all(
            mib * MIB == policy[k]
            for mib, k in zip(original, ("low_bytes", "recover_bytes"))
        ),
        "Thresholds must round-trip through the manager",
    )
    baseline_cmd = [
        *setter,
        "--low-mib",
        str(original[0]),
        "--recover-mib",
        str(original[1]),
    ]
    report = {
        "environment": "deploy-m6",
        "deployment": deploy,
        "site_deployment": stack["deployment"],
        "baseline": sample_row(
            json.loads(run(*manager, "disk-watermark")), "normal", deploy
        ),
    }
    require(
        report["baseline"]["available_bytes"] >= policy["recover_bytes"],
        "Baseline must exceed recovery",
    )
    output.mkdir(mode=0o700)
    require(
        output.stat().st_dev not in target,
        "Drill control files must use the independent disk",
    )
    work = output / "browser"
    work.mkdir(mode=0o700)
    users = {k: v["user_id"] for k, v in db["candidate_owners"].items()}
    unit = "nexpoly-deploy-m6-water-reset-" + uuid4().hex
    systemd_env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
    armed = False

    def mark(name):
        (work / name).touch(exist_ok=False)

    def status(role, expected):
        reply = workspace(root, users[role], "status")
        row = reply["result"]
        require(
            reply["ok"]
            and row["state"] == expected
            and row["workspace"] == db["candidate_owners"][role]["workspace"],
            "Unexpected workspace state for " + role,
        )
        return {
            k: row[k] for k in ("state", "generation", "uid", "volume", "workspace")
        }

    with (output / "browser.log").open("x") as log:
        browser = subprocess.Popen(
            [
                shutil.which("node") or "node",
                str(CI / "verify_openscience_deploy_watermark.mjs"),
                str(state),
                str(work),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            wait(
                browser,
                work,
                "prepared",
                seconds=2 * stack["startup_timeout_seconds"] + 120,
            )
            for _ in range(12):
                if (
                    workspace(root, users["a"], "stop").get("result", {}).get("state")
                    == "recycled"
                ):
                    break
                time.sleep(1)
            report["before_low"] = {
                "a": status("a", "recycled"),
                "b": status("b", "running"),
            }
            # Arm an independent one-shot reset before changing policy. Retry if the supervisor is down.
            run(
                "systemd-run",
                "--user",
                "--quiet",
                "--on-active=8m",
                "--timer-property=AccuracySec=1s",
                "--property=Restart=on-failure",
                "--property=RestartSec=30s",
                "--unit=" + unit,
                *baseline_cmd,
                env=systemd_env,
            )
            armed = True
            free = json.loads(run(*manager, "disk-watermark"))["available_bytes"]
            high_mib = free // MIB + 1024
            low = json.loads(
                run(
                    *setter,
                    "--low-mib",
                    str(high_mib),
                    "--recover-mib",
                    str(high_mib + 1024),
                )
            )
            report["low"] = sample_row(low, "low", deploy)
            require(
                low["available_bytes"] <= low["low_bytes"],
                "Expected threshold crossing on the actual disk",
            )
            for role, user in users.items():
                denial = workspace(root, user, "check-write")
                require(
                    not denial["ok"]
                    and denial["error"] == "workspace_disk_low_watermark",
                    "Expected write refusal",
                )
            start = workspace(root, users["a"], "ensure")
            require(
                not start["ok"] and "workspace_disk_low_watermark" in start["error"],
                "Expected cold start refusal",
            )
            report["low_admission"] = {
                "start_a": start["error"],
                "new_write_both": "workspace_disk_low_watermark",
            }
            mark("low")
            wait(browser, work, "low-verified", seconds=300)
            report["during_low"] = {
                "a": status("a", "recycled"),
                "b": status("b", "running"),
            }
            report["recovery"] = sample_row(
                json.loads(run(*baseline_cmd)), "normal", deploy
            )
            require(
                report["recovery"]["available_bytes"] >= policy["recover_bytes"],
                "Recovery margin not reached",
            )
            run("systemctl", "--user", "stop", unit + ".timer", env=systemd_env)
            armed = False
            mark("normal")
            wait(
                browser,
                work,
                "normal-verified",
                seconds=stack["startup_timeout_seconds"] + 120,
            )
            require(browser.wait(timeout=20) == 0, "Browser readback failed")
            report["after"] = {role: status(role, "running") for role in users}
            feed = json.loads(run(*manager, "disk-watermark", "--alerts"))
            events = [
                e
                for e in feed["alerts"]
                if e["sequence"] > report["baseline"]["sequence"]
            ]
            require(
                [e["notification"] for e in events] == ["firing", "resolved"]
                and all(e["deployment"] == deploy for e in events),
                "Expected one candidate alert pair",
            )
            ids = [e["id"] for e in events]
            deadline = time.monotonic() + 30
            while True:
                receipts = [
                    r
                    for r in (feed["receiver"] or {}).get("receipts", [])
                    if r["id"] in ids
                ]
                if (
                    not feed["subscribed"]
                    or [r["id"] for r in receipts] == ids
                    or time.monotonic() >= deadline
                ):
                    break
                time.sleep(1)
                feed = json.loads(run(*manager, "disk-watermark", "--alerts"))
            report.update(
                events=events,
                receiver_subscribed=feed["subscribed"],
                receiver_receipts=receipts,
                browser=read(work / "browser.json"),
            )
            with (output / "verified.json").open("x") as out:
                json.dump(report, out, indent=2)
                out.write("\n")
            require(
                not feed["subscribed"] or [r["id"] for r in receipts] == ids,
                "Configured receiver has outstanding acknowledgements",
            )
        finally:
            try:
                if armed:
                    sample_row(json.loads(run(*baseline_cmd)), "normal", deploy)
                    run("systemctl", "--user", "stop", unit + ".timer", env=systemd_env)
            finally:
                if browser.poll() is None:
                    mark("abort")
                    try:
                        browser.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        browser.terminate()
                        browser.wait(timeout=20)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state", type=Path, required=True, help="Candidate site directory"
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New output directory on the control disk",
    )
    args = parser.parse_args()
    os.umask(0o077)
    drill(args.state.resolve(), args.output.resolve())

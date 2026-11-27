"""Exercise the shared dev-m1 disk watermark through Chromium and the real quota filesystems."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from verify_openscience_m5_workspace_web import ROOT, json_write, wait_file, web_stack
from openscience_m5_quota_inputs import retained_state
from openscience_m5_storage_ssh import StorageGuest

MIB = 1024 * 1024
POOL = "/opt/nexpoly/ops/openscience-per-user-dev/water_pool.py"
MANAGER = ROOT / "scripts/openscience_per_user_dev.py"
OWNERS = ("test-user-a", "test-user-b")
LOW_MIB, RECOVER_MIB = 96, 160


def require(condition, detail):
    if not condition:
        raise RuntimeError(detail)


class Trial:
    def __init__(self, args):
        self.args = args
        self.guest = StorageGuest(args.vm / "ssh.conf")
        self.evidence = {}
        self.registry = args.registry_root / "src"

    def record(self, name, value):
        self.evidence[name] = value
        json_write(self.args.work / (name + ".json"), value)
        return value

    def manager(self, *command):
        result = subprocess.check_output(
            [sys.executable, MANAGER, "--state-dir", self.registry, *command],
            timeout=180,
        )
        return json.loads(result)

    def pool(self, *command):
        return json.loads(
            self.guest.run(
                "python3", POOL, *command, stdout=subprocess.PIPE, timeout=120
            ).stdout
        )

    def workspace(self, op, user):
        return self.manager(op, "--environment", "dev-m1", "--user-id", user)

    def wait_observed(self, state, minimum=0, maximum=None):
        # Read only the monitor's persisted observations: the operator/test
        # must not be the process which detects the threshold and sends the alert.
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            sample = self.manager("disk-watermark", "--observed")
            free = sample.get("available_bytes")
            if sample["state"] == state and isinstance(free, int) and free >= minimum:
                if maximum is None or free <= maximum:
                    require(
                        sample["write_allowed"] == (state == "normal"),
                        "Unexpected write policy",
                    )
                    return sample
            time.sleep(0.5)
        raise TimeoutError(
            f"Background monitor did not observe {state} in the requested range"
        )

    def alerts(self):
        return self.manager("disk-watermark", "--alerts")["alerts"]

    def filler_extent(self):
        # Check the actual operator allocation on each side of the browser
        # DELETE. Releasing this filler is reserved for teardown.
        raw = self.guest.run(
            "stat",
            "--format=%i %s %b",
            "--",
            "/mnt/nexpoly-m5-pool/.m5-global-drill/pressure.dat",
            capture_output=True,
            timeout=30,
        ).stdout.split()
        inode, size, blocks = map(int, raw)
        require(size > 0 and blocks > 0, "No allocated operator pressure fixture")
        return {"inode": inode, "size_bytes": size, "allocated_bytes": blocks * 512}

    def signal(self, marker):
        with (self.args.work / marker).open("x") as stream:
            stream.write(marker + "\n")

    def browser(self):
        work = self.args.work
        browser_output = work / "browser.json"
        with (work / "browser.log").open("x") as log:
            process = subprocess.Popen(
                [
                    "node",
                    ROOT / "scripts/ci/verify_openscience_m5_quota_browser.mjs",
                    "--state",
                    self.args.state,
                    "--markers",
                    work,
                    "--output",
                    browser_output,
                    "--phase",
                    "watermark",
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                wait_file(work / "watermark-browser-ready", process, 300)
                a = self.record(
                    "a_before_pressure", self.workspace("status", OWNERS[0])
                )
                b = self.record(
                    "b_before_pressure", self.workspace("status", OWNERS[1])
                )
                require(
                    a["state"] == "running" and b["state"] == "recycled",
                    "Expected warm A and cold B",
                )
                history = self.alerts()
                self.record("pressure", self.pool("pressure", "80"))
                self.record(
                    "low_observed", self.wait_observed("low", maximum=LOW_MIB * MIB)
                )
                self.record("kernel_blocked", self.pool("probe", "block"))
                retained = self.record("low_reap", self.workspace("reap", OWNERS[0]))
                require(retained["state"] == "running" and retained.get("reason") == "disk_recovery",
                        "The idle reaper must retain the warm user's low-water recovery path")
                self.signal("watermark-low")
                wait_file(work / "watermark-browser-denied", process, 300)
                denied = subprocess.run(
                    [
                        sys.executable,
                        MANAGER,
                        "--state-dir",
                        self.registry,
                        "ensure",
                        "--environment",
                        "dev-m1",
                        "--user-id",
                        OWNERS[1],
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                require(denied.returncode == 1, "Cold start should fail while low")
                reason = json.loads(denied.stderr)["error"]
                require(
                    all(
                        word in reason
                        for word in (
                            "workspace_capacity",
                            "global_disk",
                            "workspace_disk_low_watermark",
                        )
                    ),
                    "Cold start failed for a different reason",
                )
                self.record("manager_refusal", {"error": reason})
                cold = self.record(
                    "b_after_refusal", self.workspace("status", OWNERS[1])
                )
                require(
                    cold["state"] == "recycled"
                    and cold["generation"] == b["generation"],
                    "A denied cold start consumed a workspace generation",
                )
                require(
                    not subprocess.check_output(
                        ["docker", "ps", "-aq", "--filter", "volume=" + cold["volume"]]
                    ).strip(),
                    "Cold B unexpectedly has a container",
                )
                warm = self.record(
                    "a_after_refusal", self.workspace("status", OWNERS[0])
                )
                require(
                    a["container_id"] == warm["container_id"]
                    and warm["state"] == "running",
                    "The pressure replaced the incumbent backend",
                )
                self.record("partial_release", self.pool("pressure", "128"))
                self.record(
                    "hysteresis",
                    self.wait_observed("low", LOW_MIB * MIB + 1, RECOVER_MIB * MIB - 1),
                )
                incident = self.alerts()
                require(
                    len([e for e in incident if e not in history]) == 1,
                    "Expected one delivered low alert",
                )
                # Keep the operator filler in place. Only the user's subsequent
                # unlink should carry free space across the recovery threshold.
                self.record("before_user_delete", self.pool("pressure", "159"))
                self.record(
                    "still_low_before_delete",
                    self.wait_observed("low", 158 * MIB, RECOVER_MIB * MIB - 1),
                )
                filler = self.record("filler_before_delete", self.filler_extent())
                self.signal("watermark-before-delete")
                wait_file(work / "watermark-browser-deleted", process, 300)
                self.record(
                    "normal_observed", self.wait_observed("normal", RECOVER_MIB * MIB)
                )
                after_delete = self.record("after_user_delete", self.pool("sample"))
                require(
                    self.record("filler_after_delete", self.filler_extent()) == filler,
                    "The operator filler changed across user recovery",
                )
                require(
                    after_delete["available_bytes"] >= RECOVER_MIB * MIB,
                    "Deleting the visible project fixture must reclaim shared disk space",
                )
                delivered = [e for e in self.alerts() if e not in history]
                require(
                    len(delivered) == 2 and delivered[0] in incident,
                    "Expected one delivered low alert and one recovery",
                )
                firing, resolved = delivered
                require(
                    (firing["previous"], firing["state"], firing["notification"])
                    == ("normal", "low", "firing")
                    and (
                        resolved["previous"],
                        resolved["state"],
                        resolved["notification"],
                    )
                    == ("low", "normal", "resolved")
                    and firing["filesystem_uuid"]
                    == resolved["filesystem_uuid"]
                    == self.evidence["pool_before"]["filesystem_uuid"]
                    and firing["available_bytes"] <= LOW_MIB * MIB
                    and resolved["available_bytes"] >= RECOVER_MIB * MIB
                    and resolved["sequence"] == firing["sequence"] + 1
                    and self.evidence["hysteresis"]["last_transition"]["id"]
                    == firing["id"],
                    "Alert receipts disagree with the threshold, recovery or intervening samples",
                )
                self.record("alerts", delivered)
                self.record("kernel_recovered", self.pool("probe", "open"))
                self.signal("watermark-recovered")
                require(
                    process.wait(timeout=300) == 0, "Chromium failed; see browser.json"
                )
                report = json.loads(browser_output.read_bytes())
                require(report["status"] == "passed", "Missing browser pass")
                self.evidence["browser"] = report
                running = {owner: self.workspace("status", owner) for owner in OWNERS}
                require(
                    running[OWNERS[0]]["container_id"] == a["container_id"]
                    and running[OWNERS[1]]["generation"] == b["generation"] + 1
                    and all(v["state"] == "running" for v in running.values()),
                    "Expected the original warm A and newly started B after recovery",
                )
                for owner, row in running.items():
                    q = row["quota"]
                    require(
                        q["uid"] == row["uid"]
                        and q["accounting"]
                        and q["enforced"]
                        and q["hard_bytes"] == 2_000_000_000,
                        "User hard limit changed: " + owner,
                    )
                self.record("instances_after_recovery", running)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)

    def run(self):
        args = self.args
        previous = json.loads(args.previous.read_bytes())
        original = retained_state(args.original_registry)
        require(
            previous["status"] == "passed"
            and original == previous["retained_deployment_before_and_after"],
            "Expected preserved original dev-m1 deployment",
        )
        self.record("original_before", original)
        claim = json.loads((self.registry / "registry.json").read_bytes())[
            "native_claim"
        ]
        require(
            claim == previous["native_registrations"]["src"]["native_claim"],
            "Registry differs from the verified dev-m1 source owner",
        )
        rows = {u: self.workspace("status", u) for u in OWNERS}
        for user, row in rows.items():
            require(
                row["state"] == "recycled"
                and row["quota"]["filesystem_uuid"]
                == claim["filesystem_uuids"][user[-1]],
                "Expected a quiescent, retained source quota volume",
            )
        sample = self.record("pool_before", self.pool("sample"))
        self.record(
            "configuration",
            self.manager(
                "set-disk-watermark",
                "--filesystem-uuid",
                sample["filesystem_uuid"],
                "--low-mib",
                str(LOW_MIB),
                "--recover-mib",
                str(RECOVER_MIB),
            ),
        )
        self.record(
            "initial_observation", self.wait_observed("normal", RECOVER_MIB * MIB)
        )
        self.record("seed", self.pool("seed"))
        try:
            with web_stack("src", args, args.combined_gateway_image, verify_retries=bool(args.combined_gateway_image)) as stack:
                self.browser()
                if args.combined_gateway_image:
                    self.record("combined_services", {
                        key: stack[key] for key in ("gateway_image", "adapter_images", "model_manifest_sha256", "quota_files")
                    })
                    for stage in ("m3_adapters", "m3", "m4"):
                        output = args.work / (stage + ".json")
                        with (args.work / (stage + ".log")).open("x") as log:
                            subprocess.run(
                                ["node", ROOT / f"scripts/ci/verify_openscience_{stage}.mjs",
                                 "--state", args.state, "--output", output],
                                stdout=log, stderr=subprocess.STDOUT, check=True,
                            )
                        require(json.loads(output.read_bytes())["status"] == "passed", f"{stage} browser verification failed")
                        self.record(stage + "_verification", "passed")
        finally:
            # Only release the operator fixture on the pinned private pool.
            self.record("operator_cleanup", self.pool("release"))
            self.wait_observed("normal", RECOVER_MIB * MIB)
            for user in OWNERS:
                row = self.workspace("recycle", user)
                require(row["state"] == "recycled", "Could not recycle " + user)
            self.record("fixture_cleanup", self.pool("cleanup"))
        after = self.record("original_after", retained_state(args.original_registry))
        require(after == original, "Original private volume metadata changed")
        self.record("pool_after", self.pool("sample"))
        return dict(status="passed", environment="dev-m1", **self.evidence)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm", type=Path, required=True)
    parser.add_argument("--registry-root", type=Path, required=True)
    parser.add_argument(
        "--work", type=Path, required=True, help="New private checkpoint directory"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--combined-gateway-image",
                        help="Also verify six gateway tools, both adapters, model and knowledge on this quota candidate")
    parser.add_argument(
        "--state",
        type=Path,
        default=Path.home() / ".local/state/nexpoly-openscience-m2-dev",
    )
    parser.add_argument(
        "--original-registry",
        type=Path,
        default=Path.home() / ".local/state/nexpoly-openscience-m1-dev",
    )
    parser.add_argument(
        "--previous",
        type=Path,
        default=ROOT / "docs/verification/openscience-m5-workspace-quota-20261123.json",
    )
    args = parser.parse_args()
    os.umask(0o077)
    args.registry_root = args.registry_root.resolve(strict=True)
    args.state = args.state.resolve(strict=True)
    args.vm = args.vm.resolve(strict=True)
    args.work = args.work.absolute()
    if args.output.exists() or (args.state / "running.json").exists():
        parser.error("Use a new evidence file and a stopped development web stack")
    args.work.mkdir(mode=0o700)
    json_write(args.output, Trial(args).run())


if __name__ == "__main__":
    main()

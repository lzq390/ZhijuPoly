"""Verify the native M2 web stack against dedicated source and restoration quota volumes."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from openscience_m5_quota_inputs import retained_state, stage_inputs  # noqa: E402
from openscience_m5_storage_ssh import StorageGuest  # noqa: E402
from openscience_m5_backup import offline_registry  # noqa: E402
from openscience_per_user_dev import write_atomic  # noqa: E402

OPS = Path("ops/openscience-per-user-dev")
STEP = "/opt/nexpoly/scripts/ci/openscience_m5_guest_lifecycle.py"


def provision_code(guest):
    guest.run("test", "-f", "/inputs/spec.json")
    paths = [
        Path("scripts") / name
        for name in (
            "openscience_per_user_dev.py",
            "openscience_m5_archive.py",
            "openscience_m5_backup.py",
            "openscience_m5_quota_bundle.py",
            "ci/openscience_m5_guest_lifecycle.py",
        )
    ]
    paths.extend(OPS / name for name in ("quota.py", "quota-guest.py", "quota_nfs.py"))
    with tempfile.TemporaryFile() as stream:
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for path in paths:
                archive.add(ROOT / path, arcname=str(path))
        stream.seek(0)
        guest.run("tar", "-xf", "-", "-C", "/opt/nexpoly", stdin=stream)


def json_write(path, value):
    write_atomic(
        path, json.dumps(value, sort_keys=True, indent=2).encode() + b"\n", 0o600
    )


def wait_file(path, process, timeout):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if process.poll() is not None:
            raise RuntimeError(
                f"{process.args[1]} exited with status {process.returncode}; see the drill log"
            )
        if path.is_file():
            return
        time.sleep(0.4)
    raise TimeoutError(f"Timed out waiting for {path.name}")


@contextmanager
def web_stack(phase, args, combined_gateway=None, *, verify_retries=False, operator_id=None):
    info = args.state / "running.json"
    if info.exists():
        raise ValueError("The retained M2 site already has a running stack")
    with (args.work / (phase + "-stack.log")).open("a") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "scripts/openscience_m2_dev.py"),
                "--state-dir",
                str(args.state),
                "--registry",
                str(args.registry_root / phase),
                "--m5-quota-files",
                *(["--m6-operator-id", operator_id] if operator_id is not None else []),
                *(["--m3-gateway-image", combined_gateway, "--m3-adapters", "--m4"]
                  if combined_gateway else []),
                *(["--verify-retries"] if verify_retries else []),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            wait_file(info, process, 420 if combined_gateway else 240)
            running = json.loads(info.read_bytes())
            if not running.get("quota_files") or running["registry"] != str(
                args.registry_root / phase
            ):
                raise RuntimeError(
                    "Browser stack belongs to another storage configuration"
                )
            if combined_gateway and (running.get("gateway_image") != combined_gateway
                                     or not running.get("adapter_images") or running.get("m4") is not True):
                raise RuntimeError("Browser stack is missing requested gateway, adapters or M4 services")
            if running.get("m6_operator_id") != operator_id:
                raise RuntimeError("Operator scope differs from the requested development candidate")
            yield running
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=180)
            if process.returncode:
                raise RuntimeError(
                    f"M2 {phase} stack exited with status {process.returncode}"
                )


class Drill:
    def __init__(self, args):
        self.args = args
        self.guest = StorageGuest(args.ssh)
        self.results = {}

    def step(self, name, *command):
        print(name, flush=True)
        raw = self.guest.run("python3", STEP, *command, stdout=subprocess.PIPE).stdout
        self.results[name] = json.loads(raw)
        json_write(self.args.work / (name + ".json"), self.results[name])
        return self.results[name]

    def browser(self, name, phase):
        print(name, flush=True)
        output = self.args.work / (name + ".json")
        state = self.args.state
        with (self.args.work / (name + ".log")).open("w") as log:
            process = subprocess.Popen(
                [
                    "node",
                    str(ROOT / "scripts/ci/verify_openscience_m5_quota_browser.mjs"),
                    "--state",
                    str(state),
                    "--markers",
                    str(self.args.work),
                    "--output",
                    str(output),
                    "--phase",
                    phase,
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                if phase == "full":
                    wait_file(self.args.work / "quota-browser-ready", process, 900)
                    self.step("kernel_at_refusal", "fill")
                    with (self.args.work / "quota-browser-full").open("x") as marker:
                        marker.write("kernel limit reached\n")
                if process.wait(600) != 0:
                    raise RuntimeError(f"Browser verification failed; see {output}")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(15)
        self.results[name] = json.loads(output.read_bytes())
        if self.results[name].get("status") != "passed":
            raise ValueError("Browser did not confirm all required actions")
        if phase == "full":
            for marker in ("quota-browser-ready", "quota-browser-full"):
                (self.args.work / marker).unlink()

    def manager(self, phase, *command):
        raw = subprocess.check_output(
            [
                sys.executable,
                str(ROOT / "scripts/openscience_per_user_dev.py"),
                "--state-dir",
                str(self.args.registry_root / phase),
                *command,
            ]
        )
        return json.loads(raw)

    def record(self, key, value):
        self.results[key] = value
        json_write(self.args.work / (key + ".json"), value)
        return value

    def action(self, phase, role, op):
        owner = self.spec["users"][role]
        row = self.manager(
            phase, op, "--environment", "dev-m1", "--user-id", owner["user_id"]
        )
        matches = all(
            row[k] == owner[k]
            for k in ("uid", "gid", "workspace", "environment", "user_id")
        )
        q = row["quota"]
        if not (
            matches
            and row["storage_backend"] == "nfs-ext4-user-quota"
            and q["accounting"]
            and q["enforced"]
            and q["uid"] == owner["uid"]
            and q["hard_bytes"] == 2_000_000_000
        ):
            raise ValueError(
                "Native manager returned a different identity, volume policy or quota"
            )
        return row

    def register(self, phase):
        print("Register native " + phase, flush=True)
        cfg = self.manager(
            phase,
            "init",
            "--image",
            self.spec["workspace_image"],
            "--idle-seconds",
            "86400",
        )
        facts = self.step(phase + "_storage", "inspect", "--phase", phase)
        rows = []
        for role, owner in self.spec["users"].items():
            generation = (
                owner["registered_generation"]
                if phase == "src"
                else self.results["source_offline"][role]["generation"]
            )
            rows.append(
                {
                    **{
                        k: owner[k]
                        for k in ("environment", "user_id", "uid", "workspace")
                    },
                    "generation": generation,
                    "mount": "/mnt/openscience-m5-quota/" + phase + "-" + role,
                    "filesystem_uuid": facts[role]["kernel"]["filesystem_uuid"],
                }
            )
        path = self.args.work / (phase + "-bindings.json")
        json_write(path, rows)
        observed = self.manager(
            phase,
            "register-quota-nfs",
            "--manifest",
            str(self.args.storage_manifest),
            "--phase",
            phase,
            "--bindings",
            str(path),
        )
        if observed["deployment"] != cfg["deployment"]:
            raise ValueError("Quota registry changed during registration")
        self.record(phase + "_registration", observed)

    def both(self, name, phase, op):
        print(name, flush=True)
        rows = {r: self.action(phase, r, op) for r in ("a", "b")}
        expected = "recycled" if op == "recycle" else "running"
        if any(v["state"] != expected or v.get("reason") for v in rows.values()):
            raise ValueError("Native manager did not finish " + op)
        return self.record(name, rows)

    def cycle(self):
        before = self.both("before_recycle", "src", "status")
        initial = {r: process_facts(v) for r, v in before.items()}
        self.both("recycled", "src", "recycle")
        fresh = self.both("recreated", "src", "ensure")
        fresh_facts = {r: process_facts(v) for r, v in fresh.items()}
        again = self.both("restarted", "src", "restart")
        for r, row in again.items():
            latest = process_facts(row)
            if not (
                fresh[r]["generation"] > before[r]["generation"]
                and fresh[r]["container_id"] != before[r]["container_id"]
                and row["container_id"] == fresh[r]["container_id"]
                and latest["started_at"] != fresh_facts[r]["started_at"]
                and row["volume"] == fresh[r]["volume"] == before[r]["volume"]
                and initial[r]["file"] == fresh_facts[r]["file"] == latest["file"]
                and self.results["web_full"]["stages"][
                    "a_recovered" if r == "a" else "b_while_a_full"
                ]["notebook_sha256"]
                == latest["file"]["sha256"]
            ):
                raise ValueError(
                    "Container lifecycle, quota binding or visible notebook changed"
                )
            self.record(
                "lifecycle_" + r,
                dict(before=initial[r], recreated=fresh_facts[r], restarted=latest),
            )

    def run(self):
        args = self.args
        before = retained_state(args.original_registry)
        self.record("retained_before", before)
        with tempfile.TemporaryDirectory() as stage:
            self.spec = stage_inputs(
                Path(stage), args.original_registry, args.packages, args.pins
            )
        spec_guest = json.loads(
            self.guest.run("cat", "/inputs/spec.json", capture_output=True).stdout
        )
        if self.spec != spec_guest:
            raise ValueError(
                "Guest inputs differ from the pinned live dev-m1 owners and backups"
            )
        provision_code(self.guest)
        self.step("original_archive_readback", "prepare")
        self.register("src")
        self.both("source_runtime", "src", "ensure")
        with web_stack("src", args):
            self.browser("web_full", "full")
        self.step("space_released", "unfill")
        return self.finish_lifecycle(before)

    def finish_lifecycle(self, before):
        args = self.args
        self.cycle()
        with web_stack("src", args):
            self.browser("web_after_restart", "readback")
        rows = self.both("source_offline", "src", "recycle")
        with offline_registry(args.registry_root / "src"):
            for row in rows.values():
                ids = subprocess.check_output(
                    ["docker", "ps", "-aq", "--filter", "volume=" + row["volume"]],
                    text=True,
                )
                if ids.strip():
                    raise ValueError("A container still holds the source volume")
            self.step("backup_restore", "recover")
        self.register("dst")
        restored = self.both("restored_runtime", "dst", "ensure")
        with web_stack("dst", args):
            self.browser("web_after_restore", "readback")
            for r, row in restored.items():
                facts = process_facts(row)
                archive = self.results["backup_restore"][r]
                if (
                    facts["file"]["sha256"] != archive["notebook"]["sha256"]
                    or row["quota"]["filesystem_uuid"]
                    != archive["restored"]["quota"]["filesystem_uuid"]
                ):
                    raise ValueError(
                        "The restored backend read different files or a different filesystem"
                    )
                self.record("restored_process_" + r, facts)
        self.both("restore_offline", "dst", "recycle")
        after = retained_state(args.original_registry)
        if before != after:
            raise RuntimeError("Retained deployment metadata or lifecycle changed")
        return dict(
            status="passed",
            environment="dev-m1",
            workspace_image=self.spec["workspace_image"],
            retained_after=after,
            **self.results,
        )


def process_facts(row):
    cid = row["container_id"]
    limit = time.monotonic() + 90
    while True:
        status, started = (
            subprocess.check_output(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Health.Status}} {{.State.StartedAt}}",
                    cid,
                ],
                text=True,
            )
            .strip()
            .split()
        )
        if status == "healthy":
            break
        if time.monotonic() >= limit:
            raise RuntimeError("Native Docker health probe did not pass")
        time.sleep(1)
    probe = """import hashlib,json,os,pathlib
p=pathlib.Path('/openscience/files/projects/same-project/notebook.txt')
s=p.stat()
print(json.dumps(dict(uid=os.geteuid(),gid=os.getegid(),file_uid=s.st_uid,file_gid=s.st_gid,
                     sha256=hashlib.sha256(p.read_bytes()).hexdigest())))"""
    observed = json.loads(
        subprocess.check_output(["docker", "exec", cid, "python3", "-ISB", "-c", probe])
    )
    if not all(
        observed[k] == row["uid"] for k in ("uid", "gid", "file_uid", "file_gid")
    ):
        raise ValueError(
            "Container or restored file ownership differs from the account"
        )
    return dict(container=cid, started_at=started, health=status, file=observed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh", type=Path, required=True)
    parser.add_argument(
        "--work", type=Path, required=True, help="New private directory for checkpoints"
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="New acceptance evidence file"
    )
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
    parser.add_argument("--storage-manifest", type=Path, required=True)
    parser.add_argument(
        "--registry-root",
        type=Path,
        required=True,
        help="New, short host path for persistent native registries",
    )
    parser.add_argument("--packages", type=Path, required=True)
    parser.add_argument(
        "--pins",
        type=Path,
        default=ROOT / "docs/verification/openscience-m5-restore-20261123.json",
    )
    args = parser.parse_args()
    os.umask(0o077)
    if args.output.exists():
        parser.error("Choose a fresh output file")
    args.registry_root = args.registry_root.absolute()
    if args.registry_root.exists():
        parser.error("Choose a new native registry parent")
    args.registry_root.mkdir(mode=0o700, parents=True)
    args.work.mkdir(mode=0o700)
    drill = Drill(args)
    try:
        report = drill.run()
    except BaseException:
        # Retain clone data and checkpoints, but release this invocation's
        # resident containers through the manager's normal activity checks.
        for phase in ("src", "dst"):
            if phase + "_registration" not in drill.results:
                continue
            for role in ("a", "b"):
                try:
                    result = drill.action(phase, role, "recycle")
                    if result["state"] != "recycled":
                        print(
                            f"Recovery required for {phase}/{role}: {result.get('reason')}",
                            file=sys.stderr,
                        )
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    print(
                        f"Recovery required for {phase}/{role}: {type(error).__name__}",
                        file=sys.stderr,
                    )
        raise
    json_write(args.output, report)


if __name__ == "__main__":
    main()

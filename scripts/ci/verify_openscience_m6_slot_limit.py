"""Verify a full two-slot manager rejects a third workspace and admits it after one slot is released.

The deploy-m6 roster allows exactly two accounts. This complementary exercise uses
the same manager, November workspace image and budgets in a disposable development
registry that can bind three distinct users. Its Docker volumes are local to the test.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

from verify_openscience_m5_capacity import private_marker
from verify_openscience_per_user_dev import (
    MANAGER,
    check,
    generated_resources,
    run,
    together,
)


def verify(candidate, evidence):
    config = json.loads((candidate / "registry.json").read_bytes())
    check(
        config.get("max_active") == 2
        and set(config.get("candidate_owners", {})) == {"a", "b"},
        "Expected the enrolled two-user reference candidate",
    )
    check(
        all(
            o["environment"] == "deploy-m6" for o in config["candidate_owners"].values()
        ),
        "Wrong candidate",
    )
    policy = config["capacity"]
    environment = "dev-m6-slots"
    users = [str(uuid4()) for _ in range(3)]
    check(
        set(users).isdisjoint(
            owner["user_id"] for owner in config["candidate_owners"].values()
        ),
        "Account overlap",
    )
    report = {
        "started": datetime.now(timezone.utc).isoformat(),
        "status": "failed",
        "user_ids": users,
        "image": config["image"],
        "policy_mib": policy,
        "max_active": 2,
        "reference_deployment": config["deployment"],
        "storage": "new local Docker volumes",
    }
    with TemporaryDirectory(prefix="np-slots-") as directory:
        root = Path(directory)
        cli = [sys.executable, "-B", str(MANAGER), "--state-dir", directory]
        result = json.loads(
            run(*cli, "init", "--image", config["image"], "--max-active", "2")
        )
        deployment = report["deployment"] = result["deployment"]
        check(deployment != config["deployment"], "Registry must be independent")
        flags = [
            arg
            for k, value in policy.items()
            for arg in ("--" + k.replace("_", "-"), str(value))
        ]
        check(
            json.loads(run(*cli, "set-capacity", *flags))["capacity"] == policy,
            "Policy mismatch",
        )

        def supervisor(op, user):
            with socket.socket(socket.AF_UNIX) as sock:
                sock.settimeout(180)
                sock.connect(str(root / "supervisor.sock"))
                sock.sendall(
                    json.dumps(
                        dict(operation=op, environment=environment, user_id=user)
                    ).encode()
                    + b"\n"
                )
                with sock.makefile("rb") as stream:
                    return json.loads(stream.readline(65536))

        def action(op, user):
            reply = supervisor(op, user)
            check(reply.get("ok") is True, f"{op} failed: {reply.get('error')}")
            return reply["result"]

        service = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(MANAGER.with_name("openscience_per_user_service.py")),
                "--state-dir",
                directory,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        try:
            for _ in range(100):
                if (root / "supervisor.sock").is_socket():
                    break
                check(service.poll() is None, "Private supervisor exited")
                time.sleep(0.1)
            # First start and stop the third user, proving its account and volume work before saturation.
            third = action("ensure", users[2])
            private_marker(third, users[2], write=True)
            check(
                action("stop", users[2])["state"] == "recycled",
                "Could not free the third user's slot",
            )
            active = together(users[:2], lambda user: action("ensure", user))
            for user, binding in active.items():
                private_marker(binding, user, write=True)
            before = generated_resources(deployment)
            check(
                len(before["containers"]) == 4 and len(before["volumes"]) == 3,
                "Expected two pairs and three volumes",
            )
            refused = supervisor("ensure", users[2])
            check(
                refused
                == {
                    "ok": False,
                    "error": "workspace_capacity: instance_count (required=3, limit=2)",
                },
                f"Unexpected refusal: {refused}",
            )
            after = generated_resources(deployment)
            check(
                all(set(before[k]) == set(after[k]) for k in before),
                "Refusal displaced a resident or allocated a resource",
            )
            check(
                action("status", users[2])["state"] == "recycled",
                "Third user started while over the limit",
            )
            for user, prior in active.items():
                current = action("ensure", user)
                check(
                    current["container_id"] == prior["container_id"]
                    and current["generation"] == prior["generation"],
                    "Resident identity changed",
                )
                private_marker(current, user, write=True)
            check(
                action("stop", users[0])["state"] == "recycled", "One slot must be free"
            )
            retry = action("ensure", users[2])
            check(
                retry["volume"] == third["volume"]
                and retry["generation"] > third["generation"],
                "Third user's retry failed to recover its workspace",
            )
            private_marker(retry, users[2])
            private_marker(active[users[1]], users[1])
            report.update(
                refused=refused["error"],
                users_after_refusal=[
                    {
                        k: row[k]
                        for k in (
                            "user_id",
                            "uid",
                            "workspace",
                            "volume",
                            "container_id",
                            "generation",
                        )
                    }
                    for row in active.values()
                ],
                third_volume=third["volume"],
                third_generation_after_retry=retry["generation"],
                third_private_file_recovered=True,
                both_residents_read_write=True,
                resources_unchanged_at_refusal=True,
            )
        finally:
            service.terminate()
            service.wait(timeout=180)
            # Only remove resources carrying the fresh random deployment's ownership label.
            owned = generated_resources(deployment)
            for cid in owned["containers"]:
                run("docker", "rm", "-f", cid)
            for volume in owned["volumes"]:
                run("docker", "volume", "rm", volume)
            remain = generated_resources(deployment)
            check(not any(remain.values()), "Test resources remain")
            report["resources_after_cleanup"] = remain
        report.update(status="passed", ended=datetime.now(timezone.utc).isoformat())
        json.dump(report, evidence, indent=2)
        evidence.write("\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    with args.output.open("x") as evidence:
        verify(args.reference_registry, evidence)

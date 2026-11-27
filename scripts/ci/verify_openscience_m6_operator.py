"""Rehearse operator-only entry and rollback on the two retained dev-m1 quota clones."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from openscience_m3_ledger import OWNERS, pg_target, query
from verify_openscience_m5_rollback import BASELINE, binding_pins, attest_running, owner_counts, read
from verify_openscience_m5_watermark import Trial, require
from verify_openscience_m5_workspace_web import ROOT, json_write, web_stack
from openscience_m5_quota_inputs import retained_state
from openscience_per_user_dev import local_engine

ROUND3 = ROOT / "docs/verification/openscience-m5-review-round3-20261124.json"
ROSTER = {owner: f"test-user-{role}" for role, owner in OWNERS.items()}
POLICY = """
SELECT jsonb_build_object('enabled', e.enabled, 'bindings', (
     SELECT jsonb_object_agg(user_id::text,workspace_user_id) FROM openscience_m2.workspace_bindings
     WHERE environment=e.environment AND enabled), 'active_handoffs', (
     SELECT count(*) FROM openscience_m2.handoffs WHERE environment=e.environment AND revoked_at IS NULL
       AND (redeemed_at IS NOT NULL OR ticket_expires_at>clock_timestamp())))
FROM openscience_m2.environments e WHERE e.environment='dev-m1';
"""


def gate(state, deployment, action):
    local_engine()
    stack = read(state / "running.json")
    require(
        stack["deployment"] == deployment
        and (state / "deployment").read_text().strip() == deployment
        and stack["state_dir"] == str(state)
        and stack.get("quota_files")
        and stack.get("m6_operator_id") == OWNERS["a"]
        and stack["parent"] == "https://app.m2.test:18760"
        and stack["agent"] == "https://agent.m2.test:18761",
        "Only the active isolated two-owner M6 operator drill is eligible",
    )
    pg = pg_target(stack)
    if action == "status":
        (result,) = query(pg, POLICY, {})
    else:
        require(action in ("open", "close"), "Unknown gate operation")
        # Require the same enabled development bindings in the transaction that
        # flips the environment switch. Existing DB triggers invalidate tickets and sessions.
        sql = (
            """
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='12s';
SELECT 1 FROM openscience_m2.environments WHERE environment='dev-m1' FOR UPDATE;
SELECT 1 FROM openscience_m2.workspace_bindings WHERE environment='dev-m1' ORDER BY user_id FOR SHARE;
UPDATE openscience_m2.environments SET enabled=:'open'::boolean
 WHERE environment='dev-m1' AND (
     SELECT jsonb_object_agg(user_id::text,workspace_user_id) FROM openscience_m2.workspace_bindings
     WHERE environment='dev-m1' AND enabled)=:'owners'::jsonb;
"""
            + POLICY
            + "COMMIT;"
        )
        command = (
            'export PGHOST=/m2pg PGUSER=m2_admin PGPASSWORD="$(cat /admin-pw)"; '
            'exec psql -XqAt -v ON_ERROR_STOP=1 -d nexpoly_m2_dev -v "open=$1" -v "owners=$2"'
        )
        done = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                pg,
                "sh",
                "-ec",
                command,
                "m6",
                "true" if action == "open" else "false",
                json.dumps(ROSTER),
            ],
            input=sql,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        result = json.loads(done.stdout.splitlines()[-1])
        require(result["enabled"] == (action == "open"), "The development environment switch did not change")
    require(result["bindings"] == ROSTER, "Existing development roster differs from the retained fixture")
    if not result["enabled"]:
        require(result["active_handoffs"] == 0, "Closing the gate left an outstanding handoff")
    return result


def drill(args):
    local_engine()
    baseline, previous = read(BASELINE), read(ROUND3)
    images = previous["images"]
    reg = args.registry_root / "src"
    manifest = read(reg / "registry.json")
    claim = baseline["native_registrations"]["src"]["native_claim"]
    require(
        previous["status"] == "passed_dev_m1_two_owners"
        and manifest["native_claim"] == claim
        and manifest["image"] == images["rollback"]
        and not (args.state / "running.json").exists(),
        "Expected the retained, stopped M5 deployment at the reviewed original image",
    )
    operator = OWNERS["a"]
    original = retained_state(args.original_registry)
    require(original == baseline["retained_deployment_before_and_after"], "Original M1 identity changed")
    trial = Trial(args)

    def rows():
        return trial.manager("list")["workspaces"]

    initial = rows()
    pins = binding_pins(initial)
    require(
        len(initial) == 2
        and all(
            r["state"] == "recycled"
            and r["environment"] == "dev-m1"
            and r["uid"] == previous["final"]["workspace_states"][r["user_id"]]["uid"]
            and r["quota"]["filesystem_uuid"]
            == previous["final"]["workspace_states"][r["user_id"]]["filesystem_uuid"]
            and r["quota"]["enforced"]
            and r["quota"]["hard_bytes"] == 2_000_000_000
            for r in initial
        ),
        "Both registered 2 GB development workspaces must be recycled",
    )
    args.work.mkdir(mode=0o700)
    evidence = {
        "environment": "dev-m1",
        "scope": "operator rehearsal",
        "operator": "test-user-a",
        "control": "test-user-b",
        "images": {key: images[key] for key in ("candidate", "rollback", "gateway")},
        "phases": {},
    }
    old_counts = None
    try:
        for phase in ("candidate", "rollback"):
            current = rows()
            require(
                binding_pins(current) == pins and all(r["state"] == "recycled" for r in current),
                "Stop both test instances before changing the image",
            )
            require(
                trial.manager("set-image", "--image", images[phase])["image"] == images[phase],
                "Image selection failed",
            )
            with web_stack("src", args, images["gateway"], operator_id=operator) as stack:
                start = gate(args.state, stack["deployment"], "status")
                require(start["enabled"] == (phase == "candidate"), "Start from the expected durable gate state")
                try:
                    before = owner_counts(stack)
                    if old_counts is None:
                        old_counts = before
                    require(before == old_counts, "Owner ledger counts changed during image rollback")
                    with (args.work / (phase + ".log")).open("x") as log:
                        subprocess.run(
                            [
                                "node",
                                ROOT / "scripts/ci/verify_openscience_m6_operator.mjs",
                                "--phase",
                                phase,
                                "--state",
                                args.state,
                                "--work",
                                args.work,
                            ],
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            timeout=550,
                            check=True,
                        )
                    report = read(args.work / (phase + ".json"))
                    require(report["status"] == "passed", "Browser verification did not finish")
                    current = rows()
                    require(binding_pins(current) == pins, "Workspace ownership changed")
                    active = [r for r in current if r["user_id"] == "test-user-a"]
                    attest_running(
                        active, [p for p in pins if p[0] == "test-user-a"], images[phase], claim["deployment"]
                    )
                    (control,) = [r for r in current if r["user_id"] == "test-user-b"]
                    require(control["state"] == "recycled", "Ordinary member started an instance")
                    end = gate(args.state, stack["deployment"], "status")
                    require(
                        end["enabled"] == (phase == "rollback") and owner_counts(stack) == before,
                        "Access gate or owner ledger differs after exercise",
                    )
                    evidence["phases"][phase] = {
                        "initial_gate": start["enabled"],
                        "final_gate": end["enabled"],
                        "inspected_operator_image": active[0]["inspected_image"],
                        "ordinary_member_state": control["state"],
                        "owner_ledger_counts_match": True,
                        "ledger_buckets_checked": sorted(before),
                        "browser": report,
                    }
                except BaseException:
                    # Recover the pre-drill QA switch only in this guarded operator-only stack.
                    gate(args.state, stack["deployment"], "open")
                    raise
    finally:
        current = rows()
        require(
            binding_pins(current) == pins and all(r["state"] == "recycled" for r in current),
            "Expected both original registered quota volumes to be quiescent",
        )
        if read(reg / "registry.json")["image"] != images["rollback"]:
            trial.manager("set-image", "--image", images["rollback"])
    evidence["offline_notebooks"] = {}
    for user in ROSTER.values():
        dest = args.work / (user + "-notebook")
        subprocess.run(
            [
                sys.executable,
                ROOT / "scripts/openscience_m5_offline_file.py",
                "--state-dir",
                reg,
                "--user-id",
                user,
                "--path",
                "same-project/notebook.txt",
                "read",
                "--output",
                dest,
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        digest = hashlib.sha256(dest.read_bytes()).hexdigest()
        require(
            digest == previous["rollback"]["files_panel"][user]["notebook_sha256"], "Restored owner file differs"
        )
        evidence["offline_notebooks"][user] = digest
    require(original == retained_state(args.original_registry), "Original private M1 registry changed")
    evidence.update(status="passed", original_private_registry_verified=True, both_instances_recycled=True)
    json_write(args.work / "summary.json", evidence)
    print("M6 development operator rehearsal passed", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    for name in ("vm", "registry-root", "state", "work"):
        run.add_argument("--" + name, type=Path, required=True)
    run.add_argument(
        "--original-registry", type=Path, default=Path.home() / ".local/state/nexpoly-openscience-m1-dev"
    )
    control = sub.add_parser("gate")
    control.add_argument("--state", type=Path, required=True)
    control.add_argument("--deployment", required=True)
    control.add_argument("action", choices=("status", "open", "close"))
    args = parser.parse_args()
    os.umask(0o077)
    args.state = args.state.resolve(strict=True)
    if args.command == "gate":
        print(json.dumps(gate(args.state, args.deployment, args.action)))
        return
    for key in ("vm", "registry_root", "original_registry"):
        setattr(args, key, getattr(args, key).resolve(strict=True))
    args.work = args.work.absolute()
    drill(args)


if __name__ == "__main__":
    main()

"""Verify the restored dev-m1 workspace image against its two original owners and candidate conversations."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from verify_openscience_m5_watermark import Trial, require
from verify_openscience_m5_workspace_web import ROOT, json_write, wait_file, web_stack
from openscience_m5_quota_inputs import retained_state
from openscience_per_user_dev import PREFIX
from openscience_m3_ledger import OTHERS, OWNERS, pg_target, query
from openscience_m4_ledger import TABLES

BASELINE = ROOT / "docs/verification/openscience-m5-workspace-quota-20261123.json"
GUEST = Path(__file__).with_name("openscience_m5_rollback_guest.py")


def read(path):
    return json.loads(path.read_bytes())


def candidate_evidence(reports, rollback_image, gateway_image):
    m3, m4 = (read(reports / (suite + ".json")) for suite in ("m3", "m4"))
    for report in (m3, m4):
        require(report.get("status") == "passed" and report.get("environment") == "dev-m1"
                and set(report["users"]) == {"a", "b"}, "Expected passed dev-m1 M3 and M4 reports for A and B")
    candidate = m4["workspace_image"]
    require(re.fullmatch(r"sha256:[a-f0-9]{64}", candidate) and candidate != rollback_image
            and m3["gateway_image"] == gateway_image and m3.get("workspace_image", candidate) == candidate,
            "Candidate and rollback image evidence differ from their expected digests")
    sessions = []
    for role in ("a", "b"):
        model = m4["users"][role]["model"]
        sessions += [m3["users"][role]["workspace_session"], model["workspace_session"]]
        require(re.fullmatch(r"msg_[a-zA-Z0-9_-]+", model["turn_id"])
                and re.fullmatch(r"[0-9a-f]{64}", model["answer_sha256"]), "Invalid candidate model turn")
    require(len(set(sessions)) == 4 and all(re.fullmatch(r"ses_[a-zA-Z0-9_-]+", s) for s in sessions),
            "Expected four owner-specific candidate session identifiers")
    return candidate


def binding_pins(rows):
    return sorted((row["user_id"], row["workspace"], row["volume"], row["uid"], row["gid"],
                   row["quota"]["hard_bytes"], row["quota"]["filesystem_uuid"]) for row in rows)


def owner_counts(stack):
    # Use the existing read-only query guard for the labelled, network-isolated development database.
    pg = pg_target(stack)
    tables = {key: value for key, value in OTHERS.items() if key not in {"logins", "request_observations"}}
    tables.update({name: (table, "owner_id") for name, table in TABLES.items()})
    statements = [f"SELECT '{name}' AS bucket, {owner}::text AS owner, count(*) AS n FROM {table} GROUP BY {owner}"
                  for name, (table, owner) in tables.items()]
    rows, = query(pg, "SELECT jsonb_agg(to_jsonb(s)) FROM (" + " UNION ALL ".join(statements) + ") s;", {})
    return {name: {r["owner"]: r["n"] for r in rows if r["bucket"] == name and r["owner"] in OWNERS.values()}
            for name in tables}


def attest_running(rows, pins, image, deployment):
    require(binding_pins(rows) == pins, "Running owners differ from the original volume bindings")
    for row in rows:
        require(row["state"] == "running" and re.fullmatch(r"[0-9a-f]{64}", row["container_id"]),
                "Expected an active backend for each owner")
        inspected = json.loads(subprocess.check_output(["docker", "container", "inspect", row["container_id"]]))[0]
        labels = inspected["Config"]["Labels"]
        expected = {"environment": "dev-m1", "role": "backend", "deployment": deployment,
                    "workspace": row["workspace"], "uid": str(row["uid"]), "generation": str(row["generation"])}
        require(inspected["State"]["Running"] and inspected["Image"] == image
                and all(labels.get(PREFIX + k) == v for k, v in expected.items()),
                "Rollback backend's running image or development owner labels differ")
        row["inspected_image"] = inspected["Image"]


def rollback(args):
    baseline = read(BASELINE)
    original = retained_state(args.original_registry)
    require(baseline["status"] == "passed" and baseline["environment"] == "dev-m1"
            and original == baseline["retained_deployment_before_and_after"]
            and args.expected_image == baseline["workspace_image"], "Retained M1 baseline or rollback image differs")
    trial = Trial(args)
    registry = read(trial.registry / "registry.json")
    claim = baseline["native_registrations"]["src"]["native_claim"]
    require(registry["image"] == args.expected_image and registry["native_claim"] == claim,
            "Switch the quiescent source registry back to its original image and verified quota volumes first")
    expected = {role: {"workspace": row["backup"]["quota"]["identity"]["workspace"],
                       "notebook": row["notebook"], "uuid": claim["filesystem_uuids"][role]}
                for role, row in baseline["backup_restore"].items()}
    before = trial.manager("list")["workspaces"]
    pinned = binding_pins(before)
    expected_pins = []
    for role in ("a", "b"):
        row = baseline["lifecycle"][role]["before_recycle"]
        expected_pins.append(("test-user-" + role, row["workspace"], row["volume"], row["uid"], row["gid"],
                              row["quota"]["hard_bytes"], claim["filesystem_uuids"][role]))
    require(pinned == sorted(expected_pins) and all(r["state"] == "recycled"
            and r["storage_backend"] == "nfs-ext4-user-quota" for r in before),
            "Expected the two previously verified, recycled quota-backed dev-m1 source volumes")

    def guest(action):
        result = trial.guest.run("python3", "-IB", "-", action, token, json.dumps(expected),
                                 input=GUEST.read_bytes(), stdout=subprocess.PIPE, timeout=180)
        return trial.record(action, json.loads(result.stdout))

    if args.cleanup:
        token = (args.work / "run-id").read_text().strip()
        guest("cleanup")
        return
    candidate = candidate_evidence(args.candidate_reports, args.expected_image, args.gateway_image)
    args.work.mkdir(mode=0o700)
    token = uuid.uuid4().hex
    (args.work / "run-id").write_text(token)
    trial.record("bindings-before", before)
    guest("seed")
    try:
        with web_stack("src", args, args.gateway_image) as stack:
            require(not stack.get("retry_evidence") and not any(stack.get("adapter_retry_evidence", {}).values()),
                    "Rollback requires the ordinary gateway with retry-injection disabled")
            counts = trial.record("counts-before", owner_counts(stack))
            trial.record("stack", {k: stack[k] for k in ("quota_files", "gateway_image", "adapter_images", "model_image")})
            with (args.work / "browser.log").open("x") as log:
                cmd = ["node", ROOT / "scripts/ci/verify_openscience_m5_quota_browser.mjs", "--phase", "rollback-full",
                       "--run-id", token, "--state", args.state, "--markers", args.work, "--output", args.work / "browser.json",
                       "--candidate-reports", args.candidate_reports]
                with subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT) as browser:
                    try:
                        wait_file(args.work / "quota-browser-ready", browser, 720)
                        warm = trial.manager("list")["workspaces"]
                        attest_running(warm, pinned, args.expected_image, claim["deployment"])
                        trial.record("live-bindings", warm)
                        guest("fill")
                        pool = trial.record("pool-during-uid-refusal", trial.manager("disk-watermark"))
                        require(pool["state"] == "normal" and pool["write_allowed"], "UID refusal also hit the global waterline")
                        (args.work / "quota-browser-full").touch(exist_ok=False)
                        require(browser.wait(timeout=550) == 0, "Rollback browser failed; inspect browser.log")
                    finally:
                        if browser.poll() is None:
                            browser.terminate()
                            try:
                                browser.wait(timeout=20)
                            except subprocess.TimeoutExpired:
                                browser.kill()
            after_counts = trial.record("counts-after", owner_counts(stack))
            require(counts == after_counts, "Rollback changed per-owner task or usage counts")
    finally:
        guest("cleanup")
    after = trial.record("bindings-after", trial.manager("list")["workspaces"])
    require(binding_pins(after) == pinned and all(row["state"] == "recycled" for row in after)
            and original == retained_state(args.original_registry), "Original ownership or retained deployment changed")
    browser_result = read(args.work / "browser.json")
    require(browser_result["status"] == "passed", "Browser did not finish rollback validation")
    trial.record("summary", {"status": "passed", "workspace_image": args.expected_image, "candidate_image": candidate,
                              "retained_private_volumes_verified": True, "binding_pins": pinned, "fixture_cleanup": True,
                              "normal_combined_stack": True, "per_owner_counts_before_and_after": counts,
                              "browser": browser_result})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("vm", "registry-root", "state", "work"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--candidate-reports", type=Path, help="Directory with this candidate's passed m3.json and m4.json")
    parser.add_argument("--expected-image", required=True, help="Pinned original workspace digest after the switch back")
    parser.add_argument("--gateway-image", required=True, help="Pinned dev gateway matching the candidate report")
    parser.add_argument("--original-registry", type=Path, default=Path.home() / ".local/state/nexpoly-openscience-m1-dev")
    parser.add_argument("--cleanup", action="store_true", help="Remove only this previously interrupted run's fixtures")
    args = parser.parse_args()
    if not all(re.fullmatch(r"sha256:[a-f0-9]{64}", value) for value in (args.expected_image, args.gateway_image)):
        parser.error("Supply two immutable image digests")
    for name in ("vm", "registry_root", "state", "original_registry"):
        setattr(args, name, getattr(args, name).resolve(strict=True))
    args.work = args.work.absolute()
    if (args.state / "running.json").exists():
        parser.error("Stop the independent development web stack before running the rollback drill")
    if not args.cleanup:
        if not args.candidate_reports or os.path.lexists(args.work):
            parser.error("Select the M3/M4 candidate reports and a new private work directory")
        args.candidate_reports = args.candidate_reports.resolve(strict=True)
    os.umask(0o077)
    rollback(args)


if __name__ == "__main__":
    main()

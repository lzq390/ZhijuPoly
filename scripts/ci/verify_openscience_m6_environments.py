"""Verify AC-22 on the two dedicated loopback sites, using their own authenticated PostgreSQL control planes."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

from verify_openscience_m5_workspace_web import ROOT, json_write
from verify_openscience_per_user_dev import inspect_runtime
from openscience_m2_dev import DEFAULT, PROFILES, SECOND_ROOT
from openscience_m3_ledger import OWNERS, pg_target
from openscience_m5_quota_inputs import retained_state
from openscience_per_user_dev import local_engine

SITES = {
    "dev-m1": (DEFAULT / "nexpoly-openscience-m2-dev", DEFAULT / "nexpoly-m5-native-01/src"),
    "dev-m6": (SECOND_ROOT / "site", SECOND_ROOT / "ws"),
}
ROSTER = {owner: "test-user-" + role for role, owner in OWNERS.items()}
BASELINE = ROOT / "docs/verification/openscience-m6-operator-20261124.json"


def require(value, detail):
    if not value:
        raise ValueError(detail)


def manifest(env):
    state, registry = SITES[env]
    stack = json.loads((state / "running.json").read_bytes())
    site = json.loads((state / "candidate.json").read_bytes())
    require(
        stack["environment"] == site["environment"] == env
        and stack["state_dir"] == str(state)
        and stack["registry"] == str(registry)
        and (state / "deployment").read_text().strip() == stack["deployment"]
        and all(stack[k] == PROFILES[env][k] for k in ("parent", "agent"))
        and not stack.get("m6_operator_id")
        and bool(stack.get("quota_files")) == (env == "dev-m1"),
        "Unexpected candidate authority, workspace registry or browser profile",
    )
    return stack


def gate(env, expected_deployment, action):
    stack = manifest(env)
    require(stack["deployment"] == expected_deployment, "Site changed since the browser was opened")
    pg = pg_target(stack)
    updates = {
        "remove-a": "UPDATE openscience_m2.workspace_bindings SET enabled=false WHERE environment=:'env' AND user_id=:'owner'::uuid;",
        "restore-a": "UPDATE openscience_m2.workspace_bindings SET enabled=true WHERE environment=:'env' AND user_id=:'owner'::uuid;",
        "close": "UPDATE openscience_m2.environments SET enabled=false WHERE environment=:'env';",
        "open": "UPDATE openscience_m2.environments SET enabled=true WHERE environment=:'env';",
        "status": "",
    }
    # A failed guard exits psql and rolls back. Mutations hold the guard locks until commit.
    begin = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;" if action == "status" else "BEGIN;"
    lock = (
        ""
        if action == "status"
        else "LOCK TABLE openscience_m2.environments, openscience_m2.workspace_bindings IN SHARE ROW EXCLUSIVE MODE;"
    )
    sql = (
        begin
        + "\nSET LOCAL lock_timeout='5s';\nSET LOCAL statement_timeout='10s';\n"
        + lock
        + r"""
SELECT (count(*)=1 AND min(environment)=:'env' AND
        (SELECT jsonb_object_agg(user_id::text,workspace_user_id) FROM openscience_m2.workspace_bindings
          WHERE environment=:'env')=:'roster'::jsonb) AS fixture_ok FROM openscience_m2.environments
\gset
\if :fixture_ok
"""
        + updates[action]
        + r"""
\else
\quit 3
\endif
SELECT jsonb_build_object('enabled',e.enabled,'members',
    (SELECT jsonb_object_agg(workspace_user_id,enabled) FROM openscience_m2.workspace_bindings
     WHERE environment=e.environment))
FROM openscience_m2.environments e WHERE e.environment=:'env';
COMMIT;
"""
    )
    cmd = (
        'export PGHOST=/m2pg PGUSER=m2_admin PGPASSWORD="$(cat /admin-pw)"; '
        'exec psql -XqAt -v ON_ERROR_STOP=1 -d nexpoly_m2_dev -v "env=$1" -v "owner=$2" -v "roster=$3"'
    )
    done = subprocess.run(
        ["docker", "exec", "-i", pg, "sh", "-ec", cmd, "ac22", env, OWNERS["a"], json.dumps(ROSTER)],
        input=sql,
        capture_output=True,
        text=True,
        check=True,
        timeout=25,
    )
    return json.loads(done.stdout)


def verify(work, fixture):
    work.mkdir(mode=0o700)
    prior = json.loads(BASELINE.read_bytes())
    original_root = DEFAULT / "nexpoly-openscience-m1-dev"
    original = retained_state(original_root)
    require(
        original["registry_sha256"] == prior["final"]["original_private_registry_sha256"],
        "Original M1 registry changed",
    )
    stacks = {env: manifest(env) for env in SITES}
    require(
        len({s["deployment"] for s in stacks.values()}) == len({pg_target(s) for s in stacks.values()}) == 2,
        "Candidates must use separate deployments and databases",
    )
    primary, secondary = (SITES[env][0] / "credentials" for env in ("dev-m1", "dev-m6"))
    require(
        all(
            not secrets.compare_digest((primary / name).read_bytes(), (secondary / name).read_bytes())
            for name in ("router", "a", "b")
        ),
        "Expected separately provisioned candidate credentials",
    )
    source = json.loads((SITES["dev-m1"][1] / "registry.json").read_bytes())
    for user, baseline in prior["final"]["workspace_states"].items():
        pin = source["quota_storage"]["dev-m1/" + user]
        require(
            (pin["uid"], pin["filesystem_uuid"]) == (baseline["uid"], baseline["filesystem_uuid"]),
            "Retained M5 source workspace differs from the reviewed UID or filesystem",
        )
    db_volumes = {}
    for env, s in stacks.items():
        mounts = json.loads(
            subprocess.check_output(["docker", "inspect", "--format", "{{json .Mounts}}", s["pg"]])
        )
        (db_volumes[env],) = [
            m["Name"]
            for m in mounts
            if m["Type"] == "volume" and m["Destination"] == "/var/lib/postgresql/data" and m["RW"]
        ]
        require(db_volumes[env] == "nexpoly-m2-auth-" + s["deployment"], "Unrecognized authority volume")
    original_volumes = {r["volume"] for r in original["workspaces"].values()}
    volumes = [v for s in stacks.values() for v in s["user_volumes"].values()]
    require(
        len(set(volumes)) == 4 and original_volumes.isdisjoint(volumes),
        "Expected four separate development volumes",
    )
    open_policy = {"enabled": True, "members": {name: True for name in ROSTER.values()}}
    for env, s in stacks.items():
        require(
            gate(env, s["deployment"], "status") == open_policy,
            "Both members must start enabled in each candidate",
        )
    try:
        subprocess.run(
            [
                "node",
                ROOT / "scripts/ci/verify_openscience_m6_environments.mjs",
                "--work",
                work,
                *(["--fixture", fixture] if fixture else []),
            ],
            check=True,
            timeout=850,
        )
        browser = json.loads((work / "browser.json").read_bytes())
        require(browser["status"] == "passed", "Browser verification did not finish")
        bindings, ws_deployments = {}, {}
        for env, (_, registry) in SITES.items():
            reg = json.loads((registry / "registry.json").read_bytes())
            ws_deployments[env] = reg["deployment"]
            require(reg["image"] == prior["images"]["rollback"], "Candidate workspace image differs")
            rows = json.loads(
                subprocess.check_output(
                    [sys.executable, ROOT / "scripts/openscience_per_user_dev.py", "--state-dir", registry, "list"]
                )
            )["workspaces"]
            require(
                len(rows) == 2
                and all(
                    r["environment"] == env and r["volume"] == stacks[env]["user_volumes"][r["user_id"]]
                    for r in rows
                ),
                "Workspace binding differs from the routed volume",
            )
            bindings[env] = {}
            for row in rows:
                result = inspect_runtime(row, reg["deployment"], reg["image"])
                result["workspace"] = row["workspace"]
                bindings[env][row["user_id"]] = result
        four = [r for env in bindings.values() for r in env.values()]
        require(
            all(len({r[k] for r in four}) == 4 for k in ("workspace", "uid", "volume", "container_id"))
            and len(set(ws_deployments.values())) == 2,
            "Runtime boundaries overlap",
        )
        require(retained_state(original_root) == original, "Original M1 metadata changed during exercise")
        for env, s in stacks.items():
            require(
                gate(env, s["deployment"], "status") == open_policy,
                "The browser did not restore the original pilot policy",
            )
        json_write(
            work / "summary.json",
            {
                "status": "passed",
                "browser": browser,
                "running_workspaces": bindings,
                "workspace_deployments": ws_deployments,
                "database_volumes": db_volumes,
                "restored_policies": open_policy,
                "original_m1_registry_sha256": original["registry_sha256"],
            },
        )
    finally:
        # Restore only switches changed by this drill, under the original deployment identity.
        failures = []
        for env, s in stacks.items():
            try:
                now = gate(env, s["deployment"], "status")
                if not now["members"]["test-user-a"]:
                    gate(env, s["deployment"], "restore-a")
                if not now["enabled"]:
                    gate(env, s["deployment"], "open")
            except Exception as error:
                failures.append((env, error))
        if failures:
            names = ", ".join(env for env, _ in failures)
            raise RuntimeError(f"Could not restore the original candidate policy for {names}") from failures[0][1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("verify")
    run.add_argument("--work", type=Path, required=True)
    run.add_argument(
        "--fixture", type=Path, help="Reopen the four previously verified AC-22 project sessions and files"
    )
    control = sub.add_parser("gate")
    control.add_argument("environment", choices=SITES)
    control.add_argument("--deployment", required=True)
    control.add_argument("action", choices=("status", "remove-a", "restore-a", "close", "open"))
    args = parser.parse_args()
    os.umask(0o077)
    local_engine()
    if args.command == "gate":
        print(json.dumps(gate(args.environment, args.deployment, args.action)))
    else:
        with (SECOND_ROOT / "ac22.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            verify(args.work.absolute(), args.fixture.resolve(strict=True) if args.fixture else None)


if __name__ == "__main__":
    main()

"""Reconcile one M3 browser run against its private PostgreSQL cluster (JSON stdin/stdout)."""

import base64
import hashlib
import io
import json
import re
import subprocess
import sys
from uuid import UUID
from zipfile import ZipFile

OWNERS = {letter: f"10000000-0000-4000-8000-{index:012d}" for index, letter in enumerate("ab", 1)}
PG_IMAGE = "sha256:eb4759788a2182f08257135e61a34f2cfc3c2914079f3465d64ee62350f4d081"
CHARGED = {
    "predict_polymer_properties": "predict",
    "polymerize_monomers": "polymerization",
    "dft_job_submit": "monomer_dft",
}
READS = {"dft_job_status", "dft_job_list", "dft_job_result"}
OTHERS = {
    "logins": ("auth.sessions", "user_id"),
    "tool_operations": ("openscience_m3.operations", "owner_id"),
    "dft_jobs": ("monomer_dft.jobs", "owner_user_id"),
    "quota_operations": ("usage_entitlements.operations", "owner_user_id"),
    "quota_events": ("usage_entitlements.operation_events", "owner_user_id"),
    "request_observations": ("usage_entitlements.request_observations", "owner_user_id"),
    "usage_events": ("usage.events", "owner_user_id"),
    "usage_attempts": ("usage.attempts", "owner_user_id"),
}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def pg_target(spec):
    pg = spec["pg"]
    deployment = spec["deployment"]
    require(
        re.fullmatch("[a-f0-9]{64}", pg) and UUID(deployment).hex == deployment,
        "Invalid M2 container identity",
    )
    fmt = '{{index .Config.Labels "org.nexpoly.m2.deployment"}} {{.Image}} {{.HostConfig.NetworkMode}} {{.State.Status}}'
    actual = subprocess.check_output(
        ["docker", "inspect", "--type", "container", "--format", fmt, pg], text=True
    ).split()
    require(
        actual == [deployment, PG_IMAGE, "none", "running"],
        "Expected the dedicated running M2/M3 database",
    )
    return pg


def query(pg, script, values, database="nexpoly_m2_dev"):
    # psql quotes the JSON parameter as an SQL literal. The only password stays inside the database container.
    require(database in {"nexpoly_m2_dev", "nexpoly_deploy_m6"}, "Unknown candidate database")
    shell = 'export PGPASSWORD="$(cat /admin-pw)" PGHOST=/m2pg PGUSER=m2_admin; exec psql -XqAt -v ON_ERROR_STOP=1 -d "$2" -v "spec=$1"'
    sql = (
        "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\nSET LOCAL statement_timeout='12s';\n"
        + script
        + "\nCOMMIT;\n"
    )
    result = subprocess.run(
        ["docker", "exec", "-i", pg, "sh", "-ec", shell, "ledger", json.dumps(values), database],
        input=sql,
        text=True,
        check=True,
        capture_output=True,
        timeout=30,
    )
    return [json.loads(line) for line in result.stdout.splitlines() if line]


def other_counts_sql(owners=OWNERS):
    # These counts cover every account outside the two seeded M1 identities, including system/service accounts.
    exclude = ",".join("'" + str(UUID(value)) + "'::uuid" for value in owners.values())
    pairs = [
        f"'{key}',(SELECT count(*) FROM {table} WHERE {field} NOT IN ({exclude}))"
        for key, (table, field) in OTHERS.items()
    ]
    return (
        "SELECT jsonb_build_object('at',transaction_timestamp(),'other_owners',jsonb_build_object("
        + ",".join(pairs)
        + "));"
    )


def reference(value):
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def check_downloaded_bundle(downloads):
    expected = {item["name"]: item for item in downloads["files"]}
    require(len(expected) == len(downloads["files"]) > 0, "Duplicate or absent artifact names")
    raw = base64.b64decode(downloads["archive"], validate=True)
    with ZipFile(io.BytesIO(raw)) as archive:
        require(sorted(archive.namelist()) == sorted(expected), "Bundle contains unexpected files")
        for member in archive.infolist():
            file = expected[member.filename]
            require(member.file_size == file["bytes"], "Bundle member size differs from the download")
            with archive.open(member) as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            require(digest == file["sha256"], "Bundle member differs from the owner's artifact")
    return len(expected)


def main():
    spec = json.load(sys.stdin)
    pg = pg_target(spec)
    environment = spec.get("environment", "dev-m1")
    require(environment in {"dev-m1", "deploy-m6"}, "Unknown M3 candidate")
    database = "nexpoly_deploy_m6" if environment == "deploy-m6" else "nexpoly_m2_dev"
    owners = OWNERS
    workspaces = {k: f"test-user-{k}" for k in OWNERS}
    if environment == "deploy-m6":
        members = spec["members"]
        require(len(members) == 2, "Expected two registered owners")
        owners = {k: str(UUID(m["user_id"])) for k, m in zip("ab", members, strict=True)}
        workspaces = {k: m["workspace_user_id"] for k, m in zip("ab", members, strict=True)}
        (registered,) = query(
            pg,
            """
            SELECT jsonb_object_agg(user_id,workspace_user_id) FROM openscience_m2.workspace_bindings
                WHERE environment=:'spec'::jsonb->>'environment' AND enabled;
        """,
            {"environment": environment},
            database,
        )
        require(
            registered == dict(zip(owners.values(), workspaces.values(), strict=True))
            and len(set(owners.values())) == len(set(workspaces.values())) == 2,
            "Candidate database bindings differ from the enrolled owners",
        )
    if "capacity_calls" in spec:
        require(environment == "deploy-m6", "Expected the capacity candidate")
        before, calls = spec["baseline"], spec["capacity_calls"]
        require(before["deployment"] == spec["deployment"] and 1 <= len(calls) <= 2, "Invalid capacity round")
        require(all(c["user_id"] in owners.values() for c in calls), "Unregistered capacity account")
        counts, records = query(pg, other_counts_sql(owners) + CALLS_SQL, {
            "since": before["at"], "sessions": [c["workspace_session"] for c in calls],
            "observed": {c["user_id"]: {"tg_c": c["predicted_tg_c"]} for c in calls},
        }, database)
        require(counts["other_owners"] == before["other_owners"], "Unrelated account activity during the round")
        print(json.dumps(records))
        return
    if "users" not in spec:
        (snapshot,) = query(pg, other_counts_sql(owners), {}, database)
        print(json.dumps({"deployment": spec["deployment"], **snapshot}))
        return
    before = spec["baseline"]
    require(before["deployment"] == spec["deployment"], "Baseline belongs to another deployment")
    users = spec["users"]
    require(set(users) == set(owners), "Expected both enrolled test owners")
    for label, user in users.items():
        require(str(UUID(user["id"])) == owners[label], "Account differs from the registered binding")
        require(
            re.fullmatch(r"ses_[A-Za-z0-9_-]{1,150}", user["workspace_session"]),
            "Invalid workspace session",
        )
        require(
            user["session_ids"] and all(str(UUID(v)) == v for v in user["session_ids"]), "Missing logins"
        )
        require(str(UUID(user["job_id"])) == user["job_id"], "Invalid native DFT job")
    require(
        users["a"]["workspace_session"] != users["b"]["workspace_session"]
        and users["a"]["job_id"] != users["b"]["job_id"],
        "Expected separate test sessions and jobs",
    )
    counts, records, jobs = query(
        pg,
        other_counts_sql(owners) + CALLS_SQL + JOBS_SQL,
        {
            "since": before["at"],
            "sessions": [v["workspace_session"] for v in users.values()],
            "jobs": [v["job_id"] for v in users.values()],
            "observed": {v["id"]: v["observed"] for v in users.values()},
        },
        database,
    )
    require(
        counts["other_owners"] == before["other_owners"],
        "Non-test owners have new native or authorization records",
    )
    operation_count = 0
    output = {}
    for label, user in users.items():
        selected = [r for r in records if r["workspace_session"] == user["workspace_session"]]
        operation_count += len(selected)
        output[label] = reconcile(
            label, user, users["b" if label == "a" else "a"]["job_id"], selected, jobs, workspaces[label]
        )
        output[label]["verified_bundle_members"] = check_downloaded_bundle(user["downloads"])
    require(operation_count == len(records), "Unaccounted workspace records")
    print(json.dumps({"users": output, "other_owners_delta": {k: 0 for k in OTHERS}}, indent=2))


# Compare browser observations inside PostgreSQL; return only matches, counts and correlation fields.
CALLS_SQL = """
WITH input AS (SELECT :'spec'::jsonb s),
ops AS (SELECT o.*,s->'observed'->(o.owner_id::text) AS browser FROM openscience_m3.operations o,input
        WHERE o.created_at >= (s->>'since')::timestamptz
          AND o.workspace_session IN (SELECT jsonb_array_elements_text(s->'sessions'))),
receipts AS (
  SELECT d.operation_id,d.response_status AS status,d.usage_request_id,d.usage_operation_id,q.key_hash,q.module,
         q.state,q.qualified,q.native_task_id,q.result_json IS NOT NULL AS has_result,
         CASE q.module
           WHEN 'predict' THEN q.result_json #> '{payload,predictions,Glass transition temperature}' = ops.browser->'tg_c'
           WHEN 'polymerization' THEN q.result_json #> '{payload,total}' = ops.browser->'polymer_total'
             AND (SELECT jsonb_agg(candidate->'polymer_smiles' ORDER BY ordinal)
                    FROM jsonb_array_elements(q.result_json #> '{payload,results}')
                         WITH ORDINALITY AS candidates(candidate,ordinal)) = ops.browser->'polymer_smiles'
         END AS browser_result_matches,
         arrived.request_id IS NOT NULL AS native_received, finished.http_status AS native_status,
         finished.operation_id AS native_operation, finished.outcome AS native_outcome,
         (SELECT count(*) FROM usage_entitlements.operation_events e
           WHERE e.owner_user_id=q.owner_user_id AND e.operation_id=q.operation_id AND e.action='settle') AS settlements,
         (SELECT count(*) FROM usage.attempts a
           WHERE a.owner_user_id=ops.owner_id AND a.module=q.module AND a.kind='task' AND a.status='completed'
             AND a.first_occurred_at BETWEEN arrived.occurred_at AND finished.occurred_at) AS completed_sync_spans
    FROM ops JOIN openscience_m3.delegations d USING (operation_id)
    LEFT JOIN usage_entitlements.operations q ON q.owner_user_id=ops.owner_id AND q.operation_id=d.usage_operation_id
    LEFT JOIN usage_entitlements.request_observations arrived ON arrived.owner_user_id=ops.owner_id
         AND arrived.request_id=d.usage_request_id AND arrived.phase='received'
    LEFT JOIN usage_entitlements.request_observations finished ON finished.owner_user_id=ops.owner_id
         AND finished.request_id=d.usage_request_id AND finished.phase='finished'
   WHERE d.audience='nexpoly-native' AND d.used_at IS NOT NULL
), evidence AS (
  SELECT o.operation_id,o.owner_id,o.login_id,o.workspace_user_id,o.workspace_session,o.turn_id,o.call_id,o.tool,o.job_id,o.confirmed,
         (SELECT count(*) FROM usage_entitlements.operations q
           WHERE q.key_hash=encode(sha256(convert_to('openscience-'||o.operation_id::text,'UTF8')),'hex')) AS linked_quota_rows,
         (SELECT count(*) FROM monomer_dft.jobs j WHERE j.idempotency_key='openscience-'||o.operation_id::text) AS linked_jobs,
         EXISTS(SELECT FROM auth.sessions s WHERE s.session_id=o.login_id AND s.user_id=o.owner_id) AS login_owned,
         EXISTS(SELECT FROM openscience_m2.workspace_bindings b WHERE b.environment=o.environment AND b.user_id=o.owner_id
                  AND b.workspace_user_id=o.workspace_user_id) AS workspace_bound,
         COALESCE((SELECT jsonb_agg(to_jsonb(r)-'operation_id') FROM receipts r WHERE r.operation_id=o.operation_id),'[]') AS receipts
    FROM ops o
)
SELECT COALESCE(jsonb_agg(to_jsonb(evidence)),'[]') FROM evidence;
"""
JOBS_SQL = """
WITH input AS (SELECT :'spec'::jsonb s), evidence AS (
 SELECT j.job_id,j.owner_user_id AS owner,j.status,j.idempotency_key,j.result_json IS NOT NULL AS has_result,
        j.result_json #> '{properties,energy,value_eV}' = b.observed->'fixture_energy' AS fixture_energy_matches,
        page.ids = b.observed->'dft_page' AS owner_page_matches,
        jsonb_array_length(page.ids) AS owner_page_size,
        (SELECT count(*) FROM monomer_dft.jobs own WHERE own.owner_user_id=j.owner_user_id) AS owner_total,
        (b.observed->>'dft_total')::bigint AS browser_total,
        (SELECT count(*) FROM monomer_dft.job_attempts a WHERE a.job_id=j.job_id AND a.start_authorized_at IS NOT NULL) AS authorized_attempts,
        (SELECT count(*) FROM monomer_dft.artifacts a WHERE a.job_id=j.job_id AND a.available AND a.size_bytes>0) AS nonempty_artifacts,
        (SELECT count(*) FROM usage.attempts a WHERE a.owner_user_id=j.owner_user_id AND a.module='monomer_dft' AND a.kind='task'
          AND a.task_id='monomer_dft:'||j.job_id::text AND a.status='completed') AS completed_usage_attempts
   FROM monomer_dft.jobs j CROSS JOIN input
   CROSS JOIN LATERAL (SELECT s->'observed'->(j.owner_user_id::text) AS observed) b
   CROSS JOIN LATERAL (
       SELECT COALESCE(jsonb_agg(p.job_id ORDER BY p.created_at DESC,p.job_id DESC),'[]') AS ids
         FROM (SELECT own.job_id,own.created_at FROM monomer_dft.jobs own
                WHERE own.owner_user_id=j.owner_user_id ORDER BY own.created_at DESC,own.job_id DESC LIMIT 50) p
   ) page
   WHERE j.job_id::text IN (SELECT jsonb_array_elements_text(s->'jobs'))
)
SELECT COALESCE(jsonb_agg(to_jsonb(evidence)),'[]') FROM evidence;
"""


def reconcile(label, user, other_job, operations, all_jobs, workspace):
    matches = [j for j in all_jobs if j["job_id"] == user["job_id"]]
    require(len(matches) == 1, f"{label}: native DFT task missing")
    job = matches[0]
    require(
        job["fixture_energy_matches"] is True,
        f"{label}: browser DFT energy differs from this owner's native result",
    )
    require(
        job["owner_page_matches"] is True and job["owner_total"] == job["browser_total"],
        f"{label}: DFT list IDs, ordering or total differ from the owner's native first page",
    )
    require(
        job["owner"] == user["id"]
        and job["status"] == "completed"
        and job["has_result"]
        and job["authorized_attempts"] == job["completed_usage_attempts"] == 1
        and job["nonempty_artifacts"] > 0,
        f"{label}: DFT job, confirmed execution, result, artifacts or native usage differs",
    )
    successful = set()
    cross_denied = set()
    operation_receipts = {}
    raw_qualifications = {module: set() for module in CHARGED.values()}
    for op in operations:
        tool = op["tool"]
        require(
            op["workspace_user_id"] == workspace
            and op["owner_id"] == user["id"]
            and op["login_owned"]
            and op["workspace_bound"]
            and op["login_id"] in user["session_ids"]
            and op["turn_id"]
            and op["call_id"],
            f"{label}/{tool}: call owner, workspace, turn or original login differs",
        )
        require(tool in READS or tool in CHARGED, f"{label}: unexpected tool")
        foreign = tool in {"dft_job_status", "dft_job_result"} and op["job_id"] == other_job
        if tool in {"dft_job_status", "dft_job_result"} and not foreign:
            require(op["job_id"] == job["job_id"], f"{label}/{tool}: unexpected job scope")
        require(
            op["linked_quota_rows"] == int(tool in CHARGED)
            and op["linked_jobs"] == int(tool == "dft_job_submit"),
            f"{label}/{tool}: extra or missing task or charge across owners",
        )
        receipt = {
            "tool": tool,
            "operation_ref": reference(op["operation_id"]),
            "foreign_job": foreign,
            "native_statuses": [],
            "new_quota_charges": 0,
        }
        for r in op["receipts"]:
            receipt["native_statuses"].append(r["status"])
            if foreign:
                require(
                    r["status"] == 404
                    and r["usage_operation_id"] is None
                    and r["usage_request_id"] is None,
                    f"{label}/{tool}: foreign job exposed or billed",
                )
                cross_denied.add(tool)
                continue
            require(
                r["status"] is not None and 200 <= r["status"] < 300,
                f"{label}/{tool}: native operation failed",
            )
            successful.add(tool)
            if tool in READS:
                require(
                    r["usage_operation_id"] is None and r["usage_request_id"] is None,
                    f"{label}/{tool}: read was billed",
                )
                continue
            module = CHARGED[tool]
            require(
                r["usage_operation_id"]
                and r["module"] == module
                and r["native_received"]
                and r["native_operation"] == r["usage_operation_id"]
                and r["native_status"] == r["status"]
                and r["native_outcome"] == "response_finished",
                f"{label}/{tool}: missing native request/operation linkage",
            )
            key = "openscience-" + op["operation_id"]
            require(
                r["key_hash"] == hashlib.sha256(key.encode()).hexdigest()
                and r["state"] == "succeeded"
                and r["qualified"] is True
                and r["settlements"] == 1,
                f"{label}/{tool}: missing idempotent successful settlement",
            )
            raw_qualifications[module].add(r["usage_operation_id"])
            if tool == "dft_job_submit":
                require(
                    op["confirmed"]
                    and job["idempotency_key"] == key
                    and r["native_task_id"] == job["job_id"],
                    f"{label}: DFT confirmation and native job/charge do not match",
                )
            else:
                require(
                    r["browser_result_matches"] is True,
                    f"{label}/{tool}: browser result differs from the owner's retained native payload",
                )
                require(
                    r["completed_sync_spans"] == 1 and r["has_result"],
                    f"{label}/{tool}: missing synchronous usage span or retained result",
                )
            receipt["quota_ref"] = reference(r["usage_operation_id"])
            receipt["native_request_ref"] = reference(r["usage_request_id"])
            receipt["new_quota_charges"] = 1
        require(receipt["native_statuses"], f"{label}/{tool}: no consumed native authorization")
        require(
            op["call_id"] not in operation_receipts,
            f"{label}: duplicated tool call reference in the run",
        )
        operation_receipts[op["call_id"]] = receipt
    require(
        successful == READS | CHARGED.keys(),
        f"{label}: not all six tools have successful native receipts",
    )
    require(
        cross_denied == {"dft_job_status", "dft_job_result"},
        f"{label}: cross-user status/result refusals absent",
    )
    require(
        all(len(charges) == 1 for charges in raw_qualifications.values()),
        f"{label}: expected one native success charge for each of three computations",
    )
    for call in user.get("calls", []):
        matched = [
            r
            for cid, r in operation_receipts.items()
            if (not call.get("call_id") or call["call_id"] == cid)
            and r["tool"] == call["tool"]
            and r["foreign_job"] is (not call["allowed"])
        ]
        require(matched, f"{label}/{call['tool']}: requested call was not accounted for")
    return {
        "workspace": workspace,
        "job_ref": reference(job["job_id"]),
        "dft_authorized_attempts": 1,
        "dft_completed_usage_attempts": 1,
        "available_artifacts": job["nonempty_artifacts"],
        "native_results_match_browser": {
            "tg_c": True,
            "ordered_polymer_smiles": True,
            "dft_fixture_energy": True,
        },
        "native_dft_list": {
            "ordered_ids_match": True,
            "page_size": job["owner_page_size"],
            "total": job["owner_total"],
        },
        "qualified_native_operations": {k: len(v) for k, v in raw_qualifications.items()},
        "tool_calls": sorted(operation_receipts.values(), key=lambda r: (r["tool"], r["operation_ref"])),
    }


if __name__ == "__main__":
    main()

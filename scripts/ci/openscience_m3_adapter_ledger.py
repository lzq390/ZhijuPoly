"""Reconcile the dev-m1 adapter journals, native tasks and accounting for one browser run."""

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from uuid import UUID

from openscience_m3_ledger import OWNERS, other_counts_sql, pg_target, query, require

MODULES = {"polytao": "generation_drafts", "retrosynthesis": "retrosynthesis_drafts"}
SUBMIT = {"polytao": "confirm_polytao_generation", "retrosynthesis": "confirm_monomer_retrosynthesis"}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def native_key(owner, draft, revision):
    original = json.dumps([owner, owner, f"draft:{draft}:{revision}"], separators=(",", ":"))
    return digest(original)


def inspect_journal(root, module, owner, logins, sample, other_draft):
    file = (root / "m3-adapters" / module / owner / "journal.db").resolve(strict=True)
    table = MODULES[module]
    db = sqlite3.connect(file.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        leaks = db.execute(
            f"SELECT count(*) FROM {table} WHERE client_id<>? OR account_uuid<>? OR draft_id=?",
            (owner, owner, other_draft),
        ).fetchone()[0]
        row = db.execute(f"SELECT * FROM {table} WHERE draft_id=?", (sample["draft_id"],)).fetchone()
        require(row and not leaks, "Journal contains a foreign draft or is missing the expected draft")
        require(
            row["state"] == "submitted"
            and row["job_id"] == sample["job_id"]
            and json.loads(row["payload"])["_m3_initiating_login"] == logins[0]
            and row["conversation_id"] == sample["workspace_session"],
            "Journal draft, original login, revision, or native job differs from the browser",
        )
        draft_key = f"{row['draft_id']}:{row['revision']}"
        accepted = db.execute(
            "SELECT count(*) FROM submissions WHERE account_uuid=? AND client_id=? AND idempotency_key=? "
            "AND job_id=? AND state='accepted'",
            (owner, owner, "draft:" + draft_key, row["job_id"]),
        ).fetchone()[0]
        admitted = db.execute(
            "SELECT count(*) FROM m3_admissions WHERE draft_key=?", (draft_key,)
        ).fetchone()[0]
        require(accepted == admitted == 1, "Expected one admission and one accepted draft receipt")
        if limited_id := sample.get("limited_draft_id"):
            limited = db.execute(f"SELECT * FROM {table} WHERE draft_id=?", (limited_id,)).fetchone()
            require(
                limited
                and limited["job_id"] is None
                and limited["state"] == "editing"
                and len(logins) == 2
                and logins[1] != logins[0]
                and json.loads(limited["payload"])["_m3_initiating_login"] == logins[1],
                "A's rate-limited draft acquired a job or lost its owner",
            )
            limited_key = f"{limited_id}:{limited['revision']}"
            require(
                not db.execute("SELECT 1 FROM m3_admissions WHERE draft_key=?", (limited_key,)).fetchone()
                and not db.execute(
                    "SELECT 1 FROM submissions WHERE idempotency_key=?", ("draft:" + limited_key,)
                ).fetchone(),
                "A's rate-limited draft acquired an admission or submission",
            )
        return row["revision"]
    finally:
        db.close()


def browser_signature(module, result):
    require(result["status"] == "completed", "Expected a completed browser result")
    if module == "polytao":
        require(result["returned_count"] == len(result["results"]) > 0, "Missing generation candidates")
        return {
            key: result[key] for key in ("requested_count", "returned_count", "filter_counter", "results")
        }
    raw = result["result"]
    require(raw and raw["total"] == len(raw["candidates"]) > 0, "Missing retrosynthesis candidates")
    return {key: value for key, value in raw.items() if key != "query_time_ms"}


# Qualifying operations across both modules and all owners plus the durable native retrosynthesis jobs.
# Scope by the database clock so any extra, wrongly owned, incomplete or abandoned operation fails the run.
WINDOW_SQL = """
WITH i AS (SELECT (:'spec'::jsonb->>'since')::timestamptz at)
SELECT jsonb_build_object(
  'ops',(SELECT count(*) FROM usage_entitlements.operations,i
         WHERE created_at>=at AND module IN ('polytao','retrosynthesis') AND pool_id IS NOT NULL),
  'jobs',(SELECT count(*) FROM retrosynthesis.jobs,i WHERE created_at>=at));
"""

PROOF_SQL = """
WITH i AS (SELECT :'spec'::jsonb s),
q AS (SELECT q.* FROM usage_entitlements.operations q,i WHERE q.module=s->>'module'
      AND q.native_task_id=s->>'job' AND q.created_at>=(s->>'since')::timestamptz),
e AS (SELECT e.* FROM usage_entitlements.operation_events e JOIN q USING(owner_user_id,operation_id)),
native AS (SELECT j.* FROM retrosynthesis.jobs j,i WHERE j.job_id::text=s->>'job'),
proof AS (
  SELECT q.owner_user_id AS owner,q.operation_id,q.state,q.qualified,q.pool_id,q.key_hash,
    (SELECT count(*) FROM e WHERE action='reserve') AS reserved,
    (SELECT count(*) FROM e WHERE action='claim_running') AS executions,
    (SELECT count(*) FROM e WHERE action='settle') AS settlements,
    (SELECT count(*) FROM usage.attempts a WHERE a.module=q.module AND a.owner_user_id=q.owner_user_id
         AND a.task_id=q.module||':'||q.native_task_id AND a.kind='task' AND a.status='completed') AS completed,
    (SELECT count(*) FROM openscience_m3.operations o JOIN openscience_m3.delegations d USING(operation_id)
       JOIN usage_entitlements.request_observations r ON r.owner_user_id=q.owner_user_id AND
        r.request_id=d.usage_request_id AND r.operation_id=q.operation_id AND r.phase='finished' AND r.http_status=202
       WHERE o.environment='dev-m1' AND o.owner_id=q.owner_user_id AND o.login_id::text=s->>'login'
         AND o.workspace_session=s->>'workspace' AND o.channel='ui' AND o.confirmed AND o.tool=s->>'submit'
         AND o.draft_id=s->>'draft' AND o.revision=(s->>'revision')::integer
         AND d.audience='nexpoly-native' AND d.method='POST' AND d.path LIKE '%/jobs'
         AND d.used_at IS NOT NULL AND d.response_status=202 AND d.native_key=s->>'key'
         AND d.usage_operation_id=q.operation_id) AS original_submissions,
    (SELECT count(*) FROM openscience_m3.operations o JOIN openscience_m3.delegations d USING(operation_id)
       WHERE o.owner_id=q.owner_user_id AND o.login_id::text=s->>'login' AND o.workspace_session=s->>'workspace'
         AND d.audience='nexpoly-native' AND d.method='GET' AND d.path LIKE '%/jobs/lookup'
         AND d.used_at IS NOT NULL AND d.response_status=200 AND d.query='idempotency_key='||(s->>'key')) AS recovered,
    CASE WHEN q.module='polytao' THEN q.result_json->>'job_id'=q.native_task_id
      AND q.result_json->>'status'='completed' ELSE
      (SELECT owner_user_id=q.owner_user_id AND status='completed' AND start_authorized_at IS NOT NULL
             AND idempotency_key=s->>'key' FROM native) END AS native_completed,
    jsonb_strip_nulls(CASE WHEN q.module='polytao' THEN
      jsonb_build_object('requested_count',q.result_json->'requested_count','returned_count',q.result_json->'returned_count',
        'filter_counter',q.result_json #> '{result,filter_counter}','results',
        (SELECT jsonb_agg(v-'structure_svg'-'raw_smiles' ORDER BY pos)
         FROM jsonb_array_elements(q.result_json #> '{result,results}') WITH ORDINALITY AS c(v,pos)))
    ELSE (SELECT n.result_json-'device'-'detected_target_role'-'query_time_ms' FROM native n) END)
      = jsonb_strip_nulls(s->'signature') AS matches
  FROM q,i
)
SELECT coalesce(jsonb_agg(to_jsonb(proof)),'[]') FROM proof;
"""


def main():
    spec = json.load(sys.stdin)
    pg = pg_target(spec)
    if "users" not in spec:
        (before,) = query(pg, other_counts_sql(), {})
        print(json.dumps({"deployment": spec["deployment"], **before}))
        return
    users, before = spec["users"], spec["baseline"]
    require(
        set(users) == set(OWNERS) and before["deployment"] == spec["deployment"], "Development run mismatch"
    )
    after, window = query(pg, other_counts_sql() + WINDOW_SQL, {"since": before["at"]})
    require(after["other_owners"] == before["other_owners"], "Unrelated owner received new records")
    require(window == {"ops": 4, "jobs": 2}, f"Unexpected native operations in test window: {window}")
    output = {}
    for label, user in users.items():
        owner = OWNERS[label]
        require(
            user["id"] == owner and str(UUID(user["session_ids"][0])) == user["session_ids"][0],
            "Invalid test identity",
        )
        login = user["session_ids"][0]
        output[label] = {}
        for module in MODULES:
            sample = user["adapters"][module]
            other = users["b" if label == "a" else "a"]["adapters"][module]
            require(
                sample["job_id"] != other["job_id"] and sample["draft_id"] != other["draft_id"],
                "Draft or job identity reused between owners",
            )
            require(
                sample["result"]["job_id"] == sample["job_id"], "Browser result belongs to a different job"
            )
            if label == "a":
                require(sample.get("limited_draft_id"), "Missing A's rate-limited draft")
            revision = inspect_journal(
                Path(spec["state_dir"]), module, owner, user["session_ids"], sample, other["draft_id"]
            )
            key = native_key(owner, sample["draft_id"], revision)
            (rows,) = query(
                pg,
                PROOF_SQL,
                {
                    "module": module,
                    "submit": SUBMIT[module],
                    "owner": owner,
                    "login": login,
                    "workspace": sample["workspace_session"],
                    "draft": sample["draft_id"],
                    "revision": revision,
                    "job": sample["job_id"],
                    "key": key,
                    "since": before["at"],
                    "signature": browser_signature(module, sample["result"]),
                },
            )
            require(len(rows) == 1, f"{label}/{module}: expected one native quota row across all owners")
            r = rows[0]
            require(
                r["owner"] == owner
                and r["key_hash"] == digest(key)
                and r["state"] == "succeeded"
                and r["qualified"]
                and r["pool_id"] == {"polytao": "generation", "retrosynthesis": "chemistry_small"}[module]
                and r["native_completed"]
                and r["matches"],
                f"{label}/{module}: native owner, key, result or settlement mismatch",
            )
            required_ones = ("reserved", "executions", "settlements", "completed", "original_submissions")
            require(
                all(r[n] == 1 for n in required_ones) and r["recovered"] >= 1,
                f"{label}/{module}: submit/recovery/usage mismatch "
                + str({n: r[n] for n in (*required_ones, "recovered")}),
            )
            output[label][module] = {
                "workspace_owner": "test-user-" + label,
                "draft_revision": revision,
                "original_login_confirmed": True,
                "native_result_matches": True,
                "quota_pool": r["pool_id"],
                "operation_ref": digest(r["operation_id"])[:16],
                "native_submissions": r["original_submissions"],
                "successful_receipt_lookups": r["recovered"],
                "authorized_starts": r["executions"],
                "settled_charges": r["settlements"],
                "completed_usage_attempts": r["completed"],
                "accepted_journal_receipts": 1,
                **({"limited_draft_has_no_submission": True} if label == "a" else {}),
            }
    print(
        json.dumps(
            {
                "users": output,
                "new_native_operations": window["ops"],
                "other_owners_delta": {k: 0 for k in before["other_owners"]},
            }
        )
    )


if __name__ == "__main__":
    main()

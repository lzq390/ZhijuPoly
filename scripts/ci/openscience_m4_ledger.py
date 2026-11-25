"""Match the two browser uploads, citations and actual model calls to their private dev PostgreSQL rows."""

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys

from openscience_m3_ledger import OWNERS, pg_target, query, require

TABLES = {
    "collections": "openscience_m4_knowledge.collections",
    "documents": "openscience_m4_knowledge.documents",
    "chunks": "openscience_m4_knowledge.chunks",
    "model": "openscience_m4.model_requests",
}


def snapshot(pg):
    # Count the entire dedicated catalog, including service accounts and owners outside this test.
    counts = " UNION ALL ".join(
        f"SELECT '{name}' kind, owner_id::text owner, count(*) n FROM {table} GROUP BY owner_id"
        for name, table in TABLES.items()
    )
    statement = f"SELECT coalesce(jsonb_agg(to_jsonb(s)),'[]') FROM ({counts}) s;"
    (rows,) = query(pg, statement, {})
    return {name: {r["owner"]: r["n"] for r in rows if r["kind"] == name} for name in TABLES}


KNOWLEDGE = """
WITH s AS (SELECT :'spec'::jsonb v)
SELECT coalesce(jsonb_agg(jsonb_build_object(
    'owner', k.owner_id, 'doc_owner', d.owner_id, 'chunk_owner', c.owner_id, 'kb',k.kb_id,
    'doc',d.doc_id,'chunk',c.chunk_id,'name',d.filename,'content_sha256',encode(sha256(d.original),'hex'),
    'chunk_sha256',encode(sha256(convert_to(c.body,'UTF8')),'hex')
)), '[]') FROM openscience_m4_knowledge.collections k JOIN openscience_m4_knowledge.documents d USING(kb_id)
JOIN openscience_m4_knowledge.chunks c USING(kb_id,doc_id), s WHERE d.filename=v->>'filename';
SELECT coalesce(jsonb_agg(c.relname ORDER BY c.relname),'[]') FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON c.relnamespace=n.oid WHERE n.nspname='openscience_m4_knowledge'
AND c.relname IN ('collections','documents','chunks') AND c.relrowsecurity AND c.relforcerowsecurity;
"""

MODEL = """
WITH s AS (SELECT :'spec'::jsonb v)
SELECT coalesce(jsonb_agg(jsonb_build_object(
    'request_id',r.request_id,'owner_id',r.owner_id,'login_id',r.login_id,'workspace_session',r.workspace_session,
    'turn_id',r.turn_id,'workspace_generation_fingerprint',r.workspace_generation_fingerprint,
    'model',r.model,'protocol',r.protocol,'settlement',r.state,
    'input_tokens',r.input_tokens,'output_tokens',r.output_tokens
)), '[]') FROM openscience_m4.model_requests r, s WHERE r.workspace_session=v->>'workspace_session';
"""


def match_provider_receipts(state_dir, rows):
    db = (Path(state_dir) / "m4-model-receipts" / "receipts.sqlite").resolve(strict=True)
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        for row in rows:
            observed = conn.execute(
                "SELECT input_tokens,output_tokens FROM receipts WHERE request_id=?",
                (row["request_id"],),
            ).fetchall()
            require(
                observed == [(row["input_tokens"], row["output_tokens"])],
                "The model worker's durable token receipt differs from the same request in PostgreSQL",
            )
    return len(rows)


def reconcile(spec, pg, after):
    users, baseline = spec["users"], spec["baseline"]
    require(
        baseline["deployment"] == spec["deployment"] and set(users) == set(OWNERS), "Wrong dev deployment"
    )
    docs, rls = query(pg, KNOWLEDGE, {"filename": spec["filename"]})
    require(
        rls == ["chunks", "collections", "documents"], "Knowledge RLS missing on one or more private tables"
    )
    require(len(docs) == 2, "Expected exactly two single-chunk uploads under the shared filename")
    report = {"forced_rls": rls, "users": {}, "other_account_deltas": {}}
    for name in TABLES:
        before = baseline["counts"][name]
        others = (set(before) | set(after[name])) - set(OWNERS.values())
        require(
            all(after[name].get(uid, 0) == before.get(uid, 0) for uid in others),
            "Unrelated owner received " + name,
        )
        report["other_account_deltas"][name] = 0
    for tag, uid in OWNERS.items():
        user = users[tag]
        require(user["id"] == uid, "Test login is not the retained dev-m1 account")
        (doc,) = [r for r in docs if r["doc"] == user["document_id"]]
        require(
            doc
            == {
                "owner": uid,
                "doc_owner": uid,
                "chunk_owner": uid,
                "kb": user["kb_id"],
                "doc": user["document_id"],
                "chunk": user["chunk_id"],
                "name": spec["filename"],
                "content_sha256": user["content_sha256"],
                "chunk_sha256": user["citation_text_sha256"],
            },
            "Stored owner, original bytes or cited passage differs from browser evidence",
        )
        (model_rows,) = query(pg, MODEL, {"workspace_session": user["workspace_session"]})
        visible = {row["request_id"]: row for row in user["requests"]}
        require(
            len(model_rows) == len(visible) == len(user["requests"]) > 0,
            "Model visibility or settlement count mismatch",
        )
        for row in model_rows:
            require(
                row
                == {
                    **visible[row["request_id"]],
                    "owner_id": uid,
                    "login_id": user["login_id"],
                    "workspace_session": user["workspace_session"],
                    "turn_id": user["turn_id"],
                    "model": user["model"],
                },
                "Provider token counts, actor or settled request differs from browser",
            )
        expected_counts = {"collections": 1, "documents": 1, "chunks": 1, "model": len(model_rows)}
        counts = {
            table: after[table].get(uid, 0) - baseline["counts"][table].get(uid, 0) for table in TABLES
        }
        require(counts == expected_counts, "Unexpected records during the browser test: " + str(counts))
        report["users"][tag] = {
            "owner": "test-user-" + tag,
            "new_rows": counts,
            "upload_and_citation_match": True,
            "original_workspace_and_login": True,
            "matching_provider_receipts": match_provider_receipts(spec["state_dir"], model_rows),
            "actual_input_tokens": sum(row["input_tokens"] for row in model_rows),
            "actual_output_tokens": sum(row["output_tokens"] for row in model_rows),
        }
    return report


def main():
    spec = json.load(sys.stdin)
    pg = pg_target(spec)
    counts = snapshot(pg)
    result = (
        reconcile(spec, pg, counts)
        if "users" in spec
        else {"deployment": spec["deployment"], "counts": counts}
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()

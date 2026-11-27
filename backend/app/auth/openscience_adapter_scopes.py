"""Native request scopes for the two private dev-m1 form adapters."""

import hashlib
import json
import re
from typing import Literal, get_args

AdapterTool = Literal[
    "get_polytao_capabilities",
    "calculate_polytao_descriptors",
    "prepare_polytao_generation",
    "get_polytao_draft",
    "get_polytao_job",
    "update_polytao_draft",
    "confirm_polytao_generation",
    "cancel_polytao_draft",
    "get_retrosynthesis_capabilities",
    "resolve_retrosynthesis_target",
    "prepare_monomer_retrosynthesis",
    "get_retrosynthesis_draft",
    "get_retrosynthesis_job",
    "update_retrosynthesis_draft",
    "confirm_monomer_retrosynthesis",
    "cancel_retrosynthesis_draft",
]
TOOLS = frozenset(get_args(AdapterTool))
CONFIRM = {"confirm_polytao_generation", "confirm_monomer_retrosynthesis"}
FORM_ONLY = CONFIRM | {name for name in TOOLS if name.startswith(("update_", "cancel_"))}
UI = FORM_ONLY | {"resolve_retrosynthesis_target", "get_polytao_draft", "get_retrosynthesis_draft"}
JOB_READS = {"get_polytao_job", "get_retrosynthesis_job"}
BASES = {
    "polytao": "/api/v1/conditional-generation/polytao",
    "retrosynthesis": "/api/v1/monomer-retrosynthesis",
}


def service(tool):
    if tool in TOOLS:
        return "polytao" if "polytao" in tool else "retrosynthesis"
    return "gateway"


def draft_key(owner, draft, revision):
    value = json.dumps([str(owner), str(owner), f"draft:{draft}:{revision}"], separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def native_target(row, body, conn):
    tool = row["tool"]
    base = BASES[service(tool)]
    path = body.path
    query = body.query
    key = body.idempotency_key
    if body.method == "POST" and path == base + "/jobs":
        permitted = (
            tool in CONFIRM
            and row["confirmed"]
            and not query
            and key == draft_key(row["owner_id"], row["draft_id"], row["revision"])
        )
        if not permitted:
            return False
        preparation = (
            "prepare_polytao_generation"
            if service(tool) == "polytao"
            else "prepare_monomer_retrosynthesis"
        )
        # The form's original tool part identifies which login prepared it. Check again
        # at native admission so a sibling login cannot submit via an adapter credential.
        return bool(
            conn.execute(
                "SELECT 1 FROM openscience_m3.operations WHERE owner_id=%s AND login_id=%s "
                "AND workspace_session=%s AND turn_id=%s AND call_id=%s AND channel='model' AND tool=%s",
                (
                    row["owner_id"],
                    row["login_id"],
                    row["workspace_session"],
                    row["turn_id"],
                    row["call_id"],
                    preparation,
                ),
            ).fetchone()
        )
    if body.method == "GET" and path == base + "/jobs/lookup":
        if (
            tool not in CONFIRM | {"get_polytao_draft", "get_retrosynthesis_draft"}
            or not key
            or query != "idempotency_key=" + key
        ):
            return False
        # The readback must recover a key previously confirmed for this exact owner's draft.
        revisions = conn.execute(
            "SELECT revision FROM openscience_m3.operations WHERE owner_id=%s AND draft_id=%s "
            "AND workspace_session=%s AND confirmed AND tool=ANY(%s)",
            (row["owner_id"], row["draft_id"], row["workspace_session"], list(CONFIRM)),
        ).fetchall()
        return any(key == draft_key(row["owner_id"], row["draft_id"], r["revision"]) for r in revisions)
    if key:
        return False
    if tool in JOB_READS:
        if body.method != "GET" or path != base + "/jobs/" + str(row["job_id"]):
            return False
        return not query or (
            tool == "get_retrosynthesis_job" and re.fullmatch(r"wait_seconds=(?:[0-9]|1[0-9]|20)", query)
        )
    if query:
        return False
    if path == base + "/status":
        return body.method == "GET" and tool.startswith("get_") and tool.endswith("_capabilities")
    if body.method != "POST":
        return False
    return (
        path == BASES["polytao"] + "/descriptors"
        and tool
        in {"calculate_polytao_descriptors", "prepare_polytao_generation", "update_polytao_draft"}
    ) or (
        path == BASES["retrosynthesis"] + "/targets/resolve"
        and tool
        in {
            "resolve_retrosynthesis_target",
            "prepare_monomer_retrosynthesis",
            "update_retrosynthesis_draft",
        }
    )

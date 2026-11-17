#!/usr/bin/env python3
"""Explicit, durable retrosynthesis acceptance through model/UI MCP credentials.

One stable report key creates one draft and job. An interrupted run is recovered
with --resume; recovery only reads the saved draft and job, never confirms again.
Reports and molecular results remain in the private development runtime.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import urllib.error
import urllib.request

import retrosynthesis_mcp_dev as dev


class VerificationError(dev.DevError):
    pass


def require(value, message: str):
    if not value:
        raise VerificationError(message)


@contextmanager
def report_lock(report_file: str):
    """Keep the key's marker check, submission and report writes indivisible.

    Retain the lock file after closing: unlinking it could let another process
    lock a different inode while an existing run still owns the original lock.
    This lock is separate from the deployment lock and from other report keys.
    """
    descriptor = os.open(dev.RUNTIME / (report_file + ".lock"),
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
                "Acceptance report lock must be an owner-only ordinary file with one link.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise VerificationError("This report key is already being verified. No request was sent; inspect the active report before continuing with its accepted --job-id.") from None
        yield
    finally:
        os.close(descriptor)


def native_get(path: str) -> dict:
    require(path.startswith("/api/v1/monomer-retrosynthesis/") and ".." not in path, "Native verification is restricted to retrosynthesis reads.")
    session = json.loads(dev.read_private("server/session.json"))
    request = urllib.request.Request(dev.BACKEND_LOOPBACK + path, headers={
        "Host": "nexpoly_dev-backend-1:8000", "Cookie": session["cookie"], "X-Session-Context": session["session_id"]})
    with dev.opener().open(request, timeout=15) as response:
        return dev.read_json_response(response, max_bytes=2 * 1024 * 1024)


def runtime_metadata() -> dict:
    state = native_get("/api/v1/monomer-retrosynthesis/status")
    digest = hashlib.sha256()
    sources = [*sorted((dev.ROOT / "backend/app").glob("retrosynthesis_mcp*.py")),
               dev.ROOT / "backend/app/mcp_security.py", dev.ROOT / "Dockerfile.retrosynthesis-mcp",
               dev.ROOT / "backend/requirements-retrosynthesis-mcp.lock"]
    for source in sources:
        digest.update(str(source.relative_to(dev.ROOT)).encode() + b"\0" + source.read_bytes() + b"\0")
    return {"code_revision": dev.run(["git", "rev-parse", "HEAD"]).strip(), "adapter_source_sha256": digest.hexdigest(),
        "mcp_image_id": dev.run(["docker", "inspect", "--format", "{{.Image}}", dev.PROJECT + "-" + dev.SERVICE + "-1"]).strip(),
        "backend_image_id": dev.docker_field(".Image"), "model_loaded_before": state.get("model_loaded"),
        "model_available": state.get("model_available"), "schema_ready": state.get("schema_ready")}


def timings(snapshot: dict) -> dict:
    def difference(end, start):
        if not snapshot.get(end) or not snapshot.get(start):
            return None
        return round(max(0, (datetime.fromisoformat(snapshot[end].replace("Z", "+00:00"))
            - datetime.fromisoformat(snapshot[start].replace("Z", "+00:00"))).total_seconds() * 1000), 3)
    return {"queue_and_loading_ms": difference("started_at", "created_at"),
        "execution_and_persistence_ms": difference("finished_at", "started_at"),
        "native_total_ms": difference("finished_at", "created_at"),
        "model_prediction_ms": (snapshot.get("result") or {}).get("query_time_ms"),
        "note": "Queue and initial model loading share the pre-start interval; no separate loading duration is inferred."}


def verify(key: str, smiles: str = "Nc1ccc(N)cc1", *, name: str | None = None, job_id: str | None = None,
           resume: bool = False, polls: int = 15, scenario: str = "unspecified") -> dict:
    require(isinstance(key, str) and 1 <= len(key) <= 128, "A stable report key of 1–128 characters is required.")
    require(type(polls) is int and 1 <= polls <= 30, "polls must be between 1 and 30.")
    require(scenario in {"cold", "hot", "queued", "unspecified"}, "Invalid timing scenario.")
    dev.private_directory()
    filename = "acceptance-" + hashlib.sha256(key.encode()).hexdigest()[:20] + ".json"
    with report_lock(filename):
        return _verify_locked(key, smiles, name=name, job_id=job_id, resume=resume or bool(job_id),
                              polls=polls, scenario=scenario, filename=filename)


def _verify_locked(key, smiles, *, name, job_id, resume, polls, scenario, filename):
    with dev.operation_lock():
        dev.verify_project_ownership()
        dev.verify_dev_target()
        dev.verify_session_file()
    previous = json.loads(dev.read_private(filename, max_bytes=2 * 1024 * 1024)) if (dev.RUNTIME / filename).exists() else None
    if previous and previous.get("submit_attempted") and not resume:
        raise VerificationError("This key already has a submission marker. Use --resume to read its original draft and job; no automatic resubmission.")
    if resume:
        require(previous and previous.get("draft_id"), "Resume requires this key's saved draft report.")
        require(not job_id or previous.get("job_id") in {None, job_id}, "The supplied job_id differs from this key's saved job.")
    target = {"source": "name", "name": name} if name is not None else {"source": "smiles", "smiles": smiles}
    conversation_id = "ses_retro_acceptance_" + hashlib.sha256(key.encode()).hexdigest()[:24]
    report = previous or {"report_key": key, "started_at": datetime.now(timezone.utc).isoformat(),
        "mcp_url": dev.BASE_URL + "/mcp", "conversation_id": conversation_id, "target": target,
        "parameters": {"num_return_sequences": 3, "num_beams": 5, "max_new_tokens": 128},
        "submit_attempted": False, "job_id": None, "checks": [], "requested_scenario": scenario}
    require(report["conversation_id"] == conversation_id, "Saved conversation differs from this report key.")
    model, ui = dev.rpc_client(), dev.rpc_client(ui=True)
    counter = 0
    def save():
        dev.write_private(filename, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    def call(tool, arguments, *, ui_only=False):
        nonlocal counter
        counter += 1
        result = (ui if ui_only else model)("tools/call", {"name": tool, "arguments": arguments}, counter)
        value = result.get("structuredContent", {})
        if result.get("isError") or value.get("ok") is not True:
            code = (value.get("error") or {}).get("code", "TOOL_CALL_FAILED")
            raise VerificationError("Acceptance tool returned " + str(code) + ". Preserve the report and recover the same draft.")
        return value
    started = time.monotonic()
    try:
        for rpc in (model, ui):
            rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "nexpoly-retrosynthesis-acceptance", "version": "1"}}, 0)
        if resume:
            draft = call("get_retrosynthesis_draft", {"conversation_id": conversation_id, "draft_id": report["draft_id"]})
            require(not job_id or draft.get("job_id") in {None, job_id}, "Native draft receipt differs from requested job.")
            if not draft.get("job_id"):
                report["status"] = "submission_unknown"
                report["checks"].append("Recovery read the original draft without confirming or submitting.")
                return {"status": "pending", "draft_id": report["draft_id"], "report": str(dev.RUNTIME / filename)}
            job_id = report["job_id"] = draft["job_id"]
        else:
            capabilities = call("get_retrosynthesis_capabilities", {})
            require(capabilities.get("enabled") and capabilities.get("available") and capabilities.get("submission_ready"), "Retrosynthesis is unavailable for prediction.")
            report["versions"] = runtime_metadata()
            draft = call("prepare_monomer_retrosynthesis", {"conversation_id": conversation_id})
            require(not draft.get("ready") and not draft.get("job_id"), "An incomplete form must not create a job.")
            report["draft_id"] = draft["draft_id"]
            report["checks"].append("Empty form is incomplete and creates no job.")
            save()
            resolved = call("resolve_retrosynthesis_target", {"conversation_id": conversation_id, "target": target})
            require(resolved.get("status") == "resolved", "Target requires clarification; this verifier never selects an ambiguous molecule.")
            updated = call("update_retrosynthesis_draft", {"conversation_id": conversation_id, "draft_id": draft["draft_id"],
                "revision": draft["revision"], "target": target, "parameters": report["parameters"]}, ui_only=True)
            require(updated.get("ready") and not updated.get("job_id"), "Saving a resolved form must not create a job.")
            draft = call("get_retrosynthesis_draft", {"conversation_id": conversation_id, "draft_id": draft["draft_id"]})
            require(draft["revision"] == updated["revision"] and draft["resolved_target"]["canonical_smiles"] == resolved["target"]["canonical_smiles"], "Draft readback differs from the saved target.")
            report["canonical_smiles"], report["revision"] = draft["resolved_target"]["canonical_smiles"], draft["revision"]
            report["submit_attempted"] = True
            save()
            arguments = {"conversation_id": conversation_id, "draft_id": draft["draft_id"], "revision": draft["revision"]}
            submit_started = time.monotonic()
            confirmed = call("confirm_monomer_retrosynthesis", arguments, ui_only=True)
            report["submit_return_ms"] = round((time.monotonic() - submit_started) * 1000, 3)
            job_id = report["job_id"] = confirmed["job_id"]
            save()
            replay = call("confirm_monomer_retrosynthesis", arguments, ui_only=True)
            require(replay["job_id"] == job_id and replay.get("submission", {}).get("replayed") is True, "Repeated confirmation did not replay the original job.")
            report["checks"].extend(["Resolved form saved and read without submitting.", "Two explicit confirmations returned one job identifier."])
        snapshot = {}
        for _ in range(polls):
            snapshot = call("get_retrosynthesis_job", {"conversation_id": conversation_id, "job_id": job_id, "wait_seconds": 20})
            report["snapshot"] = snapshot
            save()
            if snapshot["status"] in {"completed", "failed", "timed_out", "interrupted", "unknown"}:
                break
        if snapshot.get("status") not in {"completed", "failed", "timed_out", "interrupted", "unknown"}:
            report["status"] = "pending"
            return {"status": "pending", "job_id": job_id, "report": str(dev.RUNTIME / filename)}
        require(snapshot["status"] == "completed", "Prediction reached a non-success state; inspect the saved snapshot without resubmitting.")
        prediction = snapshot.get("result") or {}
        candidates = prediction.get("candidates", [])
        require(prediction.get("canonical_smiles") == report["canonical_smiles"], "Result target differs from the confirmed target.")
        require(prediction.get("total") == len(candidates) and len(candidates) <= report["parameters"]["num_return_sequences"], "Candidate counts differ from the confirmed request.")
        require(len({candidate["rank"] for candidate in candidates}) == len(candidates), "Duplicate candidate ranks.")
        for candidate in candidates:
            require(type(candidate.get("valid_smiles")) is bool and isinstance(candidate.get("reactants_smiles"), str), "Candidate structure or validity is missing.")
        native = native_get("/api/v1/monomer-retrosynthesis/jobs/" + job_id)
        require(native.get("job_id") == job_id and native.get("status") == "completed"
            and (native.get("result") or {}).get("canonical_smiles") == prediction["canonical_smiles"], "Persisted native receipt differs from the MCP result.")
        report["timings"] = timings(snapshot)
        report["valid_candidate_count"] = sum(candidate["valid_smiles"] for candidate in candidates)
        report["checks"].extend(["Every returned candidate is accounted for, including invalid or zero candidates.", "Authorized native read confirms the same persisted job and target."])
        report["status"] = "passed"
        return {"status": "passed", "job_id": job_id, "returned_count": len(candidates),
            "valid_candidate_count": report["valid_candidate_count"], "timings": report["timings"], "report": str(dev.RUNTIME / filename)}
    except Exception as error:
        report["status"] = "failed"
        report["error"] = str(error) if isinstance(error, dev.DevError) else "Verification transport or response failure; private diagnostics suppressed."
        raise
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["last_checked_at"] = datetime.now(timezone.utc).isoformat()
        save()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-key", required=True)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--smiles", default="Nc1ccc(N)cc1")
    target.add_argument("--name")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--job-id", help="Read a job already associated with this report; implies --resume")
    parser.add_argument("--polls", type=int, default=15)
    parser.add_argument("--scenario", choices=("cold", "hot", "queued", "unspecified"), default="unspecified")
    args = parser.parse_args()
    try:
        result = verify(args.report_key, args.smiles, name=args.name, job_id=args.job_id, resume=args.resume, polls=args.polls, scenario=args.scenario)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] == "passed" else 2
    except (dev.DevError, OSError, ValueError, KeyError, urllib.error.URLError) as error:
        print(json.dumps({"error": "retrosynthesis_acceptance_failed", "message": str(error) if isinstance(error, dev.DevError)
            else "Verification failed; inspect the private acceptance report."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


#!/usr/bin/env python3
"""Explicit small generation acceptance against the independent development MCP.

Unlike the launcher's read-only smoke, this command explicitly confirms GPU
work through the UI-only MCP credential. An operator-supplied stable report key
is mandatory. It never retries confirmation after an uncertain response.
Concurrent runs for the same report key fail immediately rather than queue.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
import stat
import time
import urllib.error
import urllib.request

import polytao_mcp_dev as dev


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


def runtime_metadata() -> dict:
    """Record actual image and model provenance without exposing session data."""
    session = json.loads(dev.read_private("server/session.json"))
    request = urllib.request.Request(dev.BACKEND_LOOPBACK + "/api/v1/conditional-generation/polytao/status", headers={
        "Host": "nexpoly_dev-backend-1:8000", "Cookie": session["cookie"],
        "X-Session-Context": session["session_id"]})
    with dev.opener().open(request, timeout=15) as response:
        status = dev.read_json_response(response)
    digest = hashlib.sha256()
    sources = [*sorted((dev.ROOT / "backend/app").glob("polytao_mcp*.py")),
               dev.ROOT / "backend/app/mcp_security.py", dev.ROOT / "backend/app/models.py",
               dev.ROOT / "Dockerfile.polytao-mcp", dev.ROOT / "backend/requirements-polytao-mcp.lock"]
    for source in sources:
        digest.update(str(source.relative_to(dev.ROOT)).encode() + b"\0" + source.read_bytes() + b"\0")
    return {"code_revision": dev.run(["git", "rev-parse", "HEAD"]).strip(),
            "adapter_source_sha256": digest.hexdigest(),
            "mcp_image_id": dev.run(["docker", "inspect", "--format", "{{.Image}}",
                                     dev.PROJECT + "-" + dev.SERVICE + "-1"]).strip(),
            "backend_image_id": dev.docker_field(".Image"),
            "model_id": status.get("model_id"), "model_revision": status.get("model_revision"),
            "runtime_mode": status.get("worker_mode")}


def verify(key: str, smiles: str = "*CC*", *, job_id: str | None = None, polls: int = 10) -> dict:
    require(1 <= len(key) <= 128, "A stable idempotency key of 1–128 characters is required.")
    dev.private_directory()
    # One report and lock per stable key; never create a fresh draft while
    # another run may be checking or persisting the same submission marker.
    report_file = "acceptance-" + hashlib.sha256(key.encode()).hexdigest()[:20] + ".json"
    with report_lock(report_file):
        return _verify_locked(key, smiles, job_id=job_id, polls=polls, report_file=report_file)


def _verify_locked(key: str, smiles: str, *, job_id: str | None, polls: int, report_file: str) -> dict:
    dev.verify_project_ownership()
    dev.verify_dev_target()
    dev.verify_session_file()
    # One report per stable key, so an interrupted run can be inspected without
    # guessing whether a previous request reached the GPU service.
    report = {"mcp_url": dev.BASE_URL + "/mcp", "started_at": datetime.now(timezone.utc).isoformat(),
              "idempotency_key": key, "requested_count": 3, "reference_smiles": smiles,
              "job_id": job_id, "submit_attempted": bool(job_id), "checks": []}
    if (dev.RUNTIME / report_file).exists():
        previous = json.loads(dev.read_private(report_file, max_bytes=262144))
        if previous.get("submit_attempted") and not job_id:
            raise VerificationError("This key already has an acceptance report. Inspect it and use --job-id to continue querying; no automatic resubmission.")
        if job_id and previous.get("job_id") not in {None, job_id}:
            raise VerificationError("The supplied job_id differs from this key's saved accepted job.")
        report["submit_attempted"] = bool(previous.get("submit_attempted") or job_id)
    dev.write_private(report_file, json.dumps(report, indent=2) + "\n")
    start = time.monotonic()
    rpc = dev.rpc_client()
    ui_rpc = dev.rpc_client(ui=True) if job_id is None else None
    counter = 0
    def call(name: str, arguments: dict, *, confirm: bool = False):
        nonlocal counter
        counter += 1
        result = (ui_rpc if confirm else rpc)("tools/call", {"name": name, "arguments": arguments}, counter)
        output = result.get("structuredContent", {})
        if result.get("isError") or output.get("ok") is not True:
            error = (output.get("error") or {}).get("code", "TOOL_CALL_FAILED")
            raise VerificationError("Acceptance tool returned " + str(error) + "; inspect the private report and do not invent a new submission key.")
        return output
    try:
        rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                          "clientInfo": {"name": "nexpoly-polytao-generation-acceptance", "version": "1"}}, 0)
        if ui_rpc:
            ui_rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "nexpoly-polytao-confirmation-acceptance", "version": "1"}}, 0)
        capabilities = call("get_polytao_capabilities", {})
        require(capabilities.get("enabled") and capabilities.get("available"), "PolyTAO is not available for generation.")
        report["capabilities"] = capabilities
        report["versions"] = runtime_metadata()
        descriptors = call("calculate_polytao_descriptors", {"smiles": smiles})
        require(len(descriptors.get("descriptors", {})) == 15, "Reference descriptor vector must contain 15 values.")
        report["parameters"] = {"descriptors": descriptors["descriptors"], "candidate_count": 3,
                                "temperature": 1.0, "top_k": 100, "top_p": 0.999, "max_length": 300}
        if job_id is None:
            conversation_id = "ses_acceptance_" + hashlib.sha256(key.encode()).hexdigest()[:24]
            draft = call("prepare_polytao_generation", {**report["parameters"], "conversation_id": conversation_id})
            require(draft.get("ready") and draft.get("job_id") is None, "Preparing parameters must not create a generation task.")
            report["draft"] = draft
            report["submit_attempted"] = True
            # Persist before sending. A crash after this point cannot silently
            # lead this verifier to issue another submit on its next run.
            dev.write_private(report_file, json.dumps(report, indent=2) + "\n")
            confirmed = call("confirm_polytao_generation", {"draft_id": draft["draft_id"],
                "conversation_id": conversation_id, "revision": draft["revision"]}, confirm=True)
            submitted = confirmed["submission"]
            job_id = confirmed["job_id"]
            report["job_id"] = job_id
            report["submission"] = submitted
            dev.write_private(report_file, json.dumps(report, indent=2) + "\n")
        snapshot = {}
        for _ in range(polls):
            snapshot = call("get_polytao_job", {"job_id": job_id, "wait_seconds": 20, "offset": 0, "limit": 10})
            report["snapshot"] = snapshot
            dev.write_private(report_file, json.dumps(report, indent=2) + "\n")
            if snapshot["status"] in {"completed", "failed", "cancelled"}:
                break
        report["status"] = snapshot.get("status")
        if snapshot.get("status") not in {"completed", "failed", "cancelled"}:
            report["checks"].append("query budget exhausted; continue with the same job_id")
            return {"status": "pending", "job_id": job_id, "report": str(dev.RUNTIME / report_file)}
        require(snapshot["status"] == "completed", "Generation reached a non-success terminal state; inspect its saved snapshot.")
        results = snapshot.get("results", [])
        require(snapshot["requested_count"] == 3 and 0 <= snapshot["returned_count"] <= 3,
                "Candidate counts do not match the acceptance request.")
        require(len(results) == snapshot["returned_count"] and snapshot.get("next_offset") is None,
                "Acceptance snapshot must contain every candidate.")
        canonical = set()
        for candidate in results:
            require(candidate.get("valid_smiles") is True, "Backend returned a candidate not marked as valid.")
            extracted = call("calculate_polytao_descriptors", {"smiles": candidate["generated_smiles"]})
            structure = extracted["canonical_smiles"]
            require(structure.count("*") >= 2, "Canonical candidate has fewer than two wildcard connection atoms.")
            require(structure not in canonical, "Canonical candidate structures are not unique.")
            canonical.add(structure)
            score = candidate.get("sa_score")
            require(score is None or (type(score) in {float, int} and math.isfinite(score) and score >= 0),
                    "Candidate SA score is not a finite nonnegative value or null.")
        report["checks"].extend(["counts match request", "all returned candidates parse through RDKit descriptor API",
                                  "at least two wildcard connection atoms", "canonical structures unique",
                                  "SA scores finite or absent"])
        return {"status": "passed", "job_id": job_id, "returned_count": len(results),
                "report": str(dev.RUNTIME / report_file)}
    except Exception as error:
        report["status"] = "failed"
        report["error"] = str(error) if isinstance(error, dev.DevError) else "Verification transport or response failure; private diagnostics suppressed."
        raise
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - start, 3)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        dev.write_private(report_file, json.dumps(report, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-key", "--idempotency-key", dest="report_key", required=True,
                        help="Stable report key; never change it to retry uncertain confirmation")
    parser.add_argument("--smiles", default="*CC*", help="Reference structure used only for descriptors")
    parser.add_argument("--job-id", help="Continue querying an already accepted job, without submitting")
    args = parser.parse_args()
    try:
        result = verify(args.report_key, args.smiles, job_id=args.job_id)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] == "passed" else 2
    except (dev.DevError, OSError, ValueError, KeyError, urllib.error.URLError) as error:
        print(json.dumps({"error": "polytao_generation_acceptance_failed", "message": str(error)
              if isinstance(error, dev.DevError) else "Verification failed; inspect the private acceptance report."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

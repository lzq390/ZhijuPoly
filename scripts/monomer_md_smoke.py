#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from typing import Any


EXPECTED_STEPS = 300
OPERATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
CAPABILITY_RE = re.compile(r"^[0-9a-f]{64}$")


def request_json(url: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
    encoded = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/json"} if encoded is not None else {},
        method="POST" if encoded is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"monomer MD smoke request returned HTTP {exc.code}") from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError("monomer MD smoke request failed") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("monomer MD smoke response was not a JSON object")
    return payload


def _require_control_response(
    payload: dict[str, Any],
    *,
    operation_id: str,
    job_id: str | None = None,
) -> tuple[str, str | None]:
    returned_operation = payload.get("operation_id")
    returned_job_id = payload.get("job_id")
    if (
        payload.get("schema_version") != 1
        or returned_operation != operation_id
        or not isinstance(returned_job_id, str)
        or not returned_job_id
        or (job_id is not None and returned_job_id != job_id)
    ):
        raise RuntimeError("monomer MD canary control returned mismatched evidence")
    capability = payload.get("capability")
    if capability is not None and (
        not isinstance(capability, str)
        or CAPABILITY_RE.fullmatch(capability) is None
    ):
        raise RuntimeError("monomer MD canary control returned an invalid capability")
    return returned_job_id, capability


def _wait_for_zero_capacity(api: str, deadline: float) -> None:
    while time.monotonic() < deadline:
        final_status = request_json(f"{api}/status")
        if (
            final_status.get("active_jobs") == 0
            and final_status.get("database_active_jobs") == 0
            and final_status.get("can_submit") is True
        ):
            return
        time.sleep(2)
    raise RuntimeError("monomer MD smoke capacity did not return to zero")


def run_smoke(
    base_url: str,
    timeout_seconds: int,
    expected_byteff2_commit: str,
    operation_id: str,
    source_sha: str,
) -> str:
    api = f"{base_url.rstrip('/')}/api/v1/monomer-md"
    control = (
        f"{base_url.rstrip('/')}/internal/deployment/monomer-md-canary"
    )
    identity = {
        "operation_id": operation_id,
        "source_sha": source_sha,
        "expected_byteff2_commit": expected_byteff2_commit,
    }
    created = request_json(f"{control}/submit", body=identity)
    job_id, capability = _require_control_response(
        created,
        operation_id=operation_id,
    )
    if capability is None:
        raise RuntimeError("monomer MD canary submission omitted its capability")
    continuation = {**identity, "capability": capability}
    deadline = time.monotonic() + timeout_seconds
    if created.get("status") in {"cleanup-intent", "cleaned"}:
        recovered = request_json(f"{control}/cleanup", body=continuation)
        _require_control_response(
            recovered,
            operation_id=operation_id,
            job_id=job_id,
        )
        if recovered.get("status") != "cleaned":
            raise RuntimeError(
                "monomer MD canary recovery did not reach the cleaned state"
            )
        if recovered.get("validated") is True:
            _wait_for_zero_capacity(api, deadline)
            return job_id
        created = request_json(
            f"{control}/submit",
            body={**identity, "capability": capability},
        )
        job_id, capability = _require_control_response(
            created,
            operation_id=operation_id,
            job_id=job_id,
        )
        if capability is None:
            raise RuntimeError(
                "monomer MD canary retry omitted its replacement capability"
            )
        continuation = {**identity, "capability": capability}

    payload: dict[str, Any] = {}
    try:
        while time.monotonic() < deadline:
            payload = request_json(f"{api}/jobs/{job_id}")
            job_status = payload.get("status")
            if job_status == "completed":
                break
            if job_status in {"failed", "cancelled"}:
                raise RuntimeError(
                    f"monomer MD smoke reached terminal status {job_status}"
                )
            time.sleep(5)
        else:
            raise RuntimeError("monomer MD smoke timed out")

        result = (
            payload.get("result")
            if isinstance(payload.get("result"), dict)
            else {}
        )
        summary = (
            result.get("summary")
            if isinstance(result.get("summary"), dict)
            else {}
        )
        if payload.get("requested_steps") != EXPECTED_STEPS:
            raise RuntimeError(
                "monomer MD smoke requested step count was not 300"
            )
        if (
            payload.get("completed_steps") != EXPECTED_STEPS
            or summary.get("n_steps") != EXPECTED_STEPS
        ):
            raise RuntimeError(
                "monomer MD smoke completed step count was not 300"
            )
        if payload.get("byteff2_git_sha") != expected_byteff2_commit:
            raise RuntimeError(
                "monomer MD smoke ByteFF2 commit differs from the pinned asset release"
            )
        if (
            result.get("not_equilibrated") is not True
            or result.get("physical_density_estimate") is not False
        ):
            raise RuntimeError(
                "monomer MD smoke lost its non-physical demo markers"
            )
        if not result.get("warnings"):
            raise RuntimeError("monomer MD smoke did not return a warning")
        artifacts = (
            payload.get("artifacts")
            if isinstance(payload.get("artifacts"), dict)
            else {}
        )
        serialized_artifacts = json.dumps(artifacts, sort_keys=True)
        for required in ("npt_state.csv", "npt.dcd"):
            if required not in serialized_artifacts:
                raise RuntimeError(
                    f"monomer MD smoke did not report {required}"
                )

        validated = request_json(f"{control}/validated", body=continuation)
        _require_control_response(
            validated,
            operation_id=operation_id,
            job_id=job_id,
        )
        if (
            validated.get("status") != "validated"
            or validated.get("validated") is not True
        ):
            raise RuntimeError(
                "monomer MD canary validation did not persist exact evidence"
            )
    finally:
        cleaned = request_json(f"{control}/cleanup", body=continuation)
        _require_control_response(
            cleaned,
            operation_id=operation_id,
            job_id=job_id,
        )
        if cleaned.get("status") != "cleaned":
            raise RuntimeError(
                "monomer MD canary cleanup did not reach the cleaned state"
            )

    _wait_for_zero_capacity(api, deadline)
    return job_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the authoritative 300-step monomer MD CCO smoke.")
    parser.add_argument("--base-url", default="http://127.0.0.1:9000")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--expected-byteff2-commit", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    if args.timeout_seconds < 30 or args.timeout_seconds > 3600:
        raise SystemExit("--timeout-seconds must be between 30 and 3600")
    if OPERATION_ID_RE.fullmatch(args.operation_id) is None:
        raise SystemExit("--operation-id is invalid")
    if SHA_RE.fullmatch(args.source_sha) is None:
        raise SystemExit("--source-sha must be a full lowercase Git SHA")
    if SHA_RE.fullmatch(args.expected_byteff2_commit) is None:
        raise SystemExit(
            "--expected-byteff2-commit must be a full lowercase Git SHA"
        )
    job_id = run_smoke(
        args.base_url,
        args.timeout_seconds,
        args.expected_byteff2_commit,
        args.operation_id,
        args.source_sha,
    )
    print(f"monomer MD 300-step smoke completed: {job_id}")


if __name__ == "__main__":
    main()

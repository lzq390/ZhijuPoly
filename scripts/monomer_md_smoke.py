#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from typing import Any


EXPECTED_STEPS = 300


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


def run_smoke(base_url: str, timeout_seconds: int, expected_byteff2_commit: str) -> str:
    api = f"{base_url.rstrip('/')}/api/v1/monomer-md"
    before = request_json(f"{api}/status")
    if before.get("available") is not True or before.get("can_submit") is not True:
        raise RuntimeError("monomer MD service was not ready before smoke submission")
    if before.get("default_steps") != EXPECTED_STEPS:
        raise RuntimeError("monomer MD backend did not report the 300-step contract")

    created = request_json(f"{api}/jobs", body={"smiles": "CCO"})
    job_id = created.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise RuntimeError("monomer MD smoke submission did not return a job ID")

    deadline = time.monotonic() + timeout_seconds
    payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = request_json(f"{api}/jobs/{job_id}")
        job_status = payload.get("status")
        if job_status == "completed":
            break
        if job_status in {"failed", "cancelled"}:
            raise RuntimeError(f"monomer MD smoke reached terminal status {job_status}")
        time.sleep(5)
    else:
        raise RuntimeError("monomer MD smoke timed out")

    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    if payload.get("requested_steps") != EXPECTED_STEPS:
        raise RuntimeError("monomer MD smoke requested step count was not 300")
    if payload.get("completed_steps") != EXPECTED_STEPS or summary.get("n_steps") != EXPECTED_STEPS:
        raise RuntimeError("monomer MD smoke completed step count was not 300")
    if payload.get("byteff2_git_sha") != expected_byteff2_commit:
        raise RuntimeError("monomer MD smoke ByteFF2 commit differs from the pinned asset release")
    if result.get("not_equilibrated") is not True or result.get("physical_density_estimate") is not False:
        raise RuntimeError("monomer MD smoke lost its non-physical demo markers")
    if not result.get("warnings"):
        raise RuntimeError("monomer MD smoke did not return a warning")
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    serialized_artifacts = json.dumps(artifacts, sort_keys=True)
    for required in ("npt_state.csv", "npt.dcd"):
        if required not in serialized_artifacts:
            raise RuntimeError(f"monomer MD smoke did not report {required}")

    while time.monotonic() < deadline:
        final_status = request_json(f"{api}/status")
        if (
            final_status.get("active_jobs") == 0
            and final_status.get("database_active_jobs") == 0
            and final_status.get("can_submit") is True
        ):
            return job_id
        time.sleep(2)
    raise RuntimeError("monomer MD smoke capacity did not return to zero")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the authoritative 300-step monomer MD CCO smoke.")
    parser.add_argument("--base-url", default="http://127.0.0.1:9000")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--expected-byteff2-commit", required=True)
    args = parser.parse_args()
    if args.timeout_seconds < 30 or args.timeout_seconds > 3600:
        raise SystemExit("--timeout-seconds must be between 30 and 3600")
    job_id = run_smoke(args.base_url, args.timeout_seconds, args.expected_byteff2_commit)
    print(f"monomer MD 300-step smoke completed: {job_id}")


if __name__ == "__main__":
    main()

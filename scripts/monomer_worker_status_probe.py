#!/usr/bin/env python3
"""Validate a bounded Worker health payload and print only a safe summary."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from monomer_backend_status_probe import (
    MAX_RESPONSE_BYTES,
    ProbeFailure,
    decode_status,
    safe_status_summary,
    transport_is_ready,
)


def worker_is_ready(
    health: dict[str, object], *, require_transport_ready: bool
) -> bool:
    if health.get("status") != "ok" or health.get("runtime_ready") is not True:
        return False
    return not require_transport_ready or transport_is_ready(health)


def worker_is_drained(health: dict[str, object]) -> bool:
    """Accept a valid degraded snapshot once it reports no active jobs.

    A deployment may exist specifically to repair a degraded runtime.  Requiring
    runtime readiness while waiting for the old Worker to drain would make that
    repair impossible even after ``POST /drain`` succeeded.
    """

    active_jobs = health.get("active_jobs")
    return (
        isinstance(active_jobs, int)
        and not isinstance(active_jobs, bool)
        and active_jobs == 0
        and health.get("draining") is True
        and health.get("accepting_jobs") is False
    )


def safe_worker_summary(health: dict[str, object]) -> str:
    summary = json.loads(safe_status_summary(health))
    if health.get("status") in {"degraded", "ok"}:
        summary["status"] = health["status"]
    summary["runtime_ready"] = health.get("runtime_ready") is True
    active_jobs = health.get("active_jobs")
    if isinstance(active_jobs, int) and not isinstance(active_jobs, bool):
        summary["active_jobs"] = active_jobs
    if isinstance(health.get("draining"), bool):
        summary["draining"] = health["draining"]
    if isinstance(health.get("accepting_jobs"), bool):
        summary["accepting_jobs"] = health["accepting_jobs"]
    protocols = health.get("protocols")
    transport = protocols.get("Transport") if isinstance(protocols, dict) else None
    if isinstance(transport, dict):
        summary["transport"] = {
            "runtime_ready": transport.get("runtime_ready") is True,
            "supported": transport.get("supported") is True,
        }
    summary["valid_payload"] = True
    return json.dumps(summary, sort_keys=True, separators=(",", ":"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-transport-ready", action="store_true")
    parser.add_argument(
        "--drain-check",
        action="store_true",
        help="accept a valid draining, non-accepting snapshot with zero active jobs",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    payload = sys.stdin.buffer.read(MAX_RESPONSE_BYTES + 1)
    try:
        health = decode_status(payload)
    except ProbeFailure as exc:
        category = (
            "response_too_large"
            if exc.kind == "response_too_large" or len(payload) > MAX_RESPONSE_BYTES
            else "invalid_payload"
        )
        print(
            json.dumps(
                {"error_category": category, "valid_payload": False},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1

    print(safe_worker_summary(health))
    if args.drain_check:
        return 0 if worker_is_drained(health) else 1
    return 0 if worker_is_ready(
        health, require_transport_ready=args.require_transport_ready
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())

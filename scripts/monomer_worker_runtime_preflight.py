#!/usr/bin/env python3
"""Run the candidate Worker's cached-runtime probe without starting its API."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-root", required=True, type=Path)
    parser.add_argument("--require-transport-ready", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    worker_root = args.worker_root.resolve()
    if not (worker_root / "app" / "config.py").is_file():
        print(
            '{"error_category":"candidate_worker_missing","valid_preflight":false}',
            file=sys.stderr,
        )
        return 2

    sys.path.insert(0, str(worker_root))
    try:
        from app.config import load_settings
        from app.runtime_health import probe_runtime_snapshot

        settings = load_settings()
        snapshot = probe_runtime_snapshot(settings)
        protocols = snapshot.protocols_dict()
        transport = protocols.get("Transport")
        transport_supported = (
            isinstance(transport, dict) and transport.get("supported") is True
        )
        transport_ready = (
            transport_supported
            and transport.get("runtime_ready") is True
            and "runtime_error" in transport
            and transport.get("runtime_error") is None
        )
        summary = {
            "runtime_ready": snapshot.runtime_ready is True,
            "transport_ready": transport_ready,
            "transport_supported": transport_supported,
            "valid_preflight": True,
        }
        print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
        if snapshot.runtime_ready is not True:
            return 1
        if args.require_transport_ready and not transport_ready:
            return 1
        return 0
    except Exception:
        print(
            '{"error_category":"candidate_probe_failed","valid_preflight":false}',
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

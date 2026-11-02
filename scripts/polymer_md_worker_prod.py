#!/usr/bin/env python3
"""Verify, run or control one explicitly bound production polymer MD Worker."""
from __future__ import annotations

import argparse
import http.client
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import polymer_md_worker_dev as worker


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "run-worker", "check", "drain", "resume"))
    parser.add_argument("--config", required=True, type=Path, help="private production JSON (schema_version=2)")
    args = parser.parse_args(argv)
    config = worker.load_configuration(args.config, _production=True)
    digest, runtime = worker.verify_configuration(config)
    if args.action == "verify":
        print(json.dumps({"operation_id": config.values["operation_id"], "source_sha": config.values["source_sha"],
            "worker_release_digest": digest, "runtime_fingerprint": runtime.get("runtime_fingerprint"),
            "runtime_ready": runtime.get("ready") is True, "scientific_acceptance": "not_evaluated"}, sort_keys=True))
        return 0 if runtime.get("ready") is True else 1
    if args.action == "run-worker":
        worker.run_worker(config, runtime)
        return 0
    return worker.control(config, args.action, digest, runtime)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, http.client.HTTPException) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        raise SystemExit(1)

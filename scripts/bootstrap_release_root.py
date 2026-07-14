#!/usr/bin/env python3
"""Plan or initialize the NexPoly release-control directory layout.

This tool never changes containers, databases, systemd, credentials, or the
``ops/current`` pointer.  It is safe to run without ``--apply`` for an audit.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


PRODUCTION_ROOT = Path("/data/lzq/gith/nexpoly")
DIRECTORIES = {
    "ops": 0o700,
    "ops/config": 0o700,
    "ops/incoming": 0o700,
    "ops/logs": 0o700,
    "ops/releases": 0o700,
    "ops/state": 0o700,
    "ops/state/monomer-md-worker-socket": 0o700,
    "ops/state/monomer-md-worker-runs": 0o700,
    "backups": 0o700,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-root", default=str(PRODUCTION_ROOT))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--confirm-production-root",
        help=f"must equal {PRODUCTION_ROOT} when --apply is used",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.production_root).resolve()
    plan = {
        "action": "bootstrap-release-root",
        "apply": args.apply,
        "production_root": str(root),
        "directories": [str(root / relative) for relative in DIRECTORIES],
        "excluded_actions": [
            "change running services",
            "rotate credentials",
            "create current symlink",
            "copy development assets",
        ],
    }
    if not args.apply:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    if root != PRODUCTION_ROOT or args.confirm_production_root != str(PRODUCTION_ROOT):
        print(
            f"bootstrap-release-root: error: --apply requires --production-root and "
            f"--confirm-production-root to equal {PRODUCTION_ROOT}",
            file=sys.stderr,
        )
        return 2
    os.umask(0o077)
    for relative, mode in DIRECTORIES.items():
        path = root / relative
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)
    lock = root / "ops" / "state" / "deploy.lock"
    lock.touch(exist_ok=True)
    os.chmod(lock, 0o600)
    plan["status"] = "initialized"
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

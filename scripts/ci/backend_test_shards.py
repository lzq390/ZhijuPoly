#!/usr/bin/env python3
"""Select every backend pytest module exactly once across stable CI shards."""

from __future__ import annotations

import argparse
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = REPOSITORY_ROOT / "backend" / "tests"


def test_modules() -> list[Path]:
    return sorted(
        path.relative_to(REPOSITORY_ROOT)
        for path in TEST_ROOT.glob("test_*.py")
        if path.is_file()
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shards", type=int, default=3)
    parser.add_argument("--shard", type=int)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    if args.shards < 1:
        parser.error("--shards must be positive")
    if args.shard is not None and not 0 <= args.shard < args.shards:
        parser.error("--shard must be between 0 and --shards - 1")

    modules = test_modules()
    if not modules:
        raise SystemExit("no backend test modules were discovered")
    if any(path.parts[:2] != ("backend", "tests") or not (REPOSITORY_ROOT / path).is_file() for path in modules):
        raise SystemExit("backend shard paths must be repository-relative files under backend/tests")

    assignments = {
        shard: [path for index, path in enumerate(modules) if index % args.shards == shard]
        for shard in range(args.shards)
    }
    flattened = [path for shard in range(args.shards) for path in assignments[shard]]
    if sorted(flattened) != modules or len(flattened) != len(set(flattened)):
        raise SystemExit("backend test shard assignment is incomplete or duplicated")

    if args.verify:
        sizes = ", ".join(f"shard-{shard}={len(paths)}" for shard, paths in assignments.items())
        print(f"verified {len(modules)} backend test modules: {sizes}")
        return 0

    if args.shard is None:
        parser.error("--shard is required unless --verify is used")
    for path in assignments[args.shard]:
        print(path.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

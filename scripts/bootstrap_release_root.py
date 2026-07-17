#!/usr/bin/env python3
"""Fail-closed shim for the retired in-checkout release-root bootstrap."""

from __future__ import annotations

import sys


REPLACEMENT = "./scripts/bootstrap_pull_deploy.py"


def main(argv: list[str] | None = None) -> int:
    del argv
    print(
        "bootstrap-release-root: error: this ops/current release bootstrap is "
        f"retired and never mutates state; use `{REPLACEMENT}` to initialize "
        "the external /data/lzq/gith/nexpoly-runtime control plane",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

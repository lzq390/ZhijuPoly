"""Scope entrypoint: no scientific program starts before durable scope ownership."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import time

from .journal import atomic_json, read_json


def process_start_ticks(pid: int) -> int | None:
    try:
        # comm may contain spaces or parentheses; fields following its final ')' start at state (field 3).
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--gate", type=Path, required=True)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--expires", type=float, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    identity = {"unit": args.unit, "nonce": args.nonce, "pid": os.getpid(), "start_ticks": process_start_ticks(os.getpid())}
    atomic_json(args.identity, identity)
    while time.time() < args.expires:
        if args.gate.exists():
            gate = read_json(args.gate)
            if gate != {**identity, "allowed": True}:
                raise SystemExit(125)
            if not command:
                raise SystemExit(125)
            os.execvpe(command[0], command, os.environ)
        time.sleep(.05)
    raise SystemExit(125)


if __name__ == "__main__":
    main()

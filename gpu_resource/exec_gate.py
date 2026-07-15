"""Block an execution child until its host process identity is fenced."""

from __future__ import annotations

import os
import sys


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "--":
        return 125
    raw_descriptor = os.environ.pop("NEXPOLY_GPU_EXEC_GATE_FD", "")
    try:
        descriptor = int(raw_descriptor)
    except ValueError:
        return 125
    try:
        admitted = os.read(descriptor, 1)
    except OSError:
        return 126
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    if admitted != b"1":
        return 126
    command = sys.argv[2:]
    try:
        os.execvpe(command[0], command, os.environ)
    except OSError:
        return 127


if __name__ == "__main__":
    raise SystemExit(main())

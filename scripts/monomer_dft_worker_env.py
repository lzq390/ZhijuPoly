#!/usr/bin/env python3
"""Load the sealed production Monomer-DFT runtime environment.

The control-runtime selector deliberately drops arbitrary systemd variables.
This loader rebuilds the complete, reviewed production environment from the
content-addressed runtime binding written by the deploy controller.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import sys
from typing import Mapping


RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
SOURCE_ROOT = Path("/data/lzq/gith/nexpoly")
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
RELEASE_RE = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
EXPECTED_KEYS = frozenset(
    {
        "MONOMER_DFT_RELEASE_SHA",
        "MONOMER_DFT_RUNTIME_CONTRACT_SHA256",
        "MONOMER_DFT_RUNTIME_INVENTORY_SHA256",
        "MONOMER_DFT_PYTHON",
        "AIMNET_CACHE_DIR",
        "WARP_CACHE_PATH",
        "NEXPOLY_DFT_GPU_GUARD_MODE",
    }
)


class EnvironmentError(RuntimeError):
    """A public-safe environment validation failure."""


def load_runtime_environment(path: Path) -> dict[str, str]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EnvironmentError("sealed monomer DFT environment is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size < 1
            or metadata.st_size > 64 * 1024
        ):
            raise EnvironmentError("sealed monomer DFT environment is unsafe")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024 + 1))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        final_metadata = os.fstat(descriptor)
        path_metadata = path.lstat()
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            len(payload) != metadata.st_size
            or any(
                getattr(metadata, field) != getattr(final_metadata, field)
                for field in stable_fields
            )
            or any(
                getattr(metadata, field) != getattr(path_metadata, field)
                for field in stable_fields
            )
        ):
            raise EnvironmentError("sealed monomer DFT environment changed while reading")
    finally:
        os.close(descriptor)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EnvironmentError("sealed monomer DFT environment is not UTF-8") from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if (
            not separator
            or not key
            or not value
            or key in values
            or key not in EXPECTED_KEYS
            or line != line.strip()
            or any(character in value for character in ("\x00", "\r", "\n", "'", '"', "\\"))
        ):
            raise EnvironmentError("sealed monomer DFT environment is malformed")
        values[key] = value
    if set(values) != EXPECTED_KEYS:
        raise EnvironmentError("sealed monomer DFT environment is incomplete")
    if values["NEXPOLY_DFT_GPU_GUARD_MODE"] not in {"enforce", "observe"}:
        raise EnvironmentError("sealed monomer DFT guard mode is invalid")
    release = values["MONOMER_DFT_RELEASE_SHA"]
    if (
        RELEASE_RE.fullmatch(release) is None
        or DIGEST_RE.fullmatch(values["MONOMER_DFT_RUNTIME_CONTRACT_SHA256"])
        is None
        or DIGEST_RE.fullmatch(values["MONOMER_DFT_RUNTIME_INVENTORY_SHA256"])
        is None
    ):
        raise EnvironmentError("sealed monomer DFT runtime identity is invalid")
    expected_root = RUNTIME_ROOT / "worker-venvs/dft" / release
    expected_paths = {
        "MONOMER_DFT_PYTHON": expected_root / "venv/bin/python",
        "AIMNET_CACHE_DIR": expected_root / "aimnet-cache",
        "WARP_CACHE_PATH": (
            RUNTIME_ROOT / "state/monomer-dft-warp-cache" / release
        ),
    }
    if any(Path(values[key]) != expected for key, expected in expected_paths.items()):
        raise EnvironmentError("sealed monomer DFT runtime paths differ")
    return values


def build_environment(
    values: Mapping[str, str], inherited: Mapping[str, str] | None = None
) -> dict[str, str]:
    source = os.environ if inherited is None else inherited
    result = {
        key: source[key]
        for key in ("HOME", "LANG", "LC_ALL", "LOGNAME", "USER", "XDG_RUNTIME_DIR")
        if key in source
    }
    result.update(values)
    result.update(
        {
            "PATH": SAFE_PATH,
            "MONOMER_DFT_DEPLOYMENT": "prod",
            "MONOMER_DFT_PROD_RUNTIME_ROOT": str(RUNTIME_ROOT),
            "MONOMER_DFT_WORKER_UDS": str(
                RUNTIME_ROOT / "state/monomer-dft-worker-socket/worker.sock"
            ),
            "MONOMER_DFT_JOB_ROOT": str(
                RUNTIME_ROOT / "state/monomer-dft-worker-runs"
            ),
            "MONOMER_DFT_DOWNLOAD_SPOOL_ROOT": str(
                RUNTIME_ROOT / "state/monomer-dft-download-spool"
            ),
            "MONOMER_DFT_GPU_GUARD_STATE": str(RUNTIME_ROOT / "state/gpu2-guard.json"),
            "NEXPOLY_DFT_GPU_DEVICE": "2",
            "NEXPOLY_DFT_OVERFLOW_GPU_DEVICES": "",
            "NEXPOLY_DEV_GPU1_ONLY_SESSION": "0",
            "MONOMER_DFT_GPU_BROKER_ENABLED": "0",
            "MONOMER_DFT_STANDALONE_GPU_SMOKE": "0",
            "MONOMER_DFT_MAX_CONCURRENT_JOBS": "1",
            "MONOMER_DFT_MAX_QUEUED_JOBS": "8",
            "MONOMER_DFT_GPU_BUDGET_MIB": "4096",
            "MONOMER_DFT_GPU_ACTIVE_THREAD_PERCENTAGE": "50",
            "MONOMER_DFT_WORKER_INSTANCE": str(SOURCE_ROOT),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("exec", "validate"))
    parser.add_argument("path", type=Path)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        values = load_runtime_environment(args.path)
        environment = build_environment(values)
        if args.command == "validate":
            return 0
        command = list(args.argv)
        if command and command[0] == "--":
            command.pop(0)
        if not command:
            raise EnvironmentError("monomer DFT launcher command is missing")
        os.execvpe(command[0], command, environment)
    except EnvironmentError as exc:
        print(f"monomer-dft-worker-env: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(
            f"monomer-dft-worker-env: command execution failed with errno {exc.errno}",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

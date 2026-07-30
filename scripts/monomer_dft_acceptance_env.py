#!/usr/bin/env python3
"""Parse the formal monomer-DFT dotenv file without executing shell syntax."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import sys


KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
ALLOWED_KEYS = frozenset(
    {
        "AIMNET_CACHE_DIR",
        "AIMNET_MODEL_SOURCE_DIR",
        "AIMNET_SOURCE_CLONE",
        "AIMNET_SOURCE_DIR",
        "AIMNET_SOURCE_LOCK",
        "CUDA_DEVICE_ORDER",
        "MONOMER_DFT_ARTIFACT_RETENTION_DAYS",
        "MONOMER_DFT_DEPLOYMENT",
        "MONOMER_DFT_JOB_RETENTION_DAYS",
        "MONOMER_DFT_JOB_RETENTION_ENABLED",
        "MONOMER_DFT_DOWNLOAD_MAX_CONCURRENT",
        "MONOMER_DFT_DOWNLOAD_SPOOL_ROOT",
        "MONOMER_DFT_DRAIN_TIMEOUT_SECONDS",
        "MONOMER_DFT_FATAL_RESTART_BACKOFF_SECONDS",
        "MONOMER_DFT_FATAL_RESTART_MAX_ATTEMPTS",
        "MONOMER_DFT_FATAL_RESTART_MAX_BACKOFF_SECONDS",
        "MONOMER_DFT_FATAL_RESTART_RESET_SECONDS",
        "MONOMER_DFT_GPU_ACTIVE_THREAD_PERCENTAGE",
        "MONOMER_DFT_GPU_BROKER_ENABLED",
        "MONOMER_DFT_GPU_BROKER_UDS",
        "MONOMER_DFT_GPU_BUDGET_MIB",
        "MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS",
        "MONOMER_DFT_GPU_MPS_PIPE_ROOT",
        "MONOMER_DFT_JOB_ROOT",
        "MONOMER_DFT_MAX_CONCURRENT_JOBS",
        "MONOMER_DFT_MAX_QUEUED_JOBS",
        "MONOMER_DFT_OPTIMIZATION_TIMEOUT_SECONDS",
        "MONOMER_DFT_PYTHON",
        "MONOMER_DFT_RECONCILE_INTERVAL_SECONDS",
        "MONOMER_DFT_SINGLE_POINT_TIMEOUT_SECONDS",
        "MONOMER_DFT_STANDALONE_GPU_SMOKE",
        "MONOMER_DFT_VALIDATION_CONCURRENCY",
        "MONOMER_DFT_WORKER_TIMEOUT_SECONDS",
        "MONOMER_DFT_WORKER_UDS",
        "NEXPOLY_DFT_BACKEND_PORT",
        "NEXPOLY_DFT_FRONTEND_PORT",
        "NEXPOLY_DFT_GPU_DEVICE",
        "NEXPOLY_DFT_OVERFLOW_GPU_DEVICES",
        "NEXPOLY_DFT_POSTGRES_PASSWORD",
        "NEXPOLY_DFT_POSTGRES_PORT",
        "NEXPOLY_DFT_PROJECT_NAME",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
        "PYTHONPATH",
        "UV_CACHE_DIR",
        "WARP_CACHE_PATH",
    }
)
EXPANSION_MARKERS = ("$", "`")
MAX_ENV_BYTES = 64 * 1024


class AcceptanceEnvError(ValueError):
    """The formal acceptance dotenv file is unsafe or malformed."""


def _file_snapshot(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _validate_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise AcceptanceEnvError(
            "formal acceptance dotenv must be an owner-private, "
            "single-link 0600 regular file"
        )
    if metadata.st_size > MAX_ENV_BYTES:
        raise AcceptanceEnvError(
            "formal acceptance dotenv exceeds the bounded input size"
        )


def _read_private_file(path: Path) -> bytes:
    flags = (
        os.O_RDONLY
        | os.O_CLOEXEC
        | os.O_NOFOLLOW
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AcceptanceEnvError(
            "formal acceptance dotenv cannot be opened safely"
        ) from exc
    try:
        before = os.fstat(descriptor)
        _validate_metadata(before)
        expected = _file_snapshot(before)
        payload = bytearray()
        while True:
            remaining = MAX_ENV_BYTES + 1 - len(payload)
            if remaining <= 0:
                raise AcceptanceEnvError(
                    "formal acceptance dotenv exceeds the bounded input size"
                )
            try:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
            except InterruptedError:
                continue
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
        if (
            _file_snapshot(after) != expected
            or len(payload) != after.st_size
        ):
            raise AcceptanceEnvError(
                "formal acceptance dotenv changed while it was read"
            )
        _validate_metadata(after)
        try:
            path_metadata = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise AcceptanceEnvError(
                "formal acceptance dotenv path changed while it was read"
            ) from exc
        if _file_snapshot(path_metadata) != expected:
            raise AcceptanceEnvError(
                "formal acceptance dotenv path changed while it was read"
            )
        return bytes(payload)
    except OSError as exc:
        raise AcceptanceEnvError(
            "formal acceptance dotenv could not be read safely"
        ) from exc
    finally:
        os.close(descriptor)


def _decode_value(raw: str, *, line_number: int) -> str:
    if raw != raw.strip():
        raise AcceptanceEnvError(
            f"line {line_number}: leading/trailing value whitespace is forbidden"
        )
    quoted = raw.startswith(("'", '"'))
    if quoted:
        quote = raw[0]
        if len(raw) < 2 or raw[-1] != quote or quote in raw[1:-1]:
            raise AcceptanceEnvError(
                f"line {line_number}: quoted value is malformed"
            )
        value = raw[1:-1]
    else:
        value = raw
        if any(character in value for character in ("'", '"', "#")):
            raise AcceptanceEnvError(
                f"line {line_number}: unquoted comment/quote syntax is forbidden"
            )
    if any(marker in value for marker in EXPANSION_MARKERS):
        raise AcceptanceEnvError(
            f"line {line_number}: shell expansion syntax is forbidden"
        )
    if any(ord(character) < 0x20 and character != "\t" for character in value):
        raise AcceptanceEnvError(
            f"line {line_number}: control characters are forbidden"
        )
    return value


def parse_dotenv(path: Path) -> dict[str, str]:
    try:
        text = _read_private_file(path).decode("utf-8")
    except UnicodeError as exc:
        raise AcceptanceEnvError("formal acceptance dotenv is not UTF-8") from exc
    if "\x00" in text or "\r" in text:
        raise AcceptanceEnvError(
            "formal acceptance dotenv contains forbidden bytes or line endings"
        )
    values: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line or line.startswith("#"):
            continue
        key, separator, raw_value = line.partition("=")
        if (
            not separator
            or KEY_RE.fullmatch(key) is None
            or key not in ALLOWED_KEYS
        ):
            raise AcceptanceEnvError(
                f"line {line_number}: dotenv key is not allowed"
            )
        if key in values:
            raise AcceptanceEnvError(
                f"line {line_number}: duplicate dotenv key is forbidden"
            )
        values[key] = _decode_value(raw_value, line_number=line_number)
    missing = sorted(ALLOWED_KEYS - values.keys())
    if missing:
        raise AcceptanceEnvError(
            "formal acceptance dotenv is incomplete; missing: "
            + ", ".join(missing)
        )
    return values


def encode_nul_pairs(values: dict[str, str]) -> bytes:
    payload = bytearray()
    for key in sorted(values):
        payload.extend(key.encode("ascii"))
        payload.append(0)
        payload.extend(values[key].encode("utf-8"))
        payload.append(0)
    return bytes(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        payload = encode_nul_pairs(parse_dotenv(args.env_file))
        sys.stdout.buffer.write(payload)
    except (AcceptanceEnvError, OSError) as exc:
        print(f"formal acceptance dotenv: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

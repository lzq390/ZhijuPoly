#!/usr/bin/env python3
"""Validate and apply the production Monomer-MD Worker environment safely.

Only a deliberately small, literal ``KEY=VALUE`` format is accepted.  Values
are never interpreted as shell syntax.  The same implementation is used by
the release controller, candidate runtime preflight, and the stable systemd
launcher so those paths cannot disagree about configuration semantics.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import sys
from typing import Mapping
import unicodedata


MAX_ENV_FILE_BYTES = 64 * 1024
KEY_PATTERN = re.compile(r"[A-Z][A-Z0-9_]*\Z")
SAFE_SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SANITIZED_MARKER = "NEXPOLY_MONOMER_MD_ENV_SANITIZED"
SAFE_INHERITED_KEYS = frozenset(
    {"HOME", "LANG", "LC_ALL", "LOGNAME", "TMPDIR", "TZ", "USER"}
)

# This is an exact public configuration surface.  Unknown Worker settings fail
# closed instead of silently acquiring different semantics in systemd and the
# release controller.
ALLOWED_KEYS = frozenset(
    {
        "APP_POSTGRES_DSN",
        "BYTEFF2_DEMO_COMMAND",
        "BYTEFF2_DENSITY_DEMO_ENTRY",
        "BYTEFF2_DENSITY_DEMO_ENTRY_MODE",
        "BYTEFF2_OPENMM_DIR",
        "BYTEFF2_PYTHON",
        "BYTEFF2_ROOT",
        "MONOMER_MD_ARTIFACTS_COLUMN",
        "MONOMER_MD_ARTIFACT_MANIFEST_COLUMN",
        "MONOMER_MD_BYTEFF2_GIT_SHA_COLUMN",
        "MONOMER_MD_COMPLETED_STEPS_COLUMN",
        "MONOMER_MD_CUDA_VISIBLE_DEVICES",
        "MONOMER_MD_DEFAULT_STEPS",
        "MONOMER_MD_DEMO_INPUT_BOX_NM",
        "MONOMER_MD_DEMO_NATOMS",
        "MONOMER_MD_DEMO_TEMPERATURE_K",
        "MONOMER_MD_ERROR_CATEGORY_COLUMN",
        "MONOMER_MD_ERROR_COLUMN",
        "MONOMER_MD_FINISHED_AT_COLUMN",
        "MONOMER_MD_FORMAL_TIMEOUT_SECONDS",
        "MONOMER_MD_GPU_BROKER_ENABLED",
        "MONOMER_MD_GPU_BROKER_ENVIRONMENT",
        "MONOMER_MD_GPU_BROKER_HEARTBEAT_INTERVAL_SECONDS",
        "MONOMER_MD_GPU_BROKER_SOCKET_PATH",
        "MONOMER_MD_GPU_BROKER_WAIT_TIMEOUT_SECONDS",
        "MONOMER_MD_GPU_DEVICE_COLUMN",
        "MONOMER_MD_GPU_MPS_PIPE_ROOT",
        "MONOMER_MD_HEARTBEAT_AT_COLUMN",
        "MONOMER_MD_HEARTBEAT_INTERVAL_SECONDS",
        "MONOMER_MD_HEALTH_PROBE_TIMEOUT_SECONDS",
        "MONOMER_MD_JOB_ID_COLUMN",
        "MONOMER_MD_JOB_ROOT",
        "MONOMER_MD_JOB_TABLE",
        "MONOMER_MD_LEASE_EXPIRES_AT_COLUMN",
        "MONOMER_MD_LEASE_SECONDS",
        "MONOMER_MD_MAX_ACTIVE_JOBS",
        "MONOMER_MD_MAX_CONCURRENT_JOBS",
        "MONOMER_MD_MAX_STEPS",
        "MONOMER_MD_OUTPUT_DIR_COLUMN",
        "MONOMER_MD_PROGRESS_MESSAGE_COLUMN",
        "MONOMER_MD_PROGRESS_PERCENT_COLUMN",
        "MONOMER_MD_PROGRESS_STAGE_COLUMN",
        "MONOMER_MD_PROTOCOL_COLUMN",
        "MONOMER_MD_PYTHON",
        "MONOMER_MD_RECOVERY_RETRY_SECONDS",
        "MONOMER_MD_REPORT_INTERVAL",
        "MONOMER_MD_RESULT_COLUMN",
        "MONOMER_MD_RESULT_SUMMARY_COLUMN",
        "MONOMER_MD_RUN_MODE_COLUMN",
        "MONOMER_MD_STARTED_AT_COLUMN",
        "MONOMER_MD_STATUS_COLUMN",
        "MONOMER_MD_TIMEOUT_SECONDS",
        "MONOMER_MD_TRANSPORT_CUDA_SMOKE_ENABLED",
        "MONOMER_MD_UPDATED_AT_COLUMN",
        "MONOMER_MD_WORKER_HEALTH_HOST",
        "MONOMER_MD_WORKER_HOST",
        "MONOMER_MD_WORKER_ID",
        "MONOMER_MD_WORKER_ID_COLUMN",
        "MONOMER_MD_WORKER_INSTANCE_ID_COLUMN",
        "MONOMER_MD_WORKER_JOB_ID_COLUMN",
        "MONOMER_MD_WORKER_MODE",
        "MONOMER_MD_WORKER_PORT",
        "MONOMER_MD_WORKER_UDS",
        "MONOMER_MD_WORKER_VERSION",
        "MONOMER_MD_WORKER_VERSION_COLUMN",
        "NEXPOLY_GPU_DEVICE",
        "PYTHONPATH",
    }
)

# These settings belong to the deploy controller or to the derived native
# runtime environment.  They must never be accepted from worker.env.
RESERVED_KEYS = frozenset(
    {
        "LD_LIBRARY_PATH",
        "OPENMM_DIR",
        "OPENMM_PLUGIN_DIR",
        "MONOMER_MD_REQUIRE_TRANSPORT_READY",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
        SANITIZED_MARKER,
    }
)

class WorkerEnvError(RuntimeError):
    """A configuration error whose message never includes a setting value."""


def load_worker_env(path: Path) -> dict[str, str]:
    """Read one owner-only literal environment without following symlinks."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise WorkerEnvError(f"Worker environment file is missing: {path}") from None
    except OSError:
        raise WorkerEnvError(
            f"Worker environment file must be a readable non-symlink: {path}"
        ) from None

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise WorkerEnvError(f"Worker environment file must be regular: {path}")
        if metadata.st_uid != os.geteuid():
            raise WorkerEnvError(
                f"Worker environment file must be owned by uid {os.geteuid()}: {path}"
            )
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise WorkerEnvError(f"Worker environment file must have mode 0600: {path}")
        if metadata.st_size > MAX_ENV_FILE_BYTES:
            raise WorkerEnvError(
                f"Worker environment file exceeds {MAX_ENV_FILE_BYTES} bytes: {path}"
            )
        # A single os.read() is not guaranteed to return the whole regular
        # file.  Keep the descriptor opened with O_NOFOLLOW, but consume it
        # through a buffered reader so validation cannot silently ignore a
        # trailing line after a short read.
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(MAX_ENV_FILE_BYTES + 1)
    finally:
        os.close(descriptor)

    if len(raw) > MAX_ENV_FILE_BYTES:
        raise WorkerEnvError(
            f"Worker environment file exceeds {MAX_ENV_FILE_BYTES} bytes: {path}"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise WorkerEnvError(f"Worker environment file must be UTF-8: {path}") from None

    # Validate the original text before splitting.  str.splitlines() treats
    # vertical tab, form feed, record separators, and NEL as line boundaries,
    # which could otherwise erase the evidence of an injected control byte.
    if any(
        character != "\n" and unicodedata.category(character) == "Cc"
        for character in text
    ):
        raise WorkerEnvError(
            "Worker environment file contains a forbidden control character"
        )

    values: dict[str, str] = {}
    for line_number, line in enumerate(text.split("\n"), start=1):
        if not line or line.startswith("#"):
            continue
        if line != line.strip():
            raise WorkerEnvError(
                f"Worker environment line {line_number} has leading or trailing whitespace"
            )
        if "=" not in line:
            raise WorkerEnvError(
                f"Worker environment line {line_number} must use KEY=VALUE"
            )
        key, value = line.split("=", 1)
        if not KEY_PATTERN.fullmatch(key):
            raise WorkerEnvError(
                f"Worker environment line {line_number} has an invalid key"
            )
        if key in RESERVED_KEYS:
            raise WorkerEnvError(
                f"Worker environment key {key} is reserved and must not be configured"
            )
        if key not in ALLOWED_KEYS:
            raise WorkerEnvError(f"Worker environment key {key} is not allowed")
        if key in values:
            raise WorkerEnvError(f"Worker environment key {key} is duplicated")
        if value != value.strip():
            raise WorkerEnvError(
                f"Worker environment key {key} has leading or trailing whitespace"
            )
        if any(character in value for character in ("'", '"', "\\")):
            raise WorkerEnvError(
                f"Worker environment key {key} must use an unquoted literal value"
            )
        values[key] = value
    return values


def build_worker_process_environment(
    values: Mapping[str, str],
    *,
    inherited: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a scrubbed child-only environment for Worker code."""

    unknown = set(values).difference(ALLOWED_KEYS)
    if unknown:
        raise WorkerEnvError("Worker environment contains a non-allowlisted key")
    override_values = dict(overrides or {})
    unknown_overrides = set(override_values).difference(ALLOWED_KEYS)
    if unknown_overrides:
        raise WorkerEnvError("Worker environment override contains a non-allowlisted key")

    inherited_values = os.environ if inherited is None else inherited
    # Keep the stable launcher and the candidate preflight on the same small
    # manager-environment contract.  A denylist cannot anticipate future
    # CUDA, Python, Torch, package-manager, or loader controls.
    environment = {
        key: inherited_values[key]
        for key in SAFE_INHERITED_KEYS
        if key in inherited_values
    }
    environment.update(values)
    environment.update(override_values)

    byteff2_python = environment.get("BYTEFF2_PYTHON", "")
    byteff2_bin = os.path.dirname(byteff2_python) if os.path.isabs(byteff2_python) else ""
    environment["PATH"] = (
        f"{byteff2_bin}:{SAFE_SYSTEM_PATH}" if byteff2_bin else SAFE_SYSTEM_PATH
    )
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment[SANITIZED_MARKER] = "1"
    return environment


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate without printing values")
    validate.add_argument("path", type=Path)

    get = commands.add_parser("get", help="print one validated literal value")
    get.add_argument("path", type=Path)
    get.add_argument("key")
    get.add_argument("--default")

    execute = commands.add_parser("exec", help="execute with the validated environment")
    execute.add_argument("path", type=Path)
    execute.add_argument("argv", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        values = load_worker_env(args.path)
        if args.command == "validate":
            return 0
        if args.command == "get":
            if args.key not in values:
                if args.default is not None:
                    sys.stdout.write(args.default)
                    return 0
                raise WorkerEnvError(
                    f"Worker environment key {args.key} is required but missing"
                )
            sys.stdout.write(values[args.key])
            return 0

        command = list(args.argv)
        if command and command[0] == "--":
            command.pop(0)
        if not command:
            raise WorkerEnvError("Worker environment exec requires a command")
        environment = build_worker_process_environment(values)
        os.execvpe(command[0], command, environment)
    except WorkerEnvError as exc:
        print(f"monomer-worker-env: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(
            f"monomer-worker-env: command execution failed with errno {exc.errno}",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Maintain a secret-free, owner-only deployment recovery record."""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path


MAX_STATE_BYTES = 64 * 1024


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", text=True
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read(path: Path) -> dict[str, object]:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise SystemExit(
            f"Cannot read deployment state {path}: {type(exc).__name__}"
        ) from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > MAX_STATE_BYTES
        ):
            raise SystemExit(f"Deployment state security contract failed: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(MAX_STATE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_STATE_BYTES:
        raise SystemExit(f"Deployment state exceeds size limit: {path}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"Cannot read deployment state {path}: {type(exc).__name__}") from None
    if not isinstance(value, dict):
        raise SystemExit(f"Deployment state is not an object: {path}")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init")
    initialize.add_argument("path", type=Path)
    initialize.add_argument("--previous-sha", required=True)
    initialize.add_argument("--target-sha", required=True)
    initialize.add_argument("--service", action="append", default=[])
    initialize.add_argument("--worker-unit", required=True)
    initialize.add_argument("--worker-pid", required=True, type=int)
    initialize.add_argument(
        "--worker-active", required=True, choices=("true", "false", "unknown")
    )
    initialize.add_argument("--previous-venv-target", default="")
    initialize.add_argument("--candidate-venv-target", required=True)

    update = subparsers.add_parser("update")
    update.add_argument("path", type=Path)
    update.add_argument("--phase", required=True)
    update.add_argument("--status", choices=("running", "failed", "complete"), default="running")
    update.add_argument("--backup-path")
    update.add_argument("--error-category")
    update.add_argument("--current-venv-target")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "init":
        services: dict[str, dict[str, str]] = {}
        for encoded in args.service:
            try:
                name, container_id, image_id = encoded.split("=", 2)
            except ValueError:
                raise SystemExit("--service must use NAME=CONTAINER_ID=IMAGE_ID") from None
            services[name] = {"container_id": container_id, "image_id": image_id}
        state: dict[str, object] = {
            "created_at": _timestamp(),
            "phase": "initialized",
            "previous_sha": args.previous_sha,
            "services": services,
            "status": "running",
            "target_sha": args.target_sha,
            "updated_at": _timestamp(),
            "venv": {
                "candidate_target": args.candidate_venv_target,
                "current_target": args.previous_venv_target,
                "previous_target": args.previous_venv_target,
            },
            "worker": {
                "active": args.worker_active,
                "pid": args.worker_pid,
                "unit": args.worker_unit,
            },
        }
        _write(args.path, state)
        return 0

    state = _read(args.path)
    state["phase"] = args.phase
    state["status"] = args.status
    state["updated_at"] = _timestamp()
    if args.backup_path is not None:
        state["backup_path"] = args.backup_path
    if args.error_category is not None:
        state["error_category"] = args.error_category
    if args.current_venv_target is not None:
        venv = state.get("venv")
        if not isinstance(venv, dict):
            raise SystemExit("Deployment state venv field is invalid")
        venv["current_target"] = args.current_venv_target
    _write(args.path, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

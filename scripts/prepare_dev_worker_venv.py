#!/usr/bin/env python3
"""Prepare and verify the isolated development Monomer-MD Worker venv."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid

import release_controller


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VENV_NAME = ".venv-monomer-md-worker"
LOCK_RELATIVE_PATH = Path("workers/monomer_md_worker/requirements.lock")
BASE_IDENTITY_FILE = ".nexpoly-base-python-identity.json"
LOCK_EXPECTATION_FILE = ".nexpoly-worker-lock.json"
LOCK_DIGEST_FILE = ".nexpoly-worker-lock-digest.json"


class DevWorkerVenvError(RuntimeError):
    """Raised when the development Worker venv cannot be trusted."""


def _clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        environment.pop(key, None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return environment


def _load_json(path: Path) -> object:
    if not path.is_file() or path.is_symlink():
        raise DevWorkerVenvError(f"required venv record is missing or unsafe: {path}")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise DevWorkerVenvError(f"required venv record must have mode 0600: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DevWorkerVenvError(f"required venv record is invalid: {path}") from exc


def validate_layout(
    repository_root: Path,
    target: Path,
    lock: Path,
) -> tuple[Path, Path, Path]:
    try:
        root = repository_root.resolve(strict=True)
    except OSError as exc:
        raise DevWorkerVenvError("repository root cannot be resolved") from exc
    if not root.is_dir() or repository_root.is_symlink():
        raise DevWorkerVenvError("repository root must be a real directory")

    if not target.is_absolute() or ".." in target.parts:
        raise DevWorkerVenvError("development Worker venv target must be an absolute safe path")
    expected_target = root / VENV_NAME
    if target != expected_target:
        raise DevWorkerVenvError(f"development Worker venv target must be {expected_target}")
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise DevWorkerVenvError("development Worker venv target is unsafe")

    expected_lock = root / LOCK_RELATIVE_PATH
    try:
        resolved_lock = lock.resolve(strict=True)
    except OSError as exc:
        raise DevWorkerVenvError("development Worker requirements lock is missing") from exc
    if lock.is_symlink() or resolved_lock != expected_lock or not resolved_lock.is_file():
        raise DevWorkerVenvError(f"development Worker requirements lock must be {expected_lock}")
    return root, expected_target, expected_lock


def managed_worker_argv(
    pid_file: Path,
    socket_path: Path,
    *,
    proc_root: Path = Path("/proc"),
) -> list[str] | None:
    if not pid_file.is_absolute() or ".." in pid_file.parts:
        raise DevWorkerVenvError("managed Worker PID file path must be absolute and safe")
    if not socket_path.is_absolute() or ".." in socket_path.parts:
        raise DevWorkerVenvError("managed Worker socket path must be absolute and safe")
    if pid_file.is_symlink():
        raise DevWorkerVenvError("managed Worker PID file is unsafe")
    if not pid_file.exists():
        if socket_path.exists() or socket_path.is_symlink():
            raise DevWorkerVenvError(
                "managed Worker socket exists without a PID file; inspect it before preparing the venv"
            )
        return None
    if not pid_file.is_file() or pid_file.stat().st_size > 32:
        raise DevWorkerVenvError("managed Worker PID file is unsafe")
    try:
        raw_pid = pid_file.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise DevWorkerVenvError("managed Worker PID file cannot be read") from exc
    if not raw_pid.isdigit() or int(raw_pid) <= 0:
        raise DevWorkerVenvError("managed Worker PID file is invalid")
    command_path = proc_root / raw_pid / "cmdline"
    try:
        raw_command = command_path.read_bytes()
    except OSError as exc:
        raise DevWorkerVenvError(
            "managed Worker PID is stale or unreadable; run worker-stop before preparing the venv"
        ) from exc
    try:
        argv = [item.decode("utf-8") for item in raw_command.split(b"\0") if item]
    except UnicodeError as exc:
        raise DevWorkerVenvError("managed Worker command line is not UTF-8") from exc
    if not argv or "-m" not in argv or "uvicorn" not in argv or "--uds" not in argv:
        raise DevWorkerVenvError("managed Worker PID does not identify the expected Uvicorn process")
    socket_index = argv.index("--uds") + 1
    if socket_index >= len(argv) or argv[socket_index] != str(socket_path):
        raise DevWorkerVenvError("managed Worker PID does not own the configured Unix socket")
    return argv


def assert_target_not_running(
    pid_file: Path,
    socket_path: Path,
    target: Path,
    *,
    proc_root: Path = Path("/proc"),
) -> None:
    argv = managed_worker_argv(pid_file, socket_path, proc_root=proc_root)
    if argv is not None and argv[0] == str(target / "bin/python"):
        raise DevWorkerVenvError(
            "the managed Worker is using the target venv; stop it safely before replacing the venv"
        )


def lock_expectation(lock: Path, repository_root: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "requirements": release_controller.worker_lock_requirements(lock, repository_root),
    }


def lock_digest_record(lock: Path, repository_root: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "path": lock.relative_to(repository_root).as_posix(),
        "sha256": release_controller.sha256_file(lock),
    }


def _verify_runtime_prefix(venv: Path, base_identity: dict[str, object]) -> None:
    program = (
        "import json, pathlib, sys; "
        "print(json.dumps({'executable': sys.executable, "
        "'prefix': str(pathlib.Path(sys.prefix).resolve()), "
        "'base_prefix': str(pathlib.Path(sys.base_prefix).resolve())}, sort_keys=True))"
    )
    result = subprocess.run(
        [str(venv / "bin/python"), "-I", "-c", program],
        env=_clean_environment(),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    try:
        runtime = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DevWorkerVenvError("development Worker venv returned invalid runtime identity") from exc
    expected = {
        "executable": str(venv / "bin/python"),
        "prefix": str(venv.resolve(strict=True)),
        "base_prefix": base_identity["prefix"],
    }
    if runtime != expected:
        raise DevWorkerVenvError("development Worker venv runtime identity is invalid")


def verify_venv(
    repository_root: Path,
    venv: Path,
    lock: Path,
    base_python: str,
    expected_base_identity: str,
) -> dict[str, object]:
    if not venv.is_dir() or venv.is_symlink():
        raise DevWorkerVenvError(f"development Worker venv is missing or unsafe: {venv}")
    python = venv / "bin/python"
    if not python.exists() or not os.access(python, os.X_OK):
        raise DevWorkerVenvError("development Worker venv Python is missing or not executable")
    config = venv / "pyvenv.cfg"
    if not config.is_file() or config.is_symlink():
        raise DevWorkerVenvError("development Worker pyvenv.cfg is missing or unsafe")
    settings = {
        key.strip().lower(): value.strip().lower()
        for line in config.read_text(encoding="utf-8").splitlines()
        if "=" in line
        for key, value in [line.split("=", 1)]
    }
    if settings.get("include-system-site-packages") != "true":
        raise DevWorkerVenvError("development Worker venv must inherit the frozen ByteFF2 base")

    environment = _clean_environment()
    before = release_controller.inspect_worker_base_python(
        base_python,
        expected_base_identity,
        environment,
    )
    expectation = lock_expectation(lock, repository_root)
    if _load_json(venv / LOCK_EXPECTATION_FILE) != expectation:
        raise DevWorkerVenvError("development Worker lock expectation record has drifted")
    if _load_json(venv / LOCK_DIGEST_FILE) != lock_digest_record(lock, repository_root):
        raise DevWorkerVenvError("development Worker requirements lock digest has drifted")
    if _load_json(venv / BASE_IDENTITY_FILE) != before:
        raise DevWorkerVenvError("development Worker base Python identity record has drifted")

    _verify_runtime_prefix(venv, before)
    subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            release_controller.WORKER_VENV_VERIFY_PROGRAM,
            str(venv),
            str(venv / LOCK_EXPECTATION_FILE),
        ],
        env=environment,
        check=True,
    )
    pip_check = subprocess.run(
        [str(python), "-I", "-m", "pip", "check"],
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if pip_check.returncode != 0:
        detail = pip_check.stdout.strip() or "pip check failed without output"
        raise DevWorkerVenvError(f"development Worker venv has broken requirements: {detail}")
    after = release_controller.inspect_worker_base_python(
        base_python,
        expected_base_identity,
        environment,
    )
    if after != before:
        raise DevWorkerVenvError("frozen Worker base Python changed during venv verification")
    return before


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def prepare_venv(
    repository_root: Path,
    target: Path,
    lock: Path,
    pid_file: Path,
    socket_path: Path,
    base_python: str,
    expected_base_identity: str,
    wheelhouse: Path | None = None,
) -> dict[str, object]:
    root, target, lock = validate_layout(repository_root, target, lock)
    assert_target_not_running(pid_file, socket_path, target)
    environment = _clean_environment()
    base_identity = release_controller.inspect_worker_base_python(
        base_python,
        expected_base_identity,
        environment,
    )
    resolved_wheelhouse: Path | None = None
    if wheelhouse is not None:
        try:
            resolved_wheelhouse = wheelhouse.resolve(strict=True)
        except OSError as exc:
            raise DevWorkerVenvError("development Worker wheelhouse is missing") from exc
        if wheelhouse.is_symlink() or not resolved_wheelhouse.is_dir():
            raise DevWorkerVenvError("development Worker wheelhouse is unsafe")

    staging = Path(tempfile.mkdtemp(prefix=f"{VENV_NAME}.staging-", dir=root))
    os.chmod(staging, 0o700)
    backup = root / f"{VENV_NAME}.previous-{uuid.uuid4().hex}"
    moved_old = False
    moved_new = False
    try:
        subprocess.run(
            [
                str(base_identity["resolved_path"]),
                "-m",
                "venv",
                "--system-site-packages",
                str(staging),
            ],
            env=environment,
            check=True,
        )
        install = [
            str(staging / "bin/python"),
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "--ignore-installed",
            "--only-binary=:all:",
        ]
        if resolved_wheelhouse is not None:
            install.extend(["--no-index", "--find-links", str(resolved_wheelhouse)])
        install.extend(["-r", str(lock)])
        subprocess.run(install, env=environment, check=True)
        release_controller.atomic_json(
            staging / LOCK_EXPECTATION_FILE,
            lock_expectation(lock, root),
        )
        release_controller.atomic_json(
            staging / LOCK_DIGEST_FILE,
            lock_digest_record(lock, root),
        )
        release_controller.atomic_json(staging / BASE_IDENTITY_FILE, base_identity)
        verify_venv(root, staging, lock, base_python, expected_base_identity)

        if target.exists():
            target.rename(backup)
            moved_old = True
        staging.rename(target)
        moved_new = True
        _fsync_directory(root)
        try:
            final_identity = verify_venv(
                root,
                target,
                lock,
                base_python,
                expected_base_identity,
            )
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            moved_new = False
            if moved_old:
                backup.rename(target)
                moved_old = False
            _fsync_directory(root)
            raise
        if moved_old:
            shutil.rmtree(backup)
            moved_old = False
        return final_identity
    finally:
        if not moved_new:
            shutil.rmtree(staging, ignore_errors=True)
        if moved_old and not target.exists() and backup.exists():
            backup.rename(target)
        if backup.exists() and target.exists():
            shutil.rmtree(backup, ignore_errors=True)


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--base-python", required=True)
    parser.add_argument("--expected-base-identity", required=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    identity = subparsers.add_parser("identity", help="print the frozen base Python identity")
    identity.add_argument("--base-python", required=True)

    prepare = subparsers.add_parser("prepare", help="atomically prepare the isolated dev venv")
    _common_arguments(prepare)
    prepare.add_argument("--pid-file", type=Path, required=True)
    prepare.add_argument("--socket", type=Path, required=True)
    prepare.add_argument("--wheelhouse", type=Path)

    verify = subparsers.add_parser("verify", help="verify the current isolated dev venv")
    _common_arguments(verify)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "identity":
            result = release_controller.inspect_worker_base_python(args.base_python, None)
        elif args.command == "prepare":
            result = prepare_venv(
                args.repository_root,
                args.target,
                args.lock,
                args.pid_file,
                args.socket,
                args.base_python,
                args.expected_base_identity,
                args.wheelhouse,
            )
        else:
            root, target, lock = validate_layout(args.repository_root, args.target, args.lock)
            result = verify_venv(
                root,
                target,
                lock,
                args.base_python,
                args.expected_base_identity,
            )
    except (DevWorkerVenvError, release_controller.ReleaseError) as exc:
        print(f"dev Worker venv error: {exc}", file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"dev Worker venv command failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

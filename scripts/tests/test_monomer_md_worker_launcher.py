from __future__ import annotations

from pathlib import Path
import os
import shutil
import socket as socket_module
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import monomer_md_worker_launcher as launcher


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _runtime_layout(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    runtime = tmp_path / "runtime"
    _private_directory(source)
    _private_directory(runtime)
    _private_directory(runtime / "state")
    _private_directory(runtime / "state/current-assets/byteff2/submodules/bytemol")
    _private_directory(runtime / "state/monomer-md-worker-runs")
    _private_directory(runtime / "state/monomer-md-worker-socket")
    _private_directory(runtime / "state/gpu-resource")
    return source, runtime


def _environment(source: Path, runtime: Path) -> dict[str, str]:
    return {
        **launcher.expected_environment(source_root=source, runtime_root=runtime),
        launcher.SANITIZED_MARKER: "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def test_launcher_executes_selected_python_with_safe_path_flags(
    tmp_path: Path, monkeypatch
) -> None:
    source, runtime = _runtime_layout(tmp_path)
    python = runtime / "worker-venvs/md-a/venv/bin/python"
    python.parent.mkdir(mode=0o700, parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o700)
    monkeypatch.setattr(
        launcher,
        "verify_runtime_binding",
        lambda **_kwargs: (SimpleNamespace(), SimpleNamespace(), python),
    )
    captured: dict[str, object] = {}

    def execute(path: str, argv: list[str], environment: dict[str, str]) -> None:
        captured.update(path=path, argv=argv, environment=environment)

    launcher.launch(
        _environment(source, runtime),
        source_root=source,
        runtime_root=runtime,
        execute=execute,
        change_directory=lambda path: captured.update(cwd=path),
    )

    assert captured["path"] == str(python)
    assert captured["cwd"] == source
    assert captured["argv"] == [
        str(python),
        "-B",
        "-P",
        "-m",
        "uvicorn",
        "workers.monomer_md_worker.app.main:app",
        "--uds",
        str(runtime / "state/monomer-md-worker-socket/worker.sock"),
    ]


def test_launcher_rejects_configuration_selected_python(tmp_path: Path) -> None:
    source, runtime = _runtime_layout(tmp_path)
    environment = _environment(source, runtime)
    environment["MONOMER_MD_PYTHON"] = "/unreviewed/python"
    with pytest.raises(launcher.LauncherError, match="must not select"):
        launcher.validate_environment(
            environment,
            source_root=source,
            runtime_root=runtime,
        )


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("NEXPOLY_GPU_DEVICE", "1"),
        ("MONOMER_MD_CUDA_VISIBLE_DEVICES", "1"),
        ("MONOMER_MD_GPU_BROKER_ENABLED", "true"),
        ("MONOMER_MD_MAX_ACTIVE_JOBS", "1"),
        ("MONOMER_MD_MAX_CONCURRENT_JOBS", "2"),
    ),
)
def test_launcher_rejects_unpinned_production_runtime_policy(
    tmp_path: Path,
    key: str,
    value: str,
) -> None:
    source, runtime = _runtime_layout(tmp_path)
    environment = _environment(source, runtime)
    environment[key] = value

    with pytest.raises(launcher.LauncherError, match=key):
        launcher.validate_environment(
            environment,
            source_root=source,
            runtime_root=runtime,
        )


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("BYTEFF2_PYTHON", "/unreviewed/byteff2/python"),
        ("BYTEFF2_OPENMM_DIR", "/unreviewed/openmm"),
    ),
)
def test_launcher_rejects_configuration_selected_toolchain_path(
    tmp_path: Path,
    key: str,
    value: str,
) -> None:
    source, runtime = _runtime_layout(tmp_path)
    environment = _environment(source, runtime)
    environment[key] = value

    with pytest.raises(launcher.LauncherError, match=key):
        launcher.validate_environment(
            environment,
            source_root=source,
            runtime_root=runtime,
        )


def test_socket_cleanup_accepts_only_owned_unix_socket(tmp_path: Path) -> None:
    parent = tmp_path / "socket"
    _private_directory(parent)
    path = parent / "worker.sock"
    sock = socket_module.socket(socket_module.AF_UNIX)
    try:
        sock.bind(str(path))
    finally:
        sock.close()

    launcher.prepare_socket(path)
    assert not path.exists()

    path.write_text("not a socket\n", encoding="utf-8")
    with pytest.raises(launcher.LauncherError, match="not a Unix socket"):
        launcher.prepare_socket(path)
    path.unlink()
    path.symlink_to(parent / "target")
    with pytest.raises(launcher.LauncherError, match="not a Unix socket"):
        launcher.prepare_socket(path)


def test_worker_singleton_fences_a_second_launcher_and_unsafe_lock(
    tmp_path: Path,
) -> None:
    _source, runtime = _runtime_layout(tmp_path)
    first = launcher.acquire_worker_singleton(runtime)
    try:
        assert os.get_inheritable(first) is True
        with pytest.raises(launcher.LauncherError, match="already running"):
            launcher.acquire_worker_singleton(runtime)
    finally:
        os.close(first)

    lock = runtime / "state/monomer-md-worker.lock"
    lock.chmod(0o644)
    with pytest.raises(launcher.LauncherError, match="lock is unsafe"):
        launcher.acquire_worker_singleton(runtime)


def test_worker_singleton_rejects_a_symlink_lock(tmp_path: Path) -> None:
    _source, runtime = _runtime_layout(tmp_path)
    target = runtime / "state/target.lock"
    target.write_text("", encoding="utf-8")
    target.chmod(0o600)
    (runtime / "state/monomer-md-worker.lock").symlink_to(target)

    with pytest.raises(launcher.LauncherError, match="missing or unsafe"):
        launcher.acquire_worker_singleton(runtime)


def test_launcher_fails_before_exec_when_binding_is_invalid(
    tmp_path: Path, monkeypatch
) -> None:
    source, runtime = _runtime_layout(tmp_path)

    def fail_binding(**_kwargs):
        from scripts.worker_slot_runtime import WorkerSlotError

        raise WorkerSlotError("active record digest mismatch")

    monkeypatch.setattr(launcher, "verify_runtime_binding", fail_binding)
    executed = False

    def execute(_path: str, _argv: list[str], _environment: dict[str, str]) -> None:
        nonlocal executed
        executed = True

    with pytest.raises(launcher.LauncherError, match="digest mismatch"):
        launcher.launch(
            _environment(source, runtime),
            source_root=source,
            runtime_root=runtime,
            execute=execute,
            change_directory=lambda _path: None,
        )
    assert executed is False


def test_stable_launcher_imports_sibling_runtime_when_executed_standalone(
    tmp_path: Path,
) -> None:
    binary_root = tmp_path / "bin"
    binary_root.mkdir(mode=0o700)
    source_root = Path(__file__).resolve().parents[1]
    shutil.copy2(source_root / "monomer_md_worker_launcher.py", binary_root)
    shutil.copy2(source_root / "worker_slot_runtime.py", binary_root)
    shutil.copy2(source_root / "git_source_trust.py", binary_root)

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(binary_root / "monomer_md_worker_launcher.py"),
        ],
        env={"PATH": "/usr/bin:/bin"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "stable sanitizer" in completed.stderr
    assert "ModuleNotFoundError" not in completed.stderr


@pytest.mark.parametrize("unsafe_kind", ("symlink", "writable"))
def test_stable_launcher_rejects_unsafe_sibling_before_import(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    binary_root = tmp_path / "bin"
    binary_root.mkdir(mode=0o700)
    source_root = Path(__file__).resolve().parents[1]
    shutil.copy2(source_root / "monomer_md_worker_launcher.py", binary_root)
    sibling = binary_root / "worker_slot_runtime.py"
    if unsafe_kind == "symlink":
        target = tmp_path / "runtime.py"
        shutil.copy2(source_root / "worker_slot_runtime.py", target)
        sibling.symlink_to(target)
    else:
        shutil.copy2(source_root / "worker_slot_runtime.py", sibling)
        sibling.chmod(0o666)

    completed = subprocess.run(
        [sys.executable, "-I", "-B", str(binary_root / "monomer_md_worker_launcher.py")],
        env={"PATH": "/usr/bin:/bin"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "runtime helper is unsafe" in completed.stderr

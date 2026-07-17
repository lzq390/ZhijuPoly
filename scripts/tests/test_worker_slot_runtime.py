from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from scripts import worker_slot_runtime as slots


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _test_base_python() -> Path:
    # Production is Linux/systemd-only.  Use the host Python rather than the
    # pytest venv executable: CI venvs may deliberately be group-writable,
    # while a frozen production base must fail closed on that mode.
    configured = Path("/usr/bin/python3")
    assert configured.resolve(strict=True).is_file()
    return configured


def _write_private(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _git(source: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(source), *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return completed.stdout.strip()


def _initialize_source(root: Path) -> tuple[Path, str, str]:
    source = root / "source"
    source.mkdir(mode=0o700)
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "Worker Slot Test")
    _git(source, "config", "user.email", "worker-slot@example.invalid")
    lock = source / slots.WORKER_LOCK_RELATIVE_PATH
    lock.parent.mkdir(parents=True)
    lock.write_text(
        "fixture-pkg==1.0 \\\n    --hash=sha256:" + "1" * 64 + "\n",
        encoding="utf-8",
    )
    module = source / "workers/monomer_md_worker/app/main.py"
    module.parent.mkdir(parents=True)
    module.write_text("# production Worker fixture\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "fixture")
    return (
        source,
        _git(source, "rev-parse", "HEAD"),
        _git(source, "rev-parse", "HEAD^{tree}"),
    )


def _initialize_venv(
    runtime: Path,
    slot: str = "a",
    *,
    configured_base: Path | None = None,
) -> Path:
    _private_directory(runtime / "worker-venvs")
    _private_directory(slots.slot_root(runtime, slot))
    prefix = slots.slot_venv_prefix(runtime, slot)
    _private_directory(prefix)
    _private_directory(prefix / "bin")
    base = configured_base or _test_base_python()
    resolved_base = base.resolve(strict=True)
    (prefix / "bin/python").symlink_to(resolved_base)
    (prefix / "pyvenv.cfg").write_text(
        "include-system-site-packages = true\n"
        f"executable = {resolved_base}\n",
        encoding="utf-8",
    )
    return prefix


def _records(
    runtime: Path,
    *,
    slot: str,
    source_sha: str,
    source_tree: str,
    lock_digest: str,
    configured_base: Path | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    prefix = slots.slot_venv_prefix(runtime, slot)
    base = configured_base or _test_base_python()
    base_identity = slots.inspect_base_python_identity(base)
    slot_document: dict[str, object] = {
        "schema_version": slots.SLOT_RECORD_SCHEMA_VERSION,
        "component": "monomer-md",
        "status": "ready",
        "slot": slot,
        "source_sha": source_sha,
        "source_tree": source_tree,
        "worker_lock_sha256": lock_digest,
        "requirements_sha256": DIGEST_A,
        "wheel_cache_key": DIGEST_B,
        "wheel_inventory_sha256": DIGEST_A,
        "venv_prefix": str(prefix),
        "venv_inventory_sha256": slots.directory_inventory_digest(prefix),
        "base_python_configured_path": str(base),
        "base_python_identity_sha256": base_identity["identity_sha256"],
        "prepared_operation_id": "prepare-0001",
        "prepared_at": "2026-07-16T04:00:00Z",
    }
    active_document: dict[str, object] = {
        "schema_version": 1,
        "component": "monomer-md",
        "slot": slot,
        "source_sha": source_sha,
        "source_tree": source_tree,
        "worker_lock_sha256": lock_digest,
        "slot_record_sha256": slots.canonical_json_digest(slot_document),
        "operation_id": "deploy-0001",
        "activated_at": "2026-07-16T04:01:00Z",
    }
    return slot_document, active_document


def _write_records(
    runtime: Path,
    slot_document: dict[str, object],
    active_document: dict[str, object],
) -> None:
    _private_directory(runtime / "state")
    _private_directory(runtime / slots.SLOT_RECORD_DIRECTORY)
    _write_private(
        slots.slot_record_path(runtime, str(slot_document["slot"])),
        slot_document,
    )
    _write_private(runtime / slots.ACTIVE_RECORD_RELATIVE_PATH, active_document)


def test_runtime_binding_verifies_live_git_lock_and_selected_venv(tmp_path: Path) -> None:
    source, source_sha, source_tree = _initialize_source(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    prefix = _initialize_venv(runtime)
    lock_digest = slots.sha256_file(source / slots.WORKER_LOCK_RELATIVE_PATH)
    slot_document, active_document = _records(
        runtime,
        slot="a",
        source_sha=source_sha,
        source_tree=source_tree,
        lock_digest=lock_digest,
    )
    _write_records(runtime, slot_document, active_document)

    checkout, selection, python = slots.verify_runtime_binding(
        source_root=source,
        runtime_root=runtime,
    )

    assert checkout.source_sha == source_sha
    assert checkout.source_tree == source_tree
    assert selection.active.slot == "a"
    assert selection.slot.venv_prefix == str(prefix)
    assert python == prefix / "bin/python"


def test_shared_base_python_identity_matches_legacy_release_contract() -> None:
    from scripts import release_controller as legacy

    configured = _test_base_python()
    shared = slots.inspect_base_python_identity(configured)
    established = legacy.inspect_worker_base_python(
        str(configured),
        None,
        dict(os.environ),
    )

    assert shared == established


def test_runtime_binding_accepts_clean_previous_sha_behind_origin_for_rollback(
    tmp_path: Path,
) -> None:
    source, previous_sha, previous_tree = _initialize_source(tmp_path)
    (source / "next.txt").write_text("new release\n", encoding="utf-8")
    _git(source, "add", "next.txt")
    _git(source, "commit", "-m", "new release")
    new_sha = _git(source, "rev-parse", "HEAD")
    _git(source, "update-ref", "refs/remotes/origin/main", new_sha)
    _git(source, "reset", "--hard", previous_sha)

    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    _initialize_venv(runtime)
    lock_digest = slots.sha256_file(source / slots.WORKER_LOCK_RELATIVE_PATH)
    slot_document, active_document = _records(
        runtime,
        slot="a",
        source_sha=previous_sha,
        source_tree=previous_tree,
        lock_digest=lock_digest,
    )
    _write_records(runtime, slot_document, active_document)

    checkout, selection, _python = slots.verify_runtime_binding(
        source_root=source,
        runtime_root=runtime,
    )

    assert checkout.source_sha == previous_sha
    assert selection.active.source_sha == previous_sha


def test_runtime_binding_rejects_dirty_checkout_and_venv_tampering(tmp_path: Path) -> None:
    source, source_sha, source_tree = _initialize_source(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    prefix = _initialize_venv(runtime)
    lock_digest = slots.sha256_file(source / slots.WORKER_LOCK_RELATIVE_PATH)
    slot_document, active_document = _records(
        runtime,
        slot="a",
        source_sha=source_sha,
        source_tree=source_tree,
        lock_digest=lock_digest,
    )
    _write_records(runtime, slot_document, active_document)

    (source / "untracked.py").write_text("raise RuntimeError\n", encoding="utf-8")
    with pytest.raises(slots.WorkerSlotError, match="not clean"):
        slots.verify_runtime_binding(source_root=source, runtime_root=runtime)
    (source / "untracked.py").unlink()

    (prefix / "tampered.py").write_text("TAMPERED = True\n", encoding="utf-8")
    with pytest.raises(slots.WorkerSlotError, match="inventory"):
        slots.verify_runtime_binding(source_root=source, runtime_root=runtime)


def test_runtime_binding_rejects_base_python_binary_drift_without_venv_tree_change(
    tmp_path: Path,
) -> None:
    source, source_sha, source_tree = _initialize_source(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    base_root = tmp_path / "base-python"
    _private_directory(base_root)
    resolved_base = base_root / "python3"
    shutil.copy2(_test_base_python().resolve(strict=True), resolved_base)
    resolved_base.chmod(0o700)
    configured_base = base_root / "python"
    configured_base.symlink_to(resolved_base.name)
    prefix = _initialize_venv(runtime, configured_base=configured_base)
    lock_digest = slots.sha256_file(source / slots.WORKER_LOCK_RELATIVE_PATH)
    slot_document, active_document = _records(
        runtime,
        slot="a",
        source_sha=source_sha,
        source_tree=source_tree,
        lock_digest=lock_digest,
        configured_base=configured_base,
    )
    _write_records(runtime, slot_document, active_document)
    sealed_inventory = slots.directory_inventory_digest(prefix)

    with resolved_base.open("ab") as stream:
        stream.write(b"\0")
        stream.flush()
        os.fsync(stream.fileno())

    assert slots.directory_inventory_digest(prefix) == sealed_inventory
    with pytest.raises(slots.WorkerSlotError, match="identity differs"):
        slots.verify_runtime_binding(source_root=source, runtime_root=runtime)


def test_runtime_binding_rejects_configured_base_symlink_target_drift(
    tmp_path: Path,
) -> None:
    source, source_sha, source_tree = _initialize_source(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    base_root = tmp_path / "base-python"
    _private_directory(base_root)
    first = base_root / "python-first"
    second = base_root / "python-second"
    shutil.copy2(_test_base_python().resolve(strict=True), first)
    shutil.copy2(_test_base_python().resolve(strict=True), second)
    first.chmod(0o700)
    second.chmod(0o700)
    with second.open("ab") as stream:
        stream.write(b"\0")
    configured_base = base_root / "python"
    configured_base.symlink_to(first.name)
    prefix = _initialize_venv(runtime, configured_base=configured_base)
    lock_digest = slots.sha256_file(source / slots.WORKER_LOCK_RELATIVE_PATH)
    slot_document, active_document = _records(
        runtime,
        slot="a",
        source_sha=source_sha,
        source_tree=source_tree,
        lock_digest=lock_digest,
        configured_base=configured_base,
    )
    _write_records(runtime, slot_document, active_document)
    sealed_inventory = slots.directory_inventory_digest(prefix)

    configured_base.unlink()
    configured_base.symlink_to(second.name)

    assert slots.directory_inventory_digest(prefix) == sealed_inventory
    with pytest.raises(slots.WorkerSlotError, match="base executable differs"):
        slots.verify_runtime_binding(source_root=source, runtime_root=runtime)


def test_selection_rejects_extra_fields_bad_digest_and_unsafe_record(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    _initialize_venv(runtime)
    slot_document, active_document = _records(
        runtime,
        slot="a",
        source_sha="1" * 40,
        source_tree="2" * 40,
        lock_digest=DIGEST_A,
    )
    slot_document["unexpected"] = True
    _write_records(runtime, slot_document, active_document)
    with pytest.raises(slots.WorkerSlotError, match="digest"):
        slots.load_runtime_selection(runtime)

    active_document["slot_record_sha256"] = slots.canonical_json_digest(slot_document)
    _write_records(runtime, slot_document, active_document)
    with pytest.raises(slots.WorkerSlotError, match="invalid shape"):
        slots.load_runtime_selection(runtime)

    _write_private(slots.slot_record_path(runtime, "a"), slot_document)
    _write_private(runtime / slots.ACTIVE_RECORD_RELATIVE_PATH, active_document)
    (runtime / slots.ACTIVE_RECORD_RELATIVE_PATH).chmod(0o644)
    with pytest.raises(slots.WorkerSlotError, match="0600"):
        slots.load_runtime_selection(runtime)


def test_selection_rejects_record_for_the_wrong_slot(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    _initialize_venv(runtime, "b")
    _private_directory(runtime / "state")
    _private_directory(runtime / slots.SLOT_RECORD_DIRECTORY)
    slot_document, active_document = _records(
        runtime,
        slot="b",
        source_sha="1" * 40,
        source_tree="2" * 40,
        lock_digest=DIGEST_A,
    )
    active_document["slot"] = "a"
    active_document["slot_record_sha256"] = slots.canonical_json_digest(slot_document)
    _write_private(slots.slot_record_path(runtime, "a"), slot_document)
    _write_private(runtime / slots.ACTIVE_RECORD_RELATIVE_PATH, active_document)

    with pytest.raises(slots.WorkerSlotError, match="different slot"):
        slots.load_runtime_selection(runtime)


def test_operation_ids_are_lowercase_and_bounded() -> None:
    value = {
        "schema_version": 1,
        "component": "monomer-md",
        "slot": "a",
        "source_sha": "1" * 40,
        "source_tree": "2" * 40,
        "worker_lock_sha256": DIGEST_A,
        "slot_record_sha256": DIGEST_B,
        "operation_id": "Bad:1",
        "activated_at": "2026-07-16T04:01:00Z",
    }
    with pytest.raises(slots.WorkerSlotError, match="operation ID"):
        slots.validate_active_record(value)

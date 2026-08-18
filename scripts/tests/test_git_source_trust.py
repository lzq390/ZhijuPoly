from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from typing import Callable

from scripts import git_source_trust as trust
from scripts import legacy_takeover
from scripts import pull_deploy_controller
from scripts import reconcile_production_0005_polytao_alias as alias_reconcile
from scripts import worker_slot_runtime


@contextmanager
def raises(
    expected: type[BaseException],
    *,
    match: str,
):
    """Dependency-free equivalent of the small pytest.raises surface used here."""

    try:
        yield
    except expected as error:
        if re.search(match, str(error)) is None:
            raise AssertionError(
                f"{expected.__name__} message {str(error)!r} does not match "
                f"{match!r}"
            ) from error
    else:
        raise AssertionError(f"{expected.__name__} was not raised")


class MonkeyPatch:
    """Minimal reversible setattr helper for the one integration seam below."""

    def __init__(self) -> None:
        self._original: list[tuple[object, str, object]] = []

    def setattr(self, target: object, name: str, value: object) -> None:
        self._original.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self) -> None:
        while self._original:
            target, name, value = self._original.pop()
            setattr(target, name, value)


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["/usr/bin/git", "-C", str(root), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "source"
    root.mkdir(mode=0o700)
    prior = os.umask(0o077)
    try:
        git(root, "init", "-b", "main")
        git(root, "config", "user.name", "Trust Test")
        git(root, "config", "user.email", "trust@example.invalid")
        (root / "payload.txt").write_text("trusted\n", encoding="utf-8")
        git(root, "add", "payload.txt")
        git(root, "commit", "-m", "trusted")
    finally:
        os.umask(prior)
    return (
        root,
        git(root, "rev-parse", "HEAD"),
        git(root, "rev-parse", "HEAD^{tree}"),
    )


def canonical_remote(root: Path) -> None:
    git(
        root,
        "remote",
        "add",
        "origin",
        "https://github.com/lzq390/ZhijuPoly.git",
    )


def permission_marker(tmp_path: Path) -> tuple[Path, Path]:
    runtime = tmp_path / "runtime"
    state = runtime / "state"
    state.mkdir(parents=True, mode=0o700)
    runtime.chmod(0o700)
    state.chmod(0o700)
    return runtime, trust.permission_takeover_marker_path(runtime)


def make_git_authority_group_writable(root: Path) -> dict[str, int]:
    paths = [root, *(path for path, _relative in trust._permission_walk(root))]
    for path in paths:
        metadata = path.lstat()
        if path.is_dir():
            path.chmod((metadata.st_mode & 0o700) | 0o070)
        else:
            path.chmod((metadata.st_mode & 0o700) | 0o060)
    return {
        str(path.relative_to(root)) if path != root else ".": (
            path.lstat().st_mode & 0o777
        )
        for path in paths
    }


def permission_record_modes(
    root: Path,
    records: list[dict[str, object]],
) -> dict[str, int]:
    return {
        str(record["path"]): (
            root
            if record["path"] == "."
            else root / str(record["path"])
        ).lstat().st_mode
        & 0o777
        for record in records
    }


def permission_retired_paths(marker: Path) -> list[Path]:
    prefix = trust._permission_retired_prefix(marker)
    return sorted(
        path
        for path in marker.parent.iterdir()
        if path.name.startswith(prefix)
    )


def evidence(root: Path, source_sha: str, source_tree: str):
    return trust.repository_trust_evidence(
        root,
        source_sha=source_sha,
        source_tree=source_tree,
        branch="refs/heads/main",
        origin=None,
        ambient={},
    )


PERMISSION_CRASH_CASES = [
    ("takeover", "permission:captured"),
    ("takeover", "permission:root-intent"),
    ("takeover", "permission:root:action"),
    ("takeover", "permission:root-hardened"),
    ("takeover", "permission:metadata-directories-intent"),
    ("takeover", "permission:directory:.git:action"),
    ("takeover", "permission:metadata-directories-hardened"),
    ("takeover", "permission:metadata-files-intent"),
    ("takeover", "permission:file:.git/config:action"),
    ("takeover", "permission:metadata-files-hardened"),
    ("takeover", "permission:hardened"),
    ("restore", "permission:restore-files-intent"),
    ("restore", "permission:restore-file:.git/config:action"),
    ("restore", "permission:restore-files-restored"),
    ("restore", "permission:restore-directories-intent"),
    ("restore", "permission:restore-directory:.git:action"),
    ("restore", "permission:restore-directories-restored"),
    ("restore", "permission:restore-root-intent"),
    ("restore", "permission:restore-root:action"),
    ("restore", "permission:restored"),
]

PERSISTENT_ROTATION_CRASH_WINDOWS = (
    "previous-to-retired",
    "marker-to-previous",
    "staging-to-marker",
)

HELD_SOURCE_SWAP_WINDOWS = (
    "previous-to-retired",
    "marker-to-previous",
)

REBUILD_DURABILITY_FAULTS = (
    "write",
    "file-fsync",
)


def test_permission_takeover_resumes_every_marker_and_action_phase(
    tmp_path: Path,
    operation: str,
    crash_at: str,
) -> None:
    root, source_sha, source_tree = repository(tmp_path)
    original = make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    crashed = False

    def checkpoint(label: str) -> None:
        nonlocal crashed
        if label == crash_at and not crashed:
            crashed = True
            raise RuntimeError(f"injected crash: {label}")

    if operation == "restore":
        trust.takeover_repository_permissions(root, marker)
        hardened = trust.verify_repository_permission_takeover(root, marker)
        assert hardened["phase"] == "hardened"
        assert evidence(root, source_sha, source_tree)["policy"] == trust.POLICY_NAME
        with raises(RuntimeError, match="injected crash"):
            trust.restore_repository_permissions(
                root,
                marker,
                checkpoint=checkpoint,
            )
    else:
        with raises(RuntimeError, match="injected crash"):
            trust.takeover_repository_permissions(
                root,
                marker,
                checkpoint=checkpoint,
            )
        trust.takeover_repository_permissions(root, marker)
    assert crashed
    if operation == "takeover":
        hardened = trust.verify_repository_permission_takeover(root, marker)
        assert hardened["phase"] == "hardened"
        assert (root.stat().st_mode & 0o777) == 0o700
        assert (
            evidence(root, source_sha, source_tree)["policy"]
            == trust.POLICY_NAME
        )

    restored = trust.restore_repository_permissions(root, marker)
    assert restored["phase"] == "restored"
    for relative, mode in original.items():
        path = root if relative == "." else root / relative
        assert (path.lstat().st_mode & 0o777) == mode
    assert trust.restore_repository_permissions(root, marker) == restored
    with raises(
        trust.GitPermissionTakeoverError,
        match="cannot be silently reused",
    ):
        trust.takeover_repository_permissions(root, marker)


def test_permission_takeover_rejects_immutable_object_drift(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    trust.takeover_repository_permissions(root, marker)
    loose = next(
        path
        for path in (root / ".git/objects").glob("[0-9a-f][0-9a-f]/*")
        if path.is_file()
    )
    loose.chmod(0o600)
    loose.write_bytes(loose.read_bytes() + b"tamper")
    loose.chmod(0o400)
    with raises(
        trust.GitPermissionTakeoverError,
        match="content changed",
    ):
        trust.verify_repository_permission_takeover(root, marker)


def test_permission_takeover_rejects_explicit_pushurl_before_mutation(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    canonical_remote(root)
    git(
        root,
        "config",
        "remote.origin.pushurl",
        "https://github.com/lzq390/ZhijuPoly.git",
    )
    original = make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)

    with raises(
        trust.GitPermissionTakeoverError,
        match="redirect policy",
    ):
        trust.takeover_repository_permissions(root, marker)

    assert not marker.exists()
    assert not marker.is_symlink()
    assert git(root, "config", "--get-all", "remote.origin.pushurl") == (
        "https://github.com/lzq390/ZhijuPoly.git"
    )
    for relative, mode in original.items():
        path = root if relative == "." else root / relative
        assert (path.lstat().st_mode & 0o777) == mode


def test_permission_plan_is_zero_write_and_first_marker_is_inventory_cas(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    original = make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)

    before = {
        relative: (
            (root if relative == "." else root / relative).lstat().st_mode
            & 0o777
        )
        for relative in original
    }
    first = trust.plan_repository_permission_takeover(root, marker)
    second = trust.plan_repository_permission_takeover(root, marker)

    assert first == second
    assert first["phase"] == "captured"
    assert first["generation"] == 1
    assert not marker.exists()
    assert {
        relative: (
            (root if relative == "." else root / relative).lstat().st_mode
            & 0o777
        )
        for relative in original
    } == before

    with raises(
        trust.GitPermissionTakeoverError,
        match="inventory_sha256 changed before mutation",
    ):
        trust.takeover_repository_permissions(
            root,
            marker,
            expected_inventory_sha256="sha256:" + "0" * 64,
            expected_original_permissions_sha256=first[
                "original_permissions_sha256"
            ],
            expected_hardened_permissions_sha256=first[
                "hardened_permissions_sha256"
            ],
        )
    assert not marker.exists()
    assert (root.stat().st_mode & 0o777) == before["."]

    hardened = trust.takeover_repository_permissions(
        root,
        marker,
        expected_inventory_sha256=first["inventory_sha256"],
        expected_original_permissions_sha256=first[
            "original_permissions_sha256"
        ],
        expected_hardened_permissions_sha256=first[
            "hardened_permissions_sha256"
        ],
    )
    assert hardened["phase"] == "hardened"
    assert hardened["inventory_sha256"] == first["inventory_sha256"]
    with raises(
        trust.GitPermissionTakeoverError,
        match="inventory_sha256 changed before mutation",
    ):
        trust.takeover_repository_permissions(
            root,
            marker,
            expected_inventory_sha256="sha256:" + "1" * 64,
            expected_original_permissions_sha256=first[
                "original_permissions_sha256"
            ],
            expected_hardened_permissions_sha256=first[
                "hardened_permissions_sha256"
            ],
        )
    assert (
        trust.verify_repository_permission_takeover(root, marker)
        == hardened
    )


def test_permission_marker_first_publish_target_appeared_is_not_overwritten(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    original_modes = make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    staging = marker.with_name(f".{marker.name}.staging")
    previous = marker.with_name(f".{marker.name}.previous")
    foreign_payload = b'{"foreign":"first-target"}\n'
    original_rename = trust._permission_rename_noreplace
    appeared = False

    def target_appears_before_publish(
        directory_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        nonlocal appeared
        if (
            source_name == staging.name
            and target_name == marker.name
            and not appeared
        ):
            descriptor = os.open(
                marker.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                assert os.write(descriptor, foreign_payload) == len(
                    foreign_payload
                )
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(directory_fd)
            appeared = True
        original_rename(directory_fd, source_name, target_name)

    trust._permission_rename_noreplace = target_appears_before_publish
    try:
        with raises(
            trust.GitPermissionTakeoverError,
            match="cannot persist permission takeover marker",
        ):
            trust.takeover_repository_permissions(root, marker)
    finally:
        trust._permission_rename_noreplace = original_rename

    assert appeared
    assert marker.read_bytes() == foreign_payload
    assert staging.exists()
    assert (staging.stat().st_mode & 0o777) == 0o600
    assert not previous.exists()
    for relative, mode in original_modes.items():
        path = root if relative == "." else root / relative
        assert (path.lstat().st_mode & 0o777) == mode

    marker.unlink()
    hardened = trust.takeover_repository_permissions(root, marker)
    assert hardened["phase"] == "hardened"
    assert marker.exists()
    assert not staging.exists()
    assert trust._read_permission_marker(previous)["phase"] == (
        "metadata-files-hardened"
    )


def test_permission_marker_later_publish_target_swap_is_not_moved(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    original_modes = make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    staging = marker.with_name(f".{marker.name}.staging")
    previous = marker.with_name(f".{marker.name}.previous")
    rogue = marker.with_name(f".{marker.name}.foreign-target")
    foreign_payload = b'{"foreign":"later-target"}\n'
    rogue.write_bytes(foreign_payload)
    rogue.chmod(0o600)
    rogue_inode = rogue.stat().st_ino
    original_rename = trust._permission_rename_noreplace
    swapped = False

    def swap_after_later_publish(
        directory_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        nonlocal swapped
        original_rename(directory_fd, source_name, target_name)
        if (
            source_name == staging.name
            and target_name == marker.name
            and trust._permission_entry_exists_at(
                directory_fd, previous.name
            )
            and not swapped
        ):
            os.replace(
                rogue.name,
                marker.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
            swapped = True

    trust._permission_rename_noreplace = swap_after_later_publish
    try:
        with raises(
            trust.GitPermissionTakeoverError,
            match="generation publication identity raced",
        ):
            trust.takeover_repository_permissions(root, marker)
    finally:
        trust._permission_rename_noreplace = original_rename

    assert swapped
    assert not rogue.exists()
    assert not staging.exists()
    assert marker.read_bytes() == foreign_payload
    assert marker.stat().st_ino == rogue_inode
    assert trust._read_permission_marker(previous)["phase"] == "captured"
    for relative, mode in original_modes.items():
        path = root if relative == "." else root / relative
        assert (path.lstat().st_mode & 0o777) == mode

    # Clearing the foreign target leaves one strictly validated predecessor.
    # Reconciliation restores it with NOREPLACE before retrying the transition.
    marker.unlink()
    hardened = trust.takeover_repository_permissions(root, marker)
    assert hardened["phase"] == "hardened"
    assert trust.verify_repository_permission_takeover(root, marker) == hardened
    assert not staging.exists()
    assert trust._read_permission_marker(previous)["phase"] == (
        "metadata-files-hardened"
    )


def test_permission_marker_replays_crash_after_old_moves_to_previous(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    planned = trust.plan_repository_permission_takeover(root, marker)
    staging = marker.with_name(f".{marker.name}.staging")
    previous = marker.with_name(f".{marker.name}.previous")
    original_rename = trust._permission_rename_noreplace
    crashed = False

    def crash_after_old_move(
        directory_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        nonlocal crashed
        original_rename(directory_fd, source_name, target_name)
        if (
            source_name == marker.name
            and target_name == previous.name
            and not crashed
        ):
            os.fsync(directory_fd)
            crashed = True
            raise RuntimeError("injected old-to-previous crash")

    trust._permission_rename_noreplace = crash_after_old_move
    try:
        with raises(RuntimeError, match="old-to-previous crash"):
            trust.takeover_repository_permissions(root, marker)
    finally:
        trust._permission_rename_noreplace = original_rename

    assert crashed
    assert not marker.exists()
    assert previous.exists()
    assert staging.exists()
    assert trust.plan_repository_permission_takeover(root, marker) == planned

    hardened = trust.takeover_repository_permissions(root, marker)
    assert hardened["phase"] == "hardened"
    assert trust._read_permission_marker(previous)["phase"] == (
        "metadata-files-hardened"
    )
    assert not staging.exists()


def test_permission_marker_replays_persistent_rotation_crash(
    tmp_path: Path,
    crash_window: str,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    staging = marker.with_name(f".{marker.name}.staging")
    previous = marker.with_name(f".{marker.name}.previous")
    retired_prefix = trust._permission_retired_prefix(marker)
    original_rename = trust._permission_rename_noreplace
    crashed = False

    def rename_then_crash(
        directory_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        nonlocal crashed
        original_rename(directory_fd, source_name, target_name)
        has_retired = any(
            name.startswith(retired_prefix)
            for name in os.listdir(directory_fd)
        )
        matches = {
            "previous-to-retired": (
                source_name == previous.name
                and target_name.startswith(retired_prefix)
            ),
            "marker-to-previous": (
                source_name == marker.name
                and target_name == previous.name
                and has_retired
            ),
            "staging-to-marker": (
                source_name == staging.name
                and target_name == marker.name
                and has_retired
            ),
        }
        if matches[crash_window] and not crashed:
            os.fsync(directory_fd)
            crashed = True
            raise RuntimeError(f"injected {crash_window} crash")

    trust._permission_rename_noreplace = rename_then_crash
    try:
        with raises(RuntimeError, match=re.escape(crash_window)):
            trust.takeover_repository_permissions(root, marker)
    finally:
        trust._permission_rename_noreplace = original_rename

    assert crashed
    assert len(permission_retired_paths(marker)) == 1
    hardened = trust.takeover_repository_permissions(root, marker)
    assert hardened["phase"] == "hardened"
    assert trust.verify_repository_permission_takeover(root, marker) == hardened
    assert len(permission_retired_paths(marker)) == 6


def test_permission_marker_full_lifecycle_never_unlinks_retired_history(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    original_modes = make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    previous = marker.with_name(f".{marker.name}.previous")
    retired_unlinked = False
    original_unlink = trust.os.unlink

    def reject_retired_unlink(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal retired_unlinked
        if os.fspath(path).startswith(
            trust._permission_retired_prefix(marker)
        ):
            retired_unlinked = True
            raise AssertionError("retired authority must never be unlinked")
        return original_unlink(path, *args, **kwargs)

    trust.os.unlink = reject_retired_unlink
    try:
        hardened = trust.takeover_repository_permissions(root, marker)
        restored = trust.restore_repository_permissions(root, marker)
    finally:
        trust.os.unlink = original_unlink

    assert hardened["phase"] == "hardened"
    assert restored["phase"] == "restored"
    assert not retired_unlinked
    retired = permission_retired_paths(marker)
    assert len(retired) == len(trust.PERMISSION_LIFECYCLE_PHASE_SEQUENCE) - 2
    assert trust._read_permission_marker(previous)["phase"] == (
        "restore-root-intent"
    )
    for relative, mode in original_modes.items():
        path = root if relative == "." else root / relative
        assert (path.lstat().st_mode & 0o777) == mode


def test_permission_marker_oldest_retired_deletion_breaks_anchor(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    hardened = trust.takeover_repository_permissions(root, marker)
    retired = permission_retired_paths(marker)
    assert len(retired) == 6

    retired[0].unlink()
    with raises(
        trust.GitPermissionTakeoverError,
        match="retired history has no valid anchor",
    ):
        trust.takeover_repository_permissions(root, marker)

    assert trust._read_permission_marker(marker) == hardened
    assert len(permission_retired_paths(marker)) == 5


def test_permission_marker_foreign_retired_target_is_not_overwritten(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    staging = marker.with_name(f".{marker.name}.staging")
    previous = marker.with_name(f".{marker.name}.previous")
    foreign_payload = b'{"foreign":"retired-target"}\n'
    original_rename = trust._permission_rename_noreplace
    appeared = False
    foreign_name: str | None = None

    def retired_target_appears(
        directory_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        nonlocal appeared, foreign_name
        if (
            source_name == previous.name
            and target_name.startswith(
                trust._permission_retired_prefix(marker)
            )
            and not appeared
        ):
            descriptor = os.open(
                target_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                assert os.write(descriptor, foreign_payload) == len(
                    foreign_payload
                )
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(directory_fd)
            appeared = True
            foreign_name = target_name
        original_rename(directory_fd, source_name, target_name)

    trust._permission_rename_noreplace = retired_target_appears
    try:
        with raises(
            trust.GitPermissionTakeoverError,
            match="cannot persist permission takeover marker",
        ):
            trust.takeover_repository_permissions(root, marker)
    finally:
        trust._permission_rename_noreplace = original_rename

    assert appeared and foreign_name is not None
    foreign = marker.parent / foreign_name
    assert foreign.read_bytes() == foreign_payload
    assert trust._read_permission_marker(marker)["phase"] == "root-intent"
    assert trust._read_permission_marker(previous)["phase"] == "captured"
    assert staging.exists()

    foreign.unlink()
    hardened = trust.takeover_repository_permissions(root, marker)
    assert hardened["phase"] == "hardened"
    assert trust.verify_repository_permission_takeover(root, marker) == hardened
    assert trust._read_permission_marker(previous)["phase"] == (
        "metadata-files-hardened"
    )
    assert len(permission_retired_paths(marker)) == 6


def test_permission_marker_previous_swap_after_retirement_fails_closed(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    staging = marker.with_name(f".{marker.name}.staging")
    previous = marker.with_name(f".{marker.name}.previous")
    foreign_payload = b'{"foreign":"previous-slot"}\n'
    original_rename = trust._permission_rename_noreplace
    swapped = False

    def occupy_previous_after_retirement(
        directory_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        nonlocal swapped
        original_rename(directory_fd, source_name, target_name)
        if (
            source_name == previous.name
            and target_name.startswith(
                trust._permission_retired_prefix(marker)
            )
            and not swapped
        ):
            descriptor = os.open(
                previous.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                assert os.write(descriptor, foreign_payload) == len(
                    foreign_payload
                )
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(directory_fd)
            swapped = True

    trust._permission_rename_noreplace = occupy_previous_after_retirement
    try:
        with raises(
            trust.GitPermissionTakeoverError,
            match="cannot persist permission takeover marker",
        ):
            trust.takeover_repository_permissions(root, marker)
    finally:
        trust._permission_rename_noreplace = original_rename

    assert swapped
    assert previous.read_bytes() == foreign_payload
    assert trust._read_permission_marker(marker)["phase"] == "root-intent"
    retired = permission_retired_paths(marker)
    assert len(retired) == 1
    assert trust._read_permission_marker(retired[0])["phase"] == "captured"
    assert staging.exists()

    previous.unlink()
    hardened = trust.takeover_repository_permissions(root, marker)
    assert hardened["phase"] == "hardened"
    assert trust.verify_repository_permission_takeover(root, marker) == hardened
    assert trust._read_permission_marker(previous)["phase"] == (
        "metadata-files-hardened"
    )
    assert len(permission_retired_paths(marker)) == 6


def test_permission_marker_rebuilds_after_held_source_swap(
    tmp_path: Path,
    swap_window: str,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    previous = marker.with_name(f".{marker.name}.previous")
    retired_prefix = trust._permission_retired_prefix(marker)
    rogue = marker.with_name(f".{marker.name}.{swap_window}-foreign")
    foreign_payload = f'{{"foreign":"{swap_window}"}}\n'.encode()
    rogue.write_bytes(foreign_payload)
    rogue.chmod(0o600)
    original_rename = trust._permission_rename_noreplace
    swapped = False
    foreign_target: str | None = None

    def swap_source_after_held_check(
        directory_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        nonlocal swapped, foreign_target
        has_retired = any(
            name.startswith(retired_prefix)
            for name in os.listdir(directory_fd)
        )
        matches = {
            "previous-to-retired": (
                source_name == previous.name
                and target_name.startswith(retired_prefix)
            ),
            "marker-to-previous": (
                source_name == marker.name
                and target_name == previous.name
                and has_retired
            ),
        }
        if matches[swap_window] and not swapped:
            os.replace(
                rogue.name,
                source_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
            swapped = True
            foreign_target = target_name
        original_rename(directory_fd, source_name, target_name)

    trust._permission_rename_noreplace = swap_source_after_held_check
    try:
        with raises(
            trust.GitPermissionTakeoverError,
            match="raced during rotation|target raced",
        ):
            trust.takeover_repository_permissions(root, marker)
    finally:
        trust._permission_rename_noreplace = original_rename

    assert swapped and foreign_target is not None
    foreign = marker.parent / foreign_target
    assert foreign.read_bytes() == foreign_payload
    assert (root.stat().st_mode & 0o777) == 0o700
    foreign.unlink()

    hardened = trust.takeover_repository_permissions(root, marker)
    assert hardened["phase"] == "hardened"
    assert trust.verify_repository_permission_takeover(root, marker) == hardened
    assert len(permission_retired_paths(marker)) == 6


def test_permission_marker_rebuilds_after_restore_source_swap(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    previous = marker.with_name(f".{marker.name}.previous")
    trust.takeover_repository_permissions(root, marker)
    marker.unlink()
    rogue = marker.with_name(f".{marker.name}.restore-source-foreign")
    foreign_payload = b'{"foreign":"restore-source"}\n'
    rogue.write_bytes(foreign_payload)
    rogue.chmod(0o600)
    original_rename = trust._permission_rename_noreplace
    swapped = False

    def swap_previous_before_restore(
        directory_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        nonlocal swapped
        if (
            source_name == previous.name
            and target_name == marker.name
            and not swapped
        ):
            os.replace(
                rogue.name,
                source_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
            swapped = True
        original_rename(directory_fd, source_name, target_name)

    trust._permission_rename_noreplace = swap_previous_before_restore
    try:
        with raises(
            trust.GitPermissionTakeoverError,
            match="previous restore raced",
        ):
            trust.takeover_repository_permissions(root, marker)
    finally:
        trust._permission_rename_noreplace = original_rename

    assert swapped
    assert marker.read_bytes() == foreign_payload
    assert not previous.exists()
    assert permission_retired_paths(marker)
    marker.unlink()

    hardened = trust.takeover_repository_permissions(root, marker)
    assert hardened["phase"] == "hardened"
    assert trust.verify_repository_permission_takeover(root, marker) == hardened


def test_marker_only_hardened_restore_rebuilds_after_marker_source_swap(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    original_modes = make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    previous = marker.with_name(f".{marker.name}.previous")
    staging = marker.with_name(f".{marker.name}.staging")
    hardened = trust.takeover_repository_permissions(root, marker)
    assert hardened["phase"] == "hardened"
    assert hardened["generation"] == 8

    # Reproduce the authority layout written by the legacy implementation:
    # one hardened generation and no predecessor journal.
    previous.unlink()
    for retired in permission_retired_paths(marker):
        retired.unlink()

    rogue = marker.with_name(f".{marker.name}.legacy-restore-foreign")
    foreign_payload = b'{"foreign":"legacy-restore-marker-source"}\n'
    rogue.write_bytes(foreign_payload)
    rogue.chmod(0o600)
    original_rename = trust._permission_rename_noreplace
    swapped = False

    def swap_marker_after_held_check(
        directory_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        nonlocal swapped
        if (
            source_name == marker.name
            and target_name == previous.name
            and not swapped
        ):
            os.replace(
                rogue.name,
                source_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
            swapped = True
        original_rename(directory_fd, source_name, target_name)

    trust._permission_rename_noreplace = swap_marker_after_held_check
    try:
        with raises(
            trust.GitPermissionTakeoverError,
            match="target raced during predecessor save",
        ):
            trust.restore_repository_permissions(root, marker)
    finally:
        trust._permission_rename_noreplace = original_rename

    assert swapped
    assert not marker.exists()
    assert previous.read_bytes() == foreign_payload
    staged = trust._read_permission_marker(staging)
    assert staged["phase"] == "restore-files-intent"
    assert staged["generation"] == 9

    # Once the foreign pathname owner is removed, the staged successor is the
    # surviving canonical authority.  It must reconstruct hardened/g8 rather
    # than being classified as stale and deleted.
    previous.unlink()
    restored = trust.restore_repository_permissions(root, marker)
    assert restored["phase"] == "restored"
    assert restored["generation"] == len(
        trust.PERMISSION_LIFECYCLE_PHASE_SEQUENCE
    )
    for relative, mode in original_modes.items():
        path = root if relative == "." else root / relative
        assert (path.lstat().st_mode & 0o777) == mode

    history = [
        trust._read_permission_marker(path)
        for path in permission_retired_paths(marker)
    ]
    history.extend(
        [trust._read_permission_marker(previous), restored]
    )
    assert history[0]["phase"] == "hardened"
    assert history[0]["generation"] == 8
    for predecessor, successor in zip(history, history[1:]):
        trust._validate_permission_generation_pair(predecessor, successor)


def test_permission_marker_replays_after_staging_source_swap(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    previous = marker.with_name(f".{marker.name}.previous")
    staging = marker.with_name(f".{marker.name}.staging")
    original_rename = trust._permission_rename_noreplace
    interrupted = False

    def interrupt_after_marker_save(
        directory_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        nonlocal interrupted
        original_rename(directory_fd, source_name, target_name)
        if (
            source_name == marker.name
            and target_name == previous.name
            and not interrupted
        ):
            interrupted = True
            raise RuntimeError("injected marker-save crash")

    trust._permission_rename_noreplace = interrupt_after_marker_save
    try:
        with raises(RuntimeError, match="marker-save crash"):
            trust.takeover_repository_permissions(root, marker)
    finally:
        trust._permission_rename_noreplace = original_rename

    assert interrupted
    assert not marker.exists()
    assert trust._read_permission_marker(previous)["phase"] == "captured"
    assert trust._read_permission_marker(staging)["phase"] == "root-intent"

    rogue = marker.with_name(f".{marker.name}.staging-source-foreign")
    foreign_payload = b'{"foreign":"staging-source"}\n'
    rogue.write_bytes(foreign_payload)
    rogue.chmod(0o600)
    swapped = False

    def swap_staging_after_held_check(
        directory_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        nonlocal swapped
        if (
            source_name == staging.name
            and target_name == marker.name
            and not swapped
        ):
            os.replace(
                rogue.name,
                source_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
            swapped = True
        original_rename(directory_fd, source_name, target_name)

    trust._permission_rename_noreplace = swap_staging_after_held_check
    try:
        with raises(
            trust.GitPermissionTakeoverError,
            match="replay publication raced",
        ):
            trust.takeover_repository_permissions(root, marker)
    finally:
        trust._permission_rename_noreplace = original_rename

    assert swapped
    assert marker.read_bytes() == foreign_payload
    assert trust._read_permission_marker(previous)["phase"] == "captured"
    assert not staging.exists()

    marker.unlink()
    hardened = trust.takeover_repository_permissions(root, marker)
    assert hardened["phase"] == "hardened"
    assert trust.verify_repository_permission_takeover(root, marker) == hardened


def test_permission_marker_replays_after_retired_and_rebuild_source_swaps(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    previous = marker.with_name(f".{marker.name}.previous")
    hardened = trust.takeover_repository_permissions(root, marker)
    assert hardened["phase"] == "hardened"
    assert trust._read_permission_marker(previous)["generation"] == 7

    # Reconcile a crash after P(g7)->M but before R(g6)->P.  Replacing the
    # retired source after its held-path check must fail closed and leave the
    # older, still-contiguous retired prefix untouched.
    marker.unlink()
    retired = permission_retired_paths(marker)
    latest_retired = retired[-1]
    assert trust._read_permission_marker(latest_retired)["generation"] == 6
    retired_rogue = marker.with_name(f".{marker.name}.retired-source-foreign")
    retired_foreign_payload = b'{"foreign":"retired-source"}\n'
    retired_rogue.write_bytes(retired_foreign_payload)
    retired_rogue.chmod(0o600)
    original_rename = trust._permission_rename_noreplace
    retired_swapped = False

    def swap_retired_after_held_check(
        directory_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        nonlocal retired_swapped
        if (
            source_name == latest_retired.name
            and target_name == previous.name
            and not retired_swapped
        ):
            os.replace(
                retired_rogue.name,
                source_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
            retired_swapped = True
        original_rename(directory_fd, source_name, target_name)

    trust._permission_rename_noreplace = swap_retired_after_held_check
    try:
        with raises(
            trust.GitPermissionTakeoverError,
            match="retired restore raced",
        ):
            trust.takeover_repository_permissions(root, marker)
    finally:
        trust._permission_rename_noreplace = original_rename

    assert retired_swapped
    assert trust._read_permission_marker(marker)["generation"] == 7
    assert previous.read_bytes() == retired_foreign_payload
    assert [
        trust._read_permission_marker(path)["generation"]
        for path in permission_retired_paths(marker)
    ] == [1, 2, 3, 4, 5]

    # With the foreign P removed, g6 is exactly derivable.  Race the
    # content-addressed rebuild source too; the retry must recreate it without
    # deleting or rewriting any surviving canonical generation.
    previous.unlink()
    rebuild_rogue = marker.with_name(f".{marker.name}.rebuild-source-foreign")
    rebuild_foreign_payload = b'{"foreign":"rebuild-source"}\n'
    rebuild_rogue.write_bytes(rebuild_foreign_payload)
    rebuild_rogue.chmod(0o600)
    rebuild_swapped = False

    def swap_rebuild_after_held_check(
        directory_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        nonlocal rebuild_swapped
        if (
            source_name.startswith(f".{marker.name}.rebuild-g")
            and target_name == previous.name
            and not rebuild_swapped
        ):
            os.replace(
                rebuild_rogue.name,
                source_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
            rebuild_swapped = True
        original_rename(directory_fd, source_name, target_name)

    trust._permission_rename_noreplace = swap_rebuild_after_held_check
    try:
        with raises(
            trust.GitPermissionTakeoverError,
            match="rebuild publication raced",
        ):
            trust.takeover_repository_permissions(root, marker)
    finally:
        trust._permission_rename_noreplace = original_rename

    assert rebuild_swapped
    assert previous.read_bytes() == rebuild_foreign_payload
    assert trust._read_permission_marker(marker)["generation"] == 7
    assert [
        trust._read_permission_marker(path)["generation"]
        for path in permission_retired_paths(marker)
    ] == [1, 2, 3, 4, 5]

    previous.unlink()
    hardened = trust.takeover_repository_permissions(root, marker)
    assert hardened["phase"] == "hardened"
    assert hardened["generation"] == 8
    assert trust.verify_repository_permission_takeover(root, marker) == hardened
    assert [
        trust._read_permission_marker(path)["generation"]
        for path in permission_retired_paths(marker)
    ] == [1, 2, 3, 4, 5, 6]


def test_permission_rebuild_existing_lost_response_replays_after_power_loss(
    tmp_path: Path,
    fault_point: str,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    previous = marker.with_name(f".{marker.name}.previous")
    hardened = trust.takeover_repository_permissions(root, marker)
    assert hardened["phase"] == "hardened"

    # Leave M(g7), no P, and retired g1..g5. Reconciliation must rebuild the
    # uniquely derivable g6 predecessor into P.
    marker.unlink()
    previous.replace(marker)
    retired = permission_retired_paths(marker)
    latest = retired[-1]
    rebuild_document = trust._read_permission_marker(latest)
    assert rebuild_document["generation"] == 6
    latest.unlink()
    assert trust._read_permission_marker(marker)["generation"] == 7
    assert not previous.exists()

    rebuild_payload = trust.canonical_json_bytes(rebuild_document) + b"\n"
    rebuild_name = trust._permission_rebuild_name(
        marker,
        rebuild_document,
        rebuild_payload,
    )
    rebuild = marker.with_name(rebuild_name)
    durable_write = trust.os.write
    durable_fsync = trust.os.fsync
    first_faulted = False

    def write_lost_response(
        descriptor: int,
        payload: bytes,
    ) -> int:
        nonlocal first_faulted
        written = durable_write(descriptor, payload)
        if fault_point == "write" and not first_faulted and rebuild.exists():
            opened = os.fstat(descriptor)
            observed = rebuild.stat(follow_symlinks=False)
            if (
                (opened.st_dev, opened.st_ino)
                == (observed.st_dev, observed.st_ino)
                and rebuild.read_bytes() == rebuild_payload
            ):
                first_faulted = True
                raise RuntimeError("rebuild write response lost")
        return written

    def file_fsync_lost_response(descriptor: int) -> None:
        nonlocal first_faulted
        durable_fsync(descriptor)
        if fault_point != "file-fsync" or first_faulted or not rebuild.exists():
            return
        opened = os.fstat(descriptor)
        observed = rebuild.stat(follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) == (observed.st_dev, observed.st_ino):
            first_faulted = True
            raise RuntimeError("rebuild file fsync response lost")

    if fault_point == "write":
        trust.os.write = write_lost_response
    else:
        trust.os.fsync = file_fsync_lost_response
    try:
        with raises(RuntimeError, match="rebuild .* response lost"):
            trust.takeover_repository_permissions(root, marker)
    finally:
        trust.os.write = durable_write
        trust.os.fsync = durable_fsync

    assert first_faulted
    assert rebuild.read_bytes() == rebuild_payload
    assert (rebuild.stat().st_mode & 0o777) == 0o600
    assert not previous.exists()

    rebuild_identity = (
        rebuild.stat().st_dev,
        rebuild.stat().st_ino,
    )
    marker_parent_identity = (
        marker.parent.stat().st_dev,
        marker.parent.stat().st_ino,
    )
    durable_rename = trust._permission_rename_noreplace
    rebuild_resealed = False
    published = False

    def track_rebuild_fsync(descriptor: int) -> None:
        nonlocal rebuild_resealed
        metadata = os.fstat(descriptor)
        if published and (
            metadata.st_dev,
            metadata.st_ino,
        ) == marker_parent_identity:
            raise RuntimeError("second power loss after rebuild publish")
        durable_fsync(descriptor)
        if (metadata.st_dev, metadata.st_ino) == rebuild_identity:
            rebuild_resealed = True

    def publish_then_crash(
        directory_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        nonlocal published
        if source_name == rebuild.name and target_name == previous.name:
            assert rebuild_resealed
            durable_rename(directory_fd, source_name, target_name)
            published = True
            raise RuntimeError("second power loss after rebuild publish")
        durable_rename(directory_fd, source_name, target_name)

    trust.os.fsync = track_rebuild_fsync
    trust._permission_rename_noreplace = publish_then_crash
    try:
        with raises(RuntimeError, match="second power loss"):
            trust.takeover_repository_permissions(root, marker)
    finally:
        trust.os.fsync = durable_fsync
        trust._permission_rename_noreplace = durable_rename

    assert rebuild_resealed
    assert published
    assert not rebuild.exists()
    assert previous.read_bytes() == rebuild_payload

    # Model the second power loss selecting the pre-rename namespace. The
    # next retry must re-seal the same deterministic inode and publish safely.
    previous.replace(rebuild)
    recovered = trust.takeover_repository_permissions(root, marker)
    assert recovered["phase"] == "hardened"
    assert recovered["generation"] == 8
    assert not rebuild.exists()
    assert trust.verify_repository_permission_takeover(root, marker) == recovered


def test_permission_rebuild_partial_preplant_fails_closed_without_mutation(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    previous = marker.with_name(f".{marker.name}.previous")
    hardened = trust.takeover_repository_permissions(root, marker)
    assert hardened["phase"] == "hardened"

    marker.unlink()
    previous.replace(marker)
    latest = permission_retired_paths(marker)[-1]
    rebuild_document = trust._read_permission_marker(latest)
    assert rebuild_document["generation"] == 6
    latest.unlink()
    payload = trust.canonical_json_bytes(rebuild_document) + b"\n"
    rebuild = marker.with_name(
        trust._permission_rebuild_name(marker, rebuild_document, payload)
    )
    partial = payload[: max(1, len(payload) // 3)]
    rebuild.write_bytes(partial)
    rebuild.chmod(0o600)
    identity = (rebuild.lstat().st_dev, rebuild.lstat().st_ino)

    with raises(
        trust.GitPermissionTakeoverError,
        match="rebuild staging differs",
    ):
        trust.takeover_repository_permissions(root, marker)

    assert (rebuild.lstat().st_dev, rebuild.lstat().st_ino) == identity
    assert rebuild.read_bytes() == partial
    assert trust._read_permission_marker(marker)["generation"] == 7
    assert not previous.exists()


def test_permission_marker_quarantine_replays_rename_crash(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    original_modes = make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    staging = marker.with_name(f".{marker.name}.staging")
    quarantine = staging.with_name(f"{staging.name}.quarantine")
    staging.write_bytes(b"partial marker staging")
    staging.chmod(0o600)
    original_rename = trust._permission_rename_noreplace
    crashed = False

    def rename_then_crash(
        directory_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        nonlocal crashed
        original_rename(directory_fd, source_name, target_name)
        if target_name == quarantine.name and not crashed:
            crashed = True
            raise RuntimeError("injected quarantine rename crash")

    trust._permission_rename_noreplace = rename_then_crash
    try:
        with raises(RuntimeError, match="quarantine rename crash"):
            trust.takeover_repository_permissions(root, marker)
    finally:
        trust._permission_rename_noreplace = original_rename

    assert crashed
    assert not marker.exists()
    assert not staging.exists()
    assert quarantine.exists()
    assert (quarantine.stat().st_mode & 0o777) == 0o600
    for relative, mode in original_modes.items():
        path = root if relative == "." else root / relative
        assert (path.lstat().st_mode & 0o777) == mode

    hardened = trust.takeover_repository_permissions(root, marker)
    assert hardened["phase"] == "hardened"
    assert marker.exists()
    assert not staging.exists()
    assert not quarantine.exists()


def test_permission_stable_chmod_reseals_inode_and_parent(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    planned = trust.plan_repository_permission_takeover(root, marker)
    record = next(
        value for value in planned["records"] if value["path"] == "."
    )
    expected = {
        (root.lstat().st_dev, root.lstat().st_ino),
        (root.parent.lstat().st_dev, root.parent.lstat().st_ino),
    }
    observed: set[tuple[int, int]] = set()
    original_fsync = trust.os.fsync

    def record_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        observed.add((metadata.st_dev, metadata.st_ino))
        original_fsync(descriptor)

    trust.os.fsync = record_fsync
    try:
        changed = trust._chmod_permission_record(
            root,
            record,
            desired=record["mode"],
            alternate=record["target_mode"],
            allow_mutable_changes=False,
            require_original_config=False,
        )
    finally:
        trust.os.fsync = original_fsync

    assert changed is False
    assert expected.issubset(observed)


def test_permission_stable_staging_and_absence_reseal_namespace(
    tmp_path: Path,
) -> None:
    _runtime, marker = permission_marker(tmp_path)
    staging_name = f".{marker.name}.staging"
    quarantine_name = f"{staging_name}.quarantine"
    payload = b"stable marker staging\n"
    staging = marker.parent / staging_name
    staging.write_bytes(payload)
    staging.chmod(0o600)
    directory_fd = os.open(
        marker.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    expected = {
        (staging.lstat().st_dev, staging.lstat().st_ino),
        (marker.parent.lstat().st_dev, marker.parent.lstat().st_ino),
    }
    observed: set[tuple[int, int]] = set()
    original_fsync = trust.os.fsync

    def record_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        observed.add((metadata.st_dev, metadata.st_ino))
        original_fsync(descriptor)

    trust.os.fsync = record_fsync
    try:
        trust._prepare_permission_staging_at(
            directory_fd,
            staging_name,
            quarantine_name,
            payload,
        )
        staging.unlink()
        trust._remove_permission_staging_at(
            directory_fd,
            staging_name,
            quarantine_name,
        )
    finally:
        trust.os.fsync = original_fsync
        os.close(directory_fd)

    assert expected.issubset(observed)


def test_permission_stable_staging_rejects_path_swap_after_file_fsync(
    tmp_path: Path,
) -> None:
    _runtime, marker = permission_marker(tmp_path)
    staging_name = f".{marker.name}.staging"
    quarantine_name = f"{staging_name}.quarantine"
    staging = marker.parent / staging_name
    rogue = marker.parent / ".staging-rogue"
    payload = b"stable marker staging\n"
    rogue_payload = b"rogue marker staging\n"
    staging.write_bytes(payload)
    staging.chmod(0o600)
    rogue.write_bytes(rogue_payload)
    rogue.chmod(0o600)
    staging_identity = (staging.lstat().st_dev, staging.lstat().st_ino)
    directory_fd = os.open(
        marker.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    original_fsync = trust.os.fsync
    swapped = False

    def swap_after_file_fsync(descriptor: int) -> None:
        nonlocal swapped
        metadata = os.fstat(descriptor)
        original_fsync(descriptor)
        if (
            (metadata.st_dev, metadata.st_ino) == staging_identity
            and not swapped
        ):
            os.replace(
                rogue.name,
                staging_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            swapped = True

    trust.os.fsync = swap_after_file_fsync
    try:
        with raises(
            trust.GitPermissionTakeoverError,
            match="staging changed before fsync",
        ):
            trust._prepare_permission_staging_at(
                directory_fd,
                staging_name,
                quarantine_name,
                payload,
            )
    finally:
        trust.os.fsync = original_fsync
        os.close(directory_fd)

    assert swapped
    assert staging.read_bytes() == rogue_payload


def test_permission_stable_chmod_rejects_path_swap_after_inode_fsync(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    planned = trust.plan_repository_permission_takeover(root, marker)
    record = next(
        value
        for value in planned["records"]
        if value["path"] == ".git/config"
    )
    path = root / record["path"]
    displaced = tmp_path / "held-config-original"
    rogue = tmp_path / "held-config-rogue"
    rogue_payload = b"[foreign]\n\tvalue = true\n"
    rogue.write_bytes(rogue_payload)
    rogue.chmod(int(record["target_mode"], 8))
    held_identity = (path.lstat().st_dev, path.lstat().st_ino)
    original_fsync = trust.os.fsync
    swapped = False

    def swap_after_inode_fsync(descriptor: int) -> None:
        nonlocal swapped
        metadata = os.fstat(descriptor)
        original_fsync(descriptor)
        if (
            (metadata.st_dev, metadata.st_ino) == held_identity
            and not swapped
        ):
            os.rename(path, displaced)
            os.rename(rogue, path)
            swapped = True

    trust.os.fsync = swap_after_inode_fsync
    try:
        with raises(
            trust.GitPermissionTakeoverError,
            match="permission path changed while chmod was flushed",
        ):
            trust._chmod_permission_record(
                root,
                record,
                desired=record["target_mode"],
                alternate=record["mode"],
                allow_mutable_changes=False,
                require_original_config=False,
            )
    finally:
        trust.os.fsync = original_fsync

    assert swapped
    assert path.read_bytes() == rogue_payload
    assert (displaced.lstat().st_dev, displaced.lstat().st_ino) == held_identity


def test_permission_stable_marker_replay_reseals_directory(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    hardened = trust.takeover_repository_permissions(root, marker)
    expected_directory = (
        marker.parent.lstat().st_dev,
        marker.parent.lstat().st_ino,
    )
    observed: set[tuple[int, int]] = set()
    original_fsync = trust.os.fsync

    def record_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        observed.add((metadata.st_dev, metadata.st_ino))
        original_fsync(descriptor)

    trust.os.fsync = record_fsync
    try:
        replay = trust.takeover_repository_permissions(root, marker)
    finally:
        trust.os.fsync = original_fsync

    assert replay == hardened
    assert expected_directory in observed


def test_permission_plan_rejects_oversized_marker_before_write(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    original = make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    staging = marker.with_name(f".{marker.name}.staging")
    captured = trust.plan_repository_permission_takeover(root, marker)
    captured_bytes = len(trust.canonical_json_bytes(captured)) + 1
    original_limit = trust.PERMISSION_MARKER_MAX_BYTES
    trust.PERMISSION_MARKER_MAX_BYTES = captured_bytes
    try:
        with raises(
            trust.GitPermissionTakeoverError,
            match="marker lifecycle is oversized",
        ):
            trust.plan_repository_permission_takeover(root, marker)
    finally:
        trust.PERMISSION_MARKER_MAX_BYTES = original_limit

    assert not marker.exists()
    assert not staging.exists()
    for relative, mode in original.items():
        path = root if relative == "." else root / relative
        assert (path.lstat().st_mode & 0o777) == mode


def test_permission_history_total_limit_fails_before_marker_or_chmod(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    original_modes = make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    original_limit = trust.PERMISSION_HISTORY_MAX_BYTES
    trust.PERMISSION_HISTORY_MAX_BYTES = 1
    try:
        with raises(
            trust.GitPermissionTakeoverError,
            match="marker history is oversized",
        ):
            trust.takeover_repository_permissions(root, marker)
    finally:
        trust.PERMISSION_HISTORY_MAX_BYTES = original_limit

    assert not marker.exists()
    assert not marker.with_name(f".{marker.name}.staging").exists()
    for relative, mode in original_modes.items():
        path = root if relative == "." else root / relative
        assert (path.lstat().st_mode & 0o777) == mode


def test_permission_history_free_space_fails_before_marker_or_chmod(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    original_modes = make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    original_fstatvfs = trust.os.fstatvfs

    class NoFreeSpace:
        f_frsize = 4096
        f_bsize = 4096
        f_bavail = 0

    trust.os.fstatvfs = lambda _descriptor: NoFreeSpace()  # type: ignore[assignment]
    try:
        with raises(
            trust.GitPermissionTakeoverError,
            match="history lacks free space",
        ):
            trust.takeover_repository_permissions(root, marker)
    finally:
        trust.os.fstatvfs = original_fstatvfs

    assert not marker.exists()
    assert not marker.with_name(f".{marker.name}.staging").exists()
    for relative, mode in original_modes.items():
        path = root if relative == "." else root / relative
        assert (path.lstat().st_mode & 0o777) == mode


def test_permission_history_capacity_replay_does_not_count_staging_twice(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    staging = marker.with_name(f".{marker.name}.staging")
    planned = trust.plan_repository_permission_takeover(root, marker)
    original_rename = trust._permission_rename_noreplace
    crashed = False

    def crash_before_first_publish(
        directory_fd: int,
        source_name: str,
        target_name: str,
    ) -> None:
        nonlocal crashed
        if (
            source_name == staging.name
            and target_name == marker.name
            and not crashed
        ):
            crashed = True
            raise RuntimeError("injected staged-capacity crash")
        original_rename(directory_fd, source_name, target_name)

    trust._permission_rename_noreplace = crash_before_first_publish
    try:
        with raises(RuntimeError, match="staged-capacity crash"):
            trust.takeover_repository_permissions(root, marker)
    finally:
        trust._permission_rename_noreplace = original_rename

    assert crashed and staging.exists() and not marker.exists()
    base = dict(planned)
    base["generation"] = 0
    base.pop("evidence_sha256", None)
    candidate = dict(base)
    payloads: list[bytes] = []
    generation = 0
    for phase in trust.PERMISSION_LIFECYCLE_PHASE_SEQUENCE:
        generation += 1
        candidate["phase"] = phase
        candidate["generation"] = generation
        candidate["evidence_sha256"] = trust._permission_document_digest(
            candidate
        )
        payloads.append(trust.canonical_json_bytes(candidate) + b"\n")
    fragment_size = 4096
    future_allocated = sum(
        ((len(payload) + fragment_size - 1) // fragment_size)
        * fragment_size
        for payload in payloads
    )
    staged_allocated = min(
        staging.stat().st_blocks * 512,
        ((len(payloads[0]) + fragment_size - 1) // fragment_size)
        * fragment_size,
    )
    required = (
        future_allocated
        - staged_allocated
        + trust.PERMISSION_HISTORY_FREE_MARGIN_BYTES
    )
    available_blocks = (required + fragment_size - 1) // fragment_size
    original_fstatvfs = trust.os.fstatvfs

    class BoundarySpace:
        f_frsize = fragment_size
        f_bsize = fragment_size
        f_bavail = available_blocks

    trust.os.fstatvfs = lambda _descriptor: BoundarySpace()  # type: ignore[assignment]
    try:
        hardened = trust.takeover_repository_permissions(root, marker)
    finally:
        trust.os.fstatvfs = original_fstatvfs

    assert hardened["phase"] == "hardened"
    assert not staging.exists()


def test_permission_restore_rejects_future_oversized_generation_before_chmod(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    hardened = trust.takeover_repository_permissions(root, marker)
    hardened_modes = permission_record_modes(root, hardened["records"])
    previous = marker.with_name(f".{marker.name}.previous")
    staging = marker.with_name(f".{marker.name}.staging")
    quarantine = staging.with_name(f"{staging.name}.quarantine")
    retained_bytes = sum(
        path.stat().st_size
        for path in [
            marker,
            previous,
            *permission_retired_paths(marker),
        ]
    )
    original_limit = trust.PERMISSION_HISTORY_MAX_BYTES
    trust.PERMISSION_HISTORY_MAX_BYTES = retained_bytes
    try:
        with raises(
            trust.GitPermissionTakeoverError,
            match="marker history is oversized",
        ):
            trust.restore_repository_permissions(root, marker)
    finally:
        trust.PERMISSION_HISTORY_MAX_BYTES = original_limit

    observed = trust.read_repository_permission_takeover(root, marker)
    assert observed == hardened
    assert permission_record_modes(root, hardened["records"]) == hardened_modes
    assert not staging.exists()
    assert not quarantine.exists()


def _with_legacy_pushurl_policy(function: Callable[[], None]) -> None:
    original = trust.ALLOWED_CONFIG
    trust.ALLOWED_CONFIG = {
        **original,
        'remote "origin"': frozenset(
            {*original['remote "origin"'], "pushurl"}
        ),
    }
    try:
        function()
    finally:
        trust.ALLOWED_CONFIG = original


def test_existing_captured_marker_cannot_bypass_new_pushurl_policy(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    canonical_remote(root)
    git(
        root,
        "config",
        "remote.origin.pushurl",
        "https://github.com/lzq390/ZhijuPoly.git",
    )
    original = make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)

    def capture_with_legacy_policy() -> None:
        with raises(RuntimeError, match="legacy captured marker"):
            trust.takeover_repository_permissions(
                root,
                marker,
                checkpoint=lambda label: (
                    (_ for _ in ()).throw(
                        RuntimeError("legacy captured marker")
                    )
                    if label == "permission:captured"
                    else None
                ),
            )

    _with_legacy_pushurl_policy(capture_with_legacy_policy)
    assert marker.exists()
    before_retry = {
        relative: (
            root if relative == "." else root / relative
        ).lstat().st_mode
        & 0o777
        for relative in original
    }

    with raises(
        trust.GitPermissionTakeoverError,
        match="redirect policy",
    ):
        trust.takeover_repository_permissions(root, marker)

    assert {
        relative: (
            root if relative == "." else root / relative
        ).lstat().st_mode
        & 0o777
        for relative in original
    } == before_retry
    assert trust._load_permission_document(root, marker)["phase"] == "captured"


def test_existing_hardened_pushurl_marker_can_only_restore(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    canonical_remote(root)
    git(
        root,
        "config",
        "remote.origin.pushurl",
        "https://github.com/lzq390/ZhijuPoly.git",
    )
    original = make_git_authority_group_writable(root)
    _runtime, marker = permission_marker(tmp_path)
    _with_legacy_pushurl_policy(
        lambda: trust.takeover_repository_permissions(root, marker)
    )

    with raises(
        trust.GitPermissionTakeoverError,
        match="redirect policy",
    ):
        trust.takeover_repository_permissions(root, marker)

    restored = trust.restore_repository_permissions(root, marker)
    assert restored["phase"] == "restored"
    for relative, mode in original.items():
        path = root if relative == "." else root / relative
        assert (path.lstat().st_mode & 0o777) == mode


def test_evidence_binds_interpreted_config_index_refs_and_objects(
    tmp_path: Path,
) -> None:
    root, source_sha, source_tree = repository(tmp_path)
    before = evidence(root, source_sha, source_tree)

    assert before["policy"] == trust.POLICY_NAME
    assert before["source"] == {
        "sha": source_sha,
        "tree": source_tree,
        "branch": "refs/heads/main",
        "origin": None,
    }
    assert before["local_config"]["canonical"]
    assert before["index"]["external"] is False
    assert before["objects"]["standalone"] is True
    assert before["refs"]["replace_refs"] == 0
    assert before["execution_environment"]["ambient_redirects"] is False

    git(root, "config", "user.name", "Trust Test Changed")
    after = evidence(root, source_sha, source_tree)
    assert after["source"] == before["source"]
    assert after["local_config"]["raw_sha256"] != before["local_config"][
        "raw_sha256"
    ]
    assert after["evidence_sha256"] != before["evidence_sha256"]
    with raises(trust.GitSourceTrustError, match="trust surface"):
        trust.require_stable_trust_surface(before, after)


HOSTILE_AMBIENT_NAMES = [
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_COUNT",
    "GIT_REPLACE_REF_BASE",
    "GIT_TRACE",
    "GIT_SSH_COMMAND",
    "SSH_ASKPASS",
]


def test_hostile_ambient_git_redirects_are_rejected(
    tmp_path: Path,
    name: str,
) -> None:
    root, source_sha, source_tree = repository(tmp_path)
    with raises(
        trust.GitSourceTrustError,
        match="ambient Git control",
    ):
        trust.repository_trust_evidence(
            root,
            source_sha=source_sha,
            source_tree=source_tree,
            branch="refs/heads/main",
            origin=None,
            ambient={name: "/untrusted"},
        )


REDIRECT_CONFIG_CASES = [
    ("include", "path", "/tmp/evil"),
    ('includeIf "gitdir:/data/**"', "path", "/tmp/evil"),
    ("core", "worktree", "/tmp/evil"),
    ("core", "fsmonitor", "/tmp/evil"),
    ("core", "sparseCheckout", "true"),
    ("core", "hooksPath", "/tmp/evil"),
    ("extensions", "partialClone", "origin"),
    ('remote "origin"', "promisor", "true"),
    ("filter", "evil.clean", "/tmp/evil"),
]


def test_executable_redirect_and_partial_clone_config_is_rejected(
    tmp_path: Path,
    section: str,
    key: str,
    value: str,
) -> None:
    root, source_sha, source_tree = repository(tmp_path)
    with (root / ".git/config").open("a", encoding="utf-8") as stream:
        stream.write(f'\n[{section}]\n\t{key} = {value}\n')
    with raises(trust.GitSourceTrustError, match="config"):
        evidence(root, source_sha, source_tree)


FORBIDDEN_STORAGE_MARKERS = [
    "commondir",
    "shallow",
    "config.worktree",
    "info/grafts",
    "info/sparse-checkout",
    "objects/info/alternates",
    "objects/info/http-alternates",
    "refs/replace",
]


def test_external_storage_replace_grafts_shallow_and_sparse_are_rejected(
    tmp_path: Path,
    relative: str,
) -> None:
    root, source_sha, source_tree = repository(tmp_path)
    marker = root / ".git" / relative
    marker.parent.mkdir(parents=True, exist_ok=True)
    if relative == "refs/replace":
        marker.mkdir()
    else:
        marker.write_text("/tmp/untrusted\n", encoding="utf-8")
    with raises(
        trust.GitSourceTrustError,
        match="forbidden Git storage|policy marker",
    ):
        evidence(root, source_sha, source_tree)


def test_promisor_pack_shared_object_and_linked_worktree_are_rejected(
    tmp_path: Path,
) -> None:
    root, source_sha, source_tree = repository(tmp_path)
    promisor = root / ".git/objects/pack/fixture.promisor"
    promisor.parent.mkdir(parents=True, exist_ok=True)
    promisor.write_bytes(b"promisor")
    with raises(trust.GitSourceTrustError, match="promisor"):
        evidence(root, source_sha, source_tree)
    promisor.unlink()

    loose = next(
        path
        for path in (root / ".git/objects").glob("[0-9a-f][0-9a-f]/*")
        if path.is_file()
    )
    external = tmp_path / "shared-object"
    os.link(loose, external)
    with raises(trust.GitSourceTrustError, match="unsafe metadata"):
        evidence(root, source_sha, source_tree)
    external.unlink()

    linked = tmp_path / "linked"
    git(root, "worktree", "add", "--detach", str(linked), source_sha)
    with raises(
        trust.GitSourceTrustError,
        match="metadata directory",
    ):
        evidence(linked, source_sha, source_tree)


def test_external_index_fsmonitor_and_sparse_index_extensions_are_rejected(
    tmp_path: Path,
) -> None:
    root, source_sha, source_tree = repository(tmp_path)
    with raises(trust.GitSourceTrustError, match="ambient Git control"):
        trust.safe_git_environment(
            root,
            ambient={"GIT_INDEX_FILE": str(tmp_path / "index")},
        )

    index = root / ".git/index"
    original = index.read_bytes()
    # Add a syntactically placed forbidden extension before the trailing
    # object-format checksum. The checksum itself is irrelevant because the
    # trust parser must reject the extension before Git gets an opportunity to
    # interpret the index.
    index.write_bytes(
        original[:-20] + b"FSMN" + (0).to_bytes(4, "big") + original[-20:]
    )
    with raises(trust.GitSourceTrustError, match="FSMN"):
        evidence(root, source_sha, source_tree)


def test_safe_child_environment_does_not_inherit_loader_or_user_config(
    tmp_path: Path,
) -> None:
    root, _source_sha, _source_tree = repository(tmp_path)
    child = trust.safe_git_environment(
        root,
        ambient={
            "LD_PRELOAD": "/tmp/evil.so",
            "LD_LIBRARY_PATH": "/tmp/evil",
            "HOME": "/tmp/evil",
            "GIT_PAGER": "/tmp/evil",
        },
    )
    assert "LD_PRELOAD" not in child
    assert "LD_LIBRARY_PATH" not in child
    assert child["HOME"] == "/nonexistent"
    assert child["GIT_CONFIG_GLOBAL"] == os.devnull
    assert child["GIT_OBJECT_DIRECTORY"] == str(root / ".git/objects")
    assert child["GIT_INDEX_FILE"] == str(root / ".git/index")
    assert child["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert child["GIT_SSH_COMMAND"] == "/bin/false"
    with raises(trust.GitSourceTrustError, match="subcommand"):
        trust.safe_git_command(root, "-c", "include.path=/tmp/evil", "status")
    with raises(trust.GitSourceTrustError, match="executable"):
        trust.safe_git_command(root, "status", executable="git")
    assert trust.safe_git_command(root, "status")[0] == "/usr/bin/git"


def test_object_store_symlink_is_rejected(tmp_path: Path) -> None:
    root, source_sha, source_tree = repository(tmp_path)
    objects = root / ".git/objects"
    moved = root / ".git/objects-real"
    objects.rename(moved)
    objects.symlink_to(moved, target_is_directory=True)
    with raises(
        trust.GitSourceTrustError,
        match="object database",
    ):
        evidence(root, source_sha, source_tree)
    objects.unlink()
    shutil.move(moved, objects)


def test_worker_slot_checkout_uses_shared_trust_policy(tmp_path: Path) -> None:
    root, source_sha, source_tree = repository(tmp_path)
    checkout = worker_slot_runtime.inspect_git_checkout(root)
    assert checkout.source_sha == source_sha
    assert checkout.source_tree == source_tree
    assert checkout.trust_evidence["evidence_sha256"].startswith("sha256:")

    git(root, "config", "core.fsmonitor", "/tmp/evil")
    with raises(
        worker_slot_runtime.WorkerSlotError,
        match="trust preflight",
    ):
        worker_slot_runtime.inspect_git_checkout(root)


def test_legacy_takeover_uses_shared_trust_policy(tmp_path: Path) -> None:
    root, source_sha, source_tree = repository(tmp_path)
    canonical_remote(root)
    runtime, marker = permission_marker(tmp_path)
    trust.takeover_repository_permissions(root, marker)
    system = legacy_takeover.LiveSystem(root, runtime)
    identity = system.git_identity()
    assert identity["head_sha"] == source_sha
    assert identity["head_tree"] == source_tree
    assert system.git_trust_evidence(identity)["policy"] == trust.POLICY_NAME

    with (root / ".git/config").open("a", encoding="utf-8") as stream:
        stream.write("\n[include]\n\tpath = /tmp/evil\n")
    with raises(
        legacy_takeover.LegacyTakeoverError,
        match="trust preflight",
    ):
        system.git_identity()


def test_pull_controller_uses_shared_trust_policy(tmp_path: Path) -> None:
    root, source_sha, source_tree = repository(tmp_path)
    canonical_remote(root)
    runtime, marker = permission_marker(tmp_path)
    trust.takeover_repository_permissions(root, marker)
    config = runtime / "config"
    config.mkdir(parents=True, mode=0o700)
    for name in ("git-deploy-key", "known_hosts"):
        path = config / name
        path.write_text("fixture\n", encoding="utf-8")
        path.chmod(0o600)
    controller = pull_deploy_controller.PullDeployController(
        root,
        runtime,
        apply=False,
    )
    identity = controller.repository_identity()
    assert identity["sha"] == source_sha
    assert identity["tree"] == source_tree
    assert identity["trust"]["policy"] == trust.POLICY_NAME

    git(root, "config", "core.sparseCheckout", "true")
    with raises(
        pull_deploy_controller.PullDeployError,
        match="trust preflight",
    ):
        controller.repository_identity()


def test_alias_reconcile_uses_shared_trust_policy(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    root, source_sha, source_tree = repository(tmp_path)
    canonical_remote(root)
    runtime, marker = permission_marker(tmp_path)
    trust.takeover_repository_permissions(root, marker)
    monkeypatch.setattr(alias_reconcile, "PRODUCTION_ROOT", root)
    monkeypatch.setattr(alias_reconcile, "RUNTIME_ROOT", runtime)
    monkeypatch.setattr(alias_reconcile, "LEGACY_SOURCE_SHA", source_sha)
    monkeypatch.setattr(alias_reconcile, "LEGACY_SOURCE_TREE", source_tree)
    identity = alias_reconcile._source_identity(
        alias_reconcile.SystemRunner()
    )
    assert identity["sha"] == source_sha
    assert identity["tree"] == source_tree
    assert identity["trust"]["policy"] == trust.POLICY_NAME

    alternate = root / ".git/objects/info/alternates"
    alternate.write_text("/tmp/untrusted\n", encoding="utf-8")
    alternate.chmod(0o600)
    with raises(
        alias_reconcile.ReconcileError,
        match="trust preflight",
    ):
        alias_reconcile._source_identity(alias_reconcile.SystemRunner())


def _temporary_path_case(
    name: str,
    function: Callable[..., None],
    *arguments: object,
    monkeypatch: bool = False,
) -> unittest.FunctionTestCase:
    def run() -> None:
        with tempfile.TemporaryDirectory(
            prefix="nexpoly-git-source-trust-"
        ) as directory:
            tmp_path = Path(directory)
            if not monkeypatch:
                function(tmp_path, *arguments)
                return
            patcher = MonkeyPatch()
            try:
                function(tmp_path, patcher, *arguments)
            finally:
                patcher.undo()

    run.__name__ = name
    return unittest.FunctionTestCase(run)


def load_tests(
    _loader: unittest.TestLoader,
    _tests: unittest.TestSuite,
    _pattern: str | None,
) -> unittest.TestSuite:
    """Expose every trust case to the stdlib runner used by script-tests."""

    suite = unittest.TestSuite()
    for operation, crash_at in PERMISSION_CRASH_CASES:
        label = re.sub(r"[^a-z0-9]+", "_", crash_at.lower()).strip("_")
        suite.addTest(
            _temporary_path_case(
                f"permission_{operation}_{label}",
                test_permission_takeover_resumes_every_marker_and_action_phase,
                operation,
                crash_at,
            )
        )
    for crash_window in PERSISTENT_ROTATION_CRASH_WINDOWS:
        suite.addTest(
            _temporary_path_case(
                f"permission_rotation_{crash_window.replace('-', '_')}",
                test_permission_marker_replays_persistent_rotation_crash,
                crash_window,
            )
        )
    for swap_window in HELD_SOURCE_SWAP_WINDOWS:
        suite.addTest(
            _temporary_path_case(
                f"permission_held_source_swap_{swap_window.replace('-', '_')}",
                test_permission_marker_rebuilds_after_held_source_swap,
                swap_window,
            )
        )
    for fault_point in REBUILD_DURABILITY_FAULTS:
        suite.addTest(
            _temporary_path_case(
                "permission_rebuild_existing_"
                f"{fault_point.replace('-', '_')}_lost_response",
                test_permission_rebuild_existing_lost_response_replays_after_power_loss,
                fault_point,
            )
        )
    suite.addTest(
        _temporary_path_case(
            "permission_rejects_immutable_object_drift",
            test_permission_takeover_rejects_immutable_object_drift,
        )
    )
    suite.addTest(
        _temporary_path_case(
            "evidence_binds_git_interpretation",
            test_evidence_binds_interpreted_config_index_refs_and_objects,
        )
    )
    for name in HOSTILE_AMBIENT_NAMES:
        suite.addTest(
            _temporary_path_case(
                f"ambient_{name.lower()}_rejected",
                test_hostile_ambient_git_redirects_are_rejected,
                name,
            )
        )
    for index, (section, key, value) in enumerate(REDIRECT_CONFIG_CASES):
        suite.addTest(
            _temporary_path_case(
                f"redirect_config_{index}_{key.lower()}_rejected",
                test_executable_redirect_and_partial_clone_config_is_rejected,
                section,
                key,
                value,
            )
        )
    for relative in FORBIDDEN_STORAGE_MARKERS:
        label = re.sub(r"[^a-z0-9]+", "_", relative.lower()).strip("_")
        suite.addTest(
            _temporary_path_case(
                f"storage_marker_{label}_rejected",
                test_external_storage_replace_grafts_shallow_and_sparse_are_rejected,
                relative,
            )
        )
    direct_cases = [
        (
            "promisor_shared_object_and_worktree_rejected",
            test_promisor_pack_shared_object_and_linked_worktree_are_rejected,
        ),
        (
            "external_index_extensions_rejected",
            test_external_index_fsmonitor_and_sparse_index_extensions_are_rejected,
        ),
        (
            "safe_child_environment",
            test_safe_child_environment_does_not_inherit_loader_or_user_config,
        ),
        (
            "permission_rejects_explicit_pushurl_before_mutation",
            test_permission_takeover_rejects_explicit_pushurl_before_mutation,
        ),
        (
            "permission_plan_zero_write_and_inventory_cas",
            test_permission_plan_is_zero_write_and_first_marker_is_inventory_cas,
        ),
        (
            "permission_marker_first_publish_target_appeared",
            test_permission_marker_first_publish_target_appeared_is_not_overwritten,
        ),
        (
            "permission_marker_later_publish_target_swap",
            test_permission_marker_later_publish_target_swap_is_not_moved,
        ),
        (
            "permission_marker_crash_after_old_moves_to_previous",
            test_permission_marker_replays_crash_after_old_moves_to_previous,
        ),
        (
            "permission_marker_full_lifecycle_never_unlinks_retired",
            test_permission_marker_full_lifecycle_never_unlinks_retired_history,
        ),
        (
            "permission_marker_oldest_retired_deletion_breaks_anchor",
            test_permission_marker_oldest_retired_deletion_breaks_anchor,
        ),
        (
            "permission_marker_foreign_retired_target_not_overwritten",
            test_permission_marker_foreign_retired_target_is_not_overwritten,
        ),
        (
            "permission_marker_previous_swap_after_retirement",
            test_permission_marker_previous_swap_after_retirement_fails_closed,
        ),
        (
            "permission_marker_restore_source_swap_rebuild",
            test_permission_marker_rebuilds_after_restore_source_swap,
        ),
        (
            "marker_only_hardened_restore_marker_source_swap_rebuild",
            test_marker_only_hardened_restore_rebuilds_after_marker_source_swap,
        ),
        (
            "permission_marker_staging_source_swap_replay",
            test_permission_marker_replays_after_staging_source_swap,
        ),
        (
            "permission_marker_retired_and_rebuild_source_swap_replay",
            test_permission_marker_replays_after_retired_and_rebuild_source_swaps,
        ),
        (
            "permission_marker_quarantine_replays_rename_crash",
            test_permission_marker_quarantine_replays_rename_crash,
        ),
        (
            "permission_rebuild_partial_preplant_fails_closed",
            test_permission_rebuild_partial_preplant_fails_closed_without_mutation,
        ),
        (
            "permission_stable_chmod_reseals_inode_and_parent",
            test_permission_stable_chmod_reseals_inode_and_parent,
        ),
        (
            "permission_stable_staging_and_absence_reseal_namespace",
            test_permission_stable_staging_and_absence_reseal_namespace,
        ),
        (
            "permission_stable_staging_rejects_path_swap",
            test_permission_stable_staging_rejects_path_swap_after_file_fsync,
        ),
        (
            "permission_stable_chmod_rejects_path_swap",
            test_permission_stable_chmod_rejects_path_swap_after_inode_fsync,
        ),
        (
            "permission_stable_marker_replay_reseals_directory",
            test_permission_stable_marker_replay_reseals_directory,
        ),
        (
            "permission_plan_rejects_oversized_marker_before_write",
            test_permission_plan_rejects_oversized_marker_before_write,
        ),
        (
            "permission_history_total_limit_before_mutation",
            test_permission_history_total_limit_fails_before_marker_or_chmod,
        ),
        (
            "permission_history_free_space_before_mutation",
            test_permission_history_free_space_fails_before_marker_or_chmod,
        ),
        (
            "permission_history_staging_capacity_replay",
            test_permission_history_capacity_replay_does_not_count_staging_twice,
        ),
        (
            "permission_restore_rejects_future_oversized_generation",
            test_permission_restore_rejects_future_oversized_generation_before_chmod,
        ),
        (
            "existing_captured_marker_rejects_pushurl",
            test_existing_captured_marker_cannot_bypass_new_pushurl_policy,
        ),
        (
            "existing_hardened_marker_restores_pushurl",
            test_existing_hardened_pushurl_marker_can_only_restore,
        ),
        ("object_store_symlink_rejected", test_object_store_symlink_is_rejected),
        ("worker_slot_uses_trust_policy", test_worker_slot_checkout_uses_shared_trust_policy),
        ("legacy_takeover_uses_trust_policy", test_legacy_takeover_uses_shared_trust_policy),
        ("pull_controller_uses_trust_policy", test_pull_controller_uses_shared_trust_policy),
    ]
    for name, function in direct_cases:
        suite.addTest(_temporary_path_case(name, function))
    suite.addTest(
        _temporary_path_case(
            "alias_reconcile_uses_trust_policy",
            test_alias_reconcile_uses_shared_trust_policy,
            monkeypatch=True,
        )
    )
    return suite

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from scripts import git_source_trust as trust
from scripts import legacy_takeover
from scripts import pull_deploy_controller
from scripts import reconcile_production_0005_polytao_alias as alias_reconcile
from scripts import worker_slot_runtime


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


def evidence(root: Path, source_sha: str, source_tree: str):
    return trust.repository_trust_evidence(
        root,
        source_sha=source_sha,
        source_tree=source_tree,
        branch="refs/heads/main",
        origin=None,
        ambient={},
    )


@pytest.mark.parametrize(
    ("operation", "crash_at"),
    [
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
    ],
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
        with pytest.raises(RuntimeError, match="injected crash"):
            trust.restore_repository_permissions(
                root,
                marker,
                checkpoint=checkpoint,
            )
    else:
        with pytest.raises(RuntimeError, match="injected crash"):
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
    with pytest.raises(
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
    with pytest.raises(
        trust.GitPermissionTakeoverError,
        match="content changed",
    ):
        trust.verify_repository_permission_takeover(root, marker)


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
    with pytest.raises(trust.GitSourceTrustError, match="trust surface"):
        trust.require_stable_trust_surface(before, after)


@pytest.mark.parametrize(
    "name",
    [
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
    ],
)
def test_hostile_ambient_git_redirects_are_rejected(
    tmp_path: Path,
    name: str,
) -> None:
    root, source_sha, source_tree = repository(tmp_path)
    with pytest.raises(
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


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("include", "path", "/tmp/evil"),
        ('includeIf "gitdir:/data/**"', "path", "/tmp/evil"),
        ("core", "worktree", "/tmp/evil"),
        ("core", "fsmonitor", "/tmp/evil"),
        ("core", "sparseCheckout", "true"),
        ("core", "hooksPath", "/tmp/evil"),
        ("extensions", "partialClone", "origin"),
        ('remote "origin"', "promisor", "true"),
        ("filter", "evil.clean", "/tmp/evil"),
    ],
)
def test_executable_redirect_and_partial_clone_config_is_rejected(
    tmp_path: Path,
    section: str,
    key: str,
    value: str,
) -> None:
    root, source_sha, source_tree = repository(tmp_path)
    with (root / ".git/config").open("a", encoding="utf-8") as stream:
        stream.write(f'\n[{section}]\n\t{key} = {value}\n')
    with pytest.raises(trust.GitSourceTrustError, match="config"):
        evidence(root, source_sha, source_tree)


@pytest.mark.parametrize(
    "relative",
    [
        "commondir",
        "shallow",
        "config.worktree",
        "info/grafts",
        "info/sparse-checkout",
        "objects/info/alternates",
        "objects/info/http-alternates",
        "refs/replace",
    ],
)
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
    with pytest.raises(
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
    with pytest.raises(trust.GitSourceTrustError, match="promisor"):
        evidence(root, source_sha, source_tree)
    promisor.unlink()

    loose = next(
        path
        for path in (root / ".git/objects").glob("[0-9a-f][0-9a-f]/*")
        if path.is_file()
    )
    external = tmp_path / "shared-object"
    os.link(loose, external)
    with pytest.raises(trust.GitSourceTrustError, match="unsafe metadata"):
        evidence(root, source_sha, source_tree)
    external.unlink()

    linked = tmp_path / "linked"
    git(root, "worktree", "add", "--detach", str(linked), source_sha)
    with pytest.raises(
        trust.GitSourceTrustError,
        match="metadata directory",
    ):
        evidence(linked, source_sha, source_tree)


def test_external_index_fsmonitor_and_sparse_index_extensions_are_rejected(
    tmp_path: Path,
) -> None:
    root, source_sha, source_tree = repository(tmp_path)
    with pytest.raises(trust.GitSourceTrustError, match="ambient Git control"):
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
    with pytest.raises(trust.GitSourceTrustError, match="FSMN"):
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
    with pytest.raises(trust.GitSourceTrustError, match="subcommand"):
        trust.safe_git_command(root, "-c", "include.path=/tmp/evil", "status")
    with pytest.raises(trust.GitSourceTrustError, match="executable"):
        trust.safe_git_command(root, "status", executable="git")
    assert trust.safe_git_command(root, "status")[0] == "/usr/bin/git"


def test_object_store_symlink_is_rejected(tmp_path: Path) -> None:
    root, source_sha, source_tree = repository(tmp_path)
    objects = root / ".git/objects"
    moved = root / ".git/objects-real"
    objects.rename(moved)
    objects.symlink_to(moved, target_is_directory=True)
    with pytest.raises(
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
    with pytest.raises(
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
    with pytest.raises(
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
    with pytest.raises(
        pull_deploy_controller.PullDeployError,
        match="trust preflight",
    ):
        controller.repository_identity()


def test_alias_reconcile_uses_shared_trust_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    with pytest.raises(
        alias_reconcile.ReconcileError,
        match="trust preflight",
    ):
        alias_reconcile._source_identity(alias_reconcile.SystemRunner())

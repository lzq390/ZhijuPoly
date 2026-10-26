"""Stable complete API bundles preserve code and exclude private identities."""
from pathlib import Path
import hashlib
import json

from scripts import polymer_md_api_services as bundler

import pytest

from scripts.polymer_md_api_services import PACKAGES, SCRIPTS, api_unit, freeze_api, verify_api


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "source"
    for name in PACKAGES:
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "module.py").write_text("VALUE = 1\n")
        (folder / ".env").write_text("PRIVATE_CONFIGURATION=excluded\n")
        (folder / "__pycache__").mkdir()
        (folder / "__pycache__/module.pyc").write_bytes(b"not a source artifact")
    (root / "backend/app/md_demo_trajectory.npz").write_bytes(b"required demo fixture")
    monkeypatch.setattr(bundler, "PUBLIC_DATA_FILES", {"backend/app/md_demo_trajectory.npz": hashlib.sha256(b"required demo fixture").hexdigest()})
    for name in SCRIPTS:
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("# management script\n")
    lock = tmp_path / "api.lock"
    lock.write_text("example==1.0 --hash=sha256:" + "a" * 64 + "\n")
    return root, tmp_path / "stable", lock


def test_bundle_keeps_migrations_and_fixtures_without_identity_files(source):
    root, home, lock = source
    release = freeze_api(root, home, lock)
    manifest = verify_api(release)
    names = {item["path"] for item in manifest["files"]}
    assert "backend/app/md_demo_trajectory.npz" in names
    assert "backend/migrations/postgres/module.py" in names
    assert "gpu_resource/module.py" in names
    assert "scripts/polymer_md_acceptance_registry.py" in names
    assert not any(".env" in name or "__pycache__" in name for name in names)
    assert freeze_api(root, home, lock) == release


def test_new_source_and_dependency_bytes_create_new_bundles(source):
    root, home, lock = source
    first = freeze_api(root, home, lock)
    (root / "backend/app/module.py").write_text("VALUE = 2\n")
    second = freeze_api(root, home, lock)
    lock.write_text("example==2.0 --hash=sha256:" + "b" * 64 + "\n")
    third = freeze_api(root, home, lock)
    assert len({first, second, third}) == 3
    assert (first / "backend/app/module.py").read_text() == "VALUE = 1\n"
    assert verify_api(first)["source_digest"] == first.name


@pytest.mark.parametrize("mutation", ["modified", "extra", "symlink"])
def test_bundle_tampering_is_rejected(source, mutation):
    root, home, lock = source
    release = freeze_api(root, home, lock)
    if mutation == "modified":
        (release / "backend/app/module.py").chmod(0o600)
        (release / "backend/app/module.py").write_text("tampered\n")
    elif mutation == "extra":
        (release / "unexpected.json").write_text("{}")
    else:
        (release / "unexpected-link").symlink_to(root / "backend/app/module.py")
    with pytest.raises(ValueError):
        verify_api(release)


def test_linked_source_is_never_frozen(source):
    root, home, lock = source
    (root / "backend/app/linked.py").symlink_to(root / "gpu_resource/module.py")
    with pytest.raises(ValueError, match="symbolic"):
        freeze_api(root, home, lock)


def test_unit_separates_stable_code_config_and_python_and_verifies_before_start(source):
    root, home, lock = source
    release = freeze_api(root, home, lock)
    python, config, assets = home / "backend-environment/bin/python", home / "auth", home / "platform-assets"
    unit = api_unit(release, python, config, assets)
    assert "ExecStartPre=" in unit and '"verify"' in unit
    assert '"serve-full" "--runtime" "' + str(config) + '"' in unit
    assert str(root) not in unit
    assert "postgresql://" not in unit
    assert str(python) in unit and "OMP_NUM_THREADS=1" in unit
    assert "KillMode=control-group" in unit and "Restart=on-failure" in unit


def test_non_source_private_metadata_is_never_bundled(source):
    root, home, lock = source
    (root / "backend/app/private-record.json").write_text('{"synthetic_secret":"not-a-real-secret"}')
    with pytest.raises(ValueError, match="unreviewed package data"):
        freeze_api(root, home, lock)
    assert not (home / "api-releases").exists()


def test_lock_cannot_carry_environment_or_authenticated_urls(source):
    root, home, lock = source
    for value in ("PRIVATE_CONFIGURATION=synthetic-value\n", "--extra-index-url https://synthetic:example@invalid.example/simple\n"):
        lock.write_text(value)
        with pytest.raises(ValueError, match="pinned public"):
            freeze_api(root, home, lock)
    assert not (home / "api-releases").exists()


def test_dependency_comments_are_not_copied_to_release(source):
    root, home, lock = source
    lock.write_text("# synthetic private comment\nexample==1.0 --hash=sha256:" + "a" * 64 + "\n")
    release = freeze_api(root, home, lock)
    assert "synthetic private comment" not in (release / "backend-api-requirements.lock").read_text()


def test_home_inside_source_is_rejected(source):
    root, _, lock = source
    with pytest.raises(ValueError, match="outside source"):
        freeze_api(root, root / "backend/app/releases", lock)


def test_known_data_filename_with_changed_bytes_is_rejected(source):
    root, home, lock = source
    (root / "backend/app/md_demo_trajectory.npz").write_bytes(b"different synthetic bytes")
    with pytest.raises(ValueError, match="unreviewed package data"):
        freeze_api(root, home, lock)
    assert not (home / "api-releases").exists()


def test_self_consistent_relabelled_inventory_is_rejected(source):
    root, home, lock = source
    release = freeze_api(root, home, lock)
    index = release / "api-release.json"
    manifest = json.loads(index.read_text())
    target = release / "backend/app/module.py"
    target.chmod(0o600)
    target.write_text("CHANGED = 1\n")
    for item in manifest["files"]:
        if item["path"] == "backend/app/module.py":
            item.update(sha256=bundler.sha256(target), size_bytes=target.stat().st_size)
    manifest["source_digest"] = hashlib.sha256(json.dumps(manifest["files"],sort_keys=True,separators=(",", ":")).encode()).hexdigest()
    index.chmod(0o600)
    index.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="location"):
        verify_api(release)


def test_multiline_hashed_public_dependencies_are_preserved(source):
    root, home, lock = source
    lines = ["one==1.0 " + chr(92), "    --hash=sha256:" + "a" * 64,
             "two==2.0 --hash=sha256:" + "b" * 64]
    lock.write_text("\n".join(lines) + "\n")
    release = freeze_api(root, home, lock)
    payload = (release / "backend-api-requirements.lock").read_text()
    assert "one==1.0" in payload and "two==2.0" in payload
    assert "--hash=sha256:" + "a" * 64 in payload
    assert "--hash=sha256:" + "b" * 64 in payload



def test_linked_release_parent_cannot_redirect_output_into_source(source):
    root, home, lock = source
    home.mkdir()
    target = root / "backend/app/releases"
    target.mkdir()
    (home / "api-releases").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        freeze_api(root, home, lock)
    assert not list(target.iterdir())


@pytest.mark.parametrize("parent", ["backend", "scripts"])
def test_linked_source_ancestor_cannot_read_outside_root(source, parent):
    root, home, lock = source
    outside = root.parent / ("outside-" + parent)
    (root / parent).rename(outside)
    (root / parent).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        freeze_api(root, home, lock)
    assert not (home / "api-releases").exists()


def test_linked_lock_parent_is_rejected(source):
    root, home, lock = source
    alias = root.parent / "lock-alias"
    alias.symlink_to(lock.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        freeze_api(root, home, alias / lock.name)


def test_verification_rejects_a_linked_release_ancestor(source):
    root, home, lock = source
    release = freeze_api(root, home, lock)
    outside = home.parent / "outside-releases"
    (home / "api-releases").rename(outside)
    (home / "api-releases").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        verify_api(release)



@pytest.mark.parametrize("race_kind", ["file", "symlink"])
def test_render_unit_never_overwrites_a_concurrent_file_or_link(source, monkeypatch, race_kind):
    root, home, lock = source
    release = freeze_api(root, home, lock)
    output = home / "unit.service"
    victim = home / "preserve.txt"
    victim.write_text("preserve original")
    real_unit = bundler.api_unit
    def racing_unit(*args):
        result = real_unit(*args)
        if race_kind == "file":
            output.write_text("concurrent unit")
        else:
            output.symlink_to(victim)
        return result
    monkeypatch.setattr(bundler, "api_unit", racing_unit)
    monkeypatch.setattr(__import__("sys"), "argv", ["polymer-md-api", "render-unit", "--release", str(release),
        "--python", str(home / "python"), "--config", str(home / "config"), "--assets", str(home / "assets"),
        "--output", str(output)])
    with pytest.raises((FileExistsError, ValueError)):
        bundler.main()
    assert victim.read_text() == "preserve original"
    if race_kind == "file":
        assert output.read_text() == "concurrent unit"


def test_render_unit_uses_private_exclusive_output(source, monkeypatch):
    root, home, lock = source
    release = freeze_api(root, home, lock)
    output = home / "new-unit.service"
    monkeypatch.setattr(__import__("sys"), "argv", ["polymer-md-api", "render-unit", "--release", str(release),
        "--python", str(home / "python"), "--config", str(home / "config"), "--assets", str(home / "assets"),
        "--output", str(output)])
    assert bundler.main() == 0
    assert output.stat().st_mode & 0o077 == 0
    assert "ExecStartPre=" in output.read_text()


def test_project_binary_only_lock_and_continuations_are_preserved(source):
    root, home, lock = source
    lock.write_text("--only-binary :all:\nexample==1.0 " + chr(92) +
                    "\n    --hash=sha256:" + "a" * 64 + "\n")
    release = freeze_api(root, home, lock)
    assert (release / "backend-api-requirements.lock").read_text() == (
        "--only-binary :all:\nexample==1.0 --hash=sha256:" + "a" * 64 + "\n")


@pytest.mark.parametrize("relative", ["backend/requirements.lock", "backend/requirements-ci.lock"])
def test_actual_public_project_locks_are_accepted_without_installing(relative):
    lock = Path(__file__).resolve().parents[2] / relative
    payload = bundler.public_dependency_bytes(lock).decode()
    assert payload.startswith("--only-binary :all:\n")
    pins = payload.splitlines()[1:]
    assert len(pins) > 20
    assert all("==" in line and "--hash=sha256:" in line for line in pins)
    assert "\\" not in payload and "#" not in payload
    assert bundler.public_dependency_bytes(lock) == payload.encode()


@pytest.mark.parametrize("control", [
    "--only-binary :none:", "--only-binary example", "--no-binary :all:",
    "--index-url https://invalid.example/simple", "--extra-index-url https://invalid.example/simple",
    "--trusted-host invalid.example", "-r other.lock", "--only-binary :all: --pre",
    "--only-binary :all:\n--only-binary :all:",
])
def test_other_lock_control_options_are_rejected(source, control):
    _, _, lock = source
    lock.write_text(control + "\nexample==1.0 --hash=sha256:" + "a" * 64 + "\n")
    with pytest.raises(ValueError, match="pinned public"):
        bundler.public_dependency_bytes(lock)


@pytest.mark.parametrize("payload", [
    "--only-binary :all:\n",
    "example==1.0 --hash=sha256:" + "a" * 64 + " " + chr(92) + "\n",
    "example==1.0 --hash=sha256:" + "a" * 64 + chr(92) + "injected\n",
    "example==1.0 --hash=sha256:" + "a" * 64 + "\n--only-binary :all:\n",
])
def test_incomplete_or_misplaced_lock_records_are_rejected(source, payload):
    _, _, lock = source
    lock.write_text(payload)
    with pytest.raises(ValueError, match="pinned public"):
        bundler.public_dependency_bytes(lock)


@pytest.mark.parametrize("outcome", ["success", "exception"])
def test_cli_restores_callers_creation_mask(monkeypatch, outcome):
    import os
    previous = os.umask(0o027)
    def dispatch():
        inside = os.umask(0o077)
        os.umask(inside)
        assert inside == 0o077
        if outcome == "exception": raise RuntimeError("synthetic dispatch failure")
        return 17
    monkeypatch.setattr(bundler, "_main", dispatch)
    try:
        if outcome == "exception":
            with pytest.raises(RuntimeError, match="synthetic dispatch failure"): bundler.main()
        else:
            assert bundler.main() == 17
    finally:
        observed = os.umask(0o077)
        os.umask(previous)
    assert observed == 0o027


def test_cli_parameter_error_restores_creation_mask(monkeypatch):
    import os
    import sys
    previous = os.umask(0o027)
    monkeypatch.setattr(sys, "argv", ['polymer_md_api_services.py', 'verify'])
    try:
        with pytest.raises(SystemExit) as captured: bundler.main()
        assert captured.value.code == 2
    finally:
        observed = os.umask(0o077)
        os.umask(previous)
    assert observed == 0o027

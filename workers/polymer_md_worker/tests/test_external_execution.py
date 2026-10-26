"""Durable native-execution exclusion; no child, MPI, or service is started."""
import json
from pathlib import Path

import pytest

from workers.polymer_md_worker.app.external_execution import (
    MARKER, assert_no_external_execution, claim_execution, release_execution,
)
from workers.polymer_md_worker.app.journal import RootLock, atomic_json, read_json


def setup_execution(tmp_path, name="first"):
    root = tmp_path / "jobs"; root.mkdir(mode=0o700, exist_ok=True)
    runtime = tmp_path / "runtime"; runtime.mkdir(mode=0o700, exist_ok=True)
    evidence = runtime / name; evidence.mkdir(mode=0o700)
    fact = {"job_root": str(root), "runtime_root": str(runtime), "cleanup_confirmed": False,
            "scope": {"nonce": name + "-nonce", "unit": name + ".scope"}}
    atomic_json(evidence / "journal.json", fact)
    return root, runtime, evidence, fact


def claim(root, runtime, evidence, fact):
    return claim_execution(root, evidence, runtime, fact["scope"]["nonce"], fact["scope"]["unit"])


def test_marker_survives_loss_of_both_cli_and_guardian_locks(tmp_path):
    root, runtime, evidence, fact = setup_execution(tmp_path)
    owner = RootLock(root); owner.acquire()
    claim(root, runtime, evidence, fact); owner.release()
    replacement = RootLock(root); replacement.acquire()
    try:
        with pytest.raises(RuntimeError, match="cleanup is unconfirmed"):
            assert_no_external_execution(root)
        assert (root / MARKER).exists()
    finally: replacement.release()


def test_cleanup_confirmation_removes_only_verified_marker_on_restart(tmp_path):
    root, runtime, evidence, fact = setup_execution(tmp_path)
    claim(root, runtime, evidence, fact)
    fact["cleanup_confirmed"] = True; atomic_json(evidence / "journal.json", fact)
    assert_no_external_execution(root)
    assert not (root / MARKER).exists()
    assert_no_external_execution(root)


def test_release_needs_exact_claim_and_confirmed_cleanup(tmp_path):
    root, runtime, evidence, fact = setup_execution(tmp_path)
    claim(root, runtime, evidence, fact)
    with pytest.raises(RuntimeError, match="different"):
        release_execution(root, evidence, "other-nonce", fact["scope"]["unit"])
    with pytest.raises(RuntimeError, match="before confirmed"):
        release_execution(root, evidence, fact["scope"]["nonce"], fact["scope"]["unit"])
    fact["cleanup_confirmed"] = True; atomic_json(evidence / "journal.json", fact)
    assert release_execution(root, evidence, fact["scope"]["nonce"], fact["scope"]["unit"])
    assert not release_execution(root, evidence, fact["scope"]["nonce"], fact["scope"]["unit"])


def test_claim_is_idempotent_and_cannot_replace_unconfirmed_execution(tmp_path):
    root, runtime, evidence, fact = setup_execution(tmp_path)
    original = claim(root, runtime, evidence, fact)
    assert claim(root, runtime, evidence, fact) == original
    other_root, other_runtime, other_evidence, other_fact = setup_execution(tmp_path, "second")
    with pytest.raises(RuntimeError, match="unconfirmed"):
        claim(other_root, other_runtime, other_evidence, other_fact)
    assert read_json(root / MARKER) == original
    fact["cleanup_confirmed"] = True; atomic_json(evidence / "journal.json", fact)
    claim(other_root, other_runtime, other_evidence, other_fact)
    assert read_json(root / MARKER)["nonce"] == other_fact["scope"]["nonce"]


@pytest.mark.parametrize("field,value", [("nonce", "other"), ("unit", "other.scope"),
    ("job_root", "/another/root"), ("runtime_root", "/another/runtime")])
def test_changed_journal_identity_cannot_clear_marker_even_with_cleanup_true(tmp_path, field, value):
    root, runtime, evidence, fact = setup_execution(tmp_path); claim(root, runtime, evidence, fact)
    (fact["scope"] if field in {"nonce", "unit"} else fact)[field] = value
    fact["cleanup_confirmed"] = True; atomic_json(evidence / "journal.json", fact)
    with pytest.raises(RuntimeError, match="journal is invalid"):
        assert_no_external_execution(root)
    assert (root / MARKER).exists()


@pytest.mark.parametrize("change", ["marker_permissions", "journal_permissions", "journal_missing", "marker_invalid", "journal_symlink"])
def test_invalid_or_public_metadata_fails_closed(tmp_path, change):
    root, runtime, evidence, fact = setup_execution(tmp_path); claim(root, runtime, evidence, fact)
    marker = root / MARKER; journal = evidence / "journal.json"
    if change == "marker_permissions": marker.chmod(0o644)
    elif change == "journal_permissions": journal.chmod(0o644)
    elif change == "journal_missing": journal.unlink()
    elif change == "marker_invalid": marker.write_text("{torn")
    else:
        target = evidence / "other.json"; journal.rename(target); journal.symlink_to(target)
    with pytest.raises(RuntimeError): assert_no_external_execution(root)
    assert marker.exists()


def test_truthy_cleanup_and_stale_completed_journal_do_not_authorize_spawn(tmp_path):
    root, runtime, evidence, fact = setup_execution(tmp_path)
    fact["cleanup_confirmed"] = True; atomic_json(evidence / "journal.json", fact)
    with pytest.raises(RuntimeError, match="uncompleted"):
        claim(root, runtime, evidence, fact)
    fact["cleanup_confirmed"] = False; atomic_json(evidence / "journal.json", fact)
    claim(root, runtime, evidence, fact)
    fact["cleanup_confirmed"] = 1; atomic_json(evidence / "journal.json", fact)
    with pytest.raises(RuntimeError, match="unconfirmed"): assert_no_external_execution(root)


def test_claim_requires_durable_journal_before_marker(tmp_path):
    root, runtime, evidence, fact = setup_execution(tmp_path)
    (evidence / "journal.json").unlink()
    with pytest.raises(RuntimeError, match="journal is invalid"):
        claim(root, runtime, evidence, fact)
    assert not (root / MARKER).exists()

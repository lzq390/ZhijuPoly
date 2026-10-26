"""Planning is inert; offline backup rejects live or changed environments."""
import json

import pytest

from scripts import polymer_md_backup_dev as backup


@pytest.fixture
def prepared(monkeypatch, tmp_path):
    root = tmp_path / "worktree"
    receipts = root / ".runtime/polymer-md"
    receipts.mkdir(mode=0o700, parents=True)
    runtime, auth, jobs = (tmp_path / name for name in ("runtime", "auth", "jobs"))
    for path in (runtime, auth, jobs):
        path.mkdir(mode=0o700)
    backup.save(receipts / "worker-runtime.json", {"runtime": str(runtime), "job_root": str(jobs),
        "runtime_fingerprint": "a" * 64, "unit": "nexpoly-pmd-worker-test.service"})
    backup.save(receipts / "backend-runtime.json", {"runtime": str(auth), "unit": "nexpoly-pmd-api-test.service"})
    backup.save(auth / "backend-private.json", {"database": backup.SOURCE_DATABASE, "marker": "private-owner-marker"})
    backup.save(runtime / "manifest.json", {"runtime_fingerprint": "a" * 64})
    monkeypatch.setattr(backup, "ROOT", root)
    monkeypatch.setattr(backup, "psql", lambda *_args, **_kwargs: pytest.fail("SQL must not execute in a plan/gate test"))
    return tmp_path, runtime, auth, jobs


def test_plan_does_not_connect_or_acquire_a_job_lock(prepared, monkeypatch):
    root, runtime, auth, jobs = prepared
    monkeypatch.setattr(backup.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("plan cannot call Docker/systemd"))
    result = backup.plan(root / "backup-new")
    assert result["job_root"] == str(jobs) and result["runtime"] == str(runtime)
    assert result["plan_executes_database_or_locks"] is False
    serialized = json.dumps(result)
    assert "private-owner-marker" not in serialized and "postgresql://" not in serialized
    assert "password" not in serialized and result["temporary_credential_persisted"] is False
    assert not (jobs / ".supervisor.lock").exists()


def test_plan_rejects_output_inside_either_source_root(prepared):
    _, runtime, _, jobs = prepared
    for output in (runtime / "backup", jobs / "backup"):
        with pytest.raises(ValueError, match="outside"):
            backup.plan(output)


def test_run_requires_services_stopped_before_importing_locks_or_sql(prepared, monkeypatch):
    root, _, _, jobs = prepared
    document = backup.plan(root / "backup-new")
    monkeypatch.setattr(backup, "inactive", lambda _unit: (_ for _ in ()).throw(ValueError("service active")))
    with pytest.raises(ValueError, match="service active"):
        backup.run(document, root / "evidence.json")
    assert not (jobs / ".supervisor.lock").exists()


def test_run_rejects_driver_or_runtime_change_before_locks_or_sql(prepared, monkeypatch):
    root, runtime, _, jobs = prepared
    document = backup.plan(root / "backup-new")
    with pytest.raises(ValueError, match="driver changed"):
        backup.run({**document, "driver_sha256": "0" * 64}, root / "evidence.json")
    monkeypatch.setattr(backup, "inactive", lambda _unit: None)
    (runtime / "manifest.json").write_text(json.dumps({"runtime_fingerprint": "b" * 64}))
    with pytest.raises(ValueError, match="runtime changed"):
        backup.run(document, root / "evidence.json")
    assert not (jobs / ".supervisor.lock").exists()


@pytest.mark.parametrize("field,value", [
    ("api_unit", "nexpoly-pmd-api-other.service"),
    ("worker_unit", "nexpoly-pmd-worker-other.service"),
    ("runtime", "/unselected/runtime"), ("job_root", "/unselected/jobs"),
    ("runtime_fingerprint", "0" * 64),
])
def test_edited_plan_cannot_check_other_units_or_roots(prepared, monkeypatch, field, value):
    root, _, _, jobs = prepared
    document = backup.plan(root / "backup-new")
    monkeypatch.setattr(backup, "inactive", lambda *_: pytest.fail("reject drift before any service query"))
    with pytest.raises(ValueError, match="environment changed"):
        backup.run({**document, field: value}, root / "evidence.json")
    assert not (jobs / ".supervisor.lock").exists()


def test_receipt_identity_change_is_rejected_before_service_or_database(prepared, monkeypatch):
    root, _, auth, jobs = prepared
    document = backup.plan(root / "backup-new")
    (auth / "backend-private.json").write_text(json.dumps({"database": backup.SOURCE_DATABASE, "marker": "synthetic-changed-owner"}))
    monkeypatch.setattr(backup, "inactive", lambda *_: pytest.fail("reject changed ownership before service query"))
    with pytest.raises(ValueError, match="environment changed"):
        backup.run(document, root / "evidence.json")
    assert not (jobs / ".supervisor.lock").exists()


@pytest.mark.parametrize("mutation", ["inside-jobs", "dangling-link", "parent-link", "evidence-overlap"])
def test_output_and_evidence_are_rechecked_when_plan_is_executed(prepared, monkeypatch, mutation):
    root, _, _, jobs = prepared
    document = backup.plan(root / "backup-new")
    evidence = root / "evidence.json"
    if mutation == "inside-jobs":
        document["output"] = str(jobs / "dump")
    elif mutation == "dangling-link":
        (root / "backup-new").symlink_to(root / "missing-target")
    elif mutation == "parent-link":
        (root / "redirect").symlink_to(jobs, target_is_directory=True)
        document["output"] = str(root / "redirect/dump")
    else:
        evidence = root / "backup-new/evidence.json"
    monkeypatch.setattr(backup, "inactive", lambda *_: pytest.fail("reject unsafe output before service query"))
    with pytest.raises(ValueError, match="outside active roots|symbolic-link|separate"):
        backup.run(document, evidence)
    assert not (jobs / ".supervisor.lock").exists()



def test_save_never_follows_predictable_prepare_symlink(tmp_path):
    victim = tmp_path / "preserved.txt"
    victim.write_text("preserve")
    (tmp_path / "new.json.prepare").symlink_to(victim)
    backup.save(tmp_path / "new.json", {"synthetic": True})
    assert victim.read_text() == "preserve"
    assert (tmp_path / "new.json.prepare").is_symlink()
    assert backup.private_json(tmp_path / "new.json") == {"synthetic": True}


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_save_preserves_every_existing_destination(tmp_path, kind):
    target = tmp_path / "record.json"
    victim = tmp_path / "preserve"
    victim.write_text("preserve")
    if kind == "file": target.write_text("foreign record")
    elif kind == "directory": target.mkdir()
    else: target.symlink_to(victim)
    with pytest.raises((OSError, ValueError)):
        backup.save(target, {"must_not_overwrite": True})
    assert victim.read_text() == "preserve"
    if kind == "file": assert target.read_text() == "foreign record"
    elif kind == "directory": assert target.is_dir()
    else: assert target.is_symlink()


def test_private_io_rejects_linked_or_public_parent_without_chmod(tmp_path):
    private = tmp_path / "private"; private.mkdir(mode=0o700)
    alias = tmp_path / "alias"; alias.symlink_to(private, target_is_directory=True)
    with pytest.raises((OSError, ValueError)): backup.save(alias / "record.json", {})
    public = tmp_path / "public"; public.mkdir(mode=0o755)
    public.chmod(0o755)
    assert public.stat().st_mode & 0o777 == 0o755
    before = public.stat().st_mode
    with pytest.raises(ValueError, match="owner-only"): backup.save(public / "record.json", {})
    assert public.stat().st_mode == before and not list(public.iterdir())


def test_private_reader_rejects_hardlinked_metadata(tmp_path):
    import os
    path = tmp_path / "record.json"; backup.save(path, {})
    os.link(path, tmp_path / "second.json")
    with pytest.raises(ValueError, match="unlinked"): backup.private_json(path)


def test_journal_rejects_path_replacement_and_preserves_foreign_bytes(tmp_path):
    target = tmp_path / "evidence.json"; journal = backup.EvidenceJournal(target)
    try:
        journal.publish({"stage": "first"})
        target.rename(tmp_path / "saved-original.json")
        target.write_text("foreign bytes")
        target.chmod(0o600)
        with pytest.raises(ValueError, match="identity changed"): journal.publish({"stage": "second"})
        assert target.read_text() == "foreign bytes"
        assert backup.private_json(tmp_path / "saved-original.json")["stage"] == "first"
    finally: journal.close()


def test_journal_keeps_complete_receipt_when_final_refresh_is_interrupted(tmp_path, monkeypatch):
    target = tmp_path / "evidence.json"; journal = backup.EvidenceJournal(target)
    try:
        journal.publish({"stage": "first"})
        real = backup._write_all
        def fail_final(fd, data):
            if fd == journal.fd: raise OSError("synthetic final refresh interruption")
            return real(fd, data)
        monkeypatch.setattr(backup, "_write_all", fail_final)
        with pytest.raises(OSError, match="synthetic"): journal.publish({"stage": "second"})
        history = tmp_path / journal.history_name
        assert backup.private_json(sorted(history.glob("*.json"))[-1])["stage"] == "second"
        assert history.stat().st_mode & 0o077 == 0
        assert all(path.stat().st_mode & 0o077 == 0 for path in history.iterdir())
    finally: journal.close()


@pytest.mark.parametrize("state,cleanup,scope", [
    ("running", True, None), ("completed", False, None), ("completed", True, {"unit": "synthetic"}),
])
def test_backup_cannot_pass_live_or_unclean_formal_journal(tmp_path, state, cleanup, scope):
    path = tmp_path / "job/attempt/journal.json"
    backup.save(path, {"snapshot": {"status": state, "cleanup_confirmed": cleanup}, "running_scope": scope})
    with pytest.raises(RuntimeError, match="every formal job"): backup.formal_journals(tmp_path)


@pytest.mark.parametrize("level", ["job", "attempt", "journal"])
def test_backup_rejects_journal_links_without_reading_target(tmp_path, level):
    outside = tmp_path / "outside"; outside.mkdir(mode=0o700)
    root = tmp_path / "jobs"; root.mkdir(mode=0o700)
    if level == "job": (root / "job").symlink_to(outside, target_is_directory=True)
    elif level == "attempt":
        (root / "job").mkdir(mode=0o700); (root / "job/attempt").symlink_to(outside, target_is_directory=True)
    else:
        (root / "job/attempt").mkdir(mode=0o700, parents=True)
        (root / "job/attempt/journal.json").symlink_to(outside / "missing.json")
    with pytest.raises(RuntimeError, match="unsafe formal"): backup.formal_journals(root)


def test_terminal_empty_and_clean_journals_are_not_science_execution(tmp_path):
    assert backup.formal_journals(tmp_path) == []
    path = tmp_path / "job/attempt/journal.json"
    backup.save(path, {"snapshot": {"status": "cancelled", "cleanup_confirmed": True}, "running_scope": None})
    assert backup.formal_journals(tmp_path) == [path]


class SyntheticDatabase:
    """No connection or command is executed; model only orchestration outcomes."""
    def __init__(self, mode):
        self.mode = mode; self.calls = []; self.role_created = False; self.role_disabled = False; self.role_dropped = False
    def __call__(self, database, statement, *, result=False):
        self.calls.append((database, statement))
        if "shobj_description" in statement: return "private-owner-marker"
        if "SELECT to_json(EXISTS" in statement: return self.mode == "preexisting"
        if statement.startswith("CREATE ROLE"):
            if self.mode == "role-create-before": raise RuntimeError("synthetic uncertain role result")
            self.role_created = True
            if self.mode == "role-create-after": raise RuntimeError("synthetic uncertain role result")
        elif statement.startswith("CREATE DATABASE"):
            if self.mode == "database-create": raise RuntimeError("synthetic uncertain database result")
        elif statement.startswith("ALTER ROLE"):
            assert "NOLOGIN NOSUPERUSER" in statement
            self.role_disabled = True
        elif statement.startswith("REASSIGN OWNED"):
            assert self.role_disabled
            if self.mode == "reassign-failure": raise RuntimeError("synthetic reassignment failure")
        elif "SELECT count(*) FROM pg_shdepend" in statement: return 2 if self.mode == "dependencies" else 0
        elif statement.startswith("DROP ROLE"):
            assert self.role_disabled
            if self.mode == "drop-failure": raise RuntimeError("synthetic drop failure")
            self.role_dropped = True


@pytest.mark.parametrize("mode", ["success", "backup-failure", "role-create-before", "role-create-after", "database-create", "dependencies", "reassign-failure", "drop-failure", "preexisting"])
def test_synthetic_restore_orchestration_never_loses_cleanup_uncertainty(prepared, monkeypatch, mode):
    from app.auth import backup as platform_backup
    from workers.polymer_md_worker.app.journal import RootLock
    root, _, _, jobs = prepared
    document = backup.plan(root / "new-dump")
    database = SyntheticDatabase(mode); calls = []
    monkeypatch.setattr(backup, "psql", database)
    monkeypatch.setattr(backup, "inactive", lambda *_: None)
    monkeypatch.setattr(backup.secrets, "token_hex", lambda _: "feed" * 16)
    def fake_restore(source_dsn, restore_dsn, output, *, asset_roots):
        assert database.role_created and not database.role_disabled
        assert output == root / "new-dump" and asset_roots == {"polymer_md": jobs}
        calls.append("synthetic restore")
        if mode == "backup-failure": raise RuntimeError("synthetic restore failure")
        return output / "synthetic-receipt.json"
    monkeypatch.setattr(platform_backup, "backup_and_verify", fake_restore)
    evidence = root / "evidence.json"
    if mode == "success":
        value = backup.run(document, evidence)
        assert value["restore_verified"] and value["role_cleanup_confirmed"]
        assert database.role_dropped and calls == ["synthetic restore"]
    else:
        with pytest.raises((RuntimeError, ValueError)): backup.run(document, evidence)
    value = backup.private_json(evidence)
    assert not value["scientific_acceptance"] and not value["credential_persisted"] and not value["drop_owned_used"]
    if mode.startswith("role-create-"):
        assert value["role_creation_uncertain"] and not value["role_cleanup_confirmed"]
        assert not database.role_disabled and not database.role_dropped and calls == []
    elif mode in {"dependencies", "reassign-failure", "drop-failure"}:
        assert not value["role_cleanup_confirmed"] and database.role_disabled and not database.role_dropped
        assert value["cleanup_error_type"] == "RuntimeError"
    elif mode == "database-create":
        assert value["restore_database_creation_uncertain"] and value["role_cleanup_confirmed"] and calls == []
    elif mode == "backup-failure": assert value["role_cleanup_confirmed"] and database.role_dropped
    elif mode == "preexisting": assert not database.role_created and calls == []
    assert not any("DROP OWNED" in sql or "DROP DATABASE" in sql for _, sql in database.calls)
    history = root / value["evidence_history"]
    for path in [evidence, *history.glob("*.json")]:
        text = path.read_text()
        assert "feed" * 16 not in text and "private-owner-marker" not in text and "password=" not in text
    guard = RootLock(jobs); guard.acquire(); guard.release()



def test_manifest_drift_during_lock_acquisition_rejects_before_sql(prepared, monkeypatch):
    from workers.polymer_md_worker.app.journal import RootLock
    root, runtime, _, _ = prepared
    document = backup.plan(root / "dump")
    monkeypatch.setattr(backup, "inactive", lambda *_: None)
    real = RootLock.acquire
    def drift(lock):
        real(lock)
        (runtime / "manifest.json").write_text(json.dumps({"runtime_fingerprint": "b" * 64}))
    monkeypatch.setattr(RootLock, "acquire", drift)
    with pytest.raises(ValueError, match="runtime changed"): backup.run(document, root / "evidence.json")
    assert not (root / "evidence.json").exists()


def test_backup_rejects_nonprivate_output_parent_before_sql(prepared, monkeypatch):
    root, _, _, _ = prepared
    parent = root / "public-output"; parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    assert parent.stat().st_mode & 0o777 == 0o755
    document = backup.plan(parent / "dump")
    monkeypatch.setattr(backup, "inactive", lambda *_: None)
    with pytest.raises(ValueError, match="owner-only"): backup.run(document, root / "evidence.json")
    assert not list(parent.iterdir()) and not (root / "evidence.json").exists()


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
    monkeypatch.setattr(backup, "_main", dispatch)
    try:
        if outcome == "exception":
            with pytest.raises(RuntimeError, match="synthetic dispatch failure"): backup.main()
        else:
            assert backup.main() == 17
    finally:
        observed = os.umask(0o077)
        os.umask(previous)
    assert observed == 0o027


def test_cli_parameter_error_restores_creation_mask(monkeypatch):
    import os
    import sys
    previous = os.umask(0o027)
    monkeypatch.setattr(sys, "argv", ['polymer_md_backup_dev.py', 'plan', '--plan-file', 'unused.json'])
    try:
        with pytest.raises(SystemExit) as captured: backup.main()
        assert captured.value.code == 2
    finally:
        observed = os.umask(0o077)
        os.umask(previous)
    assert observed == 0o027

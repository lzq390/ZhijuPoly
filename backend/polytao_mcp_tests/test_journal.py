from __future__ import annotations

import sqlite3

import pytest

from app.polytao_mcp_journal import JournalError, SubmissionJournal


def test_intent_replay_conflict_owner_and_retention(configuration):
    journal = SubmissionJournal(configuration.journal_file, max_entries=1)
    record, fresh = journal.begin("client", "account", "key", {"a": 1})
    assert fresh and record["state"] == "dispatching"
    assert not journal.begin("client", "account", "key", {"a": 1})[1]
    assert journal.begin("client", "account", "key", {"a": 2})[0]["conflict"]
    journal.finish("client", "account", "key", state="accepted", job_id="polytao.x.y", job_status="pending")
    assert journal.owns_job("client", "account", "polytao.x.y")
    assert not journal.owns_job("other", "account", "polytao.x.y")
    assert not journal.owns_job("client", "other", "polytao.x.y")
    assert journal.begin("client", "account", "key2", {})[0]["state"] == "capacity"
    journal.close()
    journal = SubmissionJournal(configuration.journal_file)
    assert journal.begin("client", "account", "key", {"a": 1})[0]["job_id"] == "polytao.x.y"
    journal.close()


def test_crashed_dispatch_becomes_unknown_and_single_process_lock(configuration):
    journal = SubmissionJournal(configuration.journal_file)
    journal.begin("c", "a", "k", {})
    with pytest.raises(JournalError):
        SubmissionJournal(configuration.journal_file)
    journal.close()
    journal = SubmissionJournal(configuration.journal_file)
    assert journal.begin("c", "a", "k", {})[0]["state"] == "outcome_unknown"
    journal.close()


def test_backend_account_binding_cannot_change(configuration):
    journal = SubmissionJournal(configuration.journal_file, backend_url="http://one", account_uuid="account")
    journal.close()
    with pytest.raises(JournalError):
        SubmissionJournal(configuration.journal_file, backend_url="http://two", account_uuid="account")
    journal = SubmissionJournal(configuration.journal_file, backend_url="http://one", account_uuid="account")
    journal.close()


def test_missing_replaced_corrupted_and_failed_write_fail_closed(configuration):
    journal = SubmissionJournal(configuration.journal_file)
    configuration.journal_file.unlink()
    with pytest.raises(JournalError):
        journal.begin("c", "a", "k", {})
    assert journal.failed
    journal.close()
    with pytest.raises((JournalError, OSError)):
        SubmissionJournal.initialize(configuration.journal_file)


def test_failed_update_preserves_intent(configuration):
    journal = SubmissionJournal(configuration.journal_file)
    journal.begin("c", "a", "k", {})
    with sqlite3.connect(configuration.journal_file) as db:
        db.execute("CREATE TRIGGER reject_update BEFORE UPDATE ON submissions BEGIN SELECT RAISE(FAIL, 'test'); END")
    with pytest.raises(JournalError):
        journal.finish("c", "a", "k", state="accepted", job_id="job", job_status="pending")
    assert journal.failed
    journal.close()
    with sqlite3.connect(configuration.journal_file) as db:
        db.execute("DROP TRIGGER reject_update")
    journal = SubmissionJournal(configuration.journal_file)
    assert journal.begin("c", "a", "k", {})[0]["state"] == "outcome_unknown"
    journal.close()
